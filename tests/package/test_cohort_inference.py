"""Tests for raw_klines cohort inference (``furnace.inference``).

The behavioral test trains a real Sensor and calls the real installed
``Cohort.predict`` on real klines, so an ``@main`` Limen API drift off the
raw_klines contract fails CI loudly. Source-level checks pin that
``inference.py`` drives that API (not the removed ``_by_permutation_id`` dict
payload) and that both cohort routes stay wired to the inference module.

The ``cohort_round`` / ``fake_spot_klines`` fixtures live in ``conftest.py``;
they train a small real round on a committed BTC kline slice, offline.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

from limen.experiment.trainer.sensor import Sensor

from furnace import inference

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"


def test_inference_uses_raw_klines_sensor_api() -> None:
    """inference.py drives Limen's raw_klines API — it calls ``Cohort.predict``
    with a klines DataFrame and carries no trace of the removed dict-payload
    API — and the installed Limen itself exposes that ``raw_klines`` Sensor
    signature."""
    src = (PKG_DIR / "inference.py").read_text(encoding="utf-8")
    assert "_by_permutation_id" not in src
    assert "cohort.predict(klines)" in src
    assert "raw_klines" in str(inspect.signature(Sensor.predict))


def test_inference_requires_split_dates(tmp_path: Path) -> None:
    """A manifest without ``split_dates`` is rejected before any data pull or
    training — without the training window a request could be served a
    prediction on in-sample data."""
    metadata = {
        "yaml_reference": {
            "sfd": {
                "manifest": {
                    "data_source": {
                        "method": "limen.data.HistoricalData.get_spot_klines",
                        "params": {"kline_size": 3600},
                    },
                },
            },
        },
    }
    (tmp_path / "metadata.json").write_text(json.dumps(metadata))
    try:
        inference.assert_split_dates(tmp_path)
    except ValueError as exc:
        assert "split_dates" in str(exc)
    else:
        raise AssertionError("expected ValueError for a manifest without split_dates")


def test_predict_against_installed_limen(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
) -> None:
    """Behavioral: ``predict_at`` trains real sensors, binds a real ``Cohort``,
    and returns a real ``BarPrediction`` for an out-of-sample bar. If ``@main``
    Limen drifts off the raw_klines API this fails loudly."""
    round_dir, rids, at = cohort_round
    out = inference.predict_at(round_dir, rids, at)
    assert out["datetime"] == "2025-02-18T12:00:00+00:00"
    assert out["reason"] is None  # out-of-sample bar -> a real prediction
    assert out["prediction"] in (0, 1)
    prob = out["probability"]
    assert isinstance(prob, float)
    assert 0.0 <= prob <= 1.0
    assert out["aggregation_mode"] == "probability_weighted"
    cohort_id = out["cohort_id"]
    assert isinstance(cohort_id, str)
    assert cohort_id.startswith("sha256:")
    assert out["permutation_ids"] == list(rids)


def test_klines_window_is_json_serializable(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
) -> None:
    """``klines_window_at`` is returned to the client via ``JSONResponse``, so
    its whole payload — including ``last_bar``, a kline row that carries a
    ``datetime`` — must round-trip through ``json.dumps``. A raw datetime there
    500s the /cohort/input endpoint, which this pins."""
    round_dir, _rids, at = cohort_round
    window = inference.klines_window_at(round_dir, at)
    json.dumps(window)  # must not raise (the response is JSON-encoded)
    assert window["n_klines"] > 0
    last_bar = window["last_bar"]
    assert isinstance(last_bar, dict)
    assert isinstance(last_bar["datetime"], str)
    assert window["data_source"] == "limen.data.HistoricalData.get_spot_klines"


def test_cohort_routes_registered() -> None:
    """app.py wires both cohort endpoints to the inference module."""
    app_text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "cohort/predict" in app_text
    assert "cohort/input" in app_text
    assert "inference.predict_at(" in app_text
    assert "inference.klines_window_at(" in app_text
    assert re.search(r"from \. import [\w, ]*\binference\b", app_text)
