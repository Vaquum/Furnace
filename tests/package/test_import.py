from __future__ import annotations

import importlib


def test_package_imports() -> None:
    assert importlib.import_module('new_repository_template').__name__ == 'new_repository_template'
