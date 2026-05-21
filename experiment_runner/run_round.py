from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .state import now_iso, read_status, status_flock, write_status_atomic


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


def _collect_period_info(yaml_dict: dict, uel) -> dict:
    """Round-level metadata for the round-status section. kline_size comes
    straight from the manifest's declarative `data_source` params; the
    test-period bounds come from `uel._alignment` (populated per
    permutation, so min(first) / max(last) is the widest window the
    round's results cover)."""
    out: dict = {}

    manifest = (yaml_dict.get("sfd") or {}).get("manifest") or {}
    ds_params = (manifest.get("data_source") or {}).get("params") or {}
    kline_size = ds_params.get("kline_size")
    if isinstance(kline_size, int):
        out["kline_size_seconds"] = kline_size

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


def main() -> None:
    p = argparse.ArgumentParser(prog="experiment_runner.run_round")
    p.add_argument("--round-dir", required=True, type=Path)
    args = p.parse_args()
    round_dir: Path = args.round_dir

    _self_promote_to_running(round_dir)
    _route_warnings_and_silence_tqdm(round_dir)

    # The whole experiment definition is the YAML manifest — there is no
    # user Python to exec. Limen's own pipeline parses, validates, compiles,
    # and runs it (this mirrors `limen run`). Imports land after the tqdm
    # patch so Limen's progress bars stay silenced.
    from limen.experiment import GridStrategy, RandomStrategy
    from limen.experiment.experiment_core import UniversalExperimentLoop
    from limen.experiment.param_domain import ParamDomain
    from limen.yaml.compiler import CompiledSFD
    from limen.yaml.parser import parse
    from limen.yaml.validator import validate

    yaml_dict, parse_errors = parse(round_dir / "manifest.yml")
    if parse_errors:
        for e in parse_errors:
            loc = f" (line {e.line})" if getattr(e, "line", None) else ""
            print(f"manifest parse error{loc}: {e.message}", file=sys.stderr)
        sys.exit(1)

    result = validate(yaml_dict)
    for w in result.warnings:
        print(f"manifest warning: {w.message}", file=sys.stderr)
    if not result.valid:
        for e in result.errors:
            loc = f" (line {e.line})" if getattr(e, "line", None) else ""
            path = f" [{e.path}]" if getattr(e, "path", None) else ""
            print(f"manifest error{path}{loc}: {e.message}", file=sys.stderr)
        sys.exit(1)

    uel_cfg = yaml_dict.get("uel") or {}
    sfd_cfg = yaml_dict.get("sfd") or {}
    params = {k: list(v) for k, v in (sfd_cfg.get("params") or {}).items()}
    domain = ParamDomain(params)
    strategy_type = (uel_cfg.get("search_strategy") or {}).get("type", "random")
    search_strategy = GridStrategy(domain) if strategy_type == "grid" else RandomStrategy(domain)

    test_mode = (yaml_dict.get("metadata") or {}).get("mode", "development") == "development"
    compiled = CompiledSFD(yaml_dict)

    # round_dir is experiments/<eid>/rounds/<rid>. UEL names the results CSV
    # by the experiment_name passed to run() and writes it to the cwd (the
    # subprocess runs with cwd=round_dir), so the file lands at
    # round_dir/<eid>__r<rid>.csv — exactly where the results / search /
    # bundle layer expects it.
    eid = round_dir.parent.parent.name
    round_id = round_dir.name

    uel = UniversalExperimentLoop(
        sfd=compiled,
        search_strategy=search_strategy,
        test_mode=test_mode,
        feedback_interval=int(uel_cfg.get("feedback_interval", 100)),
        checkpoint_interval=int(uel_cfg.get("checkpoint_interval", 1000)),
        yaml_reference=dict(yaml_dict),
    )
    uel.run(
        experiment_name=f"{eid}__r{round_id}",
        n_permutations=int(uel_cfg.get("n_permutations", 50)),
        prep_each_round=bool(uel_cfg.get("prep_each_round", True)),
    )

    # period.json — contained in try/except so a metadata-extraction failure
    # does not flip a finished round to "failed"; traceback.print_exc lands
    # it in stderr.log (fail-loud, not swallowed). Atomic write (temp +
    # os.replace) since round_page does exists() then json.loads() and a
    # partial-write window would raise JSONDecodeError.
    try:
        period = _collect_period_info(yaml_dict, uel)
        if period:
            tmp = round_dir / "period.json.tmp"
            tmp.write_text(json.dumps(period, indent=2, default=str))
            os.replace(tmp, round_dir / "period.json")
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
