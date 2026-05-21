"""Read and write daily JSON shards per docs/SHARD_FORMAT.md.

A shard is one source's items for one UTC day, stored at
`data/{source}/{YYYY-MM-DD}.json`. Items are ordered ascending by
`published_at`. Pollers append idempotently via `append_items`, which
dedupes against existing item ids before writing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List

from .utils import utc_now_iso

logger = logging.getLogger(__name__)

# Repo root is two parents up: scripts/lib/shards.py -> repo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Tests may monkeypatch this module-level attribute to redirect writes.
DATA_DIR = REPO_ROOT / "data"

SOURCE_PREFIXES: Dict[str, str] = {
    "youtube": "yt-",
    "substack": "ss-",
    "bluesky": "bs-",
    "reddit": "rd-",
    "google-news": "gn-",
}

REQUIRED_ITEM_FIELDS = (
    "id",
    "source",
    "published_at",
    "ingested_at",
    "url",
    "title",
    "author",
    "players",
    "teams",
)


def shard_path(source: str, date: str) -> Path:
    """Return the path to the shard file for the given source and UTC date."""
    return DATA_DIR / source / f"{date}.json"


def _fresh_envelope(source: str, date: str) -> dict:
    return {
        "date": date,
        "source": source,
        "generated_at": utc_now_iso(),
        "items": [],
    }


def load_shard(source: str, date: str) -> dict:
    """Load an existing shard, or return a fresh envelope if none exists."""
    path = shard_path(source, date)
    if not path.exists():
        return _fresh_envelope(source, date)
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_shard(source: str, date: str, shard: dict) -> None:
    """Write the shard to disk, creating parent directories as needed.

    Refreshes `generated_at` to the current UTC time on every write.
    """
    path = shard_path(source, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    shard["generated_at"] = utc_now_iso()
    with path.open("w", encoding="utf-8") as f:
        json.dump(shard, f, indent=2, sort_keys=False, ensure_ascii=False)
        f.write("\n")


def append_items(source: str, date: str, new_items: List[dict]) -> int:
    """Append new items to a shard, deduping by id. Returns count appended.

    The merged item list is sorted ascending by `published_at` before write.
    Items whose id already exists in the shard are skipped silently.
    """
    shard = load_shard(source, date)
    existing_ids = {item.get("id") for item in shard.get("items", [])}
    appended = 0
    for item in new_items:
        item_id = item.get("id")
        if item_id is None or item_id in existing_ids:
            continue
        shard["items"].append(item)
        existing_ids.add(item_id)
        appended += 1
    shard["items"].sort(key=lambda it: it.get("published_at", ""))
    save_shard(source, date, shard)
    return appended


def validate_item(item: dict) -> List[str]:
    """Return a list of validation errors for `item`. Empty list means valid.

    Checks required fields per SHARD_FORMAT.md, the source enum, the id
    prefix matching the source, and the basic shape of `author`,
    `players`, and `teams`.
    """
    errors: List[str] = []

    if not isinstance(item, dict):
        return ["item must be a dict"]

    for field in REQUIRED_ITEM_FIELDS:
        if field not in item:
            errors.append(f"missing required field: {field}")

    source = item.get("source")
    if source is not None and source not in SOURCE_PREFIXES:
        errors.append(f"unknown source: {source!r}")

    item_id = item.get("id")
    if isinstance(item_id, str) and source in SOURCE_PREFIXES:
        prefix = SOURCE_PREFIXES[source]
        if not item_id.startswith(prefix):
            errors.append(
                f"id {item_id!r} must start with {prefix!r} for source {source!r}"
            )
    elif "id" in item and not isinstance(item_id, str):
        errors.append("id must be a string")

    author = item.get("author")
    if "author" in item:
        if not isinstance(author, dict):
            errors.append("author must be an object")
        else:
            for sub in ("handle", "display_name"):
                if sub not in author:
                    errors.append(f"author missing required field: {sub}")

    for list_field in ("players", "teams"):
        if list_field in item and not isinstance(item[list_field], list):
            errors.append(f"{list_field} must be a list")

    return errors
