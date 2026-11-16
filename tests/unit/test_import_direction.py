"""DB-6: направление импортов (ARCHITECTURE.md, раздел 18).

vacancy_hunter может импортировать hunter_core, обратное запрещено.
Проверка разбирает исходники без их выполнения.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"


def forbidden_imports(source: str, forbidden: str = "vacancy_hunter") -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        found += [n for n in names if n == forbidden or n.startswith(forbidden + ".")]
    # importlib.import_module("vacancy_hunter...") — тоже импорт
    found += [
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None)) in {"import_module", "__import__"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value).startswith(forbidden)
    ]
    return found


def test_db6_hunter_core_does_not_import_vacancy_hunter():
    files = sorted((SRC / "hunter_core").rglob("*.py"))
    assert files, "в hunter_core нет файлов — путь к исходникам неверен"
    violations = {
        str(path.relative_to(SRC)): bad
        for path in files
        if (bad := forbidden_imports(path.read_text(encoding="utf-8")))
    }
    assert violations == {}, f"hunter_core импортирует vacancy_hunter: {violations}"


def test_db6_checker_catches_every_import_form():
    # Убеждаемся, что проверка действительно падает на запрещённом импорте.
    assert forbidden_imports("import vacancy_hunter") == ["vacancy_hunter"]
    assert forbidden_imports("from vacancy_hunter.storage import tables") == ["vacancy_hunter.storage"]
    assert forbidden_imports("import os, vacancy_hunter.config as c") == ["vacancy_hunter.config"]
    assert forbidden_imports("importlib.import_module('vacancy_hunter.bot')") == ["vacancy_hunter.bot"]
    assert forbidden_imports("from hunter_core.config import loader") == []
    assert forbidden_imports("import vacancy_hunterx") == []
