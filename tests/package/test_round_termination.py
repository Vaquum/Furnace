"""Tests for the round-termination fix: a stopped round always reaches a
terminal state.

run_round.py supervises the `limen run` child — it forwards the stop
signal to limen and stays alive to reap it, so limen is never orphaned
into an unreaped zombie that would hold the round's process group open
and strand the round on `terminating`. The terminate endpoint signals
the supervisor and returns at once; limen runs its full cooperative
shutdown (resumable checkpoint included) and a daemon backstop SIGKILLs
the group only if limen never exits. The deploy container runs an init
process so any orphan from limen's own multiprocessing children is
reaped too.

Source-level checks against run_round.py, app.py, and docker-compose.yml
— the codebase's test idiom (app.py cannot be imported without fastapi,
so the tests assert on source text rather than running the app)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "experiment_runner"


def test_run_round_runs_limen_under_a_supervised_popen() -> None:
    """run_round.py must hold a handle on the `limen run` child so it can
    forward the stop signal and reap the child. subprocess.run() returns
    no handle — it cannot be supervised, and is what let limen be
    orphaned when a round was terminated."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    assert "subprocess.Popen(" in text
    assert "subprocess.run(" not in text


def test_run_round_catches_the_stop_signal() -> None:
    """run_round.py installs SIGTERM and SIGINT handlers — the round's
    stop signal — instead of dying on the default action, which would
    orphan the limen child onto the container's non-reaping PID 1."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    assert "signal.signal(signal.SIGTERM," in text
    assert "signal.signal(signal.SIGINT," in text


def test_run_round_forwards_to_limen_and_stays_alive_to_reap_it() -> None:
    """On the stop signal run_round.py forwards SIGTERM to the limen child
    and then reaps it via proc.wait(). It must NOT exit on the signal:
    exiting first orphans limen, and the orphan becomes the unreaped
    zombie that strands the round on `terminating`."""
    text = (PKG_DIR / "run_round.py").read_text(encoding="utf-8")
    assert "proc.send_signal(signal.SIGTERM)" in text
    assert "proc.wait()" in text
    # The stop handler itself must not exit the supervisor process.
    start = text.index("def _on_stop(")
    handler = text[start:start + 220]
    assert "sys.exit" not in handler
    assert "os._exit" not in handler


def test_terminate_signals_the_supervisor_and_returns_at_once() -> None:
    """terminate_round signals run_round.py — the supervisor — with
    os.kill and hands escalation to the daemon backstop. The old path is
    gone from the endpoint: no group-wide killpg, no in-request poll loop
    with a 5-second guillotine that cut limen's cooperative shutdown
    short, and no 504 that left the round stuck on `terminating`."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    start = text.index("def terminate_round(")
    body = text[start:text.index("@app.", start)]   # up to the next route
    assert "os.kill(pid, signal.SIGTERM)" in body
    assert "_terminate_backstop" in body
    # Signals the leader (os.kill), not the whole group; no in-request
    # wait loop; no 504. Escalation lives in the daemon backstop.
    assert "killpg" not in body
    assert "time.monotonic()" not in body
    assert "504" not in body


def test_terminate_backstop_force_kills_the_group_after_a_grace() -> None:
    """A daemon backstop SIGKILLs the round's whole process group if it
    has not exited within the grace window — the guarantee that a stopped
    round always reaches a terminal state even if limen never exits.
    limen's graceful shutdown is the normal path and ends the group well
    inside the grace."""
    text = (PKG_DIR / "app.py").read_text(encoding="utf-8")
    assert "def _terminate_backstop(" in text
    assert "_TERMINATE_GRACE_SECONDS" in text
    start = text.index("def _terminate_backstop(")
    body = text[start:start + 800]
    assert "os.killpg(pgid, signal.SIGKILL)" in body


def test_compose_runs_an_init_process_to_reap_orphans() -> None:
    """The deploy container runs an init (tini) as PID 1 via `init: true`,
    so limen's own multiprocessing children — if orphaned when limen
    exits — are reaped. Without an init the experiment_runner server is
    PID 1 and never reaps; an orphan zombie holds the round's process
    group open and the round hangs on `terminating`."""
    text = (REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "init: true" in text
