"""Declarative forms: describe fields once, get parsing, validation and rendering.

A section's form is a list of :class:`Section`, each holding :class:`Field` objects::

    REPEATER_FORM = [
        Section("Identity", [
            Field("name", "Name", required=True, width="full"),
            Field("callsign", "Callsign", type="upper"),
        ]),
    ]

Views call :func:`values_from_obj` to fill a form, :func:`parse` to read a submission
(returns python values + errors) and :func:`apply` to copy values onto a model. The
template macro ``render_form`` (templates/_forms.html) draws it.

Field types
-----------
text, textarea, upper (callsigns: upper-cased, spaces removed), locator, int, float,
freq (MHz; accepts "145,500" and kHz like "145500"), power, lat, lon, date, datetime
(UTC, from <input type=datetime-local>), bool, select, tags, url, email.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Sequence

from shacklog.models import split_tags
from shacklog.services import bands, geo

Choices = Sequence[tuple[str, str]]


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"
    required: bool = False
    #: For ``select``: a list of (value, label), or ``callable(session) -> list``.
    choices: Choices | Callable[[Any], Choices] | None = None
    help: str | None = None
    placeholder: str | None = None
    #: Layout on wide screens: "full", "half", "third" or "quarter" of the row.
    width: str = "half"
    #: Extra HTML attributes (e.g. htmx hooks, autocomplete=off, step).
    attrs: dict = field(default_factory=dict)
    #: Add an empty first option to a select (for optional references).
    blank: bool = True


@dataclass
class Section:
    title: str | None
    fields: list[Field]
    #: Collapsed sections hold rarely used fields (shown open if any value is set).
    open: bool = True
    note: str | None = None


def all_fields(sections: list[Section]) -> list[Field]:
    return [f for s in sections for f in s.fields]


def section_is_open(section: Section, values: dict, errors: dict) -> bool:
    """Collapsed sections open themselves when they hold a value or an error."""
    if section.open:
        return True
    return any(values.get(f.name) or f.name in errors for f in section.fields)


# ---------------------------------------------------------------------------
# Python value → form string
# ---------------------------------------------------------------------------

def format_value(f: Field, value: Any) -> str:
    """How a stored value appears inside an <input>."""
    if value is None:
        return ""
    if f.type == "datetime" and isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M")
    if f.type == "date" and isinstance(value, date):
        return value.isoformat()
    if f.type == "freq" and isinstance(value, (int, float)):
        text = f"{value:.6f}".rstrip("0")
        whole, _, frac = text.partition(".")
        return f"{whole}.{frac.ljust(3, '0')}"  # at least kHz resolution: 145.500
    if f.type == "bool":
        return "1" if value else ""
    if isinstance(value, float):
        return f"{value:g}" if abs(value) < 1e15 else str(value)
    return str(value)


def values_from_obj(sections: list[Section], obj: Any) -> dict[str, str]:
    return {f.name: format_value(f, getattr(obj, f.name, None)) for f in all_fields(sections)}


def resolve_choices(sections: list[Section], session) -> dict[str, Choices]:
    """Evaluate callable choices once per request."""
    out: dict[str, Choices] = {}
    for f in all_fields(sections):
        if f.choices is None:
            continue
        out[f.name] = f.choices(session) if callable(f.choices) else f.choices
    return out


# ---------------------------------------------------------------------------
# Form string → Python value
# ---------------------------------------------------------------------------

class FieldError(ValueError):
    pass


def _number(raw: str) -> float:
    # Accept Italian/French decimal commas ("145,500") and thin spaces.
    cleaned = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        raise FieldError("Enter a number") from None


def parse_freq(raw: str) -> float:
    """MHz from user text. ``145,5`` → 145.5; ``145500`` (kHz) → 145.5 when that is a band."""
    value = _number(raw)
    if value <= 0:
        raise FieldError("Frequency must be positive")
    # Typed in kHz? ("145500", "7074"). Only when the MHz reading falls in no real band
    # (or the catch-all "submm") while the kHz reading does.
    as_mhz_band = bands.band_for_freq(value)
    if as_mhz_band in (None, "submm") and bands.band_for_freq(value / 1000):
        value = value / 1000
    return round(value, 6)


def parse_value(f: Field, raw: str | None) -> Any:
    raw = (raw or "").strip()
    if f.type == "bool":
        return raw not in ("", "0", "false", "off")
    if raw == "":
        if f.required:
            raise FieldError("Required")
        return None
    t = f.type
    if t == "upper":
        return raw.replace(" ", "").upper()
    if t == "locator":
        loc = geo.normalize_locator(raw)
        if loc is None:
            raise FieldError("Not a Maidenhead locator (e.g. JN45ol)")
        return loc
    if t == "int":
        try:
            return int(_number(raw))
        except FieldError:
            raise FieldError("Enter a whole number") from None
    if t in ("float", "power"):
        return _number(raw)
    if t == "freq":
        return parse_freq(raw)
    if t == "lat":
        v = _number(raw)
        if not -90 <= v <= 90:
            raise FieldError("Latitude is between -90 and 90")
        return v
    if t == "lon":
        v = _number(raw)
        if not -180 <= v <= 180:
            raise FieldError("Longitude is between -180 and 180")
        return v
    if t == "date":
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            raise FieldError("Use YYYY-MM-DD") from None
    if t == "datetime":
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise FieldError("Use YYYY-MM-DD HH:MM (UTC)")
    if t == "tags":
        return ", ".join(split_tags(raw)) or None
    if t == "select" and f.choices is not None and not callable(f.choices):
        allowed = {value for value, _ in f.choices}
        if raw not in allowed:
            raise FieldError("Pick a value from the list")
    return raw


def parse(sections: list[Section], formdata) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Read a submitted form.

    Returns ``(values, errors, raw)``: python values for valid fields, an error message per
    invalid field, and the raw strings (to redisplay the form exactly as typed).
    """
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    raw: dict[str, str] = {}
    for f in all_fields(sections):
        text = formdata.get(f.name)
        raw[f.name] = "1" if (f.type == "bool" and text) else (text or "")
        try:
            values[f.name] = parse_value(f, text)
        except FieldError as exc:
            errors[f.name] = str(exc)
    return values, errors, raw


def apply(obj: Any, values: dict[str, Any]) -> None:
    """Copy parsed values onto a model instance."""
    for name, value in values.items():
        setattr(obj, name, value)
