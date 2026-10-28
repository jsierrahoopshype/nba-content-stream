"""Shared pytest fixtures for the ingestion tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.lib.utils import parse_to_iso

# The feed fixtures under fixtures/ are dated 2026-05-14 .. 2026-05-21.
# Tests that run a poller end to end freeze its --since-hours clock to
# the morning after, so the window is measured from the fixtures' own
# "today" instead of the real clock (a real-clock window silently stops
# covering the fixtures a few weeks after they were recorded).
FIXTURE_NOW = datetime(2026, 5, 22, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def pin_poll_clock(monkeypatch):
    """Call `pin_poll_clock(poll_module)` to freeze that poller's
    `_default_since_iso` (the --since-hours cutoff) at FIXTURE_NOW."""

    def _pin(module, now: datetime = FIXTURE_NOW) -> None:
        monkeypatch.setattr(
            module,
            "_default_since_iso",
            lambda hours=24: parse_to_iso(now - timedelta(hours=hours)),
        )

    return _pin
