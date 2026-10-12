"""Touchstone (.s1p / .s2p) sweeps from a NanoVNA: parsing, SWR summary and an SVG plot.

The antenna & test journal attaches sweep files to an entry. This module reads them
(Touchstone 1.x, as written by NanoVNA-Saver, NanoVNA-App and the NanoVNA's own
``touchstone`` export), works out the resonance and the SWR ≤ 2 bandwidth, and draws a
small self-contained SVG that the CSS themes (``.sweep-svg`` in ``static/css/app.css``).

Format reminder
---------------
* ``!`` starts a comment (whole line or end of line).
* The option line ``# <unit> <parameter> <format> R <z0>`` may list its items in any order;
  missing items default to ``GHz S MA R 50`` (Touchstone 1.1 spec).
* Each data line is a frequency followed by pairs: ``S11`` for .s1p; ``S11 S21 S12 S22``
  for .s2p. Pair formats: ``RI`` (real, imag), ``MA`` (magnitude, angle°), ``DB``
  (20·log10 magnitude, angle°). Only S11 is used: that is the antenna port.

Pure logic: stdlib only, no Flask.
"""

from __future__ import annotations

import cmath
import math
from html import escape

_UNITS = {"HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}
_UNIT_NAMES = {1.0: "Hz", 1e3: "kHz", 1e6: "MHz", 1e9: "GHz"}
_FORMATS = {"RI", "MA", "DB"}

#: Plot range of the SWR axis. Values above are drawn on the top edge.
SWR_AXIS_MAX = 5.0
SWR_GRID = (1.5, 2.0, 3.0)


class TouchstoneError(ValueError):
    """The text is not a usable Touchstone file. The message says why, for the user."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_options(line: str) -> tuple[float, str, str, float]:
    """``# MHz S RI R 50`` → (Hz per unit, parameter, format, z0)."""
    unit, param, fmt, z0 = 1e9, "S", "MA", 50.0
    tokens = line[1:].split()
    i = 0
    while i < len(tokens):
        tok = tokens[i].upper()
        if tok in _UNITS:
            unit = _UNITS[tok]
        elif tok in _FORMATS:
            fmt = tok
        elif tok in ("S", "Y", "Z", "H", "G"):
            param = tok
        elif tok == "R" and i + 1 < len(tokens):
            try:
                z0 = float(tokens[i + 1])
            except ValueError:
                raise TouchstoneError(f"Reference impedance {tokens[i + 1]!r} is not a number.")
            i += 1
        i += 1
    if z0 <= 0:
        raise TouchstoneError("Reference impedance must be positive.")
    return unit, param, fmt, z0


def _pair_to_complex(a: float, b: float, fmt: str) -> complex:
    if fmt == "RI":
        return complex(a, b)
    magnitude = a if fmt == "MA" else 10 ** (a / 20.0)
    return cmath.rect(magnitude, math.radians(b))


def parse_touchstone(text: str) -> dict:
    """Parse Touchstone text into ``{"freq_hz": [...], "s11": [complex…], "z0": float}``.

    Also returns ``"unit"`` and ``"format"`` from the option line, for display. Points
    are sorted by frequency. Raises :class:`TouchstoneError` when there is no data or the
    file holds Y/Z parameters (which this module doesn't convert).
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    unit, param, fmt, z0 = 1e9, "S", "MA", 50.0
    seen_options = False
    points: list[tuple[float, complex]] = []
    # Numbers are read as one stream and cut into records, because a record may wrap over
    # lines. The record width is fixed by the first data line: 3 numbers (frequency + one
    # pair) is .s1p; anything longer is .s2p (frequency + four pairs = 9 numbers).
    numbers: list[float] = []
    width: int | None = None
    for lineno, line in enumerate(text.lstrip("﻿").splitlines(), start=1):
        line = line.split("!", 1)[0].strip()
        if not line:
            continue
        if line.startswith("#"):
            if not seen_options:  # the spec says only the first option line counts
                unit, param, fmt, z0 = _parse_options(line)
                seen_options = True
            continue
        if line.startswith("["):  # Touchstone 2 keywords; v1 data still follows
            continue
        try:
            values = [float(v) for v in line.replace(",", " ").split()]
        except ValueError:
            raise TouchstoneError(f"Line {lineno} is not a row of numbers: {line[:40]!r}.")
        if width is None:
            if len(values) < 3:
                raise TouchstoneError(f"Line {lineno} has too few values for a data point.")
            width = 3 if len(values) == 3 else 9
        numbers.extend(values)
        while len(numbers) >= width:
            record, numbers = numbers[:width], numbers[width:]
            points.append((record[0] * unit, _pair_to_complex(record[1], record[2], fmt)))
    # An incomplete last record (a truncated file) is dropped: the plot is still useful.
    if param != "S":
        raise TouchstoneError(f"{param}-parameters are not supported; export S-parameters.")
    if not points:
        raise TouchstoneError("No data points found in the file.")
    points.sort(key=lambda p: p[0])
    return {
        "freq_hz": [p[0] for p in points],
        "s11": [p[1] for p in points],
        "z0": z0,
        "unit": _UNIT_NAMES[unit],
        "format": fmt,
    }


# ---------------------------------------------------------------------------
# Derived quantities
# ---------------------------------------------------------------------------

def swr(s: complex) -> float:
    """VSWR from a reflection coefficient. ``inf`` for |Γ| ≥ 1 (open, short, or noise)."""
    gamma = abs(s)
    if gamma >= 1.0:
        return math.inf
    return (1 + gamma) / (1 - gamma)


def return_loss_db(s: complex) -> float:
    """Return loss in dB (positive number; larger is a better match). ``inf`` at |Γ| = 0."""
    gamma = abs(s)
    if gamma == 0:
        return math.inf
    return -20.0 * math.log10(gamma)


def impedance(s: complex, z0: float = 50.0) -> complex:
    """Load impedance Z = z0·(1+Γ)/(1−Γ). Γ = 1 (open) gives an infinite real part."""
    if s == 1:
        return complex(math.inf, 0)
    return z0 * (1 + s) / (1 - s)


def summarize(parsed: dict) -> dict:
    """Key numbers of a sweep.

    ``min_swr`` and ``freq_min_mhz`` at the best point, with ``r_ohm``/``x_ohm`` there;
    ``bw_swr2_low_mhz``/``bw_swr2_high_mhz``: the contiguous range around the minimum where
    SWR ≤ 2 (None when even the minimum is above 2; a range touching a sweep edge is
    reported up to that edge); ``start_mhz``, ``stop_mhz``, ``points``.
    """
    freqs = parsed.get("freq_hz") or []
    s11 = parsed.get("s11") or []
    z0 = parsed.get("z0", 50.0)
    out = {"min_swr": None, "freq_min_mhz": None, "r_ohm": None, "x_ohm": None,
           "return_loss_db": None, "bw_swr2_low_mhz": None, "bw_swr2_high_mhz": None,
           "start_mhz": None, "stop_mhz": None, "points": len(freqs)}
    if not freqs:
        return out
    swrs = [swr(g) for g in s11]
    best = min(range(len(swrs)), key=lambda i: swrs[i])
    z = impedance(s11[best], z0)
    out.update(
        min_swr=round(swrs[best], 3) if math.isfinite(swrs[best]) else None,
        freq_min_mhz=round(freqs[best] / 1e6, 6),
        r_ohm=round(z.real, 2) if math.isfinite(z.real) else None,
        x_ohm=round(z.imag, 2) if math.isfinite(z.imag) else None,
        return_loss_db=round(return_loss_db(s11[best]), 2) if s11[best] != 0 else None,
        start_mhz=round(freqs[0] / 1e6, 6),
        stop_mhz=round(freqs[-1] / 1e6, 6),
    )
    if swrs[best] <= 2.0:
        lo = best
        while lo > 0 and swrs[lo - 1] <= 2.0:
            lo -= 1
        hi = best
        while hi < len(swrs) - 1 and swrs[hi + 1] <= 2.0:
            hi += 1
        # Interpolate to where the curve crosses 2.0, so the bandwidth doesn't depend on
        # the step size of the sweep.
        low = freqs[lo] if lo == 0 else _crossing(freqs[lo - 1], swrs[lo - 1], freqs[lo], swrs[lo])
        high = freqs[hi] if hi == len(swrs) - 1 else _crossing(freqs[hi], swrs[hi], freqs[hi + 1], swrs[hi + 1])
        out["bw_swr2_low_mhz"] = round(low / 1e6, 6)
        out["bw_swr2_high_mhz"] = round(high / 1e6, 6)
    return out


def _crossing(f1: float, s1: float, f2: float, s2: float, level: float = 2.0) -> float:
    """Frequency where the line from (f1, s1) to (f2, s2) crosses ``level``."""
    if not math.isfinite(s1) or not math.isfinite(s2) or s1 == s2:
        return f2 if s2 <= level else f1
    return f1 + (level - s1) * (f2 - f1) / (s2 - s1)


# ---------------------------------------------------------------------------
# SVG plot
# ---------------------------------------------------------------------------

def _nice_step(span: float, target: int = 6) -> float:
    """A 1/2/5 × 10ⁿ step giving about ``target`` ticks over ``span``."""
    if span <= 0:
        return 1.0
    raw = span / target
    power = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 5, 10):
        if raw <= mult * power:
            return mult * power
    return 10 * power


def _fmt_mhz(value: float, step: float) -> str:
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    return f"{value:.{decimals}f}"


def swr_svg(parsed: dict, width: int = 640, height: int = 260,
            shade: list[tuple[float, float]] | None = None) -> str:
    """SWR-versus-frequency chart as an inline ``<svg>`` string.

    SWR axis 1–5 (higher values drawn on the top edge), grid lines at 1.5/2/3, frequency
    ticks in MHz, ``shade`` ranges in MHz (e.g. ``[(144, 146)]``) drawn behind the curve,
    and the minimum marked and labelled. No colours: CSS classes ``grid-line``, ``curve``,
    ``band``, ``marker`` and ``fill="currentColor"`` on text let the page theme it.
    """
    freqs = [f / 1e6 for f in parsed.get("freq_hz") or []]
    s11 = parsed.get("s11") or []
    summary = summarize(parsed)
    left, right, top, bottom = 40, 12, 14, 30
    plot_w, plot_h = max(width - left - right, 10), max(height - top - bottom, 10)
    title = "SWR sweep"
    if summary["min_swr"] is not None:
        title = f"SWR sweep, minimum {summary['min_swr']:.2f} at {summary['freq_min_mhz']:.3f} MHz"
    parts = [
        f'<svg class="sweep-svg" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="{escape(title)}" font-size="11">',
        f"<title>{escape(title)}</title>",
    ]
    if not freqs:
        parts.append(f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" '
                     f'fill="currentColor">No data</text></svg>')
        return "".join(parts)

    f_lo, f_hi = freqs[0], freqs[-1]
    if f_hi - f_lo < 1e-9:  # a single point: give it some room
        f_lo, f_hi = f_lo - 0.5, f_hi + 0.5

    def x_of(mhz: float) -> float:
        return left + (mhz - f_lo) / (f_hi - f_lo) * plot_w

    def y_of(value: float) -> float:
        clipped = min(max(value, 1.0), SWR_AXIS_MAX) if math.isfinite(value) else SWR_AXIS_MAX
        return top + (SWR_AXIS_MAX - clipped) / (SWR_AXIS_MAX - 1.0) * plot_h

    # Shaded ranges (amateur band edges), clipped to the sweep.
    for low, high in shade or []:
        a, b = max(min(low, high), f_lo), min(max(low, high), f_hi)
        if b > a:
            # fill="none" outside the app (no CSS) rather than a black block over the plot.
            parts.append(f'<rect class="band" fill="none" x="{x_of(a):.1f}" y="{top}" '
                         f'width="{x_of(b) - x_of(a):.1f}" height="{plot_h}"/>')

    # Horizontal grid and SWR labels. stroke="currentColor" is only a fallback for the SVG
    # seen without the app's CSS; ``.sweep-svg .grid-line`` overrides it.
    for value in (1.0, *SWR_GRID, SWR_AXIS_MAX):
        y = y_of(value)
        cls = "grid-line" if value in SWR_GRID else "grid-line axis"
        parts.append(f'<line class="{cls}" stroke="currentColor" x1="{left}" y1="{y:.1f}" '
                     f'x2="{left + plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{left - 4}" y="{y + 4:.1f}" text-anchor="end" '
                     f'fill="currentColor">{value:g}</text>')

    # Frequency ticks.
    step = _nice_step(f_hi - f_lo)
    first = math.ceil(f_lo / step - 1e-9)
    # Multiply rather than accumulate, so 0.2 MHz steps don't drift past the last tick.
    for k in range(first, math.floor(f_hi / step + 1e-9) + 1):
        tick = k * step
        x = x_of(tick)
        parts.append(f'<line class="grid-line" stroke="currentColor" x1="{x:.1f}" y1="{top}" '
                     f'x2="{x:.1f}" y2="{top + plot_h}"/>')
        parts.append(f'<text x="{x:.1f}" y="{top + plot_h + 14}" text-anchor="middle" '
                     f'fill="currentColor">{escape(_fmt_mhz(tick, step))}</text>')
    parts.append(f'<text x="{left + plot_w}" y="{height - 2}" text-anchor="end" '
                 f'fill="currentColor">MHz</text>')

    # The curve.
    coords = " ".join(f"{x_of(f):.1f},{y_of(swr(g)):.1f}" for f, g in zip(freqs, s11))
    parts.append(f'<polyline class="curve" fill="none" stroke="currentColor" points="{coords}"/>')

    # Minimum marker and label (kept inside the plot horizontally).
    if summary["freq_min_mhz"] is not None:
        mx = x_of(summary["freq_min_mhz"])
        my = y_of(summary["min_swr"] if summary["min_swr"] is not None else math.inf)
        label = (f"{summary['freq_min_mhz']:.3f} MHz · SWR "
                 f"{summary['min_swr']:.2f}" if summary["min_swr"] is not None else "SWR ∞")
        anchor = "start" if mx < left + plot_w * 0.6 else "end"
        lx = mx + 6 if anchor == "start" else mx - 6
        ly = my - 8 if my - 8 > top + 10 else my + 16
        parts.append(f'<circle class="marker" cx="{mx:.1f}" cy="{my:.1f}" r="3.5" fill="currentColor"/>')
        parts.append(f'<text class="marker-label" x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
                     f'fill="currentColor">{escape(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)
