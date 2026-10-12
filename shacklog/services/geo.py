"""Geographic helpers: Maidenhead locators, great-circle distance/bearing, position parsing.

Pure functions, no dependencies. ``db.py`` registers :func:`haversine_km` as the SQL
function ``geo_dist_km(lat1, lon1, lat2, lon2)`` so filters can sort and limit by distance
inside SQLite.
"""

from __future__ import annotations

import math
import re

EARTH_RADIUS_KM = 6371.0088  # mean Earth radius (IUGG)

_LOCATOR_RE = re.compile(r"^[A-R]{2}(\d{2}([A-X]{2}(\d{2}([A-X]{2})?)?)?)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Maidenhead locators
# ---------------------------------------------------------------------------

def is_valid_locator(text: str | None) -> bool:
    """True for 2, 4, 6, 8 or 10 character Maidenhead locators (case-insensitive)."""
    return bool(text) and bool(_LOCATOR_RE.match(text.strip()))


def normalize_locator(text: str | None) -> str | None:
    """``"jn45oL"`` → ``"JN45ol"`` (field upper case, subsquare lower case), or None."""
    if not text or not is_valid_locator(text):
        return None
    text = text.strip()
    out = text[:4].upper()
    if len(text) > 4:
        out += text[4:6].lower()
    if len(text) > 6:
        out += text[6:8]
    if len(text) > 8:
        out += text[8:10].lower()
    return out


def locator_to_latlon(locator: str, center: bool = True) -> tuple[float, float]:
    """Return (lat, lon) of a locator — its centre by default, else its SW corner.

    Raises ``ValueError`` for an invalid locator.
    """
    if not is_valid_locator(locator):
        raise ValueError(f"Invalid Maidenhead locator: {locator!r}")
    loc = locator.strip().upper()
    lon, lat = -180.0, -90.0
    # Each pair refines the square: fields 20°x10°, squares 2°x1°, subsquares 5'x2.5',
    # extended squares /10, extended subsquares /24.
    lon_size, lat_size = 20.0, 10.0
    lon += (ord(loc[0]) - 65) * lon_size
    lat += (ord(loc[1]) - 65) * lat_size
    pairs = [loc[i:i + 2] for i in range(2, len(loc), 2)]
    for index, pair in enumerate(pairs):
        if index % 2 == 0:  # digits: divide by 10
            lon_size, lat_size = lon_size / 10, lat_size / 10
            lon += int(pair[0]) * lon_size
            lat += int(pair[1]) * lat_size
        else:  # letters: divide by 24
            lon_size, lat_size = lon_size / 24, lat_size / 24
            lon += (ord(pair[0]) - 65) * lon_size
            lat += (ord(pair[1]) - 65) * lat_size
    if center:
        lon += lon_size / 2
        lat += lat_size / 2
    return round(lat, 6), round(lon, 6)


def latlon_to_locator(lat: float, lon: float, precision: int = 6) -> str:
    """Return the Maidenhead locator (2/4/6/8/10 characters) containing (lat, lon)."""
    if precision not in (2, 4, 6, 8, 10):
        raise ValueError("precision must be 2, 4, 6, 8 or 10")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("latitude/longitude out of range")
    # Clamp the exact north/east edges into the last square.
    x = min(lon + 180.0, 359.9999999)
    y = min(lat + 90.0, 179.9999999)
    lon_size, lat_size = 20.0, 10.0
    out = chr(65 + int(x // lon_size)) + chr(65 + int(y // lat_size))
    x %= lon_size
    y %= lat_size
    step = 0
    while len(out) < precision:
        if step % 2 == 0:
            lon_size, lat_size = lon_size / 10, lat_size / 10
            out += str(int(x // lon_size)) + str(int(y // lat_size))
        else:
            lon_size, lat_size = lon_size / 24, lat_size / 24
            out += chr(65 + int(x // lon_size)) + chr(65 + int(y // lat_size))
        x %= lon_size
        y %= lat_size
        step += 1
    return normalize_locator(out) or out


# ---------------------------------------------------------------------------
# Distance and bearing
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two points in decimal degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, 0–360° (0 = north)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    x = math.sin(dlmb) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def compass_point(bearing: float | None) -> str:
    """16-point compass label: 0 → "N", 225 → "SW"."""
    if bearing is None:
        return ""
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[int((bearing % 360) / 22.5 + 0.5) % 16]


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) enclosing a circle — a cheap SQL pre-filter.

    Near the poles or across the antimeridian the longitude range widens to the full
    −180…180 so that no point inside the circle is excluded.
    """
    dlat = math.degrees(radius_km / EARTH_RADIUS_KM)
    min_lat, max_lat = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    cos_lat = math.cos(math.radians(lat))
    if cos_lat < 1e-6 or max_lat >= 90.0 or min_lat <= -90.0:
        return min_lat, -180.0, max_lat, 180.0
    dlon = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    min_lon, max_lon = lon - dlon, lon + dlon
    if min_lon < -180.0 or max_lon > 180.0:
        return min_lat, -180.0, max_lat, 180.0
    return min_lat, min_lon, max_lat, max_lon


# ---------------------------------------------------------------------------
# Parsing user input
# ---------------------------------------------------------------------------

_DMS_RE = re.compile(
    r"""(?P<deg>\d+(?:\.\d+)?)\s*[°d\s]\s*
        (?:(?P<min>\d+(?:\.\d+)?)\s*['′m\s]\s*)?
        (?:(?P<sec>\d+(?:\.\d+)?)\s*(?:["″]|''|s)?\s*)?
        (?P<hem>[NSEW])""",
    re.IGNORECASE | re.VERBOSE,
)


def _dms_to_decimal(match: re.Match) -> tuple[float, str]:
    value = float(match["deg"]) + float(match["min"] or 0) / 60 + float(match["sec"] or 0) / 3600
    hem = match["hem"].upper()
    if hem in "SW":
        value = -value
    return value, hem


def parse_position(text: str | None) -> tuple[float, float] | None:
    """Turn what a person types into (lat, lon), or None if it can't be understood.

    Accepts a Maidenhead locator (``JN45oL``), decimal degrees (``45.464, 9.19`` or
    ``45.464 9.19``) and degrees-minutes-seconds (``45°27'51"N 9°11'25"E``).
    """
    if not text:
        return None
    text = text.strip()
    if is_valid_locator(text):
        return locator_to_latlon(text)
    # Decimal "lat, lon" or "lat lon" (optionally with ; separator).
    parts = re.split(r"[,;\s]+", text)
    if len(parts) == 2:
        try:
            lat, lon = float(parts[0]), float(parts[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except ValueError:
            pass
    # Degrees/minutes/seconds with hemisphere letters.
    found = [_dms_to_decimal(m) for m in _DMS_RE.finditer(text)]
    lat = next((v for v, h in found if h in "NS"), None)
    lon = next((v for v, h in found if h in "EW"), None)
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def position_from(locator: str | None, lat: float | None, lon: float | None) -> tuple[float, float] | None:
    """Best known position: explicit coordinates first, else the locator's centre."""
    if lat is not None and lon is not None:
        return lat, lon
    if locator and is_valid_locator(locator):
        return locator_to_latlon(locator)
    return None
