"""Допуск к боту только владельца (ARCHITECTURE.md, раздел 17; FR-26).

Обращения с других идентификаторов игнорируются: ответа нет, в журнал пишется
идентификатор и вид обращения. Пока идентификатор владельца не задан, бот не отвечает
никому — так владелец может узнать свой идентификатор из журнала.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

log = logging.getLogger("hunter_core.access")


def _sender_id(update: Update) -> int | None:
    for part in (update.message, update.edited_message, update.callback_query, update.inline_query,
                 update.my_chat_member):
        user = getattr(part, "from_user", None)
        if user is not None:
            return user.id
    return None


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int | None):
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _sender_id(event) if isinstance(event, Update) else None
        if self.owner_id is None or user_id != self.owner_id:
            log.warning("bot_access_denied", extra={
                "user_id": user_id,
                "update_type": getattr(event, "event_type", None),
                "hint": None if self.owner_id is not None else "TELEGRAM_OWNER_ID не задан",
            })
            return None
        return await handler(event, data)
