"""Tests for ``furnace.cache_scheduler`` — per-minute Conduit top-up (PRD #71).

Covers the scheduler's own deliverables: ``series_for_cohort`` (the nested
manifest walk that resolves a cohort's ``/opt/arrow`` series, and returns
``None`` for a non-arrow source so that cohort is skipped) and ``refresh_once``
(which drives ``prediction_cache.top_up`` for every arrow cohort, isolating a
cohort that raises so it can't starve the rest). The shared ``cohort_round`` /
``fake_spot_klines`` / ``frozen_klines`` fixtures (``conftest.py``) supply a
real trained round; ``get_arrow_file`` is patched to serve a controlled slice so
a "new bar arrived" can be simulated deterministically.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import polars as pl
import pytest
from limen.data import historical_data

from furnace import cache_scheduler, prediction_cache

_SERIES = "time_test"
_CID_A = "sha256:" + "a" * 64
_CID_B = "sha256:" + "b" * 64


def _serve_arrow(monkeypatch: pytest.MonkeyPatch, klines: pl.DataFrame) -> None:
    monkeypatch.setattr(
        historical_data.HistoricalData,
        "get_arrow_file",
        staticmethod(lambda *_a, **_k: klines),
    )


def _arrow_metadata(series: str) -> dict[str, object]:
    """The minimal ``metadata.json`` shape ``series_for_cohort`` walks: a
    ``data_source`` whose ``params.file_path`` is an ``/opt/arrow`` series."""
    return {
        "yaml_reference": {
            "sfd": {"manifest": {"data_source": {"params": {
                "file_path": f"/x/opt/arrow/{series}/latest.arrow"
            }}}}
        }
    }


def _arrow_cohort(round_dir: Path, dest: Path, series: str) -> Path:
    """Copy a real trained round to ``dest`` and point its data source at an
    arrow ``series`` (keeping the valid ``yaml_reference`` so ``Trainer`` still
    trains). Returns the experiment dir."""
    shutil.copytree(round_dir, dest)
    md_path = dest / "metadata.json"
    md = json.loads(md_path.read_text(encoding="utf-8"))
    md["yaml_reference"]["sfd"]["manifest"]["data_source"]["params"]["file_path"] = (
        f"/x/opt/arrow/{series}/latest.arrow"
    )
    md_path.write_text(json.dumps(md), encoding="utf-8")
    return dest


def _register(
    root: Path, name: str, experiment_dir: Path, rids: list[str],
    *, cohort_id: str | None = None, active: bool = True,
) -> None:
    # active=True: the scheduler refreshes only active cohorts (slice S78), so a
    # cohort registered for a top-up test must be on to be served. Every real
    # definition carries a cohort_id — the serving path records it beside the
    # served file and rebuilds on mismatch (slice S85).
    cohorts_dir = root / "cohorts"
    cohorts_dir.mkdir(parents=True, exist_ok=True)
    (cohorts_dir / f"{name}.json").write_text(
        json.dumps({
            "name": name, "experiment_dir": str(experiment_dir),
            "permutation_ids": rids, "active": active,
            "cohort_id": cohort_id or f"sha256:{name.ljust(64, '0')}",
        }),
        encoding="utf-8",
    )


def test_series_for_cohort_reads_arrow_series(tmp_path: Path) -> None:
    """The nested ``yaml_reference.sfd.manifest.data_source.params.file_path``
    walk resolves ``…/opt/arrow/<series>/latest.arrow`` -> ``<series>``."""
    exp = tmp_path / "round"
    exp.mkdir()
    (exp / "metadata.json").write_text(json.dumps(_arrow_metadata(_SERIES)), encoding="utf-8")
    assert cache_scheduler.series_for_cohort({"experiment_dir": str(exp)}) == _SERIES


def test_series_for_cohort_skips_get_spot_klines_source(
    cohort_round: tuple[Path, list[str], str],
) -> None:
    """A real round trained off ``get_spot_klines`` has no arrow ``file_path``,
    so it resolves to ``None`` and ``refresh_once`` skips it — there is no
    ``/opt/arrow`` file to top up from."""
    round_dir, _rids, _at = cohort_round
    assert cache_scheduler.series_for_cohort({"experiment_dir": str(round_dir)}) is None


def test_series_for_cohort_none_when_metadata_absent(tmp_path: Path) -> None:
    """A missing ``metadata.json`` or a cohort def without ``experiment_dir``
    resolves to ``None`` (skip), not a crash."""
    assert cache_scheduler.series_for_cohort({"experiment_dir": str(tmp_path)}) is None
    assert cache_scheduler.series_for_cohort({}) is None


def test_refresh_once_tops_up_arrow_cohort(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: ``refresh_once`` resolves the cohort's arrow series, runs
    ``top_up`` (Trainer.train -> predict_all -> write), and reports the bars it
    appended — matching the cache file it wrote."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    _register(root, "cohort-1", exp, list(rids))
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    res = cache_scheduler.refresh_once(root, conduit, arrow)

    cached = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit))
    assert res == {"cohort-1": cached.height}
    assert res["cohort-1"] > 0


def test_refresh_once_isolates_a_failing_cohort(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cohort raising (here: arrow series but no valid ``yaml_reference``, so
    ``Trainer`` raises) is logged and skipped — the healthy cohort still
    refreshes, and the failing one is absent from the result rather than
    aborting the whole tick."""
    round_dir, rids, _at = cohort_round
    good = _arrow_cohort(round_dir, tmp_path / "good", _SERIES)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "metadata.json").write_text(json.dumps(_arrow_metadata("time_bad")), encoding="utf-8")

    root = tmp_path / "data"
    _register(root, "good", good, list(rids))
    _register(root, "bad", bad, ["not-a-real-permutation"])
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    res = cache_scheduler.refresh_once(root, conduit, arrow)

    assert res.get("good", 0) > 0
    assert "bad" not in res


def test_topup_appends_only_new_bars(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A top-up appends only bars newer than the cache's last ts."""
    round_dir, rids, _at = cohort_round
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids), "cohort_id": _CID_A}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"

    head = frozen_klines.head(int(frozen_klines.height * 0.8))
    _serve_arrow(monkeypatch, head)
    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)
    n0 = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit)).height

    _serve_arrow(monkeypatch, frozen_klines)  # the series now carries the full history
    appended = prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)
    n1 = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit)).height

    assert appended > 0
    assert n1 == n0 + appended


def test_topup_is_idempotent(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running a minute over the same closed bars appends nothing and leaves
    the cache byte-identical — a missed tick self-corrects, a repeated tick is a
    no-op."""
    round_dir, rids, _at = cohort_round
    cohort_def = {"experiment_dir": str(round_dir), "permutation_ids": list(rids), "cohort_id": _CID_A}
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    prediction_cache.backfill(cohort_def, _SERIES, conduit, arrow)
    path = prediction_cache.cache_path(_SERIES, conduit)
    before = path.read_bytes()

    appended = prediction_cache.top_up(cohort_def, _SERIES, conduit, arrow)
    assert appended == 0
    assert path.read_bytes() == before


def test_refresh_once_rebuilds_same_series_after_active_cohort_switch(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the active cohort for a series rebuilds its served file as the
    new cohort's full history — the switch can never leave A-history with a
    B-tail in one file."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    _register(root, "cohort-a", exp, list(rids[:1]), cohort_id=_CID_A)
    cache_scheduler.refresh_once(root, conduit, arrow)
    identity = prediction_cache.cache_identity_path(_SERIES, conduit)
    assert identity.read_text(encoding="utf-8") == _CID_A

    # The switch: A off, B (different members, different identity) on.
    _register(root, "cohort-a", exp, list(rids[:1]), cohort_id=_CID_A, active=False)
    _register(root, "cohort-b", exp, list(rids), cohort_id=_CID_B)
    res = cache_scheduler.refresh_once(root, conduit, arrow)

    rebuilt = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit))
    assert res == {"cohort-b": rebuilt.height}  # the whole file is B's rebuild
    assert identity.read_text(encoding="utf-8") == _CID_B

    reference_root = tmp_path / "conduit_ref"
    cohort_b = {"experiment_dir": str(exp), "permutation_ids": list(rids), "cohort_id": _CID_B}
    prediction_cache.backfill(cohort_b, _SERIES, reference_root, arrow)
    reference = pl.read_ipc(prediction_cache.cache_path(_SERIES, reference_root))
    assert rebuilt.equals(reference)


def test_refresh_once_writes_serving_manifest_for_active_successes(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a pass, ``serving_manifest.json`` lists exactly the series that
    refreshed successfully, with the producing cohort's identity and the
    payload's row count / max ts read back from the written file."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    _register(root, "cohort-1", exp, list(rids), cohort_id=_CID_A)
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    cache_scheduler.refresh_once(root, conduit, arrow)

    manifest = json.loads(
        prediction_cache.serving_manifest_path(conduit).read_text(encoding="utf-8")
    )
    payload = pl.read_ipc(prediction_cache.cache_path(_SERIES, conduit))
    assert manifest["version"] == 1
    assert manifest["generated_at"]
    entry = manifest["series"][_SERIES]
    assert entry["cohort_id"] == _CID_A
    assert entry["name"] == "cohort-1"
    assert entry["path"] == f"{_SERIES}/latest.arrow"
    assert entry["rows"] == payload.height
    assert entry["max_ts"] == payload.get_column("ts").max()


def test_refresh_once_manifest_omits_inactive_stale_files(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed liveness: a retained file whose cohort is now inactive, and
    an active cohort whose refresh failed, are both absent from the manifest —
    path existence is not a liveness signal."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    # A stale retained file: served while active, then turned off.
    _register(root, "stale", exp, list(rids), cohort_id=_CID_A)
    cache_scheduler.refresh_once(root, conduit, arrow)
    assert prediction_cache.cache_path(_SERIES, conduit).is_file()
    _register(root, "stale", exp, list(rids), cohort_id=_CID_A, active=False)

    # An active cohort that fails (no valid yaml_reference -> Trainer raises).
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "metadata.json").write_text(json.dumps(_arrow_metadata("time_bad")), encoding="utf-8")
    _register(root, "bad", bad, ["not-a-real-permutation"], cohort_id=_CID_B)

    cache_scheduler.refresh_once(root, conduit, arrow)

    manifest = json.loads(
        prediction_cache.serving_manifest_path(conduit).read_text(encoding="utf-8")
    )
    assert manifest["series"] == {}  # the stale file exists on disk, yet nothing is live


def test_refresh_once_manifest_readback_failure_does_not_starve_the_pass(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest read-back is inside the per-cohort guard: a cohort whose
    ``_manifest_entry`` raises (a corrupt payload, a schema mismatch) is skipped
    like any other refresh failure — it never aborts the pass, and the manifest
    is still published (the failing cohort simply absent from it)."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)
    _register(root, "cohort-1", exp, list(rids), cohort_id=_CID_A)

    def boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OSError("payload unreadable")

    monkeypatch.setattr(cache_scheduler, "_manifest_entry", boom)

    res = cache_scheduler.refresh_once(root, conduit, arrow)

    # top_up genuinely appended/rebuilt, so the cohort is in the bar-count
    # return; the manifest is the separate fail-closed layer — unable to read
    # the payload back to describe it, it omits the series rather than lying.
    assert list(res) == ["cohort-1"]
    manifest = json.loads(
        prediction_cache.serving_manifest_path(conduit).read_text(encoding="utf-8")
    )
    assert manifest["series"] == {}  # published despite the read-back failure


def test_refresh_once_skips_second_active_cohort_on_same_series(
    cohort_round: tuple[Path, list[str], str],
    fake_spot_klines: None,
    frozen_klines: pl.DataFrame,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two active cohorts on one series (only reachable by editing definitions
    out-of-band — the activation route flips same-series peers off) must not
    trade the identity sidecar and full-rebuild the file every tick: the
    latecomer is skipped loudly, the first claimant's file and identity stay."""
    round_dir, rids, _at = cohort_round
    exp = _arrow_cohort(round_dir, tmp_path / "round", _SERIES)
    root = tmp_path / "data"
    conduit, arrow = tmp_path / "conduit", tmp_path / "arrow"
    _serve_arrow(monkeypatch, frozen_klines)

    _register(root, "first", exp, list(rids[:1]), cohort_id=_CID_A)
    _register(root, "second", exp, list(rids), cohort_id=_CID_B)
    res = cache_scheduler.refresh_once(root, conduit, arrow)

    # list_cohort_definitions orders newest-first; exactly one claimed the series.
    assert len(res) == 1
    winner = next(iter(res))
    identity = prediction_cache.cache_identity_path(_SERIES, conduit).read_text(encoding="utf-8")
    assert identity == {"first": _CID_A, "second": _CID_B}[winner]

    # A second tick must not rebuild: the same winner appends 0, the loser stays skipped.
    res2 = cache_scheduler.refresh_once(root, conduit, arrow)
    assert res2 == {winner: 0}
    assert prediction_cache.cache_identity_path(_SERIES, conduit).read_text(encoding="utf-8") == identity
