from __future__ import annotations

import argparse
import json
import os
import subprocess
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


def _limen_cli() -> Path:
    """The `limen` console script, installed next to the running
    interpreter (the venv / image bin directory). Every round is executed
    by this CLI — experiment_runner never orchestrates Limen in-process,
    so a round's artefacts are exactly what `limen run` produced, with no
    reimplementation that could drift from the authoritative tool."""
    cli = Path(sys.executable).parent / "limen"
    if not cli.exists():
        print(f"run_round: `limen` CLI not found at {cli}", file=sys.stderr)
        sys.exit(1)
    return cli


def _collect_period_info(limen_output: Path) -> dict:
    """Round metadata for the round-status section, read straight from
    `limen run`'s own artefacts — kline_size from the manifest echoed into
    metadata.json; the permutation count and the test-period bounds from
    round_data.jsonl, where each line's `alignment` states the first/last
    test datetime Limen aligned that permutation to. Nothing is
    reconstructed or inferred; a field Limen does not state plainly stays
    absent. The bounds span every permutation (min first / max last), the
    widest window the round's results cover — indicator warm-up shifts the
    first usable datetime per permutation."""
    out: dict = {}
    meta_path = limen_output / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        params = (
            meta.get("yaml_reference", {})
            .get("sfd", {}).get("manifest", {})
            .get("data_source", {}).get("params", {})
        )
        kline_size = params.get("kline_size")
        if isinstance(kline_size, int):
            out["kline_size_seconds"] = kline_size
    rd_path = limen_output / "round_data.jsonl"
    if rd_path.exists():
        firsts: list[str] = []
        lasts: list[str] = []
        count = 0
        for line in rd_path.read_text().splitlines():
            if not line.strip():
                continue
            count += 1
            alignment = json.loads(line).get("alignment") or {}
            first = alignment.get("first_test_datetime")
            last = alignment.get("last_test_datetime")
            if first is not None:
                firsts.append(first)
            if last is not None:
                lasts.append(last)
        out["permutation_count"] = count
        if firsts and lasts:
            out["test_period_start"] = min(firsts)
            out["test_period_end"] = max(lasts)
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="experiment_runner.run_round")
    p.add_argument("--round-dir", required=True, type=Path)
    args = p.parse_args()
    round_dir: Path = args.round_dir

    _self_promote_to_running(round_dir)

    # The whole experiment is run by the `limen` CLI. `limen run` parses,
    # validates, compiles, and executes the round's manifest.yml — the same
    # binary, the same path, every round. experiment_runner adds no
    # orchestration of its own. limen's stdout/stderr inherit this
    # process's, so they land in the round's stdout.log / stderr.log;
    # TQDM_DISABLE keeps limen's progress bars out of the logs.
    limen = _limen_cli()
    result = subprocess.run(
        [str(limen), "run", str(round_dir / "manifest.yml")],
        cwd=str(round_dir),
        env={**os.environ, "TQDM_DISABLE": "1"},
    )
    if result.returncode != 0:
        print(f"run_round: `limen run` exited {result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)

    # `limen run` writes its artefacts to ./results/<name>_<timestamp>/ —
    # results.csv, metadata.json, round_data.jsonl, and a copy of the
    # manifest. Relocate that one directory to the fixed name `limen_output`
    # so the results / search / bundle layer has a predictable path. Only
    # the directory is renamed; its contents are limen's, untouched.
    results_parent = round_dir / "results"
    subdirs = (
        [d for d in sorted(results_parent.iterdir()) if d.is_dir()]
        if results_parent.is_dir() else []
    )
    if not subdirs:
        print("run_round: `limen run` produced no output directory", file=sys.stderr)
        sys.exit(1)
    os.replace(subdirs[-1], round_dir / "limen_output")
    if not list(results_parent.iterdir()):
        results_parent.rmdir()

    # period.json — kline_size + permutation count, taken from limen's
    # artefacts (see _collect_period_info). Contained so a metadata read
    # failure cannot flip a finished round to "failed"; the write is
    # atomic since round_page does exists() then json.loads().
    try:
        period = _collect_period_info(round_dir / "limen_output")
        if period:
            tmp = round_dir / "period.json.tmp"
            tmp.write_text(json.dumps(period, indent=2))
            os.replace(tmp, round_dir / "period.json")
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
