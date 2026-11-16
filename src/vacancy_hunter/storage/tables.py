"""Таблицы вакансий (DATABASE_SCHEMA.md, разделы 3.4, 4–8) и предметные дополнения общих таблиц.

Всего в схеме 20 таблиц: 8 общих из hunter_core и 12 здесь.
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
from hunter_core.storage.tables import CORE_TABLES, job_runs, llm_ledger, outbox, source_state

VACANCY_STATUSES = [
    "new", "awaiting_description", "duplicate", "rejected_rules", "pending_llm", "llm_failed",
    "extracted", "rejected_gate", "scored", "reported", "closed",
]
OFFER_TYPES = ["job", "internship", "project", "unknown"]
USER_STATES = ["none", "interested", "applied", "rejected"]
JOB_NAMES = ["collect", "process", "report", "recalc", "source_review", "cleanup", "backup"]
OUTBOX_KINDS = ["report_header", "vacancy_card", "alert", "letter", "backup", "system"]

# ---------------------------------------------------------------- 3.4
source_checks = Table(
    "source_checks",
    metadata,
    id_column(),
    Column("source_code", Text, ForeignKey("source_state.code"), nullable=False),
    Column("kind", Text, nullable=False),
    ts("period_start", nullable=False),
    ts("period_end", nullable=False),
    Column("posts_total", Integer, nullable=False),
    Column("vacancies_real", Integer, nullable=False),
    Column("passed_profile", Integer, nullable=False),
    Column("llm_sampled", Integer, nullable=False),
    Column("relevance_pct", Numeric(5, 2)),
    Column("llm_cost_usd", Money),
    Column("result", Text, nullable=False),
    Column("decided_by", Text, nullable=False),
    Column("note", Text),
    ts("created_at", nullable=False, default_now=True),
    enum_check("kind", ["initial", "recheck", "monthly"]),
    enum_check("result", ["included", "kept", "extended", "sent_to_recheck", "rejected"]),
    enum_check("decided_by", ["auto", "owner"]),
    Index(None, "source_code", "created_at"),
)

# ---------------------------------------------------------------- 4.1
vacancies = Table(
    "vacancies",
    metadata,
    id_column(),
    Column("status", Text, nullable=False, server_default="new"),
    Column("status_reason", Text),
    ts("description_requested_at"),
    Column("title", Text, nullable=False),
    Column("company_name", Text),
    Column("company_norm", Text),
    Column("title_norm", Text),
    Column("description_text", Text),
    Column("description_hash", Text),
    Column("has_description", Boolean, nullable=False),
    Column("canonical_url", Text),
    Column("salary_from", Integer),
    Column("salary_to", Integer),
    Column("salary_currency", Text),
    Column("salary_period", Text),
    Column("location_raw", Text),
    Column("country_code", Text),
    Column("work_format_src", Text),
    Column("experience_src", Text),
    Column("offer_type", Text, nullable=False, server_default="unknown"),
    ts("published_at"),
    ts("first_seen_at", nullable=False, default_now=True),
    ts("last_seen_at", nullable=False, default_now=True),
    Column("primary_source_code", Text, ForeignKey("source_state.code")),
    Column("duplicate_of", BigInteger, ForeignKey("vacancies.id", ondelete="SET NULL")),
    Column("quiet_only", Boolean, nullable=False, server_default="false"),
    Column("user_state", Text, nullable=False, server_default="none"),
    ts("user_state_at"),
    ts("first_full_scored_at"),
    ts("reported_at"),
    ts("title_only_reported_at"),
    ts("alive_checked_at"),
    ts("closed_at"),
    Column("llm_attempts", Integer, nullable=False, server_default="0"),
    ts("lease_until"),
    ts("text_purged_at"),
    ts("created_at", nullable=False, default_now=True),
    ts("updated_at", nullable=False, default_now=True),
    enum_check("status", VACANCY_STATUSES),
    enum_check("offer_type", OFFER_TYPES),
    enum_check("user_state", USER_STATES),
    CheckConstraint("llm_attempts BETWEEN 0 AND 3", name="llm_attempts_range"),
    CheckConstraint("country_code IS NULL OR country_code ~ '^[A-Z]{2}$'", name="country_code_format"),
    CheckConstraint("duplicate_of IS NULL OR duplicate_of <> id", name="not_duplicate_of_self"),
    Index(None, "status"),
    Index(None, "canonical_url"),
    Index(None, "description_hash"),
    Index(None, "company_norm", "title_norm"),
    Index(
        "ix_vacancies_title_norm_trgm",
        "title_norm",
        postgresql_using="gin",
        postgresql_ops={"title_norm": "gin_trgm_ops"},
    ),
    Index(None, "first_seen_at"),
    Index(None, "offer_type", "first_seen_at"),
    Index("ix_vacancies_user_state_marked", "user_state", postgresql_where=text("user_state <> 'none'")),
)

# ---------------------------------------------------------------- 4.2
vacancy_sources = Table(
    "vacancy_sources",
    metadata,
    id_column(),
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
    Column("source_code", Text, ForeignKey("source_state.code"), nullable=False),
    Column("external_id", Text, nullable=False),
    Column("url", Text),
    Column("raw_item_id", BigInteger, ForeignKey("raw_items.id", ondelete="SET NULL")),
    ts("first_seen_at", nullable=False, default_now=True),
    UniqueConstraint("source_code", "external_id"),
    Index(None, "vacancy_id"),
)

# ---------------------------------------------------------------- 5.1, 5.2
profile_versions = Table(
    "profile_versions",
    metadata,
    id_column(),
    Column("version_no", Integer, nullable=False, unique=True),
    Column("content_hash", Text, nullable=False, unique=True),
    Column("profile", JSONB, nullable=False),
    Column("skills_dictionary", JSONB, nullable=False),
    Column("source_note", Text),
    Column("is_current", Boolean, nullable=False, server_default="false"),
    ts("created_at", nullable=False, default_now=True),
    Index("uq_profile_versions_one_current", "is_current", unique=True, postgresql_where=text("is_current")),
)

scoring_versions = Table(
    "scoring_versions",
    metadata,
    id_column(),
    Column("version_no", Integer, nullable=False, unique=True),
    Column("content_hash", Text, nullable=False, unique=True),
    Column("rules", JSONB, nullable=False),
    Column("is_current", Boolean, nullable=False, server_default="false"),
    ts("created_at", nullable=False, default_now=True),
    Index("uq_scoring_versions_one_current", "is_current", unique=True, postgresql_where=text("is_current")),
)

# ---------------------------------------------------------------- 6.1
extractions = Table(
    "extractions",
    metadata,
    id_column(),
    Column("text_hash", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("parts_total", Integer, nullable=False),
    Column("parts_done", Integer, nullable=False),
    Column("parts", JSONB),
    Column("chars_total", Integer, nullable=False),
    Column("chars_analyzed", Integer, nullable=False),
    Column("facts", JSONB),
    Column("quote_check", JSONB),
    Column("flags", JSONB),
    Column("ledger_id", BigInteger, ForeignKey("llm_ledger.id", ondelete="SET NULL")),
    ts("created_at", nullable=False, default_now=True),
    enum_check("status", ["ok", "partial", "invalid"]),
    CheckConstraint("parts_total BETWEEN 1 AND 4 AND parts_done BETWEEN 0 AND parts_total", name="parts_range"),
    CheckConstraint("chars_analyzed BETWEEN 0 AND chars_total", name="chars_range"),
    UniqueConstraint("text_hash", "model", "prompt_version"),
)

# ---------------------------------------------------------------- 6.2
scores = Table(
    "scores",
    metadata,
    id_column(),
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="current"),
    Column("extraction_id", BigInteger, ForeignKey("extractions.id", ondelete="SET NULL")),
    Column("profile_version_id", BigInteger, ForeignKey("profile_versions.id"), nullable=False),
    Column("scoring_version_id", BigInteger, ForeignKey("scoring_versions.id"), nullable=False),
    Column("gate_result", Text, nullable=False),
    Column("gates", JSONB, nullable=False),
    Column("unknowns", JSONB),
    Column("breakdown", JSONB),
    Column("total", Integer),
    Column("category", Text),
    Column("explanation", JSONB),
    Column("error_text", Text),
    Column("superseded_by", BigInteger, ForeignKey("scores.id", ondelete="SET NULL")),
    ts("created_at", default_now=True),
    ts("superseded_at"),
    enum_check("kind", ["full", "preliminary"]),
    enum_check("state", ["current", "pending_recalc", "recalc_failed", "outdated"]),
    enum_check("gate_result", ["passed", "rejected"]),
    enum_check("category", ["excellent", "good", "consider", "hidden"]),
    CheckConstraint("total IS NULL OR total BETWEEN 0 AND 100", name="total_range"),
    CheckConstraint(
        "kind <> 'preliminary' OR ((total IS NULL OR total <= 69)"
        " AND (category IS NULL OR category IN ('consider', 'hidden')))",
        name="preliminary_limits",
    ),
    # Одна действующая оценка каждого вида у вакансии.
    Index(
        "uq_scores_one_active_per_kind",
        "vacancy_id",
        "kind",
        unique=True,
        postgresql_where=text("state <> 'outdated'"),
    ),
    Index("ix_scores_selection", "kind", "total", postgresql_where=text("state <> 'outdated'")),
    Index("ix_scores_pending_recalc", "state", postgresql_where=text("state = 'pending_recalc'")),
)

# ---------------------------------------------------------------- 6.3
score_overrides = Table(
    "score_overrides",
    metadata,
    id_column(),
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("gate_code", Text),
    Column("new_total", Integer),
    Column("comment", Text),
    Column("based_on_score_id", BigInteger, ForeignKey("scores.id", ondelete="SET NULL")),
    Column("is_active", Boolean, nullable=False, server_default="true"),
    ts("created_at", default_now=True),
    ts("revoked_at"),
    enum_check("kind", ["set_total", "force_include", "force_exclude", "gate_wrong"]),
    CheckConstraint("new_total IS NULL OR new_total BETWEEN 0 AND 100", name="new_total_range"),
    CheckConstraint("kind <> 'set_total' OR new_total IS NOT NULL", name="set_total_has_value"),
    CheckConstraint("kind <> 'gate_wrong' OR gate_code IS NOT NULL", name="gate_wrong_has_code"),
    Index("ix_score_overrides_active", "vacancy_id", postgresql_where=text("is_active")),
)

# ---------------------------------------------------------------- 7.1, 7.2
reports = Table(
    "reports",
    metadata,
    id_column(),
    Column("kind", Text, nullable=False),
    Column("report_date", Date, nullable=False),
    Column("search_query", Text),
    Column("status", Text, nullable=False, server_default="building"),
    Column("stats", JSONB),
    ts("created_at", default_now=True),
    ts("completed_at"),
    enum_check("kind", ["daily", "search"]),
    enum_check("status", ["building", "ready", "sent", "partial"]),
    # Один ежедневный отчёт на дату.
    Index(
        "uq_reports_one_daily_per_date",
        "kind",
        "report_date",
        unique=True,
        postgresql_where=text("kind = 'daily'"),
    ),
)

report_items = Table(
    "report_items",
    metadata,
    id_column(),
    Column("report_id", BigInteger, ForeignKey("reports.id", ondelete="CASCADE"), nullable=False),
    # Удаляется вместе с вакансией: иначе очистка (DATABASE_SCHEMA.md 12, шаг 5) упрётся в запрет.
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
    Column("block", Text, nullable=False),
    Column("position", Integer, nullable=False),
    Column("score_id", BigInteger, ForeignKey("scores.id", ondelete="SET NULL")),
    Column("total_snapshot", Integer),
    enum_check("block", ["main", "title_only"]),
    UniqueConstraint("report_id", "vacancy_id"),
)

# ---------------------------------------------------------------- 8.1, 8.2
feedback_events = Table(
    "feedback_events",
    metadata,
    id_column(),
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE")),
    Column("event", Text, nullable=False),
    Column("reason_code", Text),
    Column("comment", Text),
    Column("score_id", BigInteger, ForeignKey("scores.id", ondelete="SET NULL")),
    ts("created_at", nullable=False, default_now=True),
    enum_check("event", ["interested", "applied", "rejected", "cleared", "note"]),
    enum_check("reason_code", ["stack", "level", "not_remote", "company", "sales", "conditions", "seen", "other"]),
    # Пустая ссылка на вакансию допустима только для общего отзыва.
    CheckConstraint("vacancy_id IS NOT NULL OR event = 'note'", name="vacancy_required_except_note"),
    Index(None, "vacancy_id", "created_at"),
)

cover_letters = Table(
    "cover_letters",
    metadata,
    id_column(),
    Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("question", Text),
    Column("version_no", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("profile_version_id", BigInteger, ForeignKey("profile_versions.id"), nullable=False),
    Column("ledger_id", BigInteger, ForeignKey("llm_ledger.id", ondelete="SET NULL")),
    ts("created_at", nullable=False, default_now=True),
    enum_check("kind", ["letter", "answer"]),
)

VACANCY_TABLES = (
    source_checks, vacancies, vacancy_sources, profile_versions, scoring_versions, extractions,
    scores, score_overrides, reports, report_items, feedback_events, cover_letters,
)

# ---------------------------------------------------------------- дополнения общих таблиц
# Предметные столбцы и перечни общих таблиц (DATABASE_SCHEMA.md, разделы 3.1, 7.3, 9.2, 10.1).
source_state.append_column(Column("vacancies_real", Integer, nullable=False, server_default="0"))
source_state.append_column(Column("passed_profile", Integer, nullable=False, server_default="0"))
outbox.append_column(Column("report_item_id", BigInteger, ForeignKey("report_items.id", ondelete="SET NULL")))
outbox.append_constraint(enum_check("kind", OUTBOX_KINDS))
llm_ledger.append_column(Column("vacancy_id", BigInteger, ForeignKey("vacancies.id", ondelete="SET NULL")))
job_runs.append_constraint(enum_check("job_name", JOB_NAMES))

ALL_TABLES = CORE_TABLES + VACANCY_TABLES
