"""Cluster archive items into beats.

A "beat" is a coherent storyline: same primary entity, items within
24h of each other. One entity can produce multiple beats if its
coverage spans multiple 24h islands (e.g., Wemby news on Monday and
again on Thursday). Each item attributes to every entity it tagged,
so a single article naming Wemby AND the Spurs contributes to both
their beats.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable


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


@dataclass
class Beat:
    entity: str
    entity_kind: str                # "player" | "team"
    start: datetime                 # earliest published_at in cluster
    end: datetime                   # latest published_at in cluster
    items: list[dict] = field(default_factory=list)

    @property
    def mention_count(self) -> int:
        return len(self.items)

    @property
    def time_span_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def source_mix(self) -> dict[str, int]:
        mix: dict[str, int] = defaultdict(int)
        for it in self.items:
            src = it.get("source") or "unknown"
            mix[src] += 1
        return dict(mix)

    @property
    def source_count(self) -> int:
        return len(self.source_mix)


def _iter_entity_tags(item: dict) -> Iterable[tuple[str, str]]:
    """Yield (slug, kind) for every player + team this item tagged.

    A single item can produce multiple (entity, kind) pairs — that's
    intentional: when an article names both Wemby and the Spurs we
    want it to feed both beats so each cluster reflects the real
    spread of coverage, not whichever entity the indexer happened to
    list first.
    """
    for slug in item.get("players") or []:
        yield slug, "player"
    for slug in item.get("teams") or []:
        yield slug, "team"


def cluster_beats(
    items: list[dict],
    *,
    window_hours: int = 24,
) -> list[Beat]:
    """Group `items` into beats. Returns one Beat per (entity, 24h
    island) pair.

    Algorithm: bucket items by entity, sort each bucket by
    `published_at`, walk forward opening a new Beat whenever the gap
    from the current beat's `start` exceeds `window_hours`. The
    24-hour window measures from the cluster's first item (not its
    most-recent), so a steady drip of one mention every 23h all roll
    into the same beat — matches the brief's "convergence over
    24h" framing.
    """
    by_entity: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for it in items:
        ts = _parse_iso(it.get("published_at", ""))
        if ts is None:
            continue
        for slug, kind in _iter_entity_tags(it):
            by_entity[(slug, kind)].append((ts, it))

    window = timedelta(hours=window_hours)
    beats: list[Beat] = []
    for (slug, kind), tagged in by_entity.items():
        tagged.sort(key=lambda pair: pair[0])
        current: Beat | None = None
        for ts, it in tagged:
            if current is None or (ts - current.start) > window:
                current = Beat(
                    entity=slug, entity_kind=kind, start=ts, end=ts, items=[it]
                )
                beats.append(current)
            else:
                current.items.append(it)
                if ts > current.end:
                    current.end = ts
    return beats
