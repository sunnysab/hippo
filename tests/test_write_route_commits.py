"""Every write route must commit what it writes.

``get_storage`` only closes the request handle, and closing rolls back
(``PostgresStorage.close``). A route that calls a repository write without
opening ``storage.transaction()`` therefore answers 200 and keeps nothing —
which is how the read/star toggles quietly lost every toggle.

The check is structural rather than a list of known-good routes: it reads the
SQL inside each repository method to learn which methods write, follows
``storage.<repo>.<method>`` calls out of each route, and asks whether that route
opens a transaction. A new write route is covered the moment it is written and
nothing has to be maintained by hand.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTERS = REPO_ROOT / 'hippo' / 'api' / 'routers'
STORAGE = REPO_ROOT / 'hippo' / 'storage.py'

#: Statements that change a row. ``UPDATE`` needs a following word so an
#: ``updated_at`` column in a SELECT does not count as a write.
WRITE_SQL = re.compile(r'\b(?:INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|TRUNCATE)\b', re.IGNORECASE)

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


def _bound_repositories() -> dict[str, str]:
    """Map ``storage.<attribute>`` to the repository class bound to it."""
    tree = ast.parse(STORAGE.read_text(encoding='utf-8'))
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == '_bind_repositories'):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            target, value = statement.targets[0], statement.value
            if isinstance(target, ast.Attribute) and isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                bound[target.attr] = value.func.id
    return bound


def _writing_methods() -> dict[str, set[str]]:
    """Map each repository class to the methods whose SQL changes a row."""
    writing: dict[str, set[str]] = {}
    for path in (REPO_ROOT / 'hippo').rglob('*.py'):
        if 'api' in path.parts or 'tests' in path.parts:
            continue
        source = path.read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if WRITE_SQL.search(ast.get_source_segment(source, method) or ''):
                    writing.setdefault(node.name, set()).add(method.name)
    return writing


BOUND = _bound_repositories()
WRITING = _writing_methods()


def _repo_writes_in(function: ast.AST) -> set[str]:
    """``storage.<repo>.<write method>`` calls made anywhere in one route."""
    found: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if not isinstance(owner, ast.Attribute):
            continue
        if node.func.attr in WRITING.get(BOUND.get(owner.attr, ''), set()):
            found.add(f'storage.{owner.attr}.{node.func.attr}')
    return found


def _commits(source: str, function: ast.AST) -> bool:
    body = ast.get_source_segment(source, function) or ''
    return 'transaction()' in body or '.commit()' in body


def _write_routes() -> list[tuple[str, ast.AST, str]]:
    """Every route that is not read-only, with the source it came from."""
    routes: list[tuple[str, ast.AST, str]] = []
    for path in sorted(ROUTERS.glob('*.py')):
        source = path.read_text(encoding='utf-8')
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods = {
                decorator.func.attr.upper()
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            }
            if not methods or methods <= SAFE_METHODS:
                continue
            routes.append((f'{path.stem}:{node.name}', node, source))
    return routes


ROUTES = {label: (function, source) for label, function, source in _write_routes()}


@pytest.mark.parametrize('label', sorted(ROUTES))
def test_a_write_route_commits_its_write(label: str) -> None:
    function, source = ROUTES[label]
    if not _repo_writes_in(function):
        return
    assert _commits(source, function), (
        f'{label} writes through the repository but never opens storage.transaction(): '
        'get_storage only closes the handle, and closing rolls the write back'
    )


def test_the_scanner_finds_the_write_it_was_built_for() -> None:
    """Guard the guard: a scanner that saw no writes would pass everything."""
    assert 'ArticleReadRepository' in BOUND.values()
    assert 'set_read' in WRITING['ArticleReadRepository']
    assert 'set_starred' in WRITING['ArticleStarRepository']

    reads, reads_source = ROUTES['article:set_article_read']
    assert 'storage.reads.set_read' in _repo_writes_in(reads)
    assert _commits(reads_source, reads)
