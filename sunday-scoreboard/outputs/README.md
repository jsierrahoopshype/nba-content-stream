# Sunday Scoreboard — `outputs/`

Local cache for rendered MP4s before they're uploaded to the
HuggingFace Space at [`cdechoch/sunday-scoreboard`](https://huggingface.co/spaces/cdechoch/sunday-scoreboard).

## Naming

```
{YYYY-MM-DD}_{format}.mp4
```

Where `format` ∈ `{horizontal, square, vertical}` and the date is
the Sunday that opens the recap week (UTC).

## .gitignore

By default this directory ignores every `.mp4` — they're generated
artifacts, not source. A single curated example file with the
suffix `.example.mp4` is allowed through (`!*_square.example.mp4`)
so a reviewer can preview the visual style without running the
pipeline.

To promote a new example:

```bash
mv outputs/2026-05-25_square.mp4 outputs/2026-05-25_square.example.mp4
git add outputs/2026-05-25_square.example.mp4
```

The previous example should be deleted (or moved out of `outputs/`)
in the same commit — keep one in-tree at a time so the repo doesn't
balloon.
