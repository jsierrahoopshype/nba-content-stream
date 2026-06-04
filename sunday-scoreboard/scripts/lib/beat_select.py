"""Beat selection rules for v2.1.

Two corrections from the first real-render review:

* **Players only** — team entities are never eligible as beats. A team
  still appears as *context* (the player's team name on the hero card),
  but the recap is a player countdown, so team beats are dropped before
  ranking.
* **One beat per player** — the 24h-window clustering can split a
  single news arc into two beats for the same player (the first render
  had the Knicks at #1 *and* #3 with identical sparklines). After
  ranking we keep only each player's single highest-mention beat so the
  top-N is N distinct players.

Pure functions over `cluster_beats.Beat`; no rendering or network. v1's
shared `rank_beats` is left untouched — these compose around it.
"""

from __future__ import annotations

from typing import Iterable

from cluster_beats import Beat


def players_only(beats: Iterable[Beat]) -> list[Beat]:
    """Drop every non-player beat. Teams surface as hero-card context,
    never as their own beat."""
    return [b for b in beats if b.entity_kind == "player"]


def one_beat_per_player(beats: Iterable[Beat]) -> list[Beat]:
    """Collapse to one beat per player, keeping each player's highest-
    signal beat and preserving the input order otherwise.

    Expects `beats` already ranked (mentions desc, then source
    diversity, then recency — i.e. `rank_beats` order), so the *first*
    beat seen for a player is the one to keep. Order of first
    appearances is preserved, so the deduped list stays ranked.
    """
    seen: set[str] = set()
    out: list[Beat] = []
    for b in beats:
        if b.entity in seen:
            continue
        seen.add(b.entity)
        out.append(b)
    return out
