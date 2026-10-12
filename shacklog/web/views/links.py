"""Linking records together and attaching files (the "Linked records & files" panel)."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import or_

from shacklog.models import ENTITIES, Link
from shacklog.services import library
from shacklog.services.links import add_link, get_entity
from shacklog.web import helpers as h

bp = Blueprint("links", __name__, url_prefix="/links")

#: Columns searched by the picker, per entity.
PICKER_COLUMNS = {
    "file": ("title", "filename", "tags"),
    "equipment": ("name", "model", "manufacturer"),
    "repeater": ("name", "callsign", "ident", "location_name"),
    "station": ("name", "locator"),
    "qso": ("call", "name"),
    "swl": ("heard", "description"),
    "journal": ("title",),
    "radio_config": ("title", "edition"),
    "contact": ("callsign", "name"),
}


def _back(default: str = "/") -> str:
    back = request.values.get("back") or default
    return back if back.startswith("/") else "/"


@bp.route("/picker")
def picker():
    """Candidates for linking (htmx fragment, or a full page without JavaScript)."""
    entity = request.args.get("type", "file")
    model = ENTITIES.get(entity)
    columns = PICKER_COLUMNS.get(entity)
    if model is None or columns is None:
        abort(400)
    text = (request.args.get("q") or "").strip()
    q = h.live(model)
    if text:
        q = q.filter(or_(*[getattr(model, c).ilike(f"%{text}%") for c in columns]))
    order = model.time_on if hasattr(model, "time_on") else model.updated_at
    items = q.order_by(order.desc()).limit(25).all()
    ctx = dict(items=items, entity=entity, a_type=request.args.get("a_type"),
               a_id=request.args.get("a_id"))
    if request.headers.get("HX-Request"):
        return render_template("links/_picker_results.html", **ctx)
    return render_template("links/picker.html", **ctx)


@bp.route("/add", methods=["POST"])
def add():
    s = h.session()
    a = get_entity(s, request.form.get("a_type", ""), request.form.get("a_id", ""))
    b = get_entity(s, request.form.get("b_type", ""), request.form.get("b_id", ""))
    if a is None or b is None:
        abort(404)
    add_link(s, a, b, role=request.form.get("role") or None)
    s.commit()
    flash(f"Linked {b.display_name()}.", "ok")
    target = h.entity_url(a.__entity__, a.id) or "/"
    return redirect(_back(target + "#links"))


@bp.route("/<record_id>/delete", methods=["POST"])
def remove(record_id: str):
    link = h.get_or_404(Link, record_id)
    link.soft_delete()
    h.session().commit()
    flash("Unlinked.", "ok")
    return redirect(_back())


@bp.route("/attach", methods=["POST"])
def attach_upload():
    """Upload one or more files to the library and link them to a record."""
    s = h.session()
    owner = get_entity(s, request.form.get("a_type", ""), request.form.get("a_id", ""))
    if owner is None:
        abort(404)
    category = request.form.get("category") or "document"
    role = request.form.get("role") or None
    added = 0
    for upload in request.files.getlist("files"):
        if not upload or not upload.filename:
            continue
        entry, _created = library.store_upload(s, h.filestore(), upload.stream, upload.filename,
                                               category=category)
        add_link(s, owner, entry, role=role or category)
        added += 1
    s.commit()
    flash(f"Attached {added} file{'s' if added != 1 else ''}." if added else "No file chosen.",
          "ok" if added else "error")
    return redirect(_back((h.entity_url(owner.__entity__, owner.id) or "/") + "#links"))

