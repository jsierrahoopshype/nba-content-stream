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


def _build_candidate_index(
    canonical: CanonicalDict,
    include_last_name: bool,
) -> Dict[str, List[str]]:
    """Map lowercased phrase -> list of slugs that phrase could refer to.

    Same phrase can resolve to multiple slugs (e.g. "JB" -> jalen-brunson
    and jaylen-brown). Callers use the list length to detect ambiguity.
    """
    index: Dict[str, List[str]] = {}
    for slug, info in canonical.items():
        phrases: List[str] = [str(info["name"])]
        phrases.extend(str(a) for a in info.get("aliases", []) or [])
        if include_last_name:
            last = _last_name(str(info["name"]))
            phrases.append(last)
        for phrase in phrases:
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
    index = _build_candidate_index(teams_dict, include_last_name=False)
    for phrase, slugs in index.items():
        if _compile_phrase(phrase).search(text):
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
    index = _build_candidate_index(players_dict, include_last_name=True)
    for phrase, slugs in index.items():
        if not _compile_phrase(phrase).search(text):
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
