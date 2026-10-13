"""Country (DXCC entity), CQ and ITU zone and continent of a callsign, from AD1C's
country files (https://www.country-files.com/).

Two formats of the same data are understood:

``cty.dat``
    The classic file used by contest loggers. One entity per header line::

        Italy:   15:  28:  EU:   42.82:   -12.58:    -1.0:  I:
            I,=II0GDF/J,=IU0HTJ(15)[28];

    Fields: name, CQ zone, ITU zone, continent, latitude, longitude (**west positive**),
    UTC offset (**west positive**), primary prefix (``*`` = not a DXCC entity, only on
    the WAE list). Then the prefixes and exact calls (``=CALL``) of the entity, ending
    with ``;``. Each may carry overrides: ``(CQ)`` ``[ITU]`` ``<lat/lon>`` ``{continent}``
    ``~utc offset~``.

``cty.csv``
    The same entities, one per line, plus the **ADIF DXCC entity number**::

        I,Italy,248,EU,15,28,42.82,-12.58,-1.0,I =II0GDF/J =IU0HTJ(15)[28];

This is the version ShackLog downloads, because the DXCC number is what ADIF and awards
use. The file is reference data of *this* instance (``<data>/reference/``): it doesn't
sync, each instance downloads its own copy (Settings › Data).

The lookup is pure (no Flask). :func:`configure` tells the module which data folder to
read; :func:`lookup` then answers from a cached table, reloaded when the file changes.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

SOURCE_URL = "https://www.country-files.com/cty/cty.csv"
FILE_NAMES = ("cty.csv", "cty.dat")  # in order of preference
MAX_AGE_DAYS = 30  # AD1C publishes updates every few weeks
MAX_SIZE = 5_000_000  # the real files are ~100-200 kB

# Portable designators that say nothing about the country.
IGNORED_SUFFIXES = {"P", "M", "QRP", "QRPP", "A", "LH", "PM", "AE", "AG", "KT"}
# Maritime / aeronautical mobile: not in any DXCC entity.
NO_ENTITY_SUFFIXES = {"MM", "AM"}


@dataclass(frozen=True)
class Entity:
    """One country of the list, or one of its exceptions (zones/position overridden)."""

    name: str
    prefix: str  # primary prefix, e.g. "I", "EA8"
    dxcc: int | None  # ADIF DXCC entity number (cty.csv only)
    continent: str
    cq_zone: int
    itu_zone: int
    latitude: float  # north positive
    longitude: float  # EAST positive (the files use west positive; converted on load)
    utc_offset: float  # hours EAST of UTC (converted too)

    @property
    def label(self) -> str:
        """Name and zones in one short line, e.g. "Italy · EU · CQ 15 · ITU 28"."""
        return f"{self.name} · {self.continent} · CQ {self.cq_zone} · ITU {self.itu_zone}"


class CountryTable:
    """Prefix and exact-call tables of one country file."""

    def __init__(self, entities: list[Entity], prefixes: dict[str, Entity],
                 exact: dict[str, Entity], version: str = ""):
        self.entities = entities
        self.prefixes = prefixes
        self.exact = exact
        self.version = version
        self._longest = max((len(p) for p in prefixes), default=0)

    def __len__(self) -> int:
        return len(self.entities)

    def lookup(self, call: str | None) -> Entity | None:
        """The entity of ``call`` (exceptions applied), or None if it can't be told."""
        call = normalize_call(call)
        if not call:
            return None
        if call in self.exact:
            return self.exact[call]
        where = location_part(call)
        if where is None:
            return None
        if where in self.exact:  # e.g. "4U1UN" given as the location of a portable call
            return self.exact[where]
        for n in range(min(len(where), self._longest), 0, -1):
            found = self.prefixes.get(where[:n])
            if found is not None:
                return found
        return None


# ---------------------------------------------------------------------------
# Callsigns
# ---------------------------------------------------------------------------

def normalize_call(call: str | None) -> str:
    return re.sub(r"\s+", "", call or "").upper().strip("/")


_CALL_PARTS = re.compile(r"^([A-Z0-9]*?)(\d+)([A-Z]+)$")


def location_part(call: str) -> str | None:
    """The part of a callsign that says where the station is.

    ``DL1ABC`` → ``DL1ABC``; ``EA8/DL1ABC`` and ``DL1ABC/EA8`` → ``EA8``;
    ``DL1ABC/P`` → ``DL1ABC``; ``W1ABC/4`` → ``W4ABC`` (another call area);
    ``DL1ABC/MM`` → None (maritime mobile: no country).
    """
    parts = [p for p in call.split("/") if p]
    if not parts:
        return None
    if any(p in NO_ENTITY_SUFFIXES for p in parts[1:]):
        return None
    parts = [parts[0]] + [p for p in parts[1:] if p not in IGNORED_SUFFIXES]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and len(parts[1]) == 1 and parts[1].isdigit():
        match = _CALL_PARTS.match(parts[0])
        if match:
            return f"{match.group(1)}{parts[1]}{match.group(3)}"
        return parts[0]
    # A prefix added before or after the home call: the shorter part (first if equal).
    return min(parts, key=len)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_ALIAS = re.compile(r"^(=?)([A-Z0-9/]+)(.*)$")
_CQ = re.compile(r"\((\d+)\)")
_ITU = re.compile(r"\[(\d+)\]")
_POS = re.compile(r"<(-?[\d.]+)/(-?[\d.]+)>")
_CONT = re.compile(r"\{([A-Z]{2})\}")
_TZ = re.compile(r"~(-?[\d.]+)~")
_VERSION = re.compile(r"^=?VER(\d{8})$")
# The name may contain a comma: anchor on the numeric fields that follow it.
_CSV_LINE = re.compile(r"^([^,]*),(.*?),(\d*),([A-Za-z]{2}),(\d+),(\d+),(-?[\d.]+),"
                       r"(-?[\d.]+),(-?[\d.]+),(.*)$")


def _with_overrides(entity: Entity, extra: str) -> Entity:
    """Apply ``(CQ)[ITU]<lat/lon>{CONT}~tz~`` written after one prefix or call."""
    if not extra:
        return entity
    changes: dict = {}
    if m := _CQ.search(extra):
        changes["cq_zone"] = int(m.group(1))
    if m := _ITU.search(extra):
        changes["itu_zone"] = int(m.group(1))
    if m := _POS.search(extra):
        changes["latitude"], changes["longitude"] = float(m.group(1)), -float(m.group(2))
    if m := _CONT.search(extra):
        changes["continent"] = m.group(1)
    if m := _TZ.search(extra):
        changes["utc_offset"] = -float(m.group(1))
    return replace(entity, **changes) if changes else entity


def _add_aliases(entity: Entity, aliases: list[str], prefixes: dict, exact: dict) -> str:
    """Register an entity's prefixes and exact calls. Returns the file version if one of
    them is the ``VERyyyymmdd`` marker AD1C puts in every release."""
    version = ""
    for raw in aliases:
        token = raw.strip().upper()
        if not token:
            continue
        if m := _VERSION.match(token):
            version = m.group(1)
            continue
        match = _ALIAS.match(token)
        if not match:
            continue
        is_exact, text, extra = match.groups()
        (exact if is_exact else prefixes)[text] = _with_overrides(entity, extra)
    return version


def _entity(name, prefix, dxcc, cont, cq, itu, lat, lon, tz) -> Entity:
    return Entity(name=name.strip(), prefix=prefix.strip().lstrip("*"), dxcc=dxcc,
                  continent=cont.strip().upper(), cq_zone=int(cq), itu_zone=int(itu),
                  latitude=float(lat), longitude=-float(lon), utc_offset=-float(tz))


def parse_cty_dat(text: str, include_wae: bool = False) -> CountryTable:
    """Parse the classic ``cty.dat`` format (see the module docstring)."""
    entities: list[Entity] = []
    prefixes: dict[str, Entity] = {}
    exact: dict[str, Entity] = {}
    version = ""
    current: Entity | None = None
    skip = False
    pending: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():  # header line of an entity
            fields = [f.strip() for f in line.split(":")]
            if len(fields) < 8:
                raise ValueError(f"Not a cty.dat header line: {line[:60]!r}")
            skip = fields[7].startswith("*") and not include_wae
            name, cq, itu, cont, lat, lon, tz, prefix = fields[:8]
            current = _entity(name, prefix, None, cont, cq, itu, lat, lon, tz)
            if not skip:
                entities.append(current)
            pending = []
            continue
        if current is None:
            raise ValueError("cty.dat: prefixes before the first entity")
        body = line.strip()
        pending.extend(body.rstrip(";").split(","))
        if body.endswith(";"):
            if not skip:
                version = _add_aliases(current, pending, prefixes, exact) or version
            pending = []
    if not entities:
        raise ValueError("No countries found in the file.")
    return CountryTable(entities, prefixes, exact, version)


def parse_cty_csv(text: str, include_wae: bool = False) -> CountryTable:
    """Parse ``cty.csv``: prefix, name, DXCC number, continent, CQ, ITU, lat, lon, UTC
    offset, then the prefixes separated by spaces and ending with ``;``."""
    entities: list[Entity] = []
    prefixes: dict[str, Entity] = {}
    exact: dict[str, Entity] = {}
    version = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _CSV_LINE.match(line.strip())
        if not match:
            raise ValueError(f"Not a cty.csv line: {line[:60]!r}")
        prefix, name, dxcc, cont, cq, itu, lat, lon, tz, aliases = match.groups()
        if prefix.strip().startswith("*") and not include_wae:
            continue
        entity = _entity(name, prefix, int(dxcc) if dxcc.strip().isdigit() else None,
                         cont, cq, itu, lat, lon, tz)
        entities.append(entity)
        tokens = re.split(r"[\s,]+", aliases.strip().rstrip(";"))
        version = _add_aliases(entity, tokens, prefixes, exact) or version
    if not entities:
        raise ValueError("No countries found in the file.")
    return CountryTable(entities, prefixes, exact, version)


def parse(text: str) -> CountryTable:
    """Either format, told apart by the first line (``cty.dat`` headers use colons)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if first.count(":") >= 7:
        return parse_cty_dat(text)
    return parse_cty_csv(text)


# ---------------------------------------------------------------------------
# The installed file
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_data_dir: Path | None = None
_cache: tuple[Path | None, float, CountryTable | None] = (None, 0.0, None)


def configure(data_dir: Path | None) -> None:
    """Use the country file of this data folder for :func:`lookup`."""
    global _data_dir
    _data_dir = Path(data_dir) if data_dir else None


def reference_dir(data_dir: Path) -> Path:
    from shacklog.config import Paths

    return Paths(Path(data_dir)).reference


def installed_file(data_dir: Path) -> Path | None:
    for name in FILE_NAMES:
        path = reference_dir(data_dir) / name
        if path.is_file():
            return path
    return None


def load(data_dir: Path | None = None) -> CountryTable | None:
    """The table of the installed file (cached until the file changes), or None."""
    global _cache
    data_dir = data_dir or _data_dir
    if data_dir is None:
        return None
    path = installed_file(data_dir)
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _lock:
        cached_path, cached_mtime, table = _cache
        if cached_path == path and cached_mtime == mtime:
            return table
        try:
            table = parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            log.exception("Couldn't read the country file %s", path)
            table = None
        _cache = (path, mtime, table)
        return table


def lookup(call: str | None) -> Entity | None:
    """Entity of ``call`` from the configured data folder; None without a country file."""
    table = load()
    return table.lookup(call) if table is not None else None


def install(data_dir: Path, content: bytes, filename: str = "cty.csv") -> CountryTable:
    """Check and install a country file (from the download or an upload).

    Raises ValueError if it isn't a country file; the installed one is then untouched.
    """
    if len(content) > MAX_SIZE:
        raise ValueError("That file is far too big for a country file.")
    text = content.decode("utf-8", errors="replace")
    table = parse(text)
    if len(table) < 100:  # the real list has ~340 entities
        raise ValueError(f"Only {len(table)} countries in that file: not a complete list.")
    name = "cty.dat" if text.lstrip().split("\n", 1)[0].count(":") >= 7 else "cty.csv"
    folder = reference_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, folder / name)
    for other in FILE_NAMES:  # one file only, so it's clear which one is used
        if other != name:
            (folder / other).unlink(missing_ok=True)
    return table


def download(data_dir: Path, url: str = SOURCE_URL, timeout: int = 30) -> CountryTable:
    """Fetch the current list from country-files.com and install it."""
    request = urllib.request.Request(url, headers={"User-Agent": "ShackLog"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        content = resp.read(MAX_SIZE + 1)
    return install(data_dir, content, Path(url).name)


def update_if_due(data_dir: Path, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """Download the list when there is none or it is older than ``max_age_days``.
    Best effort (offline is fine). True if a new file was installed."""
    path = installed_file(data_dir)
    if path is not None and time.time() - path.stat().st_mtime < max_age_days * 86400:
        return False
    try:
        table = download(data_dir)
    except Exception as exc:  # offline, site down, bad file: try again next time
        log.info("Country list not updated: %s", exc)
        return False
    log.info("Country list updated: %d countries (version %s)", len(table), table.version or "?")
    return True


def info(data_dir: Path) -> dict:
    """For Settings › Data: which file, how many countries, version, when installed."""
    path = installed_file(data_dir)
    if path is None:
        return {"installed": False}
    table = load(data_dir)
    return {
        "installed": table is not None, "file": path.name,
        "countries": len(table) if table else 0,
        "version": (table.version if table else "") or "",
        "updated": time.strftime("%Y-%m-%d", time.gmtime(path.stat().st_mtime)),
    }
