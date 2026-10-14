"""Callbook lookups (HamQTH, QRZ.com) with a local cache.

Who is behind a callsign: name, QTH, locator, postal address, QSL manager. Two services
answer over XML:

* **HamQTH** (https://www.hamqth.com) – free with an account.
* **QRZ.com** XML data – full records need an XML subscription; without one QRZ answers
  with a few fields only.

Credentials are local settings of this instance (``callbook_*`` in prefs.py): they never
sync and are never logged. Answers are kept in ``<data>/reference/callbook.sqlite3`` (a
cache, not part of the logbook: it doesn't sync either and can be deleted any time) for
:data:`CACHE_DAYS`; "not found" answers for a day. The QSO form shows cached details at
once and asks the service only when you press *Look up*, so typing a callsign letter by
letter never spends lookups.

Pure module (no Flask). Network access goes through ``opener`` so tests can replace it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shacklog import APP_NAME, __version__

log = logging.getLogger(__name__)

SERVICES = [("", "None"), ("hamqth", "HamQTH"), ("qrz", "QRZ.com (XML)")]
CACHE_DAYS = 30
NOT_FOUND_HOURS = 24
TIMEOUT_S = 10
HAMQTH_URL = "https://www.hamqth.com/xml.php"
QRZ_URL = "https://xmldata.qrz.com/xml/current/"
AGENT = f"{APP_NAME}-{__version__}"


class CallbookError(Exception):
    """The service can't be used: wrong login, no subscription, offline…"""


@dataclass
class Entry:
    """What a callbook says about a callsign (empty strings when it doesn't say)."""

    call: str
    name: str = ""
    qth: str = ""
    locator: str = ""
    country: str = ""
    address: list[str] = field(default_factory=list)  # postal address, line by line
    email: str = ""
    qsl_via: str = ""
    latitude: float | None = None
    longitude: float | None = None
    source: str = ""  # "HamQTH" or "QRZ.com"
    fetched: float = 0.0  # time.time() of the lookup (set by the cache)

    @property
    def fetched_day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.fetched)) if self.fetched else ""

    def form_values(self) -> dict[str, str]:
        """Values for the QSO form's empty boxes."""
        return {k: v for k, v in (("name", self.name), ("qth", self.qth),
                                  ("locator", self.locator), ("qsl_via", self.qsl_via)) if v}


def _text(node, tag: str, ns: str) -> str:
    found = node.find(f"{ns}{tag}") if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def _float(text: str) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _ns(root) -> str:
    return root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""


# ---------------------------------------------------------------------------
# HamQTH
# ---------------------------------------------------------------------------

def parse_hamqth_session(xml: bytes) -> str:
    root = ET.fromstring(xml)
    ns = _ns(root)
    session = root.find(f"{ns}session")
    key = _text(session, "session_id", ns)
    if not key:
        raise CallbookError(_text(session, "error", ns) or "HamQTH refused the login.")
    return key


def parse_hamqth(xml: bytes, call: str) -> Entry | None:
    """An Entry, or None if HamQTH doesn't know the callsign. Raises CallbookError when
    the session expired or another error came back."""
    root = ET.fromstring(xml)
    ns = _ns(root)
    search = root.find(f"{ns}search")
    if search is None:
        error = _text(root.find(f"{ns}session"), "error", ns)
        if "not found" in error.lower():
            return None
        raise CallbookError(error or "Unexpected answer from HamQTH.")
    t = lambda tag: _text(search, tag, ns)  # noqa: E731
    address = [line for line in (t("adr_name"), t("adr_street1"), t("adr_street2"),
                                 " ".join(x for x in (t("adr_zip"), t("adr_city")) if x),
                                 t("adr_country")) if line]
    return Entry(call=t("callsign").upper() or call, name=t("adr_name") or t("nick"),
                 qth=t("qth") or t("adr_city"), locator=t("grid"),
                 country=t("country") or t("adr_country"), address=address, email=t("email"),
                 qsl_via=t("qsl_via"), latitude=_float(t("latitude")),
                 longitude=_float(t("longitude")), source="HamQTH")


# ---------------------------------------------------------------------------
# QRZ.com
# ---------------------------------------------------------------------------

def parse_qrz(xml: bytes, call: str) -> tuple[Entry | None, str]:
    """(Entry or None if not found, session key to reuse). Raises CallbookError."""
    root = ET.fromstring(xml)
    ns = _ns(root)
    session = root.find(f"{ns}Session")
    key, error = _text(session, "Key", ns), _text(session, "Error", ns)
    record = root.find(f"{ns}Callsign")
    if record is None:
        if error.lower().startswith("not found"):
            return None, key
        raise CallbookError(error or "Unexpected answer from QRZ.com.")
    t = lambda tag: _text(record, tag, ns)  # noqa: E731
    name = " ".join(x for x in (t("fname"), t("name")) if x)
    address = [line for line in (name, t("addr1"),
                                 " ".join(x for x in (t("zip"), t("addr2"), t("state")) if x),
                                 t("country")) if line]
    return Entry(call=t("call").upper() or call, name=name, qth=t("addr2"), locator=t("grid"),
                 country=t("country"), address=address, email=t("email"),
                 qsl_via=t("qslmgr"), latitude=_float(t("lat")), longitude=_float(t("lon")),
                 source="QRZ.com"), key


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class Cache:
    """``<data>/reference/callbook.sqlite3``: one JSON answer per callsign."""

    def __init__(self, data_dir: Path):
        from shacklog.config import Paths

        self.path = Paths(Path(data_dir)).reference / "callbook.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS answers (call TEXT PRIMARY KEY, "
                         "fetched REAL NOT NULL, found INTEGER NOT NULL, data TEXT)")
        except sqlite3.Error:
            conn.close()  # Windows keeps an open file locked
            raise
        return conn

    def get(self, call: str, max_age_s: float | None = None) -> tuple[bool, Entry | None]:
        """(known?, entry). ``known`` is True for a fresh "not found" too."""
        if not self.path.is_file():
            return False, None
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute("SELECT fetched, found, data FROM answers WHERE call = ?",
                                   (call,)).fetchone()
        except sqlite3.Error:
            return False, None
        if row is None:
            return False, None
        fetched, found, data = row
        age = time.time() - fetched
        limit = max_age_s if max_age_s is not None else (
            CACHE_DAYS * 86400 if found else NOT_FOUND_HOURS * 3600)
        if age > limit:
            return False, None
        if not found:
            return True, None
        values = json.loads(data)
        values["fetched"] = fetched
        return True, Entry(**values)

    def put(self, call: str, entry: Entry | None) -> None:
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?)",
                             (call, time.time(), int(entry is not None),
                              json.dumps(asdict(entry)) if entry else None))
        except sqlite3.Error:
            log.warning("Couldn't write the callbook cache")

    def stats(self) -> tuple[int, int]:
        """(callsigns cached, file size)."""
        if not self.path.is_file():
            return 0, 0
        try:
            with closing(self._connect()) as conn, conn:
                count = conn.execute("SELECT COUNT(*) FROM answers WHERE found = 1").fetchone()[0]
        except sqlite3.Error:
            count = 0
        return count, self.path.stat().st_size

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_sessions: dict[tuple[str, str], tuple[str, float]] = {}  # (service, user) → (key, time)
_session_lock = threading.Lock()
SESSION_S = 50 * 60  # HamQTH sessions last an hour; QRZ keys about a day


class Callbook:
    """One configured service plus the cache of one data folder."""

    def __init__(self, data_dir: Path, service: str, user: str, password: str, opener=None):
        self.cache = Cache(data_dir)
        self.service = service if service in dict(SERVICES) else ""
        self.user, self.password = user or "", password or ""
        self._open = opener or urllib.request.urlopen

    @property
    def configured(self) -> bool:
        return bool(self.service and self.user and self.password)

    @property
    def label(self) -> str:
        return dict(SERVICES).get(self.service, "")

    def cached(self, call: str) -> Entry | None:
        return self.cache.get(_norm(call))[1]

    def lookup(self, call: str, refresh: bool = False) -> Entry | None:
        """Cached answer, else ask the service. Raises CallbookError if it can't."""
        call = _norm(call)
        if not call:
            return None
        if not refresh:
            known, entry = self.cache.get(call)
            if known:
                return entry
        if not self.configured:
            raise CallbookError("No callbook is set up (Settings › General › Callbook).")
        entry = self._ask(call)
        self.cache.put(call, entry)
        return entry

    def test(self) -> str:
        """Log in once; returns a short confirmation or raises CallbookError."""
        self._session(force=True)
        return f"Logged in to {self.label} as {self.user}."

    # -- service calls -------------------------------------------------------

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with self._open(request, timeout=TIMEOUT_S) as resp:
                return resp.read(1_000_000)
        except Exception as exc:  # offline, DNS, TLS, HTTP error: same message
            raise CallbookError(f"{self.label} can't be reached ({exc}).") from exc

    def _session(self, force: bool = False) -> str:
        key_id = (self.service, self.user)
        with _session_lock:
            cached = _sessions.get(key_id)
            if cached and not force and time.time() - cached[1] < SESSION_S:
                return cached[0]
        try:
            key = self._login()
        except ET.ParseError as exc:
            raise CallbookError(f"{self.label} sent an answer that isn't XML.") from exc
        with _session_lock:
            _sessions[key_id] = (key, time.time())
        return key

    def _login(self) -> str:
        if self.service == "hamqth":
            query = urllib.parse.urlencode({"u": self.user, "p": self.password})
            return parse_hamqth_session(self._get(f"{HAMQTH_URL}?{query}"))
        query = urllib.parse.urlencode({"username": self.user, "password": self.password,
                                        "agent": AGENT})
        return parse_qrz_login(self._get(f"{QRZ_URL}?{query}"))

    def _ask(self, call: str) -> Entry | None:
        for attempt in (1, 2):  # a second time with a fresh session if it expired
            key = self._session(force=attempt == 2)
            try:
                if self.service == "hamqth":
                    query = urllib.parse.urlencode({"id": key, "callsign": call, "prg": APP_NAME})
                    return parse_hamqth(self._get(f"{HAMQTH_URL}?{query}"), call)
                query = urllib.parse.urlencode({"s": key, "callsign": call})
                entry, new_key = parse_qrz(self._get(f"{QRZ_URL}?{query}"), call)
                if new_key and new_key != key:
                    with _session_lock:
                        _sessions[(self.service, self.user)] = (new_key, time.time())
                return entry
            except CallbookError as exc:
                if attempt == 1 and "session" in str(exc).lower():
                    continue
                raise
            except ET.ParseError as exc:
                raise CallbookError(f"{self.label} sent an answer that isn't XML.") from exc
        return None


def parse_qrz_login(xml: bytes) -> str:
    """The session key from a QRZ login answer. Raises CallbookError on a refusal."""
    root = ET.fromstring(xml)
    ns = _ns(root)
    session = root.find(f"{ns}Session")
    key = _text(session, "Key", ns)
    if not key:
        raise CallbookError(_text(session, "Error", ns) or "QRZ.com refused the login.")
    return key


def _norm(call: str | None) -> str:
    return "".join((call or "").split()).upper()
