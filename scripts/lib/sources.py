"""Live source lists with local add/remove overrides.

Pollers fetch their source list (Bluesky reporters, YouTube channels, etc.)
from an upstream URL — usually a HuggingFace Space file or a GitHub raw
file — and then apply a local overrides JSON at `data/sources/{name}.json`
to add or remove entries.

`load_effective_list` is the entry point. It takes a parser callable so
the upstream can be in any format (Python list literal in app.py, JSON
array, JSON object keys, one-per-line text). Parser helpers are
exported for the common cases.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from pathlib import Path
from typing import Callable, List

import requests

logger = logging.getLogger(__name__)


class SourcesError(RuntimeError):
    """Raised when the live list cannot be fetched and no overrides exist."""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def parse_python_list_literal(text: str, variable_name: str) -> List[str]:
    """Extract a list-of-strings assigned to `variable_name` in Python source.

    Uses AST (not eval) so it's safe to run on arbitrary upstream files.
    Only matches top-level assignments and only returns the value if it's a
    list literal of strings. Raises ValueError if not found or wrong shape.
    """
    tree = ast.parse(text)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == variable_name:
                value = node.value
                if not isinstance(value, (ast.List, ast.Tuple)):
                    raise ValueError(
                        f"{variable_name} is not a list/tuple literal"
                    )
                out: List[str] = []
                for elt in value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        out.append(elt.value)
                    else:
                        raise ValueError(
                            f"{variable_name} contains a non-string element"
                        )
                return out
    raise ValueError(f"variable {variable_name!r} not found in source")


def parse_json_array(text: str) -> List[str]:
    """Parse a JSON array of strings. Raises ValueError on non-array JSON."""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    out: List[str] = []
    for item in data:
        if not isinstance(item, str):
            raise ValueError("array contains a non-string element")
        out.append(item)
    return out


def parse_json_object_keys(text: str) -> List[str]:
    """Parse a JSON object and return its top-level keys in insertion order."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return list(data.keys())


def parse_lines(text: str) -> List[str]:
    """One identifier per line. Strip whitespace, drop blanks and `#` comments."""
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Effective list (live + overrides)
# ---------------------------------------------------------------------------


def _fetch_with_retry(url: str, timeout: float = 10.0) -> str:
    """GET `url` with one retry on transient errors. Returns response text."""
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except (requests.RequestException, requests.HTTPError) as exc:
            last_exc = exc
            if attempt == 1:
                logger.debug("fetch %s attempt 1 failed: %s; retrying", url, exc)
                time.sleep(1.0)
                continue
    assert last_exc is not None
    raise last_exc


def _load_overrides(path: Path) -> tuple[List[str], List[str]]:
    """Load the overrides JSON. Missing file is OK; returns ([], [])."""
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8") as f:
        blob = json.load(f)
    adds = list(blob.get("add", []) or [])
    removes = list(blob.get("remove", []) or [])
    return adds, removes


def load_effective_list(
    name: str,
    live_url: str,
    parser: Callable[[str], List[str]],
    overrides_path: Path,
) -> List[str]:
    """Fetch the live list, apply overrides, return the merged list.

    Merge rule: `(live ∪ add) \\ remove`. Order is preserved — live entries
    first (in upstream order), then `add` entries appended in order. Each
    identifier appears at most once.

    On live fetch failure, logs a WARNING and falls back to `add` only. If
    that's also empty, raises `SourcesError`.
    """
    adds, removes = _load_overrides(overrides_path)
    remove_set = set(removes)

    try:
        raw = _fetch_with_retry(live_url)
        live = parser(raw)
        live_ok = True
    except Exception as exc:
        logger.warning(
            "%s: live fetch failed (%s); falling back to overrides.add only",
            name,
            exc,
        )
        live = []
        live_ok = False

    seen: set[str] = set()
    out: List[str] = []
    for entry in live:
        if entry in remove_set or entry in seen:
            continue
        seen.add(entry)
        out.append(entry)
    for entry in adds:
        if entry in remove_set or entry in seen:
            continue
        seen.add(entry)
        out.append(entry)

    if not out:
        raise SourcesError(
            f"{name}: effective list is empty "
            f"(live_ok={live_ok}, adds={len(adds)}, removes={len(removes)})"
        )

    logger.info(
        "%s: live=%d +adds=%d -removes=%d final=%d",
        name,
        len(live),
        len(adds),
        len(removes),
        len(out),
    )
    return out
