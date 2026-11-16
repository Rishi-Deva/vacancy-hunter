"""Секреты из переменных окружения.

Значения секретов никогда не попадают в текст ошибок и в журнал:
сообщается только имя отсутствующей переменной.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy.engine import URL


class SecretsError(Exception):
    """Не заданы обязательные переменные окружения."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Не заданы обязательные переменные окружения: " + ", ".join(missing))


def missing_env(env: Mapping[str, str], names: Iterable[str]) -> list[str]:
    """Имена переменных, которые отсутствуют или пусты."""
    return [name for name in names if not env.get(name, "").strip()]


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    name: str
    user: str
    password: str = field(repr=False)

    def url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
        )


def read_database_settings(
    env: Mapping[str, str],
    *,
    user_var: str,
    password_var: str,
    name_var: str = "DB_NAME",
    host_var: str = "DB_HOST",
    port_var: str = "DB_PORT",
) -> DatabaseSettings:
    """Прочитать параметры подключения. Порт по умолчанию 5432."""
    required = [host_var, name_var, user_var, password_var]
    missing = missing_env(env, required)
    port_raw = env.get(port_var, "").strip() or "5432"
    if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
        missing.append(f"{port_var} (должно быть число от 1 до 65535)")
    if missing:
        raise SecretsError(missing)
    return DatabaseSettings(
        host=env[host_var].strip(),
        port=int(port_raw),
        name=env[name_var].strip(),
        user=env[user_var].strip(),
        password=env[password_var],
    )
