"""Начальная схема: 20 таблиц AI Vacancy Hunter (DATABASE_SCHEMA.md).

Создаётся по описанию таблиц из hunter_core.storage.tables и vacancy_hunter.storage.tables
с ограничениями и индексами раздела 11. Применённую миграцию не редактировать:
изменения схемы — только новыми миграциями (DATABASE_SCHEMA.md, раздел 15).

Revision ID: 0001
Revises: 
Create Date: 2026-11-13 10:55:40.537422
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Расширение для триграммного индекса названий. Обычно уже создано скриптом
    # docker/db-init; pg_trgm — доверенное расширение, владелец базы может создать его сам.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('app_state',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_app_state'))
    )
    op.create_table('job_runs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('job_name', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.CheckConstraint("job_name IN ('collect', 'process', 'report', 'recalc', 'source_review', 'cleanup', 'backup')", name=op.f('ck_job_runs_job_name')),
    sa.CheckConstraint("status IN ('running', 'success', 'failed', 'abandoned')", name=op.f('ck_job_runs_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_runs'))
    )
    op.create_index('uq_job_runs_one_running', 'job_runs', ['job_name'], unique=True, postgresql_where=sa.text("status = 'running'"))
    op.create_table('llm_budget',
    sa.Column('month', sa.Date(), nullable=False),
    sa.Column('limit_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('spent_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('reserved_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('letters_reserve_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('daily_extraction_limit_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('quiet_limit_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('quiet_spent_usd', sa.Numeric(precision=12, scale=6), server_default='0', nullable=False),
    sa.Column('warn1_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('warn2_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('warn1_sent', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('warn2_sent', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("date_trunc('month', month) = month", name=op.f('ck_llm_budget_month_is_first_day')),
    sa.CheckConstraint('limit_usd >= 0 AND spent_usd >= 0 AND reserved_usd >= 0 AND letters_reserve_usd >= 0 AND daily_extraction_limit_usd >= 0 AND quiet_limit_usd >= 0 AND quiet_spent_usd >= 0 AND warn1_usd >= 0 AND warn2_usd >= 0', name=op.f('ck_llm_budget_amounts_non_negative')),
    sa.CheckConstraint('spent_usd + reserved_usd <= limit_usd', name=op.f('ck_llm_budget_spent_plus_reserved_within_limit')),
    sa.PrimaryKeyConstraint('month', name=op.f('pk_llm_budget'))
    )
    op.create_table('profile_versions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('profile', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('skills_dictionary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_note', sa.Text(), nullable=True),
    sa.Column('is_current', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_profile_versions')),
    sa.UniqueConstraint('content_hash', name=op.f('uq_profile_versions_content_hash')),
    sa.UniqueConstraint('version_no', name=op.f('uq_profile_versions_version_no'))
    )
    op.create_index('uq_profile_versions_one_current', 'profile_versions', ['is_current'], unique=True, postgresql_where=sa.text('is_current'))
    op.create_table('reports',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('report_date', sa.Date(), nullable=False),
    sa.Column('search_query', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='building', nullable=False),
    sa.Column('stats', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('daily', 'search')", name=op.f('ck_reports_kind')),
    sa.CheckConstraint("status IN ('building', 'ready', 'sent', 'partial')", name=op.f('ck_reports_status')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports'))
    )
    op.create_index('uq_reports_one_daily_per_date', 'reports', ['kind', 'report_date'], unique=True, postgresql_where=sa.text("kind = 'daily'"))
    op.create_table('scoring_versions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.Text(), nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('is_current', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scoring_versions')),
    sa.UniqueConstraint('content_hash', name=op.f('uq_scoring_versions_content_hash')),
    sa.UniqueConstraint('version_no', name=op.f('uq_scoring_versions_version_no'))
    )
    op.create_index('uq_scoring_versions_one_current', 'scoring_versions', ['is_current'], unique=True, postgresql_where=sa.text('is_current'))
    op.create_table('source_state',
    sa.Column('code', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('mode', sa.Text(), nullable=False),
    sa.Column('mode_reason', sa.Text(), nullable=True),
    sa.Column('health', sa.Text(), server_default='ok', nullable=False),
    sa.Column('cursor', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('check_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('check_extensions', sa.Integer(), server_default='0', nullable=False),
    sa.Column('failed_rechecks', sa.Integer(), server_default='0', nullable=False),
    sa.Column('posts_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('llm_sampled', sa.Integer(), server_default='0', nullable=False),
    sa.Column('relevance_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('vacancies_real', sa.Integer(), server_default='0', nullable=False),
    sa.Column('passed_profile', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint("health IN ('ok', 'degraded', 'blocked')", name=op.f('ck_source_state_health')),
    sa.CheckConstraint("kind IN ('telegram', 'rss', 'api')", name=op.f('ck_source_state_kind')),
    sa.CheckConstraint("mode IN ('active', 'quiet', 'waiting', 'disabled')", name=op.f('ck_source_state_mode')),
    sa.CheckConstraint("mode_reason IN ('initial_check', 'recheck', 'owner', 'blocked', 'failed_check')", name=op.f('ck_source_state_mode_reason')),
    sa.CheckConstraint('check_extensions BETWEEN 0 AND 2', name=op.f('ck_source_state_check_extensions_range')),
    sa.CheckConstraint('consecutive_failures >= 0 AND failed_rechecks >= 0 AND posts_total >= 0 AND llm_sampled >= 0', name=op.f('ck_source_state_counters_non_negative')),
    sa.PrimaryKeyConstraint('code', name=op.f('pk_source_state'))
    )
    op.create_table('fetch_runs',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('source_code', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('items_seen', sa.Integer(), nullable=True),
    sa.Column('items_new', sa.Integer(), nullable=True),
    sa.Column('cursor_before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('cursor_after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('http_requests', sa.Integer(), nullable=True),
    sa.Column('error_code', sa.Text(), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('running', 'success', 'partial', 'failed', 'abandoned')", name=op.f('ck_fetch_runs_status')),
    sa.ForeignKeyConstraint(['source_code'], ['source_state.code'], name=op.f('fk_fetch_runs_source_code_source_state')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fetch_runs'))
    )
    op.create_index(op.f('ix_fetch_runs_source_code_started_at'), 'fetch_runs', ['source_code', 'started_at'], unique=False)
    op.create_table('source_checks',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('source_code', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('posts_total', sa.Integer(), nullable=False),
    sa.Column('vacancies_real', sa.Integer(), nullable=False),
    sa.Column('passed_profile', sa.Integer(), nullable=False),
    sa.Column('llm_sampled', sa.Integer(), nullable=False),
    sa.Column('relevance_pct', sa.Numeric(precision=5, scale=2), nullable=True),
    sa.Column('llm_cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('result', sa.Text(), nullable=False),
    sa.Column('decided_by', sa.Text(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decided_by IN ('auto', 'owner')", name=op.f('ck_source_checks_decided_by')),
    sa.CheckConstraint("kind IN ('initial', 'recheck', 'monthly')", name=op.f('ck_source_checks_kind')),
    sa.CheckConstraint("result IN ('included', 'kept', 'extended', 'sent_to_recheck', 'rejected')", name=op.f('ck_source_checks_result')),
    sa.ForeignKeyConstraint(['source_code'], ['source_state.code'], name=op.f('fk_source_checks_source_code_source_state')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source_checks'))
    )
    op.create_index(op.f('ix_source_checks_source_code_created_at'), 'source_checks', ['source_code', 'created_at'], unique=False)
    op.create_table('vacancies',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('status', sa.Text(), server_default='new', nullable=False),
    sa.Column('status_reason', sa.Text(), nullable=True),
    sa.Column('description_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('company_name', sa.Text(), nullable=True),
    sa.Column('company_norm', sa.Text(), nullable=True),
    sa.Column('title_norm', sa.Text(), nullable=True),
    sa.Column('description_text', sa.Text(), nullable=True),
    sa.Column('description_hash', sa.Text(), nullable=True),
    sa.Column('has_description', sa.Boolean(), nullable=False),
    sa.Column('canonical_url', sa.Text(), nullable=True),
    sa.Column('salary_from', sa.Integer(), nullable=True),
    sa.Column('salary_to', sa.Integer(), nullable=True),
    sa.Column('salary_currency', sa.Text(), nullable=True),
    sa.Column('salary_period', sa.Text(), nullable=True),
    sa.Column('location_raw', sa.Text(), nullable=True),
    sa.Column('country_code', sa.Text(), nullable=True),
    sa.Column('work_format_src', sa.Text(), nullable=True),
    sa.Column('experience_src', sa.Text(), nullable=True),
    sa.Column('offer_type', sa.Text(), server_default='unknown', nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('primary_source_code', sa.Text(), nullable=True),
    sa.Column('duplicate_of', sa.BigInteger(), nullable=True),
    sa.Column('quiet_only', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('user_state', sa.Text(), server_default='none', nullable=False),
    sa.Column('user_state_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_full_scored_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reported_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('title_only_reported_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('alive_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('llm_attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('text_purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("country_code IS NULL OR country_code ~ '^[A-Z]{2}$'", name=op.f('ck_vacancies_country_code_format')),
    sa.CheckConstraint("offer_type IN ('job', 'internship', 'project', 'unknown')", name=op.f('ck_vacancies_offer_type')),
    sa.CheckConstraint("status IN ('new', 'awaiting_description', 'duplicate', 'rejected_rules', 'pending_llm', 'llm_failed', 'extracted', 'rejected_gate', 'scored', 'reported', 'closed')", name=op.f('ck_vacancies_status')),
    sa.CheckConstraint("user_state IN ('none', 'interested', 'applied', 'rejected')", name=op.f('ck_vacancies_user_state')),
    sa.CheckConstraint('duplicate_of IS NULL OR duplicate_of <> id', name=op.f('ck_vacancies_not_duplicate_of_self')),
    sa.CheckConstraint('llm_attempts BETWEEN 0 AND 3', name=op.f('ck_vacancies_llm_attempts_range')),
    sa.ForeignKeyConstraint(['duplicate_of'], ['vacancies.id'], name=op.f('fk_vacancies_duplicate_of_vacancies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['primary_source_code'], ['source_state.code'], name=op.f('fk_vacancies_primary_source_code_source_state')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vacancies'))
    )
    op.create_index(op.f('ix_vacancies_canonical_url'), 'vacancies', ['canonical_url'], unique=False)
    op.create_index(op.f('ix_vacancies_company_norm_title_norm'), 'vacancies', ['company_norm', 'title_norm'], unique=False)
    op.create_index(op.f('ix_vacancies_description_hash'), 'vacancies', ['description_hash'], unique=False)
    op.create_index(op.f('ix_vacancies_first_seen_at'), 'vacancies', ['first_seen_at'], unique=False)
    op.create_index(op.f('ix_vacancies_offer_type_first_seen_at'), 'vacancies', ['offer_type', 'first_seen_at'], unique=False)
    op.create_index(op.f('ix_vacancies_status'), 'vacancies', ['status'], unique=False)
    op.create_index('ix_vacancies_title_norm_trgm', 'vacancies', ['title_norm'], unique=False, postgresql_using='gin', postgresql_ops={'title_norm': 'gin_trgm_ops'})
    op.create_index('ix_vacancies_user_state_marked', 'vacancies', ['user_state'], unique=False, postgresql_where=sa.text("user_state <> 'none'"))
    op.create_table('llm_ledger',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('month', sa.Date(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('part_no', sa.Integer(), nullable=True),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('reserved_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('actual_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('tokens_in', sa.Integer(), nullable=True),
    sa.Column('tokens_cache_write', sa.Integer(), nullable=True),
    sa.Column('tokens_cache_read', sa.Integer(), nullable=True),
    sa.Column('tokens_out', sa.Integer(), nullable=True),
    sa.Column('stop_reason', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("purpose IN ('extraction', 'quiet_check', 'letter', 'answer')", name=op.f('ck_llm_ledger_purpose')),
    sa.CheckConstraint("state IN ('reserved', 'settled', 'released', 'assumed_spent')", name=op.f('ck_llm_ledger_state')),
    sa.CheckConstraint('reserved_usd >= 0 AND (actual_usd IS NULL OR actual_usd >= 0)', name=op.f('ck_llm_ledger_amounts_non_negative')),
    sa.ForeignKeyConstraint(['month'], ['llm_budget.month'], name=op.f('fk_llm_ledger_month_llm_budget')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_llm_ledger_vacancy_id_vacancies'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_ledger'))
    )
    op.create_index(op.f('ix_llm_ledger_month_started_at'), 'llm_ledger', ['month', 'started_at'], unique=False)
    op.create_index('ix_llm_ledger_reserved', 'llm_ledger', ['state'], unique=False, postgresql_where=sa.text("state = 'reserved'"))
    op.create_table('raw_items',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('source_code', sa.Text(), nullable=False),
    sa.Column('external_id', sa.Text(), nullable=False),
    sa.Column('fetch_run_id', sa.BigInteger(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['fetch_run_id'], ['fetch_runs.id'], name=op.f('fk_raw_items_fetch_run_id_fetch_runs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_code'], ['source_state.code'], name=op.f('fk_raw_items_source_code_source_state')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_items')),
    sa.UniqueConstraint('source_code', 'external_id', name=op.f('uq_raw_items_source_code_external_id'))
    )
    op.create_index(op.f('ix_raw_items_fetched_at'), 'raw_items', ['fetched_at'], unique=False)
    op.create_table('cover_letters',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('question', sa.Text(), nullable=True),
    sa.Column('version_no', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('profile_version_id', sa.BigInteger(), nullable=False),
    sa.Column('ledger_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('letter', 'answer')", name=op.f('ck_cover_letters_kind')),
    sa.ForeignKeyConstraint(['ledger_id'], ['llm_ledger.id'], name=op.f('fk_cover_letters_ledger_id_llm_ledger'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['profile_version_id'], ['profile_versions.id'], name=op.f('fk_cover_letters_profile_version_id_profile_versions')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_cover_letters_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cover_letters'))
    )
    op.create_table('extractions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('text_hash', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('parts_total', sa.Integer(), nullable=False),
    sa.Column('parts_done', sa.Integer(), nullable=False),
    sa.Column('parts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('chars_total', sa.Integer(), nullable=False),
    sa.Column('chars_analyzed', sa.Integer(), nullable=False),
    sa.Column('facts', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('quote_check', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ledger_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ok', 'partial', 'invalid')", name=op.f('ck_extractions_status')),
    sa.CheckConstraint('chars_analyzed BETWEEN 0 AND chars_total', name=op.f('ck_extractions_chars_range')),
    sa.CheckConstraint('parts_total BETWEEN 1 AND 4 AND parts_done BETWEEN 0 AND parts_total', name=op.f('ck_extractions_parts_range')),
    sa.ForeignKeyConstraint(['ledger_id'], ['llm_ledger.id'], name=op.f('fk_extractions_ledger_id_llm_ledger'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_extractions')),
    sa.UniqueConstraint('text_hash', 'model', 'prompt_version', name=op.f('uq_extractions_text_hash_model_prompt_version'))
    )
    op.create_table('vacancy_sources',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('source_code', sa.Text(), nullable=False),
    sa.Column('external_id', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('raw_item_id', sa.BigInteger(), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['raw_item_id'], ['raw_items.id'], name=op.f('fk_vacancy_sources_raw_item_id_raw_items'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_code'], ['source_state.code'], name=op.f('fk_vacancy_sources_source_code_source_state')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_vacancy_sources_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vacancy_sources')),
    sa.UniqueConstraint('source_code', 'external_id', name=op.f('uq_vacancy_sources_source_code_external_id'))
    )
    op.create_index(op.f('ix_vacancy_sources_vacancy_id'), 'vacancy_sources', ['vacancy_id'], unique=False)
    op.create_table('scores',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), server_default='current', nullable=False),
    sa.Column('extraction_id', sa.BigInteger(), nullable=True),
    sa.Column('profile_version_id', sa.BigInteger(), nullable=False),
    sa.Column('scoring_version_id', sa.BigInteger(), nullable=False),
    sa.Column('gate_result', sa.Text(), nullable=False),
    sa.Column('gates', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('unknowns', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('total', sa.Integer(), nullable=True),
    sa.Column('category', sa.Text(), nullable=True),
    sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.Column('superseded_by', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("category IN ('excellent', 'good', 'consider', 'hidden')", name=op.f('ck_scores_category')),
    sa.CheckConstraint("gate_result IN ('passed', 'rejected')", name=op.f('ck_scores_gate_result')),
    sa.CheckConstraint("kind <> 'preliminary' OR ((total IS NULL OR total <= 69) AND (category IS NULL OR category IN ('consider', 'hidden')))", name=op.f('ck_scores_preliminary_limits')),
    sa.CheckConstraint("kind IN ('full', 'preliminary')", name=op.f('ck_scores_kind')),
    sa.CheckConstraint("state IN ('current', 'pending_recalc', 'recalc_failed', 'outdated')", name=op.f('ck_scores_state')),
    sa.CheckConstraint('total IS NULL OR total BETWEEN 0 AND 100', name=op.f('ck_scores_total_range')),
    sa.ForeignKeyConstraint(['extraction_id'], ['extractions.id'], name=op.f('fk_scores_extraction_id_extractions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['profile_version_id'], ['profile_versions.id'], name=op.f('fk_scores_profile_version_id_profile_versions')),
    sa.ForeignKeyConstraint(['scoring_version_id'], ['scoring_versions.id'], name=op.f('fk_scores_scoring_version_id_scoring_versions')),
    sa.ForeignKeyConstraint(['superseded_by'], ['scores.id'], name=op.f('fk_scores_superseded_by_scores'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_scores_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scores'))
    )
    op.create_index('ix_scores_pending_recalc', 'scores', ['state'], unique=False, postgresql_where=sa.text("state = 'pending_recalc'"))
    op.create_index('ix_scores_selection', 'scores', ['kind', 'total'], unique=False, postgresql_where=sa.text("state <> 'outdated'"))
    op.create_index('uq_scores_one_active_per_kind', 'scores', ['vacancy_id', 'kind'], unique=True, postgresql_where=sa.text("state <> 'outdated'"))
    op.create_table('feedback_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=True),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('reason_code', sa.Text(), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('score_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("event IN ('interested', 'applied', 'rejected', 'cleared', 'note')", name=op.f('ck_feedback_events_event')),
    sa.CheckConstraint("reason_code IN ('stack', 'level', 'not_remote', 'company', 'sales', 'conditions', 'seen', 'other')", name=op.f('ck_feedback_events_reason_code')),
    sa.CheckConstraint("vacancy_id IS NOT NULL OR event = 'note'", name=op.f('ck_feedback_events_vacancy_required_except_note')),
    sa.ForeignKeyConstraint(['score_id'], ['scores.id'], name=op.f('fk_feedback_events_score_id_scores')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_feedback_events_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_feedback_events'))
    )
    op.create_index(op.f('ix_feedback_events_vacancy_id_created_at'), 'feedback_events', ['vacancy_id', 'created_at'], unique=False)
    op.create_table('report_items',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('report_id', sa.BigInteger(), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('block', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('score_id', sa.BigInteger(), nullable=True),
    sa.Column('total_snapshot', sa.Integer(), nullable=True),
    sa.CheckConstraint("block IN ('main', 'title_only')", name=op.f('ck_report_items_block')),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], name=op.f('fk_report_items_report_id_reports'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['score_id'], ['scores.id'], name=op.f('fk_report_items_score_id_scores')),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_report_items_vacancy_id_vacancies')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_items')),
    sa.UniqueConstraint('report_id', 'vacancy_id', name=op.f('uq_report_items_report_id_vacancy_id'))
    )
    op.create_table('score_overrides',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('vacancy_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('gate_code', sa.Text(), nullable=True),
    sa.Column('new_total', sa.Integer(), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('based_on_score_id', sa.BigInteger(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind <> 'gate_wrong' OR gate_code IS NOT NULL", name=op.f('ck_score_overrides_gate_wrong_has_code')),
    sa.CheckConstraint("kind <> 'set_total' OR new_total IS NOT NULL", name=op.f('ck_score_overrides_set_total_has_value')),
    sa.CheckConstraint("kind IN ('set_total', 'force_include', 'force_exclude', 'gate_wrong')", name=op.f('ck_score_overrides_kind')),
    sa.CheckConstraint('new_total IS NULL OR new_total BETWEEN 0 AND 100', name=op.f('ck_score_overrides_new_total_range')),
    sa.ForeignKeyConstraint(['based_on_score_id'], ['scores.id'], name=op.f('fk_score_overrides_based_on_score_id_scores'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['vacancy_id'], ['vacancies.id'], name=op.f('fk_score_overrides_vacancy_id_vacancies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_score_overrides'))
    )
    op.create_index('ix_score_overrides_active', 'score_overrides', ['vacancy_id'], unique=False, postgresql_where=sa.text('is_active'))
    op.create_table('outbox',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('dedup_key', sa.Text(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('state', sa.Text(), server_default='pending', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sending_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('report_item_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("kind IN ('report_header', 'vacancy_card', 'alert', 'letter', 'backup', 'system')", name=op.f('ck_outbox_kind')),
    sa.CheckConstraint("state IN ('pending', 'sending', 'sent', 'failed', 'unconfirmed')", name=op.f('ck_outbox_state')),
    sa.CheckConstraint('attempts BETWEEN 0 AND 5', name=op.f('ck_outbox_attempts_range')),
    sa.ForeignKeyConstraint(['report_item_id'], ['report_items.id'], name=op.f('fk_outbox_report_item_id_report_items'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbox')),
    sa.UniqueConstraint('dedup_key', name=op.f('uq_outbox_dedup_key'))
    )
    op.create_index(op.f('ix_outbox_state_next_attempt_at'), 'outbox', ['state', 'next_attempt_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_outbox_state_next_attempt_at'), table_name='outbox')
    op.drop_table('outbox')
    op.drop_index('ix_score_overrides_active', table_name='score_overrides', postgresql_where=sa.text('is_active'))
    op.drop_table('score_overrides')
    op.drop_table('report_items')
    op.drop_index(op.f('ix_feedback_events_vacancy_id_created_at'), table_name='feedback_events')
    op.drop_table('feedback_events')
    op.drop_index('uq_scores_one_active_per_kind', table_name='scores', postgresql_where=sa.text("state <> 'outdated'"))
    op.drop_index('ix_scores_selection', table_name='scores', postgresql_where=sa.text("state <> 'outdated'"))
    op.drop_index('ix_scores_pending_recalc', table_name='scores', postgresql_where=sa.text("state = 'pending_recalc'"))
    op.drop_table('scores')
    op.drop_index(op.f('ix_vacancy_sources_vacancy_id'), table_name='vacancy_sources')
    op.drop_table('vacancy_sources')
    op.drop_table('extractions')
    op.drop_table('cover_letters')
    op.drop_index(op.f('ix_raw_items_fetched_at'), table_name='raw_items')
    op.drop_table('raw_items')
    op.drop_index('ix_llm_ledger_reserved', table_name='llm_ledger', postgresql_where=sa.text("state = 'reserved'"))
    op.drop_index(op.f('ix_llm_ledger_month_started_at'), table_name='llm_ledger')
    op.drop_table('llm_ledger')
    op.drop_index('ix_vacancies_user_state_marked', table_name='vacancies', postgresql_where=sa.text("user_state <> 'none'"))
    op.drop_index('ix_vacancies_title_norm_trgm', table_name='vacancies', postgresql_using='gin', postgresql_ops={'title_norm': 'gin_trgm_ops'})
    op.drop_index(op.f('ix_vacancies_status'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_offer_type_first_seen_at'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_first_seen_at'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_description_hash'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_company_norm_title_norm'), table_name='vacancies')
    op.drop_index(op.f('ix_vacancies_canonical_url'), table_name='vacancies')
    op.drop_table('vacancies')
    op.drop_index(op.f('ix_source_checks_source_code_created_at'), table_name='source_checks')
    op.drop_table('source_checks')
    op.drop_index(op.f('ix_fetch_runs_source_code_started_at'), table_name='fetch_runs')
    op.drop_table('fetch_runs')
    op.drop_table('source_state')
    op.drop_index('uq_scoring_versions_one_current', table_name='scoring_versions', postgresql_where=sa.text('is_current'))
    op.drop_table('scoring_versions')
    op.drop_index('uq_reports_one_daily_per_date', table_name='reports', postgresql_where=sa.text("kind = 'daily'"))
    op.drop_table('reports')
    op.drop_index('uq_profile_versions_one_current', table_name='profile_versions', postgresql_where=sa.text('is_current'))
    op.drop_table('profile_versions')
    op.drop_table('llm_budget')
    op.drop_index('uq_job_runs_one_running', table_name='job_runs', postgresql_where=sa.text("status = 'running'"))
    op.drop_table('job_runs')
    op.drop_table('app_state')
