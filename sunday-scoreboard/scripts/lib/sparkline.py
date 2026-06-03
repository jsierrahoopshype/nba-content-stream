"""Animated mention-spike sparkline for the v2 spotlight Phase 3.

The chart tells one player's 7-day mention story: daily counts Monday
→ Sunday, drawn left-to-right, peak day called out. The Y-axis is
scaled *per player* — each beat owns its own data story, so a quiet
week still reads as a shape rather than a flat line lost under a
shared global maximum.

Math (daily bucketing, peak detection, point mapping, draw progress)
is pure and unit-tested; `draw_sparkline` composes it onto a PIL
canvas using the shared draw primitives.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from . import draw as d
from . import easing
from . import format_specs as fs

# Day-of-week labels for the X axis. The week opens on Sunday in the
# pipeline's WeekRange, but the brief labels Monday→Sunday; we label by
# each bucket's actual weekday so the axis is always truthful.
_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _parse_iso(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def daily_mention_counts(
    items: list[dict], week_start: datetime, *, days: int = 7
) -> list[int]:
    """Bucket `items` into `days` daily mention counts starting at
    `week_start` (inclusive). Items outside the window are ignored.
    Returns a list of length `days`."""
    counts = [0] * days
    for it in items:
        ts = _parse_iso(it.get("published_at", ""))
        if ts is None:
            continue
        offset = int((ts - week_start).total_seconds() // 86400)
        if 0 <= offset < days:
            counts[offset] += 1
    return counts


def day_labels(week_start: datetime, *, days: int = 7) -> list[str]:
    """Weekday abbreviations for each bucket, derived from the actual
    calendar weekday of `week_start + offset`."""
    return [
        _WEEKDAY_ABBR[(week_start + timedelta(days=i)).weekday()]
        for i in range(days)
    ]


def peak_index(values: list[int]) -> int:
    """Index of the maximum value (first wins on ties); -1 if empty or
    all-zero (no peak worth calling out)."""
    if not values or max(values) == 0:
        return -1
    return max(range(len(values)), key=lambda i: values[i])


def normalize_series(values: list[int], height: int) -> list[float]:
    """Scale `values` to pixel heights in `[0, height]`, per-series:
    the series max maps to `height`. An all-zero series maps to all
    zeros (a flat baseline)."""
    if not values:
        return []
    peak = max(values)
    if peak <= 0:
        return [0.0] * len(values)
    return [v / peak * height for v in values]


def sparkline_points(
    values: list[int], box: tuple[int, int, int, int]
) -> list[tuple[int, int]]:
    """Map `values` to pixel points inside `box` (x0, y0, x1, y1).

    X is evenly spaced across the box width; Y is inverted (larger
    value → higher on screen, i.e. smaller pixel-y). A single value is
    pinned to the box's left edge.
    """
    x0, y0, x1, y1 = box
    n = len(values)
    if n == 0:
        return []
    h = max(1, y1 - y0)
    norm = normalize_series(values, h)
    if n == 1:
        return [(x0, int(y1 - norm[0]))]
    step = (x1 - x0) / (n - 1)
    return [(int(x0 + i * step), int(y1 - norm[i])) for i in range(n)]


def draw_progress(n_points: int, t: float) -> tuple[int, float]:
    """How much of an `n_points` polyline to draw at progress `t` (0→1).

    Returns `(full_points, partial)`: `full_points` whole vertices are
    connected, plus a `partial` (0→1) fraction of the segment to the
    next vertex. Lets the line animate smoothly rather than snapping
    vertex-to-vertex.
    """
    if n_points <= 1:
        return (n_points, 0.0)
    t = max(0.0, min(1.0, t))
    segments = n_points - 1
    pos = t * segments
    full = int(pos)
    partial = pos - full
    # At t==1 we want every vertex connected, no dangling partial.
    if full >= segments:
        return (n_points, 0.0)
    return (full + 1, partial)


def _lerp_point(
    a: tuple[int, int], b: tuple[int, int], f: float
) -> tuple[int, int]:
    return (int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f))


def draw_sparkline(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    values: list[int],
    t: float,
    *,
    labels: list[str] | None = None,
    line_color: str = fs.ACCENT,
    axis_color: str = fs.BORDER,
    peak_callout: str | None = None,
) -> None:
    """Draw an animated sparkline into `box` at progress `t`.

    Renders the baseline + a left-to-right growing polyline, X-axis day
    labels, and — once the draw passes the peak — a dot + callout on
    the peak day. The Y-axis is implicit (per-series scaled); we don't
    clutter the frame with numeric gridlines.
    """
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(canvas)

    # Baseline.
    draw.line([(x0, y1), (x1, y1)], fill=d.rgb(axis_color, 255), width=2)

    pts = sparkline_points(values, box)
    if not pts:
        return

    full, partial = draw_progress(len(pts), t)
    visible = list(pts[:full])
    # Append the partial segment tip so the line grows continuously.
    if 0 < full < len(pts) and partial > 0:
        visible.append(_lerp_point(pts[full - 1], pts[full], partial))

    if len(visible) >= 2:
        draw.line(visible, fill=d.rgb(line_color, 255), width=4, joint="curve")
    elif len(visible) == 1:
        cx, cy = visible[0]
        draw.ellipse((cx - 3, cy - 3, cx + 3, cy + 3), fill=d.rgb(line_color, 255))

    # X-axis labels.
    if labels:
        label_fnt = d.font(fs.FONT_MONO, max(14, (y1 - y0) // 10))
        for (px, _), lbl in zip(pts, labels):
            d.draw_text(
                canvas, (px, y1 + 10), lbl, label_fnt,
                fill=fs.TEXT_SECONDARY, anchor="mt",
            )

    # Peak callout — only once the animation has drawn through it.
    pk = peak_index(values)
    if pk >= 0:
        peak_px, peak_py = pts[pk]
        drawn_through = (full - 1) >= pk or full >= len(pts)
        if drawn_through:
            draw.ellipse(
                (peak_px - 6, peak_py - 6, peak_px + 6, peak_py + 6),
                fill=d.rgb(line_color, 255),
            )
            if peak_callout:
                callout_fnt = d.font(fs.FONT_MONO_BOLD, max(16, (y1 - y0) // 9))
                # Place the callout above the dot, clamped into the box.
                ty = max(y0, peak_py - 28)
                anchor_x = min(max(peak_px, x0 + 60), x1 - 60)
                d.draw_text(
                    canvas, (anchor_x, ty), peak_callout, callout_fnt,
                    fill=fs.ACCENT, anchor="mb",
                )
