"""Knowledge base: notes, quick references, manuals and band plans in one place.

The knowledge base is a *view* of the library (StoredFile), not a separate store: every
entry is a library file with a knowledge category, so it syncs, is searchable (text
inside PDFs and notes included) and can be linked to equipment like any file.

* ``/kb/``            grouped list, pinned entries first, search box
* ``/kb/<id>``        reader: Markdown rendered, text shown, PDFs in the browser viewer
* ``/kb/new-note``    write a Markdown note in the browser (stored as a .md file)

Pin an entry by giving it the tag ``pinned``; pinned entries also appear on the dashboard.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import or_

from shacklog.models import FILE_CATEGORIES, StoredFile, choice_label, split_tags
from shacklog.services import library, markdown_render, search
from shacklog.services.links import links_for
from shacklog.web import helpers as h

bp = Blueprint("kb", __name__, url_prefix="/kb")

#: Knowledge categories, in the order the page shows them.
KB_CATEGORIES = ["note", "reference", "manual", "bandplan", "datasheet", "schematic", "document"]
TEXTLIKE = {".md", ".markdown", ".txt", ".text", ".csv", ".log", ".ini", ".cfg"}
PIN_TAG = "pinned"


def is_pinned(f: StoredFile) -> bool:
    return PIN_TAG in [t.lower() for t in split_tags(f.tags)]


def reader_kind(f: StoredFile) -> str:
    ext = Path(f.filename or "").suffix.lower()
    if ext in (".md", ".markdown"):
        return "markdown"
    if ext in TEXTLIKE or (f.mime or "").startswith("text/plain"):
        return "text"
    if ext == ".pdf" or f.mime == "application/pdf":
        return "pdf"
    return "other"


def _read_text(f: StoredFile) -> str | None:
    store = h.filestore()
    if not store.exists(f.sha256):
        return None
    data = store.read_bytes(f.sha256)[:4_000_000]
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


@bp.route("/")
def index():
    s = h.session()
    q = (request.args.get("q") or "").strip()
    query = h.live(StoredFile).filter(StoredFile.category.in_(KB_CATEGORIES))
    hit_snippets: dict[str, str] = {}
    if q:
        hits = search.query(s, q, entities=["file"], limit=200)
        ids = [hit.entity_id for hit in hits]
        hit_snippets = {hit.entity_id: hit.snippet for hit in hits}
        like = f"%{q}%"
        query = query.filter(or_(StoredFile.id.in_(ids), StoredFile.title.ilike(like),
                                 StoredFile.filename.ilike(like), StoredFile.tags.ilike(like)))
    files = query.order_by(StoredFile.title).all()
    pinned = [f for f in files if is_pinned(f)]
    groups = []
    for category in KB_CATEGORIES:
        items = [f for f in files if f.category == category and not is_pinned(f)]
        if items:
            groups.append((category, choice_label(FILE_CATEGORIES, category), items))
    return render_template("kb/index.html", pinned=pinned, groups=groups, q=q,
                           snippets=hit_snippets, total=len(files))


@bp.route("/<record_id>")
def view(record_id: str):
    f = h.get_or_404(StoredFile, record_id)
    kind = reader_kind(f)
    body = None
    present = h.filestore().exists(f.sha256)
    if kind == "markdown" and present:
        body = markdown_render.render(_read_text(f))
    elif kind == "text" and present:
        body = _read_text(f)
    return render_template("kb/view.html", f=f, kind=kind, body=body, present=present,
                           pinned=is_pinned(f), linked=links_for(h.session(), "file", f.id),
                           category_label=choice_label(FILE_CATEGORIES, f.category))


@bp.route("/<record_id>/pin", methods=["POST"])
def pin(record_id: str):
    f = h.get_or_404(StoredFile, record_id)
    tags = [t for t in split_tags(f.tags) if t.lower() != PIN_TAG]
    if request.form.get("pin") == "1":
        tags.insert(0, PIN_TAG)
    f.tags = ", ".join(tags) or None
    h.session().commit()
    flash("Pinned to the top of the knowledge base." if request.form.get("pin") == "1"
          else "Unpinned.", "ok")
    return redirect(url_for(".view", record_id=f.id))


@bp.route("/new-note", methods=["GET", "POST"])
def new_note():
    """Write a Markdown note; it is stored as a .md file in the library."""
    s = h.session()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "")
        if not title or not body.strip():
            flash("A note needs a title and some text.", "error")
            return render_template("kb/note_form.html", title=title, body=body), 422
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in title).strip()[:80]
        text = body if body.lstrip().startswith("#") else f"# {title}\n\n{body}"
        entry, _created = library.store_bytes(
            s, h.filestore(), text.encode("utf-8"), f"{safe or 'note'}.md", category="note",
            title=title, tags=request.form.get("tags", "").strip() or None)
        s.commit()
        flash(f"Saved the note “{title}”.", "ok")
        return redirect(url_for(".view", record_id=entry.id))
    return render_template("kb/note_form.html", title="", body="")


@bp.route("/<record_id>/edit-note", methods=["GET", "POST"])
def edit_note(record_id: str):
    """Edit a Markdown note: the new text becomes a new blob; the entry keeps its id."""
    s = h.session()
    f = h.get_or_404(StoredFile, record_id)
    if reader_kind(f) != "markdown":
        abort(400, description="Only Markdown notes can be edited here.")
    if request.method == "POST":
        body = request.form.get("body", "")
        sha, size = h.filestore().put_bytes(body.encode("utf-8"))
        f.sha256, f.size = sha, size
        f.text_content = body
        title = request.form.get("title", "").strip()
        if title:
            f.title = title
        s.commit()
        flash("Note saved.", "ok")
        return redirect(url_for(".view", record_id=f.id))
    return render_template("kb/note_form.html", title=f.title, body=_read_text(f) or "", f=f)


def pinned_for_dashboard(limit: int = 8) -> list[StoredFile]:
    files = (h.live(StoredFile).filter(StoredFile.category.in_(KB_CATEGORIES))
             .filter(StoredFile.tags.ilike(f"%{PIN_TAG}%")).order_by(StoredFile.title).all())
    return [f for f in files if is_pinned(f)][:limit]
