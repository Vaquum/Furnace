"""Tests for slice S12: add absolute split-date inputs to round creation.
Source-level checks against run_round.py (where the manifest is wired with
set_split_dates after UEL construction), app.py (where the split_dates
payload is validated), and experiment.html (where the six date inputs
and the gatherSplitDates() JS live)."""

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
    with the six expanded datetimes. The call sits BEFORE uel.run so the
    manifest's split_dates is in place when prepare_data runs — that's
    the Limen contract that gives split_dates precedence over
    split_config in _run_prepare_setup and compute_test_bars."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    assert "sfd_args.get(\"split_dates\")" in text
    assert "uel.manifest.set_split_dates(" in text
    # Ordering: set_split_dates must precede uel.run so the manifest is
    # mutated before prepare_data consumes it.
    idx_set = text.find("uel.manifest.set_split_dates(")
    idx_run = text.find("uel.run(**uel_kwargs)")
    assert idx_set != -1 and idx_run != -1
    assert idx_set < idx_run, "set_split_dates must be called before uel.run"


def test_run_round_expands_dates_with_end_of_day_inclusive_semantics() -> None:
    """The form collects YYYY-MM-DD date strings. run_round.py expands each:
        start_*: YYYY-MM-DD 00:00:00 UTC  (beginning of the typed day)
        end_*:   next-day   00:00:00 UTC  (end of the typed day, inclusive,
                                           since Limen's windows are [start, end))
    So train_end="2024-12-31" means "include all bars on Dec 31" and Limen
    receives train_end_dt = 2025-01-01 00:00:00 UTC. The two helpers
    `_split_start_dt` and `_split_end_dt` encode this; both must carry a
    UTC tzinfo (polars data column is Datetime('ms', 'UTC') and a naive
    literal would crash with a SchemaError)."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    # Helpers live at module scope so they can be unit-tested independently
    # of the round-running flow.
    assert "def _split_start_dt(" in text
    assert "def _split_end_dt(" in text
    # Conversion path: date.fromisoformat, combine with time.min, attach UTC.
    assert "date.fromisoformat" in text
    assert "time.min" in text
    assert "timezone.utc" in text
    # End-of-day-inclusive: end_dt = start_dt + 1 day.
    assert "timedelta(days=1)" in text
    # All six bounds use these helpers.
    assert text.count("_split_start_dt(split_dates[") == 3
    assert text.count("_split_end_dt(split_dates[") == 3


def test_app_validates_split_dates_as_date_strings_with_ordering() -> None:
    """app.py::_validate_args treats split_dates as optional but, when
    present, requires all six keys to be YYYY-MM-DD date strings (not
    datetimes — the form is type="date") and to satisfy the mixed
    ordering rule:
        within a split:  start <= end  (1-day window OK)
        between splits:  prev_end < next_start  (no overlap)
    i.e. train_start <= train_end < val_start <= val_end < test_start <= test_end.
    This maps cleanly to Limen's half-open [start, next-day-start) windows
    after the run_round expansion."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    validate_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_validate_args":
            validate_fn = node
            break
    assert validate_fn is not None, "_validate_args function not found in app.py"
    body = ast.unparse(validate_fn)
    # All six keys named explicitly so a refactor that drops one trips this.
    for key in ("train_start", "train_end", "val_start", "val_end", "test_start", "test_end"):
        assert f"'{key}'" in body or f'"{key}"' in body, f"split_dates required key {key!r} not validated"
    # Date-string parsing (NOT datetime). After ast.unparse the call is
    # normalised to single quotes.
    assert "date.fromisoformat" in body
    # Adjacent-pair ordering check uses itertools.pairwise — no defensive
    # double-zip.
    assert "pairwise(" in body
    # Mixed-operator ordering: <= within a split (False), < between splits (True).
    # Pattern: [F, T, F, T, F] for (train_start..train_end, train_end..val_start,
    # val_start..val_end, val_end..test_start, test_start..test_end).
    assert "[False, True, False, True, False]" in body
    # Error messages cite the actual operator so the user knows which rule
    # was violated.
    assert "must be {op}" in body or "must be <=" in body or "must be <" in body


def test_experiment_html_has_six_date_inputs_with_advanced_toggle() -> None:
    """The experiment-page form exposes:
       - Always: two type="date" inputs (train_start, test_end) for the
         basic case (drives fetch bounds via data_source.params overrides).
       - Behind an "Advanced" checkbox: four more type="date" inputs
         (train_end, val_start, val_end, test_start). Filling all six
         engages Limen's set_split_dates; filling only the two basic
         ones leaves the SFD's ratio split alone but bounds the fetch.
       Auto-fill: when train_end is set, val_start defaults to next-day;
       when test_start is set, val_end defaults to prior-day. Either can
       be overridden by the user."""
    text = (PKG_DIR / "templates" / "experiment.html").read_text(encoding="utf-8")
    # Six type="date" inputs (NOT datetime-local — operator should not have
    # to deal with hours), each named per Limen's positional set_split_dates.
    for key in ("train_start", "train_end", "val_start", "val_end", "test_start", "test_end"):
        pattern = re.compile(
            rf'<input[^>]*type="date"[^>]*name="{key}"|<input[^>]*name="{key}"[^>]*type="date"'
        )
        assert pattern.search(text), f"type=date input for {key!r} missing"
    # Legend identifies the section as UTC.
    assert "UTC" in text
    # Advanced toggle: a checkbox that flips .adv-only elements in place.
    # Default state is unchecked so the form opens with only train_start and
    # test_end visible regardless of what's in saved args — even saved
    # inner-four values stay hidden until the operator explicitly opts in.
    assert 'id="f-advanced"' in text
    assert "adv-only" in text
    assert "function setAdvancedMode" in text
    # Auto-fill helper and listeners exist.
    assert "function addDays(" in text
    assert "f-train-end" in text and "f-val-start" in text
    assert "f-test-start" in text and "f-val-end" in text
    # JS gather: mode-aware. Basic mode (advanced unchecked) never emits
    # split_dates, even if the hidden inner-four DOM still carries values
    # from a previous advanced session — the saved payload matches what the
    # user can see in the form. Advanced mode emits split_dates iff all six
    # are filled.
    assert "function gatherSplitDates()" in text
    assert "SPLIT_DATE_KEYS.every" in text
    assert "advancedEl.checked" in text or "advanced = advancedEl" in text
    assert "fetch_start" in text and "fetch_end" in text
    # gatherWorking writes fetch bounds into data_source.params so
    # start_date_limit / end_date_limit stay in sync with train_start /
    # test_end — these two keys are also filtered out of the dynamic
    # data-source render so the user has one source of truth.
    assert "params.start_date_limit = sd.fetch_start" in text
    assert "params.end_date_limit   = sd.fetch_end" in text
    assert "name === 'start_date_limit' || name === 'end_date_limit'" in text
    # Basic-mode reload: train_start / test_end values are read back from
    # data_source.params.start_date_limit / end_date_limit (where
    # gatherWorking writes them), not just from args.split_dates (which
    # is absent when advanced was off at save time). Either field name in
    # the Jinja expression is fine; assert the data_source.params path
    # contributes to the input value.
    assert "start_date_limit" in text and "end_date_limit" in text


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
    next_header_idx = next(
        (i for i in range(header_idx + 1, len(lines)) if lines[i].startswith("# v")),
        len(lines),
    )
    content = [ln for ln in lines[header_idx + 1 : next_header_idx] if ln.strip()]
    assert content, "v0.12.0 CHANGELOG entry is empty"


def test_args_json_split_dates_roundtrip_shape() -> None:
    """The args.json schema gains an optional top-level 'split_dates' object
    with the six YYYY-MM-DD string keys (not datetimes — the form is
    type="date" and the conversion to UTC datetimes happens at the
    run_round.py boundary right before Manifest.set_split_dates)."""
    payload = {
        "sfd_identifier": "TestSFD",
        "data_source": {"method": "get_spot_klines", "params": {
            "kline_size": 3600,
            # start_date_limit / end_date_limit are auto-derived from
            # split_dates.train_start / .test_end in gatherWorking.
            "start_date_limit": "2024-01-01",
            "end_date_limit":   "2025-12-31",
        }},
        "uel_run": {"experiment_name": "x", "n_permutations": 1},
        "split_dates": {
            "train_start": "2024-01-01",
            "train_end":   "2024-12-31",
            "val_start":   "2025-01-01",
            "val_end":     "2025-06-30",
            "test_start":  "2025-07-01",
            "test_end":    "2025-12-31",
        },
    }
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["split_dates"]["train_start"] == "2024-01-01"
    assert set(decoded["split_dates"].keys()) == {
        "train_start", "train_end", "val_start", "val_end", "test_start", "test_end",
    }
    # The fetch bounds in data_source.params mirror split_dates.train_start
    # / .test_end exactly — gatherWorking writes the same string.
    assert decoded["data_source"]["params"]["start_date_limit"] == decoded["split_dates"]["train_start"]
    assert decoded["data_source"]["params"]["end_date_limit"]   == decoded["split_dates"]["test_end"]
