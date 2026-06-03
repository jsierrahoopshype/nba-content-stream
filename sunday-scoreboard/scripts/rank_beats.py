"""Rank clustered beats and select the top N for the recap.

Brief edge case: a single-item beat (one mention, no convergence) is
allowed only if it has 3+ distinct sources contributing — i.e., the
"convergence" came from independent verification rather than
multiple posts from the same reporter. In practice no single archive
item carries multiple source values, so a 1-item beat will always
fail the 3-source test and we drop it. The rule still matters once
we start coalescing items into bundles in a future iteration.
"""

from __future__ import annotations

from typing import Iterable

from cluster_beats import Beat


def filter_noise(beats: Iterable[Beat], *, min_sources_for_single: int = 3) -> list[Beat]:
    """Drop low-signal beats:
      - 0 items (defensive; shouldn't happen)
      - 1 item with fewer than `min_sources_for_single` sources
    """
    out: list[Beat] = []
    for b in beats:
        if b.mention_count == 0:
            continue
        if b.mention_count == 1 and b.source_count < min_sources_for_single:
            continue
        out.append(b)
    return out


def rank_beats(beats: Iterable[Beat], *, top_n: int = 10) -> list[Beat]:
    """Order beats by mention count (desc), break ties by source
    diversity (more sources = higher), then by recency of the cluster
    end. Truncate to `top_n`."""
    sorted_beats = sorted(
        beats,
        key=lambda b: (-b.mention_count, -b.source_count, -b.end.timestamp()),
    )
    return sorted_beats[:top_n]


def rank_and_filter(beats: Iterable[Beat], *, top_n: int = 10) -> list[Beat]:
    """Convenience: filter noise then rank. Returns ranks 1..N."""
    filtered = filter_noise(beats)
    return rank_beats(filtered, top_n=top_n)
