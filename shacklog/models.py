"""ShackLog data model (SQLAlchemy ORM). Source of truth for every field name.

Two kinds of tables live here:

* **Synced tables** inherit :class:`SyncMixin`. They hold Andrea's data (logs, repeaters,
  equipment, files…), have UUID primary keys, and travel between instances via sync and
  bundles. They are listed in :data:`SYNC_ORDER`; adding a model there is all it takes for
  sync, bundles, trash and backups to handle it.
* **Local tables** (settings, sync peers, conflicts…) describe *this* instance only and
  never leave it.

Conventions
-----------
* Timestamps are naive ``datetime`` values in **UTC**.
* Frequencies are in **MHz** (float), powers in **W**, distances in **km**.
* References between synced records are UUID strings. ``ForeignKey`` is declared so the
  ORM can join, but SQLite does not enforce it: during a sync a QSO may arrive before the
  station it points to, and that must not be an error. Code must tolerate a reference to a
  missing record (show "unknown").
* ``tags`` are a comma-separated string; see :func:`split_tags`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def new_uuid() -> str:
    """Random UUID as a string; used as primary key of every synced record."""
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (the storage convention, see docstring)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def split_tags(tags: str | None) -> list[str]:
    """``"hike, sota ,,portable"`` → ``["hike", "sota", "portable"]``."""
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


class Base(DeclarativeBase):
    """Declarative base shared by all tables."""


# ---------------------------------------------------------------------------
# Sync mixin
# ---------------------------------------------------------------------------

#: Columns that describe this instance's bookkeeping and are never sent to peers.
LOCAL_COLUMNS = frozenset({"change_seq", "received_from"})


class SyncMixin:
    """Columns and behaviour shared by every synced table (PLAN.md §5).

    ``updated_at``/``modified_by_instance``/``change_seq`` are stamped automatically by a
    flush hook in ``db.py`` — application code never sets them by hand.
    """

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    origin_instance: Mapped[str | None] = mapped_column(String(36))
    modified_by_instance: Mapped[str | None] = mapped_column(String(36))
    # --- local only (LOCAL_COLUMNS) ---
    change_seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    received_from: Mapped[str | None] = mapped_column(String(36))

    #: Short machine name used in URLs, links and search ("qso", "repeater"…).
    __entity__: ClassVar[str] = ""
    #: Human label for the UI ("QSO", "Repeater"…).
    __label__: ClassVar[str] = ""

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        """Mark as deleted. The row stays (and syncs) so the deletion reaches peers."""
        self.deleted_at = utcnow()

    def restore(self) -> None:
        self.deleted_at = None

    def display_name(self) -> str:
        """One-line description used in lists, links and search results."""
        return self.id

    def search_document(self) -> tuple[str, str] | None:
        """``(title, body)`` for the full-text index, or ``None`` to stay out of it."""
        return None


# ---------------------------------------------------------------------------
# Choice lists (value, label). Forms and filters read these.
# ---------------------------------------------------------------------------

STATION_KINDS = [
    ("home", "Home station"),
    ("fixed", "Fixed location"),
    ("portable", "Portable"),
    ("mobile", "Mobile / car"),
]

EQUIPMENT_KINDS = [
    ("radio", "Radio / transceiver"),
    ("handheld", "Handheld"),
    ("receiver", "Receiver / SDR"),
    ("antenna", "Antenna"),
    ("feedline", "Coax / feedline"),
    ("amplifier", "Amplifier"),
    ("meter", "Meter / test gear"),
    ("power", "Power supply / battery"),
    ("computer", "Computer / interface"),
    ("accessory", "Accessory"),
    ("other", "Other"),
]

EQUIPMENT_STATUS = [
    ("active", "In use"),
    ("spare", "Spare"),
    ("wishlist", "Wishlist"),
    ("ordered", "Ordered"),
    ("broken", "Broken / repair"),
    ("retired", "Retired / sold"),
]

REPEATER_KINDS = [
    ("FM", "Analog FM"),
    ("DMR", "DMR"),
    ("C4FM", "C4FM / Fusion"),
    ("DSTAR", "D-STAR"),
    ("M17", "M17"),
    ("ECHOLINK", "EchoLink node / gateway"),
    ("ALLSTAR", "AllStar node"),
    ("APRS", "APRS digipeater / iGate"),
    ("BEACON", "Beacon"),
    ("OTHER", "Other"),
]

REPEATER_STATUS = [("active", "Active"), ("inactive", "Off air"), ("unknown", "Unknown")]

#: ADIF PROP_MODE values that matter at VHF/UHF, plus the most common HF ones.
PROP_MODES = [
    ("", "Direct / unspecified"),
    ("RPT", "Repeater"),
    ("SAT", "Satellite"),
    ("ECH", "EchoLink"),
    ("IRL", "Internet-linked (IRLP/AllStar/DMR network)"),
    ("INTERNET", "Internet-assisted"),
    ("TR", "Tropospheric ducting"),
    ("ES", "Sporadic E"),
    ("MS", "Meteor scatter"),
    ("EME", "Earth-Moon-Earth"),
    ("AUR", "Aurora"),
    ("F2", "F2 reflection"),
    ("TEP", "Trans-equatorial"),
    ("LOS", "Line of sight"),
]

#: ADIF QSL_SENT / QSL_RCVD style statuses.
QSL_STATUS = [
    ("", "—"),
    ("N", "No"),
    ("R", "Requested"),
    ("Q", "Queued"),
    ("Y", "Yes"),
    ("I", "Ignore / invalid"),
]

SWL_CATEGORIES = [
    ("amateur", "Amateur station"),
    ("repeater", "Repeater"),
    ("satellite", "Satellite"),
    ("airband", "Airband"),
    ("marine", "Marine"),
    ("broadcast", "Broadcast"),
    ("utility", "Utility / service"),
    ("beacon", "Beacon"),
    ("pmr", "PMR446 / CB"),
    ("weather", "Weather satellite / sonde"),
    ("data", "Data (APRS, ADS-B, AIS…)"),
    ("other", "Other"),
]

JOURNAL_KINDS = [("test", "Antenna & test"), ("diary", "Station diary")]

FILE_CATEGORIES = [
    ("note", "Note / guide"),
    ("reference", "Quick reference / cheat sheet"),
    ("manual", "Manual"),
    ("bandplan", "Band plan / regulations"),
    ("datasheet", "Datasheet / specs"),
    ("schematic", "Schematic / diagram"),
    ("chirp", "CHIRP image / CSV"),
    ("firmware", "Firmware / software"),
    ("sweep", "VNA sweep (Touchstone)"),
    ("photo", "Photo"),
    ("qsl", "QSL card"),
    ("recording", "Recording"),
    ("document", "Document"),
    ("other", "Other"),
]


def choice_label(choices: list[tuple[str, str]], value: str | None) -> str:
    """Label for a stored choice value, falling back to the raw value."""
    for key, label in choices:
        if key == value:
            return label
    return value or ""


# ---------------------------------------------------------------------------
# Synced tables
# ---------------------------------------------------------------------------

class Equipment(SyncMixin, Base):
    """Anything in the shack: radios, antennas, SDRs, meters, coax, supplies."""

    __tablename__ = "equipment"
    __entity__ = "equipment"
    __label__ = "Equipment"

    kind: Mapped[str] = mapped_column(String(20), default="radio", index=True)
    name: Mapped[str] = mapped_column(String(120))
    manufacturer: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    serial: Mapped[str | None] = mapped_column(String(80))
    firmware: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")
    acquired_on: Mapped[date | None] = mapped_column(Date)
    price: Mapped[str | None] = mapped_column(String(40))  # free text, e.g. "€48.99"
    vendor: Mapped[str | None] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(String(500))
    freq_range: Mapped[str | None] = mapped_column(String(200))  # e.g. "144-146, 430-440 MHz"
    connector: Mapped[str | None] = mapped_column(String(80))
    specs: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))

    def display_name(self) -> str:
        return self.name

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.kind, self.manufacturer, self.model, self.serial,
                                      self.firmware, self.freq_range, self.connector,
                                      self.specs, self.notes, self.tags]))
        return self.name, body


class Station(SyncMixin, Base):
    """A place (and default setup) I operate from: home, a fixed second QTH, portable…

    The ``home`` kind is the default reference point for distance filters.
    """

    __tablename__ = "stations"
    __entity__ = "station"
    __label__ = "Station"

    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20), default="home")
    callsign: Mapped[str | None] = mapped_column(String(20))
    operator: Mapped[str | None] = mapped_column(String(20))
    location_name: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    locator: Mapped[str | None] = mapped_column(String(10))
    altitude_m: Mapped[float | None] = mapped_column(Float)
    country: Mapped[str | None] = mapped_column(String(60))
    region: Mapped[str | None] = mapped_column(String(80))
    radio_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"))
    antenna_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"))
    power_w: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)

    radio: Mapped[Equipment | None] = relationship(foreign_keys=[radio_id])
    antenna: Mapped[Equipment | None] = relationship(foreign_keys=[antenna_id])

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def display_name(self) -> str:
        return self.name

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.kind, self.callsign, self.location_name,
                                      self.locator, self.country, self.region, self.notes]))
        return self.name, body


class Repeater(SyncMixin, Base):
    """A repeater, EchoLink node, digipeater or beacon. QSOs and SWL entries link here."""

    __tablename__ = "repeaters"
    __entity__ = "repeater"
    __label__ = "Repeater"

    name: Mapped[str] = mapped_column(String(120))  # e.g. "Milano R1 Campo dei Fiori"
    callsign: Mapped[str | None] = mapped_column(String(20), index=True)  # e.g. IR2UFD
    ident: Mapped[str | None] = mapped_column(String(40), index=True)  # e.g. R1, RU5
    kind: Mapped[str] = mapped_column(String(12), default="FM")
    output_mhz: Mapped[float | None] = mapped_column(Float, index=True)  # what I listen on
    input_mhz: Mapped[float | None] = mapped_column(Float)  # what I transmit on
    ctcss_up: Mapped[float | None] = mapped_column(Float)  # tone I must send (Hz)
    ctcss_down: Mapped[float | None] = mapped_column(Float)  # tone the repeater sends
    dcs_code: Mapped[str | None] = mapped_column(String(8))
    color_code: Mapped[int | None] = mapped_column(Integer)  # DMR
    talkgroups: Mapped[str | None] = mapped_column(String(300))
    network: Mapped[str | None] = mapped_column(String(80))  # BrandMeister, WIRES-X…
    echolink_node: Mapped[str | None] = mapped_column(String(12))
    location_name: Mapped[str | None] = mapped_column(String(200))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    locator: Mapped[str | None] = mapped_column(String(10))
    altitude_m: Mapped[float | None] = mapped_column(Float)
    country: Mapped[str | None] = mapped_column(String(60))
    region: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(12), default="active")
    source: Mapped[str | None] = mapped_column(String(300))  # where the data came from
    last_verified: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))

    @property
    def offset_mhz(self) -> float | None:
        """Signed shift (input − output), e.g. −0.6 on 2 m, +1.6 on Italian 70 cm."""
        if self.output_mhz is None or self.input_mhz is None:
            return None
        return round(self.input_mhz - self.output_mhz, 6)

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def display_name(self) -> str:
        bits = [self.name]
        if self.output_mhz:
            bits.append(f"{self.output_mhz:.4f}".rstrip("0").rstrip("."))
        return " · ".join(bits)

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.callsign, self.ident, self.kind, self.network,
                                      self.echolink_node, self.location_name, self.locator,
                                      self.country, self.region, self.talkgroups,
                                      self.source, self.notes, self.tags,
                                      f"{self.output_mhz}" if self.output_mhz else None]))
        return self.name, body


class Contact(SyncMixin, Base):
    """My own notes about another operator (one per callsign).

    The callsign page combines this card with every QSO and SWL entry for that call.
    """

    __tablename__ = "contacts"
    __entity__ = "contact"
    __label__ = "Contact"

    callsign: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    qth: Mapped[str | None] = mapped_column(String(200))
    locator: Mapped[str | None] = mapped_column(String(10))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    country: Mapped[str | None] = mapped_column(String(60))
    email: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))

    def display_name(self) -> str:
        return f"{self.callsign} {self.name or ''}".strip()

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.name, self.qth, self.locator, self.country,
                                      self.notes, self.tags]))
        return self.callsign, body


class StoredFile(SyncMixin, Base):
    """Library entry. The bytes live in the blob store under ``sha256`` (filestore.py)."""

    __tablename__ = "files"
    __entity__ = "file"
    __label__ = "File"

    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(20), default="document", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))
    # Extracted text (PDF, TXT, MD, CSV…) for full-text search. Deferred: not loaded in lists.
    text_content: Mapped[str | None] = mapped_column(Text, deferred=True)

    def display_name(self) -> str:
        return self.title or self.filename

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.filename, self.category, self.description,
                                      self.tags, self.text_content]))
        return self.display_name(), body


class RadioConfig(SyncMixin, Base):
    """One saved programming of a radio (a CHIRP snapshot): image, CSV and notes."""

    __tablename__ = "radio_configs"
    __entity__ = "radio_config"
    __label__ = "Radio configuration"

    radio_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    firmware: Mapped[str | None] = mapped_column(String(120))  # e.g. "F4HWN v6.1.0"
    edition: Mapped[str | None] = mapped_column(String(60))  # bank/edition: Fusion, Labs…
    config_date: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False)
    image_file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"))  # CHIRP .img
    csv_file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"))  # CHIRP .csv
    settings: Mapped[str | None] = mapped_column(Text)  # settings applied, free text
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))

    radio: Mapped[Equipment | None] = relationship(foreign_keys=[radio_id])
    image_file: Mapped[StoredFile | None] = relationship(foreign_keys=[image_file_id])
    csv_file: Mapped[StoredFile | None] = relationship(foreign_keys=[csv_file_id])

    def display_name(self) -> str:
        return self.title

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.firmware, self.edition, self.settings,
                                      self.notes, self.tags]))
        return self.title, body


class RadioChannel(SyncMixin, Base):
    """A memory channel of a :class:`RadioConfig`, parsed from its CHIRP CSV."""

    __tablename__ = "radio_channels"
    __entity__ = "radio_channel"
    __label__ = "Channel"

    config_id: Mapped[str] = mapped_column(ForeignKey("radio_configs.id"), index=True)
    location: Mapped[int] = mapped_column(Integer)  # CHIRP "Location" (memory number)
    name: Mapped[str | None] = mapped_column(String(40))
    freq_mhz: Mapped[float | None] = mapped_column(Float, index=True)
    duplex: Mapped[str | None] = mapped_column(String(8))  # "", "+", "-", "split", "off"
    offset_mhz: Mapped[float | None] = mapped_column(Float)
    tone_mode: Mapped[str | None] = mapped_column(String(12))  # "", Tone, TSQL, DTCS, Cross
    rtone: Mapped[float | None] = mapped_column(Float)
    ctone: Mapped[float | None] = mapped_column(Float)
    dtcs: Mapped[str | None] = mapped_column(String(8))
    dtcs_polarity: Mapped[str | None] = mapped_column(String(4))
    rx_dtcs: Mapped[str | None] = mapped_column(String(8))
    cross_mode: Mapped[str | None] = mapped_column(String(20))
    mode: Mapped[str | None] = mapped_column(String(10))
    tstep: Mapped[str | None] = mapped_column(String(10))
    skip: Mapped[str | None] = mapped_column(String(4))
    power: Mapped[str | None] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(String(300))
    raw: Mapped[str | None] = mapped_column(Text)  # JSON of every original CSV column
    repeater_id: Mapped[str | None] = mapped_column(ForeignKey("repeaters.id"))

    config: Mapped[RadioConfig] = relationship(foreign_keys=[config_id])
    repeater: Mapped[Repeater | None] = relationship(foreign_keys=[repeater_id])

    def display_name(self) -> str:
        return f"#{self.location} {self.name or ''} {self.freq_mhz or ''}".strip()


class Qso(SyncMixin, Base):
    """A two-way contact, shaped after ADIF so import/export is lossless."""

    __tablename__ = "qsos"
    __entity__ = "qso"
    __label__ = "QSO"

    # --- when / who ---
    time_on: Mapped[datetime] = mapped_column(DateTime, index=True)
    time_off: Mapped[datetime | None] = mapped_column(DateTime)
    call: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    qth: Mapped[str | None] = mapped_column(String(200))
    locator: Mapped[str | None] = mapped_column(String(10))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    country: Mapped[str | None] = mapped_column(String(60))
    region: Mapped[str | None] = mapped_column(String(80))
    operator: Mapped[str | None] = mapped_column(String(20))  # who operated on my side
    # --- radio ---
    freq_mhz: Mapped[float | None] = mapped_column(Float)
    freq_rx_mhz: Mapped[float | None] = mapped_column(Float)
    band: Mapped[str | None] = mapped_column(String(10), index=True)
    mode: Mapped[str | None] = mapped_column(String(20), index=True)
    submode: Mapped[str | None] = mapped_column(String(20))
    rst_sent: Mapped[str | None] = mapped_column(String(10))
    rst_rcvd: Mapped[str | None] = mapped_column(String(10))
    tx_power_w: Mapped[float | None] = mapped_column(Float)
    # --- my side (links + snapshot of the station at QSO time) ---
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"), index=True)
    radio_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    antenna_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    my_call: Mapped[str | None] = mapped_column(String(20))
    my_locator: Mapped[str | None] = mapped_column(String(10))
    my_latitude: Mapped[float | None] = mapped_column(Float)
    my_longitude: Mapped[float | None] = mapped_column(Float)
    # --- path ---
    repeater_id: Mapped[str | None] = mapped_column(ForeignKey("repeaters.id"), index=True)
    prop_mode: Mapped[str | None] = mapped_column(String(10))
    sat_name: Mapped[str | None] = mapped_column(String(40))
    sat_mode: Mapped[str | None] = mapped_column(String(20))
    distance_km: Mapped[float | None] = mapped_column(Float)
    bearing_deg: Mapped[float | None] = mapped_column(Float)
    # --- confirmations ---
    qsl_sent: Mapped[str | None] = mapped_column(String(2))
    qsl_rcvd: Mapped[str | None] = mapped_column(String(2))
    qsl_via: Mapped[str | None] = mapped_column(String(120))
    lotw_sent: Mapped[str | None] = mapped_column(String(2))
    lotw_rcvd: Mapped[str | None] = mapped_column(String(2))
    eqsl_sent: Mapped[str | None] = mapped_column(String(2))
    eqsl_rcvd: Mapped[str | None] = mapped_column(String(2))
    # --- activities ---
    sota_ref: Mapped[str | None] = mapped_column(String(20))
    pota_ref: Mapped[str | None] = mapped_column(String(40))
    wwff_ref: Mapped[str | None] = mapped_column(String(20))
    my_sota_ref: Mapped[str | None] = mapped_column(String(20))
    my_pota_ref: Mapped[str | None] = mapped_column(String(40))
    my_wwff_ref: Mapped[str | None] = mapped_column(String(20))
    contest_id: Mapped[str | None] = mapped_column(String(40))
    # --- text ---
    comment: Mapped[str | None] = mapped_column(String(500))  # ADIF COMMENT (short, shareable)
    notes: Mapped[str | None] = mapped_column(Text)  # ADIF NOTES (private, long)
    tags: Mapped[str | None] = mapped_column(String(300))
    extra_adif: Mapped[str | None] = mapped_column(Text)  # JSON of unmapped ADIF fields
    import_ref: Mapped[str | None] = mapped_column(String(200))  # file it came from

    station: Mapped[Station | None] = relationship(foreign_keys=[station_id])
    radio: Mapped[Equipment | None] = relationship(foreign_keys=[radio_id])
    antenna: Mapped[Equipment | None] = relationship(foreign_keys=[antenna_id])
    repeater: Mapped[Repeater | None] = relationship(foreign_keys=[repeater_id])

    __table_args__ = (Index("ix_qsos_call_time", "call", "time_on"),)

    def display_name(self) -> str:
        when = self.time_on.strftime("%Y-%m-%d %H:%M") if self.time_on else "?"
        return f"{self.call} · {when}Z · {self.band or ''} {self.mode or ''}".strip()

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.name, self.qth, self.locator, self.country,
                                      self.region, self.band, self.mode, self.submode,
                                      self.sat_name, self.sota_ref, self.pota_ref,
                                      self.wwff_ref, self.contest_id, self.comment,
                                      self.notes, self.tags, self.my_call]))
        return self.display_name(), body


class SwlEntry(SyncMixin, Base):
    """Listening log: something heard (no two-way contact). Kept out of ADIF exports."""

    __tablename__ = "swl_entries"
    __entity__ = "swl"
    __label__ = "Listening entry"

    time_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    time_end: Mapped[datetime | None] = mapped_column(DateTime)
    freq_mhz: Mapped[float | None] = mapped_column(Float, index=True)
    band: Mapped[str | None] = mapped_column(String(10), index=True)
    mode: Mapped[str | None] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(20), default="amateur", index=True)
    heard: Mapped[str | None] = mapped_column(String(80), index=True)  # call/flight/sat/station
    description: Mapped[str | None] = mapped_column(String(500))
    signal: Mapped[str | None] = mapped_column(String(20))  # S-meter, RST, SINPO…
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"), index=True)
    receiver_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    antenna_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    repeater_id: Mapped[str | None] = mapped_column(ForeignKey("repeaters.id"), index=True)
    locator: Mapped[str | None] = mapped_column(String(10))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    my_latitude: Mapped[float | None] = mapped_column(Float)
    my_longitude: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    bearing_deg: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[str | None] = mapped_column(String(300))

    station: Mapped[Station | None] = relationship(foreign_keys=[station_id])
    receiver: Mapped[Equipment | None] = relationship(foreign_keys=[receiver_id])
    antenna: Mapped[Equipment | None] = relationship(foreign_keys=[antenna_id])
    repeater: Mapped[Repeater | None] = relationship(foreign_keys=[repeater_id])

    def display_name(self) -> str:
        when = self.time_utc.strftime("%Y-%m-%d %H:%M") if self.time_utc else "?"
        freq = f"{self.freq_mhz:g} MHz" if self.freq_mhz else ""
        return f"{self.heard or self.category} · {when}Z · {freq}".strip(" ·")

    def search_document(self) -> tuple[str, str]:
        body = " ".join(filter(None, [self.category, self.mode, self.band, self.description,
                                      self.signal, self.locator, self.notes, self.tags]))
        return self.display_name(), body


class JournalEntry(SyncMixin, Base):
    """Antenna & test journal (``kind="test"``) or station diary (``kind="diary"``).

    Sweeps, photos and documents are attached through :class:`Link` to a StoredFile.
    """

    __tablename__ = "journal_entries"
    __entity__ = "journal"
    __label__ = "Journal entry"

    kind: Mapped[str] = mapped_column(String(10), default="diary", index=True)
    time_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str | None] = mapped_column(Text)
    station_id: Mapped[str | None] = mapped_column(ForeignKey("stations.id"))
    equipment_id: Mapped[str | None] = mapped_column(ForeignKey("equipment.id"), index=True)
    tags: Mapped[str | None] = mapped_column(String(300))

    station: Mapped[Station | None] = relationship(foreign_keys=[station_id])
    equipment: Mapped[Equipment | None] = relationship(foreign_keys=[equipment_id])

    def display_name(self) -> str:
        return self.title

    def search_document(self) -> tuple[str, str]:
        return self.title, " ".join(filter(None, [self.kind, self.body, self.tags]))


class Measurement(SyncMixin, Base):
    """One reading in a test journal entry (SWR at a frequency, power, impedance…)."""

    __tablename__ = "measurements"
    __entity__ = "measurement"
    __label__ = "Measurement"

    entry_id: Mapped[str] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    freq_mhz: Mapped[float | None] = mapped_column(Float)
    swr: Mapped[float | None] = mapped_column(Float)
    power_fwd_w: Mapped[float | None] = mapped_column(Float)
    power_ref_w: Mapped[float | None] = mapped_column(Float)
    return_loss_db: Mapped[float | None] = mapped_column(Float)
    r_ohm: Mapped[float | None] = mapped_column(Float)
    x_ohm: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(300))

    def display_name(self) -> str:
        return f"{self.freq_mhz} MHz SWR {self.swr}"


class Link(SyncMixin, Base):
    """Generic link between two records of any type (attachments, manuals, QSL images…).

    ``a`` is the record the link is shown on first (e.g. the radio), ``b`` the thing linked
    (e.g. its manual). Pages show links in both directions.
    """

    __tablename__ = "links"
    __entity__ = "link"
    __label__ = "Link"

    a_type: Mapped[str] = mapped_column(String(20))
    a_id: Mapped[str] = mapped_column(String(36))
    b_type: Mapped[str] = mapped_column(String(20))
    b_id: Mapped[str] = mapped_column(String(36))
    role: Mapped[str | None] = mapped_column(String(40))  # "manual", "sweep", "photo"…
    note: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        Index("ix_links_a", "a_type", "a_id"),
        Index("ix_links_b", "b_type", "b_id"),
    )


class SavedFilter(SyncMixin, Base):
    """A named filter (the query string of a list page) for one section."""

    __tablename__ = "saved_filters"
    __entity__ = "saved_filter"
    __label__ = "Saved filter"

    name: Mapped[str] = mapped_column(String(120))
    target: Mapped[str] = mapped_column(String(20), index=True)  # entity name, e.g. "qso"
    query: Mapped[str] = mapped_column(Text)  # URL query string without "?"
    notes: Mapped[str | None] = mapped_column(Text)

    def display_name(self) -> str:
        return self.name


#: Every synced model, parents before children. Sync, bundles and trash iterate this.
SYNC_ORDER: list[type[SyncMixin]] = [
    Equipment,
    Station,
    Repeater,
    Contact,
    StoredFile,
    RadioConfig,
    RadioChannel,
    Qso,
    SwlEntry,
    JournalEntry,
    Measurement,
    Link,
    SavedFilter,
]

#: ``"qso" → Qso`` lookup used by links, search and the trash page.
ENTITIES: dict[str, type[SyncMixin]] = {m.__entity__: m for m in SYNC_ORDER}
#: ``"qsos" → Qso`` lookup used by the sync engine.
TABLES: dict[str, type[SyncMixin]] = {m.__tablename__: m for m in SYNC_ORDER}


# ---------------------------------------------------------------------------
# Local tables (never synced)
# ---------------------------------------------------------------------------

class Setting(Base):
    """Key/value preferences of this instance. Values are JSON text."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class Meta(Base):
    """Engine state: schema version, this instance's id and name."""

    __tablename__ = "meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class SeqCounter(Base):
    """Single row holding the last ``change_seq`` handed out (see db.py)."""

    __tablename__ = "seq_counter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class SyncPeer(Base):
    """Another instance this one syncs with (the hub, or clients seen by the hub)."""

    __tablename__ = "sync_peers"

    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(10), default="client")  # "hub" or "client"
    last_pull_seq: Mapped[int] = mapped_column(Integer, default=0)  # peer's seq we have
    last_push_seq: Mapped[int] = mapped_column(Integer, default=0)  # our seq peer has
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)


class SyncConflict(Base):
    """A version that lost a merge. Kept forever unless Andrea dismisses it."""

    __tablename__ = "sync_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(40))
    record_id: Mapped[str] = mapped_column(String(36), index=True)
    peer_id: Mapped[str | None] = mapped_column(String(36))
    peer_name: Mapped[str | None] = mapped_column(String(120))
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # "local_overwritten": my unsynced edit lost to a newer remote edit;
    # "incoming_discarded": an older remote version arrived after mine.
    kind: Mapped[str] = mapped_column(String(24))
    loser_snapshot: Mapped[str] = mapped_column(Text)  # JSON of the losing version
    winner_snapshot: Mapped[str | None] = mapped_column(Text)  # JSON of the winner then
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution: Mapped[str | None] = mapped_column(String(20))  # "dismissed", "restored"


class ImportLog(Base):
    """History of imports (ADIF, CHIRP, bundles…) with a JSON summary."""

    __tablename__ = "import_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(20))
    filename: Mapped[str | None] = mapped_column(String(300))
    summary: Mapped[str | None] = mapped_column(Text)
