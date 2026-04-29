from __future__ import annotations

import ast


def _is_call_free(node: ast.AST | None) -> bool:
    if node is None:
        return True
    return not any(isinstance(n, ast.Call) for n in ast.walk(node))


def strip_to_definitions(source: str) -> ast.Module:
    """Return an AST module with only safe top-level forms retained:
    Imports, function/class defs, and Assigns whose right-hand side and targets
    contain no Call nodes (so module-level constants like TRAIN_SPLIT = 50 survive,
    but bottom-block runtime calls are dropped).
    """
    tree = ast.parse(source)
    kept: list[ast.stmt] = []
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                          ast.AsyncFunctionDef, ast.ClassDef)):
            kept.append(n)
        elif isinstance(n, ast.Assign):
            if all(_is_call_free(t) for t in n.targets) and _is_call_free(n.value):
                kept.append(n)
        elif isinstance(n, ast.AnnAssign):
            if _is_call_free(n.annotation) and _is_call_free(n.value):
                kept.append(n)
    tree.body = kept
    ast.fix_missing_locations(tree)
    return tree
