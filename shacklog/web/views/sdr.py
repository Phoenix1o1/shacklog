"""SDR monitor: OFFgrid-SDR next to a quick listening log, plus memory files for its Scan.

OFFgrid-SDR (github.com/diddy-boy/OFFgrid-SDR) is a complete RTL-SDR receiver in one
``index.html`` that talks to the stick over **WebUSB** (Chrome/Edge). WebUSB only works
on a *secure context* — ``https://`` or ``http://localhost`` — and the stick must be
plugged into the computer running the browser. So the monitor is meant for **desktop
mode on the PC with the stick**; the hub shows an explanation instead.

ShackLog does not ship OFFgrid-SDR (it is someone else's project); it serves the copy
you downloaded. :func:`find_offgrid_sdr` looks for it in *Settings › Program*'s path, then
in ``OFFgrid-SDR-main`` folders next to the ShackLog folder.

Memories export
---------------
OFFgrid-SDR keeps 50 memories and can Scan a chosen set, stopping on activity — exactly
"which channels are busy right now". ``/sdr/memories.json`` writes an OFFgrid-SDR
settings backup (its own *Import settings* format) from either the repeaters matching a
filter (sorted by distance) or the channels of a radio configuration, with all of them
pre-selected for Scan.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from shacklog.models import RadioChannel, RadioConfig, Repeater, SwlEntry, utcnow
from shacklog.services import bands, enrich
from shacklog.services.system import repo_root
from shacklog.web import helpers as h

bp = Blueprint("sdr", __name__, url_prefix="/sdr")

#: OFFgrid-SDR's backup format (see its exportSettings/checkBackup).
OFFGRID_APP = "OFFgrid-SDR"
OFFGRID_BACKUP_VERSION = 1
OFFGRID_SLOTS = 50
#: Its frequency coverage (general coverage band), Hz.
OFFGRID_MIN_HZ, OFFGRID_MAX_HZ = 100e3, 1766e6

LOCAL_ADDRESSES = {"127.0.0.1", "::1", "localhost"}


# ---------------------------------------------------------------------------
# Finding OFFgrid-SDR
# ---------------------------------------------------------------------------

def _candidates() -> list[Path]:
    """Folders where a downloaded OFFgrid-SDR usually ends up, nearest first."""
    import sys

    starts: list[Path] = []
    root = repo_root()
    if root is not None:
        starts.append(root)
    if getattr(sys, "frozen", False):
        starts.append(Path(sys.executable).resolve().parent)
    starts.append(current_app.config["PATHS"].data_dir)
    found: list[Path] = []
    for start in starts:
        for base in (start, *list(start.parents)[:3]):
            for rel in ("OFFgrid-SDR-main/OFFgrid-SDR-main", "OFFgrid-SDR-main",
                        "OFFgrid-SDR", "offgrid-sdr"):
                found.append(base / rel / "index.html")
    return found


def find_offgrid_sdr() -> Path | None:
    configured = h.setting("offgrid_sdr_path")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    for candidate in _candidates():
        if candidate.is_file():
            return candidate
    return None


def _is_local_request() -> bool:
    return (request.remote_addr or "") in LOCAL_ADDRESSES


def _can_run_here() -> bool:
    return current_app.config["MODE"] == "desktop" or _is_local_request()


# ---------------------------------------------------------------------------
# Memories file
# ---------------------------------------------------------------------------

def _memory(freq_mhz: float, mode: str | None) -> dict:
    """One OFFgrid-SDR memory. Broadcast FM goes to its FM band, everything else to
    general coverage (which accepts any frequency the tuner reaches)."""
    hz = int(round(freq_mhz * 1e6))
    if 87.5e6 <= hz <= 108e6:
        return {"band": "FM", "freqHz": hz, "mode": "WBFM", "swBand": None, "sb": "both",
                "bw": 180000, "step": 100000, "stereo": "stereo"}
    am = (mode or "").upper() == "AM" or 118e6 <= hz <= 137e6  # airband is AM
    return {"band": "GEN", "freqHz": hz, "mode": "AM" if am else "NBFM", "swBand": None,
            "sb": "both", "bw": 8000 if am else 10000, "cwbw": 250,
            "step": 8330 if am and 118e6 <= hz <= 137e6 else 12500,
            "ssbbw": 2000, "ssbshift": 0, "anf": False, "nr": 0, "nb": False, "hum": 0}


def build_memories(channels: list[tuple[float, str | None, str]]) -> tuple[dict, list[dict]]:
    """``channels`` = [(MHz, mode, label)] → (OFFgrid backup dict, list shown to the user).

    Duplicated frequencies are dropped, out-of-range ones skipped, at most 50 kept.
    """
    seen: set[int] = set()
    memories: list[dict] = []
    listing: list[dict] = []
    for mhz, mode, label in channels:
        if mhz is None or not (OFFGRID_MIN_HZ <= mhz * 1e6 <= OFFGRID_MAX_HZ):
            continue
        key = int(round(mhz * 1e3))
        if key in seen:
            continue
        seen.add(key)
        memories.append(_memory(mhz, mode))
        listing.append({"slot": len(memories), "mhz": mhz, "label": label,
                        "mode": memories[-1]["mode"]})
        if len(memories) == OFFGRID_SLOTS:
            break
    backup = {
        "app": OFFGRID_APP,
        "version": OFFGRID_BACKUP_VERSION,
        "appVersion": "ShackLog",
        "exported": datetime.now(timezone.utc).isoformat(),
        # Only what we decide; OFFgrid fills every other setting with its defaults.
        "state": {"band": "GEN", "scanSel": list(range(len(memories)))},
        "memories": memories + [None] * (OFFGRID_SLOTS - len(memories)),
    }
    return backup, listing


def _channels_from_request() -> tuple[list[tuple[float, str | None, str]], str]:
    """Repeaters (current filters, nearest first) or a radio configuration's channels."""
    config_id = request.args.get("config")
    if config_id:
        config = h.get_or_404(RadioConfig, config_id)
        rows = (h.live(RadioChannel).filter(RadioChannel.config_id == config.id)
                .order_by(RadioChannel.location).all())
        return [(c.freq_mhz, c.mode, f"#{c.location} {c.name or ''}".strip()) for c in rows], config.title
    from shacklog.web.views import repeaters as rpt

    result = rpt._with_reference(rpt.filtered_query())
    query = result.query
    if result.distance is not None:
        query = query.order_by(result.distance.asc().nulls_last())
    query = query.order_by(Repeater.output_mhz)
    rows = query.limit(200).all()
    label = f"repeaters near {result.center_label}" if result.center_label else "repeaters"
    return [(r.output_mhz, "FM" if r.kind in ("FM", "ECHOLINK", "ALLSTAR") else r.kind,
             r.ident or r.callsign or r.name) for r in rows if r.kind in ("FM", "ECHOLINK", "ALLSTAR")], label


@bp.route("/memories.json")
def memories():
    channels, label = _channels_from_request()
    backup, _listing = build_memories(channels)
    stamp = utcnow().strftime("%Y%m%d")
    name = f"offgrid-sdr-memories-{stamp}.json"
    return Response(json.dumps(backup, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "X-ShackLog-Source": label.encode("ascii", "replace").decode()})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@bp.route("/")
def index():
    s = h.session()
    path = find_offgrid_sdr() if _can_run_here() else None
    configs = (h.live(RadioConfig).order_by(RadioConfig.is_current.desc(),
                                            RadioConfig.config_date.desc()).all())
    channels, source = _channels_from_request()
    _backup, listing = build_memories(channels)
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    recent = (h.live(SwlEntry).filter(SwlEntry.time_utc >= since)
              .order_by(SwlEntry.time_utc.desc()).limit(30).all())
    return render_template(
        "sdr/index.html", path=path, can_run=_can_run_here(), configs=configs,
        listing=listing, source=source, recent=recent, categories=_categories(),
        station=h.default_station(s), demo=request.args.get("demo") is not None,
    )


@bp.route("/app")
def app_page():
    """OFFgrid-SDR itself, served from this origin so WebUSB sees a secure context."""
    if not _can_run_here():
        abort(404)
    path = find_offgrid_sdr()
    if path is None:
        abort(404)
    resp = send_file(path, mimetype="text/html", max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _categories():
    from shacklog.models import SWL_CATEGORIES

    return SWL_CATEGORIES


@bp.route("/heard", methods=["POST"])
def heard():
    """Quick "I hear something" entry from the monitor (htmx or plain form)."""
    from shacklog.web.forms import FieldError, parse_freq

    s = h.session()
    form = request.form
    try:
        freq = parse_freq(form.get("freq_mhz", "")) if form.get("freq_mhz", "").strip() else None
    except FieldError as exc:
        return _heard_reply(f"Frequency: {exc}", error=True)
    if freq is None:
        return _heard_reply("Enter the frequency you're listening to.", error=True)
    station = h.default_station(s)
    entry = SwlEntry(
        time_utc=utcnow().replace(microsecond=0), freq_mhz=freq,
        mode=(form.get("mode") or "FM").upper(),
        category=form.get("category") or "amateur",
        heard=(form.get("heard") or "").strip() or None,
        signal=(form.get("signal") or "").strip() or None,
        notes=(form.get("notes") or "").strip() or None,
        station_id=station.id if station else None,
        tags="sdr-monitor",
    )
    if entry.heard and entry.category in ("amateur", "repeater"):
        entry.heard = entry.heard.replace(" ", "").upper()
    rpt = (h.live(Repeater).filter(Repeater.output_mhz.between(freq - 0.0015, freq + 0.0015))
           .first())
    if rpt is not None:
        entry.repeater_id = rpt.id
        entry.category = "repeater" if entry.category == "amateur" else entry.category
    enrich.enrich_swl(s, entry, is_new=True)
    s.add(entry)
    s.commit()
    msg = f"Logged {bands.format_freq(freq)} MHz" + (f" ({entry.heard})" if entry.heard else "")
    if rpt is not None:
        msg += f" — {rpt.display_name()}"
    return _heard_reply(msg)


def _heard_reply(message: str, error: bool = False):
    if request.headers.get("HX-Request"):
        since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        recent = (h.live(SwlEntry).filter(SwlEntry.time_utc >= since)
                  .order_by(SwlEntry.time_utc.desc()).limit(30).all())
        return render_template("sdr/_recent.html", recent=recent, message=message, error=error)
    flash(message, "error" if error else "ok")
    return redirect(url_for(".index"))
