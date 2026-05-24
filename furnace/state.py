from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ACTIVE_STATES = ("pending", "running", "terminating")
TERMINAL_STATES = ("completed", "terminated", "failed", "unknown_after_restart")


class StateMachine:
    def __init__(self, root: Path):
        self.root = root
        self._registry: dict[Path, int] = {}
        self._registry_lock = threading.RLock()
        self._exp_locks: dict[str, threading.Lock] = {}
        self._exp_locks_lock = threading.Lock()

    def exp_lock(self, experiment_id: str) -> threading.Lock:
        with self._exp_locks_lock:
            return self._exp_locks.setdefault(experiment_id, threading.Lock())

    def register(self, round_dir: Path, pgid: int) -> None:
        with self._registry_lock:
            self._registry[round_dir] = pgid

    def unregister(self, round_dir: Path) -> None:
        with self._registry_lock:
            self._registry.pop(round_dir, None)

    def registry_snapshot(self) -> list[tuple[Path, int]]:
        with self._registry_lock:
            return list(self._registry.items())


@contextmanager
def status_flock(round_dir: Path):
    lock_path = round_dir / ".status.lock"
    lock_path.touch(exist_ok=True)
    f = open(lock_path, "r+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def read_status(round_dir: Path) -> dict:
    return json.loads((round_dir / "status.json").read_text())


def write_status_atomic(round_dir: Path, data: dict) -> None:
    tmp = round_dir / "status.json.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, round_dir / "status.json")


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def mark_terminating(round_dir: Path) -> bool:
    with status_flock(round_dir):
        current = read_status(round_dir)
        state = current["state"]
        if state == "pending":
            return False
        if state not in ("running", "terminating"):
            return False
        if state == "running":
            current["state"] = "terminating"
            write_status_atomic(round_dir, current)
        return True


def shutdown_mark_terminating(round_dir: Path, trusted_pgid: int) -> bool:
    with status_flock(round_dir):
        current = read_status(round_dir)
        state = current["state"]
        if state == "pending":
            current["state"] = "terminating"
            current["pid"] = trusted_pgid
            write_status_atomic(round_dir, current)
            return True
        if state == "running":
            current["state"] = "terminating"
            write_status_atomic(round_dir, current)
            return True
        if state == "terminating":
            return True
        return False


def finalize_status(round_dir: Path, *, rc: int | None = None) -> bool:
    with status_flock(round_dir):
        current = read_status(round_dir)
        state = current["state"]
        if state in TERMINAL_STATES:
            return True
        if state == "pending":
            current["state"] = "failed"
            current["exit_code"] = None
            current["ended_at"] = now_iso()
            write_status_atomic(round_dir, current)
            return True
        try:
            os.killpg(current["pid"], 0)
        except ProcessLookupError:
            pass
        else:
            return False
        if state == "terminating":
            current["state"] = "terminated"
            current["exit_code"] = None
        elif rc is not None:
            current["state"] = "completed" if rc == 0 else "failed"
            current["exit_code"] = rc
        else:
            current["state"] = "unknown_after_restart"
            current["exit_code"] = None
        current["ended_at"] = now_iso()
        write_status_atomic(round_dir, current)
        return True


def watcher_thread(popen, round_dir: Path, sm: StateMachine) -> None:
    rc = popen.wait()
    while True:
        try:
            os.killpg(popen.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.25)
    if finalize_status(round_dir, rc=rc):
        sm.unregister(round_dir)


def start_watcher(popen, round_dir: Path, sm: StateMachine) -> None:
    t = threading.Thread(target=watcher_thread, args=(popen, round_dir, sm), daemon=True)
    t.start()


def restart_poll_thread(round_dir: Path, pgid: int, sm: StateMachine) -> None:
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(5)
    if finalize_status(round_dir, rc=None):
        sm.unregister(round_dir)


def reconcile_at_startup(sm: StateMachine) -> None:
    experiments = sm.root / "experiments"
    if not experiments.exists():
        return
    for exp_dir in experiments.iterdir():
        rounds_dir = exp_dir / "rounds"
        if not rounds_dir.exists():
            continue
        for round_dir in rounds_dir.iterdir():
            status_path = round_dir / "status.json"
            if not status_path.exists():
                continue
            current = read_status(round_dir)
            state = current.get("state")
            if state not in ACTIVE_STATES:
                continue
            if state == "pending":
                finalize_status(round_dir, rc=None)
                continue
            pid = current.get("pid")
            if not isinstance(pid, int):
                finalize_status(round_dir, rc=None)
                continue
            if state == "terminating":
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                finalize_status(round_dir, rc=None)
                continue
            sm.register(round_dir, pid)
            t = threading.Thread(
                target=restart_poll_thread, args=(round_dir, pid, sm), daemon=True,
            )
            t.start()


def shutdown_handler(sm: StateMachine) -> None:
    snapshot = sm.registry_snapshot()
    for round_dir, pgid in snapshot:
        if shutdown_mark_terminating(round_dir, pgid):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def is_experiment_active(experiment_dir: Path) -> bool:
    rounds_dir = experiment_dir / "rounds"
    if not rounds_dir.exists():
        return False
    for round_dir in rounds_dir.iterdir():
        status_path = round_dir / "status.json"
        if not status_path.exists():
            continue
        try:
            data = read_status(round_dir)
        except json.JSONDecodeError:
            continue
        if data.get("state") in ACTIVE_STATES:
            return True
    return False


@contextmanager
def version_flock(experiment_dir: Path):
    lock_path = experiment_dir / ".version.lock"
    lock_path.touch(exist_ok=True)
    f = open(lock_path, "r+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def read_version(experiment_dir: Path) -> int:
    p = experiment_dir / "version.json"
    if not p.exists():
        return 0
    return int(json.loads(p.read_text())["version"])


def write_version_atomic(experiment_dir: Path, version: int) -> None:
    tmp = experiment_dir / "version.json.tmp"
    tmp.write_text(json.dumps({"version": version}))
    os.replace(tmp, experiment_dir / "version.json")
