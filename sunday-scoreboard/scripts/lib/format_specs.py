"""Format specs for Sunday Scoreboard videos.

One template per aspect ratio. Each format defines its own dimensions,
font sizes, portrait size, and layout key. Renderers consult these to
position elements per format — we deliberately do NOT share layouts
across formats so each aspect ratio reads polished on its native
surface (16:9 for YouTube, 1:1 for Instagram feed, 9:16 for Reels /
Shorts / TikTok).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ----- Brand identity -----
# Brand color tokens match the HoopsMatic NBA-content-stream site so the
# video reads as the same product. Keep these in sync if the site palette
# moves.
ACCENT = "#3b82f6"            # primary accent (HoopsMatic blue)
ACCENT_DIM = "#dbeafe"        # subtle blue background tint
BACKGROUND = "#f5f5f7"        # off-white app background
SURFACE = "#ffffff"           # card surface
TEXT = "#1a1a1a"              # body text
TEXT_SECONDARY = "#71717a"    # de-emphasized metadata
BORDER = "#e5e7eb"            # divider hairline
SHADOW = (0, 0, 0, 28)        # RGBA — translucent drop shadow

# ----- Paths -----
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
MUSIC_DIR = ASSETS_DIR / "music"
BRAND_DIR = ASSETS_DIR / "brand"
OUTPUTS_DIR = REPO_ROOT / "outputs"

FONT_REGULAR = FONTS_DIR / "DMSans-Regular.ttf"
FONT_BOLD = FONTS_DIR / "DMSans-Bold.ttf"
FONT_MONO = FONTS_DIR / "JetBrainsMono-Regular.ttf"
FONT_MONO_BOLD = FONTS_DIR / "JetBrainsMono-Bold.ttf"

MUSIC_FILE = MUSIC_DIR / "background-recap.mp3"


@dataclass(frozen=True)
class FormatSpec:
    """One aspect-ratio template. Renderers read every dimension from
    here so changing a font size or padding in one place updates every
    frame for that format."""

    key: str
    width: int
    height: int
    fps: int
    layout: str  # "side-by-side", "stacked", "vertical-stack"

    # Font sizes — type scale per format. Vertical and horizontal need
    # different headline weights because horizontal has more width to
    # breathe; vertical needs bigger text on smaller column.
    title_font_size: int
    headline_font_size: int
    meta_font_size: int
    rank_font_size: int

    # Portrait diameter (player headshot / team logo). Different per
    # format because the headshot's relative weight in the composition
    # changes with aspect ratio.
    portrait_size: int

    # Padding around content blocks; same value used for outer margins
    # and inter-block gutters scaled by 1.5×.
    pad: int

    # Title-card phase duration (seconds). Beat segment total is the
    # sum of title + headlines + reporters + transition.
    title_seconds: float = 3.0
    headlines_seconds: float = 6.0
    reporters_seconds: float = 3.0
    transition_seconds: float = 1.0

    intro_seconds: float = 6.0
    outro_seconds: float = 8.0

    @property
    def beat_seconds(self) -> float:
        return (
            self.title_seconds
            + self.headlines_seconds
            + self.reporters_seconds
            + self.transition_seconds
        )

    @property
    def aspect(self) -> float:
        return self.width / self.height


HORIZONTAL = FormatSpec(
    key="horizontal",
    width=1920, height=1080, fps=30,
    layout="side-by-side",
    title_font_size=96,
    headline_font_size=44,
    meta_font_size=28,
    rank_font_size=200,
    portrait_size=420,
    pad=80,
)

SQUARE = FormatSpec(
    key="square",
    width=1080, height=1080, fps=30,
    layout="stacked",
    title_font_size=72,
    headline_font_size=36,
    meta_font_size=24,
    rank_font_size=160,
    portrait_size=320,
    pad=60,
)

VERTICAL = FormatSpec(
    key="vertical",
    width=1080, height=1920, fps=30,
    layout="vertical-stack",
    title_font_size=80,
    headline_font_size=40,
    meta_font_size=26,
    rank_font_size=180,
    portrait_size=400,
    pad=70,
)

FORMAT_SPECS = {
    "horizontal": HORIZONTAL,
    "square": SQUARE,
    "vertical": VERTICAL,
}


def get_format(key: str) -> FormatSpec:
    if key not in FORMAT_SPECS:
        raise ValueError(
            f"unknown format {key!r}; expected one of {sorted(FORMAT_SPECS)}"
        )
    return FORMAT_SPECS[key]


def hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """`#rrggbb` → `(r, g, b)`. Pillow speaks tuples for fills, hex for
    color names — this lets the same brand token serve both APIs."""
    s = hex_str.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
