"""Collision-safe layout zones (v2.1).

The first render had the hero name overlapping the portrait circle and
the mention count clipped behind the source-pill row. The fix is to
reserve explicit, non-overlapping rectangles up front and place every
element inside its reserved zone.

`hero_layout` is pure — it takes pre-measured element sizes and returns
a `Box` per element — so a test can assert no two zones intersect for
the longest canonical names without rendering a single pixel.

Geometry only; no Pillow / moviepy imports.
"""

from __future__ import annotations

from typing import NamedTuple

from . import format_specs as fs


class Box(NamedTuple):
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def cx(self) -> int:
        return (self.x0 + self.x1) // 2

    @property
    def cy(self) -> int:
        return (self.y0 + self.y1) // 2


def intersects(a: Box, b: Box, *, pad: int = 0) -> bool:
    """True if `a` and `b` overlap (touching edges do not count). `pad`
    inflates both boxes first, so `pad > 0` enforces a minimum gap."""
    return not (
        a.x1 + pad <= b.x0 - pad
        or b.x1 + pad <= a.x0 - pad
        or a.y1 + pad <= b.y0 - pad
        or b.y1 + pad <= a.y0 - pad
    )


def hero_portrait_diameter(spec: fs.FormatSpec) -> int:
    """Steady-state portrait diameter for the hero card — capped at 45%
    of frame height per the v2.1 review so name/count have room."""
    return min(spec.portrait_size, int(spec.height * 0.42))


def hero_layout(
    spec: fs.FormatSpec,
    *,
    rank_size: tuple[int, int],
    name_size: tuple[int, int],
    sub_size: tuple[int, int],
    count_size: tuple[int, int],
    count_label_size: tuple[int, int],
    pill_size: tuple[int, int],
    gap: int | None = None,
) -> dict[str, Box]:
    """Return non-overlapping `Box` zones for the hero card.

    Elements stack as vertical bands: rank glyph (top-left), portrait
    (centered), name, team sub — then, anchored from the bottom edge
    up: the source-pill row, the count label, and the count itself
    (right-aligned in its own band above the pills). Every `*_size` is
    a measured `(width, height)`.
    """
    w, h, pad = spec.width, spec.height, spec.pad
    g = gap if gap is not None else pad // 2

    rank_w, rank_h = rank_size
    name_w, name_h = name_size
    sub_w, sub_h = sub_size
    count_w, count_h = count_size
    clabel_w, clabel_h = count_label_size
    pill_w, pill_h = pill_size

    # ----- top-down stack -----
    y = pad
    rank = Box(pad, y, pad + rank_w, y + rank_h)
    y = rank.y1 + g

    d = hero_portrait_diameter(spec)
    px0 = (w - d) // 2
    portrait = Box(px0, y, px0 + d, y + d)
    y = portrait.y1 + g

    name = Box((w - name_w) // 2, y, (w + name_w) // 2, y + name_h)
    y = name.y1 + g // 2

    sub = Box((w - sub_w) // 2, y, (w + sub_w) // 2, y + sub_h)

    # ----- bottom-up stack -----
    pills = Box((w - pill_w) // 2, h - pad - pill_h, (w + pill_w) // 2, h - pad)
    clabel = Box(
        w - pad - clabel_w, pills.y0 - g - clabel_h,
        w - pad, pills.y0 - g,
    )
    count = Box(w - pad - count_w, clabel.y0 - count_h, w - pad, clabel.y0)

    return {
        "rank": rank,
        "portrait": portrait,
        "name": name,
        "sub": sub,
        "count": count,
        "count_label": clabel,
        "pills": pills,
    }
