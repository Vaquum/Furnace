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
import time
from pathlib import Path
from typing import cast

from furnace import cohorts, prediction_cache

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


def refresh_once(
    root: Path = DATA_ROOT,
    conduit_root: Path = prediction_cache.CONDUIT_ROOT,
    arrow_root: Path = prediction_cache.ARROW_ROOT,
) -> dict[str, int]:
    """One pass: top up every graduated cohort that maps to an arrow series.
    Returns ``{cohort_name: bars_appended}``."""
    out: dict[str, int] = {}
    for cohort_def in cohorts.list_cohort_definitions(root):
        series = series_for_cohort(cohort_def)
        if series is None:
            continue
        name = cohort_def.get("name")
        out[name if isinstance(name, str) else series] = prediction_cache.top_up(
            cohort_def, series, conduit_root, arrow_root
        )
    return out


def run_forever(
    interval_seconds: int = 60,
    root: Path = DATA_ROOT,
    conduit_root: Path = prediction_cache.CONDUIT_ROOT,
    arrow_root: Path = prediction_cache.ARROW_ROOT,
) -> None:
    """Refresh every cohort's cache once per ``interval_seconds`` forever — the
    cadence the ``/opt/arrow`` store itself refreshes at."""
    while True:
        refresh_once(root, conduit_root, arrow_root)
        time.sleep(interval_seconds)
