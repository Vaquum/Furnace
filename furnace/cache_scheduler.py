"""Per-minute Conduit prediction-cache top-up scheduler (PRD slice #71).

Mirrors the ``/opt/arrow`` store's once-a-minute refresh: every interval, for
each graduated cohort, append the newest closed bar(s) to its
``/opt/conduit/<series>/latest.arrow`` via ``prediction_cache.top_up``.
Deterministic and idempotent — a missed tick self-corrects on the next run.

A cohort's series is read from its round manifest's ``get_arrow_file`` data
source (the ``/opt/arrow/<series>`` the cohort trains and predicts over); a
cohort whose data source is not an arrow series is skipped — there is no
``/opt/arrow`` file to top up from.

No ``limen`` import here (only ``furnace.prediction_cache`` /
``furnace.cohorts``), so this module adds nothing to the pyright stub budget.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import polars as pl

from furnace import cohorts, prediction_cache

logger = logging.getLogger(__name__)

DATA_ROOT = Path("/data/experiments")

# yaml_reference.sfd.manifest.data_source.params.file_path
_FILE_PATH_KEYS = ("yaml_reference", "sfd", "manifest", "data_source", "params", "file_path")


def series_for_cohort(cohort_def: dict[str, object]) -> str | None:
    """The ``/opt/arrow`` series a cohort predicts over, read from its round's
    ``metadata.json`` ``data_source.file_path`` (``…/opt/arrow/<series>/latest.arrow``
    → ``<series>``). Returns ``None`` when the cohort's data source is not an
    arrow series (e.g. get_spot_klines / get_spot_dollar_klines)."""
    experiment_dir = cohort_def.get("experiment_dir")
    if not isinstance(experiment_dir, str):
        return None
    metadata_path = Path(experiment_dir) / "metadata.json"
    if not metadata_path.is_file():
        return None
    node: object = json.loads(metadata_path.read_text(encoding="utf-8"))
    for key in _FILE_PATH_KEYS:
        if not isinstance(node, dict):
            return None
        node = cast(dict[object, object], node).get(key)
    if not isinstance(node, str) or "arrow" not in node:
        return None
    return Path(node).parent.name


def _manifest_entry(
    cohort_def: dict[str, object], name: str, series: str, conduit_root: Path
) -> dict[str, object]:
    """One serving-manifest entry, read back from the payload ``top_up`` just
    wrote (an mmap read, not a recompute) so the manifest can never describe
    rows the file does not hold."""
    payload = pl.read_ipc(prediction_cache.cache_path(series, conduit_root), memory_map=True)
    max_ts = payload.get_column("ts").max() if payload.height else None
    return {
        "cohort_id": str(cohort_def.get("cohort_id", "")),
        "name": name,
        "path": f"{series}/latest.arrow",
        "rows": payload.height,
        "max_ts": cast(int | None, max_ts),
    }


def _write_serving_manifest(series_entries: dict[str, dict[str, object]], conduit_root: Path) -> None:
    """Atomically (re)write ``serving_manifest.json`` — the consumer-facing
    liveness authority. Fail-closed: only this tick's successful active
    refreshes are listed, so an inactive cohort's retained file, a failed
    refresh, or a cohort turned off simply does not appear, whatever is on
    disk."""
    manifest: dict[str, object] = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "series": series_entries,
    }
    path = prediction_cache.serving_manifest_path(conduit_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(path)


def refresh_once(
    root: Path = DATA_ROOT,
    conduit_root: Path = prediction_cache.CONDUIT_ROOT,
    arrow_root: Path = prediction_cache.ARROW_ROOT,
) -> dict[str, int]:
    """One pass: top up every graduated cohort that maps to an arrow series.
    Returns ``{cohort_name: rows}`` for the cohorts that succeeded — ``rows``
    is the bars appended that tick, or the file's full row count when the
    cohort's cache was (re)built from scratch (first serve / cohort switch). A
    single cohort raising (a round caught mid-write, a corrupt sensor, a full
    disk) is logged with its traceback and skipped — it must not starve every
    other cohort of its refresh this tick. The failure is loud (``logger`` at
    error level), not swallowed. After the pass the serving manifest is
    rewritten with exactly the series that refreshed successfully."""
    out: dict[str, int] = {}
    served: dict[str, dict[str, object]] = {}
    for cohort_def in cohorts.list_cohort_definitions(root):
        if not cohort_def.get("active", False):
            continue  # only an operator-activated cohort is served
        series = series_for_cohort(cohort_def)
        if series is None:
            continue
        name_obj = cohort_def.get("name")
        name = name_obj if isinstance(name_obj, str) else series
        if series in served:
            # Two active cohorts on one series can only arise from out-of-band
            # definition edits (the activation route flips same-series peers
            # off). Served alternately they would trade the identity sidecar
            # and full-rebuild the file every tick — skip the latecomer loudly
            # instead of entering that rebuild storm.
            logger.error(
                "cohort %r maps to series %r already served by %r this tick; "
                "skipping it — deactivate one of the two (only the activation "
                "route keeps one cohort active per series)",
                name,
                series,
                served[series]["name"],
            )
            continue
        try:
            out[name] = prediction_cache.top_up(cohort_def, series, conduit_root, arrow_root)
            served[series] = _manifest_entry(cohort_def, name, series, conduit_root)
        except Exception:
            logger.exception(
                "prediction-cache refresh failed for cohort %r (series %r) — in top_up or "
                "in the manifest read-back; skipping it this tick so the rest of the cache "
                "surface still refreshes and the manifest still publishes",
                name,
                series,
            )
            continue
    _write_serving_manifest(served, conduit_root)
    return out


def run_forever(
    interval_seconds: int = 60,
    root: Path = DATA_ROOT,
    conduit_root: Path = prediction_cache.CONDUIT_ROOT,
    arrow_root: Path = prediction_cache.ARROW_ROOT,
) -> None:
    """Refresh every cohort's cache once per ``interval_seconds`` forever — the
    cadence the ``/opt/arrow`` store itself refreshes at. Each tick logs how many
    bars it appended and how long it took, and warns when a tick overruns the
    interval (the cache is then lagging the store rather than mirroring it). A
    tick that raises is logged and retried on the next interval rather than
    killing the daemon — a dead scheduler tops up nothing until someone notices;
    ``KeyboardInterrupt`` / ``SystemExit`` still propagate for a clean shutdown.
    The sleep is measured from the tick's start, so the cadence stays
    ``interval_seconds`` instead of drifting to ``interval_seconds + work``."""
    while True:
        start = time.monotonic()
        appended: dict[str, int]
        try:
            appended = refresh_once(root, conduit_root, arrow_root)
        except Exception:
            logger.exception("prediction-cache refresh tick failed; retrying after the interval")
            appended = {}
        elapsed = time.monotonic() - start
        logger.info(
            "prediction-cache tick: %d cohort(s), %d bar(s) appended in %.1fs",
            len(appended),
            sum(appended.values()),
            elapsed,
        )
        if elapsed > interval_seconds:
            logger.warning(
                "prediction-cache tick took %.1fs, over the %ds interval — the cache is "
                "lagging the /opt/arrow cadence (top_up re-predicts each cohort's full "
                "history per tick; bound the prediction window if this persists)",
                elapsed,
                interval_seconds,
            )
        time.sleep(max(0.0, interval_seconds - elapsed))
