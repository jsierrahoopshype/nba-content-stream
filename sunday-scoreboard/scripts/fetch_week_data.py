"""Pull last-7-days items from the archive.

Strategy: read the manifest, walk every player + team index, dedupe
on item id, filter to the week's window. The per-entity index files
already carry up to 1000 items each within a 30-day rolling window,
so a manifest-driven walk gives us complete coverage of every entity
that surfaced in the week.

CLI:
  python scripts/fetch_week_data.py --week-of 2026-05-25
  python scripts/fetch_week_data.py --week-of 2026-05-25 --out /tmp/wk.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib import archive_client

logger = logging.getLogger("fetch_week_data")


@dataclass
class WeekRange:
    """The 7-day window. `week_of` is the Sunday that opens the week
    (00:00 UTC); `end` is the next Sunday (exclusive)."""

    week_of: datetime
    end: datetime

    @classmethod
    def from_date_str(cls, s: str) -> "WeekRange":
        d = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return cls(week_of=d, end=d + timedelta(days=7))

    def contains(self, iso: str) -> bool:
        if not iso:
            return False
        try:
            # Tolerate "Z" suffix and missing fractional seconds.
            ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return self.week_of <= ts < self.end


def gather_week(week: WeekRange) -> list[dict]:
    """Return every item from the archive that falls in `week`,
    deduped by item id. An item that surfaces in multiple entity
    indexes (e.g., a single article tags both Wemby and Spurs) is
    returned once and carries its full `players` + `teams` tag list,
    so downstream clustering attributes it to every entity it
    touched."""
    manifest = archive_client.fetch_manifest()
    if not manifest:
        logger.error("manifest fetch failed; cannot enumerate entity indexes")
        return []

    seen_ids: set[str] = set()
    out: list[dict] = []

    def _ingest(entity_index: dict | None) -> None:
        if not entity_index:
            return
        for it in entity_index.get("items", []) or []:
            iid = it.get("id")
            if not iid or iid in seen_ids:
                continue
            if not week.contains(it.get("published_at", "")):
                continue
            seen_ids.add(iid)
            out.append(it)

    for row in manifest.get("players", []) or []:
        _ingest(archive_client.fetch_player_index(row["slug"]))
    for row in manifest.get("teams", []) or []:
        _ingest(archive_client.fetch_team_index(row["slug"]))

    logger.info(
        "fetched %d unique items across %d players + %d teams for week %s",
        len(out),
        len(manifest.get("players", [])),
        len(manifest.get("teams", [])),
        week.week_of.date(),
    )
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch nba-content-stream items for a 7-day window."
    )
    p.add_argument(
        "--week-of",
        required=True,
        help="Sunday that opens the week, YYYY-MM-DD (UTC).",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Write the JSON list to this path; otherwise print to stdout.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    week = WeekRange.from_date_str(args.week_of)
    items = gather_week(week)
    payload = {"week_of": args.week_of, "items": items}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("wrote %s items to %s", len(items), args.out)
    else:
        print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(run())
