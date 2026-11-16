"""Разделение ролей базы (решение владельца от 13.11.2026, вариант Б).

- В окружении контейнера app нет данных администратора и пользователя миграций.
- Пользователь приложения имеет только SELECT, INSERT, UPDATE, DELETE и не может
  создавать, изменять и удалять таблицы.

Проверки идут на рабочей базе под пользователем приложения. Любая попытка изменения
выполняется в транзакции, которая откатывается, поэтому данные не меняются даже
если бы право неожиданно оказалось.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from tests.integration.test_schema import EXPECTED_TABLES

pytestmark = pytest.mark.integration

FORBIDDEN_ENV = (
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_ADMIN_USER", "POSTGRES_ADMIN_PASSWORD",
    "DB_MIGRATOR_USER", "DB_MIGRATOR_PASSWORD",
)


def denied(engine, sql: str) -> bool:
    """True, если база отказала в правах. Транзакция всегда откатывается."""
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            conn.execute(text(sql))
        except ProgrammingError as exc:
            tx.rollback()
            return getattr(exc.orig, "sqlstate", None) == "42501"  # insufficient_privilege
        tx.rollback()
        return False


def test_app_environment_has_no_admin_or_migrator_credentials():
    present = [name for name in FORBIDDEN_ENV if name in os.environ]
    assert present == [], f"в окружении app не должно быть: {present}"


def test_app_role_has_no_elevated_attributes(app_engine):
    with app_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
            " FROM pg_roles WHERE rolname = current_user"
        )).one()
    assert tuple(row) == (False, False, False, False, False)


def test_app_role_does_not_own_tables_or_schema(app_engine):
    with app_engine.connect() as conn:
        owners = set(conn.execute(text(
            "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
        schema_owner = conn.execute(text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'"
        )).scalar()
        me = conn.execute(text("SELECT current_user")).scalar()
    assert me not in owners
    assert schema_owner != me
    assert len(owners) == 1  # все таблицы принадлежат пользователю миграций


def test_app_role_has_exactly_dml_privileges_on_all_tables(app_engine):
    with app_engine.connect() as conn:
        for table in sorted(EXPECTED_TABLES):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert conn.execute(
                    text("SELECT has_table_privilege(current_user, :t, :p)"), {"t": f"public.{table}", "p": privilege}
                ).scalar(), f"{table}: нет права {privilege}"
            for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not conn.execute(
                    text("SELECT has_table_privilege(current_user, :t, :p)"), {"t": f"public.{table}", "p": privilege}
                ).scalar(), f"{table}: лишнее право {privilege}"


def test_app_role_can_use_sequences_for_inserts(app_engine):
    with app_engine.connect() as conn:
        sequences = list(conn.execute(text(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
        )).scalars())
        assert sequences
        for seq in sequences:
            assert conn.execute(
                text("SELECT has_sequence_privilege(current_user, :s, 'USAGE')"), {"s": f"public.{seq}"}
            ).scalar(), seq


def test_app_role_cannot_create_table(app_engine):
    assert denied(app_engine, "CREATE TABLE public.intruder (id int)")


def test_app_role_cannot_drop_table(app_engine):
    assert denied(app_engine, "DROP TABLE public.app_state")


def test_app_role_cannot_alter_table(app_engine):
    assert denied(app_engine, "ALTER TABLE public.app_state ADD COLUMN intruder int")


def test_app_role_cannot_truncate(app_engine):
    assert denied(app_engine, "TRUNCATE public.app_state")


def test_app_role_cannot_create_schema_or_index(app_engine):
    assert denied(app_engine, "CREATE SCHEMA intruder")
    assert denied(app_engine, "CREATE INDEX ix_intruder ON public.app_state (key)")


def _cannot_connect(prefix: str, database: str) -> bool:
    """True, если роль `prefix` не может подключиться к базе `database`."""
    from sqlalchemy.exc import OperationalError

    from hunter_core.config import DatabaseSettings
    from hunter_core.storage import create_db_engine

    settings = DatabaseSettings(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")), name=database,
        user=os.environ[f"{prefix}_USER"], password=os.environ[f"{prefix}_PASSWORD"],
    )
    engine = create_db_engine(settings, application_name="pytest-isolation")
    try:
        with engine.connect():
            return False
    except OperationalError as exc:
        return "permission denied" in str(exc.orig).lower()
    finally:
        engine.dispose()


@pytest.mark.parametrize("prefix,database_var", [
    ("DB_APP", "DB_TEST_NAME"),   # приложение не видит тестовую базу
    ("DB_TEST", "DB_NAME"),       # тесты не видят рабочую базу
])
def test_roles_are_isolated_between_databases(prefix, database_var):
    assert _cannot_connect(prefix, os.environ[database_var])


@pytest.mark.parametrize("prefix", ["DB_APP", "DB_TEST"])
@pytest.mark.parametrize("database", ["postgres", "template1"])
def test_roles_cannot_connect_to_service_databases(prefix, database):
    assert _cannot_connect(prefix, database)
