"""OUT-1…OUT-5: исходящие сообщения (TEST_PLAN.md, раздел 4.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from hunter_core.notify import MAX_ATTEMPTS, OutboxSender, enqueue, enqueue_copies
from hunter_core.scheduler import recover_core_state
from hunter_core.storage.tables import outbox
from tests.fakes import FakeClock, FakeSender, refused, unknown

pytestmark = pytest.mark.integration

PAYLOAD = {"text": "Сводка"}


async def put(engine, key="report:1:header", clock=None, **kw):
    async with engine.begin() as conn:
        return await enqueue(conn, dedup_key=key, chat_id=42, kind="report_header", payload=PAYLOAD,
                             clock=clock or FakeClock(), **kw)


async def rows(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(outbox).order_by(outbox.c.id))).mappings()]


async def test_normal_flow_pending_sending_sent(aengine):
    states_during_send = []

    async def check_state(chat_id, payload):
        states_during_send.extend(r["state"] for r in await rows(aengine))

    clock = FakeClock()
    await put(aengine, clock=clock)
    sender = FakeSender(on_send=check_state)
    result = await OutboxSender(aengine, sender, clock=clock).send_once()
    assert result["sent"] == 1
    assert states_during_send == ["sending"]  # перед вызовом Telegram запись уже в «отправляется»
    (row,) = await rows(aengine)
    assert row["state"] == "sent" and row["telegram_message_id"] == 1001 and row["attempts"] == 1
    assert sender.sent == [(42, PAYLOAD)]
    # повторный проход ничего не отправляет
    assert (await OutboxSender(aengine, sender, clock=clock).send_once())["sent"] == 0


# ------------------------------------------------------------------ OUT-1
async def test_out1_same_key_creates_no_second_row(aengine):
    assert await put(aengine) is not None
    assert await put(aengine) is None
    assert len(await rows(aengine)) == 1


# ------------------------------------------------------------------ OUT-2
async def test_out2_rate_limit_retried_later_at_most_five_then_failed(aengine):
    clock = FakeClock()
    await put(aengine, clock=clock)
    sender = FakeSender(script=[refused(retry_after=30)] * 10)
    out = OutboxSender(aengine, sender, clock=clock)

    assert (await out.send_once())["retry"] == 1
    (row,) = await rows(aengine)
    assert row["state"] == "pending" and row["attempts"] == 1
    assert (row["next_attempt_at"] - clock()).total_seconds() == 30   # просьба Telegram подождать учтена
    assert (await out.send_once())["retry"] == 0                       # раньше срока не повторяется

    for _ in range(10):
        clock.advance(minutes=15)
        await out.send_once()
    (row,) = await rows(aengine)
    assert row["state"] == "failed"
    assert row["attempts"] == MAX_ATTEMPTS == 5
    assert sender.calls == 5


async def test_out2_retry_pause_grows(aengine):
    clock = FakeClock()
    await put(aengine, clock=clock)
    out = OutboxSender(aengine, FakeSender(script=[refused()] * 3), clock=clock)
    delays = []
    for _ in range(3):
        await out.send_once()
        (row,) = await rows(aengine)
        delays.append((row["next_attempt_at"] - clock()).total_seconds())
        clock.advance(minutes=10)
    assert delays == [5, 10, 20]


# ------------------------------------------------------------------ OUT-3
async def test_out3_stop_between_sending_and_confirmation_gives_unconfirmed_no_resend(aengine):
    clock = FakeClock()
    await put(aengine, clock=clock)
    out = OutboxSender(aengine, FakeSender(), clock=clock)
    claimed = await out._claim()            # запись переведена в «отправляется»…
    assert len(claimed) == 1                # …и процесс «упал» до подтверждения
    result = await recover_core_state(aengine, clock=clock)
    assert result["outbox_unconfirmed"] == 1
    sender = FakeSender()
    clock.advance(hours=1)
    assert (await OutboxSender(aengine, sender, clock=clock).send_once())["sent"] == 0
    assert sender.calls == 0
    (row,) = await rows(aengine)
    assert row["state"] == "unconfirmed"


async def test_out3_unknown_outcome_during_send_is_unconfirmed(aengine):
    clock = FakeClock()
    await put(aengine, clock=clock)
    sender = FakeSender(script=[unknown()])
    out = OutboxSender(aengine, sender, clock=clock)
    assert (await out.send_once())["unconfirmed"] == 1
    clock.advance(hours=1)
    await out.send_once()
    assert sender.calls == 1
    (row,) = await rows(aengine)
    assert row["state"] == "unconfirmed"


# ------------------------------------------------------------------ OUT-4
async def test_out4_rolled_back_event_leaves_no_message(aengine):
    async with aengine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(text("INSERT INTO reports (kind, report_date) VALUES ('daily', '2026-11-13')"))
        await enqueue(conn, dedup_key="report:1:header", chat_id=42, kind="report_header", payload=PAYLOAD)
        await tx.rollback()
    assert await rows(aengine) == []


# ------------------------------------------------------------------ OUT-5
async def test_out5_reissue_creates_new_rows_and_keeps_originals(aengine):
    clock = FakeClock()
    first = await put(aengine, key="report:1:header", clock=clock)
    second = await put(aengine, key="report:1:card:7", clock=clock)
    await OutboxSender(aengine, FakeSender(script=[None, unknown()]), clock=clock).send_once()
    before = {r["id"]: r for r in await rows(aengine)}

    async with aengine.begin() as conn:
        created = await enqueue_copies(conn, [first, second], request_key="upd:555", clock=clock)
    async with aengine.begin() as conn:  # тот же запрос второй раз — копий нет
        assert await enqueue_copies(conn, [first, second], request_key="upd:555", clock=clock) == []

    after = {r["id"]: r for r in await rows(aengine)}
    assert len(created) == 2 and len(after) == 4
    for original_id, original in before.items():
        assert after[original_id] == original           # исходные строки не изменены
    keys = {after[i]["dedup_key"] for i in created}
    assert keys == {"report:1:header:req:upd:555", "report:1:card:7:req:upd:555"}
    assert all(after[i]["state"] == "pending" for i in created)
