from __future__ import annotations

import difflib
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .state import (
    ACTIVE_STATES,
    StateMachine,
    finalize_status,
    is_experiment_active,
    mark_terminating,
    now_iso,
    read_status,
    read_version,
    reconcile_at_startup,
    shutdown_handler,
    start_watcher,
    status_flock,
    version_flock,
    write_status_atomic,
    write_version_atomic,
)


import ast as _ast

PKG_DIR = Path(__file__).resolve().parent
LAUNCHER_PATH = PKG_DIR / "run_round.py"
ASSET_VERSION = str(int(time.time()))


_WARNING_HEADER = re.compile(r"^[/\\].+?:\d+:\s+\w*Warning:.*$")
_TRACEBACK_HEADER = re.compile(r"^Traceback \(most recent call last\):")


def _split_stderr_content(text: str) -> tuple[str, str]:
    """Classify stderr.log content into (errors, warnings).

    A warning block starts with `<path>:<line>: <Name>Warning:` and continues
    until the next warning header, a Traceback line, or EOF. Everything else
    (including tracebacks and any other prints) is treated as errors.
    Catches sklearn/scipy warnings emitted via `warnings.warn` from inside
    `warnings.catch_warnings()` blocks that bypass our showwarning override.
    """
    if not text:
        return "", ""
    errors: list[str] = []
    warning_blocks: list[str] = []
    cur_warning: list[str] = []
    in_warning = False
    for line in text.splitlines(keepends=True):
        if _WARNING_HEADER.match(line):
            if cur_warning:
                warning_blocks.append("".join(cur_warning))
            cur_warning = [line]
            in_warning = True
        elif _TRACEBACK_HEADER.match(line):
            if cur_warning:
                warning_blocks.append("".join(cur_warning))
                cur_warning = []
            in_warning = False
            errors.append(line)
        elif in_warning:
            cur_warning.append(line)
        else:
            errors.append(line)
    if cur_warning:
        warning_blocks.append("".join(cur_warning))
    return "".join(errors), "".join(warning_blocks)


def _strip_for_diff(source: str) -> str:
    """Drop top-level imports and `_*` helper functions from source — convention
    that those don't participate in the round-to-round diff. Everything else
    (constants, classes, named functions, the SFD itself) is preserved verbatim."""
    if not source.strip():
        return ""
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return source
    excluded: set[int] = set()
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            start = node.lineno
            end = node.end_lineno or start
            excluded.update(range(start, end + 1))
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name.startswith("_"):
            start = node.lineno
            end = node.end_lineno or start
            excluded.update(range(start, end + 1))
    lines = source.splitlines(keepends=True)
    return "".join(line for i, line in enumerate(lines, start=1) if i not in excluded)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
DEFAULT_ARGS: dict = {
    "sfd_identifier": "",
    "data_source": {"method": "get_spot_klines", "params": {}},
    "uel_run": {
        "experiment_name": "",
        "n_permutations": 100,
        "prep_each_round": True,
        "random_search": True,
        "maintain_details_in_params": False,
    },
}


def _slugify(name: str) -> str:
    s = SLUG_RE.sub("-", name.strip()).strip("-")
    return s or "experiment"


def _allocate_experiment_id(experiments_dir: Path, name: str) -> str:
    base = _slugify(name)
    candidate = base
    n = 2
    while (experiments_dir / candidate).exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _allocate_round_id(rounds_dir: Path) -> str:
    rounds_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(p.name) for p in rounds_dir.iterdir() if p.name.isdigit())
    nxt = (existing[-1] + 1) if existing else 1
    return f"{nxt:04d}"


def _read_working(experiment_dir: Path) -> tuple[str, dict, int]:
    with version_flock(experiment_dir):
        code_path = experiment_dir / "code.py"
        args_path = experiment_dir / "args.json"
        code = code_path.read_text() if code_path.exists() else ""
        args = json.loads(args_path.read_text()) if args_path.exists() else DEFAULT_ARGS
        version = read_version(experiment_dir)
    return code, args, version


def _validate_args(args: dict) -> None:
    sid = args.get("sfd_identifier", "")
    if not isinstance(sid, str) or not NAME_RE.match(sid):
        raise HTTPException(400, "sfd_identifier must match ^[A-Za-z0-9_-]+$")
    method = args.get("data_source", {}).get("method", "")
    if not isinstance(method, str) or not method:
        raise HTTPException(400, "data_source.method is required")
    params = args.get("data_source", {}).get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(400, "data_source.params must be an object")


def _all_experiments(root: Path) -> list[dict]:
    experiments = root / "experiments"
    if not experiments.exists():
        return []
    out = []
    for exp_dir in sorted(experiments.iterdir()):
        rounds = _experiment_rounds(exp_dir)
        latest = rounds[0] if rounds else None
        latest_at = ""
        if latest:
            latest_at = latest.get("ended_at") or latest.get("started_at") or ""
        out.append({
            "id": exp_dir.name,
            "rounds": rounds,
            "round_count": len(rounds),
            "latest_state": latest.get("state") if latest else "",
            "latest_at": latest_at,
        })
    out.sort(key=lambda e: e["latest_at"] or "", reverse=True)
    return out


def _experiment_rounds(experiment_dir: Path) -> list[dict]:
    rounds_dir = experiment_dir / "rounds"
    out: list[dict] = []
    if not rounds_dir.exists():
        return out
    for round_dir in sorted(rounds_dir.iterdir(), reverse=True):
        sp = round_dir / "status.json"
        if not sp.exists():
            continue
        try:
            st = read_status(round_dir)
        except json.JSONDecodeError:
            continue
        out.append({
            "round_id": round_dir.name,
            "state": st.get("state"),
            "pid": st.get("pid"),
            "started_at": st.get("started_at"),
            "ended_at": st.get("ended_at"),
            "exit_code": st.get("exit_code"),
        })
    return out


def create_app(root: Path) -> FastAPI:
    templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))
    templates.env.globals["asset_version"] = ASSET_VERSION
    sm = StateMachine(root)
    reconcile_at_startup(sm)

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")

    @app.on_event("shutdown")
    def _on_shutdown() -> None:
        shutdown_handler(sm)

    def _exp_dir(experiment_id: str) -> Path:
        d = root / "experiments" / experiment_id
        if not d.exists():
            raise HTTPException(404, "experiment not found")
        return d

    def _round_dir(experiment_id: str, round_id: str) -> Path:
        d = _exp_dir(experiment_id) / "rounds" / round_id
        if not d.exists():
            raise HTTPException(404, "round not found")
        return d

    @app.get("/", response_class=HTMLResponse)
    def jobs(request: Request):
        experiments = _all_experiments(root)
        return templates.TemplateResponse(
            "jobs.html", {"request": request, "experiments": experiments},
        )

    @app.get("/_partials/jobs", response_class=HTMLResponse)
    def jobs_partial(request: Request):
        experiments = _all_experiments(root)
        return templates.TemplateResponse(
            "_jobs_rows.html", {"request": request, "experiments": experiments},
        )

    @app.delete("/experiments/{experiment_id}")
    def delete_experiment(experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        if is_experiment_active(exp_dir):
            raise HTTPException(409, "experiment has an active round; terminate it first")
        import shutil
        shutil.rmtree(exp_dir)
        return {"ok": True}

    @app.post("/experiments")
    async def create_experiment(request: Request):
        form = await request.form()
        name = form.get("name", "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        experiments_dir = root / "experiments"
        experiments_dir.mkdir(parents=True, exist_ok=True)
        eid = _allocate_experiment_id(experiments_dir, name)
        exp_dir = experiments_dir / eid
        (exp_dir / "rounds").mkdir(parents=True, exist_ok=True)
        (exp_dir / "code.py").write_text("")
        seeded_args = json.loads(json.dumps(DEFAULT_ARGS))
        seeded_args["uel_run"]["experiment_name"] = eid
        (exp_dir / "args.json").write_text(json.dumps(seeded_args, indent=2))
        write_version_atomic(exp_dir, 0)
        return RedirectResponse(f"/experiments/{eid}", status_code=303)

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(request: Request, experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        code, args, version = _read_working(exp_dir)
        rounds = _experiment_rounds(exp_dir)
        active = is_experiment_active(exp_dir)
        return templates.TemplateResponse(
            "experiment.html",
            {
                "request": request,
                "experiment_id": experiment_id,
                "code": code,
                "args": args,
                "args_json": json.dumps(args, indent=2),
                "version": version,
                "rounds": rounds,
                "active": active,
            },
        )

    @app.get("/_partials/experiments/{experiment_id}/rounds", response_class=HTMLResponse)
    def experiment_rounds_partial(request: Request, experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        rounds = _experiment_rounds(exp_dir)
        return templates.TemplateResponse(
            "_round_rows.html",
            {"request": request, "experiment_id": experiment_id, "rounds": rounds},
        )

    @app.post("/experiments/{experiment_id}/working")
    async def save_working(experiment_id: str, request: Request):
        exp_dir = _exp_dir(experiment_id)
        body = await request.json()
        if_match = body.get("if_match")
        code = body.get("code", "")
        args = body.get("args", {})
        if not isinstance(args, dict):
            raise HTTPException(400, "args must be an object")
        # experiment_name is always derived from the experiment id; ignore client value
        args.setdefault("uel_run", {})
        if isinstance(args["uel_run"], dict):
            args["uel_run"]["experiment_name"] = experiment_id
        with sm.exp_lock(experiment_id):
            with version_flock(exp_dir):
                current = read_version(exp_dir)
                if if_match != current:
                    return JSONResponse({"current_version": current}, status_code=409)
                tmp_code = exp_dir / "code.py.tmp"
                tmp_code.write_text(code)
                os.replace(tmp_code, exp_dir / "code.py")
                tmp_args = exp_dir / "args.json.tmp"
                tmp_args.write_text(json.dumps(args, indent=2))
                os.replace(tmp_args, exp_dir / "args.json")
                new_version = current + 1
                write_version_atomic(exp_dir, new_version)
        return {"version": new_version}

    @app.post("/experiments/{experiment_id}/rounds")
    async def start_round(experiment_id: str, request: Request):
        exp_dir = _exp_dir(experiment_id)
        body = await request.json()
        if_match = body.get("if_match")
        code = body.get("code", "")
        args = body.get("args", {})
        if not code.strip():
            raise HTTPException(400, "code editor is empty — paste your SFD before starting a round")
        if not isinstance(args, dict):
            raise HTTPException(400, "args must be an object")
        args.setdefault("uel_run", {})
        if isinstance(args["uel_run"], dict):
            args["uel_run"]["experiment_name"] = experiment_id
        _validate_args(args)
        sfd_id = args["sfd_identifier"]
        if sfd_id not in code:
            raise HTTPException(
                400,
                f"sfd_identifier '{sfd_id}' does not appear in the pasted code",
            )

        with sm.exp_lock(experiment_id):
            with version_flock(exp_dir):
                current_version = read_version(exp_dir)
                if if_match != current_version:
                    return JSONResponse({"current_version": current_version}, status_code=409)
                if is_experiment_active(exp_dir):
                    return JSONResponse(
                        {"error": "another round is active"}, status_code=409,
                    )
                tmp_code = exp_dir / "code.py.tmp"
                tmp_code.write_text(code)
                os.replace(tmp_code, exp_dir / "code.py")
                tmp_args = exp_dir / "args.json.tmp"
                tmp_args.write_text(json.dumps(args, indent=2))
                os.replace(tmp_args, exp_dir / "args.json")
                new_version = current_version + 1
                write_version_atomic(exp_dir, new_version)

            rounds_dir = exp_dir / "rounds"
            round_id = _allocate_round_id(rounds_dir)
            round_dir = rounds_dir / round_id
            round_dir.mkdir(parents=True, exist_ok=False)
            (round_dir / "code.py").write_text(code)
            (round_dir / "args.json").write_text(json.dumps(args, indent=2))
            stdout_path = round_dir / "stdout.log"
            stderr_path = round_dir / "stderr.log"
            stdout_path.touch()
            stderr_path.touch()

            write_status_atomic(round_dir, {
                "state": "pending",
                "pid": None,
                "started_at": now_iso(),
                "ended_at": None,
                "exit_code": None,
            })

            try:
                stdout_f = open(stdout_path, "w")
                stderr_f = open(stderr_path, "w")
                env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                env["PYTHONPATH"] = str(PKG_DIR.parent) + os.pathsep + env.get("PYTHONPATH", "")
                popen = subprocess.Popen(
                    [
                        sys.executable, "-u",
                        "-m", "experiment_runner.run_round",
                        "--round-dir", str(round_dir),
                    ],
                    cwd=str(round_dir),
                    stdout=stdout_f,
                    stderr=stderr_f,
                    env=env,
                    start_new_session=True,
                )
            except OSError as e:
                finalize_status(round_dir, rc=None)
                raise HTTPException(500, f"spawn failed: {e}")

            try:
                sm.register(round_dir, popen.pid)
            except Exception:
                try:
                    os.killpg(popen.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                group_dead = False
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    popen.poll()
                    try:
                        os.killpg(popen.pid, 0)
                    except ProcessLookupError:
                        group_dead = True
                        break
                    time.sleep(0.1)
                if group_dead:
                    with status_flock(round_dir):
                        st = read_status(round_dir)
                        if st["state"] in ("pending", "running"):
                            st.update(state="failed", exit_code=None, ended_at=now_iso())
                            write_status_atomic(round_dir, st)
                raise HTTPException(500, "round-start failed during registry handoff")

            start_watcher(popen, round_dir, sm)

        return {"version": new_version, "round_id": round_id}

    @app.get("/experiments/{experiment_id}/rounds/{round_id}", response_class=HTMLResponse)
    def round_page(request: Request, experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        st = read_status(round_dir)
        frozen_code = (round_dir / "code.py").read_text()
        frozen_args = json.loads((round_dir / "args.json").read_text())
        diff_text = ""
        prev_id = f"{int(round_id) - 1:04d}"
        prev_dir = round_dir.parent / prev_id
        if (prev_dir / "code.py").exists():
            prev_code = (prev_dir / "code.py").read_text()
            stripped_prev = _strip_for_diff(prev_code)
            stripped_this = _strip_for_diff(frozen_code)
            diff_text = "".join(difflib.unified_diff(
                stripped_prev.splitlines(keepends=True),
                stripped_this.splitlines(keepends=True),
                fromfile=f"r{prev_id}/code.py",
                tofile=f"r{round_id}/code.py",
                n=3,
            ))
        return templates.TemplateResponse(
            "round.html",
            {
                "request": request,
                "experiment_id": experiment_id,
                "round_id": round_id,
                "status": st,
                "frozen_code": frozen_code,
                "frozen_args_json": json.dumps(frozen_args, indent=2),
                "diff_text": diff_text,
                "active": st.get("state") in ACTIVE_STATES,
            },
        )

    @app.post("/experiments/{experiment_id}/rounds/{round_id}/terminate")
    def terminate_round(experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        st = read_status(round_dir)
        if st.get("state") == "pending":
            return JSONResponse(
                {"error": "round still spawning, retry shortly"}, status_code=409,
            )
        if not mark_terminating(round_dir):
            return JSONResponse({"error": "already terminal"}, status_code=409)
        pid = read_status(round_dir)["pid"]
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kdeadline = time.monotonic() + 1.0
            while time.monotonic() < kdeadline:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            if finalize_status(round_dir, rc=None):
                sm.unregister(round_dir)
            return {"ok": True}
        return JSONResponse(
            {"error": "process group still alive after SIGKILL grace"}, status_code=504,
        )

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/log", response_class=PlainTextResponse)
    def round_log(experiment_id: str, round_id: str, source: str = "stdout", since: int = 0):
        if source not in ("stdout", "stderr", "warnings", "all"):
            raise HTTPException(400, "source must be stdout, stderr, warnings, or all")
        round_dir = _round_dir(experiment_id, round_id)

        if source == "stdout":
            path = round_dir / "stdout.log"
            if not path.exists():
                return PlainTextResponse(
                    "", headers={"X-Next-Offset": "0", "X-Tail-Mode": "append"},
                )
            size = path.stat().st_size
            if since >= size:
                return PlainTextResponse(
                    "", headers={"X-Next-Offset": str(size), "X-Tail-Mode": "append"},
                )
            with open(path, "rb") as f:
                f.seek(since)
                data = f.read()
            return PlainTextResponse(
                data.decode("utf-8", errors="replace"),
                headers={"X-Next-Offset": str(size), "X-Tail-Mode": "append"},
            )

        # stderr, warnings, all are post-processed; client replaces, not appends.
        stderr_text = ""
        stderr_path = round_dir / "stderr.log"
        if stderr_path.exists():
            stderr_text = stderr_path.read_text(errors="replace")
        warnings_text = ""
        warnings_path = round_dir / "warnings.log"
        if warnings_path.exists():
            warnings_text = warnings_path.read_text(errors="replace")
        errors_only, warnings_from_stderr = _split_stderr_content(stderr_text)

        if source == "stderr":
            body = errors_only
        elif source == "warnings":
            body = warnings_from_stderr + warnings_text
        else:  # all
            stdout_path = round_dir / "stdout.log"
            stdout_text = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
            parts = []
            for label, txt in (("stdout", stdout_text), ("stderr", errors_only),
                               ("warnings", warnings_from_stderr + warnings_text)):
                if txt:
                    parts.append(f"=== {label} ===\n")
                    parts.append(txt)
                    if not txt.endswith("\n"):
                        parts.append("\n")
            body = "".join(parts)

        return PlainTextResponse(
            body, headers={"X-Next-Offset": str(len(body.encode("utf-8"))),
                           "X-Tail-Mode": "replace"},
        )

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/csv")
    def round_csv(experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        frozen_args = json.loads((round_dir / "args.json").read_text())
        ename = frozen_args["uel_run"]["experiment_name"]
        csv_path = round_dir / f"{ename}__r{round_id}.csv"
        headers = {"Content-Disposition": f'attachment; filename="{ename}__r{round_id}.csv"'}
        if not csv_path.exists():
            return PlainTextResponse("", media_type="text/csv", headers=headers)
        return PlainTextResponse(csv_path.read_text(), media_type="text/csv", headers=headers)

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/bundle")
    def round_bundle(experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        frozen_args = json.loads((round_dir / "args.json").read_text())
        ename = frozen_args["uel_run"]["experiment_name"]
        base = f"{ename}__r{round_id}"
        code_path = round_dir / "code.py"
        args_path = round_dir / "args.json"
        csv_path = round_dir / f"{base}.csv"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{base}.py", code_path.read_text())
            z.writestr(f"{base}.json", args_path.read_text())
            if csv_path.exists():
                z.writestr(f"{base}.csv", csv_path.read_text())

        headers = {"Content-Disposition": f'attachment; filename="{base}.zip"'}
        return Response(buf.getvalue(), media_type="application/zip", headers=headers)

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/results", response_class=HTMLResponse)
    def round_results_page(request: Request, experiment_id: str, round_id: str):
        _round_dir(experiment_id, round_id)
        cols = _RESULTS_COLUMNS
        return templates.TemplateResponse(
            "results.html",
            {
                "request": request,
                "experiment_id": experiment_id,
                "round_id": round_id,
                "columns": [{"key": c, "label": _display_label(c),
                             "is_int": c in _RESULTS_INT_COLS} for c in cols],
            },
        )

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/results.json")
    def round_results_json(
        experiment_id: str,
        round_id: str,
        offset: int = 0,
        limit: int = 40,
        sort: str | None = None,
        direction: str = "asc",
    ):
        round_dir = _round_dir(experiment_id, round_id)
        return JSONResponse(_compute_results_page(round_dir, offset, limit, sort, direction))

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/stats")
    def round_stats(experiment_id: str, round_id: str):
        return JSONResponse(_compute_round_stats(_round_dir(experiment_id, round_id)))

    return app


_RESULTS_COLUMNS: tuple[str, ...] = (
    "id",
    "backtest_trades_count",
    "precision",
    "recall",
    "confusion_tp_mean_return_pct",
    "confusion_fp_mean_return_pct",
    "backtest_total_return_net_pct",
    "backtest_sharpe_per_bar",
    "backtest_max_drawdown_pct",
    "backtest_mean_kelly_pct",
)
_RESULTS_INT_COLS: frozenset[str] = frozenset({"id", "backtest_trades_count"})


def _display_label(col: str) -> str:
    for prefix in ("backtest_", "confusion_"):
        if col.startswith(prefix):
            col = col[len(prefix):]
            break
    for tok in ("mean_", "total_"):
        col = col.replace(tok, "")
    return col


def _compute_results_page(
    round_dir: Path, offset: int, limit: int, sort: str | None, direction: str,
) -> dict:
    frozen_args = json.loads((round_dir / "args.json").read_text())
    ename = frozen_args["uel_run"]["experiment_name"]
    csv_path = round_dir / f"{ename}__r{round_dir.name}.csv"
    if not csv_path.exists():
        return {"rows": [], "total": 0}

    import polars as pl
    try:
        df = pl.read_csv(csv_path, infer_schema_length=200)
    except Exception:
        return {"rows": [], "total": 0}

    present = [c for c in _RESULTS_COLUMNS if c in df.columns]
    if not present:
        return {"rows": [], "total": 0}
    df = df.select(present)

    casts = []
    for c in present:
        if c in _RESULTS_INT_COLS:
            if df[c].dtype != pl.Int64:
                casts.append(pl.col(c).cast(pl.Float64, strict=False).cast(pl.Int64, strict=False))
        else:
            if df[c].dtype not in (pl.Float32, pl.Float64):
                casts.append(pl.col(c).cast(pl.Float64, strict=False))
    if casts:
        df = df.with_columns(casts)

    total = df.height

    sort = sort if (sort and sort in present) else "id"
    descending = (direction == "desc")
    df = df.sort(sort, descending=descending, nulls_last=True)

    if offset > 0 or limit > 0:
        df = df.slice(max(offset, 0), max(limit, 0))

    rows = df.to_dicts()
    cleaned = []
    for row in rows:
        clean = {}
        for k, v in row.items():
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                clean[k] = None
            else:
                clean[k] = v
        cleaned.append(clean)
    return {"rows": cleaned, "total": total}


def _parse_iso(s: str | None):
    if not s:
        return None
    from datetime import datetime
    return datetime.fromisoformat(s.rstrip("Z"))


def _compute_round_stats(round_dir: Path) -> dict:
    from datetime import datetime
    status = read_status(round_dir)
    frozen_args = json.loads((round_dir / "args.json").read_text())
    target = int(frozen_args["uel_run"]["n_permutations"])
    ename = frozen_args["uel_run"]["experiment_name"]
    csv_path = round_dir / f"{ename}__r{round_dir.name}.csv"

    state = status.get("state")
    started = _parse_iso(status.get("started_at"))
    ended = _parse_iso(status.get("ended_at"))

    progress = {
        "current": 0, "target": target, "rate_per_sec": None,
        "elapsed_sec": None, "eta_sec": None,
        "state": state,
        "pid": status.get("pid"),
        "started_at": status.get("started_at"),
        "ended_at": status.get("ended_at"),
        "exit_code": status.get("exit_code"),
    }
    stats: list[dict] = []

    if not csv_path.exists():
        return {"progress": progress, "stats": stats}

    try:
        with open(csv_path, "r") as f:
            for current, _line in enumerate(f):
                pass
    except OSError:
        return {"progress": progress, "stats": stats}
    current = max(current, 0)  # subtract header (0-indexed last loop var)
    progress["current"] = current

    if started:
        end_t = ended or datetime.utcnow()
        elapsed = max((end_t - started).total_seconds(), 0.0)
        progress["elapsed_sec"] = elapsed
        if elapsed > 0 and current > 0:
            rate = current / elapsed
            progress["rate_per_sec"] = rate
            if current < target and rate > 0:
                progress["eta_sec"] = (target - current) / rate

    if current <= 0:
        return {"progress": progress, "stats": stats}

    try:
        import polars as pl
        df = pl.read_csv(csv_path, infer_schema_length=200)
    except Exception:
        return {"progress": progress, "stats": stats}

    desired = (
        "backtest_trades_count",
        "precision",
        "recall",
        "confusion_tp_mean_return_pct",
        "confusion_fp_mean_return_pct",
        "backtest_total_return_net_pct",
        "backtest_sharpe_per_bar",
        "backtest_max_drawdown_pct",
        "backtest_mean_kelly_pct",
    )
    for col in desired:
        if col not in df.columns:
            continue
        try:
            s = df[col].cast(pl.Float64, strict=False).drop_nulls().drop_nans()
        except Exception:
            continue
        if s.is_empty():
            continue
        try:
            stats.append({
                "name": col,
                "label": _display_label(col),
                "p5": s.quantile(0.05),
                "p25": s.quantile(0.25),
                "p50": s.quantile(0.50),
                "p75": s.quantile(0.75),
                "p95": s.quantile(0.95),
            })
        except Exception:
            continue

    return {"progress": progress, "stats": stats}
