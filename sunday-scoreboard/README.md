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

## Tests

```bash
PYTHONPATH=scripts python3 -m pytest scripts/tests/ -q
```

28 smoke tests cover clustering, ranking, format specs, source
styling, and the canonical / reporter lookups.

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
│   ├── render_video.py        ← orchestrator
│   ├── fetch_week_data.py     ← archive pull
│   ├── cluster_beats.py       ← 24h-window grouping
│   ├── rank_beats.py          ← top-N + noise filter
│   ├── render_intro.py        ← 6s branded intro
│   ├── render_beat.py         ← 13s per beat (4 phases)
│   ├── render_outro.py        ← 8s leaderboard
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
│       └── ffmpeg_compose.py  ← concat + music mux
├── assets/
│   ├── fonts/                 ← DM Sans + JetBrains Mono TTFs
│   ├── music/                 ← background-recap.mp3 (see DESIGN.md)
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
