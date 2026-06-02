"""Easing functions for premium-feeling animations.

The brief says quart-out for entrances, sin-in/out for transitions.
No bouncy / overshoot. All functions take and return a unit progress
value `t ∈ [0, 1]`.
"""

from __future__ import annotations

import math


def linear(t: float) -> float:
    return max(0.0, min(1.0, t))


def quart_out(t: float) -> float:
    """Fast start, decelerates into the final position. The standard
    "premium entrance" curve — material design easing standard."""
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 4


def quart_in(t: float) -> float:
    """Slow start, accelerates out. Used for exits so an element
    leaves with momentum rather than lingering."""
    t = max(0.0, min(1.0, t))
    return t ** 4


def sin_in_out(t: float) -> float:
    """Symmetric sine ease for transitions where the midpoint should
    feel balanced — no obvious "start" or "end" velocity."""
    t = max(0.0, min(1.0, t))
    return -(math.cos(math.pi * t) - 1) / 2


def fade(t: float, *, fade_in: float = 0.3, fade_out: float = 0.3, dur: float = 1.0) -> float:
    """Alpha envelope: ramp 0→1 over `fade_in` seconds at the start,
    hold at 1, ramp 1→0 over `fade_out` seconds at the end. Used for
    headline cards rolling in/out, source-mix appearing, etc."""
    if t < 0 or t > dur:
        return 0.0
    if t < fade_in and fade_in > 0:
        return quart_out(t / fade_in)
    if t > dur - fade_out and fade_out > 0:
        return 1.0 - quart_in((t - (dur - fade_out)) / fade_out)
    return 1.0


def pulse(t: float, *, period: float = 1.6, amplitude: float = 0.06) -> float:
    """Subtle sinusoidal scale modulation around 1.0. Used for the
    mention-count callout so the number breathes without distracting."""
    return 1.0 + amplitude * math.sin(2 * math.pi * t / period)
