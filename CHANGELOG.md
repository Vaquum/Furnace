# v0.8.0

- Replace the 5-row `<textarea>` on the experiment page with a dedicated SFD code-editor route at `/experiments/{id}/code`. The editor occupies the full viewport below the nav, paginates a mono line-number gutter that scroll-syncs with the textarea, marks the active line with a 1 px accent left-border (DS hairline-only), inserts 4 spaces on `Tab` (and outdents one level on `Shift+Tab` if the caret sits at start-of-indent) without losing focus, saves on `⌘S`/`Ctrl+S` (preventing the browser's "save page" dialog), and shows a `· UNSAVED` mono-micro accent-warm kicker beside the Save button while the buffer is dirty. The new `PUT /experiments/{id}/code` endpoint writes `code.py` atomically (temp + `os.replace`), bumps the existing optimistic-concurrency version counter, and returns `409` with the current version if the client's `if_match` is stale (the same conflict shape `/working` already uses, so the editor's reload-and-retry path is identical to today's). The experiment page's `Code` section is now a single `Open code editor →` link; args editing stays where it is. The round page's `Frozen code` `<details>` panel is unchanged — that's the immutable record of a past round.

# v0.7.0

- Drop the `127.0.0.1:` prefix from the compose port mapping so the runner is reachable on the host's public IP, mirroring Explorer's posture (`<TDW_IP>:8501`). The runner's auth (slice S06) is the security boundary; the loopback bind from S02 was the pre-auth baseline and is no longer needed. The `pr_checks_deploy_contract` regression gate flips: instead of asserting every port entry has `host_ip 127.0.0.1`, it now asserts (a) every port entry's container side is `8502`, and (b) every port entry has an explicit host port — short form must include `HOST:CONTAINER` (rejects bare `'8502'` and `127.0.0.1::8502`); long form must set `published` (rejects `0`/`'0'` since Compose treats those as "pick a random host port"). Host IP is unconstrained, and the host-port *value* is flexible (operator picks via the existing `vars.EXPERIMENT_RUNNER_PORT` repo variable) — but it must be specified, so a future PR can't silently bypass `vars.EXPERIMENT_RUNNER_PORT` by publishing on an ephemeral port.

# v0.6.0

- Protect every runtime endpoint with HTTP Basic + Bearer-token auth. Browser users authenticate via HTTP Basic against `APP_USERNAME` / `APP_PASSWORD` (e.g. `curl -u user:pass http://host/...`); programmatic API consumers send `Authorization: Bearer <API_SECRET>`. The middleware accepts both schemes case-insensitively per RFC 7235 (so `bearer <token>` works alongside `Bearer <token>`). Without auth, every JSON, HTML, log, and bundle route returns `401` with `WWW-Authenticate: Basic realm="experiment_runner"`. Three exemptions: the new `/health` endpoint (used by the compose healthcheck), the `/static/*` mount (CSS/JS — not sensitive), and the existing `HEAD /` smoke check from slice S02 (kept public so the operator's `curl -sI` still works). The runner refuses to start (`RuntimeError`) if any of the three env vars is unset — there is no "no auth" mode. The deploy workflow plumbs the secrets from repo settings (`secrets.APP_USERNAME`, `secrets.APP_PASSWORD`, `secrets.API_SECRET`) through to the compose `.env` on the TDW host so the container reads them at startup, with a fail-fast guard rejecting any secret value that contains a newline (which would corrupt `.env` line-orientation). The compose healthcheck moves from `GET /` to `GET /health` so the unauthenticated probe still works after this change.

# v0.5.2

- Fix the runner's container-startup crash: `_load_build_meta()` reads `pyproject.toml` from `PKG_DIR.parent` at app import time, but the Dockerfile copied only the `experiment_runner/` package — `/app/pyproject.toml` did not exist and the import raised `FileNotFoundError` before uvicorn could start. The container exited with status 1, `compose up -d --wait` reported "is unhealthy" within a second, and the deploy workflow failed. The Dockerfile now `COPY`s `pyproject.toml` into the image. The deploy workflow also passes `GIT_SHA=${{ github.sha }}` as a Docker build arg, which the Dockerfile bakes into a `GIT_SHA` env var so the build-stamp in the page footer renders the deployed commit (the in-container `git rev-parse HEAD` previously raised `CalledProcessError` because there is no `.git` directory; the function caught it and returned an empty SHA, leaving the build-stamp without the commit hash even though `git`'s own "fatal: not a git repository" was visible in container logs).

# v0.5.1

- Migrate all `TemplateResponse(...)` calls in `experiment_runner/app.py` to the starlette 1.0 signature (`request` is now the first positional arg). The previous old-style `(name, context)` form was a silent runtime failure on starlette 1.0.0+: the template name was being read as `request` (a string), the context dict was being read as `name`, and the unhashable dict propagated into jinja2's template cache as `TypeError: unhashable type: 'dict'` — every HTML page returned 500 (jobs page, experiment page, search page, round page, results page, and the `_partials/*` rows). Pin `starlette>=1.0.0` in `experiment_runner/requirements.txt` so the contract is explicit at install time.

# v0.5.0

- Deploy experiment_runner to TDW server: Dockerfile (port 8502, `--host` CLI flag), loopback-only compose with rw bind mount on `/data/experiments`, deploy workflow with host-data-root allowlist (rejects typos and `/data/notebooks`), and `pr_checks_deploy_contract` PR-time regression gate. The mount gate also rejects additional bind mounts whose `source` resolves to `/data/notebooks` (or a subpath), in literal or `${VAR:-...}` form, and the destructive-volume-ops scanner now sweeps every workflow including its own file (no `--exclude` self-skip), with the gate's documentation reworded to avoid literal pattern matches.

# v0.4.0

- Replace the experiment page's data_source JSON textarea with code-driven controls: AST-extract the SFD's manifest data_source params and merge with `inspect.signature(get_spot_klines)` via a new `/experiments/{id}/inspect` endpoint; render one input per param with override > manifest > library > fallback prefill, design-system Field-grouping, and an `args.json` snapshot that captures every shown field. Honour the SFD manifest in `run_round` when no overrides are set, and stamp `vX.Y.Z · <short-sha>` on every page.

# v0.3.0

- Add CSV export from the rounds results view, round bundle download from the round status page, cross-round search at /search with parquet-backed filter, dedup'd search bundle export, copy-curl, multi-experiment scope, bidirectional slider cross-talk; fix per-bar metric denominator (bars_total) and stop dropping zero-trade rows.

# v0.2.1

- Make first-run repository bootstrap create and merge a bootstrap PR through protected main.

# v0.2.0

- Add the self-bootstrapping repository law template.

# v0.1.0

- Initial repository law baseline.
