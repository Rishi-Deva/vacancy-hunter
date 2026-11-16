"""DB-2…DB-5, DB-7: ограничения, которые проверяет сама база (TEST_PLAN.md, раздел 4.1).

Каждый тест работает в транзакции с откатом. Нарушение проверяется во вложенной
транзакции (SAVEPOINT), чтобы после ошибки базы можно было продолжить проверки.
Тест сверяет имя сработавшего ограничения: отказ по другой причине тест не засчитает.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration


def insert(conn, sql: str, **params):
    return conn.execute(text(sql), params)


def violation(conn, sql: str, **params) -> str | None:
    """Выполнить запись во вложенной транзакции. Вернуть имя нарушенного ограничения или None."""
    savepoint = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except IntegrityError as exc:
        savepoint.rollback()
        return exc.orig.diag.constraint_name
    savepoint.rollback()
    return None


def add_source(conn, code="tg_test"):
    insert(conn, "INSERT INTO source_state (code, kind, mode) VALUES (:c, 'telegram', 'active')", c=code)


def add_vacancy(conn) -> int:
    return insert(
        conn, "INSERT INTO vacancies (title, has_description) VALUES ('Python-разработчик', true) RETURNING id"
    ).scalar_one()


def add_versions(conn) -> tuple[int, int]:
    profile_id = insert(
        conn,
        "INSERT INTO profile_versions (version_no, content_hash, profile, skills_dictionary, is_current)"
        " VALUES (1, 'p1', '{}', '{}', true) RETURNING id",
    ).scalar_one()
    scoring_id = insert(
        conn,
        "INSERT INTO scoring_versions (version_no, content_hash, rules, is_current)"
        " VALUES (1, 's1', '{}', true) RETURNING id",
    ).scalar_one()
    return profile_id, scoring_id


SCORE_SQL = (
    "INSERT INTO scores (vacancy_id, kind, state, profile_version_id, scoring_version_id, gate_result, gates, total)"
    " VALUES (:v, :kind, :state, :p, :s, 'passed', '{}', :total)"
)


# ------------------------------------------------------------------ DB-2
def test_db2_duplicate_raw_item_rejected(conn):
    add_source(conn)
    sql = "INSERT INTO raw_items (source_code, external_id, payload) VALUES ('tg_test', 'it_interns/101', '{}')"
    assert violation(conn, sql) is None
    insert(conn, sql)
    assert violation(conn, sql) == "uq_raw_items_source_code_external_id"
    assert violation(conn, sql.replace("101", "102")) is None  # другой пост — допустим


def test_db2_duplicate_vacancy_source_rejected(conn):
    add_source(conn)
    sql = "INSERT INTO vacancy_sources (vacancy_id, source_code, external_id) VALUES (:v, 'tg_test', 'x/1')"
    insert(conn, sql, v=add_vacancy(conn))
    assert violation(conn, sql, v=add_vacancy(conn)) == "uq_vacancy_sources_source_code_external_id"


# ------------------------------------------------------------------ DB-3
def test_db3_second_active_score_of_same_kind_rejected(conn):
    vacancy_id = add_vacancy(conn)
    p, s = add_versions(conn)
    insert(conn, SCORE_SQL, v=vacancy_id, kind="full", state="current", p=p, s=s, total=70)
    for state in ("current", "pending_recalc", "recalc_failed"):
        assert violation(conn, SCORE_SQL, v=vacancy_id, kind="full", state=state, p=p, s=s, total=70) \
            == "uq_scores_one_active_per_kind", state


def test_db3_outdated_and_other_kind_allowed(conn):
    vacancy_id = add_vacancy(conn)
    p, s = add_versions(conn)
    insert(conn, SCORE_SQL, v=vacancy_id, kind="full", state="current", p=p, s=s, total=70)
    # история: устаревшие оценки того же вида допускаются в любом количестве
    insert(conn, SCORE_SQL, v=vacancy_id, kind="full", state="outdated", p=p, s=s, total=70)
    assert violation(conn, SCORE_SQL, v=vacancy_id, kind="full", state="outdated", p=p, s=s, total=70) is None
    # предварительная оценка — другой вид, не пересекается с полной
    assert violation(conn, SCORE_SQL, v=vacancy_id, kind="preliminary", state="current", p=p, s=s, total=60) is None


def test_db3_preliminary_score_above_69_rejected(conn):
    vacancy_id = add_vacancy(conn)
    p, s = add_versions(conn)
    assert violation(conn, SCORE_SQL, v=vacancy_id, kind="preliminary", state="current", p=p, s=s, total=70) \
        == "ck_scores_preliminary_limits"


# ------------------------------------------------------------------ DB-4
BUDGET_SQL = (
    "INSERT INTO llm_budget (month, limit_usd, spent_usd, reserved_usd, letters_reserve_usd,"
    " daily_extraction_limit_usd, quiet_limit_usd, warn1_usd, warn2_usd)"
    " VALUES (:m, 3.00, :spent, :reserved, 0.30, 0.08, 0.50, 1.50, 2.50)"
)
LIMIT_CK = "ck_llm_budget_spent_plus_reserved_within_limit"


def test_db4_spent_plus_reserved_over_limit_rejected(conn):
    month = date(2026, 11, 1)
    assert violation(conn, BUDGET_SQL, m=month, spent=2.5, reserved=0.500001) == LIMIT_CK
    assert violation(conn, BUDGET_SQL, m=month, spent=2.5, reserved=0.5) is None  # ровно лимит — допустимо


def test_db4_update_over_limit_rejected(conn):
    month = date(2026, 11, 1)
    insert(conn, BUDGET_SQL, m=month, spent=1.0, reserved=0.0)
    sql = "UPDATE llm_budget SET reserved_usd = reserved_usd + :add WHERE month = :m"
    assert violation(conn, sql, add=2.000001, m=month) == LIMIT_CK
    assert violation(conn, sql, add=2, m=month) is None


def test_db4_negative_amounts_and_mid_month_rejected(conn):
    assert violation(conn, BUDGET_SQL, m=date(2026, 11, 1), spent=-0.01, reserved=0) \
        == "ck_llm_budget_amounts_non_negative"
    assert violation(conn, BUDGET_SQL, m=date(2026, 11, 15), spent=0, reserved=0) \
        == "ck_llm_budget_month_is_first_day"


# ------------------------------------------------------------------ DB-5
def test_db5_second_daily_report_same_date_rejected(conn):
    sql = "INSERT INTO reports (kind, report_date) VALUES (:k, :d)"
    insert(conn, sql, k="daily", d=date(2026, 11, 13))
    assert violation(conn, sql, k="daily", d=date(2026, 11, 13)) == "uq_reports_one_daily_per_date"
    # поиски в тот же день и отчёт на другую дату — допустимы
    insert(conn, sql, k="search", d=date(2026, 11, 13))
    assert violation(conn, sql, k="search", d=date(2026, 11, 13)) is None
    assert violation(conn, sql, k="daily", d=date(2026, 11, 14)) is None


# ------------------------------------------------------------------ DB-7
@pytest.mark.parametrize("table,columns,values", [
    ("profile_versions", "version_no, content_hash, profile, skills_dictionary, is_current",
     "{n}, 'h{n}', '{{}}', '{{}}', {cur}"),
    ("scoring_versions", "version_no, content_hash, rules, is_current", "{n}, 'h{n}', '{{}}', {cur}"),
])
def test_db7_second_current_version_rejected(conn, table, columns, values):
    def sql(n, cur):
        return f"INSERT INTO {table} ({columns}) VALUES ({values.format(n=n, cur=cur)})"

    insert(conn, sql(1, "true"))
    assert violation(conn, sql(2, "true")) == f"uq_{table}_one_current"
    insert(conn, sql(2, "false"))
    assert violation(conn, sql(3, "false")) is None  # нетекущих версий сколько угодно


# ------------------------------------------------------------------ прочие ограничения схемы
def test_unknown_enum_value_rejected(conn):
    assert violation(conn, "INSERT INTO vacancies (title, has_description, status) VALUES ('x', true, 'archived')") \
        == "ck_vacancies_status"
    assert violation(conn, "INSERT INTO outbox (dedup_key, chat_id, kind, payload) VALUES ('k', 1, 'spam', '{}')") \
        == "ck_outbox_kind"


def test_outbox_dedup_key_unique(conn):
    sql = "INSERT INTO outbox (dedup_key, chat_id, kind, payload) VALUES ('report:1', 1, 'report_header', '{}')"
    insert(conn, sql)
    assert violation(conn, sql) == "uq_outbox_dedup_key"


def test_one_running_job_per_name(conn):
    sql = "INSERT INTO job_runs (job_name, status) VALUES ('collect', :s)"
    insert(conn, sql, s="running")
    assert violation(conn, sql, s="running") == "uq_job_runs_one_running"
    assert violation(conn, sql, s="success") is None


def test_feedback_without_vacancy_only_for_note(conn):
    assert violation(conn, "INSERT INTO feedback_events (event) VALUES ('interested')") \
        == "ck_feedback_events_vacancy_required_except_note"
    assert violation(conn, "INSERT INTO feedback_events (event, comment) VALUES ('note', 'общий отзыв')") is None


def test_deleting_shown_vacancy_cleans_dependent_rows(conn):
    """Очистка (DATABASE_SCHEMA.md 12, шаг 5): показанную вакансию можно удалить вместе с оценками."""
    vacancy_id = add_vacancy(conn)
    p, s = add_versions(conn)
    score_id = insert(conn, SCORE_SQL + " RETURNING id", v=vacancy_id, kind="full", state="current",
                      p=p, s=s, total=80).scalar_one()
    report_id = insert(conn, "INSERT INTO reports (kind, report_date) VALUES ('daily', '2026-11-13') RETURNING id"
                       ).scalar_one()
    insert(conn, "INSERT INTO report_items (report_id, vacancy_id, block, position, score_id)"
                 " VALUES (:r, :v, 'main', 1, :s)", r=report_id, v=vacancy_id, s=score_id)
    insert(conn, "INSERT INTO feedback_events (vacancy_id, event, score_id) VALUES (:v, 'interested', :s)",
           v=vacancy_id, s=score_id)
    assert violation(conn, "DELETE FROM vacancies WHERE id = :v", v=vacancy_id) is None
    insert(conn, "DELETE FROM vacancies WHERE id = :v", v=vacancy_id)
    for table in ("scores", "report_items", "feedback_events"):
        assert insert(conn, f"SELECT count(*) FROM {table} WHERE vacancy_id = :v", v=vacancy_id).scalar() == 0
