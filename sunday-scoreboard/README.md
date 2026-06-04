# Sunday Scoreboard

Automated weekly NBA video recap built from the
[nba-content-stream](https://github.com/jsierrahoopshype/nba-content-stream) archive.

Every Sunday morning, a GitHub Actions cron:

1. Pulls the past 7 days of items from `nba-content-stream`'s public
   `data/` folder via `raw.githubusercontent.com`.
2. Clusters items into "beats" (one entity × one 24-hour window).
3. Ranks beats by mention volume, takes the top 10.
4. Renders a 2–3 minute video in three aspect ratios
   (16:9 / 1:1 / 9:16) with HoopsMatic brand styling.
5. Mixes in a YouTube-Audio-Library music bed.
6. Uploads the three MP4s to the HuggingFace Space
   [`cdechoch/sunday-scoreboard`](https://huggingface.co/spaces/cdechoch/sunday-scoreboard).

No voiceover. No editorial commentary. Pure data-driven recap.

## Two pipelines

This sub-project ships two **parallel** render pipelines that share the
same `lib/` and data layer:

- **v1 — leaderboard recap** (`render_video.py`): top-10 with a title
  card → headlines roll → reporters per beat. Shipped; kept as a
  reference/fallback.
- **v2 — "The Spotlight Edit"** (`render_video_v2.py`): the **current**
  direction. Top-10 per-player deep-dives — hero parallax → best
  Bluesky quote (with live engagement) → 7-day mention-spike sparkline.
  See [DESIGN.md § v2](./DESIGN.md#v2--the-spotlight-edit).

## Quickstart

```bash
# Install deps (Python 3.12 recommended, 3.11+ works).
pip install -r requirements.txt
sudo apt-get install -y ffmpeg

# Dry-run for last Sunday's recap (no encoding, prints data shape).
python scripts/render_video.py --week-of 2026-05-25 --dry-run -v

# Render the square format for last Sunday.
python scripts/render_video.py --week-of 2026-05-25 --format square

# Render all three formats.
python scripts/render_video.py --week-of 2026-05-25 --all-formats

# Upload everything in outputs/ to HuggingFace.
export HF_TOKEN=hf_...
python scripts/upload_to_hf.py
```

### v2 — The Spotlight Edit

```bash
# Dry-run (walks the pipeline, reuses cached engagement, no encode).
python scripts/render_video_v2.py --week-of 2026-06-01 --dry-run -v

# Warm the engagement cache on its own (paced Bluesky re-fetch).
python scripts/fetch_engagement.py --week-of 2026-06-01 --top-n 10

# Render the square spotlight recap (v2 validates square first).
python scripts/render_video_v2.py --week-of 2026-06-01 --format square --top-n 10

# Skip the live engagement fetch and rely on the cache only.
python scripts/render_video_v2.py --week-of 2026-06-01 --format square --no-engagement

# v2.1 review build — players only, quote quality filters, layout fixes.
# Render a square top-5 (fast iteration) on a machine with network:
python scripts/render_video_v2.py --week-of 2026-06-01 --format square --top-n 5
```

**v2.1** is players-only (teams appear as hero-card context, never as
beats), keeps one beat per player (10 distinct players), and gates the
"best quote" to the curated reporter roster
(`data/sources/bluesky_handles.csv`) plus an explicit
`data/quote_blocklist.json` so official/league/team accounts never get
quoted. See [DESIGN.md § v2.1](./DESIGN.md#v21--review-fixes-current).

v2 fetches live Bluesky engagement (likes/reposts/replies) at render
time, paced in chunks of 10 with 500ms between, and caches it to
`assets/cache/engagement_{week}.json` (git-ignored). Horizontal +
vertical formats arrive in v2.1 — square is the validated target for
the first v2 PR.

## Tests

```bash
PYTHONPATH=scripts python3 -m pytest scripts/tests/ -q
```

118 smoke tests: the original 28 (clustering, ranking, format specs,
source styling, canonical / reporter lookups), 43 for v2 (parallax +
Ken Burns math, the animated sparkline, engagement scoring + AT-URI
derivation, the paced engagement fetcher), and 47 for v2.1 (players-
only + per-player dedupe, quote quality filters + roster/blocklist +
sentence-safe truncation + emoji strip preserving accented names, and
collision-safe hero layout zones).

## Layout

```
sunday-scoreboard/
├── README.md           ← you are here
├── DESIGN.md           ← visual spec, music choice, migration plan
├── requirements.txt
├── .github-workflows-deferred/  ← weekly-render.yml lives here for v1
│                                  (DISABLED — copy to .github/workflows
│                                   when ready to enable)
├── scripts/
│   ├── render_video.py        ← v1 orchestrator
│   ├── render_video_v2.py     ← v2 orchestrator (Spotlight Edit)
│   ├── fetch_week_data.py     ← archive pull
│   ├── fetch_engagement.py    ← v2 paced Bluesky engagement re-fetch
│   ├── cluster_beats.py       ← 24h-window grouping
│   ├── rank_beats.py          ← top-N + noise filter
│   ├── render_intro.py        ← v1 6s branded intro
│   ├── render_beat.py         ← v1 13s per beat (4 phases)
│   ├── render_outro.py        ← v1 8s leaderboard
│   ├── render_intro_v2.py     ← v2 dynamic intro
│   ├── render_beat_v2.py      ← v2 12s spotlight beat (3 phases)
│   ├── render_outro_v2.py     ← v2 animated leaderboard
│   ├── upload_to_hf.py        ← HF Space upload
│   ├── tests/                 ← pytest smoke suite
│   └── lib/
│       ├── archive_client.py  ← raw.githubusercontent fetcher
│       ├── canonical_lookup.py← slug → headshot/logo/team-context
│       ├── reporter_lookup.py ← Bluesky handle → avatar/display
│       ├── source_styling.py  ← per-source color palette
│       ├── format_specs.py    ← per-format dimensions + fonts
│       ├── draw.py            ← Pillow drawing primitives
│       ├── easing.py          ← quart-out / sin-in-out
│       ├── ffmpeg_compose.py  ← concat + music mux
│       ├── parallax.py        ← v2 parallax + Ken Burns helpers
│       ├── sparkline.py       ← v2 animated mention-spike chart
│       ├── engagement_score.py← v2 AT-URI + scoring + quote pick
│       ├── beat_select.py     ← v2.1 players-only + per-player dedupe
│       ├── layout.py          ← v2.1 collision-safe hero zones
│       └── quote_filter.py    ← v2.1 roster/quality filters + clean/truncate
├── data/
│   └── quote_blocklist.json   ← v2.1 official/team handle blocklist
├── assets/
│   ├── fonts/                 ← DM Sans + JetBrains Mono TTFs
│   ├── music/                 ← background-recap.mp3 (see DESIGN.md)
│   ├── cache/                 ← v2 engagement_{week}.json (git-ignored)
│   ├── brand/                 ← HoopsMatic logo (when added)
│   └── templates/             ← bundled team-logo PNGs (optional)
└── outputs/
    ├── .gitignore             ← ignores *.mp4 except .example.mp4
    └── README.md
```

## Where this lives

For v1 this scaffolds as a sub-project inside the `nba-content-stream`
repo at `/sunday-scoreboard/`. Once the first weekly render passes
review, we'll migrate to a standalone repo via `git subtree split`
— see [DESIGN.md § Migration](./DESIGN.md#migration-to-a-standalone-repo).

## Workflow status

The cron workflow is committed to `.github-workflows-deferred/`, **not**
`.github/workflows/`. It does **not** run automatically yet. Jorge promotes
it manually once the first MP4 has been reviewed:

```bash
mkdir -p .github/workflows
cp sunday-scoreboard/.github-workflows-deferred/weekly-render.yml \
   .github/workflows/sunday-scoreboard.yml
git add .github/workflows/sunday-scoreboard.yml
```

## Constraints (hard)

- Python 3.12 (3.11 verified), MoviePy 2.x, Pillow, `requests`, `huggingface_hub`.
- ffmpeg installed at the system level.
- Brand identity: `#3b82f6` accent, `#f5f5f7` background, DM Sans + JetBrains Mono.
- No copyrighted footage / music / images.
- No voiceover / no editorial overlay.
- No browser-based rendering (Puppeteer, Playwright) — pure Python + ffmpeg.
- No paid AI in the pipeline.
