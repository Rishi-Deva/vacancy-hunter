"""Подставные компоненты для тестов: часы, отправщик Telegram, HTTP, обновления бота."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from hunter_core.notify import SendOutcomeUnknown, SendRefused


class FakeClock:
    """Часы, которые идут только по команде теста."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 11, 13, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeSender:
    """Записывает отправленные сообщения. Сценарий: список исключений или None по попыткам."""

    def __init__(self, script: list[BaseException | None] | None = None,
                 on_send: Callable[[int, Mapping[str, Any]], Any] | None = None):
        self.script = list(script or [])
        self.on_send = on_send
        self.sent: list[tuple[int, dict]] = []
        self.calls = 0

    async def send(self, chat_id: int, payload: Mapping[str, Any]) -> int:
        self.calls += 1
        if self.on_send is not None:
            result = self.on_send(chat_id, payload)
            if asyncio.iscoroutine(result):
                await result
        outcome = self.script.pop(0) if self.script else None
        if outcome is not None:
            raise outcome
        self.sent.append((chat_id, dict(payload)))
        return 1000 + len(self.sent)


def refused(retry_after: float | None = None) -> SendRefused:
    return SendRefused("Too Many Requests", retry_after=retry_after)


def unknown() -> SendOutcomeUnknown:
    return SendOutcomeUnknown("ReadTimeout")


class FakeSleep:
    """Подмена asyncio.sleep: запоминает паузы и двигает подставные монотонные часы."""

    def __init__(self):
        self.pauses: list[float] = []
        self.t = 1000.0

    async def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.t += seconds

    def monotonic(self) -> float:
        return self.t


def scripted_transport(responses: list[httpx.Response | Exception], seen: list[httpx.Request] | None = None):
    """HTTP без сети: отдаёт ответы по порядку; исключение имитирует сетевую ошибку."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler)


def make_message_update(update_id: int, user_id: int, text: str):
    from aiogram.types import Chat, Message, MessageEntity, Update, User

    entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") \
        else None
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime(2026, 11, 13, 6, 0, tzinfo=UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            text=text,
            entities=entities,
        ),
    )


def fake_bot():
    from aiogram import Bot

    return Bot("123456:TEST-token-not-real")


def make_callback_update(update_id: int, user_id: int, data: str, callback_id: str | None = None):
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    user = User(id=user_id, is_bot=False, first_name="Test")
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=callback_id or f"cb{update_id}", from_user=user, chat_instance="ci", data=data,
            message=Message(message_id=1, date=datetime(2026, 11, 13, 6, 0, tzinfo=UTC),
                            chat=Chat(id=user_id, type="private"), text="карточка"),
        ),
    )
