"""Canonical player and team vocabulary, plus entity detection.

Loads `data/canonical/players.json` and `data/canonical/teams.json` and
exposes a `detect_entities` function that scans free text for mentions
of canonical names and aliases and returns the matching slugs.

Detection rules:
  - Case-insensitive, word-boundary regex match.
  - Matches both canonical display names and aliases.
  - Also matches a player's last name (derived from the display name),
    which is how we catch things like "Murray" without the first name.
  - When a match could mean more than one player (most commonly a shared
    last name like "Murray"), we disambiguate by checking whether exactly
    one of the candidates' teams is also mentioned in the same text. If
    that doesn't narrow it down to one, the match is dropped rather
    than guessed.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# Repo root is two parents up from this file: scripts/lib/canonical.py -> repo
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_DIR = REPO_ROOT / "data" / "canonical"

CanonicalDict = Dict[str, Dict[str, object]]


def _strip_meta(blob: dict) -> CanonicalDict:
    """Drop the leading `_meta` block; return only real entries."""
    return {k: v for k, v in blob.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def load_canonical() -> Tuple[CanonicalDict, CanonicalDict]:
    """Load players.json and teams.json once and cache the result.

    Returns a tuple `(players, teams)` where each is a dict mapping slug
    to its record (without the `_meta` entry).
    """
    players_path = CANONICAL_DIR / "players.json"
    teams_path = CANONICAL_DIR / "teams.json"
    with players_path.open(encoding="utf-8") as f:
        players_blob = json.load(f)
    with teams_path.open(encoding="utf-8") as f:
        teams_blob = json.load(f)
    return _strip_meta(players_blob), _strip_meta(teams_blob)


def _last_name(display_name: str) -> str:
    """Return the last whitespace-separated token of a display name."""
    return display_name.split()[-1]


def _compile_phrase(phrase: str) -> re.Pattern:
    """Word-boundary, case-insensitive regex for the literal phrase."""
    return re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)


# Memoize the combined-regex + index per (canonical dict identity, include_last_name).
# detect_entities runs once per shard item; without this cache the
# 7K-item backfill in build_indexes compiled ~1500 regexes per item,
# which made the index step take 2.5 minutes. Cached, the same step
# runs in seconds.
_COMBINED_CACHE: Dict[Tuple[int, bool], Tuple[Dict[str, List[str]], "re.Pattern"]] = {}


def _combined_pattern(
    canonical: CanonicalDict, include_last_name: bool
) -> Tuple[Dict[str, List[str]], "re.Pattern"]:
    key = (id(canonical), include_last_name)
    cached = _COMBINED_CACHE.get(key)
    if cached is not None:
        return cached
    index = _build_candidate_index(canonical, include_last_name)
    # Sort longest-first so "LeBron James" wins over "James" when both
    # are in the index. re.finditer with alternation matches leftmost
    # first, then tries the next alternation at the next position;
    # longest-first ordering keeps multi-word names from being
    # shadowed by a shorter overlap.
    phrases = sorted(index.keys(), key=len, reverse=True)
    if phrases:
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b",
            re.IGNORECASE,
        )
    else:
        # No phrases — match nothing. Use a regex that never matches.
        pattern = re.compile(r"(?!)")
    _COMBINED_CACHE[key] = (index, pattern)
    return index, pattern


def _punct_variants(phrase: str) -> List[str]:
    """Generate punctuation-tolerant variants of a name phrase.

    Real-world posts drop apostrophes ("DeAaron" for "De'Aaron"),
    swap hyphens for spaces ("Trayce Jackson Davis" for "Trayce
    Jackson-Davis"), and elide periods ("DJ" for "D.J."). We can't
    rewrite the canonical to all of these, so the tagger indexes
    each variant as an additional phrase pointing at the same slug.
    """
    variants = {phrase}
    # Strip apostrophes entirely ("De'Aaron Fox" -> "DeAaron Fox").
    no_apos = phrase.replace("'", "").replace("’", "")
    if no_apos != phrase:
        variants.add(no_apos)
    # Hyphens → spaces ("Trayce Jackson-Davis" -> "Trayce Jackson Davis").
    no_hyph = phrase.replace("-", " ")
    if no_hyph != phrase:
        variants.add(no_hyph)
    # Combined: drop apostrophes AND swap hyphens.
    combined = no_apos.replace("-", " ")
    if combined != phrase and combined != no_apos and combined != no_hyph:
        variants.add(combined)
    # Periods elided ("D.J. Carton" -> "DJ Carton").
    no_period = phrase.replace(".", "")
    if no_period != phrase:
        variants.add(no_period)
    # Collapse any double spaces introduced by hyphen→space.
    return [re.sub(r"\s+", " ", v).strip() for v in variants if v.strip()]


def _build_candidate_index(
    canonical: CanonicalDict,
    include_last_name: bool,
) -> Dict[str, List[str]]:
    """Map lowercased phrase -> list of slugs that phrase could refer to.

    Same phrase can resolve to multiple slugs (e.g. "JB" -> jalen-brunson
    and jaylen-brown). Callers use the list length to detect ambiguity.

    For each canonical name and alias, we also index punctuation-
    tolerant variants (apostrophe-stripped, hyphen-as-space, period-
    elided) so casual spellings still tag. "DeAaron Fox" maps to
    de'aaron-fox; "Trayce Jackson Davis" to trayce-jackson-davis.
    """
    index: Dict[str, List[str]] = {}
    for slug, info in canonical.items():
        phrases: List[str] = [str(info["name"])]
        phrases.extend(str(a) for a in info.get("aliases", []) or [])
        if include_last_name:
            last = _last_name(str(info["name"]))
            phrases.append(last)
        # Expand each phrase into its punctuation variants.
        expanded = []
        for p in phrases:
            if p:
                expanded.extend(_punct_variants(p))
        for phrase in expanded:
            if not phrase:
                continue
            key = phrase.lower()
            slugs = index.setdefault(key, [])
            if slug not in slugs:
                slugs.append(slug)
    return index


def _detect_teams(text: str, teams_dict: CanonicalDict) -> List[str]:
    """Return sorted list of team slugs mentioned in `text`."""
    found: set[str] = set()
    index, pattern = _combined_pattern(teams_dict, include_last_name=False)
    for m in pattern.finditer(text):
        slugs = index.get(m.group(0).lower())
        if not slugs:
            continue
        for slug in slugs:
            found.add(slug)
    return sorted(found)


def _detect_players(
    text: str,
    players_dict: CanonicalDict,
    detected_team_slugs: List[str],
) -> List[str]:
    """Return sorted list of player slugs mentioned in `text`.

    Ambiguous matches (phrase resolves to multiple players) are kept only
    when team context narrows them to exactly one candidate.
    """
    detected_teams_set = set(detected_team_slugs)
    found: set[str] = set()
    # include_last_name=False: with a 500+ player pool, bare last names
    # collide too often (Mitchell, Murray, Williams, Thompson, Brown,
    # Jackson, ...). The old team-context disambiguator handled the
    # tail of collisions but produced false positives when a post
    # mentioned a team without naming the player. Match only on full
    # names, display names, and explicitly curated short-form aliases.
    index, pattern = _combined_pattern(players_dict, include_last_name=False)
    # Walk the matches found by the single combined regex (≈10-100x
    # faster than compiling N regexes per text on a 500-player canon).
    # Track each matched phrase so we resolve the same phrase only once.
    seen_phrases: set[str] = set()
    for m in pattern.finditer(text):
        phrase = m.group(0).lower()
        if phrase in seen_phrases:
            continue
        seen_phrases.add(phrase)
        slugs = index.get(phrase)
        if not slugs:
            continue
        if len(slugs) == 1:
            found.add(slugs[0])
            continue
        # Ambiguous phrase. Keep candidates whose team is mentioned.
        narrowed = [
            slug
            for slug in slugs
            if players_dict[slug].get("team") in detected_teams_set
        ]
        if len(narrowed) == 1:
            found.add(narrowed[0])
        else:
            logger.debug(
                "skipping ambiguous match for %r: candidates=%s narrowed=%s",
                phrase,
                slugs,
                narrowed,
            )
    return sorted(found)


def detect_entities(
    text: str,
    players_dict: CanonicalDict,
    teams_dict: CanonicalDict,
) -> Tuple[List[str], List[str]]:
    """Detect canonical players and teams mentioned in `text`.

    Returns `(player_slugs, team_slugs)`, each a deduplicated, sorted list.
    """
    if not text:
        return [], []
    team_slugs = _detect_teams(text, teams_dict)
    player_slugs = _detect_players(text, players_dict, team_slugs)
    return player_slugs, team_slugs
