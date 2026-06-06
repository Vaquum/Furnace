"""Tests for ``furnace.sensor_registry`` — version-keyed Sensor pickle
persistence and regenerate-on-mismatch reload.

Sensors are trained on a committed real BTC kline slice via the shared
``cohort_round`` / ``fake_spot_klines`` fixtures (see ``conftest.py``), so the
suite exercises a real Limen Sensor offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
from limen.experiment.trainer import Trainer

from furnace import sensor_registry


def test_pickle_roundtrip_is_bit_identical(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
) -> None:
    """Persisting and reloading a Sensor yields bit-identical predictions — the
    model and fitted params survive the round-trip exactly."""
    round_dir, rids, _at = cohort_round
    sensor = Trainer(round_dir).train(rids)[0]
    before = sensor.predict_all(frozen_klines)

    pid = sensor_registry.persist_sensor(sensor, tmp_path)
    reloaded = sensor_registry.load_sensor(tmp_path, pid)
    assert reloaded is not None
    after = reloaded.predict_all(frozen_klines)

    assert before == after


def test_manifest_is_nulled_before_pickle(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
) -> None:
    """persist_sensor nulls the lazy ``_manifest`` (an unpicklable
    ``CompiledSFD`` closure) before pickling; predicting first materializes it,
    and the persist must clear it so stdlib pickle succeeds."""
    round_dir, rids, _at = cohort_round
    sensor = Trainer(round_dir).train(rids)[0]
    sensor.predict_all(frozen_klines)  # materializes the lazy _manifest
    assert getattr(sensor, "_manifest") is not None

    sensor_registry.persist_sensor(sensor, tmp_path)
    assert getattr(sensor, "_manifest") is None
    assert (tmp_path / f"{sensor.permutation_id}.pkl").is_file()


def test_version_mismatch_triggers_regenerate(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    tmp_path: Path,
) -> None:
    """A sidecar whose recorded version differs from the running env makes
    ``load_sensor`` return ``None`` (regenerate signal), not a stale pickle."""
    round_dir, rids, _at = cohort_round
    sensor = Trainer(round_dir).train(rids)[0]
    pid = sensor_registry.persist_sensor(sensor, tmp_path)

    sidecar = tmp_path / f"{pid}.json"
    data = json.loads(sidecar.read_text())
    data["limen_version"] = "0.0.0-stale"
    sidecar.write_text(json.dumps(data))

    assert sensor_registry.load_sensor(tmp_path, pid) is None


def test_sidecar_permutation_id_mismatch_returns_none(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    tmp_path: Path,
) -> None:
    """A sidecar whose recorded permutation_id does not match the requested one
    (stale/corrupted store) makes ``load_sensor`` return ``None`` rather than
    unpickling the paired — possibly wrong — sensor."""
    round_dir, rids, _at = cohort_round
    sensor = Trainer(round_dir).train(rids)[0]
    pid = sensor_registry.persist_sensor(sensor, tmp_path)

    sidecar = tmp_path / f"{pid}.json"
    data = json.loads(sidecar.read_text())
    data["permutation_id"] = "some-other-round-id"
    sidecar.write_text(json.dumps(data))

    assert sensor_registry.load_sensor(tmp_path, pid) is None
