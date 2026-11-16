"""Тексты ответов бота. Всё, что приходит не из кода (ошибки, имена), экранируется."""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.storage.tables import app_state, job_runs, outbox, source_state
from hunter_core.timeutil import Clock
from vacancy_hunter.storage.tables import JOB_NAMES

UNKNOWN_COMMAND = "Не знаю такой команды. Список команд — /start"

COMMANDS = [
    ("/today", "сегодняшний отчёт"),
    ("/search", "ручной поиск"),
    ("/settings", "настройки"),
    ("/feedback", "отзыв о подборе"),
    ("/stats", "статистика"),
    ("/status", "состояние системы"),
]

STATE_RU = {"pending": "ожидают", "sending": "отправляются", "sent": "отправлено", "failed": "ошибка",
            "unconfirmed": "не подтверждено"}
MODE_RU = {"active": "активен", "quiet": "тихий сбор", "waiting": "ожидает", "disabled": "отключён"}
HEALTH_RU = {"ok": "", "degraded": " ⚠️ деградировал", "blocked": " ⛔ запрет доступа"}
VACANCY_STATUS_RU = {
    "new": "новые", "awaiting_description": "ждут описания", "duplicate": "дубли",
    "rejected_rules": "отклонены правилами", "pending_llm": "ждут анализа", "llm_failed": "ошибка анализа",
    "extracted": "проанализированы", "rejected_gate": "отклонены фильтрами", "scored": "оценены",
    "reported": "показаны", "closed": "закрыты",
}
JOB_STATUS_RU = {"running": "выполняется", "success": "успешно", "failed": "ошибка", "abandoned": "прервана"}


def start_text(report_time: str, timezone: str) -> str:
    lines = [
        "<b>AI Vacancy Hunter</b>",
        "Каждый день собираю IT-вакансии, стажировки и разовые проекты, оцениваю их по вашему профилю "
        f"и присылаю отчёт в {html.escape(report_time)} по времени Грузии.",
        "",
        "Команды:",
        *[f"{cmd} — {desc}" for cmd, desc in COMMANDS],
        "",
        "Под каждой карточкой — кнопки: интересно, откликнулся, отклонить, подробнее. "
        "Письма, поиск и отзывы появятся на следующем этапе.",
    ]
    return "\n".join(lines)


def _fmt(dt: datetime | None, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%d.%m %H:%M") if dt else "—"


async def status_text(engine: AsyncEngine, timezone: str, clock: Clock,
                      extra: Callable[[], Awaitable[list[str]]] | None = None) -> str:
    tz = ZoneInfo(timezone)
    lines = [f"<b>Состояние</b> на {_fmt(clock(), tz)}"]
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            last_runs = {}
            for name in JOB_NAMES:
                row = (await conn.execute(
                    select(job_runs.c.status, job_runs.c.started_at, job_runs.c.finished_at, job_runs.c.error_text)
                    .where(job_runs.c.job_name == name).order_by(job_runs.c.id.desc()).limit(1)
                )).mappings().first()
                if row:
                    last_runs[name] = row
            counts = dict((await conn.execute(
                select(outbox.c.state, func.count()).group_by(outbox.c.state)
            )).all())
            sources = (await conn.execute(
                select(source_state).order_by(source_state.c.mode, source_state.c.code)
            )).mappings().all()
            from vacancy_hunter.storage.tables import vacancies

            vacancy_counts = dict((await conn.execute(
                select(vacancies.c.status, func.count()).group_by(vacancies.c.status)
            )).all())
            from vacancy_hunter.storage.tables import profile_versions, scores, scoring_versions

            versions = (await conn.execute(select(
                select(profile_versions.c.version_no).where(profile_versions.c.is_current).scalar_subquery(),
                select(scoring_versions.c.version_no).where(scoring_versions.c.is_current).scalar_subquery(),
            ))).first()
            recalc = dict((await conn.execute(select(scores.c.state, func.count())
                                              .where(scores.c.state.in_(("pending_recalc", "recalc_failed")))
                                              .group_by(scores.c.state))).all())
            heartbeat = (await conn.execute(
                select(app_state.c.value).where(app_state.c.key == "heartbeat_last_ok")
            )).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        return "\n".join([*lines, f"База: <b>недоступна</b> ({html.escape(type(exc).__name__)})"])

    lines.append("База: работает")
    lines.append("")
    lines.append("<b>Задачи</b>")
    if not last_runs:
        lines.append("запусков пока не было")
    for name, row in last_runs.items():
        status = JOB_STATUS_RU.get(row["status"], row["status"])
        line = f"{name}: {status}, {_fmt(row['finished_at'] or row['started_at'], tz)}"
        if row["status"] in ("failed", "abandoned") and row["error_text"]:
            line += f" — {html.escape(row['error_text'][:120])}"
        lines.append(line)
    lines.append("")
    lines.extend(sources_lines(sources, tz))
    lines.append("")
    total = sum(vacancy_counts.values())
    lines.append(f"<b>Вакансии</b>: всего {total}" + ("" if not total else " (" + ", ".join(
        f"{VACANCY_STATUS_RU.get(k, html.escape(k))}: {v}" for k, v in sorted(vacancy_counts.items())) + ")"))
    if versions and versions[0]:
        line = f"Версия профиля {versions[0]}, правил {versions[1]}"
        if recalc:
            line += (f"; ожидают пересчёта {recalc.get('pending_recalc', 0)}, "
                     f"ошибок пересчёта {recalc.get('recalc_failed', 0)}")
        lines.append(line)
    lines.append("")
    lines.append("<b>Сообщения</b>")
    lines.append(", ".join(f"{STATE_RU[s]}: {counts.get(s, 0)}" for s in STATE_RU))
    lines.append("")
    lines.append(f"Пульс: {'последний ' + html.escape(str(heartbeat)) if heartbeat else 'не отправлялся или отключён'}")
    if extra is not None:
        lines.extend(await extra())
    return "\n".join(lines)


def sources_lines(sources, tz: ZoneInfo) -> list[str]:
    """Состояние источников (SRC-14): режим, здоровье, последний успех, последняя ошибка."""
    if not sources:
        return ["<b>Источники</b>: сбор ещё не запускался"]
    by_mode: dict[str, int] = {}
    for row in sources:
        by_mode[row["mode"]] = by_mode.get(row["mode"], 0) + 1
    lines = ["<b>Источники</b>: " + ", ".join(f"{MODE_RU.get(m, m)} {n}" for m, n in sorted(by_mode.items()))]
    for row in sources:
        problem = row["health"] != "ok" or row["consecutive_failures"] or row["mode"] == "disabled"
        if row["mode"] in ("active", "quiet") and not problem:
            line = (f"{html.escape(row['code'])}: {MODE_RU[row['mode']]}, успех {_fmt(row['last_success_at'], tz)},"
                    f" публикаций {row['posts_total']}")
        else:
            line = (f"{html.escape(row['code'])}: {MODE_RU.get(row['mode'], row['mode'])}"
                    f"{HEALTH_RU.get(row['health'], '')}, успех {_fmt(row['last_success_at'], tz)}")
            if row["consecutive_failures"]:
                line += f", сбоев подряд {row['consecutive_failures']}"
            if row["last_error"]:
                line += f" — {html.escape(row['last_error'][:120])}"
        lines.append(line)
    return lines
