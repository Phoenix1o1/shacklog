"""Search everything: full-text hits grouped by record type, plus smart shortcuts.

The hits come from the FTS5 index (``services/search.py``). Before them, the page offers
shortcuts for what radio people type into a search box:

* a **callsign** already in the log or the listening log → its callsign page and QSO list;
* a **frequency** ("145.650", "145,65", "430500" in kHz) → repeaters whose output or input
  is within ±5 kHz, CHIRP channels on it, and the log entries around it;
* a **locator** ("JN45", "JN45ol") → QSOs, repeaters and listening entries near it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flask import Blueprint, render_template, request, url_for
from sqlalchemy import func, or_

from shacklog.models import (
    ENTITIES,
    Contact,
    Qso,
    RadioChannel,
    RadioConfig,
    Repeater,
    SwlEntry,
)
from shacklog.services import bands, geo
from shacklog.services import search as search_service
from shacklog.web import helpers as h
from shacklog.web.views.swl import parse_frequency_text

bp = Blueprint("search", __name__, url_prefix="/search")

#: Tabs: (key, label, entity names). "all" has no restriction.
KINDS: list[tuple[str, str, list[str] | None]] = [
    ("all", "All", None),
    ("qso", "QSOs", ["qso"]),
    ("swl", "Listening", ["swl"]),
    ("journal", "Journal", ["journal"]),
    ("repeater", "Repeaters", ["repeater"]),
    ("station", "Stations", ["station"]),
    ("equipment", "Equipment", ["equipment"]),
    ("contact", "Contacts", ["contact"]),
    ("file", "Files", ["file"]),
    ("radio_config", "Configs", ["radio_config"]),
]
#: Most hits fetched for one search (counts above it show as "1000+").
MAX_HITS = 1000
PER_PAGE = 50
#: Frequency tolerance of the shortcut: a repeater "on 145.650" may be stored as 145.6500
#: or 145.6525 (12.5 kHz raster), and people round.
FREQ_TOLERANCE_MHZ = 0.005
NEAR_RADIUS_KM = 50

_CALL_RE = re.compile(r"^(?:[A-Z0-9]{1,4}/)?[A-Z0-9]{1,3}[0-9][A-Z0-9]{0,3}[A-Z](?:/[A-Z0-9]{1,4})?$")


def looks_like_callsign(text: str) -> bool:
    """Amateur callsign shape: prefix, a digit, a suffix ending in a letter (IK2ABC, F/IU2TST/P)."""
    return bool(_CALL_RE.match(text)) and any(c.isalpha() for c in text.split("/")[0])


def frequency_of(text: str) -> float | None:
    """The frequency a search means, or None. Plain numbers only, within 0.1–3000 MHz."""
    freq = parse_frequency_text(text)
    if freq is None or not 0.1 <= freq <= 3000:
        return None
    return freq


@dataclass
class Shortcuts:
    call: dict | None = None
    freq: dict | None = None
    locator: dict | None = None

    @property
    def any(self) -> bool:
        return bool(self.call or self.freq or self.locator)


def _call_shortcut(text: str) -> dict | None:
    call = text.replace(" ", "").upper()
    if not looks_like_callsign(call):
        return None
    qsos = h.live(Qso).filter(Qso.call == call)
    n_qso = qsos.count()
    n_swl = h.live(SwlEntry).filter(func.upper(SwlEntry.heard) == call).count()
    if not n_qso and not n_swl:
        return None
    return {
        "call": call, "qsos": n_qso, "heard": n_swl,
        "last": qsos.order_by(Qso.time_on.desc()).first(),
        "contact": h.live(Contact).filter(Contact.callsign == call).first(),
    }


def _freq_shortcut(text: str) -> dict | None:
    freq = frequency_of(text)
    if freq is None:
        return None
    lo, hi = freq - FREQ_TOLERANCE_MHZ, freq + FREQ_TOLERANCE_MHZ
    repeaters = (h.live(Repeater)
                 .filter(or_(Repeater.output_mhz.between(lo, hi), Repeater.input_mhz.between(lo, hi)))
                 .order_by(Repeater.name).limit(50).all())
    channels = (h.session().query(RadioChannel, RadioConfig)
                .join(RadioConfig, RadioConfig.id == RadioChannel.config_id)
                .filter(RadioChannel.deleted_at.is_(None), RadioConfig.deleted_at.is_(None),
                        RadioChannel.freq_mhz.between(lo, hi))
                .order_by(RadioConfig.is_current.desc(), RadioConfig.title, RadioChannel.location)
                .limit(50).all())
    n_swl = h.live(SwlEntry).filter(SwlEntry.freq_mhz.between(lo, hi)).count()
    n_qso = h.live(Qso).filter(or_(Qso.freq_mhz.between(lo, hi), Qso.freq_rx_mhz.between(lo, hi))).count()
    explicit = bool(re.search(r"[.,]|[a-z]", text, re.I))
    if not (repeaters or channels or n_swl or n_qso or explicit):
        return None  # a bare number like "2026" that matches nothing isn't worth a card
    return {
        "mhz": freq, "band": bands.band_for_freq(freq), "service": bands.service_hint(freq),
        "repeaters": repeaters, "channels": channels, "swl": n_swl, "qsos": n_qso,
        "lo": round(lo, 6), "hi": round(hi, 6),
    }


def _locator_shortcut(text: str) -> dict | None:
    if len(text) < 4 or not geo.is_valid_locator(text):
        return None
    loc = geo.normalize_locator(text)
    lat, lon = geo.locator_to_latlon(loc)
    return {"locator": loc, "lat": lat, "lon": lon, "radius": NEAR_RADIUS_KM}


def shortcuts_for(text: str) -> Shortcuts:
    text = (text or "").strip()
    if not text or len(text) > 40:
        return Shortcuts()
    return Shortcuts(call=_call_shortcut(text), freq=_freq_shortcut(text),
                     locator=_locator_shortcut(text))


@dataclass
class Results:
    hits: list = field(default_factory=list)  # hits of the current tab and page
    counts: dict = field(default_factory=dict)  # tab key -> count
    capped: bool = False
    page: int = 1
    pages: int = 1
    first_index: int = 0


def run_search(text: str, kind: str, page: int) -> Results:
    hits = search_service.query(h.session(), text, limit=MAX_HITS + 1)
    capped = len(hits) > MAX_HITS
    hits = hits[:MAX_HITS]
    counts = {"all": len(hits)}
    for key, _label, entities in KINDS[1:]:
        counts[key] = sum(1 for hit in hits if hit.entity in entities)
    selected = next((e for k, _l, e in KINDS if k == kind), None)
    if selected is not None:
        hits = [hit for hit in hits if hit.entity in selected]
    pages = max(1, -(-len(hits) // PER_PAGE))
    page = min(max(1, page), pages)
    start = (page - 1) * PER_PAGE
    return Results(hits[start:start + PER_PAGE], counts, capped, page, pages,
                   start + 1 if hits else 0)


@bp.route("", strict_slashes=False)
def index():
    text = (request.args.get("q") or "").strip()
    kind = request.args.get("kind") or "all"
    if kind not in {k for k, _l, _e in KINDS}:
        kind = "all"
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    results = run_search(text, kind, page) if text else Results()
    labels = {name: model.__label__ for name, model in ENTITIES.items()}
    return render_template(
        "search/index.html", q=text, kind=kind, kinds=KINDS, results=results,
        shortcuts=shortcuts_for(text), labels=labels, per_page=PER_PAGE, max_hits=MAX_HITS,
        page_url=lambda p: url_for(".index", q=text, kind=kind, page=p),
    )
