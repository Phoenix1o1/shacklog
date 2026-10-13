"""Hub side of the sync protocol: ``/api/sync/*`` (JSON over HTTP, bearer token).

Active only when *Settings › Sync › Accept sync* is on (setting ``sync_hub_enabled``).
Every request carries ``Authorization: Bearer <sync_hub_token>``; there is no login and no
CSRF here (``auth.TOKEN_BLUEPRINTS``). Errors are JSON ``{"error": "..."}`` with 401 (bad
or missing token), 403 (sync not accepted here), 409 (schema versions differ) or 400.

Endpoints (the client is services/sync_client.py; PLAN.md §6 describes the protocol):

``POST /hello``           identities and versions; registers the client as a peer
``POST /push``            records from a client, merged with ``apply_records(mode="hub")``
``GET  /pull``            records changed here since ``since`` (not those from the caller)
``POST /blobs/missing``   which of these SHA-256 hashes this instance lacks
``PUT  /blob/<sha256>``   upload a blob (body = raw bytes, verified against the hash)
``GET  /blob/<sha256>``   download a blob
"""

from __future__ import annotations

import hmac
import logging
import re

from flask import Blueprint, abort, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from shacklog import APP_NAME, __version__, migrations
from shacklog.models import SyncPeer, utcnow
from shacklog.services import sync_engine
from shacklog.web import helpers as h

log = logging.getLogger(__name__)

bp = Blueprint("api_sync", __name__, url_prefix="/api/sync")

SHA_RE = re.compile(r"^[0-9a-f]{64}$")
PULL_DEFAULT_LIMIT = 500
PULL_MAX_LIMIT = 2000
#: Keep a pull page to a few MB: library files carry their extracted text.
PULL_MAX_BYTES = 8 * 1024 * 1024
MISSING_MAX_HASHES = 5000


def _error(status: int, message: str, **extra):
    response = jsonify(error=message, **extra)
    response.status_code = status
    return response


@bp.before_request
def authenticate():
    if not h.setting("sync_hub_enabled"):
        return _error(403, "This instance doesn't accept sync. Turn on “Accept sync from "
                           "other instances” in Settings › Sync on the hub.")
    expected = h.setting("sync_hub_token") or ""
    if not expected:
        return _error(403, "The hub has no sync token yet. Generate one in Settings › Sync.")
    header = request.headers.get("Authorization", "")
    sent = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not sent or not hmac.compare_digest(sent.encode(), expected.encode()):
        return _error(401, "The sync token is wrong. Copy it again from Settings › Sync on the hub.")
    return None


@bp.errorhandler(HTTPException)
def http_error(exc: HTTPException):
    return _error(exc.code or 500, exc.description or exc.name)


@bp.errorhandler(Exception)
def unexpected_error(exc: Exception):
    log.exception("Sync API error on %s", request.path)
    h.session().rollback()
    return _error(500, f"The hub failed to process the request ({type(exc).__name__}). "
                       "Its log file has the details.")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Expected a JSON object.")
    return data


def _schema_mismatch(client_schema) -> object | None:
    """409 response when the client's schema differs from ours, saying who must update."""
    if client_schema is None:
        return None
    try:
        client_schema = int(client_schema)
    except (TypeError, ValueError):
        return _error(400, "schema_version must be a number.")
    if client_schema == migrations.LATEST:
        return None
    update = "hub" if client_schema > migrations.LATEST else "client"
    who = "the hub" if update == "hub" else "this computer"
    return _error(409, f"Database versions differ (hub {migrations.LATEST}, client "
                       f"{client_schema}). Update ShackLog on {who} first.",
                  update=update, hub_schema=migrations.LATEST, client_schema=client_schema)


def _peer(data: dict) -> SyncPeer:
    """Find or register the calling client (identified by its instance id)."""
    instance_id = str(data.get("instance_id") or request.args.get("instance_id") or "").strip()
    if not instance_id or len(instance_id) > 36:
        abort(400, description="instance_id is missing.")
    if instance_id == h.database().instance_id:
        abort(409, description="That is this instance's own id: a ShackLog can't sync with itself.")
    s = h.session()
    peer = s.get(SyncPeer, instance_id)
    if peer is None:
        peer = SyncPeer(instance_id=instance_id, role="client", last_pull_seq=0, last_push_seq=0)
        s.add(peer)
    name = data.get("instance_name")
    if name:
        peer.name = str(name)[:120]
    peer.last_sync_at = utcnow()
    return peer


def _identity() -> dict:
    db = h.database()
    return {"app": APP_NAME, "app_version": __version__, "instance_id": db.instance_id,
            "instance_name": db.instance_name, "schema_version": migrations.LATEST,
            "current_seq": sync_engine.current_seq(h.session())}


@bp.route("/hello", methods=["POST"])
def hello():
    data = _body()
    mismatch = _schema_mismatch(data.get("schema_version", -1))
    if mismatch is not None:
        return mismatch
    peer = _peer(data)
    peer.last_status = "hello"
    peer.last_error = None
    h.session().commit()
    # received_seq: how far into the client's changes we are. A client trusts the lower of
    # this and its own watermark, so a hub restored from a backup gets re-sent what it lost.
    return jsonify(**_identity(), received_seq=peer.last_pull_seq or 0,
                   server_time=utcnow().isoformat())


@bp.route("/push", methods=["POST"])
def push():
    data = _body()
    mismatch = _schema_mismatch(data.get("schema_version"))
    if mismatch is not None:
        return mismatch
    records = data.get("records") or []
    if not isinstance(records, list):
        abort(400, description="records must be a list.")
    try:
        last_pull = int(data.get("last_pull_seq") or 0)
        batch_last = data.get("batch_last_seq")
        batch_last = int(batch_last) if batch_last is not None else None
    except (TypeError, ValueError):
        abort(400, description="last_pull_seq and batch_last_seq must be numbers.")
    s = h.session()
    peer = _peer(data)
    stats = sync_engine.apply_records(s, records, peer.instance_id, peer.name, mode="hub",
                                      client_last_pull=last_pull)
    if batch_last is not None:
        peer.last_pull_seq = batch_last  # the client's changes we now hold
    peer.last_push_seq = last_pull  # our changes the client has (informational)
    peer.last_status = "ok"
    from shacklog.services import merge

    merge.apply_merges(s)  # merges the client made: re-point what refers to the old copy here
    s.commit()
    return jsonify(ok=True, stats=stats, current_seq=sync_engine.current_seq(s))


@bp.route("/pull", methods=["GET"])
def pull():
    try:
        since = max(0, int(request.args.get("since", 0)))
        limit = min(PULL_MAX_LIMIT, max(1, int(request.args.get("limit", PULL_DEFAULT_LIMIT))))
    except ValueError:
        abort(400, description="since and limit must be numbers.")
    s = h.session()
    instance_id = str(request.args.get("instance_id") or "").strip()
    if not instance_id:
        abort(400, description="instance_id is missing.")
    # include_own=1: a client restored from a backup wants its own records back too.
    include_own = request.args.get("include_own") in ("1", "true")
    # Read first, write the "last seen" afterwards: building a page must not hold the
    # database write lock.
    page = sync_engine.changes_page(s, since, exclude_peer=None if include_own else instance_id,
                                    limit=limit, max_bytes=PULL_MAX_BYTES)
    current = sync_engine.current_seq(s)
    _peer({"instance_id": instance_id})
    s.commit()
    return jsonify(records=page.records, last_seq=page.last_seq, more=page.more,
                   current_seq=current)


@bp.route("/blobs/missing", methods=["POST"])
def blobs_missing():
    hashes = _body().get("hashes") or []
    if not isinstance(hashes, list) or len(hashes) > MISSING_MAX_HASHES:
        abort(400, description=f"hashes must be a list of at most {MISSING_MAX_HASHES}.")
    store = h.filestore()
    missing = [x for x in dict.fromkeys(hashes)
               if isinstance(x, str) and SHA_RE.match(x) and not store.exists(x)]
    return jsonify(missing=missing)


@bp.route("/blob/<sha256>", methods=["PUT"])
def put_blob(sha256: str):
    if not SHA_RE.match(sha256):
        abort(400, description="Not a SHA-256 hash.")
    store = h.filestore()
    if store.exists(sha256):
        while request.stream.read(1024 * 1024):  # drain, so keep-alive connections stay usable
            pass
        return jsonify(ok=True, sha256=sha256, stored=False)
    if not sync_engine.store_blob_verified(store, request.stream, sha256):
        return _error(400, "The uploaded bytes don't match their SHA-256: transfer damaged, "
                           "nothing was linked to them. Sync again.")
    return jsonify(ok=True, sha256=sha256, stored=True)


@bp.route("/blob/<sha256>", methods=["GET"])
def get_blob(sha256: str):
    if not SHA_RE.match(sha256) or not h.filestore().exists(sha256):
        return _error(404, "This instance doesn't have that file.")
    return send_file(h.filestore().path_for(sha256), mimetype="application/octet-stream",
                     as_attachment=True, download_name=sha256, conditional=False, etag=False)
