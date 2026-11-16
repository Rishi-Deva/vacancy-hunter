#!/bin/bash
# Первичная настройка PostgreSQL. Выполняется администратором один раз,
# при создании пустого тома базы (официальный образ postgres запускает
# файлы из /docker-entrypoint-initdb.d только в этот момент).
#
# Создаёт:
#   - пользователя миграций — владельца рабочей базы и её схемы;
#   - пользователя приложения — только SELECT, INSERT, UPDATE, DELETE;
#   - пользователя тестов — владельца только тестовой базы;
#   - рабочую и тестовую базы и расширение pg_trgm в каждой.
#
# Пароли передаются через переменные psql (:'имя'), поэтому кавычки
# и спецсимволы в них не ломают команды и не попадают в журнал.
set -euo pipefail

read_secret() { tr -d '\r\n' < "/run/secrets/$1"; }

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname postgres \
     -v db_name="$DB_NAME" -v admin_db="$POSTGRES_DB" \
     -v db_test_name="$DB_TEST_NAME" \
     -v migrator="$DB_MIGRATOR_USER" -v migrator_pw="$(read_secret db_migrator_password)" \
     -v app="$DB_APP_USER" -v app_pw="$(read_secret db_app_password)" \
     -v tester="$DB_TEST_USER" -v tester_pw="$(read_secret db_test_password)" <<'SQL'
CREATE ROLE :"migrator" LOGIN PASSWORD :'migrator_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"app" LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"tester" LOGIN PASSWORD :'tester_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

CREATE DATABASE :"db_name" OWNER :"migrator" ENCODING 'UTF8' TEMPLATE template0;
CREATE DATABASE :"db_test_name" OWNER :"tester" ENCODING 'UTF8' TEMPLATE template0;

-- Подключаться к базам могут только назначенные роли.
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
REVOKE ALL ON DATABASE :"db_test_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO :"app";
-- Служебные базы закрыты для всех, кроме администратора.
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE template1 FROM PUBLIC;
REVOKE ALL ON DATABASE :"admin_db" FROM PUBLIC;

-- Рабочая база
\connect :"db_name"
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER SCHEMA public OWNER TO :"migrator";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app";
-- Права на таблицы и последовательности, которые миграции создадут позже.
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app";

-- Тестовая база
\connect :"db_test_name"
CREATE EXTENSION IF NOT EXISTS pg_trgm;
ALTER SCHEMA public OWNER TO :"tester";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
