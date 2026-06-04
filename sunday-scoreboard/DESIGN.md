# Sunday Scoreboard — Design

## Versions at a glance

| Version | Editorial spine | Per-beat shape | Status |
| ------- | --------------- | -------------- | ------ |
| **v1** | Top-10 leaderboard recap | Title card → headlines roll → reporters → transition (13s) | Shipped, parallel |
| **v2 — "The Spotlight Edit"** | Top-10 per-player deep-dives | Hero parallax → best Bluesky quote → mention-spike sparkline (12s) | **Current** |

v2 is **parallel** to v1, not a replacement: it reuses the shared
`lib/` unchanged and the v1 scripts are untouched, so either pipeline
can run. v2 is the current editorial direction; v1 remains as a
fallback and reference. The v2 entry point is
`scripts/render_video_v2.py`. The first v2 PR validates **square
only** — horizontal/vertical layouts land in v2.1 once square reads
well.

## v2 — The Spotlight Edit

Same "top 10 players ranked by mention volume" spine, but each beat is
a slower, more emotional spotlight on one player instead of a fast
leaderboard roll.

### Beat phases (12s, hard cut between beats)

| Phase | Duration | What's on screen |
| ----- | -------- | ---------------- |
| 1 · Hero portrait | 4.0s | Headshot slow-zooms 100%→60% (quart-out) while a brand-blue→navy gradient parallaxes **behind** it at 0.3× (opposite direction). Rank glyph slides in from the top; player name reveals letter-by-letter (50ms stagger); mention count pulses bottom-right; source-mix pills along the bottom. |
| 2 · Best Bluesky quote | 5.0s | Same headshot — desaturated, blurred, slow Ken Burns pan — behind the week's best quote, fading in line-by-line. Reporter avatar + display name + `@handle` top-left; engagement ticker (likes + reposts + replies) counts up 0→total over 1.5s with a pulse on the final tick; `via @reporter` under the quote. |
| 3 · Mention spike | 3.0s | Per-player 7-day sparkline (Mon→Sun), drawn left-to-right over 1.5s, Y-axis scaled **per player** (each beat owns its data story). Peak day called out (`Tuesday: +47 mentions`); five spike source-mix pills below; weekly total above. |

The "sharp cut to the next rank" is realized at the compose layer:
beats concatenate with **no crossfade**, so the next beat's rank glyph
slides in immediately at the top of its hero phase. There is no fourth
phase — the cut *is* the transition.

### Engagement sourcing

The archive doesn't store Bluesky engagement, so `fetch_engagement.py`
pulls it live at render time:

1. For each top-10 player, take their Bluesky candidate items from the
   beat cluster.
2. Derive each post's AT-URI. The archive `id` already encodes it —
   `bs-` + URL-encoded `did:…/app.bsky.feed.post/<rkey>` — so decoding
   the id yields a `getPostThread`-ready URI with **no** handle→DID
   round-trip. Fallback: DID from the CDN `thumbnail` URL + rkey from
   the post `url`.
3. `GET public.api.bsky.app/xrpc/app.bsky.feed.getPostThread?uri=…` →
   `likeCount` / `repostCount` / `replyCount`.
4. Score `(likes + reposts*2 + replies*3)` — replies/reposts weighted
   higher because active engagement beats a passive like. Highest
   score wins per player (ties → recency).

Fetches are **paced** — chunks of 10 with 500ms between, a sync port
of `pacedBatchFetch` in nba-content-stream's `assets/common.js`.
Worst case ~500 candidate posts → ~30–60s. Results cache to
`assets/cache/engagement_{week}.json` (git-ignored) so re-renders
don't re-hit the API. If a fetch fails or every candidate scores 0,
selection falls back to the most recent candidate — the render never
fails on missing engagement.

### Parallax + Ken Burns

MoviePy has no native parallax; we render each frame ourselves with
shifted/scaled PIL layers (same per-frame approach as v1). `lib/parallax.py`
keeps the math pure (zoom curve, parallax offset, letter-stagger
alpha, Ken Burns offset) and wraps the heavier transforms (gradient
plate, scaled placement, desaturate+blur) thinly on top. `lib/sparkline.py`
does the same for the spike chart (daily bucketing, peak detection,
point mapping, draw progress). Both are unit-tested without touching
the network or the encoder.

### v2 module map (all additive — v1 untouched)

```
scripts/
  render_video_v2.py   orchestrator (square first; --top-n; --dry-run)
  render_beat_v2.py     3-phase spotlight beat
  render_intro_v2.py    dynamic intro (slide + letter reveal)
  render_outro_v2.py    animated leaderboard (count-up bars)
  fetch_engagement.py   paced Bluesky engagement re-fetch + cache
  lib/parallax.py        parallax + Ken Burns helpers
  lib/sparkline.py       animated mention-spike sparkline
  lib/engagement_score.py AT-URI derivation + scoring + quote pick
```

Music is unchanged from v1 — still the empty/silent slot at
`assets/music/background-recap.mp3` (sourced separately; see § Music).

### v2.1 — review fixes (current)

Corrections after the first real local render (2026-06-01, square,
top-3). All are content/layout only — the 3-phase structure and per-
beat timing are unchanged.

**Players only.** Team entities are never beats — `beat_select.players_only`
drops them before ranking. A team still appears as *context* (the
player's team name on the hero card). Rationale: the recap is a player
countdown; team beats produced near-duplicate stories.

**One beat per player (dedupe rule).** The 24h-window clustering can
split one news arc into two beats for the same player (the first render
had the same player at #1 and #3 with identical sparklines). The rule:
after ranking (`rank_beats`: mentions desc → source diversity →
recency), `beat_select.one_beat_per_player` keeps **only each player's
single highest-signal beat**, preserving rank order. Because the input
is already ranked, the first beat seen per player is the one kept, so
the top-N is **N distinct players**.

**Quote quality gates** (`lib/quote_filter.py`). Pure engagement
scoring surfaced @nba.com marketing copy, since official accounts win
raw engagement. Replaced with filter-then-score:

1. *Roster gate* — only posts from handles in nba-content-stream's
   curated reporter roster (`data/sources/bluesky_handles.csv`, ~164
   reporters) are eligible. **That roster is the editorial filter** —
   official/league/team accounts simply aren't in it. An explicit
   `data/quote_blocklist.json` (league + 30 team handles, plus any
   `nba.com` handle) is a second gate. If the roster fetch fails the
   gate disables (degrade to scoring) rather than yielding zero quotes.
2. *Length* — drop posts under 60 chars (no "GAME DAY" one-liners).
3. *Mostly emoji/caps* — drop posts >50% emoji or uppercase.
4. *Score* the survivors with the unchanged `likes + reposts*2 + replies*3`.

If nothing survives, the quote phase shows the spotlight cleanly
("No standout reporter quote this week.") rather than airing junk.

**Emoji / unrenderable handling.** Quote text is cleaned before render:
strip emoji, pictographs, dingbats, arrows, and variation selectors by
codepoint range — **never** a blanket regex — so accented Latin
(French / Serbian-Latin / Turkish / Croatian player + reporter names)
is fully preserved. Whitespace is then collapsed.

**Truncation.** Quotes cut at the last sentence boundary that fits,
falling back to the last whole word + `…`. Never mid-word. The wrap is
measure-injected (`prepare_quote_lines`) so it's unit-tested without a
font.

**Hero layout (collision-safe).** `lib/layout.hero_layout` reserves
non-overlapping rectangles — rank (top-left band), portrait (centered,
capped at 45% frame height), name, team sub, count (own right-aligned
band above the pills), pills (bottom edge). A test asserts no two zones
intersect for the longest canonical names ("Giannis Antetokounmpo",
"Shai Gilgeous-Alexander", "Karl-Anthony Towns") at ranks 1/3/10.

**Quote-phase background.** Dropped the muddy blurred-portrait backdrop
for a clean treatment: light `#f5f5f7` with a subtle diagonal accent
gradient strip; the portrait becomes a small circular avatar beside the
reporter attribution. Premium = clean, not layered blur.

**Sparkline layout.** Chart now occupies the middle ~50% of the frame;
peak callout above, source-mix pills + a one-line context row
("378 mentions this week · peaked Wednesday") below — no more dead
whitespace at the bottom.

## Intent

A polished, fully automated weekly NBA recap. Same brand language as
`nba-content-stream` and the broader HoopsMatic site, so anyone who's
seen the dashboard recognises this as the same product. No human
in the loop after merge — the GitHub Actions cron does the whole
thing every Sunday.

## Brand tokens

| Token | Value | Notes |
| ----- | ----- | ----- |
| `ACCENT` | `#3b82f6` | HoopsMatic blue |
| `ACCENT_DIM` | `#dbeafe` | Pill background tint |
| `BACKGROUND` | `#f5f5f7` | Off-white plate |
| `SURFACE` | `#ffffff` | Card / pill surfaces |
| `TEXT` | `#1a1a1a` | Body |
| `TEXT_SECONDARY` | `#71717a` | Meta |
| `BORDER` | `#e5e7eb` | Hairlines |
| Body font | DM Sans (Regular + Bold) | Names, headlines |
| Mono font | JetBrains Mono (Regular + Bold) | Counts, ranks, source labels |

Source palette mirrors the `--src-*` tokens in
`nba-content-stream/assets/styles.css`:

| Source | Color |
| ------ | ----- |
| Bluesky | `#1083fe` |
| Google News | `#1a73e8` |
| Reddit | `#ff4500` |
| Substack | `#ff6719` |
| YouTube | `#ff0000` |

## Beat segment — phase breakdown

| Phase | Duration | What's on screen |
| ----- | -------- | ---------------- |
| Title card | 3.0s | Rank glyph, portrait (cover crop for players, contain for teams), name, mention count with pulse, source-mix pills |
| Headlines roll | 6.0s | Three source headlines fade in/out top→bottom, each ~2s, on a blurred portrait backdrop (~10% opacity) |
| Reporters | 3.0s | 3 reporter avatars (staggered 0.25s entrance), `@handle` + display name |
| Transition | 1.0s | Final reporters frame slides up; accent bar grows across the bottom |

**Total per beat:** 13s. Ten beats = 130s. Intro 6s + outro 8s →
**~144s total** (~2:24 min). Inside the brief's 2-3 minute target.

## Format specs

| Key | Dimensions | Layout | Title font | Portrait |
| --- | ---------- | ------ | ---------- | -------- |
| `horizontal` | 1920×1080 @ 30fps | Side-by-side (portrait left, content right) | 96px | 420px |
| `square` | 1080×1080 @ 30fps | Stacked (portrait top, content below) | 72px | 320px |
| `vertical` | 1080×1920 @ 30fps | Vertical-stack | 80px | 400px |

Each format has its own layout — no sharing, because what reads well
on 16:9 (lots of horizontal space) gets cramped at 9:16.

## Animation language

Easings are explicit so the result feels designed, not generated:

- **`quart_out`** for entrances. Fast start, decelerates into final
  position. Material-Design-standard "premium" curve.
- **`quart_in`** for exits. Slow start then accelerates out — element
  leaves with momentum.
- **`sin_in_out`** for transitions. Symmetric — no obvious start /
  end velocity at the midpoint.
- **`pulse`** (sine, amplitude 6%, period 1.6s) on the mention count.
  Subtle breath; never bouncy.

No spring physics, no overshoot, no bouncy ease-out-back.

## Headlines selection

`render_video._pick_headlines`:
1. Bucket cluster items by source.
2. Sort each bucket by recency (desc).
3. Pass 1: take the top item from each source (max 3 sources).
4. Pass 2: if `< n` chosen, fill from remaining items by recency.
5. Each headline carries: title, source, "8h ago" label,
   source-mix rank.

Result: the 3 headlines read source-diverse when possible (Bluesky
+ Reddit + News rather than three Bluesky posts), but if the
cluster is one source it gracefully shows three of that source.

## Music

**Track:** placeholder slot at `assets/music/background-recap.mp3`.

For v1 the track is **not bundled** because no licensing-clean track
has been chosen yet. The pipeline writes a silent MP4 if the file
is absent and logs `music track missing` — fully watchable, just
mute.

**To finalize before promoting the workflow:**

1. Pick a track from the [YouTube Audio Library](https://studio.youtube.com)
   tagged "No attribution required, Instrumental, Inspirational" —
   ~3 minutes, building energy.
2. Suggested vibe: "Slate" by Quincas Moreira, or any of:
   - "On the Hunt" (instrumental, mid-tempo)
   - "Clear and Clean" (cinematic, builds)
   - "Lights" by Patrick Patrikios
3. Download as MP3 → `assets/music/background-recap.mp3`.
4. Record the YouTube Audio Library URL + title + author in this
   doc under § Music ledger.

The pipeline expects to **loop and fade** the track:

- `-stream_loop -1` on the music input — loops seamlessly until the
  video runs out (the `-shortest` flag stops the mix when the
  video ends, not the audio).
- `volume=-18dB` so the bed stays under text-heavy frames.
- `afade=t=in:st=0:d=1.5` — 1.5s fade in.
- `afade=t=out:st={dur-2.5}:d=2.5` — 2.5s fade out into the final
  frame.

### Music ledger (fill in after first promotion)

| Promoted | Track title | Author | Source URL | License |
| -------- | ----------- | ------ | ---------- | ------- |
| _pending_ | _TBD_ | _TBD_ | _TBD_ | YouTube Audio Library — no attribution required |

## Data sourcing

We pull from `raw.githubusercontent.com/jsierrahoopshype/nba-content-stream/main/...`
even though sunday-scoreboard currently lives in the same repo. This
makes the migration to a standalone repo a no-op — the upstream URLs
don't change. `archive_client.py` is the single point of entry; if
the upstream ever moves, only that file needs to change.

| Endpoint | Path |
| -------- | ---- |
| Manifest | `data/index/manifest.json` |
| Feed | `data/index/feed.json` |
| Per-player | `data/index/players/{slug}.json` |
| Per-team | `data/index/teams/{slug}.json` |
| Canonical players | `data/canonical/players.json` |
| Canonical teams | `data/canonical/teams.json` |
| Headshots | `nba-headshots/main/players/headshots/face/{filename}` |
| Team logos | ESPN CDN — `a.espncdn.com/i/teamlogos/nba/500/{abbr}.png` |
| Bluesky avatars | `cdn.bsky.app/img/avatar/plain/{did}/{cid}@jpeg` |

### Team-logo fallback

ESPN's CDN returns `403 host_not_allowed` to some cloud IPs. When
that happens the renderer falls back to a colored initials circle
(same look as the entity-page initials avatar in nba-content-stream).
`canonical_lookup.team_logo_url` prefers a bundled local PNG at
`assets/templates/team-logos/{slug}.png` if present, so a future
commit can ship real logos without touching the renderer.

## Migration to a standalone repo

Once the v1 cron is producing acceptable videos for a few weeks,
move the sub-project into its own repository:

```bash
# 1. Split the subdir history out.
git checkout main
git subtree split --prefix=sunday-scoreboard --branch=sunday-scoreboard-standalone

# 2. Create the new repo on GitHub (jsierrahoopshype/sunday-scoreboard)
#    via the UI, then point the local branch at it:
git remote add scoreboard git@github.com:jsierrahoopshype/sunday-scoreboard.git
git push scoreboard sunday-scoreboard-standalone:main

# 3. Move the workflow into the standard location in the new repo:
#    .github-workflows-deferred/weekly-render.yml  →  .github/workflows/weekly-render.yml
#    (and drop the cd sunday-scoreboard lines — paths become root-relative)

# 4. Remove the subdir from nba-content-stream once the standalone repo
#    is producing videos on schedule:
git rm -r sunday-scoreboard/
git commit -m "Move Sunday Scoreboard to its own repo"
```

### Constants that hardcode the subdir path

A short list of things to touch when migrating:

| File | What changes |
| ---- | ------------ |
| `.github-workflows-deferred/weekly-render.yml` | Drop `cd sunday-scoreboard`; move to `.github/workflows/` |
| `scripts/lib/format_specs.py:REPO_ROOT` | Already computes from `__file__` — no change needed |
| `scripts/lib/canonical_lookup.py:_local_team_logo_path` | Already relative — no change |
| `README.md` | Update Quickstart paths and remove "lives as a subdir" note |
| `DESIGN.md` | Update Migration section to a "✅ done" note |

## Auth

`HF_TOKEN` for HuggingFace uploads:

- **Local development:** `export HF_TOKEN=hf_...` in your shell, or
  drop a token at `~/.huggingface/token`.
- **GitHub Actions (when promoted):** repository or organization
  secret named `HF_TOKEN`. Jorge has an existing HoopsMatic
  org-level secret with the same name reused here.

## Validation checklist (pre-promotion)

Before copying the workflow into `.github/workflows/`:

- [ ] Run `pytest scripts/tests/` — all 28 smoke tests pass.
- [ ] Run `python scripts/render_video.py --week-of <last-sunday> --dry-run -v` — pipeline produces 10 ranked beats with the expected entities.
- [ ] Render one square MP4 locally; play through.
- [ ] Render the horizontal + vertical formats; confirm layouts read cleanly per aspect.
- [ ] Confirm `assets/music/background-recap.mp3` exists and is the chosen track (update the Music ledger).
- [ ] Manually upload one MP4 to the HF Space to confirm auth works:
      `python scripts/upload_to_hf.py --week-of <last-sunday>`.
- [ ] Copy `weekly-render.yml` from `.github-workflows-deferred/` into
      the top-level `.github/workflows/` (adjusting paths) and merge.
