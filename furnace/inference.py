"""Cohort inference — drive Limen's raw_klines Sensor/Cohort API in-process.

Given a completed round, a set of permutation_ids (Limen round_ids), and a
datetime ``at``, pull a bounded klines window ending at ``at`` from the
manifest's declared data_source, rebuild the member Sensors via
``Trainer.train``, bind them into a ``Cohort``, and return the cohort's
``BarPrediction`` for the bar at ``at``.

The flow is Limen's documented inference path verbatim (see Limen's own
``test_e2e_loop_trainer_sensor``)::

    trainer = Trainer(limen_output_dir)
    sensors = trainer.train(permutation_ids)        # list[Sensor]
    cohort  = Cohort(experiment_log_path=..., permutation_ids=...)
    cohort.set_members(sensors)
    bar = cohort.predict(raw_klines)                # raw_klines: pl.DataFrame -> BarPrediction

``Sensor.predict`` / ``Cohort.predict`` take a raw klines DataFrame (the
same schema the manifest's data_source produces) and return a
``BarPrediction`` — the Sensor computes its own features through the
compiled manifest, so we never touch ``prepare_data``. Sensors are rebuilt
on every call; consumer-side persistence is ``furnace.sensor_registry``'s
concern, wired into the serving path by a later slice.

Inference requires the manifest to declare ``split_dates`` — without the
training window the Sensor cannot mask in-sample bars, so a request could
silently be served a prediction on data the model trained on. A manifest
without ``split_dates`` is rejected before any data pull or training.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import polars as pl
from limen.cohort import Cohort
from limen.data import HistoricalData
from limen.experiment import Trainer

# Bars of history pulled before ``at`` so the manifest's indicators clear
# their warm-up before the bar we score. Generous for the canonical feature
# set (long-period indicators, fractional differencing); pulling more is
# cheap because get_spot_klines resolves one dataset snapshot regardless of
# the window and ``Cohort.predict`` scores only the last bar. If a manifest
# needs a longer warm-up than this, the bar at ``at`` returns
# ``reason='warm-up'`` honestly rather than a fabricated value.
_WARMUP_BARS = 2048

# The one data_source this slice serves. Limen's own inference tests call
# get_spot_klines directly; per the one-bar-type-per-cohort decision we
# accept exactly this method and fail loud on anything else (dollar bars,
# arrow files) rather than silently substituting a different source.
_SUPPORTED_DATA_SOURCE = "limen.data.HistoricalData.get_spot_klines"

_DATE_FMT = "%Y-%m-%d %H:%M:%S"


def _loads_object(text: str) -> object:
    """``json.loads`` is typed ``Any``; the module narrows from ``object``
    via isinstance, so the boundary is wrapped once here."""
    decoded: object = json.loads(text)
    return decoded


def _as_object_dict(value: object, where: str) -> dict[str, object]:
    """Narrow ``object`` to ``dict[str, object]`` or raise. ``isinstance(x,
    dict)`` narrows only to a bare ``dict`` under strict pyright, so cast the
    runtime-narrowed value to ``dict[object, object]`` and walk it."""
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


def _manifest_cfg(metadata: dict[str, object]) -> dict[str, object]:
    yaml_ref = _as_object_dict(metadata.get("yaml_reference"), "metadata.yaml_reference")
    sfd = _as_object_dict(yaml_ref.get("sfd"), "metadata.yaml_reference.sfd")
    return _as_object_dict(sfd.get("manifest"), "metadata.yaml_reference.sfd.manifest")


def _data_source(metadata: dict[str, object]) -> dict[str, object]:
    return _as_object_dict(
        _manifest_cfg(metadata).get("data_source"),
        "metadata.yaml_reference.sfd.manifest.data_source",
    )


def _split_dates(metadata: dict[str, object]) -> dict[str, object]:
    """Extract and validate ``split_dates`` from already-read metadata. The
    training window is what lets the Sensor mark a requested datetime as
    in-sample (``reason='inside-training-window'``); without it inference could
    serve a prediction on data the model trained on."""
    split_obj = _manifest_cfg(metadata).get("split_dates")
    if split_obj is None:
        raise ValueError(
            "inference requires the manifest to declare split_dates so the "
            "requested datetime can be confirmed out-of-sample; this "
            "manifest has none"
        )
    split = _as_object_dict(split_obj, "metadata.yaml_reference.sfd.manifest.split_dates")
    if not split:
        raise ValueError("manifest split_dates is empty; inference needs a training window")
    return split


def assert_split_dates(limen_output_dir: Path) -> dict[str, object]:
    """``_split_dates`` against a round dir — reads ``metadata.json`` once.
    Used where only the directory is at hand (the route guard, the test)."""
    return _split_dates(_read_metadata(limen_output_dir))


def _kline_size_seconds(metadata: dict[str, object]) -> int:
    params = _as_object_dict(
        _data_source(metadata).get("params"),
        "metadata.yaml_reference.sfd.manifest.data_source.params",
    )
    kline = params.get("kline_size")
    if not isinstance(kline, int) or isinstance(kline, bool):
        raise ValueError(
            "manifest data_source.params.kline_size must be an int (seconds); "
            f"got {kline!r}"
        )
    return kline


def _data_source_method(metadata: dict[str, object]) -> str:
    method = _data_source(metadata).get("method")
    if not isinstance(method, str):
        raise ValueError(
            f"manifest data_source.method must be a string; got {method!r}"
        )
    return method


def _parse_at(value: str) -> datetime:
    """Parse the ``at`` query parameter as ISO 8601 and return a tz-aware UTC
    datetime. Limen normalises kline ``datetime`` to ``Datetime('μs','UTC')``;
    a naive ``at`` is interpreted as UTC."""
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: object) -> str | None:
    """Render a klines/BarPrediction datetime (a python ``datetime`` once
    Polars hands it back, or ``None``) as an ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fetch_klines(
    metadata: dict[str, object],
    end: datetime,
    kline_size: int,
) -> pl.DataFrame:
    """Pull the klines window ``[end - _WARMUP_BARS*kline_size, end]`` from the
    manifest's declared data_source. Only get_spot_klines is served for now;
    any other declared method fails loud."""
    method = _data_source_method(metadata)
    if method != _SUPPORTED_DATA_SOURCE:
        raise ValueError(
            f"inference currently serves {_SUPPORTED_DATA_SOURCE} only; this "
            f"manifest's data_source.method is {method!r}"
        )
    start = end - timedelta(seconds=_WARMUP_BARS * kline_size)
    return HistoricalData().get_spot_klines(
        kline_size=kline_size,
        start_date_limit=start.strftime(_DATE_FMT),
        end_date_limit=end.strftime(_DATE_FMT),
    )


def klines_window_at(limen_output_dir: Path, at: str) -> dict[str, object]:
    """Return the raw klines window the cohort would consume to predict the
    bar at ``at`` — the window bounds, bar count, columns, and the final bar
    (the one whose features drive the prediction). No training runs."""
    metadata = _read_metadata(limen_output_dir)
    split = _split_dates(metadata)
    kline_size = _kline_size_seconds(metadata)
    when = _parse_at(at)
    klines = _fetch_klines(metadata, when, kline_size)
    dt_col = klines.get_column("datetime") if "datetime" in klines.columns else None
    last_bar: dict[str, object] = {}
    if klines.height:
        for raw in klines.tail(1).iter_rows(named=True):
            narrowed = cast(dict[object, object], raw)
            # The row's ``datetime`` value is a python datetime, which the
            # JSONResponse encoder cannot serialize — render it ISO like every
            # other datetime in this payload. The remaining kline columns are
            # plain float/int, already JSON-native.
            last_bar = {
                str(k): (_iso(v) if isinstance(v, datetime) else v)
                for k, v in narrowed.items()
            }
    return {
        "at": when.isoformat(),
        "kline_size_seconds": kline_size,
        "data_source": _data_source_method(metadata),
        "split_dates": split,
        "n_klines": klines.height,
        "window_start": _iso(dt_col.min()) if dt_col is not None else None,
        "window_end": _iso(dt_col.max()) if dt_col is not None else None,
        "columns": list(klines.columns),
        "last_bar": last_bar,
    }


def predict_at(
    limen_output_dir: Path,
    permutation_ids: list[str],
    at: str,
) -> dict[str, object]:
    """Train the member sensors, bind a cohort over the requested
    permutations, and return the cohort's aggregated ``BarPrediction`` for the
    bar at ``at``. Sequencing — Trainer → Sensors → Cohort.set_members →
    Cohort.predict(klines) — is the Limen inference flow verbatim."""
    metadata = _read_metadata(limen_output_dir)
    _split_dates(metadata)  # guard: require a training window before any work
    kline_size = _kline_size_seconds(metadata)
    when = _parse_at(at)
    klines = _fetch_klines(metadata, when, kline_size)

    sensors = Trainer(limen_output_dir).train(permutation_ids)

    cohort = Cohort(
        experiment_log_path=str(limen_output_dir),
        permutation_ids=list(permutation_ids),
    )
    cohort.set_members(sensors)
    bar = cohort.predict(klines)

    return {
        "at": when.isoformat(),
        "datetime": _iso(bar.datetime),
        "prediction": bar.prediction,
        "probability": bar.probability,
        "reason": bar.reason,
        "permutation_ids": list(permutation_ids),
        "aggregation_mode": cohort.aggregation_mode,
        "cohort_id": cohort.cohort_id,
        "kline_size_seconds": kline_size,
        "n_klines": klines.height,
    }
