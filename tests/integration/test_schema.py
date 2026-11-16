"""DB-1 и DB-8: миграции и состав базы (TEST_PLAN.md, раздел 4.1)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from hunter_core.storage.metadata import metadata
from tests.integration.conftest import alembic_config, reset_schema
import vacancy_hunter.storage  # noqa: F401 — регистрирует все таблицы

pytestmark = pytest.mark.integration

# Ровно 20 прикладных таблиц из DATABASE_SCHEMA.md. Служебная таблица Alembic не учитывается.
EXPECTED_TABLES = {
    "source_state", "fetch_runs", "raw_items", "source_checks",
    "vacancies", "vacancy_sources",
    "profile_versions", "scoring_versions",
    "extractions", "scores", "score_overrides",
    "reports", "report_items", "outbox",
    "feedback_events", "cover_letters",
    "llm_budget", "llm_ledger",
    "job_runs", "app_state",
}
SERVICE_TABLES = {"alembic_version"}

# Поля всех 20 таблиц, выписанные из DATABASE_SCHEMA.md (разделы 3–10), независимо от кода.
EXPECTED_COLUMNS = {
    "source_state": {"code", "kind", "mode", "mode_reason", "health", "cursor", "last_run_at", "last_success_at",
                     "consecutive_failures", "last_error", "check_started_at", "check_extensions",
                     "failed_rechecks", "posts_total", "vacancies_real", "passed_profile", "llm_sampled",
                     "relevance_pct", "last_check_at", "created_at", "updated_at"},
    "fetch_runs": {"id", "source_code", "started_at", "finished_at", "status", "items_seen", "items_new",
                   "cursor_before", "cursor_after", "http_requests", "error_code", "error_text"},
    "raw_items": {"id", "source_code", "external_id", "fetch_run_id", "url", "payload", "published_at",
                  "fetched_at"},
    "source_checks": {"id", "source_code", "kind", "period_start", "period_end", "posts_total", "vacancies_real",
                      "passed_profile", "llm_sampled", "relevance_pct", "llm_cost_usd", "result", "decided_by",
                      "note", "created_at"},
    "vacancies": {"id", "status", "status_reason", "description_requested_at", "title", "company_name",
                  "company_norm", "title_norm", "description_text", "description_hash", "has_description",
                  "canonical_url", "salary_from", "salary_to", "salary_currency", "salary_period", "location_raw",
                  "country_code", "work_format_src", "experience_src", "offer_type", "published_at",
                  "first_seen_at", "last_seen_at", "primary_source_code", "duplicate_of", "quiet_only",
                  "user_state", "user_state_at", "first_full_scored_at", "reported_at", "title_only_reported_at",
                  "alive_checked_at", "closed_at", "llm_attempts", "lease_until", "text_purged_at", "created_at",
                  "updated_at"},
    "vacancy_sources": {"id", "vacancy_id", "source_code", "external_id", "url", "raw_item_id", "first_seen_at"},
    "profile_versions": {"id", "version_no", "content_hash", "profile", "skills_dictionary", "source_note",
                         "is_current", "created_at"},
    "scoring_versions": {"id", "version_no", "content_hash", "rules", "is_current", "created_at"},
    "extractions": {"id", "text_hash", "model", "prompt_version", "status", "parts_total", "parts_done", "parts",
                    "chars_total", "chars_analyzed", "facts", "quote_check", "flags", "ledger_id", "created_at"},
    "scores": {"id", "vacancy_id", "kind", "state", "extraction_id", "profile_version_id", "scoring_version_id",
               "gate_result", "gates", "unknowns", "breakdown", "total", "category", "explanation",
               "error_text", "superseded_by", "created_at", "superseded_at"},
    "score_overrides": {"id", "vacancy_id", "kind", "gate_code", "new_total", "comment", "based_on_score_id",
                        "is_active", "created_at", "revoked_at"},
    "reports": {"id", "kind", "report_date", "search_query", "status", "stats", "created_at", "completed_at"},
    "report_items": {"id", "report_id", "vacancy_id", "block", "position", "score_id", "total_snapshot"},
    "outbox": {"id", "dedup_key", "chat_id", "kind", "report_item_id", "payload", "state", "attempts",
               "next_attempt_at", "sending_started_at", "telegram_message_id", "error_text", "created_at",
               "sent_at"},
    "feedback_events": {"id", "vacancy_id", "event", "reason_code", "comment", "score_id", "created_at"},
    "cover_letters": {"id", "vacancy_id", "kind", "question", "version_no", "text", "model", "prompt_version",
                      "profile_version_id", "ledger_id", "created_at"},
    "llm_budget": {"month", "limit_usd", "spent_usd", "reserved_usd", "letters_reserve_usd",
                   "daily_extraction_limit_usd", "quiet_limit_usd", "quiet_spent_usd", "warn1_usd", "warn2_usd",
                   "warn1_sent", "warn2_sent", "updated_at"},
    "llm_ledger": {"id", "month", "purpose", "model", "vacancy_id", "attempt_no", "part_no", "state",
                   "reserved_usd", "actual_usd", "tokens_in", "tokens_cache_write", "tokens_cache_read",
                   "tokens_out", "stop_reason", "started_at", "finished_at", "error_text"},
    "job_runs": {"id", "job_name", "status", "started_at", "finished_at", "lease_until", "stats", "error_text"},
    "app_state": {"key", "value", "updated_at"},
}


def app_tables(connection) -> set[str]:
    return set(inspect(connection).get_table_names(schema="public")) - SERVICE_TABLES


def test_db1_migrations_on_empty_database_and_repeat(test_engine, test_settings):
    reset_schema(test_engine)
    with test_engine.connect() as conn:
        assert app_tables(conn) == set()
    cfg = alembic_config(test_settings.url())
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # повторный запуск — без ошибок и без изменений
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == head
        assert app_tables(conn) == EXPECTED_TABLES


def test_db8_exactly_twenty_application_tables(test_engine):
    with test_engine.connect() as conn:
        tables = app_tables(conn)
    assert len(EXPECTED_TABLES) == 20
    assert tables == EXPECTED_TABLES
    assert set(metadata.tables) == EXPECTED_TABLES


def test_db8_columns_match_database_schema_document(test_engine):
    with test_engine.connect() as conn:
        insp = inspect(conn)
        actual = {t: {c["name"] for c in insp.get_columns(t)} for t in EXPECTED_TABLES}
    assert set(EXPECTED_COLUMNS) == EXPECTED_TABLES
    for table in sorted(EXPECTED_TABLES):
        assert actual[table] == EXPECTED_COLUMNS[table], table
    # описание таблиц в коде совпадает с базой
    described = {name: {c.name for c in table.columns} for name, table in metadata.tables.items()}
    assert actual == described


def test_db8_trigram_extension_and_index_present(test_engine):
    with test_engine.connect() as conn:
        assert conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar() == 1
        indexdef = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_vacancies_title_norm_trgm'")
        ).scalar()
    assert "gin" in indexdef and "gin_trgm_ops" in indexdef
