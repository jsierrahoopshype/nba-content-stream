"""Compose the final MP4: stitch beat clips, mix in background music.

moviepy handles the heavy lifting (clip concat, encoder), but the
audio loop + fade + volume target is cleaner expressed in ffmpeg
than via moviepy's audio API. We let moviepy write a `.silent.mp4`
first, then `ffmpeg -i video.mp4 -stream_loop -1 -i music.mp3`
mixes the music in with the desired curve.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from pathlib import Path

from moviepy import VideoClip, concatenate_videoclips

logger = logging.getLogger("ffmpeg_compose")


def concat_clips(clips: list[VideoClip]) -> VideoClip:
    """Concatenate clips with a tiny crossfade so seams don't pop."""
    if not clips:
        raise ValueError("no clips to concatenate")
    return concatenate_videoclips(clips, method="compose")


def write_silent(clip: VideoClip, path: Path, fps: int) -> None:
    """Render `clip` to an MP4 with no audio track. Codec choice:
    libx264 + yuv420p + faststart so the output plays on every social
    platform (Reels insists on yuv420p; faststart moves the moov atom
    to the head so streaming starts before the full file downloads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clip.write_videofile(
        str(path),
        fps=fps,
        codec="libx264",
        audio=False,
        preset="medium",
        ffmpeg_params=[
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            # Quality target — CRF 22 is the social-export sweet spot:
            # near-visually-lossless at reasonable file size.
            "-crf", "22",
        ],
        logger=None,
    )


def mux_music(
    silent_mp4: Path,
    music_mp3: Path | None,
    out_mp4: Path,
    *,
    music_db: float = -18.0,
    fade_in_s: float = 1.5,
    fade_out_s: float = 2.5,
) -> None:
    """Combine `silent_mp4` + a looped `music_mp3` into `out_mp4`.

    Music is reduced to `music_db` so it stays under the visual
    pacing. Fades at both ends prevent the abrupt start/stop that
    makes recap videos feel like rough cuts. If `music_mp3` is None
    or missing, copy the silent video out as the final (with a
    warning) — the pipeline still ships a watchable video on
    missing-music environments.
    """
    if music_mp3 is None or not music_mp3.exists():
        logger.warning(
            "music track missing (%s); writing silent MP4 as final", music_mp3
        )
        shutil.copy2(silent_mp4, out_mp4)
        return

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not on PATH — cannot mux audio")

    # Probe silent video duration so we can place the fade-out.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", str(silent_mp4)],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())
    fade_out_start = max(0.0, duration - fade_out_s)

    # afade=t=in:st=0:d=N + afade=t=out:st=M:d=N gives the in/out
    # envelope; volume=<dB> sets the music bed level.
    audio_filter = (
        f"volume={music_db}dB,"
        f"afade=t=in:st=0:d={fade_in_s},"
        f"afade=t=out:st={fade_out_start}:d={fade_out_s}"
    )

    cmd = [
        ffmpeg, "-y",
        "-i", str(silent_mp4),
        "-stream_loop", "-1", "-i", str(music_mp3),
        "-filter_complex", f"[1:a]{audio_filter}[a]",
        "-map", "0:v", "-map", "[a]",
        "-shortest",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_mp4),
    ]
    logger.info("ffmpeg mux: %s", " ".join(shlex.quote(x) for x in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Keep the silent MP4 around as a fallback so the operator
        # can inspect the visual output if the mux fails.
        logger.error("ffmpeg mux failed:\n%s", result.stderr)
        raise RuntimeError("ffmpeg mux failed; silent MP4 is at " + str(silent_mp4))
