"""Исходящие сообщения через `outbox` (ARCHITECTURE.md, раздел 12; DATABASE_SCHEMA.md, 7.3).

Правила:
1. Сообщение создаётся в той же транзакции, что и событие, с уникальным смысловым ключом.
   Повторное создание с тем же ключом ничего не делает (вторая строка не появляется).
2. Состояния: pending → sending → sent | failed; unconfirmed — исход неизвестен.
3. Перед вызовом Telegram запись переводится в `sending` и это фиксируется в базе.
4. Явный отказ Telegram или сетевая ошибка до отправки — повтор с нарастающей паузой,
   не более 5 попыток, затем `failed`.
5. Обрыв после отправки запроса — исход неизвестен: `unconfirmed`, автоматического повтора нет.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.storage.tables import outbox
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.outbox")

MAX_ATTEMPTS = 5


class SendRefused(Exception):
    """Сообщение точно не доставлено: Telegram отказал или соединение не установлено.

    `retry_after` — пауза, которую попросил Telegram (секунды), если она известна.
    """

    def __init__(self, reason: str, retry_after: float | None = None):
        super().__init__(reason)
        self.retry_after = retry_after


class SendOutcomeUnknown(Exception):
    """Запрос мог дойти до Telegram, но подтверждения нет."""


class MessageSender(Protocol):
    async def send(self, chat_id: int, payload: Mapping[str, Any]) -> int:
        """Отправить сообщение, вернуть его номер в Telegram."""


async def enqueue(
    conn: AsyncConnection,
    *,
    dedup_key: str,
    chat_id: int,
    kind: str,
    payload: Mapping[str, Any],
    clock: Clock = utcnow,
    **extra: Any,
) -> int | None:
    """Поставить сообщение в очередь внутри текущей транзакции.

    Возвращает номер новой строки или None, если сообщение с таким ключом уже есть.
    Откат транзакции отменяет и сообщение.
    """
    now = clock()
    stmt = (
        pg_insert(outbox)
        .values(dedup_key=dedup_key, chat_id=chat_id, kind=kind, payload=dict(payload), state="pending",
                attempts=0, next_attempt_at=now, created_at=now, **extra)
        .on_conflict_do_nothing(index_elements=[outbox.c.dedup_key])
        .returning(outbox.c.id)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def enqueue_copies(
    conn: AsyncConnection, source_ids: list[int], *, request_key: str, clock: Clock = utcnow
) -> list[int]:
    """Повторная выдача по явному запросу пользователя (например, /today).

    Создаются новые строки с ключом «<исходный ключ>:req:<ключ запроса>».
    Исходные строки не меняются. Тот же запрос второй раз копий не создаёт.
    """
    rows = (await conn.execute(
        select(outbox).where(outbox.c.id.in_(source_ids)).order_by(outbox.c.id)
    )).mappings().all()
    created = []
    for row in rows:
        extra = {"report_item_id": row["report_item_id"]} if "report_item_id" in row else {}
        new_id = await enqueue(conn, dedup_key=f"{row['dedup_key']}:req:{request_key}", chat_id=row["chat_id"],
                               kind=row["kind"], payload=row["payload"], clock=clock, **extra)
        if new_id is not None:
            created.append(new_id)
    return created


def retry_delay(attempt: int, retry_after: float | None) -> timedelta:
    """Нарастающая пауза: 5, 10, 20, 40 секунд; просьба Telegram подождать важнее."""
    base = 5 * 2 ** (attempt - 1)
    if retry_after is not None:
        base = max(base, retry_after)
    return timedelta(seconds=min(base, 600))


class OutboxSender:
    def __init__(self, engine: AsyncEngine, sender: MessageSender, *, clock: Clock = utcnow,
                 interval: float = 10.0, batch: int = 20):
        self.engine = engine
        self.sender = sender
        self.clock = clock
        self.interval = interval
        self.batch = batch
        self._wake = asyncio.Event()
        self._stopping = False

    def wake(self) -> None:
        """Отправить без ожидания очередного интервала (ответы на команды)."""
        self._wake.set()

    async def _claim(self) -> list[dict]:
        """Взять порцию готовых к отправке сообщений и перевести их в `sending`."""
        now = self.clock()
        async with self.engine.begin() as conn:
            rows = (await conn.execute(
                select(outbox.c.id, outbox.c.chat_id, outbox.c.payload, outbox.c.attempts)
                .where(outbox.c.state == "pending",
                       or_(outbox.c.next_attempt_at.is_(None), outbox.c.next_attempt_at <= now))
                .order_by(outbox.c.id)
                .limit(self.batch)
                .with_for_update(skip_locked=True)
            )).mappings().all()
            if rows:
                await conn.execute(
                    update(outbox).where(outbox.c.id.in_([r["id"] for r in rows]))
                    .values(state="sending", sending_started_at=now)
                )
        return [dict(r) for r in rows]

    async def _mark(self, message_id: int, **values) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(outbox).where(and_(outbox.c.id == message_id,
                                                         outbox.c.state == "sending")).values(**values))

    async def send_once(self) -> dict[str, int]:
        """Один проход отправки. Возвращает число отправленных, отложенных, ошибок и неподтверждённых."""
        result = {"sent": 0, "retry": 0, "failed": 0, "unconfirmed": 0}
        for row in await self._claim():
            attempt = row["attempts"] + 1
            try:
                telegram_id = await self.sender.send(row["chat_id"], row["payload"])
            except SendRefused as exc:
                if attempt >= MAX_ATTEMPTS:
                    await self._mark(row["id"], state="failed", attempts=attempt, error_text=str(exc)[:1000])
                    result["failed"] += 1
                    log.error("outbox_failed", extra={"outbox_id": row["id"], "error": str(exc)})
                else:
                    await self._mark(row["id"], state="pending", attempts=attempt, error_text=str(exc)[:1000],
                                     next_attempt_at=self.clock() + retry_delay(attempt, exc.retry_after))
                    result["retry"] += 1
                    log.warning("outbox_retry", extra={"outbox_id": row["id"], "attempt": attempt})
            except SendOutcomeUnknown as exc:
                await self._mark(row["id"], state="unconfirmed", attempts=attempt, error_text=str(exc)[:1000])
                result["unconfirmed"] += 1
                log.warning("outbox_unconfirmed", extra={"outbox_id": row["id"]})
            else:
                await self._mark(row["id"], state="sent", attempts=attempt, telegram_message_id=telegram_id,
                                 sent_at=self.clock(), error_text=None)
                result["sent"] += 1
                log.info("outbox_sent", extra={"outbox_id": row["id"]})
        return result

    def stop(self) -> None:
        """Мягкая остановка: текущий проход завершается, новый не начинается."""
        self._stopping = True
        self._wake.set()

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                await self.send_once()
            except Exception:  # noqa: BLE001 — сбой базы не должен останавливать цикл
                log.exception("outbox_loop_error")
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except TimeoutError:
                pass
            self._wake.clear()
