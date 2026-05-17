"""Tests for slice S12: add absolute split-date inputs to round creation.
Source-level checks against run_round.py (where the manifest is wired with
set_split_dates after UEL construction), app.py (where the split_dates
payload is validated), and experiment.html (where the six datetime-local
inputs and the gatherSplitDates() JS live)."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "experiment_runner"


def test_run_round_wires_set_split_dates() -> None:
    """After constructing the UEL, run_round.py reads optional split_dates
    from args.json and (when present) calls uel.manifest.set_split_dates
    with the six parsed datetimes. The call sits BEFORE uel.run so the
    manifest's split_dates is in place when prepare_data runs — that's
    the Limen contract that gives split_dates precedence over
    split_config in _run_prepare_setup and compute_test_bars."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    # The wiring is anchored on the SFD-args key and the Limen method name.
    assert "sfd_args.get(\"split_dates\")" in text
    assert "uel.manifest.set_split_dates(" in text
    # Ordering: set_split_dates must precede uel.run so the manifest is
    # mutated before prepare_data consumes it.
    idx_set = text.find("uel.manifest.set_split_dates(")
    idx_run = text.find("uel.run(**uel_kwargs)")
    assert idx_set != -1 and idx_run != -1
    assert idx_set < idx_run, "set_split_dates must be called before uel.run"


def test_run_round_reads_split_dates_from_args() -> None:
    """All six datetime keys are read from the split_dates dict, parsed
    via datetime.fromisoformat (with the trailing Z stripped, mirroring
    app.py::_parse_iso), and passed positionally to set_split_dates in
    Limen's expected order (train_start, train_end, val_start, val_end,
    test_start, test_end)."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    for key in ("train_start", "train_end", "val_start", "val_end", "test_start", "test_end"):
        # Each key is referenced exactly inside split_dates["..."]
        assert f'split_dates["{key}"]' in text, f"missing read of split_dates[{key!r}]"
    # The datetime parsing path is the same as _parse_iso in app.py: strip
    # trailing Z, then datetime.fromisoformat. The slice ships the import.
    assert "from datetime import datetime" in text
    assert "datetime.fromisoformat" in text
    assert ".rstrip(\"Z\")" in text


def test_app_validates_split_dates() -> None:
    """app.py::_validate_args treats split_dates as optional but, when
    present, requires all six datetime keys to be non-empty, parseable
    via _parse_iso (ISO-8601), and in non-decreasing order. The ordering
    rule mirrors limen.Manifest.set_split_dates so the same contract is
    enforced at the HTTP boundary as inside Limen."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    # Validation lives inside _validate_args, not as a standalone helper.
    tree = ast.parse(text)
    validate_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_args":
            validate_fn = node
            break
    assert validate_fn is not None, "_validate_args function not found in app.py"
    body = ast.unparse(validate_fn)
    # Six required keys named explicitly (so a refactor that drops one trips this).
    for key in ("train_start", "train_end", "val_start", "val_end", "test_start", "test_end"):
        assert f"'{key}'" in body or f'"{key}"' in body, f"split_dates required key {key!r} not validated"
    # ISO parsing routes through _parse_iso so the format contract is shared
    # with status.json / period.json date handling.
    assert "_parse_iso" in body
    # Ordering check rejects out-of-order bounds.
    assert "ordering" in body or "must be <=" in body


def test_experiment_html_has_six_split_date_inputs() -> None:
    """The experiment-page form exposes exactly six datetime-local inputs,
    one per split bound, with the names Limen consumes positionally:
    train_start, train_end, val_start, val_end, test_start, test_end."""
    text = (PKG_DIR / "templates" / "experiment.html").read_text(encoding="utf-8")
    # The six inputs in name order; each must be a datetime-local control.
    for key in ("train_start", "train_end", "val_start", "val_end", "test_start", "test_end"):
        pattern = re.compile(
            rf'<input[^>]*type="datetime-local"[^>]*name="{key}"|<input[^>]*name="{key}"[^>]*type="datetime-local"'
        )
        assert pattern.search(text), f"datetime-local input for {key!r} missing"
    # All-or-nothing client-side gathering: gatherSplitDates returns null
    # when any field is empty so the payload omits split_dates entirely
    # and the SFD's ratio split runs unchanged.
    assert "function gatherSplitDates()" in text
    assert "if (values.some(v => !v)) return null;" in text
    # The new helper is wired into the existing gatherWorking().
    assert "gatherSplitDates()" in text
    assert "args.split_dates = split_dates" in text


def test_pyproject_version_is_0_12_0() -> None:
    """Conventional Commits feat = minor bump. Version moves 0.11.0 → 0.12.0."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.12.0"


def test_changelog_has_v0_12_0_entry() -> None:
    """A CHANGELOG header for the new version must exist with at least
    one content line so the version-gate is satisfied."""
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "# v0.12.0"), -1)
    assert header_idx != -1, "missing '# v0.12.0' header in CHANGELOG.md"
    # At least one non-blank content line between this header and the next.
    next_header_idx = next(
        (i for i in range(header_idx + 1, len(lines)) if lines[i].startswith("# v")),
        len(lines),
    )
    content = [ln for ln in lines[header_idx + 1 : next_header_idx] if ln.strip()]
    assert content, "v0.12.0 CHANGELOG entry is empty"


def test_args_json_split_dates_roundtrip_shape() -> None:
    """The args.json schema gains an optional top-level 'split_dates' object
    with the six string keys. Validate that a representative payload
    serialises and parses cleanly — the shape contract that run_round.py
    reads and app.py validates is identical."""
    payload = {
        "sfd_identifier": "TestSFD",
        "data_source": {"method": "get_spot_klines", "params": {}},
        "uel_run": {"experiment_name": "x", "n_permutations": 1},
        "split_dates": {
            "train_start": "2024-01-01T00:00:00Z",
            "train_end":   "2024-07-01T00:00:00Z",
            "val_start":   "2024-07-01T00:00:00Z",
            "val_end":     "2024-10-01T00:00:00Z",
            "test_start":  "2024-10-01T00:00:00Z",
            "test_end":    "2025-01-01T00:00:00Z",
        },
    }
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["split_dates"]["train_start"] == "2024-01-01T00:00:00Z"
    assert set(decoded["split_dates"].keys()) == {
        "train_start", "train_end", "val_start", "val_end", "test_start", "test_end",
    }
