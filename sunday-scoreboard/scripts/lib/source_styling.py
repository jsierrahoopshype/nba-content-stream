"""Per-source visual identity.

Color tokens match the source-pill palette on the nba-content-stream
site so a viewer who's seen the dashboard recognizes the cue here.
Pure data module — no Pillow / moviepy imports so it stays cheap to
load in tests.
"""

from __future__ import annotations

from dataclasses import dataclass

ALL_SOURCES = ("bluesky", "google-news", "reddit", "substack", "youtube")

# Display labels (shorter than slugs, suitable for source-mix pills).
DISPLAY = {
    "bluesky": "Bluesky",
    "google-news": "News",
    "reddit": "Reddit",
    "substack": "Substack",
    "youtube": "YouTube",
}

# Colors mirror assets/styles.css `--src-*` tokens in nba-content-stream.
COLOR = {
    "bluesky": "#1083fe",
    "google-news": "#1a73e8",
    "reddit": "#ff4500",
    "substack": "#ff6719",
    "youtube": "#ff0000",
}

# Background tint for pills (lighter / desaturated version of COLOR).
COLOR_DIM = {
    "bluesky": "#dbeafe",
    "google-news": "#dbeafe",
    "reddit": "#fee5d6",
    "substack": "#fee5d6",
    "youtube": "#fde7e7",
}


@dataclass(frozen=True)
class SourceStyle:
    key: str
    label: str
    color: str
    color_dim: str


def source_style(key: str) -> SourceStyle:
    if key not in COLOR:
        # Unknown sources still get a neutral fallback so renderers
        # never crash on a future source they don't yet know about.
        return SourceStyle(
            key=key, label=key, color="#71717a", color_dim="#e5e7eb"
        )
    return SourceStyle(
        key=key, label=DISPLAY[key], color=COLOR[key], color_dim=COLOR_DIM[key]
    )
