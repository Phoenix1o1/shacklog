"""Library: manuals, datasheets, photos, sweeps, CHIRP images… stored once by SHA-256.

* ``index``    list with filters (category, text, tags, searchable text, linked-to type)
* ``upload``   several files at once, with category/tags/description and an optional link
               to a piece of equipment; identical bytes reuse the existing entry
* ``view``     metadata, preview, "Used by" (the links panel), edit, delete
* ``download`` the original file as an attachment
* ``raw``      the bytes for previews. Only images, PDFs and plain text are served inline;
               everything else (HTML and SVG above all) is an attachment, and every response
               carries ``nosniff`` + ``CSP: sandbox`` so a stored file can never run script
               in ShackLog's origin.
* ``fetch``    a file synced as metadata only: ask the hub for its bytes.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import and_, exists, func, or_, select

from shacklog.models import FILE_CATEGORIES, Equipment, Link, SavedFilter, StoredFile
from shacklog.services import library
from shacklog.services.filestore import TEXT_EXTENSIONS
from shacklog.services.links import add_link, links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("files", __name__, url_prefix="/files")
h.register_entity_view("file", "files.view")

#: Images the browser may show inline (no SVG: it can carry script).
INLINE_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
PDF_MIME = "application/pdf"
#: Text formats shown as plain text (preview and ``raw``). Markup formats are excluded on
#: purpose: they are offered as downloads only.
MARKUP_EXTENSIONS = {".html", ".htm", ".xhtml", ".svg", ".svgz", ".xml", ".js", ".mjs"}
MARKUP_MIMES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
                "application/xml", "text/javascript", "application/javascript"}
PREVIEW_LINES = 300
PREVIEW_MAX_BYTES = 512 * 1024

#: Record types a file can be linked to (for the "linked to" filter).
LINK_TYPES = [
    ("equipment", "Equipment"), ("journal", "Journal entry"), ("qso", "QSO"),
    ("swl", "Listening entry"), ("repeater", "Repeater"), ("station", "Station"),
    ("radio_config", "Radio config"), ("contact", "Contact"), ("file", "Another file"),
    ("none", "Nothing (unlinked)"),
]

FORM = [
    Section(None, [
        Field("title", "Title", required=True, width="full"),
        Field("category", "Category", type="select", choices=FILE_CATEGORIES, blank=False,
              width="half"),
        Field("tags", "Tags", type="tags", width="half", placeholder="uv-k5, firmware"),
        Field("description", "Description", type="textarea", width="full"),
    ]),
]

UPLOAD_FORM = [
    Section("About these files", [
        Field("category", "Category", type="select", width="half",
              choices=[("", "Guess from the file type")] + FILE_CATEGORIES, blank=False),
        Field("tags", "Tags", type="tags", width="half", placeholder="tm-733, manual"),
        Field("description", "Description", type="textarea", width="full", attrs={"rows": 3}),
        Field("equipment_id", "Link to equipment", type="select",
              choices=h.equipment_choices(), width="full",
              help="Optional: the radio, antenna or meter these files belong to."),
    ]),
]


# ---------------------------------------------------------------------------
# File type helpers
# ---------------------------------------------------------------------------

def _ext(f: StoredFile) -> str:
    return Path(f.filename or "").suffix.lower()


def _mime(f: StoredFile) -> str:
    return (f.mime or mimetypes.guess_type(f.filename or "")[0] or
            "application/octet-stream").split(";")[0].strip().lower()


def is_markup(f: StoredFile) -> bool:
    return _ext(f) in MARKUP_EXTENSIONS or _mime(f) in MARKUP_MIMES


def preview_kind(f: StoredFile) -> str | None:
    """How the detail page previews a file: "image", "pdf", "text" or None."""
    mime, ext = _mime(f), _ext(f)
    if mime in INLINE_IMAGE_MIMES and ext not in MARKUP_EXTENSIONS:
        return "image"
    if mime == PDF_MIME or ext == ".pdf":
        return "pdf"
    if mime.startswith("text/") or ext in TEXT_EXTENSIONS or mime in MARKUP_MIMES:
        return "text"  # markup is shown as escaped source, never rendered
    return None


def guess_category(filename: str, mime: str | None) -> str:
    """Category when the uploader leaves it to us."""
    ext = Path(filename).suffix.lower()
    mime = (mime or "").lower()
    if ext in (".s1p", ".s2p"):
        return "sweep"
    if ext == ".img":
        return "chirp"
    if mime.startswith("image/"):
        return "photo"
    if mime.startswith("audio/") or ext in (".wav", ".flac", ".ogg", ".mp3", ".iq", ".sigmf-data"):
        return "recording"
    if ext in (".bin", ".hex", ".dfu", ".uf2"):
        return "firmware"
    return "document"


def _decode(data: bytes) -> tuple[str, str]:
    """Text and the charset it decoded with (UTF-8, else Latin-1, which never fails)."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        # A multi-byte character cut by the read limit is still UTF-8.
        if exc.start >= len(data) - 3:
            try:
                return data[:exc.start].decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                pass
        return data.decode("latin-1"), "iso-8859-1"


def text_preview(f: StoredFile) -> tuple[str, bool]:
    """First :data:`PREVIEW_LINES` lines of a text file, and whether there is more."""
    with h.filestore().open(f.sha256) as fh:
        data = fh.read(PREVIEW_MAX_BYTES + 1)
    cut = len(data) > PREVIEW_MAX_BYTES
    text, _charset = _decode(data[:PREVIEW_MAX_BYTES])
    lines = text.splitlines()
    return "\n".join(lines[:PREVIEW_LINES]), cut or len(lines) > PREVIEW_LINES


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def _linked_to(value: str, _session):
    allowed = {v for v, _ in LINK_TYPES}
    if value not in allowed:
        raise ValueError(value)
    if value == "none":
        any_link = select(Link.id).where(
            Link.deleted_at.is_(None),
            or_(and_(Link.a_type == "file", Link.a_id == StoredFile.id),
                and_(Link.b_type == "file", Link.b_id == StoredFile.id)))
        return ~exists(any_link)
    forward = select(Link.id).where(Link.deleted_at.is_(None), Link.a_type == "file",
                                    Link.a_id == StoredFile.id, Link.b_type == value)
    backward = select(Link.id).where(Link.deleted_at.is_(None), Link.b_type == "file",
                                     Link.b_id == StoredFile.id, Link.a_type == value)
    return or_(exists(forward), exists(backward))


FILTERS = [
    flt.Filter("q", "Text", "text", (StoredFile.title, StoredFile.filename,
                                     StoredFile.description, StoredFile.tags),
               placeholder="Title, file name, description"),
    flt.Filter("category", "Category", "choice", (StoredFile.category,), choices=FILE_CATEGORIES),
    flt.Filter("linked", "Linked to", "choice", custom=_linked_to, choices=LINK_TYPES),
    flt.Filter("text", "Searchable text inside", "has", (StoredFile.text_content,)),
    flt.Filter("tags", "Tags", "tags", (StoredFile.tags,), placeholder="tag1, tag2"),
]

SORTS = [
    flt.Sort("new", "Newest", StoredFile.created_at, default_desc=True),
    flt.Sort("name", "Name", func.lower(func.coalesce(StoredFile.title, StoredFile.filename))),
    flt.Sort("size", "Size", StoredFile.size, default_desc=True),
]


def filtered_query():
    """Library entries matching the current request's filters."""
    return flt.apply_filters(h.live(StoredFile), FILTERS, request.args, h.session())


def _link_counts(ids: list[str]) -> dict[str, int]:
    """How many live links each file has (both directions)."""
    if not ids:
        return {}
    counts: dict[str, int] = {}
    rows = (h.live(Link).with_entities(Link.a_type, Link.a_id, Link.b_type, Link.b_id)
            .filter(or_(and_(Link.a_type == "file", Link.a_id.in_(ids)),
                        and_(Link.b_type == "file", Link.b_id.in_(ids)))).all())
    for a_type, a_id, b_type, b_id in rows:
        if a_type == "file" and a_id in ids:
            counts[a_id] = counts.get(a_id, 0) + 1
        if b_type == "file" and b_id in ids:
            counts[b_id] = counts.get(b_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "new")
    query = query.add_columns(
        (func.coalesce(func.length(StoredFile.text_content), 0) > 0).label("has_text"))
    page = h.paginate(query)
    saved = (s.query(SavedFilter).filter(SavedFilter.deleted_at.is_(None),
                                         SavedFilter.target == "file")
             .order_by(SavedFilter.name).all())
    store = h.filestore()
    present = {f.id: store.exists(f.sha256) for f, _ in page.items}
    return render_template(
        "files/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, saved=saved,
        links=_link_counts([f.id for f, _ in page.items]), present=present,
        FILE_CATEGORIES=FILE_CATEGORIES,
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _render_upload(values, errors, status: int = 200):
    html = render_template("files/upload.html", sections=UPLOAD_FORM, values=values,
                           errors=errors, choices=forms.resolve_choices(UPLOAD_FORM, h.session()))
    return html, status


@bp.route("/new")
def new():
    """Conventional URL for "add a record": the library adds by uploading."""
    return redirect(url_for(".upload", **request.args))


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    s = h.session()
    if request.method == "GET":
        values = {"category": request.args.get("category", ""),
                  "equipment_id": request.args.get("equipment", "")}
        return _render_upload(values, {})

    values, errors, raw = forms.parse(UPLOAD_FORM, request.form)
    uploads = [u for u in request.files.getlist("files") if u and u.filename]
    if not uploads:
        errors["files"] = "Choose at least one file."
    owner = None
    if values.get("equipment_id"):
        owner = s.get(Equipment, values["equipment_id"])
        if owner is None or owner.deleted_at is not None:
            errors["equipment_id"] = "That equipment no longer exists. Pick another or none."
    if errors:
        return _render_upload(raw, errors, 422)

    created: list[StoredFile] = []
    reused: list[tuple[str, StoredFile]] = []
    for up in uploads:
        name = library.safe_filename(up.filename)
        category = values.get("category") or guess_category(name, mimetypes.guess_type(name)[0])
        entry, is_new = library.store_upload(
            s, h.filestore(), up.stream, name, category=category,
            description=values.get("description"), tags=values.get("tags"))
        if is_new:
            created.append(entry)
        else:
            reused.append((name, entry))
        if owner is not None:
            add_link(s, owner, entry, role=entry.category)
    s.commit()

    bits = []
    if created:
        bits.append(f"Added {len(created)} file{'s' if len(created) != 1 else ''} to the library.")
    for name, entry in reused:
        bits.append(f"{name} was already in the library as “{entry.display_name()}”: "
                    "kept the existing entry.")
    if owner is not None:
        bits.append(f"Linked to {owner.name}.")
    flash(" ".join(bits), "ok" if created else "warn")
    everything = created + [e for _, e in reused]
    if len(everything) == 1:
        return redirect(url_for(".view", record_id=everything[0].id))
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Detail, edit, delete
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    f = h.get_or_404(StoredFile, record_id)
    present = h.filestore().exists(f.sha256)
    text_chars = s.query(func.length(StoredFile.text_content)).filter(
        StoredFile.id == f.id).scalar() or 0
    kind = preview_kind(f) if present else None
    preview, preview_cut = None, False
    if kind == "text":
        preview, preview_cut = text_preview(f)
    sweep = None
    from shacklog.web.views import journal  # same author; sweep plots live there

    if present and journal.is_sweep(f):
        sweep = journal.analyse_sweep(f, journal._touchstone())
    return render_template(
        "files/view.html", f=f, present=present, kind=kind, preview=preview,
        preview_cut=preview_cut, preview_lines=PREVIEW_LINES, text_chars=text_chars,
        sweep=sweep, linked=links_for(s, "file", f.id), FILE_CATEGORIES=FILE_CATEGORIES,
        hub_configured=bool(h.setting("sync_hub_url")), is_markup=is_markup(f),
    )


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    f = h.get_or_404(StoredFile, record_id)
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        if errors:
            return render_template("files/form.html", f=f, sections=FORM, values=raw,
                                   errors=errors, choices=forms.resolve_choices(FORM, h.session())), 422
        forms.apply(f, values)
        h.session().commit()
        flash("Saved.", "ok")
        return redirect(url_for(".view", record_id=f.id))
    return render_template("files/form.html", f=f, sections=FORM,
                           values=forms.values_from_obj(FORM, f), errors={},
                           choices=forms.resolve_choices(FORM, h.session()))


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    f = h.get_or_404(StoredFile, record_id)
    f.soft_delete()  # the bytes stay in the blob store (filestore.py), restorable
    h.session().commit()
    flash(f"“{f.display_name()}” moved to the trash.", "ok")
    return redirect(url_for(".index"))


# ---------------------------------------------------------------------------
# Bytes
# ---------------------------------------------------------------------------

def _harden(resp: Response) -> Response:
    """Headers on every byte response: never sniff, never run as a page of this origin."""
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox"
    resp.headers.setdefault("Cache-Control", "private, max-age=3600")
    return resp


@bp.route("/<record_id>/download")
def download(record_id: str):
    f = h.get_or_404(StoredFile, record_id)
    if not h.filestore().exists(f.sha256):
        flash("This file's bytes aren't on this instance yet.", "warn")
        return redirect(url_for(".view", record_id=f.id))
    mime = "application/octet-stream" if is_markup(f) else _mime(f)
    resp = send_file(h.filestore().path_for(f.sha256), mimetype=mime, as_attachment=True,
                     download_name=f.filename or "file", conditional=True, etag=f.sha256)
    return _harden(resp)


@bp.route("/<record_id>/raw")
def raw(record_id: str):
    """Bytes for previews (``<img>``, ``<iframe>``, opening in a tab)."""
    f = h.get_or_404(StoredFile, record_id)
    store = h.filestore()
    if not store.exists(f.sha256):
        abort(404)
    path = store.path_for(f.sha256)
    kind = preview_kind(f)
    name = f.filename or "file"
    if kind == "image":
        resp = send_file(path, mimetype=_mime(f), download_name=name, conditional=True,
                         etag=f.sha256)
    elif kind == "pdf":
        resp = send_file(path, mimetype=PDF_MIME, download_name=name, conditional=True,
                         etag=f.sha256)
    elif kind == "text" and not is_markup(f):
        with store.open(f.sha256) as fh:
            head = fh.read(64 * 1024)
        _text, charset = _decode(head)
        resp = send_file(path, mimetype="text/plain", download_name=name, conditional=True,
                         etag=f.sha256)
        # Werkzeug assumes UTF-8; old manuals and CHIRP exports are often Latin-1.
        resp.headers["Content-Type"] = f"text/plain; charset={charset}"
    else:
        # HTML, SVG, XML, archives, binaries…: download only, as opaque bytes.
        resp = send_file(path, mimetype="application/octet-stream", as_attachment=True,
                         download_name=name, conditional=True, etag=f.sha256)
    return _harden(resp)


@bp.route("/<record_id>/fetch", methods=["POST"])
def fetch(record_id: str):
    """Download a missing blob from the hub (when sync runs without "pull files")."""
    f = h.get_or_404(StoredFile, record_id)
    if h.filestore().exists(f.sha256):
        flash("The file is already on this instance.", "ok")
        return redirect(url_for(".view", record_id=f.id))
    try:
        from shacklog.services.sync_client import fetch_blob_from_hub
    except ImportError:
        flash("Fetching from the hub isn't available in this build.", "error")
        return redirect(url_for(".view", record_id=f.id))
    try:
        ok = fetch_blob_from_hub(h.database(), h.filestore(), f.sha256)
    except Exception as exc:  # network down, hub unreachable, bad token…
        flash(f"Couldn't reach the hub: {exc}. Check Settings › Sync and try again.", "error")
        return redirect(url_for(".view", record_id=f.id))
    if ok:
        flash(f"Fetched “{f.display_name()}” from the hub.", "ok")
    else:
        flash("The hub doesn't have this file either. Sync the instance it was added on, "
              "then try again.", "error")
    return redirect(url_for(".view", record_id=f.id))
