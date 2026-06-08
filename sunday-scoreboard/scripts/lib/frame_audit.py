"""Dead-air auditing for the v2.2 retention rules (test-only helper).

The social-edit pass forbids dead air: no frame may be mostly empty
background. `background_fraction` measures how much of a frame is within
tolerance of the brand background color; `max_background_fraction`
samples a clip at fixed intervals and returns the worst (emptiest)
frame. Tests assert the worst sampled frame stays under a threshold so
a future change that reintroduces dead air fails CI automatically.
"""

from __future__ import annotations

from . import format_specs as fs


def background_fraction(frame, bg_rgb: tuple[int, int, int] | None = None, *, tol: int = 7) -> float:
    """Fraction (0→1) of `frame` pixels within `tol` (per-channel L1
    average) of `bg_rgb`. `frame` is an HxWx3 uint8 array (RGB)."""
    import numpy as np

    if bg_rgb is None:
        bg_rgb = fs.hex_to_rgb(fs.BACKGROUND)
    arr = np.asarray(frame).astype(np.int16)
    diff = np.abs(arr - np.array(bg_rgb, dtype=np.int16)).mean(axis=2)
    return float((diff <= tol).mean())


def max_background_fraction(
    clip,
    *,
    interval: float = 1.0,
    bg_rgb: tuple[int, int, int] | None = None,
    tol: int = 7,
) -> tuple[float, float]:
    """Sample `clip` every `interval` seconds; return `(worst_fraction,
    worst_t)` — the emptiest sampled frame and when it occurs."""
    worst = 0.0
    worst_t = 0.0
    t = 0.0
    # Sample inclusive of a near-end frame so the last phase is covered.
    while t < clip.duration:
        frac = background_fraction(clip.get_frame(t), bg_rgb, tol=tol)
        if frac > worst:
            worst, worst_t = frac, t
        t += interval
    return worst, worst_t


def region_content_pixels(
    frame,
    region: tuple[int, int, int, int],
    *,
    bg_rgb: tuple[int, int, int] | None = None,
    ignore: tuple[tuple[int, int, int], ...] = (),
    tol: int = 7,
) -> int:
    """Count pixels inside `region` (x0,y0,x1,y1) that are NOT the
    background AND not any `ignore` color (e.g. a flat card fill). Used
    to detect an empty chart sitting inside a colored card — which the
    plain background auditor misses."""
    import numpy as np

    if bg_rgb is None:
        bg_rgb = fs.hex_to_rgb(fs.BACKGROUND)
    x0, y0, x1, y1 = region
    arr = np.asarray(frame).astype(np.int16)[y0:y1, x0:x1]
    mask = np.abs(arr - np.array(bg_rgb, dtype=np.int16)).mean(axis=2) > tol
    for col in ignore:
        mask &= np.abs(arr - np.array(col, dtype=np.int16)).mean(axis=2) > tol
    return int(mask.sum())


def assert_chart_drawn(
    frame,
    region: tuple[int, int, int, int],
    *,
    min_pixels: int,
    ignore: tuple[tuple[int, int, int], ...] = (),
    label: str = "chart",
) -> None:
    """Fail if `region` has fewer than `min_pixels` content pixels —
    i.e. the chart (line/fill/markers) didn't actually draw inside its
    card."""
    n = region_content_pixels(frame, region, ignore=ignore)
    assert n >= min_pixels, (
        f"{label}: only {n} content px in {region} (min {min_pixels}) — "
        f"chart appears empty"
    )


def assert_no_dead_air(clip, *, threshold: float = 0.85, interval: float = 1.0, label: str = "clip") -> None:
    """Raise AssertionError if any sampled frame is more than `threshold`
    background. Hard rule: no frame >85% empty."""
    worst, worst_t = max_background_fraction(clip, interval=interval)
    assert worst <= threshold, (
        f"dead air in {label}: frame at t={worst_t:.1f}s is "
        f"{worst:.0%} background (limit {threshold:.0%})"
    )
