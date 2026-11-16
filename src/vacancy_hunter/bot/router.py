"""Команды бота шага A2: /start и /status.

Каждый ответ — сообщение в `outbox` с ключом «reply:<номер обновления>»: повторная доставка
того же обновления Telegram не создаёт второго ответа. После записи отправщик будится сразу,
поэтому ответ уходит за доли секунды (NFR-1: не позже 3 секунд). Долгие операции
(появятся на следующих шагах) запускаются в фоне и не держат обработчик.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, Update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.notify import OwnerOnlyMiddleware, enqueue
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.bot import texts
from vacancy_hunter.config import AppConfig

log = logging.getLogger("vacancy_hunter.bot")


@dataclass
class BotContext:
    engine: AsyncEngine
    owner_id: int | None
    timezone: str
    report_time: str
    wake_sender: Callable[[], None]
    clock: Clock = utcnow
    status_extra: Callable[[], Awaitable[list[str]]] | None = None
    config: AppConfig | None = None      # настройки отчёта и команды шага A7


async def reply(ctx: BotContext, update_id: int, chat_id: int, text: str) -> None:
    payload = {"text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    async with ctx.engine.begin() as conn:
        created = await enqueue(conn, dedup_key=f"reply:{update_id}", chat_id=chat_id, kind="system",
                                payload=payload, clock=ctx.clock)
    if created is not None:
        ctx.wake_sender()


def build_router(ctx: BotContext) -> Router:
    router = Router(name="a2")

    @router.message(CommandStart())
    async def cmd_start(message: Message, event_update: Update) -> None:
        log.info("bot_command", extra={"command": "start"})
        report_time = ctx.report_time
        if ctx.config is not None:
            from vacancy_hunter.reporting.settings import load_settings

            report_time = (await load_settings(ctx.engine, ctx.config)).report_time
        await reply(ctx, event_update.update_id, message.chat.id, texts.start_text(report_time, ctx.timezone))

    @router.message(Command("status"))
    async def cmd_status(message: Message, event_update: Update) -> None:
        log.info("bot_command", extra={"command": "status"})
        text = await texts.status_text(ctx.engine, ctx.timezone, ctx.clock, ctx.status_extra)
        await reply(ctx, event_update.update_id, message.chat.id, text)

    return router


def build_fallback_router(ctx: BotContext) -> Router:
    router = Router(name="fallback")

    @router.message()
    async def other(message: Message, event_update: Update) -> None:
        await reply(ctx, event_update.update_id, message.chat.id, texts.UNKNOWN_COMMAND)

    return router


class CallbackErrorMiddleware:
    """Сбой обработчика кнопки не оставляет нажатие без ответа (TECHNICAL_SPEC.md, 16.1)."""

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception:
            log.exception("callback_failed", extra={"data": getattr(event, "data", None)})
            return event.answer("Ошибка, попробуйте позже")


def build_dispatcher(ctx: BotContext) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware(ctx.owner_id))
    dp.callback_query.middleware(CallbackErrorMiddleware())
    dp.include_router(build_router(ctx))
    if ctx.config is not None:
        from vacancy_hunter.bot.handlers import build_a7_router

        dp.include_router(build_a7_router(ctx))
    dp.include_router(build_fallback_router(ctx))      # последним: иначе перехватит ввод настроек
    return dp
