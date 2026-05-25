"""Cohort inference endpoints — in-process, no subprocess hop.

Follows Limen's documented Cohort flow byte-for-byte:

    trainer = Trainer(limen_output_dir)
    sensors = trainer.train(permutation_ids)           # list[limen.experiment.Sensor]
    cohort = Cohort(experiment_log_path=..., permutation_ids=...)
    cohort.set_members(sensors)
    cohort.predict({'_by_permutation_id': payloads_by_pid})

Every callable on that path is a real Limen type. We do not construct
``Sensor`` ourselves, we do not train outside ``Trainer``, we do not
aggregate outside ``Cohort``. The two public functions —
``input_payloads_at`` and ``predict_at`` — differ only in whether
``Trainer.train(...)`` runs: ``/cohort/input`` reuses the manifest's
data pipeline to fish out the row at the requested datetime,
``/cohort/predict`` adds the train + cohort steps on top.

Inference requires both ``start_date_limit`` and ``end_date_limit``
on the manifest's data source. An open-ended manifest would let a
request fetch an unbounded series, so we reject it before any
Trainer / Cohort work runs.

Sensors are reconstructed anew on every call — no in-process cache."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import polars as pl
from limen.cohort import Cohort
from limen.experiment import Sensor, Trainer


class _ManifestSurface(Protocol):
    """The subset of Limen's MLManifest we touch. Declared here so we
    cross the Limen-typing boundary once, via a typed cast, and the
    rest of the module stays under strict pyright with no Unknowns."""

    def prepare_data(
        self,
        raw_data: object,
        round_params: dict[str, object],
    ) -> dict[str, object]: ...


class _CohortPredictor(Protocol):
    """Cohort.predict is typed as ``dict | tuple`` with unparameterised
    members; pyright reports the call site as partially unknown. We
    cast to this Protocol where we call predict (without the tuple-
    return variants) so the return is ``object`` and gets narrowed
    through the same JSON gate the rest of the module uses."""

    def predict(self, X: dict[str, object]) -> object: ...


def _loads_object(text: str) -> object:
    """``json.loads`` is typed as ``Any``; the rest of the module accepts
    only ``object`` and isinstance-narrows from there, so we wrap the
    boundary in a single helper instead of sprinkling unchecked
    ``Any``-flavoured locals through the call graph."""
    decoded: object = json.loads(text)
    return decoded


def _as_object_dict(value: object, where: str) -> dict[str, object]:
    """Narrow ``object`` to ``dict[str, object]`` or raise. ``isinstance(x,
    dict)`` only narrows to a bare ``dict`` (Unknown-keyed under strict
    pyright), so we cast the runtime-narrowed value to
    ``dict[object, object]`` and walk it explicitly."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} is not a JSON object")
    narrowed = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for key, val in narrowed.items():
        if not isinstance(key, str):
            raise ValueError(f"{where} contains a non-string key: {key!r}")
        out[key] = val
    return out


def _read_metadata(limen_output_dir: Path) -> dict[str, object]:
    metadata_path = limen_output_dir / "metadata.json"
    text = metadata_path.read_text(encoding="utf-8")
    return _as_object_dict(_loads_object(text), str(metadata_path))


def _data_source_params(metadata: dict[str, object]) -> dict[str, object]:
    yaml_ref = metadata.get("yaml_reference")
    yaml_ref_dict = _as_object_dict(yaml_ref, "metadata.yaml_reference")
    sfd = _as_object_dict(yaml_ref_dict.get("sfd"), "metadata.yaml_reference.sfd")
    manifest_cfg = _as_object_dict(
        sfd.get("manifest"), "metadata.yaml_reference.sfd.manifest"
    )
    data_source = _as_object_dict(
        manifest_cfg.get("data_source"),
        "metadata.yaml_reference.sfd.manifest.data_source",
    )
    return _as_object_dict(
        data_source.get("params"),
        "metadata.yaml_reference.sfd.manifest.data_source.params",
    )


def assert_bounded_window(limen_output_dir: Path) -> tuple[str, str]:
    """Reject manifests without a closed
    ``[start_date_limit, end_date_limit]`` window. The two literals
    are returned so callers can surface them in responses or errors."""
    params = _data_source_params(_read_metadata(limen_output_dir))
    start = params.get("start_date_limit")
    end = params.get("end_date_limit")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError(
            "inference requires the manifest's data_source.params to set "
            "BOTH start_date_limit and end_date_limit; "
            f"got start_date_limit={start!r}, end_date_limit={end!r}"
        )
    return start, end


def _kline_size_seconds(limen_output_dir: Path) -> int:
    params = _data_source_params(_read_metadata(limen_output_dir))
    kline = params.get("kline_size")
    if not isinstance(kline, int):
        raise ValueError(
            "manifest data_source.params.kline_size must be an int (seconds); "
            f"got {kline!r}"
        )
    return kline


def _load_round_params(
    limen_output_dir: Path,
    permutation_ids: list[int],
) -> dict[int, dict[str, object]]:
    path = limen_output_dir / "round_data.jsonl"
    entries: dict[int, dict[str, object]] = {}
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            obj = _loads_object(line)
            if not isinstance(obj, dict):
                continue
            row = cast(dict[object, object], obj)
            rid = row.get("round_id")
            rp = row.get("round_params")
            if isinstance(rid, int) and isinstance(rp, dict):
                params = cast(dict[object, object], rp)
                rp_out: dict[str, object] = {}
                for k, v in params.items():
                    if isinstance(k, str):
                        rp_out[k] = v
                entries[rid] = rp_out
    missing = [pid for pid in permutation_ids if pid not in entries]
    if missing:
        raise ValueError(
            f"permutation ids missing from round_data.jsonl: {missing}"
        )
    return {pid: entries[pid] for pid in permutation_ids}


def _parse_at(value: str) -> datetime:
    """Parse the ``at`` query parameter as ISO 8601. Limen klines are
    timezone-naive UTC; we coerce any tz-aware input to naive UTC so
    the equality check against the test window's datetimes succeeds."""
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _as_dataframe(value: object, where: str) -> pl.DataFrame:
    if not isinstance(value, pl.DataFrame):
        raise ValueError(f"{where} is not a polars DataFrame")
    return value


def _row_at(prepared: dict[str, object], at: datetime) -> pl.DataFrame:
    """Limen's MLManifest.prepare_data ships ``price_data_for_backtest``
    whose ``datetime`` series is row-aligned with ``x_test`` after every
    transform and NaN drop. That series is the only reliable map from
    row index back to datetime once the standalone ``datetime`` column
    has been dropped from the splits."""
    pdb_raw = prepared.get("price_data_for_backtest")
    if pdb_raw is None:
        raise ValueError(
            "manifest's prepare_data did not produce price_data_for_backtest; "
            "inference at a chosen datetime requires the OHLCV columns so "
            "the row-to-datetime mapping survives the transform drops"
        )
    pdb = _as_dataframe(pdb_raw, "price_data_for_backtest")
    if "datetime" not in pdb.columns:
        raise ValueError("price_data_for_backtest has no 'datetime' column")
    indexed = pdb.with_row_index("_row_idx").filter(
        pl.col("datetime") == pl.lit(at)
    )
    if indexed.height == 0:
        first = pdb["datetime"].min()
        last = pdb["datetime"].max()
        raise ValueError(
            f"datetime {at.isoformat()} is not in the test window of this "
            f"round (test runs from {first} to {last}); pick a datetime "
            "inside that window"
        )
    if indexed.height > 1:
        raise ValueError(
            f"datetime {at.isoformat()} matched {indexed.height} rows in "
            "the test window — round data is not unique-keyed by datetime"
        )
    idx_value = indexed["_row_idx"][0]
    if not isinstance(idx_value, int):
        raise ValueError(f"row index returned non-int: {idx_value!r}")
    x_test = _as_dataframe(prepared.get("x_test"), "x_test")
    if idx_value >= x_test.height:
        raise ValueError(
            f"row index {idx_value} out of range for x_test "
            f"(height={x_test.height})"
        )
    return x_test.slice(idx_value, 1)


def _prepared_for_round(
    trainer: Trainer,
    round_params: dict[str, object],
) -> dict[str, object]:
    """Re-run the manifest's ``prepare_data`` for one permutation. Limen's
    own Cohort docs example threads ``trainer._manifest`` /
    ``trainer._data`` through ``prepare_data`` — we go through
    ``getattr`` so the access is a string-keyed attribute lookup
    (pyright's ``reportPrivateUsage`` is name-keyed, not type-keyed),
    then narrow to ``_ManifestSurface`` so the call site itself is
    typed."""
    manifest: _ManifestSurface = cast(_ManifestSurface, getattr(trainer, "_manifest"))
    raw_data: object = getattr(trainer, "_data")
    prepared_obj: object = manifest.prepare_data(raw_data, round_params)
    return _as_object_dict(prepared_obj, "manifest.prepare_data result")


def _row_to_records(row: pl.DataFrame) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw in row.iter_rows(named=True):
        narrowed = cast(dict[object, object], raw)
        records.append({str(k): v for k, v in narrowed.items()})
    return records


def _scalar_or_list(payload: object) -> object:
    """Cohort returns numpy arrays for ``_preds`` and ``_probs``. For a
    one-row inference call we want a plain scalar; otherwise we hand
    back a Python list. ``numpy.ndarray.item()`` is the dedicated
    scalar extractor and raises for size != 1, so it doubles as the
    branch guard."""
    if not isinstance(payload, np.ndarray):
        return payload
    raw_list = cast(list[object], payload.tolist()) if payload.size > 1 else None
    if raw_list is None:
        return cast(object, payload.item())
    return raw_list


def input_payloads_at(
    limen_output_dir: Path,
    permutation_ids: list[int],
    at: str,
) -> dict[str, object]:
    """Return the per-permutation ``x_test`` row the cohort would feed
    its members at ``at``. No training runs — this only reuses the
    manifest's ``prepare_data`` pipeline."""
    assert_bounded_window(limen_output_dir)
    when = _parse_at(at)
    round_params_by_pid = _load_round_params(limen_output_dir, permutation_ids)
    trainer = Trainer(limen_output_dir)
    payloads: dict[str, list[dict[str, object]]] = {}
    for pid in permutation_ids:
        prepared = _prepared_for_round(trainer, round_params_by_pid[pid])
        row = _row_at(prepared, when)
        payloads[str(pid)] = _row_to_records(row)
    return {
        "at": when.isoformat(),
        "permutation_ids": list(permutation_ids),
        "payloads": payloads,
    }


def predict_at(
    limen_output_dir: Path,
    permutation_ids: list[int],
    at: str,
) -> dict[str, object]:
    """Train sensors, build a cohort over the requested permutations,
    feed it per-permutation payloads at ``at``, return the aggregated
    prediction. The sequencing — Trainer → Sensors → Cohort.set_members
    → Cohort.predict — is the Limen Cohort docs flow verbatim."""
    assert_bounded_window(limen_output_dir)
    when = _parse_at(at)
    round_params_by_pid = _load_round_params(limen_output_dir, permutation_ids)
    trainer = Trainer(limen_output_dir)
    sensors: list[Sensor] = trainer.train(permutation_ids)

    payloads_by_pid: dict[int, dict[str, object]] = {}
    for pid in permutation_ids:
        prepared = _prepared_for_round(trainer, round_params_by_pid[pid])
        row = _row_at(prepared, when)
        payloads_by_pid[pid] = {"x_test": row}

    cohort = Cohort(
        experiment_log_path=str(limen_output_dir),
        permutation_ids=list(permutation_ids),
    )
    cohort.set_members(sensors)
    # Cohort.predict's signature is ``dict | tuple`` with unparameterised
    # members; we never request the tuple-return variants, so the
    # runtime is always a dict. Cross the Limen-typing boundary through
    # ``_CohortPredictor`` so the return is ``object`` and we narrow
    # via the same gate JSON payloads go through.
    predictor = cast(_CohortPredictor, cohort)
    result_obj: object = predictor.predict({"_by_permutation_id": payloads_by_pid})
    result = _as_object_dict(result_obj, "Cohort.predict result")

    body: dict[str, object] = {
        "at": when.isoformat(),
        "kline_size_seconds": _kline_size_seconds(limen_output_dir),
        "permutation_ids": list(permutation_ids),
        "aggregation_mode": cohort.aggregation_mode,
        "prediction": _scalar_or_list(result.get("_preds")),
    }
    probs = result.get("_probs")
    if probs is not None:
        body["probability"] = _scalar_or_list(probs)
    return body
