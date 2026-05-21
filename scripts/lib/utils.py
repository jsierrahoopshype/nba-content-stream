"""Small datetime helpers shared by all pollers.

Every shard field that holds a timestamp is ISO 8601 UTC with a `Z` suffix
(e.g. `2026-05-21T15:30:00Z`). These helpers normalize whatever a source
hands us (RSS pubDate strings, ISO strings with offsets, naive datetimes)
into that single canonical form.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Union

from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

DateInput = Union[str, datetime]


def utc_now_iso() -> str:
    """Return the current UTC time formatted as ISO 8601 with a Z suffix."""
    return datetime.now(timezone.utc).strftime(_ISO_FORMAT)


def today_utc_date() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_to_iso(value: DateInput) -> str:
    """Parse any reasonable date input into the canonical ISO 8601 UTC form.

    Accepts:
      - `datetime` instances (naive treated as UTC, aware converted to UTC)
      - ISO 8601 strings, with or without timezone offset
      - RSS pubDate strings (RFC 822, e.g. "Wed, 21 May 2026 14:30:00 +0000")
      - Any other string `dateutil.parser` can handle

    Naive datetimes (no tzinfo) are assumed to already be UTC. This matches
    what most upstream sources emit when they omit a timezone.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = dateutil_parser.parse(str(value))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.strftime(_ISO_FORMAT)
