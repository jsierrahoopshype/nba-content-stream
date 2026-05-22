"""Tests for `scripts.lib.sources` — parsers and effective-list merge."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from scripts.lib import sources
from scripts.lib.sources import (
    SourcesError,
    load_effective_list,
    parse_json_array,
    parse_json_object_keys,
    parse_lines,
    parse_python_list_literal,
)


# ---------------------------------------------------------------------------
# parse_python_list_literal
# ---------------------------------------------------------------------------


def test_python_list_literal_extracts_named_variable():
    src = """
import os

OTHER = "ignore me"
REPORTERS = ["alice.bsky.social", "bob.bsky.social"]
ANOTHER = [1, 2, 3]
"""
    assert parse_python_list_literal(src, "REPORTERS") == [
        "alice.bsky.social",
        "bob.bsky.social",
    ]


def test_python_list_literal_handles_single_and_double_quotes():
    src = "HANDLES = ['a.bsky.social', \"b.bsky.social\"]"
    assert parse_python_list_literal(src, "HANDLES") == [
        "a.bsky.social",
        "b.bsky.social",
    ]


def test_python_list_literal_accepts_tuple_literal():
    src = "HANDLES = ('a', 'b')"
    assert parse_python_list_literal(src, "HANDLES") == ["a", "b"]


def test_python_list_literal_missing_variable_raises():
    with pytest.raises(ValueError):
        parse_python_list_literal("X = 1", "REPORTERS")


def test_python_list_literal_non_string_element_raises():
    with pytest.raises(ValueError):
        parse_python_list_literal("X = [1, 2]", "X")


def test_python_list_literal_uses_ast_not_eval():
    # Side-effect code would execute under eval; ast.parse just reads it.
    src = "import os\nos.system('echo pwned')\nX = ['safe']"
    assert parse_python_list_literal(src, "X") == ["safe"]


# ---------------------------------------------------------------------------
# parse_json_array / parse_json_object_keys
# ---------------------------------------------------------------------------


def test_json_array_parses_strings():
    assert parse_json_array('["a","b","c"]') == ["a", "b", "c"]


def test_json_array_rejects_non_array():
    with pytest.raises(ValueError):
        parse_json_array('{"a": 1}')


def test_json_array_rejects_non_string_element():
    with pytest.raises(ValueError):
        parse_json_array('["a", 2]')


def test_json_object_keys_preserves_order():
    text = '{"a": 1, "b": 2, "c": 3}'
    assert parse_json_object_keys(text) == ["a", "b", "c"]


def test_json_object_keys_rejects_non_object():
    with pytest.raises(ValueError):
        parse_json_object_keys('["a", "b"]')


# ---------------------------------------------------------------------------
# parse_lines
# ---------------------------------------------------------------------------


def test_parse_lines_ignores_blanks_and_comments():
    text = """
# this is a comment
alice.bsky.social

bob.bsky.social
   # indented comment
   carol.bsky.social
"""
    assert parse_lines(text) == [
        "alice.bsky.social",
        "bob.bsky.social",
        "carol.bsky.social",
    ]


# ---------------------------------------------------------------------------
# load_effective_list
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def overrides_path(tmp_path: Path) -> Path:
    path = tmp_path / "bluesky_overrides.json"
    return path


def _write_overrides(path: Path, add: list[str], remove: list[str]) -> None:
    path.write_text(json.dumps({"add": add, "remove": remove}))


def test_effective_list_merges_live_adds_removes(overrides_path: Path):
    _write_overrides(overrides_path, add=["c.bsky.social"], remove=["a.bsky.social"])
    live_payload = '["a.bsky.social", "b.bsky.social"]'

    with patch.object(sources, "_fetch_with_retry", return_value=live_payload):
        out = load_effective_list(
            "bluesky",
            "https://example/raw/list.json",
            parse_json_array,
            overrides_path,
        )

    # a removed, b kept from live, c appended from adds
    assert out == ["b.bsky.social", "c.bsky.social"]


def test_effective_list_deduplicates(overrides_path: Path):
    _write_overrides(overrides_path, add=["a.bsky.social"], remove=[])
    live_payload = '["a.bsky.social", "b.bsky.social"]'

    with patch.object(sources, "_fetch_with_retry", return_value=live_payload):
        out = load_effective_list(
            "bluesky", "https://x/", parse_json_array, overrides_path
        )

    assert out == ["a.bsky.social", "b.bsky.social"]


def test_effective_list_missing_overrides_file_is_ok(tmp_path: Path):
    missing = tmp_path / "does_not_exist.json"
    live_payload = '["a.bsky.social"]'

    with patch.object(sources, "_fetch_with_retry", return_value=live_payload):
        out = load_effective_list(
            "bluesky", "https://x/", parse_json_array, missing
        )

    assert out == ["a.bsky.social"]


def test_effective_list_live_fail_falls_back_to_adds(
    overrides_path: Path, caplog
):
    _write_overrides(overrides_path, add=["fallback.bsky.social"], remove=[])

    def boom(url, timeout=10.0):
        raise requests.ConnectionError("network down")

    with patch.object(sources, "_fetch_with_retry", side_effect=boom):
        with caplog.at_level("WARNING"):
            out = load_effective_list(
                "bluesky", "https://x/", parse_json_array, overrides_path
            )

    assert out == ["fallback.bsky.social"]
    assert any("live fetch failed" in rec.message for rec in caplog.records)


def test_effective_list_live_fail_empty_adds_raises(overrides_path: Path):
    _write_overrides(overrides_path, add=[], remove=[])

    def boom(url, timeout=10.0):
        raise requests.ConnectionError("network down")

    with patch.object(sources, "_fetch_with_retry", side_effect=boom):
        with pytest.raises(SourcesError):
            load_effective_list(
                "bluesky", "https://x/", parse_json_array, overrides_path
            )


def test_effective_list_logs_counts(overrides_path: Path, caplog):
    _write_overrides(
        overrides_path,
        add=["c.bsky.social"],
        remove=["a.bsky.social"],
    )
    live_payload = '["a.bsky.social", "b.bsky.social"]'

    with patch.object(sources, "_fetch_with_retry", return_value=live_payload):
        with caplog.at_level("INFO"):
            load_effective_list(
                "bluesky",
                "https://x/",
                parse_json_array,
                overrides_path,
            )

    joined = " ".join(rec.message for rec in caplog.records)
    assert "live=2" in joined
    assert "+adds=1" in joined
    assert "-removes=1" in joined
    assert "final=2" in joined
