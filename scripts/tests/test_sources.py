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
    load_effective_records,
    parse_bluesky_csv,
    parse_json_array,
    parse_json_object_keys,
    parse_lines,
    parse_python_list_literal,
)

FIXTURES = Path(__file__).parent / "fixtures"


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
# parse_bluesky_csv
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bluesky_csv_text() -> str:
    return (FIXTURES / "bluesky_handles.csv").read_text(encoding="utf-8")


def test_bluesky_csv_excludes_header_row(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    assert len(rows) == 6
    assert all("handle" in r and "display_name" in r and "did" in r for r in rows)


def test_bluesky_csv_handles_quoted_comma_in_display_name(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    shap = next(r for r in rows if r["handle"] == "shapalicious.bsky.social")
    # Comma inside the quoted display name must be preserved, not treated
    # as a column delimiter.
    assert shap["display_name"] == "Jake Shapiro, but spooky 🎃"
    assert shap["did"] == "did:plc:shapiro555aaa666bbb777ccc"


def test_bluesky_csv_preserves_emoji(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    shap = next(r for r in rows if r["handle"] == "shapalicious.bsky.social")
    assert "🎃" in shap["display_name"]


def test_bluesky_csv_preserves_custom_domain_handles(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    handles = [r["handle"] for r in rows]
    # Bare custom domains must NOT get .bsky.social appended.
    assert "pablo.show" in handles
    assert "nba.com" in handles
    assert "shrikhalpada.dev" in handles
    assert "pablo.show.bsky.social" not in handles


def test_bluesky_csv_captures_did(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    nuggets = next(r for r in rows if r["handle"] == "nuggets.bsky.social")
    assert nuggets["did"] == "did:plc:kdvkohfy7btmsdkg5mwuzjbw"
    nba = next(r for r in rows if r["handle"] == "nba.com")
    assert nba["did"] == "did:plc:nbaplcdid111222333444555"


def test_bluesky_csv_missing_did_is_none(bluesky_csv_text):
    rows = parse_bluesky_csv(bluesky_csv_text)
    zach = next(r for r in rows if r["handle"] == "zachlowe.bsky.social")
    assert zach["did"] is None


def test_bluesky_csv_skips_blank_handle_rows():
    text = (
        "Handle,Display Name,DID\n"
        ",Empty Handle,did:plc:x\n"
        "real.bsky.social,Real,did:plc:y\n"
    )
    rows = parse_bluesky_csv(text)
    assert [r["handle"] for r in rows] == ["real.bsky.social"]


def test_bluesky_csv_strips_whitespace_from_fields():
    text = "Handle,Display Name,DID\n  alice.bsky.social ,  Alice  ,  did:plc:a  \n"
    rows = parse_bluesky_csv(text)
    assert rows == [
        {
            "handle": "alice.bsky.social",
            "display_name": "Alice",
            "did": "did:plc:a",
        }
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


# ---------------------------------------------------------------------------
# load_effective_records (dict-based, e.g. Bluesky reporters)
# ---------------------------------------------------------------------------


def test_effective_records_merges_live_adds_removes(overrides_path: Path):
    _write_overrides(
        overrides_path,
        add=["added.bsky.social"],
        remove=["nba.com"],
    )
    csv_text = (FIXTURES / "bluesky_handles.csv").read_text()

    with patch.object(sources, "_fetch_with_retry", return_value=csv_text):
        out = load_effective_records(
            "bluesky",
            "https://x/handles.csv",
            parse_bluesky_csv,
            overrides_path,
        )

    handles = [r["handle"] for r in out]
    assert "nba.com" not in handles  # removed
    assert "nuggets.bsky.social" in handles
    assert "added.bsky.social" in handles  # appended from overrides.add


def test_effective_records_added_entry_is_stub_with_only_key(
    overrides_path: Path,
):
    _write_overrides(overrides_path, add=["fresh.bsky.social"], remove=[])
    csv_text = (FIXTURES / "bluesky_handles.csv").read_text()

    with patch.object(sources, "_fetch_with_retry", return_value=csv_text):
        out = load_effective_records(
            "bluesky",
            "https://x/handles.csv",
            parse_bluesky_csv,
            overrides_path,
        )

    stub = next(r for r in out if r["handle"] == "fresh.bsky.social")
    # User adding a reporter only knows the handle, not the DID — stub
    # has no `did` key, so the poller falls back to the handle as actor.
    assert "did" not in stub


def test_effective_records_dedupes_by_key_field(overrides_path: Path):
    # Adding a handle already present in live shouldn't create a duplicate.
    _write_overrides(overrides_path, add=["nuggets.bsky.social"], remove=[])
    csv_text = (FIXTURES / "bluesky_handles.csv").read_text()

    with patch.object(sources, "_fetch_with_retry", return_value=csv_text):
        out = load_effective_records(
            "bluesky",
            "https://x/handles.csv",
            parse_bluesky_csv,
            overrides_path,
        )

    handles = [r["handle"] for r in out]
    assert handles.count("nuggets.bsky.social") == 1
    # And the live record (with full fields) wins over the stub.
    nuggets = next(r for r in out if r["handle"] == "nuggets.bsky.social")
    assert nuggets.get("did") == "did:plc:kdvkohfy7btmsdkg5mwuzjbw"


def test_effective_records_live_fail_falls_back_to_adds(
    overrides_path: Path, caplog
):
    _write_overrides(overrides_path, add=["fallback.bsky.social"], remove=[])

    def boom(url, timeout=10.0):
        raise requests.ConnectionError("network down")

    with patch.object(sources, "_fetch_with_retry", side_effect=boom):
        with caplog.at_level("WARNING"):
            out = load_effective_records(
                "bluesky",
                "https://x/handles.csv",
                parse_bluesky_csv,
                overrides_path,
            )

    assert [r["handle"] for r in out] == ["fallback.bsky.social"]


def test_effective_records_empty_raises(overrides_path: Path):
    _write_overrides(overrides_path, add=[], remove=[])

    def boom(url, timeout=10.0):
        raise requests.ConnectionError("network down")

    with patch.object(sources, "_fetch_with_retry", side_effect=boom):
        with pytest.raises(SourcesError):
            load_effective_records(
                "bluesky",
                "https://x/handles.csv",
                parse_bluesky_csv,
                overrides_path,
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
