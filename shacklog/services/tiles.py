"""Map tiles for the map page, from this instance: offline files, a cache, then online.

The browser asks ShackLog for ``/map/tiles/{z}/{x}/{y}`` instead of asking OpenStreetMap
directly. :class:`TileSource` answers from, in order:

1. **MBTiles files** in ``<data>/maps/`` (``*.mbtiles``, raster PNG/JPEG/WebP). These are
   whole areas made for offline use (e.g. with MOBAC, or downloaded from a provider that
   allows it): the map then works with no internet at all, at the zoom levels they hold.
2. **The tile cache** in ``<data>/maps/cache/z/x/y``: every tile viewed once is kept, so
   the places you look at work offline later. Tiles older than :data:`REFRESH_DAYS` are
   fetched again when online; when offline the old one is used.
3. **The tile server** (OpenStreetMap unless set otherwise), when online fetching is on.

OpenStreetMap's tile policy asks for an identifying User-Agent and for caching, and
forbids bulk downloading: tiles are fetched only as they are viewed, never in advance.

The hub works the same way, so a phone browsing it over the VPN gets the hub's cache.
Pure module (no Flask): the map view creates one :class:`TileSource` per request from
the settings.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from shacklog import APP_NAME, __version__

log = logging.getLogger(__name__)

DEFAULT_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = ('&copy; <a href="https://www.openstreetmap.org/copyright">'
                   'OpenStreetMap</a> contributors')
USER_AGENT = f"{APP_NAME}/{__version__} (personal logbook; +https://github.com/Phoenix1o1/shacklog)"
MAX_ZOOM = 19
REFRESH_DAYS = 30
FETCH_TIMEOUT_S = 8
OFFLINE_RETRY_S = 60  # after a failed fetch, don't try the network again for a minute
MAX_TILE_BYTES = 2_000_000

_offline_until = 0.0  # shared by all requests: one failure spares the others the timeout
_prune_lock = threading.Lock()


@dataclass
class Tile:
    data: bytes
    content_type: str
    source: str  # "mbtiles", "cache", "online", "stale cache"


def valid(z: int, x: int, y: int) -> bool:
    return 0 <= z <= MAX_ZOOM and 0 <= x < 2 ** z and 0 <= y < 2 ** z


def content_type(data: bytes) -> str:
    """MIME type from the first bytes (tiles carry no file name)."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def safe_url(url: str | None) -> bool:
    """A tile server template must be http(s) and have the three placeholders."""
    return bool(url) and bool(re.match(r"^https?://", url)) and all(
        p in url for p in ("{z}", "{x}", "{y}"))


def maps_dir(data_dir: Path) -> Path:
    from shacklog.config import Paths

    return Paths(Path(data_dir)).maps


def cache_dir(data_dir: Path) -> Path:
    return maps_dir(data_dir) / "cache"


# ---------------------------------------------------------------------------
# MBTiles
# ---------------------------------------------------------------------------

def mbtiles_files(data_dir: Path) -> list[Path]:
    folder = maps_dir(data_dir)
    return sorted(folder.glob("*.mbtiles")) if folder.is_dir() else []


def mbtiles_info(path: Path) -> dict:
    """Name, format, zoom range and attribution from an MBTiles file's metadata."""
    info = {"file": path.name, "size": path.stat().st_size, "name": path.stem,
            "format": "", "minzoom": None, "maxzoom": None, "attribution": "", "ok": True}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            for name, value in conn.execute("SELECT name, value FROM metadata"):
                if name in ("name", "format", "attribution"):
                    info[name] = value or ""
                elif name in ("minzoom", "maxzoom") and str(value).isdigit():
                    info[name] = int(value)
            if info["minzoom"] is None:
                row = conn.execute("SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles").fetchone()
                info["minzoom"], info["maxzoom"] = row
    except sqlite3.Error as exc:
        info.update(ok=False, error=str(exc))
    if info["format"] in ("pbf", "mvt"):
        info.update(ok=False, error="vector tiles (pbf) can't be shown; use a raster file")
    return info


def _from_mbtiles(path: Path, z: int, x: int, y: int) -> bytes | None:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, (2 ** z - 1) - y)).fetchone()  # MBTiles counts rows from the south
    except sqlite3.Error:
        return None
    return bytes(row[0]) if row and row[0] else None


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------

class TileSource:
    """Answers tile requests for one data folder (see the module docstring)."""

    def __init__(self, data_dir: Path, url: str | None = None, online: bool = True,
                 cache_limit_mb: int = 500, opener=None):
        self.data_dir = Path(data_dir)
        self.url = url if safe_url(url) else DEFAULT_URL
        self.online = online
        self.cache_limit = max(0, cache_limit_mb) * 1024 * 1024
        self._open = opener or urllib.request.urlopen

    def cache_path(self, z: int, x: int, y: int) -> Path:
        return cache_dir(self.data_dir) / str(z) / str(x) / str(y)

    def get(self, z: int, x: int, y: int) -> Tile | None:
        if not valid(z, x, y):
            return None
        for path in mbtiles_files(self.data_dir):
            data = _from_mbtiles(path, z, x, y)
            if data:
                return Tile(data, content_type(data), "mbtiles")
        cached = self.cache_path(z, x, y)
        stale = None
        if cached.is_file():
            data = cached.read_bytes()
            if time.time() - cached.stat().st_mtime < REFRESH_DAYS * 86400:
                return Tile(data, content_type(data), "cache")
            stale = data
        fetched = self._fetch(z, x, y)
        if fetched is not None:
            self._store(cached, fetched)
            return Tile(fetched, content_type(fetched), "online")
        if stale is not None:
            return Tile(stale, content_type(stale), "stale cache")
        return None

    def _fetch(self, z: int, x: int, y: int) -> bytes | None:
        global _offline_until
        if not self.online or time.time() < _offline_until:
            return None
        url = self.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        url = url.replace("{s}", "abc"[(x + y) % 3])
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._open(request, timeout=FETCH_TIMEOUT_S) as resp:
                data = resp.read(MAX_TILE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            log.debug("Tile %s: HTTP %s", url, exc.code)  # missing tile: not "offline"
            return None
        except Exception as exc:  # no network: stop trying for a while
            log.info("Map tiles unavailable (%s); using the cache only for a minute.", exc)
            _offline_until = time.time() + OFFLINE_RETRY_S
            return None
        if not data or len(data) > MAX_TILE_BYTES or content_type(data).endswith("octet-stream"):
            return None
        return data

    def _store(self, path: Path, data: bytes) -> None:
        if self.cache_limit == 0:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{threading.get_ident()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            log.warning("Couldn't cache map tile %s", path)


# ---------------------------------------------------------------------------
# Cache housekeeping
# ---------------------------------------------------------------------------

def cache_usage(data_dir: Path) -> tuple[int, int]:
    """(number of tiles, bytes) in the cache."""
    count = size = 0
    folder = cache_dir(data_dir)
    if folder.is_dir():
        for path in folder.rglob("*"):
            if path.is_file():
                count += 1
                size += path.stat().st_size
    return count, size


def prune_cache(data_dir: Path, limit_mb: int) -> int:
    """Delete the least recently fetched tiles until the cache fits ``limit_mb``.
    Returns how many were removed."""
    if not _prune_lock.acquire(blocking=False):
        return 0
    try:
        folder = cache_dir(data_dir)
        if not folder.is_dir():
            return 0
        files = [(p.stat().st_mtime, p.stat().st_size, p) for p in folder.rglob("*") if p.is_file()]
        total, limit = sum(f[1] for f in files), max(0, limit_mb) * 1024 * 1024
        removed = 0
        for _mtime, size, path in sorted(files):
            if total <= limit:
                break
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed
    finally:
        _prune_lock.release()


def clear_cache(data_dir: Path) -> int:
    return prune_cache(data_dir, 0)
