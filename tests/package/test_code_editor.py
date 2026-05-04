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
    text = (PKG_DIR / 'templates' / 'experiment.html').read_text()
    assert 'id="code-editor"' not in text
    assert 'rows="5"' not in text
    assert '/experiments/{{ experiment_id }}/code' in text


def test_code_get_route_renders() -> None:
    text = (PKG_DIR / 'app.py').read_text()
    pattern = r'@app\.get\("/experiments/\{experiment_id\}/code"'
    assert re.search(pattern, text) is not None
    # The handler renders the new template.
    assert '"code.html"' in text


def test_code_put_route_persists_atomically() -> None:
    text = (PKG_DIR / 'app.py').read_text()
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


def test_code_put_rejects_syntax_error() -> None:
    """The PUT handler validates Python via ast.parse and returns a 400 with
    a structured detail (kind/line/col/msg) if the code does not parse, before
    acquiring the lock or writing anything. Saving broken code would otherwise
    leave the experiment in a confusing state where /inspect returns empty."""
    text = (PKG_DIR / 'app.py').read_text()
    put_idx = text.index('@app.put("/experiments/{experiment_id}/code"')
    body = text[put_idx:put_idx + 5000]
    assert '_ast.parse(code)' in body
    assert 'except SyntaxError' in body
    assert '"kind": "syntax_error"' in body
