"""Tests for the cohort-inference slice: two new GET endpoints under
one completed round expose the manifest-prescribed input data at a
chosen datetime, and the cohort prediction for the next bar.

These are source-level checks against ``furnace/app.py`` and
``furnace/inference.py`` — the codebase's idiom (``app.py`` can't be
imported without fastapi; tests assert on source text). The
behavioural contract is locked in via text-level assertions on the
endpoint declarations, on the validator that rejects open-ended
manifests, and on the in-process Limen call chain
(``limen.experiment.Trainer`` → ``limen.cohort.Cohort`` →
``limen.experiment.Sensor``)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"


def _app_text() -> str:
    return (PKG_DIR / "app.py").read_text(encoding="utf-8")


def _inference_text() -> str:
    return (PKG_DIR / "inference.py").read_text(encoding="utf-8")


def test_inference_module_exists() -> None:
    """The slice ships a dedicated module that mediates between the
    FastAPI surface and Limen. Locating it here so a regression that
    deletes or renames the module fails before any behavioural check
    runs."""
    assert (PKG_DIR / "inference.py").is_file(), \
        "furnace/inference.py is the cohort-inference module"


def test_inference_imports_limen_in_process() -> None:
    """The module imports Limen's ``Trainer``, ``Sensor``, and
    ``Cohort`` at module scope — no subprocess hop, no helper script.
    This is the contract that keeps us inside the documented Limen
    surface rather than reaching for a shadow implementation."""
    text = _inference_text()
    assert "from limen.cohort import Cohort" in text, \
        "inference must import Limen's Cohort"
    assert "from limen.experiment import" in text, \
        "inference must import from limen.experiment"
    assert "Trainer" in text, "inference must reference Limen's Trainer"
    assert "Sensor" in text, "inference must reference Limen's Sensor"


def test_inference_has_no_subprocess_hop() -> None:
    """An earlier draft of this slice shelled out to a script. That
    was wrong — Limen runs in-process. Lock the absence of any
    subprocess import / call so a regression cannot reintroduce the
    shadow execution path."""
    text = _inference_text()
    assert "import subprocess" not in text, \
        "inference must not import subprocess"
    assert "subprocess." not in text, \
        "inference must not call into subprocess"


def test_inference_follows_documented_limen_flow() -> None:
    """The Limen Cohort docs flow is fixed: ``Trainer(...).train(pids)``
    → list of Sensors → ``Cohort(...).set_members(sensors)`` →
    ``cohort.predict(...)``. Lock each step in source so the chain
    cannot be broken."""
    text = _inference_text()
    assert "Trainer(limen_output_dir)" in text, \
        "inference must construct Trainer with the round's limen_output"
    assert "trainer.train(permutation_ids)" in text, \
        "inference must train sensors via Trainer.train(permutation_ids)"
    assert "Cohort(" in text and "experiment_log_path=" in text, \
        "inference must build Cohort with experiment_log_path"
    assert "cohort.set_members(sensors)" in text, \
        "inference must bind sensors via Cohort.set_members"
    assert "predict({\"_by_permutation_id\":" in text, \
        "inference must call Cohort.predict with the _by_permutation_id contract"


def test_helper_script_is_gone() -> None:
    """The earlier subprocess helper at ``scripts/cohort_inference.py``
    must not exist. Its absence is the mechanical signal that the
    Limen-in-process refactor is complete."""
    assert not (REPO_ROOT / "scripts" / "cohort_inference.py").is_file(), \
        "scripts/cohort_inference.py must not exist — inference runs in-process"


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
    logic in `furnace/inference.py` means the validation and Limen
    orchestration is testable without standing up a FastAPI app."""
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


def test_inference_validator_runs_before_trainer() -> None:
    """``assert_bounded_window`` runs before the Trainer construction so
    open-ended manifests fail fast — the operator sees the manifest
    error, not a Trainer crash deep inside Limen. Both public
    functions must call the validator first."""
    text = _inference_text()
    for fn in ("input_payloads_at", "predict_at"):
        marker = f"def {fn}("
        start = text.index(marker)
        body = text[start : start + 1200]
        validator_idx = body.find("assert_bounded_window(")
        trainer_idx = body.find("Trainer(")
        assert validator_idx != -1, \
            f"{fn} must call assert_bounded_window"
        assert trainer_idx != -1, \
            f"{fn} must construct a Trainer"
        assert validator_idx < trainer_idx, \
            f"{fn} must call assert_bounded_window before constructing Trainer"


def test_predict_uses_cohort_aggregation_surface() -> None:
    """``predict_at`` runs ``Cohort.predict`` and reports the cohort's
    ``aggregation_mode`` in the response body. The aggregation mode
    is part of the API contract: it tells the caller whether the
    prediction came from probability-weighted aggregation or majority
    vote, which is the difference between trusting the probability
    and trusting only the class label."""
    text = _inference_text()
    marker = "def predict_at("
    start = text.index(marker)
    body = text[start : start + 2000]
    assert "cohort.predict(" in body or "predictor.predict(" in body, \
        "predict_at must call Cohort.predict"
    assert "aggregation_mode" in body, \
        "predict_at must report cohort.aggregation_mode in the response"


def test_input_endpoint_does_not_train() -> None:
    """``/cohort/input`` reuses the manifest's ``prepare_data`` pipeline
    but does NOT run ``Trainer.train`` — that step is the expensive
    part of the cohort flow, and the input endpoint exists precisely
    to expose the would-be inference data without paying that cost.
    Lock the absence of ``.train(`` calls in ``input_payloads_at``."""
    text = _inference_text()
    marker = "def input_payloads_at("
    start = text.index(marker)
    nxt = text.find("\ndef ", start + 1)
    body = text[start : nxt if nxt != -1 else len(text)]
    assert ".train(" not in body, \
        "input_payloads_at must not call Trainer.train — the input endpoint is the cheap path"


def test_inference_endpoints_handle_value_and_runtime_errors() -> None:
    """Validation failures (manifest missing dates, bad datetime,
    Trainer crash) surface as 400 (client) or 500 (Trainer crash);
    the handler must not let the raw exception escape as an opaque
    500 with no message body. Lock the mapping in here."""
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
            f"{fn} must raise HTTPException(500, ...) for Trainer failure"


def test_parse_at_returns_tzaware_utc() -> None:
    """Limen's ``HistoricalData`` normalises kline ``datetime`` columns to
    ``Datetime('us', 'UTC')`` — tz-aware. Polars refuses to compare a
    tz-aware column against a naive literal (``SchemaError``), so
    ``_parse_at`` must return a tz-aware UTC datetime. A naive ISO
    string is interpreted as UTC. This test locks the contract in
    source — a future refactor that goes back to ``replace(tzinfo=None)``
    fails here."""
    text = _inference_text()
    assert "tzinfo=timezone.utc" in text, \
        "_parse_at must tag naive ISO inputs as UTC"
    assert "replace(tzinfo=None)" not in text, \
        "_parse_at must NOT strip tz to naive — Polars refuses naive vs UTC comparison"


def test_predict_payload_includes_xval_yval() -> None:
    """``Sensor.predict`` for calibrated logreg (``apply_calibrated_predict``)
    and TabPFN (``data_dict_to_numpy(data, ['x_val', 'y_val', 'x_test'])``)
    both index the data dict for ``x_val`` / ``y_val`` and raise
    ``KeyError`` otherwise. The per-permutation payload must carry
    those keys through from the manifest's ``prepare_data`` so the
    cohort supports calibrated and TabPFN architectures, not only
    bare logreg."""
    text = _inference_text()
    assert "_member_payload" in text, \
        "predict_at must build payloads via _member_payload"
    payload_start = text.index("def _member_payload(")
    nxt = text.find("\ndef ", payload_start + 1)
    body = text[payload_start : nxt if nxt != -1 else len(text)]
    assert "x_val" in body and "x_test" in body, \
        "_member_payload must thread x_val alongside x_test"
    assert "y_val" in body, \
        "_member_payload must thread y_val for calibration / TabPFN"


def test_predict_response_reports_predicts_at() -> None:
    """The endpoint contract is *predict for the NEXT bar*. The response
    must surface that next datetime so the caller does not have to
    re-derive it from ``at`` + ``kline_size_seconds``."""
    text = _inference_text()
    assert '"predicts_at"' in text, \
        "predict_at response must include the predicts_at field"
    assert "timedelta(seconds=" in text, \
        "predict_at must compute predicts_at = at + kline_size_seconds"


def test_cohort_endpoints_require_existing_limen_output() -> None:
    """A round has no inference surface until ``limen run`` has produced
    ``limen_output/``. Both endpoints check the directory exists and
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
