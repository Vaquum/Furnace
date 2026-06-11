"""Tests for the maker-eval surface (PRD #125, slice S88): the per-cohort
endpoint (engine stubbed — the arithmetic itself is pinned in
``test_maker_eval.py``), and the COHORTS-table redesign (six maker columns in
the ledger grammar; the Type and conduit-arrow columns retired). Template
invariants are asserted source-level, the codebase's idiom."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
from fastapi.testclient import TestClient

from furnace import cohorts, maker_eval
from furnace.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "furnace"

_MANIFEST_ID = "sha256:" + "1" * 64


def _auth_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_USERNAME", "u")
    monkeypatch.setenv("APP_PASSWORD", "p")
    monkeypatch.setenv("API_SECRET", "s")


def _cohort(root: Path) -> str:
    cid = cohorts.write_cohort_definition(
        root, name="m", cohort_type="direction", experiment_dir="/x",
        manifest_id=_MANIFEST_ID, permutation_ids=["p1"],
    )
    return cid.replace(":", "_")


def _canned_result() -> dict[str, object]:
    ledger = {
        "n_quoted_bars": 10, "n_fills": 4, "fills_per_quoted_bar": 0.4,
        "spread_captured_bps": 4.0, "n_flattens": 1, "flatten_cost_bps": 5.0,
        "net_bps": 3.0, "net_per_fill_bps": 0.75, "exposure_share": 0.5,
        "adverse_selection_bps_k": -1.0, "n_bars_without_minute": 0,
    }
    return {
        "params": {"offset_bps": 2.0, "tau": 0.5, "k": 3, "taker_fee_bps": 5.0, "inventory_max": 1},
        "gated": dict(ledger),
        "ungated": dict(ledger),
        "delta_net_bps": 1.5,
        "delta_ci_95": (0.5, 2.5),
        "per_bar": pl.DataFrame({"ts": [0, 86_400_000_000_000], "pnl_gated_bps": [0.0, 1.5], "pnl_ungated_bps": [0.0, 0.0]}),
    }


def test_maker_eval_route_returns_ledger_json(tmp_path: Path, monkeypatch) -> None:
    """The route returns the engine's result as JSON: both ledgers, the delta
    with its CI, and window_days summarized from the per-bar frame (which is
    itself dropped — a DataFrame is not a JSON payload)."""
    _auth_env(monkeypatch)
    url_id = _cohort(tmp_path)
    monkeypatch.setattr(maker_eval, "evaluate_served", lambda *_a, **_k: _canned_result())
    client = TestClient(create_app(tmp_path))

    r = client.get(f"/cohorts/{url_id}/maker-eval", auth=("u", "p"))
    assert r.status_code == 200
    d = r.json()
    assert {"params", "gated", "ungated", "delta_net_bps", "delta_ci_95", "window_days"} <= set(d)
    assert "per_bar" not in d
    assert d["window_days"] == 1.0
    assert d["gated"]["fills_per_quoted_bar"] == 0.4


def test_params_are_clamped_and_validated(tmp_path: Path, monkeypatch) -> None:
    """Out-of-range params are clamped to the documented bounds before they
    reach the engine — never silently trusted; a non-numeric param is the
    framework's 422, not a crash."""
    _auth_env(monkeypatch)
    url_id = _cohort(tmp_path)
    seen: dict[str, object] = {}

    def spy(_definition, **kw):
        seen.update(kw)
        return _canned_result()

    monkeypatch.setattr(maker_eval, "evaluate_served", spy)
    client = TestClient(create_app(tmp_path))

    r = client.get(
        f"/cohorts/{url_id}/maker-eval",
        params={"offset_bps": 999, "tau": 5, "k": 0, "taker_fee_bps": -3},
        auth=("u", "p"),
    )
    assert r.status_code == 200
    assert seen == {"offset_bps": 50.0, "tau": 1.01, "k": 1, "taker_fee_bps": 0.0}

    bad = client.get(f"/cohorts/{url_id}/maker-eval", params={"offset_bps": "wide"}, auth=("u", "p"))
    assert bad.status_code == 422


def test_unserved_cohort_is_409_not_silent(tmp_path: Path, monkeypatch) -> None:
    """A cohort with nothing served has no gate to evaluate: the engine's
    FileNotFoundError surfaces as a 409 naming the reason — never an empty
    200. An unknown cohort stays a 404."""
    _auth_env(monkeypatch)
    url_id = _cohort(tmp_path)

    def boom(*_a, **_k):
        raise FileNotFoundError("cohort has no served predictions")

    monkeypatch.setattr(maker_eval, "evaluate_served", boom)
    client = TestClient(create_app(tmp_path))

    r = client.get(f"/cohorts/{url_id}/maker-eval", auth=("u", "p"))
    assert r.status_code == 409
    assert "no served predictions" in r.json()["detail"]

    missing = client.get("/cohorts/sha256_nope/maker-eval", auth=("u", "p"))
    assert missing.status_code == 404


def test_cohort_page_renders_maker_eval_card() -> None:
    """The cohort page carries the maker-eval card: the parameter form, the
    run control, and the fetch against this cohort's maker-eval route."""
    text = (PKG_DIR / "templates" / "cohort.html").read_text(encoding="utf-8")
    assert 'id="maker-eval-card"' in text
    assert 'id="maker-eval-form"' in text
    assert 'id="maker-eval-run"' in text
    assert "/maker-eval?" in text


def test_cohorts_table_maker_columns() -> None:
    """The six maker columns sit AFTER Decoders, named in the ledger grammar,
    and the rows fill from the maker-eval route at the fixed default params."""
    text = (PKG_DIR / "templates" / "cohorts.html").read_text(encoding="utf-8")
    columns = [
        "delta_per_day_bps", "delta_ci_95", "net_per_day_bps",
        "fills_per_day", "markout_per_fill_bps", "quoted_share",
    ]
    decoders_at = text.index(">Decoders<")
    positions = [text.index(f">{c}<") for c in columns]
    assert positions == sorted(positions)
    assert all(p > decoders_at for p in positions)
    assert "/maker-eval" in text  # rows fetch the same endpoint the card uses


def test_cohorts_table_drops_type_and_arrow_columns() -> None:
    """The Type and conduit-arrow columns are gone; the polars-read copy
    button lives in the actions cell, immediately left of the delete x."""
    text = (PKG_DIR / "templates" / "cohorts.html").read_text(encoding="utf-8")
    assert "col-cohort-type" not in text
    assert "Predictions (conduit arrow)" not in text
    actions = text.index('<td class="col-actions"')
    copy_at = text.index('class="copy-arrow"', actions)
    delete_at = text.index('class="delete-exp"', actions)
    assert actions < copy_at < delete_at
