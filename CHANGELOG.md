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
