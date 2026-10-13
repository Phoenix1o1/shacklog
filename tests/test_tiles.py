"""Map tiles: offline MBTiles, the cache, online fetching and the offline fallback."""

import io
import os
import sqlite3
import time
import urllib.error

import pytest

from shacklog.services import tiles

PNG = b"\x89PNG\r\n\x1a\n" + b"tile" * 10
JPEG = b"\xff\xd8\xff" + b"tile" * 10


class FakeNet:
    """urlopen stand-in: records requests, answers PNG, or fails like no network."""

    def __init__(self, fail=None):
        self.requests, self.fail = [], fail

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail:
            raise self.fail
        return io.BytesIO(PNG)


@pytest.fixture(autouse=True)
def online_again(monkeypatch):
    monkeypatch.setattr(tiles, "_offline_until", 0.0)


def make_mbtiles(path, z=5, x=16, y=10, data=JPEG):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute("CREATE TABLE tiles (zoom_level INT, tile_column INT, tile_row INT, tile_data BLOB)")
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", [
            ("name", "Alps"), ("format", "jpg"), ("minzoom", "5"), ("maxzoom", "12"),
            ("attribution", "<b>Map</b> data")])
        conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, 2 ** z - 1 - y, data))
    conn.close()


def test_fetches_once_then_serves_the_cache(tmp_path):
    net = FakeNet()
    source = tiles.TileSource(tmp_path, opener=net)
    first = source.get(5, 16, 10)
    assert first.source == "online" and first.content_type == "image/png"
    assert source.get(5, 16, 10).source == "cache" and len(net.requests) == 1
    request = net.requests[0]
    assert request.full_url == "https://tile.openstreetmap.org/5/16/10.png"
    assert request.get_header("User-agent").startswith("ShackLog/")  # OSM policy


def test_offline_uses_a_stale_tile_and_stops_trying(tmp_path):
    tiles.TileSource(tmp_path, opener=FakeNet()).get(5, 16, 10)
    old = time.time() - (tiles.REFRESH_DAYS + 1) * 86400
    path = tiles.TileSource(tmp_path).cache_path(5, 16, 10)
    os.utime(path, (old, old))
    net = FakeNet(fail=urllib.error.URLError("no network"))
    source = tiles.TileSource(tmp_path, opener=net)
    assert source.get(5, 16, 10).source == "stale cache"
    assert source.get(6, 1, 1) is None
    assert len(net.requests) == 1  # after one failure the network is left alone a while


def test_online_fetching_can_be_off(tmp_path):
    net = FakeNet()
    assert tiles.TileSource(tmp_path, online=False, opener=net).get(5, 16, 10) is None
    assert not net.requests


def test_mbtiles_first_and_flipped_rows(tmp_path):
    tiles.maps_dir(tmp_path).mkdir()
    make_mbtiles(tiles.maps_dir(tmp_path) / "alps.mbtiles")
    net = FakeNet()
    found = tiles.TileSource(tmp_path, opener=net).get(5, 16, 10)
    assert found.source == "mbtiles" and found.content_type == "image/jpeg" and not net.requests
    info = tiles.mbtiles_info(tiles.maps_dir(tmp_path) / "alps.mbtiles")
    assert (info["name"], info["minzoom"], info["maxzoom"], info["ok"]) == ("Alps", 5, 12, True)


def test_custom_server_and_bad_requests(tmp_path):
    net = FakeNet()
    source = tiles.TileSource(tmp_path, url="https://{s}.tile.example.org/{z}/{x}/{y}.png", opener=net)
    source.get(3, 1, 2)
    assert net.requests[0].full_url == "https://a.tile.example.org/3/1/2.png"
    assert tiles.TileSource(tmp_path, url="file:///etc/{z}{x}{y}").url == tiles.DEFAULT_URL
    assert source.get(3, 8, 0) is None and source.get(25, 0, 0) is None


def test_cache_is_pruned_oldest_first(tmp_path):
    source = tiles.TileSource(tmp_path, opener=FakeNet())
    for x in range(4):
        source.get(4, x, 0)
        path = source.cache_path(4, x, 0)
        os.utime(path, (1000 + x, 1000 + x))
    assert tiles.cache_usage(tmp_path)[0] == 4
    size = source.cache_path(4, 0, 0).stat().st_size
    tiles.prune_cache(tmp_path, 0)  # 0 MB: everything goes
    assert tiles.cache_usage(tmp_path) == (0, 0) and size > 0


def test_settings_page_map_panel(client, app, tmp_path):
    from tests.conftest import csrf

    data_dir = app.config["PATHS"].data_dir
    page = client.get("/settings/data").text
    assert 'id="map"' in page and "0 tiles" in page
    # bad server address refused, good one saved
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "tiles_settings",
                                        "map_tile_url": "ftp://x/{z}/{x}/{y}", "map_tile_cache_mb": "50"})
    assert 'value="ftp' not in client.get("/settings/data").text
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "tiles_settings",
                                        "map_tile_url": "https://t.example/{z}/{x}/{y}.png",
                                        "map_tile_cache_mb": "50"})
    page = client.get("/settings/data").text
    assert "https://t.example/{z}/{x}/{y}.png" in page and 'value="50"' in page
    assert 'name="map_tiles_online" value="1" checked' not in page  # box was unticked
    # an offline map: a non-MBTiles file is refused, a real one installed and removed
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "tiles_upload",
                                        "file": (io.BytesIO(b"junk"), "x.mbtiles")},
                content_type="multipart/form-data")
    assert not tiles.mbtiles_files(data_dir)
    make_mbtiles(tmp_path / "alps.mbtiles")
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "tiles_upload",
                                        "file": ((tmp_path / "alps.mbtiles").open("rb"), "alps.mbtiles")},
                content_type="multipart/form-data")
    assert [p.name for p in tiles.mbtiles_files(data_dir)] == ["alps.mbtiles"]
    assert "zoom 5–12" in client.get("/settings/data").text
    assert client.get("/map/tiles/5/16/10").headers["X-Tile-Source"] == "mbtiles"
    from tests.test_map_web import _config

    credit = _config(client.get("/map/").text)["tiles"]["attribution"]
    assert "&lt;b&gt;Map&lt;/b&gt; data" in credit  # the file's credit, as text not HTML
    outside = data_dir.parent / "alps.mbtiles"
    outside.write_bytes(b"not mine")
    client.post("/settings/data", data={"_csrf": csrf(client), "action": "tiles_remove",
                                        "file": "../alps.mbtiles"})
    # only the bare name counts: the map in the folder goes, the file outside stays
    assert not tiles.mbtiles_files(data_dir) and outside.read_bytes() == b"not mine"
