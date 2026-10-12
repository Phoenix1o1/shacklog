"""Derived fields filled in when a log entry is saved.

Keeping this in one place means the web form, ADIF import and any future API produce the
same data: band from frequency, ADIF mode/submode, my-station snapshot, the other
station's position from its locator, distance and bearing.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from shacklog.models import Qso, Repeater, Station, SwlEntry
from shacklog.services import bands, geo


def _station_position(station: Station | None) -> tuple[float, float] | None:
    if station is None:
        return None
    return geo.position_from(station.locator, station.latitude, station.longitude)


def snapshot_station(entry, station: Station | None, overwrite: bool = False) -> None:
    """Copy where-I-was data from the station profile onto a log entry (frozen for history).

    ``overwrite=False`` only fills empty fields, so an imported QSO keeps the
    STATION_CALLSIGN/MY_GRIDSQUARE it came with. Changing the station on an existing entry
    passes ``overwrite=True``.
    """
    if station is None:
        return
    if isinstance(entry, Qso):
        if overwrite or not entry.my_call:
            entry.my_call = station.callsign or entry.my_call
        if overwrite or not entry.my_locator:
            entry.my_locator = station.locator or entry.my_locator
    pos = _station_position(station)
    if pos and (overwrite or entry.my_latitude is None or entry.my_longitude is None):
        entry.my_latitude, entry.my_longitude = pos


def enrich_qso(session: Session, qso: Qso, is_new: bool, station_changed: bool = False) -> None:
    """Fill derived fields of a QSO. Never overwrites something the user typed."""
    station = session.get(Station, qso.station_id) if qso.station_id else None
    repeater = session.get(Repeater, qso.repeater_id) if qso.repeater_id else None

    # Frequencies: on a repeater I transmit on its input and listen on its output.
    if repeater is not None:
        if qso.freq_mhz is None:
            qso.freq_mhz = repeater.input_mhz or repeater.output_mhz
        if qso.freq_rx_mhz is None and repeater.output_mhz and repeater.input_mhz:
            qso.freq_rx_mhz = repeater.output_mhz
        if not qso.prop_mode:
            qso.prop_mode = "ECH" if repeater.kind == "ECHOLINK" else "RPT"

    qso.band = bands.band_for_freq(qso.freq_mhz) or qso.band
    qso.mode, qso.submode = bands.normalize_mode(qso.mode, qso.submode)
    qso.call = (qso.call or "").replace(" ", "").upper()
    if qso.locator:
        qso.locator = geo.normalize_locator(qso.locator) or qso.locator

    # Defaults from the station profile, for new entries only.
    if is_new and station is not None:
        qso.radio_id = qso.radio_id or station.radio_id
        qso.antenna_id = qso.antenna_id or station.antenna_id
        if qso.tx_power_w is None:
            qso.tx_power_w = station.power_w
        if not qso.operator:
            qso.operator = station.operator
    snapshot_station(qso, station, overwrite=station_changed)
    # An imported QSO may carry MY_GRIDSQUARE but no coordinates.
    if (qso.my_latitude is None or qso.my_longitude is None) and qso.my_locator:
        pos = geo.position_from(qso.my_locator, None, None)
        if pos:
            qso.my_latitude, qso.my_longitude = pos

    # The other station's position: explicit coordinates win, else its locator's centre.
    if (qso.latitude is None or qso.longitude is None) and qso.locator:
        pos = geo.position_from(qso.locator, None, None)
        if pos:
            qso.latitude, qso.longitude = pos

    _distance(qso)


def enrich_swl(session: Session, entry: SwlEntry, is_new: bool, station_changed: bool = False) -> None:
    """Same idea for listening entries; also guesses the category from the frequency."""
    station = session.get(Station, entry.station_id) if entry.station_id else None
    repeater = session.get(Repeater, entry.repeater_id) if entry.repeater_id else None
    if repeater is not None:
        if entry.freq_mhz is None:
            entry.freq_mhz = repeater.output_mhz
        if entry.latitude is None and repeater.has_position:
            entry.latitude, entry.longitude = repeater.latitude, repeater.longitude
        if not entry.heard:
            entry.heard = repeater.callsign or repeater.name
    entry.band = bands.band_for_freq(entry.freq_mhz) or entry.band
    if is_new and (not entry.category or entry.category == "amateur"):
        hint = bands.service_hint(entry.freq_mhz)
        if repeater is not None:
            entry.category = "repeater"
        elif hint:
            entry.category = hint[0]
    if entry.locator:
        entry.locator = geo.normalize_locator(entry.locator) or entry.locator
    if (entry.latitude is None or entry.longitude is None) and entry.locator:
        pos = geo.position_from(entry.locator, None, None)
        if pos:
            entry.latitude, entry.longitude = pos
    if is_new and station is not None:
        entry.receiver_id = entry.receiver_id or station.radio_id
        entry.antenna_id = entry.antenna_id or station.antenna_id
    snapshot_station(entry, station, overwrite=station_changed)
    _distance(entry)


def _distance(entry) -> None:
    """Distance and bearing from my position to theirs, when both are known."""
    if None in (entry.my_latitude, entry.my_longitude, entry.latitude, entry.longitude):
        entry.distance_km = None
        entry.bearing_deg = None
        return
    entry.distance_km = round(geo.haversine_km(entry.my_latitude, entry.my_longitude,
                                               entry.latitude, entry.longitude), 2)
    entry.bearing_deg = round(geo.bearing_deg(entry.my_latitude, entry.my_longitude,
                                              entry.latitude, entry.longitude), 1)
