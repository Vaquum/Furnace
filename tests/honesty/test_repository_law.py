from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_required_status_contexts_include_hard_power() -> None:
    payload = json.loads((REPO_ROOT / '.github/rulesets/main.json').read_text())
    checks = next(rule for rule in payload['rules'] if rule['type'] == 'required_status_checks')
    contexts = {entry['context'] for entry in checks['parameters']['required_status_checks']}
    assert {
        'pr_checks_cc',
        'pr_checks_lint',
        'pr_checks_ruleset',
        'pr_checks_slice',
        'pr_checks_fail_loud',
        'pr_checks_typing',
        'pr_checks_version',
        'pr_checks_tests',
        'pr_checks_honesty',
    } <= contexts
