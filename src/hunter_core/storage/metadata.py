"""Общий набор описаний таблиц и типовые столбцы."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Identity, MetaData, Numeric, func

# Предсказуемые имена ограничений и индексов: миграции не зависят от случайных имён.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

# Деньги: точное десятичное, 6 знаков после запятой, доллары США (DATABASE_SCHEMA.md, раздел 1).
Money = Numeric(12, 6)


def id_column() -> Column:
    """Первичный ключ: 64-битное целое, генерируется базой."""
    return Column("id", BigInteger, Identity(always=False), primary_key=True)


def ts(name: str, *, nullable: bool = True, default_now: bool = False) -> Column:
    """Время с часовым поясом (хранится в UTC)."""
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=func.now() if default_now else None,
    )


def enum_check(column: str, values: Iterable[str], name: str | None = None) -> CheckConstraint:
    """Перечисление: текстовое поле с ограничением допустимых значений."""
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name or column)
