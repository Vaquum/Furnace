"""Cohort inference endpoint glue.

Validates the round's manifest — every supported manifest pins BOTH
``start_date_limit`` and ``end_date_limit`` on its data source, so an
inference call always works against a bounded series — and then shells
out to ``scripts/cohort_inference.py`` to do the actual Trainer +
Cohort work.

The helper script lives outside the typed ``furnace/`` package so it
can import ``limen`` and ``polars`` at module scope without growing the
pyright budget. Furnace already pays this hop for ``limen run`` and
``limen validate``; this module is the same pattern for the
cohort-inference surface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent
HELPER_SCRIPT = REPO_ROOT / "scripts" / "cohort_inference.py"


def _loads_object(text: str) -> object:
    """``json.loads`` returns ``Any`` per typeshed. Wrap it in a helper
    that returns ``object`` so callers must isinstance-narrow before
    use — keeps the inference module pyright-clean even though JSON
    payloads are dynamic by definition."""
    decoded: object = json.loads(text)
    return decoded


def _as_object_dict(value: object, where: str) -> dict[str, object]:
    """Narrow ``object`` to ``dict[str, object]`` or raise. Used at the
    boundary where we accept JSON payloads. Path context is included
    in the error so callers can tell which file or response failed.
    The intermediate ``cast`` to ``dict[object, object]`` is required
    because ``isinstance(x, dict)`` only narrows to ``dict`` without
    parameters, which strict pyright treats as Unknown-keyed/valued."""
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
    ``[start_date_limit, end_date_limit]`` window. An open-ended manifest
    would let an inference call fetch an unbounded series — neither
    deterministic nor fast enough to serve a request. The two literals
    are returned so callers can surface them in responses or error
    messages."""
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


def _run_helper(
    limen_output_dir: Path,
    permutation_ids: list[int],
    at: str,
    mode: str,
) -> dict[str, object]:
    if not HELPER_SCRIPT.is_file():
        raise FileNotFoundError(
            f"cohort inference helper not found at {HELPER_SCRIPT}"
        )
    cmd = [
        sys.executable,
        str(HELPER_SCRIPT),
        "--limen-output", str(limen_output_dir),
        "--permutation-ids", ",".join(str(p) for p in permutation_ids),
        "--at", at,
        "--mode", mode,
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cohort inference helper exited {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return _as_object_dict(
        _loads_object(completed.stdout),
        "cohort inference helper stdout",
    )


def input_payloads_at(
    limen_output_dir: Path,
    permutation_ids: list[int],
    at: str,
) -> dict[str, object]:
    """Return the per-permutation x_test payloads the cohort would feed
    to its members at ``at``. The response shape matches what the
    /cohort/input endpoint emits."""
    assert_bounded_window(limen_output_dir)
    return _run_helper(limen_output_dir, permutation_ids, at, "input")


def predict_at(
    limen_output_dir: Path,
    permutation_ids: list[int],
    at: str,
) -> dict[str, object]:
    """Train sensors, build a cohort over the requested permutations,
    feed it per-permutation payloads at ``at``, and return the
    aggregated prediction. The response shape matches what the
    /cohort/predict endpoint emits."""
    assert_bounded_window(limen_output_dir)
    return _run_helper(limen_output_dir, permutation_ids, at, "predict")
