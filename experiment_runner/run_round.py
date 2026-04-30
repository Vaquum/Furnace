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
    if not method_params:
        # No user overrides — let UEL drive data loading from the SFD's
        # manifest natively. Honours set_data_source(method, params) declared
        # in code instead of calling get_spot_klines with empty kwargs and
        # getting library defaults the SFD did not ask for.
        uel = limen.UniversalExperimentLoop(sfd=sfd)
    else:
        historical = limen.HistoricalData()
        data = getattr(historical, method_name)(**method_params)
        if data is None:
            data = historical.data  # older Limen versions stored result on instance
        uel = limen.UniversalExperimentLoop(data=data, sfd=sfd)

    round_id = round_dir.name
    uel_kwargs = dict(sfd_args["uel_run"])
    uel_kwargs["experiment_name"] = f"{uel_kwargs['experiment_name']}__r{round_id}"
    uel.run(**uel_kwargs)


if __name__ == "__main__":
    main()
