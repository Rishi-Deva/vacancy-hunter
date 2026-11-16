"""Команды и кнопки шага A7 (TECHNICAL_SPEC.md, раздел 16).

- каждая команда и нажатие подтверждаются сразу: ответ на нажатие — всплывающая подсказка
  (метод возвращается из обработчика), сообщения — через `outbox` с ключом обновления;
- /today — копии сообщений сегодняшнего отчёта из `outbox` по составу `report_items`, включая
  неподтверждённые; фильтр по типу меняет только показ;
- /settings — порог, число карточек, время отчёта, пауза; ввод значения — следующим сообщением;
- /stats — показатели за 7 и 30 дней раздельно по типам;
- «Письмо», «Пересмотреть», /search, /feedback — этап B: ответ «появится на следующем этапе».
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, Update
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hunter_core.notify import enqueue, enqueue_copies
from hunter_core.scheduler.jobs import local_now
from hunter_core.storage.tables import app_state, outbox
from vacancy_hunter.reporting import feedback as fb
from vacancy_hunter.reporting.cards import render_details
from vacancy_hunter.reporting.rescored import rescored_list
from vacancy_hunter.reporting.settings import SettingError, confirmation, load_settings, parse, save_setting
from vacancy_hunter.reporting.stats import FILTERS, TYPE_TITLE, filter_keyboard, stats_text
from vacancy_hunter.storage.tables import (
    extractions,
    profile_versions,
    report_items,
    reports,
    scores,
    scoring_versions,
    vacancies,
    vacancy_sources,
)

log = logging.getLogger("vacancy_hunter.bot")

LATER = "Появится на следующем этапе."
AWAIT_KEY = "bot.await_setting"
AWAIT_MINUTES = 10
SETTING_PROMPT = {"threshold": "Введите порог показа — число от 50 до 90.",
                  "max_cards": "Введите число карточек в отчёте — от 1 до 15.",
                  "report_time": "Введите время отчёта ЧЧ:ММ с шагом 5 минут, например 07:00."}
LIST_TITLE = {"interested": "Интересные", "applied": "Отклики"}


MESSAGE_LIMIT = 4000       # запас до предела Telegram 4096


def fit_lines(lines: list[str], limit: int = MESSAGE_LIMIT) -> str:
    """Целые строки (каждая — законченный HTML), пока помещаются; остальные — «и ещё N»."""
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > limit - 20:
            out.append(f"… и ещё {len(lines) - i}")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _payload(text: str, markup: dict | None) -> dict:
    payload = {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if markup:
        payload["reply_markup"] = markup
    return payload


async def send(ctx, key: str, chat_id: int, text: str, markup: dict | None = None, conn=None) -> None:
    """Ответ через outbox. С conn — в той же транзакции, что и действие (DATABASE_SCHEMA.md, 7.3)."""
    if conn is not None:
        await enqueue(conn, dedup_key=key, chat_id=chat_id, kind="system", payload=_payload(text, markup),
                      clock=ctx.clock)
        return
    async with ctx.engine.begin() as own:
        created = await enqueue(own, dedup_key=key, chat_id=chat_id, kind="system", payload=_payload(text, markup),
                                clock=ctx.clock)
    if created is not None:
        ctx.wake_sender()


# ---------------------------------------------------------------- /today
async def today_messages(ctx, chat_id: int, request_key: str, offer_filter: str | None) -> str | None:
    """Копии сообщений сегодняшнего отчёта. None — отчёта ещё нет (возвращается текст ответа)."""
    day = local_now(ctx.clock, ctx.timezone).date()
    async with ctx.engine.begin() as conn:
        report_id = (await conn.execute(select(reports.c.id).where(reports.c.kind == "daily",
                                                                   reports.c.report_date == day))).scalar()
        if report_id is None:
            settings = await load_settings(conn, ctx.config)
            if settings.paused:
                return "Отчёты на паузе. Включить — /settings."
            from vacancy_hunter.reporting.daily import report_window

            _, end = report_window(ctx.config, settings.report_time)
            if local_now(ctx.clock, ctx.timezone).time().replace(tzinfo=None) >= end:
                return "Сегодня отчёт не формировался: приложение не работало в его время. Следующий — завтра в " \
                       f"{settings.report_time} по времени Грузии."
            return f"Сегодняшний отчёт ещё не сформирован. Он выйдет в {settings.report_time} по времени Грузии."
        header = (await conn.execute(select(outbox.c.id).where(outbox.c.dedup_key == f"report:{report_id}:header")))\
            .scalar()
        query = (select(outbox.c.id).join(report_items, report_items.c.id == outbox.c.report_item_id)
                 .join(vacancies, vacancies.c.id == report_items.c.vacancy_id)
                 .where(report_items.c.report_id == report_id, ~outbox.c.dedup_key.contains(":req:"))
                 .order_by(report_items.c.block, report_items.c.position))
        if offer_filter:
            query = query.where(vacancies.c.offer_type == offer_filter)
        ids = ([header] if header else []) + list((await conn.execute(query)).scalars())
        created = await enqueue_copies(conn, ids, request_key=request_key, clock=ctx.clock)
    if created:
        ctx.wake_sender()
    return None


def build_a7_router(ctx) -> Router:
    router = Router(name="a7")

    @router.message(Command("today"))
    async def cmd_today(message: Message, event_update: Update) -> None:
        log.info("bot_command", extra={"command": "today"})
        text = await today_messages(ctx, message.chat.id, f"u{event_update.update_id}", None)
        if text:
            await send(ctx, f"reply:{event_update.update_id}", message.chat.id, text)
        else:
            await send(ctx, f"reply:{event_update.update_id}", message.chat.id, "Показать по типу:",
                       {"inline_keyboard": filter_keyboard("today")})

    @router.callback_query(F.data.startswith("today:"))
    async def cb_today(callback: CallbackQuery):
        choice = callback.data.split(":", 1)[1]
        if choice not in FILTERS:
            return callback.answer("Неизвестный фильтр")
        text = await today_messages(ctx, callback.from_user.id, f"c{callback.id}", FILTERS[choice])
        if text:
            return callback.answer(text[:190], show_alert=True)
        return callback.answer("Отчёт: " + ("все типы" if choice == "all" else TYPE_TITLE[choice].lower()))

    # ------------------------------------------------------------ /settings
    async def settings_text_and_markup() -> tuple[str, dict]:
        s = await load_settings(ctx.engine, ctx.config)
        text = ("<b>Настройки</b>\n"
                f"Порог показа: {s.threshold}\nКарточек в отчёте: {s.max_cards}\n"
                f"Время отчёта: {s.report_time} по времени Грузии\nПауза: {'включена' if s.paused else 'выключена'}")
        markup = {"inline_keyboard": [
            [{"text": "Порог", "callback_data": "set:threshold"}, {"text": "Карточки", "callback_data": "set:max_cards"},
             {"text": "Время", "callback_data": "set:report_time"}],
            [{"text": "Снять паузу" if s.paused else "Пауза", "callback_data": f"set:paused:{'off' if s.paused else 'on'}"}],
        ]}
        return text, markup

    @router.message(Command("settings"))
    async def cmd_settings(message: Message, event_update: Update) -> None:
        text, markup = await settings_text_and_markup()
        await send(ctx, f"reply:{event_update.update_id}", message.chat.id, text, markup)

    @router.callback_query(F.data.startswith("set:"))
    async def cb_set(callback: CallbackQuery):
        parts = callback.data.split(":")
        name = parts[1]
        now = ctx.clock()
        if name == "paused" and len(parts) == 3:
            try:
                value = parse("paused", parts[2])
            except SettingError:
                return callback.answer("Неверная кнопка")
            async with ctx.engine.begin() as conn:
                await save_setting(conn, "paused", value, now)
                await send(ctx, f"cb:{callback.id}", callback.from_user.id, html.escape(confirmation("paused", value)),
                           conn=conn)
            ctx.wake_sender()
            return callback.answer(confirmation("paused", value)[:190])
        if name not in SETTING_PROMPT:
            return callback.answer("Неизвестная настройка")
        async with ctx.engine.begin() as conn:
            await conn.execute(pg_insert(app_state).values(
                key=AWAIT_KEY, value={"name": name, "at": now.isoformat()}, updated_at=now,
            ).on_conflict_do_update(index_elements=[app_state.c.key],
                                    set_={"value": {"name": name, "at": now.isoformat()}, "updated_at": now}))
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, SETTING_PROMPT[name])
        return callback.answer("Жду значение")

    # ------------------------------------------------------------ /stats и списки
    @router.message(Command("stats"))
    async def cmd_stats(message: Message, event_update: Update) -> None:
        async with ctx.engine.connect() as conn:
            text = await stats_text(conn, ctx.clock())
        markup = {"inline_keyboard": filter_keyboard("stats") + [[
            {"text": "Интересные", "callback_data": "list:interested"},
            {"text": "Отклики", "callback_data": "list:applied"}]]}
        await send(ctx, f"reply:{event_update.update_id}", message.chat.id, text, markup)

    @router.callback_query(F.data.startswith("stats:"))
    async def cb_stats(callback: CallbackQuery):
        choice = callback.data.split(":", 1)[1]
        if choice not in FILTERS:
            return callback.answer("Неизвестный фильтр")
        async with ctx.engine.connect() as conn:
            text = await stats_text(conn, ctx.clock(), FILTERS[choice])
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, text)
        return callback.answer()

    @router.callback_query(F.data.startswith("list:"))
    async def cb_list(callback: CallbackQuery):
        parts = callback.data.split(":")
        state = parts[1]
        offer_filter = FILTERS.get(parts[2]) if len(parts) > 2 else None
        if state not in LIST_TITLE:
            return callback.answer("Неизвестный список")
        async with ctx.engine.connect() as conn:
            query = (select(vacancies.c.id, vacancies.c.title, vacancies.c.company_name, vacancies.c.canonical_url,
                            vacancies.c.user_state_at)
                     .where(vacancies.c.user_state == state).order_by(vacancies.c.user_state_at.desc()).limit(30))
            if offer_filter:
                query = query.where(vacancies.c.offer_type == offer_filter)
            rows = (await conn.execute(query)).mappings().all()
        lines = [f"<b>{LIST_TITLE[state]}</b>" + ("" if not offer_filter else
                                                   f" — {html.escape(TYPE_TITLE[offer_filter].lower())}")]
        lines += [vacancy_line(r) for r in rows] or ["пока пусто"]
        text = fit_lines(lines)
        markup = {"inline_keyboard": [[{"text": t, "callback_data": f"list:{state}:{k}"} for k, t in
                                       (("all", "Все"), ("job", "Работа"), ("internship", "Стажировки"),
                                        ("project", "Проекты"))]]}
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, text, markup)
        return callback.answer()

    @router.callback_query(F.data.startswith("rs:"))
    async def cb_rescored(callback: CallbackQuery):
        try:
            _, since, until = callback.data.split(":")
            since_ts, until_ts = int(since), int(until)
        except ValueError:
            return callback.answer("Неверная кнопка")
        async with ctx.engine.connect() as conn:
            rows = await rescored_list(conn, since_ts, until_ts)
        lines = ["<b>Дооценённые вакансии</b>"] + [f"{r['total']} · " + vacancy_line(r) for r in rows]
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, fit_lines(lines))
        return callback.answer()

    # ------------------------------------------------------------ кнопки карточки
    @router.callback_query(F.data.startswith("fb:"))
    async def cb_feedback(callback: CallbackQuery):
        try:
            _, event, vid = callback.data.split(":")
            vacancy_id = int(vid)
        except ValueError:
            return callback.answer("Неверная кнопка")
        if event not in fb.EVENT_STATE or event == "rejected":
            return callback.answer("Неверная кнопка")
        result = await fb.record(ctx.engine, vacancy_id, event, clock=ctx.clock)
        return callback.answer(_feedback_answer(result, event))

    @router.callback_query(F.data.startswith("rj:"))
    async def cb_reject(callback: CallbackQuery):
        vid = callback.data.split(":", 1)[1]
        if not vid.isdigit():
            return callback.answer("Неверная кнопка")
        rows, row = [], []
        for code, text in fb.REASONS.items():
            row.append({"text": text, "callback_data": f"rjr:{code}:{vid}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, "Причина отказа:", {"inline_keyboard": rows})
        return callback.answer("Выберите причину")

    @router.callback_query(F.data.startswith("rjr:"))
    async def cb_reject_reason(callback: CallbackQuery):
        try:
            _, reason, vid = callback.data.split(":")
            vacancy_id = int(vid)
        except ValueError:
            return callback.answer("Неверная кнопка")
        if reason not in fb.REASONS:
            return callback.answer("Неверная причина")
        result = await fb.record(ctx.engine, vacancy_id, "rejected", reason=reason, clock=ctx.clock)
        return callback.answer(_feedback_answer(result, "rejected"))

    @router.callback_query(F.data.startswith("more:"))
    async def cb_more(callback: CallbackQuery):
        vid = callback.data.split(":", 1)[1]
        if not vid.isdigit():
            return callback.answer("Неверная кнопка")
        text = await details_text(ctx, int(vid))
        if text is None:
            return callback.answer("Вакансия не найдена")
        await send(ctx, f"cb:{callback.id}", callback.from_user.id, text)
        return callback.answer()

    @router.callback_query(F.data.startswith("later:"))
    async def cb_later(callback: CallbackQuery):
        return callback.answer(LATER)

    @router.message(Command("search", "feedback"))
    async def cmd_later(message: Message, event_update: Update) -> None:
        await send(ctx, f"reply:{event_update.update_id}", message.chat.id, LATER)

    # ------------------------------------------------------------ ввод значения настройки
    @router.message(F.text & ~F.text.startswith("/"))
    async def setting_input(message: Message, event_update: Update) -> None:
        now = ctx.clock()
        async with ctx.engine.begin() as conn:
            waiting = (await conn.execute(select(app_state.c.value).where(app_state.c.key == AWAIT_KEY)
                                          .with_for_update())).scalar()
            fresh = isinstance(waiting, dict) and \
                now - datetime.fromisoformat(waiting["at"]) <= timedelta(minutes=AWAIT_MINUTES)
            if not fresh:
                text = "Не знаю такой команды. Список команд — /start"
            else:
                name = waiting["name"]
                try:
                    value = parse(name, message.text)
                except SettingError as exc:
                    text = str(exc)             # ожидание остаётся: можно ввести снова
                else:
                    await save_setting(conn, name, value, now)
                    await conn.execute(app_state.delete().where(app_state.c.key == AWAIT_KEY))
                    text = confirmation(name, value)
            await send(ctx, f"reply:{event_update.update_id}", message.chat.id, html.escape(text), conn=conn)
        ctx.wake_sender()

    return router


def _feedback_answer(result: str, event: str) -> str:
    if result == fb.MISSING:
        return "Вакансия не найдена"
    if result == fb.ALREADY:
        return "Отметки нет" if event == "cleared" else "Уже отмечено"
    return fb.EVENT_TEXT[event]


def vacancy_line(row) -> str:
    title = html.escape((row["title"] or "Без названия")[:150])
    company = f" — {html.escape(row['company_name'][:80])}" if row.get("company_name") else ""
    if row.get("canonical_url"):
        return f'<a href="{html.escape(row["canonical_url"], quote=True)}">{title}</a>{company}'
    return title + company


async def details_text(ctx, vacancy_id: int) -> str | None:
    async with ctx.engine.connect() as conn:
        vacancy = (await conn.execute(select(vacancies).where(vacancies.c.id == vacancy_id))).mappings().first()
        if vacancy is None:
            return None
        rows = (await conn.execute(select(scores).where(scores.c.vacancy_id == vacancy_id,
                                                        scores.c.state != "outdated"))).mappings().all()
        by_kind = {r["kind"]: dict(r) for r in rows}
        score = by_kind.get("full") or by_kind.get("preliminary")
        if score is None:
            return f"<b>{html.escape(vacancy['title'])}</b>\nОценки пока нет."
        versions = (await conn.execute(select(
            select(profile_versions.c.version_no).where(profile_versions.c.id == score["profile_version_id"])
            .scalar_subquery(),
            select(scoring_versions.c.version_no).where(scoring_versions.c.id == score["scoring_version_id"])
            .scalar_subquery(),
        ))).first()
        sources = list((await conn.execute(select(vacancy_sources.c.source_code)
                                           .where(vacancy_sources.c.vacancy_id == vacancy_id)
                                           .order_by(vacancy_sources.c.id))).scalars())
        facts = None
        if score.get("extraction_id"):
            facts = (await conn.execute(select(extractions.c.facts).where(
                extractions.c.id == score["extraction_id"]))).scalar()
    return render_details(dict(vacancy), score, versions=tuple(versions) if versions else None,
                          sources=list(dict.fromkeys(sources)), facts=facts)


__all__ = ["build_a7_router", "details_text", "today_messages"]
