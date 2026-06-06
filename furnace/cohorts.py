"""Cohort definitions — the persisted record of which decoders (one
manifest) form a named cohort, written by the graduation view.

A cohort definition is a flat JSON file at ``<root>/cohorts/<cohort_id>.json``
(Furnace has no DB; this matches the existing flatfile state model). The
``cohort_id`` is computed exactly as Limen's ``Cohort`` computes it
(``limen.cohort.Cohort.set_members``) so a Furnace-written definition and a
Limen-built cohort agree on identity — ``test_cohort_graduation`` pins the
formula against a known hash so any Limen drift is caught.

This module is deliberately free of any ``limen`` import: graduation
persists *which permutations* form a cohort (the manifest_id + ids), not
trained sensors — sensor materialization is a separate slice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

# Mirror of limen.yaml.store._SHA256_PREFIX — the prefix Limen puts on every
# content-hash id (manifest_id, cohort_id). Pinned by test_cohort_graduation.
_SHA256_PREFIX = "sha256:"


def _as_object_dict(value: object, where: str) -> dict[str, object]:
    """Narrow a parsed-JSON ``object`` to ``dict[str, object]`` or raise.
    ``isinstance(x, dict)`` only narrows to a bare ``dict`` under strict
    pyright, so cast to ``dict[object, object]`` and walk it explicitly."""
    if not isinstance(value, dict):
        raise ValueError(f"{where} is not a JSON object")
    narrowed = cast(dict[object, object], value)
    out: dict[str, object] = {}
    for key, val in narrowed.items():
        out[str(key)] = val
    return out


def compute_cohort_id(manifest_id: str, permutation_ids: list[str]) -> str:
    """Replicate ``limen.cohort.Cohort.set_members``'s cohort_id: the
    ``sha256:``-prefixed SHA-256 of the canonical ``{manifest_id,
    sorted permutation_ids}`` JSON payload. Kept byte-identical to Limen so
    a Furnace cohort definition and a Limen-built Cohort share one id."""
    payload = json.dumps(
        {"manifest_id": manifest_id, "permutation_ids": sorted(permutation_ids)},
        sort_keys=True,
    )
    return f"{_SHA256_PREFIX}{hashlib.sha256(payload.encode()).hexdigest()}"


def _cohorts_dir(root: Path) -> Path:
    return root / "cohorts"


def read_manifest_id(limen_output_dir: Path) -> str:
    """Read the round's ``manifest_id`` from ``limen_output/metadata.json``.
    Limen >= 3.13 writes it; a round without it cannot be graduated (the
    cohort grouping key would be ambiguous), so this fails loud rather than
    inventing one."""
    metadata_path = limen_output_dir / "metadata.json"
    loaded: object = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata = _as_object_dict(loaded, str(metadata_path))
    manifest_id = metadata.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id:
        raise ValueError(
            f"{metadata_path} has no string 'manifest_id' — graduate rounds run "
            "with Limen >= 3.13, which records the manifest content hash"
        )
    return manifest_id


def write_cohort_definition(
    root: Path,
    *,
    name: str,
    experiment_dir: str,
    manifest_id: str,
    permutation_ids: list[str],
) -> str:
    """Write a cohort definition to ``<root>/cohorts/<cohort_id>.json`` and
    return its ``cohort_id``. The id is derived from ``manifest_id`` and the
    permutation set, so re-graduating the same set is idempotent (same file).
    The ``:`` in the id is filesystem-folded to ``_`` for the filename; the
    canonical ``sha256:``-prefixed id is preserved inside the JSON."""
    if not name:
        raise ValueError("cohort name must be non-empty")
    if not permutation_ids:
        raise ValueError("cohort must have at least one permutation_id")
    cohort_id = compute_cohort_id(manifest_id, permutation_ids)
    cohorts_dir = _cohorts_dir(root)
    cohorts_dir.mkdir(parents=True, exist_ok=True)
    definition = {
        "cohort_id": cohort_id,
        "name": name,
        "experiment_dir": experiment_dir,
        "manifest_id": manifest_id,
        "permutation_ids": sorted(permutation_ids),
    }
    path = cohorts_dir / f"{_filename_for(cohort_id)}.json"
    path.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    return cohort_id


def _filename_for(cohort_id: str) -> str:
    return cohort_id.replace(":", "_")


def list_cohort_definitions(root: Path) -> list[dict[str, object]]:
    """Return every cohort definition under ``<root>/cohorts/``, newest
    first. A parse error is not swallowed — a definition this app wrote is
    trusted, so a malformed one surfaces rather than being skipped."""
    cohorts_dir = _cohorts_dir(root)
    if not cohorts_dir.is_dir():
        return []
    out: list[dict[str, object]] = []
    for path in sorted(cohorts_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
        out.append(_as_object_dict(loaded, str(path)))
    return out
