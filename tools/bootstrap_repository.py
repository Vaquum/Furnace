#!/usr/bin/env python3
"""Configure a repository created from the law template."""

from __future__ import annotations

import argparse
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
BOOTSTRAP_SCRIPT: Final[Path] = Path(__file__).resolve()
RULESET_PATH: Final[Path] = REPO_ROOT / '.github' / 'rulesets' / 'main.json'
TEMPLATE_LABEL_REPOSITORY: Final[str] = 'Vaquum/new-repository-template'

TEXT_SUFFIXES: Final[frozenset[str]] = frozenset({
    '',
    '.cfg',
    '.gitignore',
    '.ini',
    '.json',
    '.md',
    '.py',
    '.toml',
    '.txt',
    '.yaml',
    '.yml',
})

SKIP_DIRS: Final[frozenset[str]] = frozenset({
    '.git',
    '.mypy_cache',
    '.pytest_cache',
    '.ruff_cache',
    '.venv',
    '__pycache__',
    'build',
    'dist',
    'htmlcov',
    'node_modules',
    'venv',
})

KNOWN_SEED_PACKAGES: Final[frozenset[str]] = frozenset({
    'backtest_simulator',
    'repo_law_template',
})


def _load_pyproject() -> dict[str, object]:
    path = REPO_ROOT / 'pyproject.toml'
    try:
        return tomllib.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f'bootstrap: cannot parse pyproject.toml: {exc}') from exc


def _slug_to_package(slug: str) -> str:
    package = re.sub(r'[^0-9A-Za-z_]+', '_', slug).strip('_').lower()
    package = re.sub(r'_+', '_', package)
    if not package:
        raise SystemExit('bootstrap: repository name did not produce a package name')
    if package[0].isdigit():
        package = f'_{package}'
    if keyword.iskeyword(package):
        package = f'{package}_pkg'
    return package


def _repo_name_from_env() -> str:
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    if '/' in repository:
        return repository.rsplit('/', 1)[1]
    return Path.cwd().name


def _owner_from_env() -> str | None:
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    if '/' in repository:
        return repository.split('/', 1)[0]
    return os.environ.get('GITHUB_REPOSITORY_OWNER') or None


def _package_roots_from_config(pyproject: dict[str, object]) -> set[str]:
    roots = set(KNOWN_SEED_PACKAGES)
    project = pyproject.get('project')
    if isinstance(project, dict):
        name = project.get('name')
        if isinstance(name, str) and name:
            roots.add(_slug_to_package(name))

    tool = pyproject.get('tool')
    if isinstance(tool, dict):
        pyright = tool.get('pyright')
        if isinstance(pyright, dict):
            include = pyright.get('include')
            if isinstance(include, list):
                roots.update(str(item) for item in include if isinstance(item, str))

    for budget_name in ('typing_budget.json', 'fail_loud_budget.json'):
        path = REPO_ROOT / '.github' / budget_name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            root = payload.get('package_root')
            if isinstance(root, str) and root:
                roots.add(root)

    return {root for root in roots if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', root)}


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob('*')):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        if path == BOOTSTRAP_SCRIPT:
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {'LICENSE', 'README'}:
            continue
        files.append(path)
    return files


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return None


def _write_text_if_changed(path: Path, text: str) -> bool:
    current = _read_text(path)
    if current == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return True


def _significant_lines(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            count += 1
    return count


def _replace_identity_tokens(
    repo_slug: str,
    package_name: str,
    old_packages: set[str],
    owner: str | None,
) -> int:
    changed = 0
    display_name = repo_slug.replace('-', ' ').replace('_', ' ').title()
    description = 'Python package with repository law built in.'
    dashed_seeds = {pkg.replace('_', '-') for pkg in old_packages}
    label_template_sentinel = '__REPOSITORY_LAW_LABEL_TEMPLATE__'
    owner_name = owner or 'Vaquum'

    for path in _iter_text_files():
        text = _read_text(path)
        if text is None:
            continue
        rel_path = path.relative_to(REPO_ROOT).as_posix()
        if rel_path.startswith('.github/workflows/'):
            continue
        if rel_path == 'tools/bootstrap_repository.py':
            updated = text.replace('Vaquum/new-repository-template', label_template_sentinel)
        else:
            updated = text
        for old_package in sorted(old_packages, key=len, reverse=True):
            if old_package != package_name:
                updated = updated.replace(old_package, package_name)
        for old_slug in sorted(dashed_seeds, key=len, reverse=True):
            if old_slug != repo_slug:
                updated = updated.replace(old_slug, repo_slug)
        updated = updated.replace('{REPOSITORY_NAME}', repo_slug)
        updated = updated.replace('{ONE_SENTENCE_DESCRIPTION}', description)
        updated = updated.replace('{DISPLAY_NAME}', display_name)
        updated = updated.replace(
            'pytest tests/origo_source_native',
            'pytest tests/package -q --maxfail=1',
        )
        updated = updated.replace(f'Vaquum/{package_name}', f'{owner_name}/{repo_slug}')
        updated = updated.replace(label_template_sentinel, 'Vaquum/new-repository-template')
        if _write_text_if_changed(path, updated):
            changed += 1
    return changed


def _remove_table(text: str, table_name: str) -> str:
    escaped = re.escape(table_name)
    return re.sub(rf'\n?\[{escaped}\]\n.*?(?=\n\[|\Z)', '\n', text, flags=re.DOTALL)


def _remove_private_integration_extra(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skip = False
    bracket_depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('# Private sibling packages'):
            skip = True
            bracket_depth = 0
            continue
        if skip:
            bracket_depth += line.count('[')
            bracket_depth -= line.count(']')
            if stripped == '' and bracket_depth <= 0:
                skip = False
            elif bracket_depth <= 0 and stripped == ']':
                skip = False
            continue
        if stripped.startswith('integration = ['):
            skip = True
            bracket_depth = line.count('[') - line.count(']')
            if bracket_depth <= 0:
                skip = False
            continue
        out.append(line)
    return '\n'.join(out).rstrip() + '\n'


def _remove_package_per_file_ignores(text: str, package_name: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if stripped.startswith(f'"{package_name}/') and ' = ' in stripped:
            if stripped.endswith(']'):
                idx += 1
                continue
            idx += 1
            while idx < len(lines):
                if lines[idx].strip() == ']':
                    idx += 1
                    break
                idx += 1
            continue
        out.append(line)
        idx += 1
    return '\n'.join(out).rstrip() + '\n'


def _replace_line(text: str, pattern: str, replacement: str) -> str:
    return re.sub(pattern, replacement, text, flags=re.MULTILINE)


def _rewrite_pyproject(repo_slug: str, package_name: str) -> bool:
    path = REPO_ROOT / 'pyproject.toml'
    text = path.read_text(encoding='utf-8')
    text = _remove_private_integration_extra(text)
    text = _remove_table(text, 'tool.uv')
    text = _remove_table(text, 'project.scripts')
    text = _remove_table(text, 'tool.dagster')
    text = _remove_package_per_file_ignores(text, package_name)
    text = _replace_line(text, r'^name = ".*"$', f'name = "{repo_slug}"')
    text = _replace_line(
        text,
        r'^include = \["[A-Za-z_][A-Za-z0-9_]*\*"\]$',
        f'include = ["{package_name}*"]',
    )
    text = _replace_line(
        text,
        r'^include = \["[A-Za-z_][A-Za-z0-9_]*"\]$',
        f'include = ["{package_name}"]',
    )
    text = _replace_line(
        text,
        r'^known-first-party = \["[A-Za-z_][A-Za-z0-9_]*"\]$',
        f'known-first-party = ["{package_name}"]',
    )
    text = _replace_line(
        text,
        r'^paths = \["[A-Za-z_][A-Za-z0-9_]*"\]$',
        f'paths = ["{package_name}"]',
    )
    text = _replace_line(
        text,
        r'^source = \["[A-Za-z_][A-Za-z0-9_]*"\]$',
        f'source = ["{package_name}"]',
    )
    text = re.sub(r'\n{3,}', '\n\n', text).rstrip() + '\n'
    return _write_text_if_changed(path, text)


def _create_package_baseline(repo_slug: str, package_name: str) -> int:
    changed = 0
    package_dir = REPO_ROOT / package_name
    package_dir.mkdir(exist_ok=True)
    init_path = package_dir / '__init__.py'
    if not init_path.exists():
        changed += _write_text_if_changed(
            init_path,
            f'"""Public package surface for {repo_slug}."""\n\n__all__: list[str] = []\n',
        )

    package_tests = REPO_ROOT / 'tests' / 'package' / 'test_import.py'
    if not package_tests.exists():
        changed += _write_text_if_changed(
            package_tests,
            (
                'from __future__ import annotations\n\n'
                'import importlib\n\n\n'
                'def test_package_imports() -> None:\n'
                f"    assert importlib.import_module('{package_name}').__name__ == '{package_name}'\n"
            ),
        )

    honesty_tests = REPO_ROOT / 'tests' / 'honesty' / 'test_repository_law.py'
    if not honesty_tests.exists():
        changed += _write_text_if_changed(
            honesty_tests,
            (
                'from __future__ import annotations\n\n'
                'import json\n'
                'from pathlib import Path\n\n'
                'REPO_ROOT = Path(__file__).resolve().parents[2]\n\n\n'
                'def test_required_status_contexts_include_hard_power() -> None:\n'
                "    payload = json.loads((REPO_ROOT / '.github/rulesets/main.json').read_text())\n"
                "    checks = next(rule for rule in payload['rules'] if rule['type'] == 'required_status_checks')\n"
                "    contexts = {entry['context'] for entry in checks['parameters']['required_status_checks']}\n"
                '    assert {\n'
                "        'pr_checks_cc',\n"
                "        'pr_checks_lint',\n"
                "        'pr_checks_ruleset',\n"
                "        'pr_checks_slice',\n"
                "        'pr_checks_fail_loud',\n"
                "        'pr_checks_typing',\n"
                "        'pr_checks_version',\n"
                "        'pr_checks_tests',\n"
                "        'pr_checks_honesty',\n"
                '    } <= contexts\n'
            ),
        )

    changelog = REPO_ROOT / 'CHANGELOG.md'
    if not changelog.exists():
        changed += _write_text_if_changed(
            changelog,
            '# v0.1.0\n\n- Initial repository law baseline.\n',
        )

    return changed


def _write_typing_budget(package_name: str) -> bool:
    path = REPO_ROOT / '.github' / 'typing_budget.json'
    payload = {
        'schema_version': 2,
        'package_root': package_name,
        'excludes': ['__pycache__', 'build', 'dist'],
        'patterns': {
            'any_annotation': {'pattern': r':\s*Any\b', 'total': 0},
            'any_return': {'pattern': r'->\s*Any\b', 'total': 0},
            'any_import': {'pattern': r'from typing import[^\n]*\bAny\b', 'total': 0},
            'cast_any': {'pattern': r'cast\([^)]*\bAny\b', 'total': 0},
            'dict_any': {'pattern': r'dict\[[^]]*\bAny\b', 'total': 0},
            'list_any': {'pattern': r'list\[\s*Any\b', 'total': 0},
            'tuple_any': {'pattern': r'tuple\[[^]]*\bAny\b', 'total': 0},
            'type_ignore': {'pattern': r'#\s*type:\s*ignore', 'total': 0},
            'pyright_ignore': {'pattern': r'#\s*pyright:\s*ignore', 'total': 0},
            'noqa': {'pattern': r'#\s*noqa', 'total': 0},
        },
        'any_references': {'total': 0},
        'pyright_errors': {'total': 0},
    }
    return _write_text_if_changed(path, json.dumps(payload, indent=2) + '\n')


def _write_fail_loud_budget(package_name: str) -> bool:
    path = REPO_ROOT / '.github' / 'fail_loud_budget.json'
    categories = [
        'bare_except',
        'empty_pass',
        'empty_ellipsis',
        'empty_return_none',
        'empty_continue_break',
        'contextlib_suppress',
        'errors_ignore_kwarg',
    ]
    payload = {
        'schema_version': 1,
        'package_root': package_name,
        'excludes': ['__pycache__', 'build', 'dist'],
        'categories': {category: {'total': 0} for category in categories},
    }
    return _write_text_if_changed(path, json.dumps(payload, indent=2) + '\n')


def _write_module_budgets(package_name: str) -> bool:
    path = REPO_ROOT / '.github' / 'module_budgets.json'
    payload: dict[str, int] = {}
    package_dir = REPO_ROOT / package_name
    if package_dir.is_dir():
        for source_file in sorted(package_dir.rglob('*.py')):
            rel = source_file.relative_to(REPO_ROOT).as_posix()
            payload[rel] = max(10, _significant_lines(source_file) + 20)
    scripts_dir = REPO_ROOT / 'scripts'
    if scripts_dir.is_dir():
        for script in sorted(scripts_dir.glob('*.py')):
            rel = script.relative_to(REPO_ROOT).as_posix()
            payload[rel] = 120 if script.name != '__init__.py' else 10
    return _write_text_if_changed(path, json.dumps(payload, indent=2) + '\n')


def _write_budgets(package_name: str) -> int:
    changed = 0
    changed += _write_typing_budget(package_name)
    changed += _write_fail_loud_budget(package_name)
    changed += _write_module_budgets(package_name)
    return changed


def _is_baseline_seed_package_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    files = [
        p
        for p in path.rglob('*')
        if p.is_file() and '__pycache__' not in p.parts
    ]
    if len(files) != 1 or files[0].name != '__init__.py':
        return False
    text = files[0].read_text(encoding='utf-8')
    return (
        text.startswith('"""Public package surface for ')
        and '__all__: list[str] = []' in text
    )


def _rename_seed_package_dirs(package_name: str, old_packages: set[str]) -> int:
    changed = 0
    target_exists = (REPO_ROOT / package_name).exists()
    for old_package in sorted(old_packages):
        old_dir = REPO_ROOT / old_package
        if old_package == package_name or not old_dir.is_dir():
            continue
        if not target_exists:
            old_dir.rename(REPO_ROOT / package_name)
            target_exists = True
            changed += 1
            continue
        if _is_baseline_seed_package_dir(old_dir):
            shutil.rmtree(old_dir)
            changed += 1
    return changed


def _apply_file_bootstrap(repo_slug: str, package_name: str, owner: str | None) -> None:
    pyproject = _load_pyproject()
    old_packages = _package_roots_from_config(pyproject)
    old_packages.add(package_name)

    changed = 0
    changed += _rename_seed_package_dirs(package_name, old_packages)
    changed += _replace_identity_tokens(repo_slug, package_name, old_packages, owner)
    changed += _rewrite_pyproject(repo_slug, package_name)
    changed += _create_package_baseline(repo_slug, package_name)
    changed += _write_budgets(package_name)
    print(f'bootstrap: file bootstrap complete ({changed} file groups changed)')


def _run_gh(args: list[str], *, input_text: str | None = None) -> str:
    if shutil.which('gh') is None:
        raise SystemExit('bootstrap: gh is required for GitHub configuration')
    result = subprocess.run(
        ['gh', *args],
        input=input_text,
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or 'no output'
        raise SystemExit(
            f"bootstrap: gh {' '.join(args)} failed with exit {result.returncode}: {stderr}"
        )
    return result.stdout


def _repo_full_name(owner: str | None, repo_slug: str) -> str:
    if owner:
        return f'{owner}/{repo_slug}'
    repository = os.environ.get('GITHUB_REPOSITORY', '')
    if '/' in repository:
        return repository
    raise SystemExit('bootstrap: --owner is required outside GitHub Actions for --github-only')


def _find_ruleset_id(repo: str, ruleset_name: str) -> int | None:
    output = _run_gh(['api', '--paginate', f'repos/{repo}/rulesets'])
    payload = json.loads(output)
    if not isinstance(payload, list):
        raise SystemExit('bootstrap: expected list from repository rulesets API')
    for item in payload:
        if (
            isinstance(item, dict)
            and item.get('name') == ruleset_name
            and item.get('source_type') == 'Repository'
        ):
            ruleset_id = item.get('id')
            if isinstance(ruleset_id, int):
                return ruleset_id
    return None


def _apply_ruleset(repo: str) -> int:
    ruleset = json.loads(RULESET_PATH.read_text(encoding='utf-8'))
    name = ruleset.get('name')
    if not isinstance(name, str) or not name:
        raise SystemExit('bootstrap: ruleset snapshot must have a name')
    ruleset_id = _find_ruleset_id(repo, name)
    body = json.dumps(ruleset)
    if ruleset_id is None:
        output = _run_gh(['api', '-X', 'POST', f'repos/{repo}/rulesets', '--input', '-'], input_text=body)
        created = json.loads(output)
        ruleset_id = int(created['id'])
        print(f'bootstrap: created ruleset {name!r} -> {ruleset_id}')
    else:
        _run_gh(['api', '-X', 'PUT', f'repos/{repo}/rulesets/{ruleset_id}', '--input', '-'], input_text=body)
        print(f'bootstrap: updated ruleset {name!r} -> {ruleset_id}')
    _run_gh(['variable', 'set', 'RULESET_ID', '--repo', repo, '--body', str(ruleset_id)])
    print('bootstrap: set repository variable RULESET_ID')
    return ruleset_id


def _copy_labels(repo: str, template_repo: str) -> None:
    existing = _run_gh(['api', '--paginate', f'repos/{repo}/labels', '--jq', '.[].name'])
    for label in existing.splitlines():
        if not label:
            continue
        encoded_label = urllib.parse.quote(label, safe='')
        _run_gh(['api', '-X', 'DELETE', f'repos/{repo}/labels/{encoded_label}'])

    labels_raw = _run_gh(['api', '--paginate', f'repos/{template_repo}/labels'])
    labels = json.loads(labels_raw)
    if not isinstance(labels, list):
        raise SystemExit('bootstrap: expected list from label template repository')
    for label in labels:
        if not isinstance(label, dict):
            continue
        name = str(label.get('name', ''))
        color = str(label.get('color', ''))
        description = str(label.get('description') or '')
        if not name or not color:
            continue
        _run_gh([
            'api',
            '-X',
            'POST',
            f'repos/{repo}/labels',
            '-f',
            f'name={name}',
            '-f',
            f'color={color}',
            '-f',
            f'description={description}',
        ])
    print(f'bootstrap: copied labels from {template_repo}')


def _apply_github_bootstrap(repo_slug: str, owner: str | None) -> None:
    if not (os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')):
        raise SystemExit(
            'bootstrap: GH_TOKEN or GITHUB_TOKEN is required for --github-only'
        )
    repo = _repo_full_name(owner, repo_slug)
    label_template = os.environ.get('LABEL_TEMPLATE_REPOSITORY') or TEMPLATE_LABEL_REPOSITORY
    _copy_labels(repo, label_template)
    _apply_ruleset(repo)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-name', default=_repo_name_from_env())
    parser.add_argument('--owner', default=_owner_from_env())
    parser.add_argument('--package-name')
    parser.add_argument('--files-only', action='store_true')
    parser.add_argument('--github-only', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    repo_slug = str(args.repo_name).strip()
    if not repo_slug:
        raise SystemExit('bootstrap: --repo-name cannot be empty')
    package_name = str(args.package_name or _slug_to_package(repo_slug)).strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', package_name):
        raise SystemExit(
            f'bootstrap: package name {package_name!r} is not a valid Python identifier'
        )

    if args.files_only and args.github_only:
        raise SystemExit('bootstrap: choose at most one of --files-only and --github-only')

    if not args.github_only:
        _apply_file_bootstrap(repo_slug, package_name, args.owner)
    if not args.files_only:
        _apply_github_bootstrap(repo_slug, args.owner)
    return 0


if __name__ == '__main__':
    sys.exit(main())
