"""Tests for ``furnace.maker_eval`` (PRD #125, slice S87) — every episode is
hand-computed: tiny explicit frames where the expected fills, inventory walk,
and bps arithmetic are derived on paper in the comments, so the engine's
ledger is pinned to numbers a reviewer can recompute by hand."""

from __future__ import annotations

import polars as pl
import pytest

from furnace import maker_eval

# Quotes at 2 bps around close 100.0: bid 99.98, ask 100.02.
_OFFSET = 2.0
_FEE = 5.0


def _gate(ts: list[int], close: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"ts": ts, "close": close})


def _preds(ts: list[int], prob: list[float], rc: list[int] | None = None) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": ts,
            "prediction": [1] * len(ts),
            "probability": prob,
            "reason_code": rc or [0] * len(ts),
        }
    )


def _minute(rows: list[tuple[int, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [r[0] for r in rows],
            "low": [r[1] for r in rows],
            "high": [r[2] for r in rows],
        }
    )


def _run(gate, minute, preds, **kw):
    defaults = {"offset_bps": _OFFSET, "tau": 0.5, "taker_fee_bps": _FEE}
    defaults.update(kw)
    return maker_eval.simulate(gate, minute, preds, **defaults)


def test_strict_through_fill_rule() -> None:
    """A 1m low EQUAL to the bid is no fill; only strictly through fills.
    Touched: 0 fills, net 0. Through (low 99.97 < 99.98): one bid fill at
    99.98, held to window end, marked to close 100.0 and charged the taker
    exit: (100.0-99.98)/99.98*1e4 - 5.0 = +2.0004 - 5.0 = -2.9996 bps."""
    gate = _gate([100, 200], [100.0, 100.0])
    preds = _preds([100, 200], [0.0, 0.0])

    touched = _run(gate, _minute([(150, 99.98, 100.00)]), preds)
    gated = touched["gated"]
    assert gated["n_fills"] == 0
    assert gated["net_bps"] == 0.0

    through = _run(gate, _minute([(150, 99.97, 100.00)]), preds)
    gated = through["gated"]
    assert gated["n_fills"] == 1
    assert gated["net_bps"] == pytest.approx(-2.9996, abs=1e-3)


def test_hand_computed_episode_ledger() -> None:
    """Four bars, all quiet, closes pinned at 100.0 so the arithmetic is flat:
    bar1 bid fill (long @ 99.98); bar2 ask fill closes it:
    (100.02-99.98)/99.98*1e4 = +4.0008; bar3 trades through BOTH quotes — a
    same-bar round trip booking the full quoted spread 2*2 = +4.0 exactly.
    Net = +8.0008 bps over 4 fills, 3 quoted bars, no flattens."""
    gate = _gate([100, 200, 300, 400], [100.0, 100.0, 100.0, 100.0])
    preds = _preds([100, 200, 300, 400], [0.0, 0.0, 0.0, 0.0])
    minute = _minute(
        [
            (150, 99.97, 100.00),   # bar1: through the bid only
            (250, 99.99, 100.03),   # bar2: through the ask only
            (350, 99.90, 100.10),   # bar3: through both
        ]
    )
    out = _run(gate, minute, preds)
    gated = out["gated"]
    assert gated["n_quoted_bars"] == 3
    assert gated["n_fills"] == 4
    assert gated["spread_captured_bps"] == pytest.approx(4.0)
    assert gated["n_flattens"] == 0
    assert gated["net_bps"] == pytest.approx(8.0008, abs=1e-3)
    assert gated["net_per_fill_bps"] == pytest.approx(8.0008 / 4, abs=1e-3)


def test_toxic_bar_pulls_quotes_and_flattens_at_taker_cost() -> None:
    """Bar1 quiet: long @ 99.98. Bar2 toxic (probability 0.9 >= tau): no
    quotes that bar, inventory flattened at its close 102.0 minus the taker
    fee: (102.0-99.98)/99.98*1e4 - 5.0 = +202.04 - 5.0 = +197.04 bps."""
    gate = _gate([100, 200, 300], [100.0, 100.0, 102.0])
    preds = _preds([100, 200, 300], [0.0, 0.0, 0.9])
    minute = _minute([(150, 99.97, 100.0), (250, 101.9, 102.0)])
    out = _run(gate, minute, preds)
    gated = out["gated"]
    assert gated["n_quoted_bars"] == 1  # bar 2 quotes; bar 3 is toxic
    assert gated["n_fills"] == 1
    assert gated["n_flattens"] == 1
    assert gated["flatten_cost_bps"] == pytest.approx(_FEE)
    assert gated["net_bps"] == pytest.approx(197.0398, abs=1e-3)


def test_inventory_bound_suppresses_quotes() -> None:
    """Already long after bar1, bar2's low trades through the bid price again
    but the bid is not quoted at +1 inventory: still exactly one fill. The
    held unit marks to the final close 100.0 with the taker exit:
    +2.0004 - 5.0 = -2.9996 bps."""
    gate = _gate([100, 200, 300], [100.0, 100.0, 100.0])
    preds = _preds([100, 200, 300], [0.0, 0.0, 0.0])
    minute = _minute([(150, 99.97, 100.0), (250, 99.96, 100.0)])
    out = _run(gate, minute, preds)
    gated = out["gated"]
    assert gated["n_fills"] == 1
    assert gated["net_bps"] == pytest.approx(-2.9996, abs=1e-3)


def test_gated_vs_ungated_delta_and_ci() -> None:
    """The gate's value isolated: both sides go long @ 99.98 in bar1. The gate
    reads bars 2-3 toxic — flattens at bar2's close 100.0 (+2.0004 - 5.0 =
    -2.9996) and stays out through the crash. Ungated holds through it and
    marks at the final close 90.0: (90-99.98)/99.98*1e4 - 5.0 = -1003.2 bps.
    Delta = gated - ungated = +1000.2; with n <= bootstrap block the CI
    degenerates to the point estimate (deterministic small-sample rule)."""
    gate = _gate([100, 200, 300, 400], [100.0, 100.0, 100.0, 90.0])
    preds = _preds([100, 200, 300, 400], [0.0, 0.0, 0.9, 0.9])
    minute = _minute([(150, 99.97, 100.0), (250, 99.99, 100.0), (350, 89.0, 100.0)])
    out = _run(gate, minute, preds)
    gated, ungated = out["gated"], out["ungated"]
    assert gated["net_bps"] == pytest.approx(-2.9996, abs=1e-3)
    assert ungated["net_bps"] == pytest.approx(-1003.2006, abs=1e-2)
    assert out["delta_net_bps"] == pytest.approx(1000.201, abs=1e-2)
    lo, hi = out["delta_ci_95"]
    assert lo == pytest.approx(out["delta_net_bps"])
    assert hi == pytest.approx(out["delta_net_bps"])
    assert {"pnl_gated_bps", "pnl_ungated_bps", "ts"} <= set(out["per_bar"].columns)
    # adverse selection is reported for both ledgers when fills exist
    assert ungated["adverse_selection_bps_k"] is not None


def test_missing_minute_coverage_fails_loud() -> None:
    """A window with no 1m rows inside any gate bar's span has nothing to
    simulate fills from — the engine raises and names the range, never
    returns a silently empty ledger."""
    gate = _gate([100, 200], [100.0, 100.0])
    preds = _preds([100, 200], [0.0, 0.0])
    minute = _minute([(50, 99.0, 101.0)])  # before the first bar: outside every span
    with pytest.raises(ValueError, match="no 1m coverage"):
        _run(gate, minute, preds)


def test_toxic_flatten_does_not_require_minute_coverage() -> None:
    """A toxic signal flattens at the gate bar's close even when that bar has
    no 1m coverage — the flatten marks off gate_bars, and only quoting needs
    the 1m extremes. Long @ 99.98 from bar1; bar2 is toxic with no 1m rows in
    its span: flattened at its close 101.0: (101-99.98)/99.98*1e4 - 5.0 =
    +102.0204 - 5.0 = +97.0204 bps."""
    gate = _gate([100, 200, 300], [100.0, 100.0, 101.0])
    preds = _preds([100, 200, 300], [0.0, 0.0, 0.9])
    minute = _minute([(150, 99.97, 100.0)])  # bar 3 has no coverage
    out = _run(gate, minute, preds)
    gated = out["gated"]
    assert gated["n_flattens"] == 1
    assert gated["net_bps"] == pytest.approx(97.0204, abs=1e-3)


def test_recut_drift_is_reported_not_fatal() -> None:
    """The canonical writer re-cuts recent closed bars (measured live), so a
    served prediction's ts can lawfully vanish from today's snapshot. The
    engine evaluates the intersection and REPORTS the exclusion count — the
    drift is visible in the result, never a crash and never silent."""
    gate = _gate([100, 200, 300], [100.0, 100.0, 100.0])
    preds = _preds([100, 200, 999], [0.0, 0.0, 0.0])  # 999 was re-cut away
    minute = _minute([(150, 99.97, 100.0), (250, 99.99, 100.0)])
    out = _run(gate, minute, preds)
    assert out["n_unmatched_predictions"] == 1
    assert out["gated"]["n_fills"] == 1  # the intersection still evaluates


def test_unserved_gate_bars_reported_and_lag_excluded() -> None:
    """A re-cut replacement bar inside the served window lacks a prediction:
    reported in n_unmatched_gate_bars, evaluation proceeds on the
    intersection. Gate bars NEWER than the last served prediction are the
    natural between-ticks serving lag and are simply outside the window."""
    preds = _preds([100, 300], [0.0, 0.0])  # bar 200 is a re-cut replacement
    gate = _gate([100, 200, 300], [100.0, 100.0, 100.0])
    minute = _minute([(150, 99.97, 100.0), (250, 99.97, 100.0)])
    out = _run(gate, minute, preds)
    assert out["n_unmatched_gate_bars"] == 1

    # trailing unserved bars are fine: evaluation ends at the last served ts
    preds2 = _preds([100, 200], [0.0, 0.0])
    gate2 = _gate([100, 200, 300], [100.0, 100.0, 100.0])
    out2 = _run(gate2, _minute([(150, 99.97, 100.0)]), preds2)
    assert out2["gated"]["n_fills"] == 1
    assert out2["n_unmatched_gate_bars"] == 0
