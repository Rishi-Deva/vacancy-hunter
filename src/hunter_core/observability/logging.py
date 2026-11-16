"""Структурированные логи в стандартный вывод (TECHNICAL_SPEC.md, раздел 23).

Каждая запись — одна строка JSON: время (UTC), уровень, компонент, событие
и дополнительные поля. Ротацию файлов (5 по 10 МБ) выполняет Docker.
Известные значения секретов заменяются на «***» в любом месте записи.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
MASK = "***"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "component": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        elif record.exc_text:
            entry["error"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        return json.dumps(entry, ensure_ascii=False, default=str)


class SecretMaskFilter(logging.Filter):
    """Заменяет значения секретов в сообщении и полях записи."""

    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        # Длинные значения первыми, чтобы маскировать целиком. Короткие (< 4) не маскируем:
        # они дали бы ложные замены и не являются настоящими секретами.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        return text

    def _mask_value(self, value):
        """Маскирует строки на любой глубине: в словарях, списках, кортежах и множествах."""
        if isinstance(value, str):
            return self._mask(value)
        if isinstance(value, dict):
            return {self._mask_value(k): self._mask_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._mask_value(v) for v in value]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        # Прочие объекты попадут в JSON через str(), поэтому маскируем их строковый вид.
        return self._mask(str(value))

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            record.msg = self._mask(record.getMessage())
            record.args = None
            for key, value in list(record.__dict__.items()):
                if key not in _STANDARD_ATTRS and not key.startswith("_"):
                    setattr(record, key, self._mask_value(value))
            if record.exc_info:
                record.exc_text = self._mask(logging.Formatter().formatException(record.exc_info))
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = self._mask(record.exc_text)
            if record.stack_info:
                record.stack_info = self._mask(record.stack_info)
        return True


def setup_logging(level: str = "INFO", secrets: Iterable[str] = (), stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretMaskFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
