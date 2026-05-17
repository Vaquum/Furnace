from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path

from .ast_strip import strip_to_definitions
from .state import read_status, status_flock, write_status_atomic, now_iso


def _self_promote_to_running(round_dir: Path) -> None:
    with status_flock(round_dir):
        st = read_status(round_dir)
        if st["state"] != "pending":
            return
        st["state"] = "running"
        st["pid"] = os.getpid()
        st["started_at"] = st.get("started_at") or now_iso()
        write_status_atomic(round_dir, st)


def _route_warnings_and_silence_tqdm(round_dir: Path) -> None:
    import warnings as _w
    f = open(round_dir / "warnings.log", "a", buffering=1)

    def _show(message, category, filename, lineno, file=None, line=None):
        try:
            f.write(_w.formatwarning(message, category, filename, lineno, line))
        except Exception:
            pass

    _w.showwarning = _show

    try:
        import tqdm.std as _tstd
        _orig_init = _tstd.tqdm.__init__

        def _patched_init(self, *a, **kw):
            kw["disable"] = True
            _orig_init(self, *a, **kw)

        _tstd.tqdm.__init__ = _patched_init
    except ImportError:
        pass


def main() -> None:
    p = argparse.ArgumentParser(prog="experiment_runner.run_round")
    p.add_argument("--round-dir", required=True, type=Path)
    args = p.parse_args()
    round_dir: Path = args.round_dir

    _self_promote_to_running(round_dir)
    _route_warnings_and_silence_tqdm(round_dir)

    sfd_args = json.loads((round_dir / "args.json").read_text())
    source = (round_dir / "code.py").read_text()

    tree = strip_to_definitions(source)
    ns: dict = {"__name__": "_runner_sfd", "__file__": str(round_dir / "code.py")}
    exec(compile(tree, str(round_dir / "code.py"), "exec"), ns)

    sfd_identifier = sfd_args["sfd_identifier"]
    sfd = ns[sfd_identifier]
    if inspect.isclass(sfd):
        sfd = sfd()

    import limen
    method_name = sfd_args["data_source"]["method"]
    method_params = sfd_args["data_source"]["params"]
    historical = limen.HistoricalData()
    data = getattr(historical, method_name)(**method_params)
    if data is None:
        data = historical.data  # older Limen versions stored result on instance
    uel = limen.UniversalExperimentLoop(data=data, sfd=sfd)

    round_id = round_dir.name
    uel_kwargs = dict(sfd_args["uel_run"])
    uel_kwargs["experiment_name"] = f"{uel_kwargs['experiment_name']}__r{round_id}"
    uel.run(**uel_kwargs)

    # Persist round-level metadata for the round-status section to read.
    # period.json carries the test-period dates Limen actually used
    # (sourced from uel._alignment, populated per permutation) plus the
    # authoritative kline_size derived from the row spacing of the data
    # the round was fed — not the nominal value from args.json, which
    # might disagree if the SFD massaged params before fetch.
    #
    # Scope this in try/except so a metadata-extraction failure does NOT
    # propagate and cause the watcher to mark a successful round as
    # "failed" (the real work — uel.run + results.csv — is already done
    # by the time we get here). Limen's _alignment is a private
    # attribute with no stability contract; Polars datetime ops can
    # raise on unusual data shapes. Both are known-fragile relative to
    # the core deliverable, so we contain the blast radius.
    # traceback.print_exc() lands in stderr.log, so the failure is
    # visible (fail-loud), not swallowed.
    #
    # Write is atomic: temp + os.replace, mirroring state.py and the
    # /working endpoint — the reader (round_page) does exists() then
    # json.loads(), so a partial-write window would yield JSONDecodeError.
    try:
        period = _collect_period_info(data, uel)
        if period:
            tmp = round_dir / "period.json.tmp"
            tmp.write_text(json.dumps(period, indent=2, default=str))
            os.replace(tmp, round_dir / "period.json")
    except Exception:
        import traceback
        traceback.print_exc()


def _collect_period_info(data, uel) -> dict:
    out: dict = {}

    # Kline size from actual data row spacing. Mode of consecutive
    # datetime diffs is the bar size in seconds. Skip if the data has no
    # 'datetime' column (custom SFD with non-time-series data).
    if "datetime" in data.columns:
        diffs = data["datetime"].diff().drop_nulls()
        if len(diffs) > 0:
            out["kline_size_seconds"] = int(diffs.dt.total_seconds().mode()[0])

    # Test-period bounds across every permutation Limen ran. When
    # split_config is fixed for the round, all entries match; when it is
    # swept via round_params, taking min(first) / max(last) gives the
    # widest window the round's results cover. all_permutations_same_period
    # tells callers whether the round-level summary is exact or aggregate.
    alignment = getattr(uel, "_alignment", None) or []
    if alignment:
        firsts = [a["first_test_datetime"] for a in alignment if "first_test_datetime" in a]
        lasts = [a["last_test_datetime"] for a in alignment if "last_test_datetime" in a]
        if firsts and lasts:
            out["test_period_start"] = str(min(firsts))
            out["test_period_end"] = str(max(lasts))
            out["all_permutations_same_period"] = (
                len(set(firsts)) == 1 and len(set(lasts)) == 1
            )
            out["permutation_count"] = len(alignment)

    return out


if __name__ == "__main__":
    main()
