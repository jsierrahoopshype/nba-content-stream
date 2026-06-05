"""Small animation math for the v2.2 social edit.

Pure functions — kept separate from `easing` (the curves) so the
counter/slide timing is unit-testable on its own. The v2.2 retention
rules need fast, legible motion: counters that finish in <1s, block
slide-ups in ~0.35s.
"""

from __future__ import annotations

from . import easing


def count_up(t: float, total: int, duration: float, *, ease=easing.quart_out) -> int:
    """Integer counter value at time `t`, ramping 0→`total` over
    `duration` seconds (eased). Clamps: t<=0 → 0, t>=duration → total."""
    if total <= 0:
        return 0
    if duration <= 0 or t >= duration:
        return total
    if t <= 0:
        return 0
    return int(round(total * ease(t / duration)))


def slide_offset(t: float, distance: float, duration: float, *, ease=easing.quart_out) -> float:
    """Offset for a slide-in: starts at `distance`, eases to 0 by
    `duration`. Use a positive `distance` to slide up from below."""
    if duration <= 0 or t >= duration:
        return 0.0
    if t <= 0:
        return distance
    return distance * (1.0 - ease(t / duration))


def block_alpha(t: float, *, fade_in: float = 0.35) -> float:
    """Single-shot block fade-in alpha (0→1) over `fade_in` seconds,
    quart-out. No fade-out — v2.2 holds content, it doesn't drip it."""
    if fade_in <= 0:
        return 1.0
    return easing.quart_out(max(0.0, min(1.0, t / fade_in)))
