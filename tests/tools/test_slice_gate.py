from __future__ import annotations

from pathlib import Path

from tools import slice_gate

SIGNIFICANCE_BLOCK = (
    '> **Significance.** This is the exact slice contract.\n'
    '> It must be preserved byte-for-byte.'
)


def _template(tmp_path: Path) -> Path:
    path = tmp_path / 'slice.yml'
    path.write_text(
        'name: Slice\n'
        'body:\n'
        '  - type: textarea\n'
        '    attributes:\n'
        '      value: |\n'
        '        > **Significance.** This is the exact slice contract.\n'
        '        > It must be preserved byte-for-byte.\n',
        encoding='utf-8',
    )
    return path


def _issue(body: str) -> dict[str, object]:
    return {
        'title': 'feat: add law template',
        'state': 'open',
        'labels': ['slice'],
        'body': body,
        'is_pull_request': False,
    }


def _body(out_of_scope: str = '- (none)') -> str:
    return (
        f'{SIGNIFICANCE_BLOCK}\n\n'
        '## Surfaces\n'
        '- `tools/**`\n'
        '- `.github/workflows/**`\n\n'
        '## Out of Scope\n'
        f'{out_of_scope}\n'
    )


def test_gate_accepts_open_slice_issue_with_matching_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(slice_gate, 'fetch_issue', lambda _repo, _number: _issue(_body()))

    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #9',
        ['tools/version_gate.py', '.github/workflows/pr_checks_version.yml'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert failures == []


def test_gate_rejects_pr_number_used_as_slice_issue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    issue = _issue(_body())
    issue['is_pull_request'] = True
    monkeypatch.setattr(slice_gate, 'fetch_issue', lambda _repo, _number: issue)

    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #8',
        ['tools/version_gate.py'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert failures == [
        '#8 is a pull request, not an issue. The closing reference must point at an OPEN '
        'slice issue filed via the slice template at `.github/ISSUE_TEMPLATE/slice.yml`, '
        'not another PR.'
    ]


def test_gate_requires_significance_blockquote(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(slice_gate, 'fetch_issue', lambda _repo, _number: _issue(_body().replace(
        SIGNIFICANCE_BLOCK,
        '> **Significance.** Different words.',
    )))

    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #9',
        ['tools/version_gate.py'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert any('missing 1 of 1 full Significance blockquotes' in item for item in failures)


def test_gate_blocks_files_outside_surfaces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(slice_gate, 'fetch_issue', lambda _repo, _number: _issue(_body()))

    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #9',
        ['README.md'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert any('not listed in issue #9 Surfaces' in item for item in failures)


def test_gate_blocks_out_of_scope_even_when_surface_allows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        slice_gate,
        'fetch_issue',
        lambda _repo, _number: _issue(_body('- `tools/experimental.py`')),
    )

    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #9',
        ['tools/experimental.py'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert any('listed in issue #9 Out of Scope' in item for item in failures)


def test_multiple_closing_references_fail_before_api_call(tmp_path: Path) -> None:
    failures = slice_gate.gate(
        'feat: add law template',
        'Closes #9\nFixes #10',
        ['tools/version_gate.py'],
        _template(tmp_path),
        'Vaquum/new-repository-template',
    )
    assert failures == [
        'PR body has 2 closing references (#9, #10). The PR must close exactly one slice issue.'
    ]
