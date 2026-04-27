from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_WORKFLOW = REPO_ROOT / '.github/workflows/bootstrap_repository.yml'
LINT_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_lint.yml'
RULESET_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_ruleset.yml'
TYPING_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_typing.yml'
VERSION_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_version.yml'
FAIL_LOUD_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_fail_loud.yml'
BOOTSTRAP_SCRIPT = REPO_ROOT / 'tools/bootstrap_repository.py'


def test_bootstrap_uses_pr_path_for_protected_main() -> None:
    workflow = BOOTSTRAP_WORKFLOW.read_text(encoding='utf-8')

    assert 'REPO_BOOTSTRAP_TOKEN is required' in workflow
    assert 'git push origin "HEAD:${BOOTSTRAP_BRANCH}"' in workflow
    assert 'gh issue create' in workflow
    assert 'gh pr create' in workflow
    assert 'gh pr checks "$pr_number"' in workflow
    assert 'gh pr merge "$pr_number"' in workflow
    assert 'git push\n' not in workflow


def test_bootstrap_pr_is_a_valid_slice_shape() -> None:
    workflow = BOOTSTRAP_WORKFLOW.read_text(encoding='utf-8')

    assert 'gh label create slice' in workflow
    assert 'print(\'\\n\\n## Surfaces\\n- `**`\\n\\n## Out of Scope\\n- (none)\\n\')' in workflow
    assert 'printf \'Closes #%s\\n\' "$issue_number" > bootstrap_pr.md' in workflow


def test_initial_specialization_uses_bootstrap_modes() -> None:
    source_template = 'new' '-repository-template'
    for workflow in (
        RULESET_WORKFLOW,
        TYPING_WORKFLOW,
        VERSION_WORKFLOW,
        FAIL_LOUD_WORKFLOW,
    ):
        text = workflow.read_text(encoding='utf-8')
        assert f'base == "{source_template}" and head == repo' in text


def test_lint_workflow_has_no_template_package_root() -> None:
    workflow = LINT_WORKFLOW.read_text(encoding='utf-8')

    assert 'new_repository_template' not in workflow
    assert 'steps.package.outputs.package_root' in workflow


def test_ruleset_lookup_ignores_organization_rulesets() -> None:
    script = BOOTSTRAP_SCRIPT.read_text(encoding='utf-8')

    assert "item.get('source_type') == 'Repository'" in script


def test_bootstrap_rewrite_leaves_workflow_files_static() -> None:
    script = BOOTSTRAP_SCRIPT.read_text(encoding='utf-8')

    assert "rel_path.startswith('.github/workflows/')" in script
    assert 'changed += _write_workflows(package_name)' not in script
    for dead_name in (
        '_lint_workflow',
        '_honesty_workflow',
        '_typing_workflow',
        '_deploy_workflow',
        '_bootstrap_workflow',
        '_write_workflows',
    ):
        assert f'def {dead_name}' not in script
