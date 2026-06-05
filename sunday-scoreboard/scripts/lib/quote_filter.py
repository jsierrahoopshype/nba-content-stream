"""Quote quality filtering + cleaning for v2.1.

The first render's "best quote" was @nba.com marketing copy
("KNICKS. SPURS. GAME DAY 🏀") — official accounts win raw engagement
every time and carry no editorial value. The text was also truncated
mid-word and littered with tofu boxes where emoji failed to render.

This module replaces pure engagement scoring with a filtered pick:

  HARD FILTERS (reject before scoring)
    1. Roster gate — only posts from handles in nba-content-stream's
       curated Bluesky reporter roster (data/sources/bluesky_handles.csv,
       ~164 reporters) are eligible. That roster IS the editorial
       filter; official/team/league accounts simply aren't in it. An
       explicit blocklist (data/quote_blocklist.json) is a belt-and-
       suspenders second gate.
    2. Length — drop posts shorter than 60 chars (no "GAME DAY"
       one-liners).
    3. Mostly emoji/caps — drop posts that are >50% emoji or uppercase.

  SCORE survivors with the unchanged engagement formula
  (engagement_score.best_quote).

  CLEAN for display — strip emoji and other non-renderable codepoints
  (Pillow + DM Sans render tofu otherwise) while preserving every
  Latin-1 / Latin-Extended character so French / Serbian / Turkish
  player and reporter names stay intact. Truncate at the last sentence
  (then word) boundary that fits — never mid-word.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import archive_client
from . import format_specs as fs
from .engagement_score import Engagement, at_uri_from_item, best_quote, quote_text
from .reporter_lookup import _handle_from_url

logger = logging.getLogger("quote_filter")

MIN_QUOTE_CHARS = 60
MAX_EMOJI_CAPS_RATIO = 0.5

ELLIPSIS = "…"

# Default blocklist path inside the sub-project.
BLOCKLIST_PATH = fs.REPO_ROOT / "data" / "quote_blocklist.json"

# Roster lives in the upstream archive; fetched via raw.githubusercontent.
ROSTER_PATH = "data/sources/bluesky_handles.csv"


# ---------------------------------------------------------------------------
# Emoji / unrenderable codepoint handling
# ---------------------------------------------------------------------------


def _is_emoji_or_symbol(cp: int) -> bool:
    """True for emoji, pictographs, dingbats, arrows, and variation
    selectors — the codepoints Pillow + DM Sans render as tofu. Crucially
    this returns False for all Latin-1 Supplement (U+00A0–U+00FF) and
    Latin Extended (U+0100–U+024F), so accented names survive."""
    return (
        0x1F000 <= cp <= 0x1FFFF   # emoji & pictographs (supplementary planes)
        or 0x2600 <= cp <= 0x27BF  # misc symbols + dingbats
        or 0x2300 <= cp <= 0x23FF  # misc technical (⏰ ⌛ …)
        or 0x2B00 <= cp <= 0x2BFF  # misc symbols & arrows
        or 0x2190 <= cp <= 0x21FF  # arrows
        or 0xFE00 <= cp <= 0xFE0F  # variation selectors
        or 0x1F1E6 <= cp <= 0x1F1FF  # regional indicators (flags)
        or cp in (0x200D, 0x20E3, 0x2122, 0x2139, 0x203C, 0x2049, 0x2728)
    )


def strip_unrenderable(text: str) -> str:
    """Remove emoji / symbol codepoints, preserving all Latin text."""
    return "".join(ch for ch in text if not _is_emoji_or_symbol(ord(ch)))


def collapse_spaces(text: str) -> str:
    """Collapse runs of whitespace to single spaces and trim."""
    return " ".join(text.split())


def clean_text(text: str) -> str:
    """Strip unrenderable codepoints then collapse whitespace."""
    return collapse_spaces(strip_unrenderable(text))


def emoji_caps_ratio(text: str) -> float:
    """Fraction of `text` (non-space chars) that is emoji/symbol or an
    uppercase letter. Used to reject shouty marketing one-liners."""
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    flagged = sum(
        1 for c in chars if _is_emoji_or_symbol(ord(c)) or (c.isalpha() and c.isupper())
    )
    return flagged / len(chars)


def is_mostly_emoji_or_caps(text: str) -> bool:
    return emoji_caps_ratio(text) > MAX_EMOJI_CAPS_RATIO


# ---------------------------------------------------------------------------
# Truncation — sentence boundary, then word boundary, never mid-word.
# ---------------------------------------------------------------------------


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate `text` to at most `max_chars` (excluding the appended
    ellipsis), cutting at the last sentence end (`.`/`!`/`?`) that fits.
    If no sentence boundary fits, cut at the last whole word. Never
    splits a word. Returns `text` unchanged if it already fits."""
    text = text.strip()
    if len(text) <= max_chars:
        return text

    window = text[:max_chars]
    # Prefer the last sentence-ending punctuation within the window.
    best_end = -1
    for i, ch in enumerate(window):
        if ch in ".!?":
            best_end = i
    if best_end >= 0:
        return window[: best_end + 1].strip()

    # No sentence boundary — fall back to the last whole word.
    if " " in window:
        trimmed = window[: window.rfind(" ")].rstrip()
    else:
        trimmed = window.rstrip()
    return (trimmed + ELLIPSIS) if trimmed else (window.rstrip() + ELLIPSIS)


def wrap_to_lines(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    max_lines: int,
) -> list[str]:
    """Word-wrap `text` into at most `max_lines` lines that each fit
    `max_width`, where `measure(s)` returns the pixel width of `s`.

    Never splits a word: overflow words are simply dropped (callers
    pre-truncate so this only trims trailing slack). `measure` is
    injected so this is unit-testable without a font.
    """
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        cand = w if not cur else f"{cur} {w}"
        if measure(cand) <= max_width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
                cur = w
            else:
                # A single word wider than the line — keep it whole on
                # its own line rather than chopping it.
                lines.append(w)
                cur = ""
            if len(lines) >= max_lines:
                return lines[:max_lines]
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def prepare_quote_lines(
    text: str,
    measure: Callable[[str], float],
    max_width: float,
    max_lines: int,
) -> list[str]:
    """Clean, fit, and (if needed) sentence-truncate `text` into display
    lines. Guarantees no mid-word cut: if the text overflows the line
    budget it's truncated at the last sentence/word boundary that fits,
    then re-wrapped."""
    text = clean_text(text)
    if not text:
        return []
    lines = wrap_to_lines(text, measure, max_width, max_lines)
    consumed = len(" ".join(lines))
    if consumed < len(text):
        truncated = truncate_at_sentence(text, consumed)
        lines = wrap_to_lines(truncated, measure, max_width, max_lines)
    return lines


# ---------------------------------------------------------------------------
# Roster + blocklist
# ---------------------------------------------------------------------------


def parse_roster(csv_text: str) -> set[str]:
    """Parse the `Handle,Display Name,DID` CSV into a lowercased set of
    handles. Tolerates a header row and blank lines."""
    handles: set[str] = set()
    for line in csv_text.splitlines():
        line = line.strip()
        if not line:
            continue
        handle = normalize_handle(line.split(",", 1)[0])
        if not handle or handle == "handle":  # skip header
            continue
        handles.add(handle)
    return handles


def load_roster(*, fetch: Callable[[str], Optional[bytes]] | None = None) -> set[str]:
    """Load the reporter roster from the upstream archive. Returns an
    empty set on failure (callers treat an empty roster as "gate off"
    so a roster outage degrades to scoring rather than zero quotes)."""
    fetch = fetch or (lambda url: archive_client.fetch_binary(url))
    url = f"{archive_client.RAW_BASE}/{ROSTER_PATH}"
    body = fetch(url)
    if not body:
        logger.warning("roster fetch failed; quote roster gate disabled this run")
        return set()
    try:
        return parse_roster(body.decode("utf-8"))
    except (UnicodeDecodeError, AttributeError) as exc:
        logger.warning("roster parse failed: %s", exc)
        return set()


def load_selfpromo_patterns(path: Path | None = None) -> list[str]:
    """Load the self-promo opening patterns (lowercased) from the
    blocklist config's `selfpromo_patterns` key. Missing → empty list."""
    path = path or BLOCKLIST_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("selfpromo patterns unreadable (%s): %s", path, exc)
        return []
    pats = data.get("selfpromo_patterns", []) if isinstance(data, dict) else []
    return [str(p).strip().lower() for p in pats if str(p).strip()]


def is_self_promo(text: str, patterns: Iterable[str], *, head_chars: int = 40) -> bool:
    """True if the opening of `text` is self-promotion ("wrote about",
    "my latest", "icymi", a leading bare URL, …) rather than an
    observation. Only the first ~`head_chars` of the cleaned text are
    inspected so a passing-mention of a link later doesn't trip it."""
    head = clean_text(text).lower().lstrip()
    if not head:
        return False
    if head.startswith(("http://", "https://", "www.")):
        return True
    head = head[:head_chars]
    return any(p in head for p in patterns)


def load_blocklist(path: Path | None = None) -> set[str]:
    """Load the explicit handle blocklist (lowercased). Missing file →
    empty set."""
    path = path or BLOCKLIST_PATH
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("blocklist unreadable (%s): %s", path, exc)
        return set()
    handles = data.get("handles", data) if isinstance(data, dict) else data
    return {normalize_handle(str(h)) for h in handles if normalize_handle(str(h))}


def is_blocked_handle(handle: str, blocklist: set[str]) -> bool:
    """True if `handle` is an official/blocked account: an exact match,
    a subdomain of a blocked domain, or an obvious league/team handle."""
    h = normalize_handle(handle)
    if not h:
        return True
    if h in blocklist:
        return True
    if any(h == b or h.endswith("." + b) for b in blocklist):
        return True
    # nba.com-style official handles regardless of explicit listing.
    return "nba.com" in h


# ---------------------------------------------------------------------------
# Filtering + selection
# ---------------------------------------------------------------------------


def normalize_handle(handle: str | None) -> str:
    """Normalize a handle for comparison: strip whitespace, drop a
    leading `@`, lowercase. Applied to BOTH the item handle and the
    roster/blocklist entries so the join can't drift on case or `@`."""
    if not handle:
        return ""
    return handle.strip().lstrip("@").strip().lower()


def _handle_of(item: dict) -> str:
    """Resolve a Bluesky item's handle, normalized.

    The archive stores the handle in different shapes depending on the
    file: the per-entity index files (what the pipeline reads) carry
    `author` as a *display-name string* with the handle only in the
    post `url`; the raw daily shards carry `author` as a dict with
    `author.handle`. We try, in order: a flat `author_handle`, then
    `author.handle`, then parse it out of the `url`
    (`bsky.app/profile/<handle>/post/…`). The DID lives in `id` but the
    handle is always recoverable from the url, so no DID→handle join is
    needed.
    """
    raw = item.get("author_handle")
    if not raw:
        author = item.get("author")
        if isinstance(author, dict):
            raw = author.get("handle")
    if not raw:
        raw = _handle_from_url(item.get("url") or "")
    return normalize_handle(raw)


def passes_filters(
    item: dict,
    *,
    roster: set[str],
    blocklist: set[str],
    min_chars: int = MIN_QUOTE_CHARS,
    selfpromo: Iterable[str] = (),
) -> bool:
    """Apply the v2.1/v2.2 hard filters to one candidate post."""
    handle = _handle_of(item)
    # Roster gate (only enforced when we actually have a roster).
    if roster and handle not in roster:
        return False
    if is_blocked_handle(handle, blocklist):
        return False
    return _quality_ok(item, min_chars, selfpromo)


def filter_candidates(
    candidates: Iterable[dict],
    *,
    roster: set[str],
    blocklist: set[str],
    min_chars: int = MIN_QUOTE_CHARS,
    selfpromo: Iterable[str] = (),
) -> list[dict]:
    return [
        it for it in candidates
        if passes_filters(
            it, roster=roster, blocklist=blocklist,
            min_chars=min_chars, selfpromo=selfpromo,
        )
    ]


def _quality_ok(item: dict, min_chars: int, selfpromo: Iterable[str] = ()) -> bool:
    raw = quote_text(item)
    if is_mostly_emoji_or_caps(raw):
        return False
    if selfpromo and is_self_promo(raw, selfpromo):
        return False
    return len(clean_text(raw)) >= min_chars


@dataclass
class QuoteStages:
    """Per-stage survivor counts for one player's quote pipeline, so a
    failure (like the 100% roster-gate rejection) is self-diagnosing in
    the render log instead of a silent `quote=—`."""

    candidates: int = 0
    after_roster: int = 0
    after_blocklist: int = 0
    after_quality: int = 0
    picked: bool = False
    picked_engagement: Optional[int] = None

    def log_line(self, slug: str) -> str:
        if self.picked:
            tail = (
                f"picked eng={self.picked_engagement}"
                if self.picked_engagement is not None
                else "picked (recency, no eng)"
            )
        else:
            tail = "picked none"
        return (
            f"quote pipeline for {slug}: {self.candidates} bsky candidates "
            f"→ roster {self.after_roster} → blocklist {self.after_blocklist} "
            f"→ length {self.after_quality} → {tail}"
        )


def select_quote_staged(
    candidates: list[dict],
    engagement_by_uri: dict[str, Engagement],
    *,
    roster: set[str],
    blocklist: set[str],
    min_chars: int = MIN_QUOTE_CHARS,
    selfpromo: Iterable[str] = (),
) -> tuple[Optional[tuple[dict, Optional[Engagement]]], QuoteStages]:
    """Like `select_quote`, but also returns per-stage survivor counts.

    Stages are applied in order — roster gate, blocklist, content
    quality (length + emoji/caps + self-promo) — counting survivors at
    each step, then the survivors are engagement-scored (recency
    fallback)."""
    cands = list(candidates)
    stages = QuoteStages(candidates=len(cands))

    after_roster = [it for it in cands if not roster or _handle_of(it) in roster]
    stages.after_roster = len(after_roster)

    after_block = [it for it in after_roster if not is_blocked_handle(_handle_of(it), blocklist)]
    stages.after_blocklist = len(after_block)

    survivors = [it for it in after_block if _quality_ok(it, min_chars, selfpromo)]
    stages.after_quality = len(survivors)

    chosen = best_quote(survivors, engagement_by_uri) if survivors else None
    if chosen is not None:
        stages.picked = True
        eng = chosen[1]
        stages.picked_engagement = eng.total if eng is not None else None
    return chosen, stages


def select_quote(
    candidates: list[dict],
    engagement_by_uri: dict[str, Engagement],
    *,
    roster: set[str],
    blocklist: set[str],
    min_chars: int = MIN_QUOTE_CHARS,
    selfpromo: Iterable[str] = (),
) -> Optional[tuple[dict, Optional[Engagement]]]:
    """Filter candidates through the quality gates, then pick the best
    survivor by engagement (recency fallback). Returns None when nothing
    survives — the renderer then shows the spotlight without a quote
    rather than airing marketing copy."""
    chosen, _ = select_quote_staged(
        candidates, engagement_by_uri,
        roster=roster, blocklist=blocklist, min_chars=min_chars, selfpromo=selfpromo,
    )
    return chosen
