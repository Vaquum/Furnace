from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

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
    by this CLI — furnace never orchestrates Limen in-process,
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


def _link_limen_output(round_dir: Path) -> None:
    """Bridge ``<round>/limen_output`` to Limen's ``results/limen_output``.

    Limen writes its artefacts under ``<cwd>/results/<uel.output_path>`` — it
    always prepends a fixed ``results/`` base — so with the round's
    ``uel.output_path: limen_output`` the output lands in
    ``<round_dir>/results/limen_output/``. Furnace's contract is
    ``<round_dir>/limen_output/`` (the round-status poll, the round page,
    inference, and the post-run check below all read it). A relative symlink
    reconciles the two in one place, so every reader keeps using
    ``limen_output/`` unchanged. It is created before ``limen run`` starts, so
    the live poll resolves the moment limen writes its first row; it dangles
    harmlessly until then.
    """
    link = round_dir / "limen_output"
    if link.is_symlink() or link.exists():
        return
    link.symlink_to(Path("results") / "limen_output")


def main() -> None:
    p = argparse.ArgumentParser(prog="furnace.run_round")
    p.add_argument("--round-dir", required=True, type=Path)
    args = p.parse_args()
    round_dir: Path = args.round_dir

    _self_promote_to_running(round_dir)
    _link_limen_output(round_dir)

    # The whole experiment is run by the `limen` CLI. `limen run` parses,
    # validates, compiles, and executes the round's manifest.yml — the same
    # binary, the same path, every round. furnace adds no
    # orchestration of its own. limen's stdout/stderr inherit this
    # process's, so they land in the round's stdout.log / stderr.log;
    # TQDM_DISABLE keeps limen's progress bars out of the logs.
    limen = _limen_cli()
    proc = subprocess.Popen(
        [str(limen), "run", str(round_dir / "manifest.yml")],
        cwd=str(round_dir),
        env={**os.environ, "TQDM_DISABLE": "1"},
    )

    # run_round.py supervises the `limen run` child for its whole life.
    # The round's stop signal (SIGTERM / SIGINT, sent to this process by
    # the terminate endpoint) is forwarded to limen, which catches it and
    # runs its own cooperative shutdown — finish the in-flight permutation,
    # write a resumable checkpoint, finalize its artefacts — then exits.
    # run_round.py stays here until limen exits so it REAPS limen itself:
    # were run_round.py to exit first (the default SIGTERM action), limen
    # would be orphaned onto the container's PID 1, which does not reap,
    # and the resulting zombie would hold the round's process group open
    # and leave the round stuck forever on `terminating`.
    #
    # The handler only sets a flag; the poll loop does the forwarding from
    # the main thread. Calling proc.send_signal() inside the handler can
    # deadlock — the handler runs on the main thread, which may already
    # hold Popen's internal waitpid lock inside a blocking wait().
    stop_requested = False

    def _on_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)

    forwarded = False
    while proc.poll() is None:
        if stop_requested and not forwarded:
            proc.send_signal(signal.SIGTERM)
            forwarded = True
        time.sleep(0.25)
    returncode = proc.wait()
    if returncode != 0:
        print(f"run_round: `limen run` exited {returncode}", file=sys.stderr)
        sys.exit(returncode)

    # `limen run` writes its artefacts — results.csv, metadata.json,
    # round_data.jsonl, and a copy of the manifest — into
    # <round_dir>/results/limen_output/ (Limen prepends a fixed `results/`
    # base to `uel.output_path: limen_output`). _link_limen_output above
    # symlinked <round_dir>/limen_output onto it, so the round-status poll
    # grows results.csv live and this is_dir() check resolves through the link.
    limen_output = round_dir / "limen_output"
    if not limen_output.is_dir():
        print(
            "run_round: `limen run` produced no results/limen_output/ — the "
            "round's manifest must set `uel.output_path: limen_output`",
            file=sys.stderr,
        )
        sys.exit(1)

    # period.json — kline_size + permutation count, taken from limen's
    # artefacts (see _collect_period_info). Contained so a metadata read
    # failure cannot flip a finished round to "failed"; the write is
    # atomic since round_page does exists() then json.loads().
    try:
        period = _collect_period_info(limen_output)
        if period:
            tmp = round_dir / "period.json.tmp"
            tmp.write_text(json.dumps(period, indent=2))
            os.replace(tmp, round_dir / "period.json")
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
