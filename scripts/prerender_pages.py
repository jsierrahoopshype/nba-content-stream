"""Pre-render player and team HTML pages from the entity indexes.

Reads `data/index/manifest.json` and emits one static HTML file per
player and team that has content. The pages are SEO-baked: real
`<title>`, meta description, and Open Graph tags. The per-entity JSON
index (`data/index/{players,teams}/{slug}.json`) is loaded by
`assets/entity.js` at page-load time, so the prerender step doesn't
duplicate the items into the HTML — the JS pulls them.

Also writes `sitemap.xml` listing the homepage + every generated page.

The script is idempotent: every run wipes `players/` and `teams/`
under the repo root (the public HTML, not the JSON indexes) and
regenerates the pages from scratch.

CLI:
  --dry-run      Report counts, write nothing.
  -v / --verbose Debug logging.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger("prerender_pages")

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "data" / "index" / "manifest.json"
PLAYERS_OUT_DIR = REPO_ROOT / "players"
TEAMS_OUT_DIR = REPO_ROOT / "teams"
SITEMAP_PATH = REPO_ROOT / "sitemap.xml"

# Optional: a base URL for canonical and sitemap. If unset, the
# sitemap uses relative paths (still valid; Google accepts both).
SITE_BASE_URL = "https://jsierrahoopshype.github.io/nba-content-stream"


# ---------------------------------------------------------------------------
# Avatar (text initials, no images)
# ---------------------------------------------------------------------------


_AVATAR_COLORS = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#14b8a6",
]


def _avatar(name: str) -> Tuple[str, str]:
    """Return `(initials, bg_color)` for a deterministic-per-name avatar."""
    parts = [p for p in name.split() if p]
    if len(parts) == 1:
        initials = parts[0][:2].upper()
    else:
        initials = (parts[0][:1] + parts[-1][:1]).upper()
    # Hash the slug for a stable color.
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    color = _AVATAR_COLORS[h % len(_AVATAR_COLORS)]
    return initials, color


# ---------------------------------------------------------------------------
# Page template
# ---------------------------------------------------------------------------


def _render_page(kind: str, slug: str, name: str, count: int) -> str:
    """Render one entity page. `kind` is 'player' or 'team'."""
    initials, color = _avatar(name)
    kind_label = "Player" if kind == "player" else "Team"
    safe_name = html.escape(name)
    title = (
        f"{safe_name} — NBA News, Quotes &amp; Buzz | NBA Content Stream · HoopsMatic"
    )
    description = (
        f"The latest {safe_name} news, podcast mentions, reporter posts, "
        f"and headlines — updated continuously by HoopsMatic's NBA Content Stream."
    )
    og_url = f"{SITE_BASE_URL}/{kind}s/{slug}.html"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<meta name="description" content="{description}">
<meta name="ncs-entity" data-kind="{kind}" data-slug="{html.escape(slug)}">
<link rel="canonical" href="{og_url}">
<link rel="icon" type="image/svg+xml" href="../favicon.svg">
<link rel="alternate icon" type="image/x-icon" href="../favicon.ico">
<meta property="og:title" content="{safe_name} — NBA Content Stream">
<meta property="og:description" content="{description}">
<meta property="og:type" content="profile">
<meta property="og:url" content="{og_url}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../assets/styles.css">
</head>
<body>
<div class="container">
  <div class="tabs">
    <a href="../index.html">Feed</a>
    <a href="../players.html"{' class="active"' if kind == "player" else ''}>Players</a>
    <a href="../teams.html"{' class="active"' if kind == "team" else ''}>Teams</a>
  </div>

  <div class="entity-hero">
    <div class="avatar" style="background:{color}">{html.escape(initials)}</div>
    <div>
      <div class="name">{safe_name}</div>
      <div class="sub">{kind_label} · {count} mentions in the rolling window</div>
    </div>
  </div>

  <div class="controls">
    <div class="search-row">
      <div class="search-group">
        <label>Jump to another player or team</label>
        <input type="text" id="q" placeholder="e.g. Wemby, Lakers…" autocomplete="off">
        <div class="suggest" id="suggest"></div>
      </div>
    </div>
    <div class="pills" id="pills"></div>
  </div>

  <div class="chart-wrap">
    <div class="chart-label">Mentions, last 14 days</div>
    <div id="chart"></div>
  </div>

  <div class="summary" id="summary">loading…</div>
  <div class="feed" id="feed"></div>
  <div class="empty" id="empty" style="display:none">No mentions in the rolling window.</div>

  <div class="foot">
    Archive refreshes every 15 minutes; the live layer merges fresh items in the browser on page open.
  </div>
</div>

<script src="../assets/config.js"></script>
<script src="../assets/tagger.js"></script>
<script src="../assets/common.js"></script>
<script src="../assets/entity.js"></script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------


def _render_sitemap(player_slugs: List[str], team_slugs: List[str]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in (
        SITE_BASE_URL + "/",
        SITE_BASE_URL + "/index.html",
        SITE_BASE_URL + "/players.html",
        SITE_BASE_URL + "/teams.html",
    ):
        lines.append(
            f"  <url><loc>{url}</loc><lastmod>{today}</lastmod><changefreq>hourly</changefreq></url>"
        )
    for slug in player_slugs:
        lines.append(
            f"  <url><loc>{SITE_BASE_URL}/players/{slug}.html</loc>"
            f"<lastmod>{today}</lastmod><changefreq>hourly</changefreq></url>"
        )
    for slug in team_slugs:
        lines.append(
            f"  <url><loc>{SITE_BASE_URL}/teams/{slug}.html</loc>"
            f"<lastmod>{today}</lastmod><changefreq>hourly</changefreq></url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> dict:
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest not found at {manifest_path}; run build_indexes first"
        )
    with manifest_path.open(encoding="utf-8") as f:
        return json.load(f)


def _safe_clear_dir(path: Path) -> None:
    """Wipe a directory's *.html files, but leave any other content alone.

    Belt-and-suspenders: we only ever expect HTML in here, but we'd
    rather not nuke a directory wholesale.
    """
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file() and child.suffix == ".html":
            child.unlink()
        # Don't recurse — these dirs are flat.


def generate_pages(
    manifest: dict,
    players_out: Path = PLAYERS_OUT_DIR,
    teams_out: Path = TEAMS_OUT_DIR,
    sitemap_path: Path = SITEMAP_PATH,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Generate all player and team HTML pages + sitemap.xml.

    Returns `(num_player_pages, num_team_pages)`.
    """
    if not dry_run:
        players_out.mkdir(parents=True, exist_ok=True)
        teams_out.mkdir(parents=True, exist_ok=True)
        _safe_clear_dir(players_out)
        _safe_clear_dir(teams_out)

    n_players = 0
    for p in manifest.get("players", []):
        slug = p["slug"]
        name = p["name"]
        count = p["count"]
        html_text = _render_page("player", slug, name, count)
        if not dry_run:
            (players_out / f"{slug}.html").write_text(html_text, encoding="utf-8")
        n_players += 1

    n_teams = 0
    for t in manifest.get("teams", []):
        slug = t["slug"]
        name = t["name"]
        count = t["count"]
        html_text = _render_page("team", slug, name, count)
        if not dry_run:
            (teams_out / f"{slug}.html").write_text(html_text, encoding="utf-8")
        n_teams += 1

    if not dry_run:
        sitemap = _render_sitemap(
            [p["slug"] for p in manifest.get("players", [])],
            [t["slug"] for t in manifest.get("teams", [])],
        )
        sitemap_path.write_text(sitemap, encoding="utf-8")

    return n_players, n_teams


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Pre-render player and team HTML pages from manifest.json."
    )
    p.add_argument("--dry-run", action="store_true", help="Compute counts, write nothing.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start = time.perf_counter()
    # Resolve the manifest path at call time so monkeypatching
    # MANIFEST_PATH in tests is observed (Python binds default arg
    # values at definition time).
    try:
        manifest = load_manifest(MANIFEST_PATH)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    n_players, n_teams = generate_pages(
        manifest,
        players_out=PLAYERS_OUT_DIR,
        teams_out=TEAMS_OUT_DIR,
        sitemap_path=SITEMAP_PATH,
        dry_run=args.dry_run,
    )
    elapsed = time.perf_counter() - start
    logger.info(
        "generated %d player pages + %d team pages in %.2fs%s",
        n_players,
        n_teams,
        elapsed,
        " (dry-run, no writes)" if args.dry_run else "",
    )
    if args.dry_run:
        print(
            f"DRY RUN — would write {n_players} player pages, {n_teams} team pages, "
            f"and sitemap.xml"
        )
    return 0


if __name__ == "__main__":
    sys.exit(run())
