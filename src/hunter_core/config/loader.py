"""Чтение YAML-файлов и проверка по схеме.

Ошибка всегда называет файл и поле, например:
    scoring.yaml: adjustments.freshness.fresh: значение должно быть не больше 5
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

M = TypeVar("M", bound=BaseModel)


class StrictModel(BaseModel):
    """Базовая схема: неизвестные поля и неверные типы отклоняются."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldProblems(ValueError):
    """Сквозная проверка нашла проблемы. Каждая — (путь к полю внутри модели, текст).

    Использование в model_validator: собрать список и `raise FieldProblems(problems)`.
    В сообщении об ошибке путь дополняется местом модели в файле.
    """

    def __init__(self, problems: list[tuple[str, str]]):
        self.problems = problems
        super().__init__("; ".join(f"{path}: {msg}" for path, msg in problems))


def check_fields(problems: list[tuple[str, str]]) -> None:
    if problems:
        raise FieldProblems(problems)


class ConfigError(Exception):
    """Ошибка конфигурации. `problems` — строки вида «файл: поле: что не так»."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


_MESSAGES = {
    "extra_forbidden": "неизвестное поле",
    "missing": "обязательное поле отсутствует",
    "int_type": "ожидается целое число",
    "int_from_float": "ожидается целое число",
    "float_type": "ожидается число",
    "string_type": "ожидается текст",
    "bool_type": "ожидается true или false",
    "list_type": "ожидается список",
    "dict_type": "ожидается набор полей",
    "model_type": "ожидается набор полей",
    "date_type": "ожидается дата в виде ГГГГ-ММ-ДД",
    "less_than_equal": "значение должно быть не больше {le}",
    "greater_than_equal": "значение должно быть не меньше {ge}",
    "less_than": "значение должно быть меньше {lt}",
    "greater_than": "значение должно быть больше {gt}",
    "string_too_short": "текст не должен быть пустым",
    "too_short": "слишком мало элементов: нужно не меньше {min_length}",
    "string_pattern_mismatch": "значение не соответствует формату",
    "literal_error": "недопустимое значение; допустимы: {expected}",
}


def _translate(item: dict) -> str:
    template = _MESSAGES.get(item["type"])
    if template is None:
        message = item["msg"]
        return message.removeprefix("Value error, ")
    try:
        return template.format(**item.get("ctx", {}))
    except (KeyError, IndexError):
        return template


def _format_validation_error(file_label: str, err: ValidationError) -> list[str]:
    problems = []
    for item in err.errors():
        cross = item.get("ctx", {}).get("error")
        # Для полей вида «A | None» pydantic добавляет в путь имя варианта — оно владельцу не нужно.
        parts = [str(p) for p in item["loc"] if not str(p).startswith(("literal[", "list[", "function-"))]
        if isinstance(cross, FieldProblems):
            for path, msg in cross.problems:
                full = ".".join([*parts, path]) if path else ".".join(parts)
                problems.append(f"{file_label}: {full or '(весь файл)'}: {msg}")
            continue
        loc = ".".join(parts) or "(весь файл)"
        message = _translate(item)
        problems.append(f"{file_label}: {loc}: {message}")
    return problems


def load_yaml_model(path: Path, model: type[M]) -> M:
    """Прочитать один YAML-файл и проверить его схемой `model`."""
    label = path.name
    if not path.is_file():
        raise ConfigError([f"{label}: файл не найден"])
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"строка {mark.line + 1}" if mark is not None else "синтаксис"
        raise ConfigError([f"{label}: {where}: ошибка разметки YAML"]) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError([f"{label}: (весь файл): ожидается набор полей «имя: значение»"])
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(label, exc)) from None


def load_config_files(config_dir: Path, schemas: Mapping[str, type[BaseModel]]) -> dict[str, BaseModel]:
    """Загрузить все файлы. Ошибки собираются по всем файлам сразу."""
    loaded: dict[str, BaseModel] = {}
    problems: list[str] = []
    for filename, model in schemas.items():
        try:
            loaded[filename] = load_yaml_model(config_dir / filename, model)
        except ConfigError as exc:
            problems.extend(exc.problems)
    if problems:
        raise ConfigError(problems)
    return loaded
