"""Regenerate data/canonical/players.json from the nba-headshots active list.

Reads players/metadata/active.json from the jsierrahoopshype/nba-headshots
repo (cached locally during build) and the existing teams.json (for the
abbreviation → team-slug crosswalk), and emits a 500+ player canonical
keyed by slug. Each entry carries:

    {
        "name": full display name,
        "full_name": same,
        "first_name": ...,
        "last_name": ...,
        "team": <my-team-slug>,           # current team
        "nba_id": <NBA.com ID>,           # for the headshot URL
        "aliases": [full_name, ...legacy short-forms]
    }

Legacy short-forms (Wemby, SGA, JB, KD, PG, ...) are merged from the
PREVIOUS players.json when present, so manually-curated unambiguous
nicknames survive the rebuild.

Bare last names are NOT included as aliases. With 532 players many
last names collide (Mitchell, Murray, Williams, Thompson, etc.); the
tagger's ambiguity-drop would silently swallow them anyway, and a
common English collision (Wilson, Brown, Hill) would cause false
positives. The tagger's _build_candidate_index also stops generating
implicit last-name candidates in this PR.

Run from repo root:
    python scripts/build_players_canonical.py
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional


def _ascii_slug(name: str) -> str:
    """NFKD-normalize and strip diacritics, then slugify.

    The upstream nba-headshots manifest has 20 slugs with un-folded
    diacritics (e.g. `nikola-joki` instead of `nikola-jokic`) — our
    canonical needs proper ASCII slugs for the URL path
    (players/nikola-jokic.html) and the tagger's display-name match.
    """
    norm = unicodedata.normalize("NFKD", name)
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    norm = norm.lower().replace("'", "").replace(".", "")
    norm = re.sub(r"[^a-z0-9 -]", "", norm)
    return re.sub(r"\s+", "-", norm.strip())

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_DIR = REPO_ROOT / "data" / "canonical"
PLAYERS_OUT = CANONICAL_DIR / "players.json"
TEAMS_PATH = CANONICAL_DIR / "teams.json"
# Additive manual overrides (e.g. incoming draft class) that nba-headshots'
# active.json hasn't picked up yet. Merged in AFTER the active-derived
# build so hand-added entries survive the full regeneration. See
# apply_overrides for the merge rule.
OVERRIDES_PATH = CANONICAL_DIR / "player_overrides.json"

# Default input is the fetched-and-stashed copy of active.json. Override
# with a path argument when running locally to use a fresh download.
DEFAULT_ACTIVE_INPUT = Path("/tmp/nbah-active.json")


def _abbr_to_slug(teams: dict) -> dict:
    out = {}
    for slug, info in teams.items():
        if slug.startswith("_"):
            continue
        abbr = info.get("abbr")
        if abbr:
            out[abbr.upper()] = slug
    return out


def _existing_aliases(prev_players: dict) -> dict:
    """Map slug -> list of legacy short-form aliases that should survive."""
    out = {}
    # The PREVIOUS canonical had things like ["Wemby", "SGA", "KAT", "JB",
    # "KD", "PG"] curated as nicknames. We want to keep those even when
    # the new larger canonical comes from automated active.json — they
    # are still single-player matches in most cases (the tagger will
    # drop any that turn ambiguous in the larger pool).
    for slug, info in prev_players.items():
        if slug.startswith("_"):
            continue
        last_name = (info.get("name") or "").split()[-1] if info.get("name") else ""
        keep = []
        for a in info.get("aliases", []) or []:
            # Skip the bare last name (would be added implicitly under the
            # OLD tagger; we want to drop that everywhere now).
            if a == last_name:
                continue
            # Skip if it duplicates the display name (canonical already has it).
            if a == info.get("name"):
                continue
            keep.append(a)
        if keep:
            out[slug] = keep
    return out


def apply_overrides(out: dict, overrides: dict) -> tuple[int, int]:
    """Merge additive `overrides` into the active-derived canonical `out`.

    players.json is fully regenerated from active.json each run, so any
    manually-added player would be wiped without this step. Merge rule:

      * slug NOT yet in `out` — nba-headshots hasn't caught up with this
        player (real nba_id / headshot still pending): add the override
        entry as-is (nba_id / headshot_filename stay null until the real
        pipeline fills them in).
      * slug ALREADY in `out` — active.json now carries the real record:
        the active-derived entry WINS (real nba_id / headshot / team), but
        any override `aliases` not already present are merged in so the
        curated alias work isn't lost when the real data arrives.

    Mutates `out` in place. Idempotent: re-running against the same inputs
    produces the same result (no duplicate entries, no duplicate aliases).
    Returns `(added_as_is, superseded_by_active)`.
    """
    added = 0
    superseded = 0
    for slug, entry in overrides.items():
        if slug.startswith("_"):
            continue
        if slug not in out:
            out[slug] = entry
            added += 1
        else:
            existing = out[slug].get("aliases") or []
            merged = list(existing)
            for alias in entry.get("aliases", []) or []:
                if alias not in merged:
                    merged.append(alias)
            out[slug]["aliases"] = merged
            superseded += 1
    return added, superseded


def build(active_path: Path = DEFAULT_ACTIVE_INPUT) -> dict:
    teams = json.load(TEAMS_PATH.open(encoding="utf-8"))
    abbr_to_slug = _abbr_to_slug(teams)

    prev_players: dict = {}
    if PLAYERS_OUT.exists():
        prev_players = json.load(PLAYERS_OUT.open(encoding="utf-8"))
    legacy_aliases = _existing_aliases(prev_players)

    active = json.load(active_path.open(encoding="utf-8"))
    src_players = active.get("players", [])

    out: dict = {
        "_meta": {
            "description": (
                "Canonical NBA player list. Generated from "
                "jsierrahoopshype/nba-headshots active.json (the project's "
                "scraped + verified NBA.com active-roster manifest). The "
                "slug, full_name, team_id, and team_abbrev fields come "
                "from there. team is mapped from team_abbrev to our slug "
                "via teams.json. aliases preserve any curated short-form "
                "nicknames (Wemby, SGA, KD, JB, ...) from the previous "
                "canonical; bare last names are NOT included because they "
                "collide too often in a 500+ player pool — the tagger's "
                "_build_candidate_index also stops adding implicit "
                "last-name candidates as part of this PR."
            ),
            "source": "https://github.com/jsierrahoopshype/nba-headshots",
            "source_file": "players/metadata/active.json",
            "source_generated_at": active.get("generated_at"),
            "total_players": len(src_players),
            "version": "2",
        },
    }
    for p in src_players:
        # Upstream slugs like 'nikola-joki' (from Nikola Jokić) skipped
        # the diacritic-folding step; we recompute the slug here so
        # players/nikola-jokic.html and detect_entities("Nikola Jokić")
        # both work correctly.
        slug = _ascii_slug(p["full_name"])
        team = abbr_to_slug.get((p.get("team_abbrev") or "").upper())
        if not team:
            # Should never trigger — recon confirmed all abbrevs map.
            print(f"WARNING: no team slug for abbrev {p.get('team_abbrev')!r} (player {slug})", file=sys.stderr)
            continue
        # Aliases lookup is keyed by upstream's slug (which is what the
        # previous canonical used) — so when the PREVIOUS canonical was
        # generated, even the broken slugs had their legacy aliases
        # attached to them.
        aliases = list(legacy_aliases.get(p["slug"], []))
        if slug != p["slug"]:
            aliases = list(legacy_aliases.get(slug, [])) or aliases
        # If the canonical name carries diacritics (Jokić, Dončić,
        # Schröder, ...), also accept the ASCII-folded variant
        # (Jokic, Doncic, Schroder) as an alias. Most public-source
        # text drops the diacritics; without this, we'd silently
        # mis-tag a third of European players.
        folded = unicodedata.normalize("NFKD", p["full_name"])
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        if folded != p["full_name"] and folded not in aliases:
            aliases.append(folded)
        # Headshot filename is stored EXACTLY as it exists in the
        # upstream nba-headshots repo — that filename is what the URL
        # path needs, regardless of how we slugify. The frontend's
        # headshotUrl(slug) helper looks up this field.
        out[slug] = {
            "name": p["full_name"],
            "full_name": p["full_name"],
            "first_name": p.get("first_name", ""),
            "last_name": p.get("last_name", ""),
            "team": team,
            "nba_id": p["nba_id"],
            "headshot_filename": (p.get("headshot") or {}).get("filename"),
            "aliases": aliases,
        }

    # Additive overrides (incoming draft class, etc.) — merged AFTER the
    # active-derived build so hand-added players survive regeneration.
    if OVERRIDES_PATH.exists():
        overrides = json.load(OVERRIDES_PATH.open(encoding="utf-8"))
        added, superseded = apply_overrides(out, overrides)
        total = len([k for k in out if not k.startswith("_")])
        out["_meta"]["total_players"] = total
        out["_meta"]["overrides_added"] = added
        out["_meta"]["overrides_superseded"] = superseded
        print(
            f"overrides: {added} added as-is, {superseded} superseded by "
            f"active.json data (total players: {total})",
            file=sys.stderr,
        )
    return out


def main(argv: list[str]) -> int:
    src = Path(argv[1]) if len(argv) > 1 else DEFAULT_ACTIVE_INPUT
    if not src.exists():
        print(f"ERROR: input {src} not found. Fetch first:\n"
              f"  curl -L -o /tmp/nbah-active.json \\\n"
              f"    https://raw.githubusercontent.com/jsierrahoopshype/nba-headshots/main/players/metadata/active.json",
              file=sys.stderr)
        return 1
    blob = build(src)
    with PLAYERS_OUT.open("w", encoding="utf-8") as f:
        json.dump(blob, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {PLAYERS_OUT} with {len(blob) - 1} players")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
