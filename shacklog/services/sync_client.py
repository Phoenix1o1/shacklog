"""Client side of the sync protocol (PLAN.md §6), plus the background sync worker.

One run, every step idempotent so an interrupted sync simply repeats:

1. ``hello``  — exchange identities and schema versions (a mismatch stops here).
2. ``push``   — our records with ``change_seq > last_push_seq`` that did not come from
   the hub, in batches; ``last_push_seq`` advances after each batch the hub acknowledges.
3. blobs up   — ask which file hashes the hub lacks, upload them.
4. ``pull``   — hub records changed since ``last_pull_seq`` that did not come from us, in
   pages; each page is merged and its watermark saved in the same transaction.
5. blobs down — download library files we lack (setting ``sync_pull_files``), otherwise
   they are fetched on demand by :func:`fetch_blob_from_hub`.

Watermarks live in the local ``sync_peers`` row of the hub (``role="hub"``).

HTTP goes through a *transport*: ``transport(method, path, json=None, data=None,
headers=None) -> (status, body)`` where ``path`` starts with ``/api/sync`` and ``body`` is
the decoded JSON (or raw bytes for blobs). The default uses urllib; tests pass one built
on the hub app's Flask test client.
"""

from __future__ import annotations

import json as jsonlib
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from shacklog import APP_NAME, __version__, migrations
from shacklog.db import get_setting, set_setting
from shacklog.models import StoredFile, SyncPeer, utcnow
from shacklog.prefs import DEFAULTS
from shacklog.services.filestore import is_sha256
from shacklog.services import sync_engine

log = logging.getLogger(__name__)

API = "/api/sync"
Transport = Callable[..., tuple[int, Any]]

#: Local setting holding the hub id while a "pull everything, own records too" recovery
#: is unfinished (see SyncClient._check_restores).
RECOVER_SETTING = "sync_recover_from_hub"
#: Same for "push everything, records received from the hub too" after a hub restore.
RECOVER_PUSH_SETTING = "sync_recover_push_to_hub"

#: One sync at a time per process (worker, "Sync now" and the CLI share it).
_RUN_LOCK = threading.Lock()


class SyncError(Exception):
    """A sync step failed; the message is meant for the Settings page."""


class SyncBusy(SyncError):
    pass


def normalize_hub_url(url: str | None) -> str:
    """``192.168.1.20:8573/`` → ``http://192.168.1.20:8573`` (also strips ``/api/sync``)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    if url.endswith(API):
        url = url[: -len(API)].rstrip("/")
    return url


def _setting(session, key: str):
    return get_setting(session, key, DEFAULTS.get(key))


# ---------------------------------------------------------------------------
# HTTP transport (urllib)
# ---------------------------------------------------------------------------

class UrllibTransport:
    """Default transport. JSON in and out; blobs as bytes or streamed to a file."""

    def __init__(self, base_url: str, timeout: float = 30.0, blob_timeout: float = 300.0):
        self.base_url = normalize_hub_url(base_url)
        self.timeout = timeout
        self.blob_timeout = blob_timeout

    def _request(self, method, path, json=None, data=None, headers=None):
        headers = dict(headers or {})
        headers.setdefault("User-Agent", f"{APP_NAME}/{__version__}")
        headers.setdefault("Accept", "application/json")
        body = data
        if json is not None:
            body = jsonlib.dumps(json).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if hasattr(body, "read") and "Content-Length" not in headers:
            headers["Content-Length"] = str(os.fstat(body.fileno()).st_size)
        req = urllib.request.Request(self.base_url + path, data=body, method=method, headers=headers)
        timeout = self.blob_timeout if "/blob/" in path else self.timeout
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc  # carries status and body
        except urllib.error.URLError as exc:
            raise SyncError(f"Couldn't reach the hub at {self.base_url}: {exc.reason}.") from None
        except (TimeoutError, OSError) as exc:
            raise SyncError(f"Couldn't reach the hub at {self.base_url}: {exc}.") from None

    @staticmethod
    def _decode(resp) -> tuple[int, Any]:
        status = resp.status if hasattr(resp, "status") else resp.code
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "") if resp.headers else ""
        if "json" in ctype:
            try:
                return status, jsonlib.loads(raw.decode("utf-8"))
            except ValueError:
                pass
        return status, raw

    def __call__(self, method, path, json=None, data=None, headers=None):
        resp = self._request(method, path, json=json, data=data, headers=headers)
        try:
            return self._decode(resp)
        except (TimeoutError, OSError) as exc:
            raise SyncError(f"The connection to the hub dropped: {exc}.") from None
        finally:
            resp.close()

    def download(self, path, out, headers=None) -> int:
        """Stream a GET body into the binary file ``out``; returns the HTTP status."""
        resp = self._request("GET", path, headers=headers)
        try:
            status = resp.status if hasattr(resp, "status") else resp.code
            if status == 200:
                while chunk := resp.read(1024 * 1024):
                    out.write(chunk)
            return status
        finally:
            resp.close()


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class SyncClient:
    """Runs the protocol against one hub. Create it per run; it holds no open sessions."""

    push_batch = 500
    push_max_bytes = 4 * 1024 * 1024
    pull_batch = 500
    hash_batch = 1000

    def __init__(self, db, filestore, hub_url: str, token: str,
                 transport: Transport | None = None):
        self.db = db
        self.filestore = filestore
        self.hub_url = normalize_hub_url(hub_url)
        self.token = (token or "").strip()
        self.transport = transport or UrllibTransport(self.hub_url)
        self.hub: dict | None = None
        #: Set by hello() when the hub holds more of our changes than we have: this
        #: database was restored from a backup, so pull everything, our own rows included.
        self.restored = False
        self._restores_checked = False

    @classmethod
    def from_settings(cls, db, filestore, transport: Transport | None = None) -> "SyncClient | None":
        """A client for the hub saved in Settings, or None when sync isn't configured."""
        with db.transaction() as s:
            url, token = _setting(s, "sync_hub_url"), _setting(s, "sync_client_token")
        if not url or not token:
            return None
        return cls(db, filestore, url, token, transport)

    # -- plumbing --------------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(extra or {})
        return headers

    def _call(self, method: str, path: str, json: Any = None, data: Any = None,
              headers: dict | None = None, ok=(200,)) -> Any:
        try:
            status, body = self.transport(method, API + path, json=json, data=data,
                                          headers=self._headers(headers))
        except SyncError:
            raise
        except Exception as exc:  # a broken transport must not crash the caller
            raise SyncError(f"Sync request {path} failed: {exc}") from exc
        if status in ok:
            return body
        message = body.get("error") if isinstance(body, dict) else None
        if status == 401:
            raise SyncError(message or "The hub refused the token. Copy it again from the hub's "
                                       "Settings › Sync page.")
        if status == 403:
            raise SyncError(message or "The hub doesn't accept sync right now.")
        if status == 404 and message is None:
            raise SyncError(f"No ShackLog sync service at {self.hub_url}. Check the hub URL.")
        raise SyncError(message or f"The hub answered {status} to {path}.")

    def _identity(self) -> dict:
        return {"instance_id": self.db.instance_id, "instance_name": self.db.instance_name,
                "schema_version": migrations.LATEST, "app_version": __version__}

    def _peer(self, s) -> SyncPeer:
        assert self.hub is not None
        peer = s.get(SyncPeer, self.hub["instance_id"])
        if peer is None:
            peer = SyncPeer(instance_id=self.hub["instance_id"], role="hub",
                            last_pull_seq=0, last_push_seq=0)
            s.add(peer)
        peer.role = "hub"
        peer.name = self.hub.get("instance_name") or peer.name
        peer.url = self.hub_url
        return peer

    # -- steps -----------------------------------------------------------

    def _check_restores(self, s, peer: SyncPeer) -> None:
        """Notice a database restored from a backup, on either side, and recover.

        Watermarks assume both databases only move forward. After a restore they lie, so
        the affected watermarks go back to 0 and everything is exchanged again — merging
        is idempotent, so the only cost is time. Saved in the database (not in memory) so
        an interrupted recovery carries on at the next run.
        """
        if self._restores_checked:
            return
        self._restores_checked = True
        received = self.hub.get("received_seq")
        hub_seq = self.hub.get("current_seq")
        hub_lost = ((isinstance(received, int) and received < (peer.last_push_seq or 0))
                    or (isinstance(hub_seq, int) and hub_seq < (peer.last_pull_seq or 0)))
        if hub_lost:
            # Send everything, including what we once received from the hub: it may be
            # the only copy left of records the hub lost.
            log.warning("The hub is behind this instance (restored from a backup?): "
                        "exchanging everything again")
            peer.last_push_seq = 0
            peer.last_pull_seq = 0
            set_setting(s, RECOVER_PUSH_SETTING, peer.instance_id)
        if self.restored:
            log.warning("This database is older than what the hub received from it "
                        "(restored from a backup?): pulling everything again")
            peer.last_pull_seq = 0
            set_setting(s, RECOVER_SETTING, peer.instance_id)

    def hello(self) -> dict:
        """Check the connection and versions; returns the hub's identity."""
        our_seq = self.db.current_seq()
        body = dict(self._identity(), current_seq=our_seq)
        hub = self._call("POST", "/hello", json=body)
        if not isinstance(hub, dict) or not hub.get("instance_id"):
            raise SyncError(f"{self.hub_url} answered, but not like a ShackLog hub.")
        if hub["instance_id"] == self.db.instance_id:
            raise SyncError("The hub URL points to this very instance. Enter the hub's address.")
        if int(hub.get("schema_version", -1)) != migrations.LATEST:
            raise SyncError(f"Database versions differ (hub {hub.get('schema_version')}, this "
                            f"computer {migrations.LATEST}). Update ShackLog on both first.")
        received = hub.get("received_seq")
        # The hub holds more of our changes than we ever made: this database was restored
        # from a backup. Pull everything again, our own records included.
        self.restored = isinstance(received, int) and received > our_seq
        self.hub = hub
        return hub

    def _push(self, stats: dict) -> None:
        hub_id = self.hub["instance_id"]
        with self.db.transaction() as s:
            peer = self._peer(s)
            self._check_restores(s, peer)
            last_pull = peer.last_pull_seq or 0
            start = peer.last_push_seq or 0
            push_all = get_setting(s, RECOVER_PUSH_SETTING) == hub_id
        while True:
            s = self.db.session()
            try:
                page = sync_engine.changes_page(s, start, exclude_peer=None if push_all else hub_id,
                                                limit=self.push_batch, max_bytes=self.push_max_bytes)
            finally:
                s.close()
            if page.last_seq <= start and not page.records:
                break
            # Sent even when every row was skipped: the hub learns how far we are, which
            # is what lets a restored hub (or a restored client) be detected at hello.
            reply = self._call("POST", "/push", json=dict(
                self._identity(), last_pull_seq=last_pull, batch_last_seq=page.last_seq,
                records=page.records))
            hub_stats = (reply or {}).get("stats", {}) if isinstance(reply, dict) else {}
            stats["pushed"] += len(page.records)
            stats["hub_conflicts"] += int(hub_stats.get("conflicts", 0) or 0)
            with self.db.transaction() as s:  # acknowledged: never send this batch again
                self._peer(s).last_push_seq = page.last_seq
            if page.last_seq <= start:  # no progress: stop rather than loop forever
                break
            start = page.last_seq
            if not page.more:
                break
        if self.restored and start < (self.hub.get("received_seq") or 0):
            # Nothing new to send, but the hub must learn that our counter restarted lower,
            # or every following run would take this database for restored again.
            self._call("POST", "/push", json=dict(self._identity(), last_pull_seq=last_pull,
                                                  batch_last_seq=start, records=[]))
        if push_all:
            with self.db.transaction() as s:
                set_setting(s, RECOVER_PUSH_SETTING, None)

    def _local_hashes(self) -> list[str]:
        """Blobs referenced by library entries and present here. Trashed entries count
        too: they can be restored, and their bytes must survive this computer."""
        s = self.db.session()
        try:
            hashes = {h for (h,) in s.query(StoredFile.sha256).distinct() if h}
        finally:
            s.close()
        return sorted(h for h in hashes if self.filestore.exists(h))

    def _upload_blobs(self, stats: dict) -> None:
        hashes = self._local_hashes()
        for i in range(0, len(hashes), self.hash_batch):
            reply = self._call("POST", "/blobs/missing", json={"hashes": hashes[i:i + self.hash_batch]})
            for sha in (reply or {}).get("missing", []):
                if not self.filestore.exists(sha):
                    continue
                with self.filestore.open(sha) as fh:
                    size = os.fstat(fh.fileno()).st_size
                    try:
                        self._call("PUT", f"/blob/{sha}", data=fh, headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(size)})
                        stats["blobs_up"] += 1
                    except SyncError as exc:
                        stats["problems"].append(f"Upload of file {sha[:12]}… failed: {exc}")

    def _pull(self, stats: dict) -> None:
        hub_id, hub_name = self.hub["instance_id"], self.hub.get("instance_name")
        with self.db.transaction() as s:
            peer = self._peer(s)
            since, dirty_after = peer.last_pull_seq or 0, peer.last_push_seq or 0
            include_own = get_setting(s, RECOVER_SETTING) == hub_id
        params = {"limit": self.pull_batch, "instance_id": self.db.instance_id}
        if include_own:
            params["include_own"] = 1
        while True:
            query = urllib.parse.urlencode(dict(params, since=since))
            reply = self._call("GET", f"/pull?{query}")
            if not isinstance(reply, dict):
                raise SyncError("The hub sent an unreadable pull reply.")
            records, last_seq = reply.get("records") or [], int(reply.get("last_seq", since))
            with self.db.transaction() as s:
                # Merge and move the watermark in one transaction: all or nothing.
                result = sync_engine.apply_records(s, records, hub_id, hub_name, mode="client",
                                                   local_dirty_after=dirty_after)
                self._peer(s).last_pull_seq = max(since, last_seq)
            stats["pulled"] += len(records)
            for key in ("inserted", "updated", "conflicts"):
                stats[key] += result.get(key, 0)
            if not reply.get("more") or last_seq <= since:
                break
            since = last_seq
        if include_own:
            with self.db.transaction() as s:
                set_setting(s, RECOVER_SETTING, None)
        # A merge made on another instance: re-point what still refers to the merged copy
        # here (local edits, pushed with the next sync).
        from shacklog.services import merge

        with self.db.transaction() as s:
            merge.apply_merges(s)

    def _missing_blobs(self) -> list[str]:
        s = self.db.session()
        try:
            hashes = {h for (h,) in s.query(StoredFile.sha256)
                      .filter(StoredFile.deleted_at.is_(None)).distinct() if h}
        finally:
            s.close()
        return sorted(h for h in hashes if not self.filestore.exists(h))

    def download_blob(self, sha: str) -> bool:
        """Fetch one blob from the hub into the file store. False if the hub lacks it."""
        if not is_sha256(sha):
            return False
        path = f"{API}/blob/{sha}"
        if hasattr(self.transport, "download"):
            tmp = self.filestore.root / f".download-{sha[:16]}-{threading.get_ident()}"
            try:
                with open(tmp, "wb") as out:
                    status = self.transport.download(path, out, headers=self._headers())
                if status != 200:
                    if status in (401, 403):
                        raise SyncError("The hub refused the token while downloading a file.")
                    return False
                with open(tmp, "rb") as fh:
                    if not sync_engine.store_blob_verified(self.filestore, fh, sha):
                        raise SyncError(f"File {sha[:12]}… arrived damaged (hash mismatch).")
                return True
            finally:
                tmp.unlink(missing_ok=True)
        body = self._call("GET", f"/blob/{sha}", ok=(200, 404))
        if not isinstance(body, (bytes, bytearray)):
            return False
        import io
        if not sync_engine.store_blob_verified(self.filestore, io.BytesIO(body), sha):
            raise SyncError(f"File {sha[:12]}… arrived damaged (hash mismatch).")
        return True

    def _download_blobs(self, stats: dict) -> None:
        for sha in self._missing_blobs():
            try:
                if self.download_blob(sha):
                    stats["blobs_down"] += 1
                else:
                    stats["blobs_unavailable"] += 1
            except SyncError as exc:
                stats["problems"].append(str(exc))

    # -- the whole run ---------------------------------------------------

    def run(self) -> dict:
        """One complete sync. Returns statistics; raises :class:`SyncError` on failure."""
        if not _RUN_LOCK.acquire(blocking=False):
            raise SyncBusy("A sync is already running.")
        started = time.monotonic()
        stats: dict[str, Any] = {"pushed": 0, "hub_conflicts": 0, "pulled": 0, "inserted": 0,
                                 "updated": 0, "conflicts": 0, "blobs_up": 0, "blobs_down": 0,
                                 "blobs_unavailable": 0, "problems": []}
        try:
            self.hello()
            stats["hub_name"] = self.hub.get("instance_name")
            if self.restored:
                # Catch up first, so the stale rows of the restored database don't reach
                # the hub as concurrent edits; then send what was really changed here.
                with self.db.transaction() as s:
                    self._check_restores(s, self._peer(s))
                self._pull(stats)
            self._push(stats)
            self._upload_blobs(stats)
            self._pull(stats)
            with self.db.transaction() as s:
                pull_files = bool(_setting(s, "sync_pull_files"))
            if pull_files:
                self._download_blobs(stats)
            stats["duration_s"] = round(time.monotonic() - started, 2)
            self._record("ok", "; ".join(stats["problems"])[:2000] or None)
            return stats
        except Exception as exc:
            message = str(exc) if isinstance(exc, SyncError) else f"Unexpected error: {exc}"
            self._record("error", message)
            if isinstance(exc, SyncError):
                raise
            raise SyncError(message) from exc
        finally:
            _RUN_LOCK.release()

    def _record(self, status: str, error: str | None) -> None:
        """Remember the outcome on the hub's peer row (best effort)."""
        try:
            with self.db.transaction() as s:
                if self.hub is not None:
                    peer = self._peer(s)
                else:
                    peer = (s.query(SyncPeer).filter(SyncPeer.role == "hub",
                                                     SyncPeer.url == self.hub_url).first())
                    if peer is None:
                        return
                peer.last_sync_at = utcnow()
                peer.last_status = status
                peer.last_error = error
        except Exception:
            log.exception("Couldn't record the sync status")


def summary_text(stats: dict) -> str:
    """One line for a flash message or the log."""
    parts = [f"sent {stats.get('pushed', 0)}", f"received {stats.get('pulled', 0)}"]
    files = stats.get("blobs_up", 0) + stats.get("blobs_down", 0)
    if files:
        parts.append(f"{files} files transferred")
    conflicts = stats.get("conflicts", 0) + stats.get("hub_conflicts", 0)
    if conflicts:
        parts.append(f"{conflicts} conflicts kept for review")
    text = f"Synced with {stats.get('hub_name') or 'the hub'}: " + ", ".join(parts) + "."
    if stats.get("problems"):
        text += f" {len(stats['problems'])} problems: {stats['problems'][0]}"
    return text


def fetch_blob_from_hub(db, filestore, sha256: str, transport: Transport | None = None) -> bool:
    """Download one library file from the configured hub (used when ``sync_pull_files``
    is off and a file is opened). False when sync isn't configured or the hub lacks it."""
    if filestore.exists(sha256):
        return True
    client = SyncClient.from_settings(db, filestore, transport)
    if client is None:
        return False
    try:
        return client.download_blob(sha256)
    except SyncError as exc:
        log.warning("On-demand download of %s failed: %s", sha256[:12], exc)
        return False


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class SyncWorker:
    """Daemon thread syncing every ``sync_interval_min`` minutes and on :meth:`trigger`.

    Settings are re-read every cycle, so changes on the Sync page apply without a
    restart. The current state goes to ``app.config["SYNC_STATUS"]`` (footer pill).
    """

    #: How often the loop wakes up to re-read settings when nothing is due.
    poll_s = 30.0
    #: Delay before the first automatic sync after start.
    first_delay_s = 5.0

    def __init__(self, app=None, transport: Transport | None = None):
        self.app = app
        self.transport = transport
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._triggered = False

    # -- control ---------------------------------------------------------

    def start(self, app=None) -> "SyncWorker":
        if app is not None:
            self.app = app
        if self.app is None:
            raise ValueError("SyncWorker needs the Flask app")
        self.app.config["SYNC_WORKER"] = self
        if self._thread is None or not self._thread.is_alive():
            self._stopping.clear()
            self._thread = threading.Thread(target=self._loop, name="shacklog-sync", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def trigger(self) -> None:
        """Sync as soon as possible (the "Sync now" button)."""
        self._triggered = True
        self._wake.set()

    # -- loop ------------------------------------------------------------

    def _set_status(self, configured: bool, state: str, label: str,
                    last_run=None, last_error=None) -> None:
        previous = self.app.config.get("SYNC_STATUS") or {}
        self.app.config["SYNC_STATUS"] = {
            "configured": configured, "state": state, "label": label,
            "last_run": last_run if last_run is not None else previous.get("last_run"),
            "last_error": last_error,
        }

    def _read_settings(self) -> tuple[bool, int]:
        db = self.app.config["DB"]
        with db.transaction() as s:
            configured = bool(_setting(s, "sync_hub_url") and _setting(s, "sync_client_token"))
            try:
                interval = max(0, int(_setting(s, "sync_interval_min") or 0))
            except (TypeError, ValueError):
                interval = 0
        return configured, interval

    def _loop(self) -> None:
        next_due = time.monotonic() + self.first_delay_s
        while not self._stopping.is_set():
            configured, interval = False, 0
            try:
                configured, interval = self._read_settings()
                due = interval > 0 and time.monotonic() >= next_due
                if not configured:
                    self._triggered = False
                    self._set_status(False, "idle", "not set up")
                elif self._triggered or due:
                    self._triggered = False
                    self._run_once()
                    next_due = time.monotonic() + max(1, interval) * 60
                elif (self.app.config.get("SYNC_STATUS") or {}).get("state") is None:
                    self._set_status(True, "idle", "manual only" if interval == 0 else "waiting")
                if interval > 0 and next_due - time.monotonic() > interval * 60:
                    next_due = time.monotonic() + interval * 60  # interval was shortened
            except Exception:  # never let the thread die
                log.exception("Sync worker cycle failed")
            wait = self.poll_s
            if configured and interval > 0:
                wait = min(wait, max(0.5, next_due - time.monotonic()))
            self._wake.wait(wait)
            self._wake.clear()

    def _run_once(self) -> None:
        app = self.app
        self._set_status(True, "running", "syncing")
        client = SyncClient.from_settings(app.config["DB"], app.config["FILESTORE"], self.transport)
        if client is None:
            self._set_status(False, "idle", "not set up")
            return
        try:
            stats = client.run()
        except SyncBusy:
            self._set_status(True, "running", "syncing")
            return
        except SyncError as exc:
            log.warning("Sync failed: %s", exc)
            now = utcnow()
            self._set_status(True, "error", f"failed {now:%H:%M} UTC", now, str(exc))
            return
        now = utcnow()
        log.info(summary_text(stats))
        label = f"OK {now:%H:%M} UTC"
        if stats.get("problems"):
            label += f" ({len(stats['problems'])} problems)"
        self._set_status(True, "ok", label, now, "; ".join(stats["problems"]) or None)
