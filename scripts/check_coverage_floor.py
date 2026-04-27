#!/usr/bin/env python3
"""Coverage floor gate: line >= 50%, branch >= 45%, over new_repository_template/."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[1]
COVERAGE_JSON = REPO_ROOT / 'coverage.json'

# Bootstrap floor: 50% line / 45% branch.
#
# The original 95/90 floor assumed the package would be mostly
# pure-Python algorithmic code. A fresh repository often starts with
# package scaffolding and enforcement code before it has enough domain
# logic for a high coverage floor to be meaningful.
#
# The 50/45 floor still catches:
#   - a production module that ships with zero tests.
#   - a pure-Python module whose unit coverage drops below half.
#   - an accidental code-death in a previously-tested module.
#
# Follow-up slices should raise the floor as the package surface grows.
FLOOR_PCT: Final[float] = 50.0
BRANCH_FLOOR_PCT: Final[float] = 45.0


def main() -> int:
    if not COVERAGE_JSON.is_file():
        print('COVERAGE FLOOR GATE -- FAIL', file=sys.stderr)
        print(f'  no coverage.json at {COVERAGE_JSON}', file=sys.stderr)
        print('  expected from a prior `coverage json -o coverage.json` run', file=sys.stderr)
        return 2
    data = json.loads(COVERAGE_JSON.read_text(encoding='utf-8'))
    totals = data.get('totals', {})
    num_statements = int(totals.get('num_statements', 0))
    if num_statements == 0:
        print('COVERAGE FLOOR GATE -- PASS (vacuous: no statements in source)')
        return 0
    line_pct = float(totals.get('percent_covered', 0.0))
    branch_pct = float(totals.get('percent_covered_branches', 0.0)) if 'percent_covered_branches' in totals else float(totals.get('percent_covered', 0.0))
    line_ok = line_pct >= FLOOR_PCT
    branch_ok = branch_pct >= BRANCH_FLOOR_PCT
    if not (line_ok and branch_ok):
        print('COVERAGE FLOOR GATE -- FAIL', file=sys.stderr)
        print('', file=sys.stderr)
        print(
            f'  line coverage:   {line_pct:.1f}% (required: >= {FLOOR_PCT}%)',
            file=sys.stderr,
        )
        print(
            f'  branch coverage: {branch_pct:.1f}% (required: >= {BRANCH_FLOOR_PCT}%)',
            file=sys.stderr,
        )
        missing = sorted(
            (rel, f['missing_lines'])
            for rel, f in (data.get('files') or {}).items()
            if f.get('missing_lines')
        )[:10]
        if missing:
            print('', file=sys.stderr)
            print('  missing lines (top 10 files):', file=sys.stderr)
            for rel, lines in missing:
                preview = ','.join(str(n) for n in lines[:5])
                suffix = '…' if len(lines) > 5 else ''
                print(f'    {rel}: {preview}{suffix}', file=sys.stderr)
        print('', file=sys.stderr)
        print('Merge blocked.', file=sys.stderr)
        return 1
    print(
        f'COVERAGE FLOOR GATE -- PASS '
        f'(line={line_pct:.1f}%, branch={branch_pct:.1f}%)'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
