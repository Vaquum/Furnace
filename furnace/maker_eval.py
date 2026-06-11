"""Maker-fill simulation of a served cohort (PRD #125, slice S87).

The payoff function the magnitude edge actually has: symmetric passive quotes
around the prior bar's close, gated by the cohort's SERVED predictions —
quote while the gate reads quiet, pull quotes and flatten while it reads
toxic. Fills are detected conservatively at 1-minute granularity: a resting
bid at ``p`` fills only when a 1m bar trades strictly THROUGH it
(``low < p``; ask: ``high > p``), at most one fill per side per gate bar, no
rebates. The ledger is always computed twice — gated and ungated — so the
headline is the model's incremental value, never the strategy's beta.

Pure module: consumes three frames (the cohort's series bars, the 1m bars,
the served predictions), no HTTP, no ``limen`` import. Deterministic,
including the moving-block bootstrap CI (fixed seed).

Price convention: quotes derive from gate bar ``i-1``'s close and live during
bar ``i``'s span ``(ts[i-1], ts[i]]``; all P&L is expressed in bps of the
entry price; open inventory at window end is marked to the final close and
charged the taker fee (it must be flattened to realize).
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import polars as pl

_FloatArray = npt.NDArray[np.float64]
_BOOTSTRAP_ITER = 200
_BOOTSTRAP_BLOCK = 24
_BOOTSTRAP_SEED = 13


def _column_f64(frame: pl.DataFrame, name: str) -> _FloatArray:
    return frame.get_column(name).cast(pl.Float64).to_numpy().astype(np.float64)


def _bar_extremes(
    gate_ts: npt.NDArray[np.int64],
    minute_bars: pl.DataFrame,
) -> tuple[_FloatArray, _FloatArray, npt.NDArray[np.bool_]]:
    """Per gate bar ``i``: the min low / max high of the 1m bars in
    ``(ts[i-1], ts[i]]``, plus a coverage mask. Fails loud when not a single
    evaluated bar has 1m coverage — there is nothing to simulate fills from."""
    m_ts = minute_bars.get_column("ts").cast(pl.Int64).to_numpy()
    m_lo = _column_f64(minute_bars, "low")
    m_hi = _column_f64(minute_bars, "high")
    bins = np.searchsorted(gate_ts, m_ts, side="left")
    inside = (bins > 0) & (bins < gate_ts.size)
    low = np.full(gate_ts.size, np.inf, dtype=np.float64)
    high = np.full(gate_ts.size, -np.inf, dtype=np.float64)
    np.minimum.at(low, bins[inside], m_lo[inside])
    np.maximum.at(high, bins[inside], m_hi[inside])
    covered = np.isfinite(low) & np.isfinite(high)
    covered[0] = False  # bar 0 has no prior close to quote from
    if not bool(covered[1:].any()):
        raise ValueError(
            "no 1m coverage for any evaluated gate bar in "
            f"[{gate_ts[0]}..{gate_ts[-1]}]; cannot simulate fills"
        )
    return low, high, covered


def _replay(
    close: _FloatArray,
    bar_low: _FloatArray,
    bar_high: _FloatArray,
    covered: npt.NDArray[np.bool_],
    toxic: npt.NDArray[np.bool_],
    *,
    offset_bps: float,
    taker_fee_bps: float,
) -> tuple[dict[str, object], _FloatArray, list[tuple[int, str, float]]]:
    """One pass over the gate bars; inventory in {-1, 0, +1}. Returns the
    aggregate ledger, the per-bar P&L series (bps), and the fills."""
    n = close.size
    pnl = np.zeros(n, dtype=np.float64)
    fills: list[tuple[int, str, float]] = []
    position = 0
    entry = 0.0
    n_quoted = 0
    n_flattens = 0
    spread_captured = 0.0
    flatten_cost = 0.0
    offset = offset_bps / 1e4

    for i in range(1, n):
        if toxic[i]:
            # The flatten marks at the gate bar's close, which gate_bars always
            # carries — a toxic signal acts even on a bar with no 1m coverage
            # (only QUOTING needs the 1m extremes to detect fills).
            if position != 0:
                exit_px = close[i]
                pnl[i] += position * (exit_px - entry) / entry * 1e4 - taker_fee_bps
                flatten_cost += taker_fee_bps
                n_flattens += 1
                position = 0
            continue
        if not covered[i]:
            continue
        n_quoted += 1
        ref = close[i - 1]
        bid = ref * (1.0 - offset)
        ask = ref * (1.0 + offset)
        bid_fill = bool(bar_low[i] < bid) and position < 1
        ask_fill = bool(bar_high[i] > ask) and position > -1
        if bid_fill and ask_fill:
            # A full round trip inside one bar captures the quoted spread; the
            # intra-bar fill order is unknowable at this granularity, and the
            # position is unchanged either way.
            spread_captured += 2.0 * offset_bps
            pnl[i] += 2.0 * offset_bps
            fills.append((i, "bid", bid))
            fills.append((i, "ask", ask))
        elif bid_fill:
            fills.append((i, "bid", bid))
            if position == -1:
                pnl[i] += (entry - bid) / entry * 1e4
                position = 0
            else:
                position, entry = 1, bid
        elif ask_fill:
            fills.append((i, "ask", ask))
            if position == 1:
                pnl[i] += (ask - entry) / entry * 1e4
                position = 0
            else:
                position, entry = -1, ask

    if position != 0:
        # Window end: mark to the final close and pay the taker exit.
        pnl[n - 1] += position * (close[n - 1] - entry) / entry * 1e4 - taker_fee_bps
        flatten_cost += taker_fee_bps
        n_flattens += 1

    ledger: dict[str, object] = {
        "n_quoted_bars": n_quoted,
        "n_fills": len(fills),
        # 2 fills can land on 1 quoted bar (a same-bar round trip), so this
        # is a rate per BAR in the ledger grammar, not a [0,1] probability.
        "fills_per_quoted_bar": (len(fills) / n_quoted) if n_quoted else None,
        "spread_captured_bps": float(spread_captured),
        "n_flattens": n_flattens,
        "flatten_cost_bps": float(flatten_cost),
        "net_bps": float(pnl.sum()),
        "net_per_fill_bps": (float(pnl.sum()) / len(fills)) if fills else None,
        "exposure_share": (n_quoted / int(covered[1:].sum())) if int(covered[1:].sum()) else None,
    }
    return ledger, pnl, fills


def _markout_bps(
    fills: list[tuple[int, str, float]],
    close: _FloatArray,
    k: int,
) -> float | None:
    """Adverse selection: the close move ``k`` gate bars after each fill,
    signed against the filled side (negative = the fill was picked off)."""
    if not fills:
        return None
    marks: list[float] = []
    last = close.size - 1
    for i, side, price in fills:
        j = min(i + k, last)
        move = (close[j] - price) / price * 1e4
        marks.append(move if side == "bid" else -move)
    return float(np.mean(np.asarray(marks, dtype=np.float64)))


def _block_bootstrap_ci(diff: _FloatArray) -> tuple[float, float]:
    """95% CI of ``diff.sum()`` under a moving-block bootstrap (block 24,
    200 iterations, fixed seed — deterministic across runs)."""
    n = diff.size
    if n <= _BOOTSTRAP_BLOCK:
        total = float(diff.sum())
        return total, total
    rng = np.random.default_rng(_BOOTSTRAP_SEED)
    # high is exclusive: +1 keeps the LAST valid block start (n - block) in the
    # draw, so diff[n-1] is reachable and the right edge is not biased away.
    starts_pool = n - _BOOTSTRAP_BLOCK + 1
    n_blocks = n // _BOOTSTRAP_BLOCK + 1
    sums = np.empty(_BOOTSTRAP_ITER, dtype=np.float64)
    for b in range(_BOOTSTRAP_ITER):
        starts = rng.integers(0, starts_pool, size=n_blocks)
        pieces = [diff[s : s + _BOOTSTRAP_BLOCK] for s in starts]
        sample = np.concatenate(pieces)[:n]
        sums[b] = sample.sum()
    lo, hi = np.percentile(sums, [2.5, 97.5])
    return float(lo), float(hi)


def simulate(
    gate_bars: pl.DataFrame,
    minute_bars: pl.DataFrame,
    predictions: pl.DataFrame,
    *,
    offset_bps: float,
    tau: float,
    k: int = 3,
    taker_fee_bps: float = 5.0,
) -> dict[str, object]:
    """Replay symmetric passive quotes over ``gate_bars``, gated by the served
    ``predictions`` (toxic when ``probability >= tau`` or ``reason_code != 0``),
    with strict-through fills detected from ``minute_bars``. Returns the gated
    and ungated ledgers, the model-value delta with a bootstrap CI, and the
    per-bar P&L frame. ``gate_bars`` needs ``ts``/``close``; ``minute_bars``
    needs ``ts``/``low``/``high``; ``predictions`` is the conduit frame
    (``ts``/``prediction``/``probability``/``reason_code``)."""
    if gate_bars.height < 2:
        raise ValueError("gate_bars must carry at least two bars (one to quote from)")
    joined = (
        gate_bars.select(["ts", "close"])
        .join(predictions.select(["ts", "probability", "reason_code"]), on="ts", how="inner")
        .sort("ts")
    )
    if joined.height < 2:
        raise ValueError("predictions and gate_bars share fewer than two ts rows")
    if joined.height < predictions.height:
        raise ValueError(
            f"{predictions.height - joined.height} prediction rows have no matching "
            "gate bar ts — inputs are misaligned (different series or a re-cut "
            "snapshot); refusing to evaluate a silently different window"
        )
    gate_ts = joined.get_column("ts").cast(pl.Int64).to_numpy()
    close = _column_f64(joined, "close")
    probability = _column_f64(joined, "probability")
    reason = joined.get_column("reason_code").cast(pl.Int64).to_numpy()

    bar_low, bar_high, covered = _bar_extremes(gate_ts, minute_bars)
    toxic_gated = (reason != 0) | (probability >= tau)
    toxic_never = np.zeros(joined.height, dtype=np.bool_)

    gated_ledger, gated_pnl, gated_fills = _replay(
        close, bar_low, bar_high, covered, toxic_gated,
        offset_bps=offset_bps, taker_fee_bps=taker_fee_bps,
    )
    ungated_ledger, ungated_pnl, ungated_fills = _replay(
        close, bar_low, bar_high, covered, toxic_never,
        offset_bps=offset_bps, taker_fee_bps=taker_fee_bps,
    )
    gated_ledger["adverse_selection_bps_k"] = _markout_bps(gated_fills, close, k)
    ungated_ledger["adverse_selection_bps_k"] = _markout_bps(ungated_fills, close, k)
    n_uncovered = int(joined.height - 1 - int(covered[1:].sum()))
    gated_ledger["n_bars_without_minute"] = n_uncovered
    ungated_ledger["n_bars_without_minute"] = n_uncovered

    diff = gated_pnl - ungated_pnl
    ci_lo, ci_hi = _block_bootstrap_ci(diff)
    per_bar = pl.DataFrame(
        {"ts": gate_ts, "pnl_gated_bps": gated_pnl, "pnl_ungated_bps": ungated_pnl}
    )
    return {
        "params": {
            "offset_bps": offset_bps,
            "tau": tau,
            "k": k,
            "taker_fee_bps": taker_fee_bps,
            "inventory_max": 1,
        },
        "gated": gated_ledger,
        "ungated": ungated_ledger,
        "delta_net_bps": float(diff.sum()),
        "delta_ci_95": (ci_lo, ci_hi),
        "per_bar": per_bar,
    }
