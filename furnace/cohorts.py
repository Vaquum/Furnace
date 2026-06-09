"""Cohort definitions — the persisted record of which decoders (one
manifest) form a named cohort, written by the graduation view.

A cohort definition is a flat JSON file under ``<root>/cohorts/`` — one per
cohort, named ``<folded_cohort_id>.json`` where the canonical ``cohort_id``'s
``:`` is folded to ``_`` for the filename (the canonical id, with the ``:``,
is the ``cohort_id`` field *inside* the JSON). A consumer enumerates
``<root>/cohorts/*.json`` and reads the ``cohort_id`` field; it never
constructs the filename. Furnace has no DB; this matches the flatfile state
model, and the file is written atomically (temp + ``os.replace``). The
``cohort_id`` is computed exactly as Limen's ``Cohort`` computes it
(``limen.cohort.Cohort.set_members``) so a Furnace-written definition and a
Limen-built cohort agree on identity — including Limen's uniqueness contract
(Limen rejects duplicate permutation_ids), which we mirror by rejecting them
here. ``test_cohort_graduation`` freezes the formula against a literal hash so
a change to *our* replica is caught; the literal was derived from Limen's
current formula, so it doubles as the cross-system agreement anchor.

This module is deliberately free of any ``limen`` import: graduation
persists *which permutations* form a cohort (the manifest_id + ids), not
trained sensors — sensor materialization is a separate slice."""

from __future__ import annotations

import hashlib
import json
import os
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
            "round has no manifest_id — it was trained on Limen < 3.13, which did "
            "not record one; re-run it on current Limen to add it to a cohort"
        )
    return manifest_id


_COHORT_TYPES = ("direction", "magnitude")


def write_cohort_definition(
    root: Path,
    *,
    name: str,
    cohort_type: str,
    experiment_dir: str,
    manifest_id: str,
    permutation_ids: list[str],
) -> str:
    """Write a cohort definition to ``<root>/cohorts/<cohort_id>.json`` and
    return its ``cohort_id``. The id is derived from ``manifest_id`` and the
    permutation set, so re-creating the same set is idempotent (same file).
    The ``:`` in the id is filesystem-folded to ``_`` for the filename; the
    canonical ``sha256:``-prefixed id is preserved inside the JSON.

    ``cohort_type`` is the operator's classification (``direction`` or
    ``magnitude``). The bar type and training window are *not* stored — they are
    derived from the round's manifest at read time, so they cannot drift from
    the experiment they came from."""
    if not name:
        raise ValueError("cohort name must be non-empty")
    if cohort_type not in _COHORT_TYPES:
        raise ValueError(f"cohort_type must be one of {_COHORT_TYPES}; got {cohort_type!r}")
    if not permutation_ids:
        raise ValueError("cohort must have at least one permutation_id")
    if len(set(permutation_ids)) != len(permutation_ids):
        # Mirror Limen's Cohort uniqueness contract ('permutation_ids must be
        # unique'). A duplicate would make our sorted-with-dups payload hash to
        # a cohort_id Limen could never produce, silently breaking id agreement.
        raise ValueError("permutation_ids must be unique")
    cohort_id = compute_cohort_id(manifest_id, permutation_ids)
    cohorts_dir = _cohorts_dir(root)
    cohorts_dir.mkdir(parents=True, exist_ok=True)
    definition = {
        "cohort_id": cohort_id,
        "name": name,
        "cohort_type": cohort_type,
        "experiment_dir": experiment_dir,
        "manifest_id": manifest_id,
        "permutation_ids": sorted(permutation_ids),
        "active": False,  # off until an operator turns it on (slice S79 enforces one-per-series)
    }
    path = cohorts_dir / f"{_filename_for(cohort_id)}.json"
    # Atomic write (temp + os.replace), mirroring the repo's other flatfile
    # state (state.py) — never leave a partial JSON for a reader to hit.
    tmp = cohorts_dir / f"{_filename_for(cohort_id)}.json.tmp"
    tmp.write_text(json.dumps(definition, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return cohort_id


def delete_cohort_definition(root: Path, cohort_id: str) -> bool:
    """Delete the cohort definition for ``cohort_id``. Returns True if one was
    removed, False if no such cohort exists. The target is resolved by matching
    the enumerated ``*.json`` files in the cohorts dir — never by building a path
    from ``cohort_id`` — so a crafted id (e.g. ``../foo``) cannot unlink a file
    outside ``<root>/cohorts/`` (the same enumerate-don't-build rule as
    ``find_cohort_definition``). A served prediction cache, if any, is a separate
    store and is left untouched."""
    cohorts_dir = _cohorts_dir(root)
    if not cohorts_dir.is_dir():
        return False
    wanted = _filename_for(cohort_id)
    for path in cohorts_dir.glob("*.json"):
        if path.stem == wanted:
            path.unlink()
            return True
    return False


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


def find_cohort_definition(root: Path, url_id: str) -> dict[str, object] | None:
    """Return the cohort definition whose URL id (the ``:``-folded
    ``cohort_id``, i.e. the filename stem) equals ``url_id``, or None.
    Matching against the enumerated definitions — never building a path from
    ``url_id`` — keeps request input from reaching the filesystem."""
    for definition in list_cohort_definitions(root):
        cohort_id = definition.get("cohort_id")
        if isinstance(cohort_id, str) and _filename_for(cohort_id) == url_id:
            return definition
    return None


def set_cohort_active(root: Path, url_id: str, active: bool) -> bool:
    """Set the cohort's ``active`` flag and return the new value. The scheduler
    refreshes only active cohorts. The target file is resolved by enumerating
    ``<root>/cohorts/*.json`` (never by building a path from ``url_id``),
    mirroring ``find_cohort_definition``; the write is atomic (temp +
    ``os.replace``). Raises ``FileNotFoundError`` when no cohort matches."""
    cohorts_dir = _cohorts_dir(root)
    if cohorts_dir.is_dir():
        for path in sorted(cohorts_dir.glob("*.json")):
            definition = _as_object_dict(json.loads(path.read_text(encoding="utf-8")), str(path))
            cohort_id = definition.get("cohort_id")
            if isinstance(cohort_id, str) and _filename_for(cohort_id) == url_id:
                definition["active"] = active
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(definition, indent=2), encoding="utf-8")
                os.replace(tmp, path)
                return active
    raise FileNotFoundError(f"no cohort with url_id {url_id!r} under {cohorts_dir}")


def activate_cohort(root: Path, url_id: str) -> dict[str, object]:
    """Make this cohort the sole active one for its bar type: set its own
    ``active`` true, and set every OTHER cohort sharing its arrow series
    (``cohort_display_meta()['arrow_series']``) to false. Returns the activated
    definition so the caller can backfill its series file. Peers are found by
    enumerating ``<root>/cohorts/*.json``, never by building a path from
    ``url_id``. Raises ``FileNotFoundError`` when no cohort matches ``url_id``."""
    target = find_cohort_definition(root, url_id)
    if target is None:
        raise FileNotFoundError(f"no cohort with url_id {url_id!r} under {_cohorts_dir(root)}")
    series = cohort_display_meta(target).get("arrow_series")
    for definition in list_cohort_definitions(root):
        cohort_id = definition.get("cohort_id")
        if not isinstance(cohort_id, str):
            continue
        peer_url_id = _filename_for(cohort_id)
        # A bar type is one arrow series; only a peer on the *same* served series
        # contends for its conduit file, so leave other-series cohorts untouched.
        if peer_url_id != url_id and series is not None and \
                cohort_display_meta(definition).get("arrow_series") == series:
            set_cohort_active(root, peer_url_id, False)
    set_cohort_active(root, url_id, True)
    target["active"] = True
    return target


def _walk(node: object, keys: tuple[str, ...]) -> object:
    """Follow a chain of dict keys, returning None on any non-dict / missing."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = cast(dict[object, object], node).get(key)
    return node


def _compact_dollars(size: object) -> str:
    """``60000000`` → ``$60M`` — a readable dollar-bar size (B/M/K), trailing
    ``.0`` dropped. Non-numeric sizes fall back to a bare ``$<value>``."""
    if not isinstance(size, (int, float)) or isinstance(size, bool):
        return f"${size}"
    n = float(size)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"${f'{n / div:.1f}'.rstrip('0').rstrip('.')}{suffix}"
    return f"${int(n)}" if n == int(n) else f"${n}"


def _bar_type(data_source: object) -> str | None:
    """A human bar-type label from a manifest ``data_source``: the ``/opt/arrow``
    series name (e.g. ``time_4h``), or the kline / dollar-bar size for the spot
    feeds, or the bare method name as a fallback."""
    if not isinstance(data_source, dict):
        return None
    ds = cast(dict[object, object], data_source)
    method = ds.get("method")
    params = ds.get("params")
    params = cast(dict[object, object], params) if isinstance(params, dict) else {}
    if not isinstance(method, str):
        return None
    if "get_arrow_file" in method:
        fp = params.get("file_path")
        return Path(fp).parent.name if isinstance(fp, str) else None
    if "dollar" in method:
        size = params.get("dollar_bar_size")
        return f"{_compact_dollars(size)} dollar bars" if size is not None else None
    if "get_spot_klines" in method:
        size = params.get("kline_size")
        return f"{size}s klines" if size is not None else None
    return method.rsplit(".", 1)[-1]


def _arrow_series(data_source: object) -> str | None:
    """The ``/opt/arrow`` series name when the data source is ``get_arrow_file``
    (so the cohort's predictions land at ``/opt/conduit/<series>/latest.arrow``),
    else None — spot/dollar feeds have no served arrow file."""
    if not isinstance(data_source, dict):
        return None
    ds = cast(dict[object, object], data_source)
    method = ds.get("method")
    params = ds.get("params")
    params = cast(dict[object, object], params) if isinstance(params, dict) else {}
    if isinstance(method, str) and "get_arrow_file" in method:
        fp = params.get("file_path")
        return Path(fp).parent.name if isinstance(fp, str) else None
    return None


def cohort_display_meta(cohort_def: dict[str, object]) -> dict[str, object]:
    """Derive the COHORTS-table fields from a cohort def and its round manifest:
    the decoder count, the bar type (the data series), and the training window
    (the *train* split only — val/test stay servable, so a cohort answers
    prediction requests outside the bars it actually fit on)."""
    perms = cohort_def.get("permutation_ids")
    decoder_count = len(cast(list[object], perms)) if isinstance(perms, list) else 0
    bar_type: str | None = None
    arrow_series: str | None = None
    train_start: object = None
    train_end: object = None
    experiment_dir = cohort_def.get("experiment_dir")
    if isinstance(experiment_dir, str):
        meta_path = Path(experiment_dir) / "metadata.json"
        if meta_path.is_file():
            root_node: object = json.loads(meta_path.read_text(encoding="utf-8"))
            manifest = _walk(root_node, ("yaml_reference", "sfd", "manifest"))
            data_source = _walk(manifest, ("data_source",))
            bar_type = _bar_type(data_source)
            arrow_series = _arrow_series(data_source)
            split = _walk(manifest, ("split_dates",))
            if isinstance(split, dict):
                sd = cast(dict[object, object], split)
                train_start = sd.get("train_start")
                train_end = sd.get("train_end")
    cohort_id = cohort_def.get("cohort_id")
    return {
        "cohort_id": cohort_id,
        # The filesystem-safe id (':' folded to '_') — used as the URL segment
        # for the cohort's own pages, so a route never carries a raw ':'.
        "url_id": _filename_for(cohort_id) if isinstance(cohort_id, str) else None,
        "name": cohort_def.get("name"),
        "cohort_type": cohort_def.get("cohort_type"),
        "bar_type": bar_type,
        "arrow_series": arrow_series,
        # The served, zero-copy prediction file for this cohort's series — None
        # for spot/dollar feeds, which have no /opt/conduit arrow.
        "arrow_path": f"/opt/conduit/{arrow_series}/latest.arrow" if arrow_series else None,
        "train_start": train_start if isinstance(train_start, str) else None,
        "train_end": train_end if isinstance(train_end, str) else None,
        "decoder_count": decoder_count,
        "active": bool(cohort_def.get("active", False)),
    }
