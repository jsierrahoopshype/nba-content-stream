"""Vertical-first type + spacing scale for the v2.2 social edit.

v2.2 designs for 9:16 (1080×1920) first, then adapts to square and
horizontal by scaling every size against the frame height (1920 = 1.0).
This keeps the shared `format_specs.FormatSpec` font fields untouched
(v1 still reads them) while giving the v2.2 renderers their own bold,
social-optimized type scale.
"""

from __future__ import annotations

from . import format_specs as fs

# Reference height — the vertical master. All sizes below are quoted at
# this height and scaled by (spec.height / REF_H) for other formats.
REF_H = 1920.0

# Brand-blue duotone endpoints (shadows → highlights) for hero portraits.
DUOTONE_SHADOW = "#1e3a8a"
DUOTONE_HIGHLIGHT = "#eff6ff"


def metrics(spec: fs.FormatSpec) -> dict[str, int]:
    """Vertical-first type/spacing sizes for `spec`, scaled by height."""
    k = spec.height / REF_H

    def s(px: float) -> int:
        return max(10, int(round(px * k)))

    return {
        "k": k,
        "pad": spec.pad,
        # cold open
        "hook": s(92),
        "hook_sub": s(44),
        # hero
        "rank": s(230),
        "name": s(140),
        "name_sub": s(40),
        "count": s(170),
        "count_label": s(38),
        "pill": s(30),
        # quote card
        "quote": s(66),
        "reporter_name": s(44),
        "handle": s(34),
        "ticker": s(104),
        "ticker_label": s(30),
        # sparkline
        "spark_head": s(56),
        "spark_total": s(150),
        "spark_total_label": s(36),
        "context": s(34),
        "axis": s(28),
        "callout": s(40),
        # outro + cta
        "outro_label": s(40),
        "outro_head": s(86),
        "row": s(48),
        "cta": s(116),
        "cta_sub": s(42),
        # persistent brand mark
        "brand": s(32),
    }
