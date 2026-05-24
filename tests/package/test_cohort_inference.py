"""Tests for the cohort-inference slice: two new GET endpoints under
one completed round expose the manifest-prescribed input data at a
chosen datetime, and the cohort prediction for the next bar.

These are source-level checks against `furnace/app.py`,
`furnace/inference.py`, and `scripts/cohort_inference.py` — the
codebase's idiom (`app.py` can't be imported without fastapi; tests
assert on source text). The behavioural contract is locked in via
text-level assertions on the endpoint declarations, on the validator
that rejects open-ended manifests, and on the helper-script signature
Furnace shells out to."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _app_text() -> str:
    return (PKG_DIR / "app.py").read_text(encoding="utf-8")


def _inference_text() -> str:
    return (PKG_DIR / "inference.py").read_text(encoding="utf-8")


def _helper_text() -> str:
    return (SCRIPTS_DIR / "cohort_inference.py").read_text(encoding="utf-8")


def test_inference_module_exists() -> None:
    """The slice ships a dedicated module that mediates between the
    FastAPI surface and the Limen-using subprocess. Locating it here so
    a regression that deletes or renames the module fails before any
    behavioural check runs."""
    assert (PKG_DIR / "inference.py").is_file(), \
        "furnace/inference.py is the cohort-inference module"


def test_helper_script_exists() -> None:
    """The Trainer + Cohort work runs in `scripts/cohort_inference.py`
    so the typed `furnace/` package never imports `limen` or `polars`
    at module scope. The pyright `include` list is `['furnace']`, so
    keeping the helper outside that surface is the only thing keeping
    the typing budget from absorbing Limen's loose type information."""
    assert (SCRIPTS_DIR / "cohort_inference.py").is_file(), \
        "scripts/cohort_inference.py is the Trainer + Cohort helper"


def test_app_exposes_cohort_input_endpoint() -> None:
    """GET on the round's `/cohort/input` returns the per-permutation
    `x_test` row the cohort would feed its members at `at`. The route
    sits under the round so the operator's path is search → round →
    cohort, mirroring how the search surface picks `(experiment_id,
    round_id, permutation_ids)` in one go."""
    text = _app_text()
    assert (
        '@app.get("/experiments/{experiment_id}/rounds/{round_id}/cohort/input")'
        in text
    ), "GET /experiments/{experiment_id}/rounds/{round_id}/cohort/input must be registered"
    assert "def cohort_input(" in text


def test_app_exposes_cohort_predict_endpoint() -> None:
    """GET on the round's `/cohort/predict` trains the selected
    permutations into sensors via Limen's `Trainer`, builds a `Cohort`
    over them, and returns the aggregated prediction for the bar
    starting at `at` + `kline_size`. Same path shape as `/cohort/input`
    so the two endpoints sit side by side under the round."""
    text = _app_text()
    assert (
        '@app.get("/experiments/{experiment_id}/rounds/{round_id}/cohort/predict")'
        in text
    ), "GET /experiments/{experiment_id}/rounds/{round_id}/cohort/predict must be registered"
    assert "def cohort_predict(" in text


def test_endpoints_take_permutation_ids_and_at() -> None:
    """Both endpoints accept the same two query parameters: a
    comma-separated `permutation_ids` and an ISO-8601 `at`. The search
    surface produces the (round, permutation_ids) tuple; the operator
    picks `at`. Anything else would split the surface so we lock the
    shared parameter names in here."""
    text = _app_text()
    for fn in ("cohort_input", "cohort_predict"):
        marker = f"def {fn}("
        start = text.index(marker)
        body = text[start : start + 400]
        assert "permutation_ids: str" in body, \
            f"{fn} must accept permutation_ids as a string query param"
        assert "at: str" in body, \
            f"{fn} must accept at as a string query param"


def test_app_routes_use_inference_module() -> None:
    """The endpoints delegate to `inference.input_payloads_at` and
    `inference.predict_at`. Keeping the FastAPI handlers thin and the
    logic in `furnace/inference.py` means the validation and subprocess
    work is testable without standing up a FastAPI app."""
    text = _app_text()
    assert "from . import inference" in text, \
        "app.py must import the inference module"
    assert "inference.input_payloads_at(" in text, \
        "/cohort/input must delegate to inference.input_payloads_at"
    assert "inference.predict_at(" in text, \
        "/cohort/predict must delegate to inference.predict_at"


def test_inference_rejects_unbounded_manifests() -> None:
    """The slice's hard constraint: every supported manifest pins BOTH
    `start_date_limit` and `end_date_limit` on its data source.
    Without an end date, the data source would fetch up to whatever
    the upstream provider has now — neither deterministic nor fast
    enough to serve a request. The validator must mention both
    literals so the failure message tells the operator exactly which
    keys are missing."""
    text = _inference_text()
    assert "assert_bounded_window" in text, \
        "inference module must expose assert_bounded_window"
    assert "start_date_limit" in text and "end_date_limit" in text, \
        "validator must check both start_date_limit and end_date_limit"


def test_inference_validator_runs_before_subprocess() -> None:
    """`assert_bounded_window` runs before the helper subprocess so
    open-ended manifests fail fast — the operator sees the manifest
    error, not a Trainer / Cohort crash deep inside Limen. Both
    public functions must call the validator first."""
    text = _inference_text()
    for fn in ("input_payloads_at", "predict_at"):
        marker = f"def {fn}("
        start = text.index(marker)
        body = text[start : start + 800]
        validator_idx = body.find("assert_bounded_window(")
        helper_idx = body.find("_run_helper(")
        assert validator_idx != -1, \
            f"{fn} must call assert_bounded_window"
        assert helper_idx != -1, \
            f"{fn} must invoke the helper"
        assert validator_idx < helper_idx, \
            f"{fn} must call assert_bounded_window before _run_helper"


def test_inference_invokes_helper_script_via_subprocess() -> None:
    """The subprocess hop is what keeps `furnace/` free of `limen` and
    `polars` imports. The helper script lives at
    `scripts/cohort_inference.py` and is invoked with a fixed argv —
    no shell, no environment leakage beyond the parent process."""
    text = _inference_text()
    assert "subprocess" in text, "inference must shell out to the helper"
    assert "cohort_inference.py" in text, \
        "inference must reference the helper script path"
    assert "shell=True" not in text, \
        "the subprocess hop must not pass through a shell"


def test_helper_script_accepts_required_flags() -> None:
    """The helper exposes a stable CLI: `--limen-output`,
    `--permutation-ids`, `--at`, `--mode`. The Furnace side passes
    those flags verbatim, so renaming or dropping any of them would
    break the bridge."""
    text = _helper_text()
    for flag in ("--limen-output", "--permutation-ids", "--at", "--mode"):
        assert flag in text, f"helper must accept {flag}"


def test_helper_script_supports_input_and_predict_modes() -> None:
    """`--mode input` returns the per-permutation `x_test` rows
    without retraining; `--mode predict` retrains sensors and runs the
    cohort. Locking the choice list here so a new mode cannot ship
    silently."""
    text = _helper_text()
    assert "choices=['input', 'predict']" in text, \
        "helper --mode must accept exactly 'input' and 'predict'"


def test_helper_uses_limen_trainer_and_cohort() -> None:
    """The helper's job is to build a Limen `Cohort` from one round's
    `limen_output` and return its prediction. Reference both classes
    so a refactor that swaps them out is caught here."""
    text = _helper_text()
    assert "from limen.cohort import Cohort" in text, \
        "helper must build a Limen Cohort"
    assert "from limen.experiment import Trainer" in text, \
        "helper must reconstruct sensors via Limen Trainer"


def test_inference_endpoints_handle_value_and_runtime_errors() -> None:
    """Validation failures (manifest missing dates, bad datetime,
    helper exit) surface as 400 (client) or 500 (helper crash); the
    handler must not let the raw exception escape as an opaque 500
    with no message body. Lock the mapping in here."""
    text = _app_text()
    for fn in ("cohort_input", "cohort_predict"):
        marker = f"def {fn}("
        start = text.index(marker)
        body = text[start : start + 800]
        assert "ValueError" in body and "FileNotFoundError" in body, \
            f"{fn} must map ValueError/FileNotFoundError to a 400"
        assert "RuntimeError" in body, \
            f"{fn} must map RuntimeError to a 500"
        assert "HTTPException(400" in body, \
            f"{fn} must raise HTTPException(400, ...) for bad input"
        assert "HTTPException(500" in body, \
            f"{fn} must raise HTTPException(500, ...) for helper failure"


def test_cohort_endpoints_require_existing_limen_output() -> None:
    """A round has no inference surface until `limen run` has produced
    `limen_output/`. Both endpoints check the directory exists and
    return 404 otherwise — the operator's mental model is that the
    round itself is the inference unit, and an unfinished round
    cannot be one."""
    text = _app_text()
    assert "_cohort_limen_output" in text, \
        "app.py must define the limen_output resolver helper"
    helper_start = text.index("def _cohort_limen_output(")
    helper_body = text[helper_start : helper_start + 400]
    assert "is_dir()" in helper_body, \
        "the resolver must check that limen_output is a directory"
    assert "HTTPException(404" in helper_body, \
        "the resolver must return 404 when limen_output is missing"
