from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import zipfile
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import cast

import polars as pl
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cohorts, inference
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


PKG_DIR = Path(__file__).resolve().parent
LAUNCHER_PATH = PKG_DIR / "run_round.py"
ASSET_VERSION = str(int(time.time()))


def _load_build_meta() -> dict[str, str]:
    """Resolve package version and git SHA without requiring a repo checkout."""
    repo_root = PKG_DIR.parent
    try:
        pkg_ver = importlib_metadata.version("furnace")
    except importlib_metadata.PackageNotFoundError:
        try:
            pp = tomllib.loads((repo_root / "pyproject.toml").read_text())
            pkg_ver = pp["project"]["version"]
        except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError):
            pkg_ver = "0.0.0"
    sha_full = os.environ.get("GIT_SHA", "").strip()
    if not sha_full:
        try:
            sha_full = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            sha_full = ""
    sha_short = sha_full[:7] if sha_full else ""
    return {
        "package_version": pkg_ver,
        "git_full_sha": sha_full,
        "git_short_sha": sha_short,
        "repo_url": "https://github.com/Vaquum/furnace",
    }


_BUILD_META = _load_build_meta()


# A new experiment is seeded with this manifest. It is a valid Limen YAML
# manifest (the same shape run_round.py parses → validates → compiles); the
# `__EXPERIMENT_NAME__` sentinel is substituted with the experiment id at
# creation time so the metadata name matches the experiment directory.
STARTER_MANIFEST = """schema_version: "1.0"

metadata:
  name: __EXPERIMENT_NAME__
  limen_version: "3.8.0"
  mode: production
  description: Furnace experiment

sfd:
  manifest:
    type: ml
    data_source:
      method: limen.data.HistoricalData.get_spot_klines
      params:
        kline_size: 900
        start_date_limit: "2026-02-01"
    split_config:
      train: 8
      val: 1
      test: 2
    indicators:
      - func: limen.indicators.roc
        params: {period: "{roc_period}"}
      - func: limen.indicators.ppo
      - func: limen.indicators.wilder_rsi
        params: {period: "{rsi_period}"}
      - func: limen.indicators.atr
        params: {period: "{atr_period}"}
      - func: limen.indicators.ema
        params: {period: "{ema_period}"}
      - func: limen.indicators.sma
    features:
      - func: limen.features.log_returns.log_returns
      - func: limen.features.vwap
      - func: limen.features.range_pct
      - func: limen.features.body_to_range
      - func: limen.features.close_position
    target:
      name: next_bar_up
      class: limen.targets.NextBarUpTarget
    scaler:
      from_params: scaler_type
    reference_architecture: limen.sfd.reference_architecture.logreg_binary

  params:
    roc_period: [96, 144]
    atr_period: [5, 7, 14]
    rsi_period: [14, 18, 21]
    ema_period: [12, 26, 96]
    scaler_type: [robust]
    solver: [lbfgs]
    penalty: [l2]
    C: [0.05, 0.07, 0.1]
    class_weight: [0.40, 0.50, 0.60, 0.75, 0.90]
    max_iter: [60, 90, 120]

uel:
  n_permutations: 50
  search_strategy:
    type: random
  prep_each_round: true
  output_format: csv
  # results land in <round>/limen_output/ — Furnace reads them there
  output_path: limen_output
"""


def _limen_cli() -> Path:
    """The `limen` console script, installed next to the running
    interpreter. Furnace reaches Limen only through this CLI —
    never in-process — so a manifest the editor accepts is exactly one
    `limen run` would accept at round-start."""
    cli = Path(sys.executable).parent / "limen"
    if not cli.exists():
        raise HTTPException(500, f"`limen` CLI not found at {cli}")
    return cli


# `limen validate` prints one error per line, indented two spaces:
#   PARSE ERROR (line N): message
#   ERROR  [config.path] (line N): message
# Both the `  [path]` and ` (line N)` segments are optional. The path may
# itself contain `[N]` index notation, so the path capture is non-greedy
# and anchored on the ` (line ` / `: ` that always follows the closing `]`.
_CLI_ERROR_RE = re.compile(
    r"^ {2}(?:PARSE ERROR|ERROR)"
    r"(?:  \[(?P<path>.+?)\](?= \(line |: ))?"
    r"(?: \(line (?P<line>\d+)\))?"
    r": (?P<message>.*)$"
)


def _manifest_errors(manifest_text: str) -> list[dict]:
    """Validate a manifest YAML string by running the `limen validate`
    CLI — the same binary, the same parse → validate path that `limen run`
    enforces at round-start — and return a flat list of
    `{"line", "path", "message"}` dicts, empty when the CLI exits 0.
    Furnace never validates in-process, so the editor's
    pre-flight check cannot drift from what a real round does."""
    limen = _limen_cli()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yml", delete=False, encoding="utf-8",
    ) as tmp:
        tmp.write(manifest_text)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [str(limen), "validate", str(tmp_path)],
            capture_output=True, text=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    if result.returncode == 0:
        return []
    out: list[dict] = []
    for line in result.stdout.splitlines():
        m = _CLI_ERROR_RE.match(line)
        if m:
            out.append({
                "line": int(m["line"]) if m["line"] else None,
                "path": m["path"] or "",
                "message": m["message"],
            })
    if not out:
        raise HTTPException(
            500,
            "`limen validate` reported the manifest invalid but produced "
            f"no parseable error lines:\n{result.stdout}",
        )
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


SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ROUND_ID_RE = re.compile(r"^\d+$")


def _format_kline_size(seconds: int | None) -> str:
    """Render a kline-size in seconds as the largest whole-unit string the
    interval divides into evenly — '4h' for 14400, '15m' for 900, '1d' for
    86400, fall back to '<n>s' for anything irregular. Returns '—' for
    None / 0 so the template can pipe `period.kline_size_seconds` through
    without an outer if-guard."""
    if not seconds:
        return '—'
    for unit_seconds, suffix in ((86400, 'd'), (3600, 'h'), (60, 'm')):
        if seconds % unit_seconds == 0:
            return f'{seconds // unit_seconds}{suffix}'
    return f'{seconds}s'


def _format_metric(value: float | None) -> str:
    """Render a ledger metric (bps) with magnitude-adaptive precision —
    fewer decimals as the number grows. '—' for None (pre-ledger rounds)."""
    if value is None:
        return '—'
    magnitude = abs(value)
    if magnitude >= 1000:
        return f'{value:.0f}'
    if magnitude >= 100:
        return f'{value:.1f}'
    if magnitude >= 10:
        return f'{value:.2f}'
    return f'{value:.3f}'


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


def _parse_cohort_permutation_ids(value: str) -> list[str]:
    # permutation_ids are Limen round_ids — SHA-256 hex strings, not ints.
    pieces = [s.strip() for s in value.split(",") if s.strip()]
    if not pieces:
        raise HTTPException(
            422, "permutation_ids must list at least one round_id",
        )
    if len(set(pieces)) != len(pieces):
        # Mirror Limen's Cohort uniqueness contract (and cohorts.py): a
        # duplicate would double-count a member and break cohort_id identity.
        raise HTTPException(422, "permutation_ids must be unique")
    return pieces


def _safe_child_dir(base: Path, child: str, pattern: re.Pattern[str], missing: str) -> Path:
    if not pattern.fullmatch(child):
        raise HTTPException(404, missing)
    base_resolved = base.resolve()
    child_path = (base / child).resolve()
    if base_resolved != child_path and base_resolved not in child_path.parents:
        raise HTTPException(404, missing)
    if not child_path.exists():
        raise HTTPException(404, missing)
    return child_path


def _read_manifest(experiment_dir: Path) -> tuple[str, int]:
    with version_flock(experiment_dir):
        manifest_path = experiment_dir / "manifest.yml"
        manifest_text = manifest_path.read_text() if manifest_path.exists() else ""
        version = read_version(experiment_dir)
    return manifest_text, version


def _all_experiments(root: Path, with_analytics: bool = False) -> list[dict]:
    experiments = root / "experiments"
    if not experiments.exists():
        return []
    out = []
    for exp_dir in sorted(experiments.iterdir()):
        rounds = _experiment_rounds(exp_dir)
        if with_analytics:
            for r in rounds:
                r.update(_round_analytics(exp_dir / "rounds" / r["round_id"]))
        latest = rounds[0] if rounds else None
        latest_at = ""
        if latest:
            latest_at = latest.get("ended_at") or latest.get("started_at") or ""
        entry = {
            "id": exp_dir.name,
            "rounds": rounds,
            "round_count": len(rounds),
            "latest_state": latest.get("state") if latest else "",
            "latest_at": latest_at,
        }
        if with_analytics:
            entry.update(_experiment_analytics(rounds))
        out.append(entry)
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


# Jobs-table analytics: trade-pnl and drawdown-depth, each at p5/p50/p95.
# Source columns are the per-permutation ledger percentiles; a round's value
# is their cross-permutation median (the p50 the round viewer reports) and an
# experiment's value is the mean of that across its rounds.
_ANALYTIC_COLUMNS = {
    "tp_p5": "backtest_trade_pnl_net_bps_p5",
    "tp_p50": "backtest_trade_pnl_net_bps_p50",
    "tp_p95": "backtest_trade_pnl_net_bps_p95",
    "dd_p5": "backtest_drawdown_depth_bps_p5",
    "dd_p50": "backtest_drawdown_depth_bps_p50",
    "dd_p95": "backtest_drawdown_depth_bps_p95",
}
_ANALYTIC_KEYS = tuple(_ANALYTIC_COLUMNS)


def _round_csv_path(round_dir: Path) -> Path:
    """The round's results CSV. `limen run` writes `results.csv` into its
    own output directory, which run_round.py relocates whole to
    <round_dir>/limen_output. Every reader resolves the path through here,
    so there is one source of truth — limen's authoritative file, never a
    renamed copy."""
    return round_dir / "limen_output" / "results.csv"


def _round_analytics(round_dir: Path) -> dict[str, float | None]:
    """Per-round trade-pnl / drawdown-depth at p5/p50/p95 — each the
    cross-permutation median of that ledger column, mirroring the round
    viewer's p50. None when the column is absent (pre-ledger rounds) or the
    round has no results CSV yet."""
    out: dict[str, float | None] = {key: None for key in _ANALYTIC_KEYS}
    csv_path = _round_csv_path(round_dir)
    if not csv_path.exists():
        return out
    df = pl.read_csv(csv_path, infer_schema_length=200)
    for key, col in _ANALYTIC_COLUMNS.items():
        if col not in df.columns:
            continue
        s = df[col].cast(pl.Float64, strict=False).drop_nulls().drop_nans()
        if not s.is_empty():
            out[key] = s.quantile(0.50)
    return out


def _experiment_analytics(rounds: list[dict]) -> dict[str, float | None]:
    """Experiment-level analytics — the mean of each round's value across the
    rounds that have one. None when no round carries the metric."""
    out: dict[str, float | None] = {}
    for key in _ANALYTIC_KEYS:
        vals: list[float] = []
        for r in rounds:
            v = r.get(key)
            if isinstance(v, (int, float)):
                vals.append(float(v))
        out[key] = sum(vals) / len(vals) if vals else None
    return out


# Worst-case wait before a terminated round's process group is force-killed.
# limen's cooperative shutdown — finish the in-flight permutation, write a
# resumable checkpoint, finalize its artefacts — completes well within this;
# the SIGKILL backstop fires only for a limen that never exits, so a stopped
# round can never hang forever on `terminating`.
_TERMINATE_GRACE_SECONDS = 300


def _terminate_backstop(pgid: int) -> None:
    """Force-kill a terminated round's process group if it has not exited on
    its own within the grace window. Run in a daemon thread by the terminate
    endpoint: limen's graceful shutdown is the normal path and ends the group
    well before the grace expires; this SIGKILL is the guarantee that a
    stopped round always reaches a terminal state. The round's watcher thread
    does the status finalize — once the group is empty it flips the round
    `terminating -> terminated` — whichever way the group ended."""
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    try:
        while time.monotonic() < deadline:
            os.killpg(pgid, 0)
            time.sleep(1.0)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def create_app(root: Path) -> FastAPI:
    # Auth credentials must be in the environment before the app builds.
    # Refuse to start if any are missing — there is no "no auth" mode.
    app_username = os.environ.get("APP_USERNAME", "")
    app_password = os.environ.get("APP_PASSWORD", "")
    api_secret = os.environ.get("API_SECRET", "")
    if not (app_username and app_password and api_secret):
        raise RuntimeError(
            "Furnace requires APP_USERNAME, APP_PASSWORD, and "
            "API_SECRET environment variables; refusing to start"
        )

    templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))
    templates.env.globals["asset_version"] = ASSET_VERSION
    # STARTER_MANIFEST is a constant; making it a global lets the creation
    # modal — now in base.html, reachable from every view — render without
    # each route threading it through its own context.
    templates.env.globals["starter_manifest"] = STARTER_MANIFEST
    templates.env.filters["kline_size_display"] = _format_kline_size
    templates.env.filters["metric_fmt"] = _format_metric
    # Short metric label — the results-table / round-stats display form,
    # so the search filters read the same as the rest of the UI.
    templates.env.filters["display_label"] = _display_label
    for k, v in _BUILD_META.items():
        templates.env.globals[k] = v
    sm = StateMachine(root)
    reconcile_at_startup(sm)

    app = FastAPI(title="Furnace")
    app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        # Exempt the healthcheck path, the static-file mount, and the
        # already-public HEAD / smoke-check (per slice S02). Every other
        # request must carry either HTTP Basic with APP_USERNAME/APP_PASSWORD
        # or `Authorization: Bearer <API_SECRET>`.
        path = request.url.path
        if path == "/health" or path == "/static" or path.startswith("/static/"):
            return await call_next(request)
        if request.method == "HEAD" and path == "/":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        # RFC 7235 says auth scheme names are case-insensitive; split on the
        # first space and lowercase the scheme so e.g. `bearer <token>` is
        # accepted just like `Bearer <token>`.
        scheme, _, credentials = auth.partition(" ")
        scheme_lc = scheme.lower()
        if scheme_lc == "basic":
            try:
                decoded = base64.b64decode(credentials, validate=True).decode(
                    "utf-8", errors="strict",
                )
            except (binascii.Error, ValueError, UnicodeDecodeError):
                decoded = ""
            user, sep, password = decoded.partition(":")
            if (
                sep
                and secrets.compare_digest(user, app_username)
                and secrets.compare_digest(password, app_password)
            ):
                return await call_next(request)
        elif scheme_lc == "bearer":
            if secrets.compare_digest(credentials, api_secret):
                return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "authentication required"},
            headers={"WWW-Authenticate": 'Basic realm="Furnace"'},
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.on_event("shutdown")
    def _on_shutdown() -> None:
        shutdown_handler(sm)

    def _exp_dir(experiment_id: str) -> Path:
        return _safe_child_dir(
            root / "experiments", experiment_id, EXPERIMENT_ID_RE, "experiment not found",
        )

    def _round_dir(experiment_id: str, round_id: str) -> Path:
        return _safe_child_dir(_exp_dir(experiment_id) / "rounds", round_id, ROUND_ID_RE, "round not found")

    @app.get("/", response_class=HTMLResponse)
    def jobs(request: Request):
        experiments = _all_experiments(root, with_analytics=True)
        return templates.TemplateResponse(
            request, "jobs.html",
            {"experiments": experiments, "starter_manifest": STARTER_MANIFEST},
        )

    @app.get("/_partials/jobs", response_class=HTMLResponse)
    def jobs_partial(request: Request):
        experiments = _all_experiments(root, with_analytics=True)
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
        is_json = request.headers.get("content-type", "").startswith("application/json")
        if is_json:
            body = await request.json()
            if not isinstance(body, dict):
                raise HTTPException(400, "request body must be a JSON object")
            raw_name = body.get("name", "")
            if not isinstance(raw_name, str):
                raise HTTPException(400, "name must be a string")
            name = raw_name.strip()
        else:
            form = await request.form()
            name = form.get("name", "").strip()
        if not name:
            raise HTTPException(400, "name is required")
        experiments_dir = root / "experiments"
        experiments_dir.mkdir(parents=True, exist_ok=True)
        eid = _allocate_experiment_id(experiments_dir, name)
        exp_dir = experiments_dir / eid
        (exp_dir / "rounds").mkdir(parents=True, exist_ok=True)
        (exp_dir / "manifest.yml").write_text(
            STARTER_MANIFEST.replace("__EXPERIMENT_NAME__", eid)
        )
        write_version_atomic(exp_dir, 0)
        if is_json:
            return JSONResponse({"id": eid})
        return RedirectResponse(f"/experiments/{eid}", status_code=303)

    @app.get("/experiments/{experiment_id}/working.json")
    def working_json(experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        manifest_text, version = _read_manifest(exp_dir)
        return {"manifest": manifest_text, "version": version}

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(request: Request, experiment_id: str):
        exp_dir = _exp_dir(experiment_id)
        manifest_text, _version = _read_manifest(exp_dir)
        return templates.TemplateResponse(
            request,
            "experiment.html",
            {
                "experiment_id": experiment_id,
                "experiments": _all_experiments(root),
                "rounds": _experiment_rounds(exp_dir),
                "frozen_manifest": manifest_text,
                "stats": _experiment_stats(exp_dir),
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

    @app.put("/experiments/{experiment_id}/manifest")
    async def save_manifest(experiment_id: str, request: Request):
        exp_dir = _exp_dir(experiment_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        if "manifest" not in body:
            raise HTTPException(400, "request must include 'manifest'")
        if_match = body.get("if_match")
        manifest = body["manifest"]
        if not isinstance(manifest, str):
            raise HTTPException(400, "manifest must be a string")
        # Save refuses an invalid manifest so the user catches parse /
        # validation errors at the moment they hit Save, not at round-start
        # (which fails after they already moved on). The 400 detail is
        # structured so the editor can list each error with its line / path.
        errors = _manifest_errors(manifest)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"kind": "manifest_invalid", "errors": errors},
            )
        with sm.exp_lock(experiment_id):
            with version_flock(exp_dir):
                current = read_version(exp_dir)
                if if_match != current:
                    return JSONResponse({"current_version": current}, status_code=409)
                tmp = exp_dir / "manifest.yml.tmp"
                tmp.write_text(manifest)
                os.replace(tmp, exp_dir / "manifest.yml")
                new_version = current + 1
                write_version_atomic(exp_dir, new_version)
        return {"version": new_version}

    @app.post("/experiments/{experiment_id}/rounds")
    async def start_round(experiment_id: str, request: Request):
        exp_dir = _exp_dir(experiment_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        if_match = body.get("if_match")

        with sm.exp_lock(experiment_id):
            with version_flock(exp_dir):
                current_version = read_version(exp_dir)
                if if_match != current_version:
                    return JSONResponse({"current_version": current_version}, status_code=409)
                if is_experiment_active(exp_dir):
                    return JSONResponse(
                        {"error": "another round is active"}, status_code=409,
                    )
                # Source of truth: the manifest is saved via PUT /manifest.
                # We snapshot it under the same flock that gates the if_match
                # check so the round is consistent with what the user saw
                # when they clicked Start.
                manifest_path = exp_dir / "manifest.yml"
                manifest = manifest_path.read_text() if manifest_path.exists() else ""
                if not manifest.strip():
                    raise HTTPException(
                        400,
                        "manifest is empty — open the manifest editor and fill it in before starting a round",
                    )
                errors = _manifest_errors(manifest)
                if errors:
                    raise HTTPException(
                        status_code=400,
                        detail={"kind": "manifest_invalid", "errors": errors},
                    )

            rounds_dir = exp_dir / "rounds"
            round_id = _allocate_round_id(rounds_dir)
            round_dir = rounds_dir / round_id
            round_dir.mkdir(parents=True, exist_ok=False)
            (round_dir / "manifest.yml").write_text(manifest)
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
                with open(stdout_path, "w") as stdout_f, open(stderr_path, "w") as stderr_f:
                    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                    env["PYTHONPATH"] = str(PKG_DIR.parent) + os.pathsep + env.get("PYTHONPATH", "")
                    popen = subprocess.Popen(
                        [
                            sys.executable, "-u",
                            "-m", "furnace.run_round",
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

        # /rounds is version-passive — it never bumps the working version,
        # only freezes a snapshot into a new round dir. The response uses
        # `current_version` (matching the 409 path's key) rather than a
        # `version` field that suggests advancement.
        return {"current_version": current_version, "round_id": round_id}

    @app.get("/experiments/{experiment_id}/rounds/{round_id}", response_class=HTMLResponse)
    def round_page(request: Request, experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        st = read_status(round_dir)
        # A round created before the manifest migration has no manifest.yml
        # — it predates the YAML format. The round page still renders — logs,
        # status, and results stay viewable — with an empty manifest panel
        # rather than a 500.
        manifest_path = round_dir / "manifest.yml"
        frozen_manifest = manifest_path.read_text() if manifest_path.exists() else ""
        # period.json is written by run_round.py after `limen run`
        # completes — test-period bounds and kline_size, read straight from
        # limen's own artefacts (round_data.jsonl, metadata.json). May be
        # absent on a crashed run that never reached the persist step.
        period_path = round_dir / "period.json"
        period = json.loads(period_path.read_text()) if period_path.exists() else None
        diff_text = ""
        prev_id = f"{int(round_id) - 1:04d}"
        prev_dir = round_dir.parent / prev_id
        if (prev_dir / "manifest.yml").exists():
            prev_manifest = (prev_dir / "manifest.yml").read_text()
            diff_text = "".join(difflib.unified_diff(
                prev_manifest.splitlines(keepends=True),
                frozen_manifest.splitlines(keepends=True),
                fromfile=f"r{prev_id}/manifest.yml",
                tofile=f"r{round_id}/manifest.yml",
                n=3,
            ))
        response = templates.TemplateResponse(
            request,
            "round.html",
            {
                "experiment_id": experiment_id,
                "round_id": round_id,
                "rounds": _experiment_rounds(_exp_dir(experiment_id)),
                "experiments": _all_experiments(root),
                "status": st,
                "frozen_manifest": frozen_manifest,
                "diff_text": diff_text,
                "period": period,
                "active": st.get("state") in ACTIVE_STATES,
            },
        )
        # State on this page moves under the user (round transitions, diff
        # appears, period.json gets written when the run finishes) — never
        # serve a cached copy. Without this, a tab opened during `running`
        # keeps showing the running-state HTML until a hard refresh.
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete("/experiments/{experiment_id}/rounds/{round_id}")
    def delete_round(experiment_id: str, round_id: str):
        # Refuse if the round is still active. The user must terminate first
        # so the watcher and pid table aren't left referencing a removed dir.
        round_dir = _round_dir(experiment_id, round_id)
        st = read_status(round_dir)
        state = st.get("state")
        if state in ACTIVE_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"round {round_id} is {state}; terminate it first",
            )
        import shutil
        shutil.rmtree(round_dir)
        return {"deleted": True}

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
        # Stop the round by signalling run_round.py — the supervisor of the
        # `limen run` child. run_round.py forwards SIGTERM to limen, which
        # runs limen's own cooperative shutdown: finish the in-flight
        # permutation, write a resumable checkpoint, finalize its artefacts,
        # then exit. run_round.py reaps limen and exits; the round's watcher
        # thread then flips it `terminating -> terminated`. This returns at
        # once — limen's graceful shutdown can take a while and the round
        # page polls for the terminal state — while a daemon backstop
        # SIGKILLs the group if limen never exits, so the round can never
        # hang forever on `terminating`.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        threading.Thread(
            target=_terminate_backstop, args=(pid,), daemon=True,
        ).start()
        return {"ok": True}

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
        csv_path = _round_csv_path(round_dir)
        headers = {"Content-Disposition": f'attachment; filename="{experiment_id}__r{round_id}.csv"'}
        if not csv_path.exists():
            return PlainTextResponse("", media_type="text/csv", headers=headers)
        return PlainTextResponse(csv_path.read_text(), media_type="text/csv", headers=headers)

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/bundle")
    def round_bundle(experiment_id: str, round_id: str):
        round_dir = _round_dir(experiment_id, round_id)
        base = f"{experiment_id}__r{round_id}"
        # The bundle is `limen run`'s output directory verbatim — results.csv,
        # metadata.json, round_data.jsonl, and the manifest copy — plus the
        # round-level frozen manifest so the bundle is self-describing.
        limen_output = round_dir / "limen_output"
        manifest_path = round_dir / "manifest.yml"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # Round-level frozen manifest — the submission the operator
            # canonicalised — pinned at the bundle root.
            if manifest_path.is_file():
                z.writestr(f"{base}/manifest.yml", manifest_path.read_bytes())
            # Every file under limen_output/, walked recursively — whether
            # the artefacts sit at the top level (post-v0.17.1 layout) or
            # in a timestamped subdirectory (legacy layout). The previous
            # iterdir() was one-level and silently dropped the subdir
            # case, producing a near-empty zip on legacy rounds. Relative
            # paths inside limen_output/ are preserved in the bundle.
            if limen_output.is_dir():
                for p in sorted(limen_output.rglob("*")):
                    if p.is_file():
                        rel = p.relative_to(limen_output)
                        z.writestr(f"{base}/limen_output/{rel.as_posix()}", p.read_bytes())

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
                "experiments": _all_experiments(root),
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

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/cohort/input")
    def cohort_input(
        experiment_id: str,
        round_id: str,
        at: str,
    ):
        # The raw klines window the cohort would feed its members to predict
        # the bar at `at` — window bounds, bar count, columns, and the final
        # bar. Pulled from the manifest's data_source; the window is
        # pid-independent (every member sees the same klines), so there is no
        # training and no permutation_ids here.
        limen_output = _cohort_limen_output(experiment_id, round_id)
        try:
            body = inference.klines_window_at(limen_output, at)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        return JSONResponse(body)

    @app.get("/experiments/{experiment_id}/rounds/{round_id}/cohort/predict")
    def cohort_predict(
        experiment_id: str,
        round_id: str,
        permutation_ids: str,
        at: str,
    ):
        # Trains the selected permutations into sensors via Limen's Trainer,
        # binds a Cohort over them, pulls a klines window ending at `at` from
        # the manifest's data_source, and returns the cohort's BarPrediction
        # for the bar at `at`. Sensors are rebuilt on every call — no cache.
        limen_output = _cohort_limen_output(experiment_id, round_id)
        pids = _parse_cohort_permutation_ids(permutation_ids)
        try:
            body = inference.predict_at(limen_output, pids, at)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        return JSONResponse(body)

    def _cohort_limen_output(experiment_id: str, round_id: str) -> Path:
        round_dir = _round_dir(experiment_id, round_id)
        limen_output = round_dir / "limen_output"
        if not limen_output.is_dir():
            raise HTTPException(404, "round has no limen_output directory")
        return limen_output

    # FastAPI holds the references through @app.get; this tuple keeps
    # pyright's reportUnusedFunction from flagging the two handlers as
    # dead code, since the decorator return is not a use-site pyright
    # tracks. Touching only the new handlers keeps the rest of the
    # endpoint surface untouched.
    _cohort_handlers = (cohort_input, cohort_predict)
    del _cohort_handlers

    @app.get("/graduation", response_class=HTMLResponse)
    def graduation_page(request: Request):
        # The graduation view: pick an experiment + round from search, select
        # the winning permutations, name a cohort, and POST it to /cohorts.
        # One manifest per cohort (Limen's grouping rule).
        return templates.TemplateResponse(
            request,
            "graduation.html",
            {
                "experiments": _all_experiments(root),
                "cohorts": cohorts.list_cohort_definitions(root),
            },
        )

    @app.get("/cohorts")
    def cohorts_list():
        return JSONResponse({"cohorts": cohorts.list_cohort_definitions(root)})

    @app.post("/cohorts")
    async def create_cohort(request: Request):
        # Graduate selected permutations of one round into a named cohort.
        # The manifest_id (the cohort grouping key) is read from the round's
        # own metadata.json — never trusted from the client — so a cohort's
        # identity is anchored to the experiment artefacts.
        raw = await request.json()
        if not isinstance(raw, dict):
            raise HTTPException(400, "request body must be a JSON object")
        body = cast(dict[str, object], raw)
        experiment_id = body.get("experiment_id")
        round_id = body.get("round_id")
        name = body.get("name")
        perms = body.get("permutation_ids")
        if not isinstance(experiment_id, str) or not isinstance(round_id, str):
            raise HTTPException(400, "experiment_id and round_id must be strings")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name is required")
        if not isinstance(perms, list) or not perms:
            raise HTTPException(400, "permutation_ids must be a non-empty list")
        # Validate element types rather than coercing with str(): a raw `[null]`
        # / `[{}]` would otherwise become the literal ids 'None' / '{...}', making
        # cohort identity depend on Python stringification instead of the client
        # contract. Permutation ids are str or int (bool is an int subclass — reject).
        permutation_ids: list[str] = []
        for p in cast(list[object], perms):
            if isinstance(p, bool) or not isinstance(p, (str, int)):
                raise HTTPException(400, "permutation_ids entries must be strings or integers")
            entry = str(p)
            if not entry:
                raise HTTPException(400, "permutation_ids entries must be non-empty")
            permutation_ids.append(entry)
        # Mirror Limen's Cohort uniqueness contract — a duplicate id would hash
        # to a cohort_id Limen could never produce. Reject (fail loud) rather
        # than silently dedupe, since /cohorts is a public endpoint.
        if len(set(permutation_ids)) != len(permutation_ids):
            raise HTTPException(400, "permutation_ids must be unique")
        limen_output = _cohort_limen_output(experiment_id, round_id)
        try:
            manifest_id = cohorts.read_manifest_id(limen_output)
            cohort_id = cohorts.write_cohort_definition(
                root,
                name=name.strip(),
                experiment_dir=str(limen_output),
                manifest_id=manifest_id,
                permutation_ids=permutation_ids,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse({
            "cohort_id": cohort_id,
            "name": name.strip(),
            "permutation_ids": sorted(permutation_ids),
        })

    _graduation_handlers = (graduation_page, cohorts_list, create_cohort)
    del _graduation_handlers

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
                "experiments": _all_experiments(root),
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


_BACKTEST_LEDGER_COLUMNS: tuple[str, ...] = (
    # 22-column decoder-level metric ledger from Limen PR #520 (>= 3.2.1).
    # 7 distribution metrics x p5/p50/p95 + 1 scalar cvar_95.
    # All ship into the CSV with the `backtest_` prefix.
    "backtest_edge_per_signal_bps_p5",
    "backtest_edge_per_signal_bps_p50",
    "backtest_edge_per_signal_bps_p95",
    "backtest_trade_pnl_net_bps_p5",
    "backtest_trade_pnl_net_bps_p50",
    "backtest_trade_pnl_net_bps_p95",
    "backtest_cost_drag_bps_p5",
    "backtest_cost_drag_bps_p50",
    "backtest_cost_drag_bps_p95",
    "backtest_rolling_return_net_bps_p5",
    "backtest_rolling_return_net_bps_p50",
    "backtest_rolling_return_net_bps_p95",
    "backtest_return_on_exposure_p5",
    "backtest_return_on_exposure_p50",
    "backtest_return_on_exposure_p95",
    "backtest_drawdown_depth_bps_p5",
    "backtest_drawdown_depth_bps_p50",
    "backtest_drawdown_depth_bps_p95",
    "backtest_drawdown_duration_days_p5",
    "backtest_drawdown_duration_days_p50",
    "backtest_drawdown_duration_days_p95",
    "backtest_cvar_95_return_bps",
)

_RESULTS_COLUMNS: tuple[str, ...] = (
    "id",
    "precision",
    "recall",
    "confusion_tp_mean_return_pct",
    "confusion_fp_mean_return_pct",
    *_BACKTEST_LEDGER_COLUMNS,
)
_RESULTS_INT_COLS: frozenset[str] = frozenset({"id"})


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
    csv_path = _round_csv_path(round_dir)
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


# The metric set surfaced as statistics — the results-table columns minus
# `id`. The round page shows each metric's cross-permutation percentiles;
# the experiment page shows the mean of those across the experiment's rounds.
_STATS_METRICS: tuple[str, ...] = (
    "precision",
    "recall",
    "confusion_tp_mean_return_pct",
    "confusion_fp_mean_return_pct",
    *_BACKTEST_LEDGER_COLUMNS,
)
_STATS_PERCENTILES: tuple[tuple[str, float], ...] = (
    ("p5", 0.05), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p95", 0.95),
)


def _metric_percentiles(csv_path: Path) -> list[dict]:
    """Each metric column's cross-permutation p5/p25/p50/p75/p95 from a
    round's results CSV. Shared by the round page (shown as-is) and the
    experiment page (averaged across the experiment's rounds)."""
    df = pl.read_csv(csv_path, infer_schema_length=200)
    out: list[dict] = []
    for col in _STATS_METRICS:
        if col not in df.columns:
            continue
        s = df[col].cast(pl.Float64, strict=False).drop_nulls().drop_nans()
        if s.is_empty():
            continue
        row: dict = {"name": col, "label": _display_label(col)}
        for key, q in _STATS_PERCENTILES:
            row[key] = s.quantile(q)
        out.append(row)
    return out


def _compute_round_stats(round_dir: Path) -> dict:
    status = read_status(round_dir)
    # n_permutations is the progress-bar denominator, read from the round's
    # own manifest. A pre-migration round has no manifest.yml; fall back to
    # the default rather than crash the /stats poll (which fires every 2s
    # while the round page is open).
    target = 50
    manifest_path = round_dir / "manifest.yml"
    if manifest_path.exists():
        from limen.yaml.parser import parse
        yaml_dict, _parse_errors = parse(manifest_path)
        target = int((yaml_dict.get("uel") or {}).get("n_permutations", 50))
    csv_path = _round_csv_path(round_dir)

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

    # Each metric's cross-permutation percentiles, computed on whatever rows
    # the CSV has at poll time — the operator watches the distribution firm
    # up as permutations complete.
    return {"progress": progress, "stats": _metric_percentiles(csv_path)}


def _experiment_stats(experiment_dir: Path) -> list[dict]:
    """Experiment-level statistics — the same metric / percentile grid as a
    round's, each cell the mean of that cell across the experiment's rounds.
    A round with no results CSV yet contributes nothing."""
    rounds_dir = experiment_dir / "rounds"
    if not rounds_dir.is_dir():
        return []
    accum: dict[str, dict] = {}
    for round_dir in sorted(rounds_dir.iterdir()):
        csv_path = _round_csv_path(round_dir)
        if not csv_path.exists():
            continue
        for row in _metric_percentiles(csv_path):
            cell = accum.setdefault(row["name"], {"label": row["label"]})
            for key, _q in _STATS_PERCENTILES:
                value = row[key]
                if value is not None:
                    cell.setdefault(key, []).append(value)
    out: list[dict] = []
    for name in _STATS_METRICS:
        if name not in accum:
            continue
        cell = accum[name]
        entry: dict = {"name": name, "label": cell["label"]}
        for key, _q in _STATS_PERCENTILES:
            values = cell.get(key, [])
            entry[key] = (sum(values) / len(values)) if values else None
        out.append(entry)
    return out


# ---- Cross-round search ---------------------------------------------------

# Search-filter metrics mirror the results table / round-status set so all
# three surfaces stay in sync. The four confusion / precision columns plus
# the 22 backtest ledger columns are all individually filterable via range
# sliders on search.html.
_SEARCH_METRICS: tuple[str, ...] = (
    "precision",
    "recall",
    "confusion_tp_mean_return_pct",
    "confusion_fp_mean_return_pct",
    *_BACKTEST_LEDGER_COLUMNS,
)

# A round is indexable only when its CSV carries every search metric — any
# missing column means a pre-Limen-3.2.1 round (old backtest scalar surface
# that PR #520 retired) or a partial run. Those rounds are skipped from the
# index until they are re-run on a Limen that emits the new ledger.
_SEARCH_REQUIRED_COLS: frozenset[str] = frozenset({"id", *_SEARCH_METRICS})


def _ensure_round_index(round_dir: Path) -> Path | None:
    state = read_status(round_dir).get("state")
    if state in ACTIVE_STATES:
        return None
    manifest_path = round_dir / "manifest.yml"
    if not manifest_path.exists():
        return None
    csv_path = _round_csv_path(round_dir)
    if not csv_path.exists():
        return None

    idx_path = round_dir / ".index.parquet"
    # Cache validity = mtime fresh AND schema matches the current
    # _SEARCH_METRICS contract. The schema check is essential because
    # the metric set itself changed (PR #520 ledger retired the
    # `*_per_bar` derived metrics in favour of pre-scaled ledger
    # columns); any `.index.parquet` written by the prior code carries
    # the old schema and would crash `_compute_search_bounds` with
    # `ColumnNotFoundError` on `pl.col("precision")`. Falling through
    # rebuilds the parquet from the CSV — if the CSV still has only
    # old columns the `_SEARCH_REQUIRED_COLS.issubset(df.columns)`
    # check below skips the round; if it has the new ledger we get a
    # fresh parquet on the new schema.
    if (
        idx_path.exists()
        and idx_path.stat().st_mtime >= csv_path.stat().st_mtime
        and _SEARCH_REQUIRED_COLS.issubset(pl.read_parquet_schema(idx_path))
    ):
        return idx_path

    df = pl.read_csv(csv_path, infer_schema_length=200)
    if not _SEARCH_REQUIRED_COLS.issubset(df.columns):
        return None

    sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    # No more per-bar normalization — the new backtest ledger emits metrics
    # already scaled to per-signal / per-trade / per-day / fraction units,
    # so the indexer just reads each metric column straight from the CSV.
    #
    # The prior code's `.filter(pl.col("_bt") > 0)` was doing two jobs:
    # (a) guard the per-bar divisions from div-by-zero — gone with the
    # ledger refactor; (b) exclude permutations whose backtest never ran
    # (zero-signal / errored runs). Job (b) still applies — Limen ≥3.2.1
    # writes NaN ledger columns for permutations the backtest skipped, and
    # we don't want NaN rows polluting `.index.parquet` (search range
    # sliders would have to cope with nulls, and the round-status panel
    # just above already drops NaN via `drop_nulls().drop_nans()` — staying
    # symmetric with it). Use `edge_per_signal_bps_p50` as the canary: if
    # a permutation produced no tradable signals, every ledger percentile
    # is NaN; if it produced even one, p50 is finite.
    out = (
        df.lazy()
        .filter(
            pl.col("backtest_edge_per_signal_bps_p50").is_not_null()
            & pl.col("backtest_edge_per_signal_bps_p50").is_not_nan()
        )
        .select([
            pl.col("id").cast(pl.Int64).alias("permutation_id"),
            pl.lit(round_dir.parent.parent.name).alias("experiment_id"),
            pl.lit(round_dir.name).alias("round_id"),
            pl.lit(sha).alias("code_sha256"),
            *[pl.col(m).cast(pl.Float64, strict=False).alias(m) for m in _SEARCH_METRICS],
        ])
        .collect()
    )
    # Unique temp name per build: /search/bounds.json and /search/rounds.json
    # are fetched concurrently and both rebuild the same round's index — a
    # shared temp filename makes their os.replace calls race (one moves the
    # temp away, the other's os.replace then hits FileNotFoundError → 500).
    tmp = idx_path.parent / f".index.{secrets.token_hex(8)}.parquet.tmp"
    out.write_parquet(tmp, compression="snappy")
    os.replace(tmp, idx_path)
    return idx_path


def _list_experiment_names(root: Path) -> list[str]:
    base = root / "experiments"
    if not base.exists():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir())


def _ensure_indices(root: Path, experiment_names: list[str] | None) -> list[Path]:
    """Build or refresh every round's search index; return the paths of the
    indices that are currently valid. `_ensure_round_index` returns None for a
    round it will not vouch for (a pre-migration round with no manifest.yml, a
    CSV missing the search columns); those are dropped here, so a stale
    `.index.parquet` left on disk by an earlier indexing never feeds the search
    — it cannot pollute a result set or a bundle."""
    base = root / "experiments"
    if not base.exists():
        return []
    if experiment_names is None:
        edirs = [d for d in base.iterdir() if d.is_dir()]
    else:
        edirs = [base / n for n in experiment_names if (base / n).is_dir()]
    paths: list[Path] = []
    for ed in edirs:
        rd = ed / "rounds"
        if not rd.exists():
            continue
        for round_dir in rd.iterdir():
            if round_dir.is_dir():
                idx = _ensure_round_index(round_dir)
                if idx is not None:
                    paths.append(idx)
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
    paths = _ensure_indices(root, experiment_names)
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
    paths = _ensure_indices(root, experiment_names)
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
    paths = _ensure_indices(root, experiment_names)
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

                # Every round under one (exp_id, sha) key shares an identical
                # manifest (sha is the manifest hash) — the first round's copy
                # is the definition for the whole group.
                first_dir = root / "experiments" / exp_id / "rounds" / sorted_round_ids[0]
                z.writestr(f"{base}.yml", (first_dir / "manifest.yml").read_text())

                meta = {
                    "experiment_id": exp_id,
                    "manifest_sha256": sha,
                    "round_ids": sorted_round_ids,
                }
                z.writestr(f"{base}.json", json.dumps(meta, indent=2))

                csv_parts: list[pl.DataFrame] = []
                for rid in sorted_round_ids:
                    rdir = root / "experiments" / exp_id / "rounds" / rid
                    csv_path = _round_csv_path(rdir)
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
