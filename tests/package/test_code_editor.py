"""Tests for slice S08: dedicated SFD code-editor page.

These checks run without importing the runner, since the runner's runtime
deps (fastapi, polars, etc.) are not in the package's dev-extras. The slice
ships its own behavioural verification in the form of mechanical predicates
on the source files: route declarations, template/asset presence, and the
removal of the inline textarea from the experiment page."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / 'experiment_runner'


def test_code_template_exists() -> None:
    assert (PKG_DIR / 'templates' / 'code.html').is_file()


def test_code_editor_js_exists() -> None:
    assert (PKG_DIR / 'static' / 'code_editor.js').is_file()


def test_experiment_page_no_inline_code_textarea() -> None:
    text = (PKG_DIR / 'templates' / 'experiment.html').read_text(encoding='utf-8')
    assert 'id="code-editor"' not in text
    assert 'rows="5"' not in text
    assert '/experiments/{{ experiment_id }}/code' in text


def test_code_get_route_renders() -> None:
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    pattern = r'@app\.get\("/experiments/\{experiment_id\}/code"'
    assert re.search(pattern, text) is not None
    # The handler renders the new template.
    assert '"code.html"' in text


def test_code_put_route_persists_atomically() -> None:
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    pattern = r'@app\.put\("/experiments/\{experiment_id\}/code"'
    assert re.search(pattern, text) is not None
    # The handler writes via temp + os.replace (atomic) and version-bumps.
    put_idx = text.index('@app.put("/experiments/{experiment_id}/code"')
    body = text[put_idx:put_idx + 5000]
    assert 'code.py.tmp' in body
    assert 'os.replace' in body
    assert 'write_version_atomic' in body
    # 409 conflict path on stale if_match.
    assert 'status_code=409' in body


def test_code_put_does_not_block_invalid_syntax() -> None:
    """The PUT handler accepts any text. Mid-edit drafts may not parse;
    blocking save would force the user to keep broken Python in their head
    or in a side scratch buffer. Validation lives at /inspect (controls
    preview) and /rounds (round start), where broken code actually causes
    downstream failure."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    put_idx = text.index('@app.put("/experiments/{experiment_id}/code"')
    body = text[put_idx:put_idx + 5000]
    assert '_ast.parse(code)' not in body
    assert '"kind": "syntax_error"' not in body


def test_start_round_reads_from_disk_not_request_body() -> None:
    """The /rounds endpoint snapshots code+args from disk under the same flock
    that gates if_match. Pre-S08 it required `code` in the request body, which
    broke after the experiment page stopped sending it. Round start is the
    natural validation gate — broken code there is a real, blocking error."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    rounds_idx = text.index('@app.post("/experiments/{experiment_id}/rounds"')
    # The handler is long — extend the window to cover the final return.
    body = text[rounds_idx:rounds_idx + 10000]
    # Reads code+args from disk under flock — not from request body.
    assert 'code_path = exp_dir / "code.py"' in body
    assert 'args_path = exp_dir / "args.json"' in body
    # Round start validates the snapshot (PUT /code does NOT — that's the
    # editor's "accept any text" contract).
    assert '_ast.parse(code)' in body
    assert 'sfd_id' in body and 'not in code' in body
    # Doesn't try to read code or args from the request body any more.
    assert 'code = body.get("code"' not in body
    assert 'args = body.get("args"' not in body
    # Version-passive: no version bump, response uses current_version
    # (matching the 409 key) rather than a misleading "version" field.
    assert '"current_version": current_version' in body
    assert '"version": new_version' not in body
    assert 'new_version = current_version' not in body


def test_experiment_start_button_sends_only_if_match() -> None:
    """experiment.html start() must post only {if_match} to /rounds. If it
    starts sending {code} or {args} again, /rounds' disk-read invariant is
    superseded by client-supplied state."""
    text = (PKG_DIR / 'templates' / 'experiment.html').read_text(encoding='utf-8')
    start_idx = text.index('async function start()')
    block = text[start_idx:start_idx + 3000]
    # body literal contains only if_match — no code or args fields.
    assert 'const body = { if_match:' in block
    # gatherWorking() is not called inside start() any more.
    pre_post = block[:block.index('postJson(`/experiments/${EXP_ID}/rounds`')]
    assert 'gatherWorking()' not in pre_post


def test_save_endpoints_reject_non_object_body() -> None:
    """Both PUT /code and POST /working should reject non-object JSON bodies
    with a 400 instead of crashing on body.get(...)."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    for marker in (
        '@app.put("/experiments/{experiment_id}/code"',
        '@app.post("/experiments/{experiment_id}/working"',
        '@app.post("/experiments/{experiment_id}/rounds"',
    ):
        idx = text.index(marker)
        block = text[idx:idx + 1200]
        assert 'isinstance(body, dict)' in block, f'missing object check in {marker}'


def test_save_working_rejects_empty_body() -> None:
    """save_working must reject requests with neither code nor args, otherwise
    the version bumps with no actual write."""
    text = (PKG_DIR / 'app.py').read_text(encoding='utf-8')
    idx = text.index('@app.post("/experiments/{experiment_id}/working"')
    block = text[idx:idx + 2500]
    assert 'has_code or has_args' in block
