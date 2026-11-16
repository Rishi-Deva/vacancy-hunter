"""Правила удаления для связей, которые иначе блокируют очистку.

DATABASE_SCHEMA.md, раздел 12, шаг 5: вакансия удаляется вместе с оценками,
а элементы отчётов — только на шаге 8. Без правил удаления база отклонила бы
удаление показанной вакансии. Решение (13.11.2026):
  - report_items.vacancy_id → удаляется вместе с вакансией (CASCADE);
  - report_items.score_id, feedback_events.score_id → обнуляются (SET NULL).

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (таблица, столбец, таблица-цель, новое правило)
CHANGES = [
    ("report_items", "vacancy_id", "vacancies", "CASCADE"),
    ("report_items", "score_id", "scores", "SET NULL"),
    ("feedback_events", "score_id", "scores", "SET NULL"),
]


def _replace(ondelete_by_row) -> None:
    for table, column, target, rule in CHANGES:
        name = f"fk_{table}_{column}_{target}"
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, target, [column], ["id"], ondelete=ondelete_by_row(rule))


def upgrade() -> None:
    _replace(lambda rule: rule)


def downgrade() -> None:
    _replace(lambda rule: None)
