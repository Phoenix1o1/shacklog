"""Radio configurations: an archive of CHIRP snapshots (image + CSV + notes) per radio.

Every time Andrea reprograms a radio (UV-K1 banks Fusion/FieldOps/Labs, the TM-733E…)
she saves a configuration here: the CHIRP ``.img`` and ``.csv`` go to the library, the CSV
is parsed into :class:`RadioChannel` rows, and each channel is matched to a known
repeater. That makes three questions easy:

* "What is on the radio right now?" — the *current* configuration of each radio;
* "Where is 145.650 programmed?" — the channel search across every configuration;
* "What changed since last time?" — the diff between two configurations.

CHIRP parsing lives in ``services/chirp.py`` (imported lazily).
"""

from __future__ import annotations

import json
import re

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload

from shacklog import APP_NAME
from shacklog.models import Equipment, ImportLog, RadioChannel, RadioConfig, Repeater, utcnow
from shacklog.services import library
from shacklog.services.links import add_link, links_for
from shacklog.web import filters as flt
from shacklog.web import forms
from shacklog.web import helpers as h
from shacklog.web.forms import Field, Section

bp = Blueprint("radio", __name__, url_prefix="/radio")
h.register_entity_view("radio_config", "radio.view")

#: Channel matching tolerance: a channel is "on" a repeater when its frequency is within
#: 1 kHz of the repeater output (CHIRP rounds to the radio's step).
MATCH_TOLERANCE_MHZ = 0.001
#: Channel search: numbers above this are frequencies typed in kHz.
SEARCH_KHZ_ABOVE = 1300.0

FORM = [
    Section("Configuration", [
        Field("radio_id", "Radio", type="select", choices=h.equipment_choices(*h.RADIO_KINDS),
              width="half"),
        Field("title", "Title", required=True, width="half",
              placeholder="UV-K1 Fusion bank, October 2026"),
        Field("firmware", "Firmware", width="third", placeholder="F4HWN v4.2"),
        Field("edition", "Edition / bank", width="third", placeholder="Fusion, FieldOps, Labs…"),
        Field("config_date", "Date", type="date", width="third"),
        Field("is_current", "This is what is on the radio now", type="bool", width="full"),
    ]),
    Section("What was applied", [
        Field("settings", "Settings", type="textarea", width="full",
              attrs={"rows": 6},
              help="Menu settings, squelch, power levels, scan lists… anything the CSV doesn't hold."),
        Field("notes", "Notes", type="textarea", width="full"),
        Field("tags", "Tags", type="tags", width="full"),
    ]),
]


def _distinct_editions(s):
    rows = (s.query(RadioConfig.edition)
            .filter(RadioConfig.deleted_at.is_(None), RadioConfig.edition.isnot(None),
                    RadioConfig.edition != "")
            .distinct().order_by(RadioConfig.edition).all())
    return [(r[0], r[0]) for r in rows]


def _current_clause(value: str, _session):
    return RadioConfig.is_current.is_(True) if value == "yes" else RadioConfig.is_current.isnot(True)


FILTERS = [
    flt.Filter("q", "Text", "text", (RadioConfig.title, RadioConfig.firmware, RadioConfig.edition,
                                     RadioConfig.settings, RadioConfig.notes, RadioConfig.tags),
               placeholder="Title, firmware, settings…"),
    flt.Filter("radio", "Radio", "ref", (RadioConfig.radio_id,),
               choices=h.equipment_choices(*h.RADIO_KINDS)),
    flt.Filter("edition", "Edition / bank", "choice", (RadioConfig.edition,),
               choices=_distinct_editions),
    flt.Filter("current", "Current", "has", custom=_current_clause),
]

SORTS = [
    flt.Sort("radio", "Radio", func.lower(Equipment.name)),
    flt.Sort("date", "Date", RadioConfig.config_date, default_desc=True),
    flt.Sort("title", "Title", func.lower(RadioConfig.title)),
]


def filtered_query():
    """Configurations matching the current request's filters (radio joined for sorting)."""
    query = h.live(RadioConfig).outerjoin(Equipment, RadioConfig.radio_id == Equipment.id)
    return flt.apply_filters(query, FILTERS, request.args, h.session())


# ---------------------------------------------------------------------------
# Channel helpers
# ---------------------------------------------------------------------------

#: RadioChannel columns that come from a CHIRP row.
CHANNEL_FIELDS = ["location", "name", "freq_mhz", "duplex", "offset_mhz", "tone_mode", "rtone",
                  "ctone", "dtcs", "dtcs_polarity", "rx_dtcs", "cross_mode", "mode", "tstep",
                  "skip", "power", "comment"]


def channel_shift(duplex: str | None, offset: float | None, freq: float | None) -> float | None:
    """Signed TX shift of a channel in MHz: ``+``/``-`` with an offset, or a split TX
    frequency. 0 for simplex, None when TX is off or unknown."""
    d = (duplex or "").strip().lower()
    if d == "+":
        return round(offset or 0.0, 6)
    if d == "-":
        return round(-(offset or 0.0), 6)
    if d == "split" and offset and freq:
        return round(offset - freq, 6)
    if d == "":
        return 0.0
    return None


def _get(ch, name: str):
    """Field of a channel given as a RadioChannel or as a chirp-style dict."""
    return ch.get(name) if isinstance(ch, dict) else getattr(ch, name, None)


def tone_text(ch) -> str:
    """Short tone description for a channel: ``T 88.5``, ``TSQL 88.5``, ``D023N``."""
    mode = (_get(ch, "tone_mode") or "").strip()
    rtone, ctone = _get(ch, "rtone"), _get(ch, "ctone")
    if mode == "Tone" and rtone:
        return f"T {float(rtone):g}"
    if mode == "TSQL" and (ctone or rtone):
        return f"TSQL {float(ctone or rtone):g}"
    if mode == "DTCS" and _get(ch, "dtcs"):
        return f"D{_get(ch, 'dtcs')}{(_get(ch, 'dtcs_polarity') or '')[:1]}"
    if mode == "Cross":
        return f"Cross {_get(ch, 'cross_mode') or ''}".strip()
    return ""


def shift_of(ch) -> float | None:
    return channel_shift(_get(ch, "duplex"), _get(ch, "offset_mhz"), _get(ch, "freq_mhz"))


def match_repeater(freq: float | None, shift: float | None, repeaters: list[Repeater]) -> Repeater | None:
    """Repeater whose output is within 1 kHz of ``freq``; one with the same shift wins."""
    if freq is None:
        return None
    candidates = [r for r in repeaters
                  if r.output_mhz is not None and abs(r.output_mhz - freq) <= MATCH_TOLERANCE_MHZ]
    if not candidates:
        return None
    candidates.sort(key=lambda r: abs(r.output_mhz - freq))
    if shift is not None:
        for rep in candidates:
            if rep.offset_mhz is not None and abs(rep.offset_mhz - shift) <= MATCH_TOLERANCE_MHZ:
                return rep
    return candidates[0]


def live_repeaters() -> list[Repeater]:
    return h.live(Repeater).filter(Repeater.output_mhz.isnot(None)).all()


def config_channels(config_id: str):
    return (h.live(RadioChannel).filter(RadioChannel.config_id == config_id)
            .options(joinedload(RadioChannel.repeater))
            .order_by(RadioChannel.location).all())


def channel_dict(ch: RadioChannel, with_raw: bool = True) -> dict:
    """A stored channel back in the shape ``services.chirp`` reads and writes."""
    out = {name: getattr(ch, name) for name in CHANNEL_FIELDS}
    if with_raw:
        try:
            out["raw"] = json.loads(ch.raw) if ch.raw else {}
        except ValueError:
            out["raw"] = {}
    return out


def replace_channels(s, config: RadioConfig, parsed: list[dict]) -> tuple[int, int]:
    """Soft-delete the config's channels and store ``parsed`` instead, matched to repeaters.

    Returns (channels stored, channels matched to a repeater).
    """
    for old in h.live(RadioChannel).filter(RadioChannel.config_id == config.id):
        old.soft_delete()
    repeaters = live_repeaters()
    stored = matched = 0
    for index, row in enumerate(parsed):
        ch = RadioChannel(config_id=config.id)
        for name in CHANNEL_FIELDS:
            if name in row:
                setattr(ch, name, row[name])
        if ch.location is None:
            ch.location = index
        raw = row.get("raw")
        ch.raw = json.dumps(raw, ensure_ascii=False) if raw is not None else None
        rep = match_repeater(ch.freq_mhz, channel_shift(ch.duplex, ch.offset_mhz, ch.freq_mhz),
                             repeaters)
        if rep is not None:
            ch.repeater_id = rep.id
            matched += 1
        s.add(ch)
        stored += 1
    return stored, matched


def set_current(s, config: RadioConfig) -> None:
    """Only one current configuration per radio."""
    if not config.is_current:
        return
    others = h.live(RadioConfig).filter(RadioConfig.id != config.id,
                                        RadioConfig.is_current.is_(True))
    if config.radio_id:
        others = others.filter(RadioConfig.radio_id == config.radio_id)
    else:
        others = others.filter(RadioConfig.radio_id.is_(None))
    for other in others:
        other.is_current = False


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    result = filtered_query()
    query, sort_key, sort_dir = flt.apply_sort(result, SORTS, request.args, "radio")
    if sort_key == "radio":
        query = query.order_by(RadioConfig.config_date.desc().nulls_last(),
                               RadioConfig.created_at.desc())
    else:
        query = query.order_by(RadioConfig.created_at.desc())
    counts = (s.query(RadioChannel.config_id, func.count(RadioChannel.id))
              .filter(RadioChannel.deleted_at.is_(None)).group_by(RadioChannel.config_id))
    page = h.paginate(query)
    return render_template(
        "radio/index.html", page=page, filters=FILTERS,
        fchoices=flt.resolve_filter_choices(FILTERS, s), fresult=result,
        sort_key=sort_key, sort_dir=sort_dir, counts=dict(counts.all()),
    )


# ---------------------------------------------------------------------------
# Create / edit / delete
# ---------------------------------------------------------------------------

def _render_form(config: RadioConfig | None, values, errors, title: str):
    return render_template(
        "radio/form.html", config=config, sections=FORM, values=values, errors=errors,
        choices=forms.resolve_choices(FORM, h.session()), title=title,
    )


def _read_uploads(errors: dict) -> tuple:
    """Read the optional .img and .csv uploads; parse the CSV before anything is saved."""
    img = request.files.get("image_file")
    csv_upload = request.files.get("csv_file")
    img_data = img.read() if img is not None and img.filename else None
    csv_data = csv_upload.read() if csv_upload is not None and csv_upload.filename else None
    parsed = None
    if csv_data is not None:
        from shacklog.services import chirp
        from shacklog.web.views.repeaters import decode_upload

        try:
            parsed = chirp.read_chirp_csv(decode_upload(csv_data))
        except Exception as exc:  # report, don't crash: the file is the user's
            errors["csv_file"] = f"Not a CHIRP CSV ({exc}). Export it from CHIRP with File › Export."
        else:
            if not parsed:
                errors["csv_file"] = "The CSV holds no channels."
    return (img, img_data), (csv_upload, csv_data), parsed


def _save(config: RadioConfig, values: dict, uploads, parsed) -> str:
    """Apply form values and uploads to ``config``; returns a flash message tail."""
    s = h.session()
    (img, img_data), (csv_upload, csv_data), _ = uploads
    forms.apply(config, values)
    set_current(s, config)
    store = h.filestore()
    tail = ""
    if img_data is not None:
        entry, _ = library.store_bytes(s, store, img_data, img.filename, category="chirp",
                                       title=f"{config.title} (CHIRP image)")
        config.image_file_id = entry.id
        add_link(s, config, entry, role="chirp image")
    if csv_data is not None:
        entry, _ = library.store_bytes(s, store, csv_data, csv_upload.filename, category="chirp",
                                       title=f"{config.title} (CHIRP CSV)")
        config.csv_file_id = entry.id
        add_link(s, config, entry, role="chirp csv")
        stored, matched = replace_channels(s, config, parsed or [])
        s.add(ImportLog(kind="chirp-config", filename=csv_upload.filename,
                        summary=json.dumps({"config_id": config.id, "channels": stored,
                                            "matched": matched})))
        tail = f" {stored} channels, {matched} matched to repeaters."
    return tail


@bp.route("/new", methods=["GET", "POST"])
def new():
    s = h.session()
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        uploads = _read_uploads(errors)
        if errors:
            return _render_form(None, raw, errors, "Save a radio configuration"), 422
        config = RadioConfig()
        s.add(config)
        tail = _save(config, values, uploads, uploads[2])
        s.commit()
        flash(f"Saved {config.title}.{tail}", "ok")
        return redirect(url_for(".view", record_id=config.id))

    template = RadioConfig(config_date=utcnow().date(), is_current=True)
    radio = s.get(Equipment, request.args.get("radio", "")) if request.args.get("radio") else None
    if radio is not None:
        template.radio_id = radio.id
        template.firmware = radio.firmware
    source = s.get(RadioConfig, request.args["from"]) if request.args.get("from") else None
    if source is not None:  # "Save a new version": start from the previous one
        for name in ("radio_id", "title", "firmware", "edition", "settings", "tags"):
            setattr(template, name, getattr(source, name))
    return _render_form(None, forms.values_from_obj(FORM, template), {},
                        "Save a radio configuration")


@bp.route("/<record_id>/edit", methods=["GET", "POST"])
def edit(record_id: str):
    s = h.session()
    config = h.get_or_404(RadioConfig, record_id)
    if request.method == "POST":
        values, errors, raw = forms.parse(FORM, request.form)
        uploads = _read_uploads(errors)
        if errors:
            return _render_form(config, raw, errors, f"Edit {config.title}"), 422
        tail = _save(config, values, uploads, uploads[2])
        s.commit()
        flash(f"Saved.{tail}", "ok")
        return redirect(url_for(".view", record_id=config.id))
    return _render_form(config, forms.values_from_obj(FORM, config), {}, f"Edit {config.title}")


@bp.route("/<record_id>/delete", methods=["POST"])
def delete(record_id: str):
    config = h.get_or_404(RadioConfig, record_id)
    # Channels stay as they are: they disappear with their config (every query joins on a
    # live config) and come back with it if the config is restored from the trash.
    config.soft_delete()
    h.session().commit()
    flash(f"{config.title} moved to the trash.", "ok")
    return redirect(url_for(".index"))


@bp.route("/<record_id>/rematch", methods=["POST"])
def rematch(record_id: str):
    """Match channels to repeaters again (after importing or editing repeaters)."""
    config = h.get_or_404(RadioConfig, record_id)
    repeaters = live_repeaters()
    matched = 0
    for ch in config_channels(config.id):
        rep = match_repeater(ch.freq_mhz, channel_shift(ch.duplex, ch.offset_mhz, ch.freq_mhz),
                             repeaters)
        new_id = rep.id if rep else None
        if ch.repeater_id != new_id:
            ch.repeater_id = new_id
        matched += rep is not None
    h.session().commit()
    flash(f"Matched {matched} channels to repeaters.", "ok")
    return redirect(url_for(".view", record_id=config.id))


# ---------------------------------------------------------------------------
# Detail, export, diff
# ---------------------------------------------------------------------------

@bp.route("/<record_id>")
def view(record_id: str):
    s = h.session()
    config = h.get_or_404(RadioConfig, record_id)
    channels = config_channels(config.id)
    rows = [(ch, channel_shift(ch.duplex, ch.offset_mhz, ch.freq_mhz), tone_text(ch))
            for ch in channels]
    # Same radio first (the usual comparison), then the rest, newest first.
    others = (h.live(RadioConfig).filter(RadioConfig.id != config.id)
              .order_by(func.coalesce(RadioConfig.radio_id, "") != (config.radio_id or ""),
                        RadioConfig.config_date.desc().nulls_last()).all())
    image = config.image_file if config.image_file and not config.image_file.is_deleted else None
    csv_file = config.csv_file if config.csv_file and not config.csv_file.is_deleted else None
    return render_template(
        "radio/view.html", config=config, rows=rows, others=others,
        matched=sum(1 for ch in channels if ch.repeater_id),
        image=image, csv_file=csv_file, linked=links_for(s, "radio_config", config.id),
    )


def _filename(title: str, ext: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "config").lower()).strip("-")
    return f"{APP_NAME.lower()}-{slug}-{utcnow():%Y%m%d}.{ext}"


@bp.route("/<record_id>/export.csv")
def export_chirp(record_id: str):
    """The configuration's channels as a CHIRP CSV (unknown columns preserved)."""
    from shacklog.services import chirp

    config = h.get_or_404(RadioConfig, record_id)
    text = chirp.write_chirp_csv([channel_dict(ch) for ch in config_channels(config.id)])
    return Response(text, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_filename(config.title, "csv")}"'})


@bp.route("/diff")
def diff():
    """What changed between configuration ``a`` (before) and ``b`` (after)."""
    from shacklog.services import chirp

    s = h.session()
    a = h.get_or_404(RadioConfig, request.args.get("a", ""))
    b = h.get_or_404(RadioConfig, request.args.get("b", ""))
    result = chirp.diff_channels([channel_dict(c, with_raw=False) for c in config_channels(a.id)],
                                 [channel_dict(c, with_raw=False) for c in config_channels(b.id)])
    configs = (h.live(RadioConfig).order_by(RadioConfig.config_date.desc().nulls_last(),
                                            RadioConfig.title).all())
    return render_template("radio/diff.html", a=a, b=b, result=result, configs=configs,
                           tone_text=tone_text, shift_of=shift_of)


# ---------------------------------------------------------------------------
# Channel search across every configuration
# ---------------------------------------------------------------------------

def _channel_text(value: str, _session):
    """A frequency or words in the channel name/comment.

    ``145.650``/``145,65``/``145650`` (kHz) find that frequency (±0.5 kHz); a shorter
    number works as a prefix: ``145.6`` finds 145.600–145.699, ``433`` all of 433 MHz.
    """
    text = value.strip().replace(",", ".")
    if re.fullmatch(r"\d+(\.\d*)?", text):
        number = float(text)
        # No radio CHIRP programs goes above 1.3 GHz, so a larger number was typed in kHz
        # (145500 → 145.5). Not forms.parse_freq: it reads "145500" as the 2 mm band.
        if number > SEARCH_KHZ_ABOVE:
            freq = round(number / 1000, 6)
            return RadioChannel.freq_mhz.between(freq - 0.0005, freq + 0.0005)
        decimals = len(text.partition(".")[2])
        if decimals >= 3:
            return RadioChannel.freq_mhz.between(number - 0.0005, number + 0.0005)
        step = 10.0 ** -decimals
        return and_(RadioChannel.freq_mhz >= number - 1e-9,
                    RadioChannel.freq_mhz < number + step - 1e-9)
    pattern = f"%{value.strip()}%"
    return or_(RadioChannel.name.ilike(pattern), RadioChannel.comment.ilike(pattern))


def _channel_current(value: str, _session):
    return RadioConfig.is_current.is_(True) if value == "yes" else RadioConfig.is_current.isnot(True)


CHANNEL_FILTERS = [
    flt.Filter("q", "Name or frequency", "text", custom=_channel_text,
               placeholder="145.650 or IR2UFD"),
    flt.Filter("radio", "Radio", "ref", (RadioConfig.radio_id,),
               choices=h.equipment_choices(*h.RADIO_KINDS)),
    flt.Filter("current", "Current configs only", "has", custom=_channel_current),
]


@bp.route("/channels")
def channels():
    s = h.session()
    query = (s.query(RadioChannel, RadioConfig)
             .join(RadioConfig, RadioChannel.config_id == RadioConfig.id)
             .filter(RadioChannel.deleted_at.is_(None), RadioConfig.deleted_at.is_(None)))
    result = flt.apply_filters(query, CHANNEL_FILTERS, request.args, s)
    query = result.query.order_by(RadioConfig.is_current.desc(),
                                  RadioConfig.config_date.desc().nulls_last(),
                                  RadioConfig.title, RadioChannel.location)
    page = h.paginate(query) if result.active else None
    return render_template(
        "radio/channels.html", page=page, filters=CHANNEL_FILTERS,
        fchoices=flt.resolve_filter_choices(CHANNEL_FILTERS, s), fresult=result,
        shift_of=shift_of, tone_text=tone_text,
    )
