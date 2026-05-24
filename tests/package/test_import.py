from __future__ import annotations

import importlib
from importlib import util as importlib_util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_package_imports() -> None:
    assert importlib.import_module('furnace').__name__ == 'furnace'
    assert importlib.import_module('experiment_runner').__name__ == 'experiment_runner'
    legacy_init = REPO_ROOT / 'new_repository_template' / '__init__.py'
    spec = importlib_util.spec_from_file_location('new_repository_template', legacy_init)
    assert spec is not None
    assert spec.loader is not None
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.__name__ == 'new_repository_template'
