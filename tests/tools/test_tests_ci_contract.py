from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_WORKFLOW = REPO_ROOT / '.github/workflows/pr_checks_tests.yml'
EXPECTED_TEST_COMMAND = 'pytest tests/package -q --maxfail=1'


def test_pr_checks_tests_workflow_exists() -> None:
    assert TESTS_WORKFLOW.exists()


def test_pr_checks_tests_pins_python_and_runtime_suite_command() -> None:
    workflow = TESTS_WORKFLOW.read_text(encoding='utf-8')

    assert "python-version: '3.12'" in workflow
    assert "python -m pip install --upgrade pip '.[dev]'" in workflow
    assert EXPECTED_TEST_COMMAND in workflow
    assert 'continue-on-error' not in workflow
