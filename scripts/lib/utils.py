"""Small helpers shared by all pollers.

Date helpers: every shard field that holds a timestamp is ISO 8601 UTC
with a `Z` suffix (e.g. `2026-05-21T15:30:00Z`). `parse_to_iso`
normalizes whatever a source hands us (RSS pubDate strings, ISO strings
with offsets, naive datetimes) into that single canonical form.

HTML helper: `strip_html` removes tags, decodes entities, and collapses
whitespace. Used by RSS pollers that need the plaintext of an HTML
description/content payload.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html import unescape
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


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace to single spaces.

    Cheap regex-based — not a full HTML parser. Good enough for RSS
    description/content payloads, which typically only use a small set
    of inline tags (a, p, br, em, strong, blockquote). The output is
    safe to embed in a JSON shard as `body_excerpt`.

    Returns an empty string for None or empty input.
    """
    if not text:
        return ""
    no_tags = _TAG_RE.sub(" ", text)
    decoded = unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()
