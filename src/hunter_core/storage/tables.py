"""Общие таблицы (DATABASE_SCHEMA.md, разделы 3, 7.3, 9, 10).

Здесь нет понятий конкретного продукта. Предметные столбцы и перечни
(например, ссылки на записи продукта или имена задач) добавляет пакет продукта.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from hunter_core.storage.metadata import Money, enum_check, id_column, metadata, ts

# 3.1
source_state = Table(
    "source_state",
    metadata,
    Column("code", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("mode", Text, nullable=False),
    Column("mode_reason", Text),
    Column("health", Text, nullable=False, server_default="ok"),
    Column("cursor", JSONB),
    ts("last_run_at"),
    ts("last_success_at"),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("last_error", Text),
    ts("check_started_at"),
    Column("check_extensions", Integer, nullable=False, server_default="0"),
    Column("failed_rechecks", Integer, nullable=False, server_default="0"),
    Column("posts_total", Integer, nullable=False, server_default="0"),
    Column("llm_sampled", Integer, nullable=False, server_default="0"),
    Column("relevance_pct", Numeric(5, 2)),
    ts("last_check_at"),
    ts("created_at", nullable=False, default_now=True),
    ts("updated_at", nullable=False, default_now=True),
    enum_check("kind", ["telegram", "rss", "api"]),
    enum_check("mode", ["active", "quiet", "waiting", "disabled"]),
    enum_check("mode_reason", ["initial_check", "recheck", "owner", "blocked", "failed_check"]),
    enum_check("health", ["ok", "degraded", "blocked"]),
    CheckConstraint("check_extensions BETWEEN 0 AND 2", name="check_extensions_range"),
    CheckConstraint(
        "consecutive_failures >= 0 AND failed_rechecks >= 0 AND posts_total >= 0 AND llm_sampled >= 0",
        name="counters_non_negative",
    ),
)

# 3.2
fetch_runs = Table(
    "fetch_runs",
    metadata,
    id_column(),
    Column("source_code", Text, ForeignKey("source_state.code"), nullable=False),
    ts("started_at", default_now=True),
    ts("finished_at"),
    Column("status", Text, nullable=False),
    Column("items_seen", Integer),
    Column("items_new", Integer),
    Column("cursor_before", JSONB),
    Column("cursor_after", JSONB),
    Column("http_requests", Integer),
    Column("error_code", Text),
    Column("error_text", Text),
    enum_check("status", ["running", "success", "partial", "failed", "abandoned"]),
    Index(None, "source_code", "started_at"),
)

# 3.3
raw_items = Table(
    "raw_items",
    metadata,
    id_column(),
    Column("source_code", Text, ForeignKey("source_state.code"), nullable=False),
    Column("external_id", Text, nullable=False),
    Column("fetch_run_id", BigInteger, ForeignKey("fetch_runs.id", ondelete="SET NULL")),
    Column("url", Text),
    Column("payload", JSONB, nullable=False),
    ts("published_at"),
    ts("fetched_at", nullable=False, default_now=True),
    UniqueConstraint("source_code", "external_id"),
    Index(None, "fetched_at"),
)

# 7.3 (перечень видов сообщений и ссылку на элемент отчёта добавляет пакет продукта)
outbox = Table(
    "outbox",
    metadata,
    id_column(),
    Column("dedup_key", Text, nullable=False, unique=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", Text, nullable=False, server_default="pending"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    ts("next_attempt_at"),
    ts("sending_started_at"),
    Column("telegram_message_id", BigInteger),
    Column("error_text", Text),
    ts("created_at", default_now=True),
    ts("sent_at"),
    enum_check("state", ["pending", "sending", "sent", "failed", "unconfirmed"]),
    CheckConstraint("attempts BETWEEN 0 AND 5", name="attempts_range"),
    Index(None, "state", "next_attempt_at"),
)

# 9.1
llm_budget = Table(
    "llm_budget",
    metadata,
    Column("month", Date, primary_key=True),
    Column("limit_usd", Money, nullable=False),
    Column("spent_usd", Money, nullable=False, server_default="0"),
    Column("reserved_usd", Money, nullable=False, server_default="0"),
    Column("letters_reserve_usd", Money, nullable=False),
    Column("daily_extraction_limit_usd", Money, nullable=False),
    Column("quiet_limit_usd", Money, nullable=False),
    Column("quiet_spent_usd", Money, nullable=False, server_default="0"),
    Column("warn1_usd", Money, nullable=False),
    Column("warn2_usd", Money, nullable=False),
    Column("warn1_sent", Boolean, nullable=False, server_default="false"),
    Column("warn2_sent", Boolean, nullable=False, server_default="false"),
    ts("updated_at", nullable=False, default_now=True),
    # Главная страховка бюджета: база отклоняет запись, где расход плюс резерв больше лимита.
    CheckConstraint("spent_usd + reserved_usd <= limit_usd", name="spent_plus_reserved_within_limit"),
    CheckConstraint(
        "limit_usd >= 0 AND spent_usd >= 0 AND reserved_usd >= 0 AND letters_reserve_usd >= 0"
        " AND daily_extraction_limit_usd >= 0 AND quiet_limit_usd >= 0 AND quiet_spent_usd >= 0"
        " AND warn1_usd >= 0 AND warn2_usd >= 0",
        name="amounts_non_negative",
    ),
    CheckConstraint("date_trunc('month', month) = month", name="month_is_first_day"),
)

# 9.2 (ссылку на запись продукта добавляет пакет продукта)
llm_ledger = Table(
    "llm_ledger",
    metadata,
    id_column(),
    Column("month", Date, ForeignKey("llm_budget.month"), nullable=False),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("part_no", Integer),
    Column("state", Text, nullable=False),
    Column("reserved_usd", Money, nullable=False),
    Column("actual_usd", Money),
    Column("tokens_in", Integer),
    Column("tokens_cache_write", Integer),
    Column("tokens_cache_read", Integer),
    Column("tokens_out", Integer),
    Column("stop_reason", Text),
    ts("started_at", default_now=True),
    ts("finished_at"),
    Column("error_text", Text),
    enum_check("purpose", ["extraction", "quiet_check", "letter", "answer"]),
    enum_check("state", ["reserved", "settled", "released", "assumed_spent"]),
    CheckConstraint("reserved_usd >= 0 AND (actual_usd IS NULL OR actual_usd >= 0)", name="amounts_non_negative"),
    Index(None, "month", "started_at"),
    Index("ix_llm_ledger_reserved", "state", postgresql_where=text("state = 'reserved'")),
)

# 10.1 (перечень имён задач добавляет пакет продукта)
job_runs = Table(
    "job_runs",
    metadata,
    id_column(),
    Column("job_name", Text, nullable=False),
    Column("status", Text, nullable=False),
    ts("started_at", default_now=True),
    ts("finished_at"),
    ts("lease_until"),
    Column("stats", JSONB),
    Column("error_text", Text),
    enum_check("status", ["running", "success", "failed", "abandoned"]),
    # Блокировка от параллельного запуска: не более одной работающей строки на задачу.
    Index(
        "uq_job_runs_one_running",
        "job_name",
        unique=True,
        postgresql_where=text("status = 'running'"),
    ),
)

# 10.2
app_state = Table(
    "app_state",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSONB, nullable=False),
    ts("updated_at", nullable=False, default_now=True),
)

CORE_TABLES = (source_state, fetch_runs, raw_items, outbox, llm_budget, llm_ledger, job_runs, app_state)
