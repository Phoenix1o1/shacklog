"""Paper QSL cards: what to send, what's out, what came back.

The status fields are the ADIF ones already on every QSO: ``qsl_sent`` / ``qsl_rcvd``
(``R`` requested, ``Q`` queued, ``Y`` yes, ``N`` no, ``I`` ignore) and ``qsl_via`` (the
QSL manager). Dates and routes have ADIF fields too: ``QSLSDATE``, ``QSLRDATE``,
``QSL_SENT_VIA``, ``QSL_RCVD_VIA`` (``B`` bureau, ``D`` direct, ``E`` electronic,
``M`` manager). Those live in ``Qso.extra_adif`` like any other ADIF field without a
column, so they travel with sync and come out in ADIF exports. No schema change.

The workflow:

* **To send** – ``qsl_sent`` is ``R`` or ``Q``. Print address labels, then mark sent.
* **Waiting** – sent (``Y``) but not received.
* **Card arrived** – mark the QSOs on the card received; the scan can be linked to them.

Pure logic (no Flask).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

TO_SEND = ("R", "Q")
ROUTES = [("B", "Bureau"), ("D", "Direct"), ("M", "Via the manager"), ("E", "Electronic")]
ROUTE_LABELS = dict(ROUTES)


def extras(qso) -> dict[str, str]:
    """The QSO's extra ADIF fields (lower-case names)."""
    try:
        loaded = json.loads(qso.extra_adif) if qso.extra_adif else {}
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def set_extras(qso, **values: str | None) -> None:
    """Set (or, with None, remove) extra ADIF fields, keeping all the others."""
    data = extras(qso)
    for key, value in values.items():
        if value in (None, ""):
            data.pop(key, None)
        else:
            data[key] = value
    qso.extra_adif = json.dumps(data, sort_keys=True, ensure_ascii=False) if data else None


def adif_date(day: date) -> str:
    return day.strftime("%Y%m%d")


def parse_adif_date(text: str | None) -> date | None:
    try:
        return datetime.strptime(text or "", "%Y%m%d").date()
    except ValueError:
        return None


def sent_info(qso) -> tuple[date | None, str | None]:
    data = extras(qso)
    return parse_adif_date(data.get("qslsdate")), data.get("qsl_sent_via")


def received_info(qso) -> tuple[date | None, str | None]:
    data = extras(qso)
    return parse_adif_date(data.get("qslrdate")), data.get("qsl_rcvd_via")


def mark_sent(qso, day: date, route: str | None = None) -> None:
    qso.qsl_sent = "Y"
    set_extras(qso, qslsdate=adif_date(day), qsl_sent_via=route if route in ROUTE_LABELS else None)


def mark_queued(qso) -> None:
    qso.qsl_sent = "Q"


def mark_received(qso, day: date, route: str | None = None) -> None:
    qso.qsl_rcvd = "Y"
    set_extras(qso, qslrdate=adif_date(day), qsl_rcvd_via=route if route in ROUTE_LABELS else None)


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@dataclass
class Label:
    """One address label: a station (or its manager) and the QSOs to confirm."""

    to: str  # the callsign on the card
    via: str | None  # QSL manager, if any
    qsos: list = field(default_factory=list)


def labels_for(qsos: list, per_label: int = 4) -> list[Label]:
    """Group QSOs into labels: one per callsign and manager, oldest QSO first, at most
    ``per_label`` QSOs on a label (a card has room for about four lines)."""
    groups: dict[tuple[str, str], list] = {}
    for qso in sorted(qsos, key=lambda q: (q.call or "", q.time_on or datetime.min)):
        key = (qso.call or "?", (qso.qsl_via or "").strip().upper())
        groups.setdefault(key, []).append(qso)
    labels = []
    for (call, via), items in groups.items():
        for start in range(0, len(items), per_label):
            labels.append(Label(call, via or None, items[start:start + per_label]))
    return labels
