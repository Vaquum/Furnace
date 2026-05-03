from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import polars as pl
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


def _load_build_meta() -> dict[str, str]:
    """Resolve package version and git SHA at import time so every template
    can stamp the running build."""
    import tomllib
    repo_root = PKG_DIR.parent
    pp = tomllib.loads((repo_root / "pyproject.toml").read_text())
    pkg_ver = pp["project"]["version"]
    sha_full = os.environ.get("GIT_SHA", "").strip()
    if not sha_full:
        try:
            sha_full = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            sha_full = ""
    sha_short = sha_full[:7] if sha_full else ""
    return {
        "package_version": pkg_ver,
        "git_full_sha": sha_full,
        "git_short_sha": sha_short,
        "repo_url": "https://github.com/Vaquum/experiment_runner",
    }


_BUILD_META = _load_build_meta()


def _ast_dict_to_python(d: _ast.Dict) -> dict | None:
    out: dict = {}
    for k, v in zip(d.keys, d.values):
        try:
            key = _ast.literal_eval(k) if k is not None else None
            val = _ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return None
        out[key] = val
    return out


def _extract_manifest_data_source(code: str, sfd_identifier: str) -> dict | None:
    """AST-walk: find `class <sfd_identifier>:`, find its `manifest` method,
    locate any `set_data_source(...)` call anywhere in its body, return
    {"method": <str|None>, "params": <dict|None>}. Handles both
    `return Manifest().set_data_source(...)` and the assignment form
    `manifest = Manifest().set_data_source(...)`. Returns None when no
    set_data_source(...) call is found."""
    if not code or not sfd_identifier:
        return None
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if not (isinstance(node, _ast.ClassDef) and node.name == sfd_identifier):
            continue
        for item in node.body:
            if not (isinstance(item, _ast.FunctionDef) and item.name == "manifest"):
                continue
            for sub in _ast.walk(item):
                if not (isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Attribute)):
                    continue
                if sub.func.attr != "set_data_source":
                    continue
                method: str | None = None
                params: dict | None = {}
                for kw in sub.keywords:
                    if kw.arg == "method":
                        try:
                            v = _ast.literal_eval(kw.value)
                        except (ValueError, SyntaxError):
                            v = None
                        if isinstance(v, str):
                            method = v
                    elif kw.arg == "params" and isinstance(kw.value, _ast.Dict):
                        params = _ast_dict_to_python(kw.value)
                return {"method": method, "params": params}
    return None


def _inspect_round_args(code: str, sfd_identifier: str) -> dict:
    """Return the schema for the experiment-edit controls. The SFD's
    `manifest()` is the source of truth: we AST-walk for the chained
    `set_data_source(method=..., params={...})` and introspect that
    method's signature for the per-parameter library defaults that drive
    the placeholder text in the UI.

    Fail-loud: a missing limen import or an un-introspectable method
    propagates so the inspect endpoint returns 500. The previous silent
    except masked real failures behind an empty-schema 200, which made
    the client save `{}` against a working SFD without any signal.

    No-manifest-no-schema: if the SFD class isn't in the code, doesn't
    define `manifest`, or doesn't call `set_data_source`, the response
    carries an empty signature. The client shows the empty-state
    message instead of synthesising controls for a default method
    unrelated to the current code (which would otherwise become real
    `args.json` overrides on the next save and mask the real manifest
    after the code is fixed)."""
    import inspect as _inspect_mod
    empty: dict = {
        "data_source": {
            "manifest_method": None,
            "manifest_params": {},
            "library_signature": [],
        },
    }
    extracted = _extract_manifest_data_source(code, sfd_identifier)
    if extracted is None:
        return empty
    manifest_method = extracted.get("method")
    manifest_params = extracted.get("params") or {}
    out: dict = {
        "data_source": {
            "manifest_method": manifest_method,
            "manifest_params": manifest_params,
            "library_signature": [],
        },
    }
    # Manifest matched but didn't name a method. Default to the legacy
    # `get_spot_klines` for introspection — UEL would do the same at run
    # time when method is unspecified.
    method_name = manifest_method or "get_spot_klines"
    import limen
    method = getattr(limen.HistoricalData, method_name, None)
    if method is None:
        # Manifest declared a method that doesn't exist on HistoricalData.
        # Fail loud at inspect time so the user can fix the manifest before
        # starting a round — otherwise run_round would crash on the same
        # getattr() with a less actionable traceback.
        raise HTTPException(
            400,
            f"manifest declares data_source.method={method_name!r} which is "
            f"not available on limen.HistoricalData",
        )
    sig = _inspect_mod.signature(method)
    sentinel = _inspect_mod.Parameter.empty
    for name, param in sig.parameters.items():
        if name == "self" or param.kind in (
            _inspect_mod.Parameter.VAR_POSITIONAL,
            _inspect_mod.Parameter.VAR_KEYWORD,
        ):
            continue
        has_default = param.default is not sentinel
        default = param.default if has_default else None
        if callable(default):
            default = None
        annotation = str(param.annotation) if param.annotation is not sentinel else "Any"
        out["data_source"]["library_signature"].append({
            "name": name,
            "default": default,
            "has_default": has_default,
            "annotation": annotation,
        })
    return out


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
    for k, v in _BUILD_META.items():
        templates.env.globals[k] = v
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
            request, "jobs.html", {"experiments": experiments},
        )

    @app.get("/_partials/jobs", response_class=HTMLResponse)
    def jobs_partial(request: Request):
        experiments = _all_experiments(root)
        return templates.TemplateResponse(
            request, "_jobs_rows.html", {"experiments": experiments},
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

    @app.post("/experiments/{experiment_id}/inspect")
    async def inspect_args(experiment_id: str, request: Request):
        _exp_dir(experiment_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        code = body.get("code", "")
        sfd_id = body.get("sfd_identifier", "")
        if not isinstance(code, str):
            raise HTTPException(400, "code must be a string")
        if not isinstance(sfd_id, str):
            raise HTTPException(400, "sfd_identifier must be a string")
        return JSONResponse(_inspect_round_args(code, sfd_id))

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(request: Request, experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        code, args, version = _read_working(exp_dir)
        rounds = _experiment_rounds(exp_dir)
        active = is_experiment_active(exp_dir)
        return templates.TemplateResponse(
            request,
            "experiment.html",
            {
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
            request,
            "_round_rows.html",
            {"experiment_id": experiment_id, "rounds": rounds},
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
            request,
            "round.html",
            {
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
            request,
            "results.html",
            {
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

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, experiment_name: str | None = None):
        scope = _parse_experiment_names(experiment_name)
        bounds = _compute_search_bounds(root, {}, scope)
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                "all_experiments": _list_experiment_names(root),
                "selected_experiments": scope if scope is not None else _list_experiment_names(root),
                "metrics": list(_SEARCH_METRICS),
                "bounds": bounds,
            },
        )

    @app.get("/search/bounds.json")
    def search_bounds(request: Request, experiment_name: str | None = None):
        scope = _parse_experiment_names(experiment_name)
        filters = _parse_filter_params(dict(request.query_params))
        return JSONResponse({
            "metrics": list(_SEARCH_METRICS),
            "bounds": _compute_search_bounds(root, filters, scope),
        })

    @app.get("/search/rounds.json")
    def search_rounds(request: Request, experiment_name: str | None = None):
        scope = _parse_experiment_names(experiment_name)
        filters = _parse_filter_params(dict(request.query_params))
        return JSONResponse({
            "rounds": _matching_rounds(root, filters, scope),
        })

    @app.get("/search/bundle")
    def search_bundle(request: Request, experiment_name: str | None = None):
        scope = _parse_experiment_names(experiment_name)
        filters = _parse_filter_params(dict(request.query_params))
        blob = _build_search_bundle(root, filters, scope)
        if scope and len(scope) == 1:
            name = f"search_{scope[0]}.zip"
        else:
            name = "search_bundle.zip"
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        return Response(blob, media_type="application/zip", headers=headers)

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

    df = pl.read_csv(csv_path, infer_schema_length=200)

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
    return datetime.fromisoformat(s.rstrip("Z"))


def _compute_round_stats(round_dir: Path) -> dict:
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

    with open(csv_path, "r") as f:
        n_lines = sum(1 for _ in f)
    current = max(n_lines - 1, 0)  # subtract one for the header line
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

    df = pl.read_csv(csv_path, infer_schema_length=200)

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
        s = df[col].cast(pl.Float64, strict=False).drop_nulls().drop_nans()
        if s.is_empty():
            continue
        stats.append({
            "name": col,
            "label": _display_label(col),
            "p5": s.quantile(0.05),
            "p25": s.quantile(0.25),
            "p50": s.quantile(0.50),
            "p75": s.quantile(0.75),
            "p95": s.quantile(0.95),
        })

    return {"progress": progress, "stats": stats}


# ---- Cross-round search ---------------------------------------------------

_SEARCH_METRICS: tuple[str, ...] = (
    "trade_per_bar",
    "return_per_bar",
    "kelly_per_bar",
    "sharpe_per_bar",
)

_SEARCH_REQUIRED_COLS: frozenset[str] = frozenset({
    "id",
    "backtest_bars_total",
    "backtest_bars_in_market_pct",
    "backtest_trades_count",
    "backtest_total_return_net_pct",
    "backtest_mean_kelly_pct",
    "backtest_sharpe_per_bar",
})


def _ensure_round_index(round_dir: Path) -> Path | None:
    state = read_status(round_dir).get("state")
    if state in ACTIVE_STATES:
        return None
    args_path = round_dir / "args.json"
    code_path = round_dir / "code.py"
    if not args_path.exists() or not code_path.exists():
        return None
    args = json.loads(args_path.read_text())
    ename = args["uel_run"]["experiment_name"]
    csv_path = round_dir / f"{ename}__r{round_dir.name}.csv"
    if not csv_path.exists():
        return None

    idx_path = round_dir / ".index.parquet"
    if idx_path.exists() and idx_path.stat().st_mtime >= csv_path.stat().st_mtime:
        return idx_path

    df = pl.read_csv(csv_path, infer_schema_length=200)
    if not _SEARCH_REQUIRED_COLS.issubset(df.columns):
        return None

    sha = hashlib.sha256(code_path.read_bytes()).hexdigest()
    bt = pl.col("backtest_bars_total").cast(pl.Float64)

    out = (
        df.lazy()
        .with_columns(bt.alias("_bt"))
        .filter(pl.col("_bt") > 0)
        .select([
            pl.col("id").cast(pl.Int64).alias("permutation_id"),
            pl.lit(round_dir.parent.parent.name).alias("experiment_id"),
            pl.lit(round_dir.name).alias("round_id"),
            pl.lit(sha).alias("code_sha256"),
            (pl.col("backtest_trades_count").cast(pl.Float64) / pl.col("_bt")).alias("trade_per_bar"),
            (pl.col("backtest_total_return_net_pct").cast(pl.Float64) / pl.col("_bt")).alias("return_per_bar"),
            (pl.col("backtest_mean_kelly_pct").cast(pl.Float64, strict=False) / pl.col("_bt")).alias("kelly_per_bar"),
            pl.col("backtest_sharpe_per_bar").cast(pl.Float64, strict=False).alias("sharpe_per_bar"),
        ])
        .collect()
    )
    tmp = idx_path.with_suffix(".parquet.tmp")
    out.write_parquet(tmp, compression="snappy")
    os.replace(tmp, idx_path)
    return idx_path


def _list_experiment_names(root: Path) -> list[str]:
    base = root / "experiments"
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def _ensure_indices(root: Path, experiment_names: list[str] | None) -> None:
    base = root / "experiments"
    if not base.exists():
        return
    if experiment_names is None:
        edirs = [d for d in base.iterdir() if d.is_dir()]
    else:
        edirs = [base / n for n in experiment_names if (base / n).is_dir()]
    for ed in edirs:
        rd = ed / "rounds"
        if not rd.exists():
            continue
        for round_dir in rd.iterdir():
            if round_dir.is_dir():
                _ensure_round_index(round_dir)


def _index_paths(root: Path, experiment_names: list[str] | None) -> list[Path]:
    base = root / "experiments"
    if not base.exists():
        return []
    pat = "rounds/*/.index.parquet"
    if experiment_names is None:
        return sorted(base.glob(f"*/{pat}"))
    paths: list[Path] = []
    for name in experiment_names:
        ed = base / name
        if ed.is_dir():
            paths.extend(ed.glob(pat))
    return sorted(paths)


_FilterMap = dict[str, tuple[float | None, float | None]]


def _apply_search_filters(lf: pl.LazyFrame, filters: _FilterMap) -> pl.LazyFrame:
    for metric, (lo, hi) in filters.items():
        if lo is None and hi is None:
            continue
        lf = lf.filter(pl.col(metric).is_not_nan())
        if lo is not None:
            lf = lf.filter(pl.col(metric) >= lo)
        if hi is not None:
            lf = lf.filter(pl.col(metric) <= hi)
    return lf


def _compute_search_bounds(
    root: Path,
    filters: _FilterMap,
    experiment_names: list[str] | None,
) -> dict[str, dict[str, float | None]]:
    """Per-metric leave-one-out bounds: each slider's range is min/max of its
    column with the other sliders' filters applied (its own filter excluded)."""
    _ensure_indices(root, experiment_names)
    paths = _index_paths(root, experiment_names)
    if not paths:
        return {m: {"min": None, "max": None} for m in _SEARCH_METRICS}
    lf = pl.scan_parquet([str(p) for p in paths])
    out: dict[str, dict[str, float | None]] = {}
    for m in _SEARCH_METRICS:
        loo = {k: v for k, v in filters.items() if k != m}
        df = (
            _apply_search_filters(lf, loo)
            .select([pl.col(m).min().alias("lo"), pl.col(m).max().alias("hi")])
            .collect()
        )
        if df.height == 0:
            out[m] = {"min": None, "max": None}
        else:
            row = df.row(0, named=True)
            out[m] = {"min": row["lo"], "max": row["hi"]}
    return out


def _matching_rounds(root: Path, filters: _FilterMap, experiment_names: list[str] | None) -> list[dict]:
    _ensure_indices(root, experiment_names)
    paths = _index_paths(root, experiment_names)
    if not paths:
        return []
    lf = pl.scan_parquet([str(p) for p in paths])
    df = (
        _apply_search_filters(lf, filters)
        .group_by(["experiment_id", "round_id"])
        .agg(pl.len().alias("match_count"))
        .sort(["match_count", "experiment_id", "round_id"], descending=[True, False, True])
        .collect()
    )
    return df.to_dicts()


def _build_search_bundle(root: Path, filters: _FilterMap, experiment_names: list[str] | None) -> bytes:
    _ensure_indices(root, experiment_names)
    paths = _index_paths(root, experiment_names)
    matches = pl.DataFrame()
    if paths:
        lf = pl.scan_parquet([str(p) for p in paths])
        matches = (
            _apply_search_filters(lf, filters)
            .select(["experiment_id", "round_id", "code_sha256", "permutation_id"])
            .collect()
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if matches.height > 0:
            groups: dict[tuple[str, str], dict[str, list[int]]] = {}
            for row in matches.iter_rows(named=True):
                key = (row["experiment_id"], row["code_sha256"])
                groups.setdefault(key, {}).setdefault(row["round_id"], []).append(row["permutation_id"])

            for (exp_id, sha), by_round in groups.items():
                sorted_round_ids = sorted(by_round.keys())
                base = f"{exp_id}__r" + "-".join(sorted_round_ids)

                first_dir = root / "experiments" / exp_id / "rounds" / sorted_round_ids[0]
                z.writestr(f"{base}.py", (first_dir / "code.py").read_text())

                rounds_meta = []
                for rid in sorted_round_ids:
                    rdir = root / "experiments" / exp_id / "rounds" / rid
                    rounds_meta.append({
                        "round_id": rid,
                        "args": json.loads((rdir / "args.json").read_text()),
                    })
                meta = {"experiment_id": exp_id, "code_sha256": sha, "rounds": rounds_meta}
                z.writestr(f"{base}.json", json.dumps(meta, indent=2))

                csv_parts: list[pl.DataFrame] = []
                for rid in sorted_round_ids:
                    rdir = root / "experiments" / exp_id / "rounds" / rid
                    args = json.loads((rdir / "args.json").read_text())
                    ename = args["uel_run"]["experiment_name"]
                    csv_path = rdir / f"{ename}__r{rid}.csv"
                    if not csv_path.exists():
                        continue
                    df = pl.read_csv(csv_path, infer_schema_length=200)
                    df = df.filter(pl.col("id").is_in(by_round[rid]))
                    df = df.select([
                        pl.lit(exp_id).alias("experiment_id"),
                        pl.lit(rid).alias("round_id"),
                        *[pl.col(c) for c in df.columns],
                    ])
                    csv_parts.append(df)
                if csv_parts:
                    combined = pl.concat(csv_parts, how="vertical_relaxed")
                    z.writestr(f"{base}.csv", combined.write_csv())

    return buf.getvalue()


def _parse_experiment_names(value: str | None) -> list[str] | None:
    # None  -> missing param: scope is "all experiments".
    # ""    -> explicit empty selection: scope is "no experiments".
    # "A,B" -> explicit list.
    if value is None:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def _parse_filter_params(qp: dict[str, str | None]) -> _FilterMap:
    out: _FilterMap = {}
    for m in _SEARCH_METRICS:
        lo_s = qp.get(f"{m}_min")
        hi_s = qp.get(f"{m}_max")
        lo = float(lo_s) if lo_s not in (None, "") else None
        hi = float(hi_s) if hi_s not in (None, "") else None
        out[m] = (lo, hi)
    return out
