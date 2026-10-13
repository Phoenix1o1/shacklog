"""QSL cards: to send (labels, mark sent), waiting, and cards that arrived.

The data lives on the QSOs (services/qsl.py explains the fields), so this section has
no records of its own: three lists over the QSO table and two bulk actions.
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import or_

from shacklog.models import Qso
from shacklog.services import qsl
from shacklog.web import helpers as h

bp = Blueprint("qsl", __name__, url_prefix="/qsl")

LIST_LIMIT = 500  # a list longer than this is shown in part, oldest first
TABS = [("send", "To send"), ("waiting", "Waiting for a card"), ("arrived", "A card arrived")]


def _not_received():
    return or_(Qso.qsl_rcvd.is_(None), Qso.qsl_rcvd.notin_(["Y", "I"]))


def _query(tab: str):
    q = h.live(Qso)
    if tab == "send":
        return q.filter(Qso.qsl_sent.in_(qsl.TO_SEND))
    if tab == "waiting":
        return q.filter(Qso.qsl_sent == "Y", _not_received())
    abort(404)


@bp.route("/")
def index():
    tab = request.args.get("tab", "send")
    if tab not in dict(TABS):
        abort(404)
    counts = {"send": _query("send").count(), "waiting": _query("waiting").count()}
    call = (request.args.get("call") or "").replace(" ", "").upper()
    qsos = []
    if tab == "arrived":
        if call:
            qsos = (h.live(Qso).filter(Qso.call == call, _not_received())
                    .order_by(Qso.time_on).limit(LIST_LIMIT).all())
    else:
        qsos = _query(tab).order_by(Qso.time_on).limit(LIST_LIMIT).all()
    return render_template(
        "qsl/index.html", tab=tab, tabs=TABS, counts=counts, qsos=qsos, call=call,
        limit=LIST_LIMIT, routes=qsl.ROUTES, route_labels=qsl.ROUTE_LABELS,
        today=date.today().isoformat(), sent_info=qsl.sent_info)


def _selected() -> list[Qso]:
    ids = request.form.getlist("ids")
    if not ids:
        return []
    return h.live(Qso).filter(Qso.id.in_(ids)).all()


def _day() -> date:
    try:
        return date.fromisoformat(request.form.get("date") or "")
    except ValueError:
        return date.today()


@bp.route("/mark", methods=["POST"])
def mark():
    """Bulk actions on the ticked QSOs: labels, queue, sent, received."""
    action = request.form.get("action", "")
    back = request.form.get("back") or url_for(".index")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for(".index")
    qsos = _selected()
    if not qsos:
        flash("Tick at least one QSO first.", "error")
        return redirect(back)
    if action == "labels":
        per_label = 4 if request.form.get("per_label") != "1" else 1
        return render_template("qsl/labels.html", labels=qsl.labels_for(qsos, per_label),
                               my_call=_my_call(qsos), route=request.form.get("route"),
                               route_labels=qsl.ROUTE_LABELS)
    route = request.form.get("route") or None
    noun = f"{len(qsos)} QSO{'s' if len(qsos) != 1 else ''}"
    if action == "sent":
        for qso in qsos:
            qsl.mark_sent(qso, _day(), route)
        flash(f"Marked the QSL of {noun} as sent.", "ok")
    elif action == "queue":
        for qso in qsos:
            qsl.mark_queued(qso)
        flash(f"Queued the QSL of {noun}.", "ok")
    elif action == "received":
        _received(qsos, route)
        flash(f"Marked {noun} as confirmed by QSL card.", "ok")
    else:
        abort(400, description="Unknown action.")
    h.session().commit()
    return redirect(back)


def _received(qsos: list[Qso], route: str | None) -> None:
    """Mark received; store the scans of the card (if any) and link them to the QSOs."""
    from shacklog.services import library, links

    for qso in qsos:
        qsl.mark_received(qso, _day(), route)
    s = h.session()
    call = qsos[0].call
    for side, upload in (("front", request.files.get("front")), ("back", request.files.get("back"))):
        if upload is None or not upload.filename:
            continue
        entry, _created = library.store_upload(
            s, h.filestore(), upload.stream, upload.filename, category="qsl",
            title=f"QSL {call} ({side})", tags="qsl")
        s.flush()
        for qso in qsos:
            links.add_link(s, qso, entry, role="QSL card")


def _my_call(qsos: list[Qso]) -> str:
    calls = [q.my_call for q in qsos if q.my_call]
    return max(set(calls), key=calls.count) if calls else ""

