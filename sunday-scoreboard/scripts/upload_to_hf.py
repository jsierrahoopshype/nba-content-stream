"""Upload weekly MP4s to the HuggingFace Space `cdechoch/sunday-scoreboard`.

Walks the local `outputs/` directory, uploads each `YYYY-MM-DD_*.mp4`
to `videos/YYYY-MM-DD_*.mp4` on the Space. Idempotent: HF's
`upload_file` overwrites by default, so re-running on the same week
republishes the latest cut.

Auth:
  - Reads HF_TOKEN from env. In production, GitHub Actions injects
    secrets.HF_TOKEN from the org-level HoopsMatic secret store.
    Locally, drop a token into ~/.huggingface/token or `export
    HF_TOKEN=hf_...` before running.

CLI:
  python scripts/upload_to_hf.py
  python scripts/upload_to_hf.py --week-of 2026-05-25
  python scripts/upload_to_hf.py --repo cdechoch/sunday-scoreboard
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from huggingface_hub import HfApi, RepositoryNotFoundError, create_repo

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = REPO_ROOT / "outputs"
DEFAULT_REPO_ID = "cdechoch/sunday-scoreboard"
DEFAULT_REPO_TYPE = "space"
DEFAULT_SPACE_SDK = "static"

logger = logging.getLogger("upload_to_hf")


def _ensure_space(api: HfApi, repo_id: str, token: str) -> None:
    """Create the HF Space if it doesn't exist yet. v1 ships as
    `static` SDK because we serve plain MP4s — no Gradio app needed.
    The Space is created private by default and Jorge can flip it
    public in the HF UI once a render passes review."""
    try:
        api.repo_info(repo_id=repo_id, repo_type=DEFAULT_REPO_TYPE, token=token)
        return
    except RepositoryNotFoundError:
        logger.info("Space %s not found — creating", repo_id)
    create_repo(
        repo_id=repo_id,
        repo_type=DEFAULT_REPO_TYPE,
        space_sdk=DEFAULT_SPACE_SDK,
        private=True,
        token=token,
        exist_ok=True,
    )


def upload_outputs(
    repo_id: str = DEFAULT_REPO_ID,
    *,
    outputs_dir: Path = OUTPUTS_DIR,
    week_of: str | None = None,
    token: str | None = None,
) -> list[str]:
    """Upload every `.mp4` under `outputs_dir` (optionally filtered
    to a single week) to the Space's `videos/` directory.

    Returns the list of repo paths that were written. Empty list
    means there was nothing to upload (which the caller may treat as
    a soft failure during the weekly cron).
    """
    token = token or os.getenv("HF_TOKEN")
    if not token:
        raise RuntimeError(
            "HF_TOKEN not set; export it or pass --token. "
            "In CI, configure GitHub repo/org secret HF_TOKEN."
        )
    api = HfApi()
    _ensure_space(api, repo_id, token)

    if not outputs_dir.exists():
        logger.warning("no outputs dir at %s — nothing to upload", outputs_dir)
        return []

    uploaded: list[str] = []
    for mp4 in sorted(outputs_dir.glob("*.mp4")):
        if week_of and not mp4.name.startswith(week_of):
            continue
        repo_path = f"videos/{mp4.name}"
        logger.info("uploading %s → %s:%s", mp4.name, repo_id, repo_path)
        api.upload_file(
            path_or_fileobj=str(mp4),
            path_in_repo=repo_path,
            repo_id=repo_id,
            repo_type=DEFAULT_REPO_TYPE,
            token=token,
        )
        uploaded.append(repo_path)
    return uploaded


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Upload Sunday Scoreboard MP4s to HuggingFace.")
    p.add_argument("--repo", default=DEFAULT_REPO_ID, help="HF Space id (default %(default)s).")
    p.add_argument("--outputs-dir", default=str(OUTPUTS_DIR))
    p.add_argument(
        "--week-of", default=None,
        help="Only upload files for this week (YYYY-MM-DD prefix match).",
    )
    p.add_argument("--token", default=None, help="HF token; defaults to $HF_TOKEN.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    paths = upload_outputs(
        repo_id=args.repo,
        outputs_dir=Path(args.outputs_dir),
        week_of=args.week_of,
        token=args.token,
    )
    if not paths:
        logger.warning("no files uploaded")
        return 1
    for rp in paths:
        print(rp)
    return 0


if __name__ == "__main__":
    sys.exit(run())
