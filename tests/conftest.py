"""Общая подготовка тестов.

- Сетевые запросы из Python запрещены (TEST_PLAN.md, раздел 2): любая попытка
  открыть TCP-соединение роняет тест. Подключение к PostgreSQL идёт через
  библиотеку libpq и этим запретом не затрагивается.
- `config_dir` — копия настоящих файлов config/ во временной папке: тест может
  испортить файл, не трогая оригинал.
"""

from __future__ import annotations

import logging
import shutil
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


class NetworkAccessError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkAccessError(f"внешний сетевой запрос в тесте запрещён: {address!r}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect)


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, target)
    return target
