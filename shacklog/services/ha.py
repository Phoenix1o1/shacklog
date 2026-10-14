"""Home Assistant: one JSON document with the station's state, for HA's ``rest`` sensors.

``GET /api/ha/state`` (web/views/api_ha.py) returns :func:`state`. Home Assistant polls
it (one request feeds all its sensors) with a **read-only token** of its own: the sync
token could rewrite the whole logbook, this one only reads this summary. The token is
generated in *Settings › General › Home Assistant*, kept as a SHA-256 hash (it is shown
once), and is a local secret: never synced, never in bundles (prefs.SECRET_KEYS).

What it reports, all times UTC with a timezone (HA ``timestamp`` sensors need one):

* ``qsos`` / ``swl`` – counts (total, today UTC) and the latest entry;
* ``diary`` – the latest diary entry's title;
* ``active`` – something logged in the last :data:`ACTIVE_MINUTES` minutes;
* ``backup`` – time of the newest database backup;
* ``sync`` – on the hub: when each PC last synced (the hub can't see a PC's failures,
  only its silence, so ``oldest_hours`` is the number to alert on); on a PC: its own
  sync state;
* ``station``, ``instance`` and ``update_available``.

Entries logged on a PC reach the hub when that PC syncs (every 15 minutes by default):
the hub's numbers lag by that much. Pure module: no Flask.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from shacklog.models import JournalEntry, Qso, SwlEntry, SyncPeer

ACTIVE_MINUTES = 30
TOKEN_PREFIX = "shk_ha_"


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def new_token() -> tuple[str, str]:
    """(token to show once, hash to store)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_ok(sent: str, stored_hash: str | None) -> bool:
    return bool(sent and stored_hash) and hmac.compare_digest(token_hash(sent), stored_hash)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def iso(value: datetime | None) -> str | None:
    """Naive UTC (how ShackLog stores times) → ISO 8601 with +00:00."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc, microsecond=0).isoformat()


def _hours(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else round((now - since).total_seconds() / 3600, 1)


def _qso(q: Qso | None) -> dict | None:
    if q is None:
        return None
    return {"call": q.call, "time_utc": iso(q.time_on), "freq_mhz": q.freq_mhz,
            "band": q.band, "mode": q.submode or q.mode, "country": q.country,
            "name": q.name, "distance_km": q.distance_km}


def _swl(e: SwlEntry | None) -> dict | None:
    if e is None:
        return None
    return {"heard": e.heard, "time_utc": iso(e.time_utc), "freq_mhz": e.freq_mhz,
            "band": e.band, "mode": e.mode, "category": e.category}


def state(s: Session, *, now: datetime, instance: dict, station=None,
          backups: list[dict] | None = None, sync_status: dict | None = None,
          hub: bool = False, update_behind: int | None = None) -> dict:
    """The JSON document (see the module docstring). ``now`` is naive UTC."""
    day = datetime(now.year, now.month, now.day)
    recent = now - timedelta(minutes=ACTIVE_MINUTES)

    qsos = s.query(Qso).filter(Qso.deleted_at.is_(None))
    swl = s.query(SwlEntry).filter(SwlEntry.deleted_at.is_(None))
    last_qso = qsos.order_by(Qso.time_on.desc()).first()
    last_swl = swl.order_by(SwlEntry.time_utc.desc()).first()
    diary = (s.query(JournalEntry).filter(JournalEntry.deleted_at.is_(None),
                                          JournalEntry.kind == "diary")
             .order_by(JournalEntry.time_utc.desc()).first())
    # Future-dated entries (a clock off by an hour) don't count as "on the air now".
    active = any(t is not None and recent <= t <= now + timedelta(minutes=5)
                 for t in (last_qso.time_on if last_qso else None,
                           last_swl.time_utc if last_swl else None))

    newest_backup = backups[0]["created"] if backups else None

    if hub:
        clients = (s.query(SyncPeer).filter(SyncPeer.role == "client")
                   .order_by(SyncPeer.last_sync_at.desc()).all())
        peers = [{"name": p.name or p.instance_id[:8], "last_sync": iso(p.last_sync_at),
                  "hours_ago": _hours(p.last_sync_at, now), "status": p.last_status}
                 for p in clients]
        last = max((p.last_sync_at for p in clients if p.last_sync_at), default=None)
        ages = [p["hours_ago"] for p in peers if p["hours_ago"] is not None]
        sync = {"role": "hub", "last": iso(last), "peers": peers,
                "oldest_hours": max(ages) if ages else None, "state": "hub", "error": None}
    else:
        status = sync_status or {}
        last_run = status.get("last_run")
        sync = {"role": "pc", "last": iso(last_run) if isinstance(last_run, datetime) else None,
                "peers": [], "oldest_hours": None,
                "state": status.get("state") or ("idle" if status.get("configured") else "off"),
                "error": status.get("last_error")}

    return {
        "instance": instance,
        "updated": iso(now),
        "active": active,
        "qsos": {"total": qsos.count(),
                 "today": qsos.filter(Qso.time_on >= day).count(),
                 "last": _qso(last_qso)},
        "swl": {"total": swl.count(),
                "today": swl.filter(SwlEntry.time_utc >= day).count(),
                "last": _swl(last_swl)},
        "diary": {"last": None if diary is None else
                  {"title": diary.title, "time_utc": iso(diary.time_utc)}},
        "countries": s.query(func.count(func.distinct(Qso.country)))
                      .filter(Qso.deleted_at.is_(None), Qso.country.isnot(None),
                              Qso.country != "").scalar() or 0,
        "backup": {"last": iso(newest_backup), "hours_ago": _hours(newest_backup, now)},
        "sync": sync,
        "station": None if station is None else {
            "name": station.name, "callsign": station.callsign, "locator": station.locator,
            "latitude": station.latitude, "longitude": station.longitude},
        "update_available": bool(update_behind),
    }


def yaml_config(base_url: str, prefix: str = "ShackLog") -> str:
    """A ready-to-paste ``configuration.yaml`` block for HA's RESTful integration.

    The token goes in ``secrets.yaml`` as ``shacklog_token: "Bearer <token>"``.
    """
    url = base_url.rstrip("/") + "/api/ha/state"
    p, k = prefix, prefix.lower()
    return f"""rest:
  - resource: {url}
    headers:
      Authorization: !secret shacklog_token
    scan_interval: 60
    sensor:
      - name: {p} QSOs today
        unique_id: {k}_qsos_today
        value_template: "{{{{ value_json.qsos.today }}}}"
        state_class: measurement
      - name: {p} QSOs
        unique_id: {k}_qsos_total
        value_template: "{{{{ value_json.qsos.total }}}}"
        state_class: total
      - name: {p} last QSO
        unique_id: {k}_last_qso
        value_template: "{{{{ value_json.qsos.last.call if value_json.qsos.last else 'none' }}}}"
        json_attributes_path: "$.qsos.last"
        json_attributes: [time_utc, freq_mhz, band, mode, country, name, distance_km]
      - name: {p} last heard
        unique_id: {k}_last_heard
        value_template: "{{{{ value_json.swl.last.heard if value_json.swl.last else 'none' }}}}"
        json_attributes_path: "$.swl.last"
        json_attributes: [time_utc, freq_mhz, band, mode, category]
      - name: {p} countries
        unique_id: {k}_countries
        value_template: "{{{{ value_json.countries }}}}"
      - name: {p} last backup
        unique_id: {k}_last_backup
        device_class: timestamp
        value_template: "{{{{ value_json.backup.last }}}}"
      - name: {p} last sync
        unique_id: {k}_last_sync
        device_class: timestamp
        value_template: "{{{{ value_json.sync.last }}}}"
        json_attributes_path: "$.sync"
        json_attributes: [peers, oldest_hours, state, error]
      - name: {p} hours since a PC synced
        unique_id: {k}_sync_oldest_hours
        unit_of_measurement: h
        value_template: "{{{{ value_json.sync.oldest_hours }}}}"
    binary_sensor:
      - name: {p} on the air
        unique_id: {k}_active
        value_template: "{{{{ value_json.active }}}}"
      - name: {p} update available
        unique_id: {k}_update
        device_class: update
        value_template: "{{{{ value_json.update_available }}}}"
"""

