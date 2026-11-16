"""BUD-1…BUD-13, EXT-1…EXT-13 с настоящей базой и подставным LLM (TEST_PLAN.md, раздел 4.5)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hunter_core.llm import Budget, BudgetExhausted, BudgetLimits, LlmGateway, ProviderRejected, Usage
from hunter_core.storage.tables import llm_budget, llm_ledger, source_state
from tests.fakes import FakeClock
from tests.fakes.llm import TEXT, FakeLlm, answer
from vacancy_hunter.config import load_app_config
from vacancy_hunter.pipeline.extract import Extractor, build_budget
from vacancy_hunter.pipeline.normalize import text_hash
from vacancy_hunter.pipeline.process import process
from vacancy_hunter.storage.tables import extractions, vacancies

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CONFIG = load_app_config()
PER_PART = 2     # схема P1 разделена на две половины: два вызова на часть текста (решение владельца, V2)
D = Decimal


def limits(monthly="3.00", letters="0.30", daily="0.08", quiet="0.50", warn1="1.50", warn2="2.50") -> BudgetLimits:
    return BudgetLimits(D(monthly), D(letters), D(daily), D(quiet), D(warn1), D(warn2))


def config_with(**budget):
    llm = CONFIG.llm.model_copy(update={"budget": CONFIG.llm.budget.model_copy(update=budget)})
    return replace(CONFIG, llm=llm)


async def add_vacancy(engine, *, text=TEXT, source="tg_a", quiet=False, mode="active") -> int:
    async with engine.begin() as conn:
        await conn.execute(pg_insert(source_state).values(code=source, kind="telegram", mode=mode)
                           .on_conflict_do_nothing())
        return (await conn.execute(insert(vacancies).values(
            title="Python-разработчик", status="pending_llm", has_description=True, description_text=text,
            description_hash=text_hash(text), quiet_only=quiet, primary_source_code=source,
            first_seen_at=NOW, last_seen_at=NOW, published_at=NOW,
        ).returning(vacancies.c.id))).scalar_one()


async def vacancy(engine, vid):
    async with engine.connect() as conn:
        return dict((await conn.execute(select(vacancies).where(vacancies.c.id == vid))).mappings().one())


async def ledger(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(llm_ledger).order_by(llm_ledger.c.id))).mappings()]


async def month_row(engine, month=NOW.date().replace(day=1)):
    async with engine.connect() as conn:
        return dict((await conn.execute(select(llm_budget).where(llm_budget.c.month == month))).mappings().one())


def extractor(engine, fake, config=CONFIG, clock=None):
    clock = clock or FakeClock(NOW)
    gateway = LlmGateway(build_budget(engine, config, clock=clock), fake)
    return Extractor(engine, gateway, config, clock=clock)


# ------------------------------------------------------------------ BUD-1
async def test_bud1_50_parallel_reservations_with_room_for_10(aengine):
    budget = Budget(aengine, limits(monthly="1.00", letters="0"), clock=FakeClock(NOW))

    async def one():
        try:
            await budget.reserve(purpose="letter", model="m", amount=D("0.10"), attempt_no=1)
            return True
        except BudgetExhausted:
            return False

    results = await asyncio.gather(*(one() for _ in range(50)))
    assert sum(results) == 10
    row = await month_row(aengine)
    assert row["spent_usd"] + row["reserved_usd"] <= row["limit_usd"]
    assert row["reserved_usd"] == D("1.000000")


# ------------------------------------------------------------------ BUD-2
async def test_bud2_zero_limit_no_calls_pipeline_keeps_working(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm()
    config = config_with(monthly_limit_usd=0.0, letters_reserve_usd=0.0, quiet_limit_usd=0.0, warn1_usd=0.0,
                         warn2_usd=0.0, daily_extraction_limit_usd=0.0)
    gateway = LlmGateway(build_budget(aengine, config, clock=FakeClock(NOW)), fake)
    result = await process(aengine, config, clock=FakeClock(NOW), gateway=gateway)
    assert fake.calls == 0
    assert result["extraction"]["stopped_by_budget"] == "monthly"
    v = await vacancy(aengine, vid)
    assert v["status"] == "pending_llm" and v["llm_attempts"] == 0 and v["lease_until"] is None


# ------------------------------------------------------------------ BUD-3, BUD-4
async def test_bud3_stop_after_reserve_assumed_spent_at_start(aengine):
    clock = FakeClock(NOW)
    budget = Budget(aengine, limits(), clock=clock)
    await budget.reserve(purpose="extraction", model="m", amount=D("0.004"), attempt_no=1)
    clock.advance(minutes=16)                     # процесс «упал» во время вызова
    assert await budget.recover_stale() == 1
    (entry,) = await ledger(aengine)
    row = await month_row(aengine)
    assert entry["state"] == "assumed_spent" and entry["actual_usd"] == D("0.004")
    assert row["spent_usd"] == D("0.004") and row["reserved_usd"] == 0


async def test_bud4_error_before_sending_returns_reserve(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm([ProviderRejected("HTTP 529: overloaded")])
    stats = await extractor(aengine, fake).run()
    (entry,) = await ledger(aengine)
    assert entry["state"] == "released" and entry["actual_usd"] == 0
    assert (await month_row(aengine))["spent_usd"] == 0
    v = await vacancy(aengine, vid)
    assert v["status"] == "pending_llm" and v["llm_attempts"] == 1 and stats["attempt_failed"] == 1


# ------------------------------------------------------------------ BUD-5, BUD-6
async def test_bud5_daily_limit_stops_extraction_letter_still_possible(aengine):
    for i in range(5):
        await add_vacancy(aengine, text=TEXT + f" Вакансия {i}.")
    config = config_with(daily_extraction_limit_usd=0.004)
    fake = FakeLlm()
    stats = await extractor(aengine, fake, config).run()
    assert stats["stopped_by_budget"] == "daily"
    assert 0 < stats["extracted"] < 5
    budget = build_budget(aengine, config, clock=FakeClock(NOW))
    assert await budget.reserve(purpose="letter", model="m", amount=D("0.003"), attempt_no=1)


async def test_bud6_extraction_part_exhausted_letters_reserve_kept(aengine):
    budget = Budget(aengine, limits(monthly="0.010", letters="0.006", daily="1"), clock=FakeClock(NOW))
    await budget.reserve(purpose="extraction", model="m", amount=D("0.004"), attempt_no=1)
    with pytest.raises(BudgetExhausted) as exc:
        await budget.reserve(purpose="extraction", model="m", amount=D("0.001"), attempt_no=1)
    assert exc.value.scope == "extraction"
    assert await budget.reserve(purpose="letter", model="m", amount=D("0.005"), attempt_no=1)


# ------------------------------------------------------------------ BUD-7, BUD-8
async def test_bud7_one_warning_per_threshold_per_month(aengine):
    alerts = []

    async def alert(kind, text):
        alerts.append(kind)

    budget = Budget(aengine, limits(monthly="3", warn1="0.010", warn2="0.020", daily="1"), clock=FakeClock(NOW),
                    alert=alert)
    for _ in range(6):
        lid = await budget.reserve(purpose="letter", model="m", amount=D("0.005"), attempt_no=1)
        await budget.settle(lid, D("0.005"), Usage(), "end_turn")
    assert alerts == ["budget_warn1", "budget_warn2"]


async def test_bud8_new_month_new_row_from_zero(aengine):
    clock = FakeClock(NOW)
    budget = Budget(aengine, limits(), clock=clock)
    lid = await budget.reserve(purpose="letter", model="m", amount=D("0.01"), attempt_no=1)
    await budget.settle(lid, D("0.01"), Usage(), "end_turn")
    clock.now = datetime(2026, 12, 1, 0, 1, tzinfo=UTC)
    await budget.reserve(purpose="letter", model="m", amount=D("0.02"), attempt_no=1)
    dec = await month_row(aengine, datetime(2026, 12, 1).date())
    assert dec["spent_usd"] == 0 and dec["reserved_usd"] == D("0.02")
    assert (await month_row(aengine))["spent_usd"] == D("0.01")


# ------------------------------------------------------------------ BUD-9, BUD-12
async def test_bud9_retry_is_separate_reserve_and_ledger_row(aengine):
    await add_vacancy(aengine)
    fake = FakeLlm(["не JSON", answer()])
    await extractor(aengine, fake).run()
    rows = await ledger(aengine)
    # первая половина недействительна → вторая не запрашивается; повтор: обе половины
    assert len(rows) == 1 + PER_PART and all(r["state"] == "settled" for r in rows)
    assert all(r["reserved_usd"] > 0 for r in rows)


async def test_bud12_four_token_kinds_and_stop_reason_logged(aengine):
    await add_vacancy(aengine)
    fake = FakeLlm(usage=Usage(input_tokens=800, cache_creation_input_tokens=2100, cache_read_input_tokens=0,
                               output_tokens=700))
    await extractor(aengine, fake).run()
    entry = (await ledger(aengine))[0]
    assert len(await ledger(aengine)) == PER_PART
    assert (entry["tokens_in"], entry["tokens_cache_write"], entry["tokens_cache_read"], entry["tokens_out"]) == \
        (800, 2100, 0, 700)
    assert entry["stop_reason"] == "end_turn"
    expected = (D(800) * D("0.10") + D(2100) * D("0.125") + D(700) * D("0.50")) / D(1_000_000)
    assert entry["actual_usd"] == expected.quantize(D("0.000001"), rounding="ROUND_CEILING")
    assert entry["actual_usd"] <= entry["reserved_usd"]


# ------------------------------------------------------------------ BUD-11
async def test_bud11_model_from_config_and_never_switched_on_errors(aengine):
    await add_vacancy(aengine)
    roles = CONFIG.llm.roles.model_copy(update={"extraction": CONFIG.llm.roles.extraction.model_copy(
        update={"model": "claude-test-model"})})
    prices = {**CONFIG.llm.prices, "claude-test-model": CONFIG.llm.prices["claude-haiku-5-5"]}
    config = replace(CONFIG, llm=CONFIG.llm.model_copy(update={"roles": roles, "prices": prices}))
    fake = FakeLlm(["плохо", "плохо", ProviderRejected("HTTP 500"), "плохо", ("x", "max_tokens")] * 3)
    for _ in range(3):
        await extractor(aengine, fake, config).run()
    assert {role.model for role, _ in fake.requests} == {"claude-test-model"}


# ------------------------------------------------------------------ BUD-13
async def test_bud13_quiet_channel_31st_post_not_sent_to_llm(aengine):
    async with aengine.begin() as conn:
        await conn.execute(insert(source_state).values(code="tg_q", kind="telegram", mode="quiet", llm_sampled=30))
    quiet = await add_vacancy(aengine, source="tg_q", quiet=True, mode="quiet")
    active = await add_vacancy(aengine, text=TEXT + " Активный источник.")
    fake = FakeLlm()
    stats = await extractor(aengine, fake).run()
    assert fake.calls == PER_PART and stats["quiet_limit"] == 1
    assert (await vacancy(aengine, quiet))["status_reason"] == "quiet_sample_limit"
    assert (await vacancy(aengine, quiet))["status"] == "pending_llm"
    assert (await vacancy(aengine, active))["status"] == "extracted"


async def test_bud13_quiet_budget_exhausted_active_continue(aengine):
    q = await add_vacancy(aengine, source="tg_q", quiet=True, mode="quiet", text=TEXT + " тихий")
    a = await add_vacancy(aengine, text=TEXT + " активный")
    config = config_with(quiet_limit_usd=0.0001)
    fake = FakeLlm()
    await extractor(aengine, fake, config).run()
    assert (await vacancy(aengine, a))["status"] == "extracted"     # активные — первыми и не тратят лимит каналов
    assert (await vacancy(aengine, q))["status"] == "pending_llm"


# ------------------------------------------------------------------ EXT-1, EXT-2
async def test_ext1_same_text_not_paid_twice(aengine):
    a = await add_vacancy(aengine)
    fake = FakeLlm()
    await extractor(aengine, fake).run()
    b = await add_vacancy(aengine, source="tg_b")                      # тот же текст в другой записи
    await extractor(aengine, fake).run()
    assert fake.calls == PER_PART
    assert (await vacancy(aengine, a))["status"] == (await vacancy(aengine, b))["status"] == "extracted"
    async with aengine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(extractions))).scalar() == 1


async def test_ext2_new_prompt_version_new_call_old_row_kept(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm()
    await extractor(aengine, fake).run()
    roles = CONFIG.llm.roles.model_copy(update={"extraction": CONFIG.llm.roles.extraction.model_copy(
        update={"prompt_version": "p1-1.1"})})
    config = replace(CONFIG, llm=CONFIG.llm.model_copy(update={"roles": roles}))
    async with aengine.begin() as conn:
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(status="pending_llm"))
    await extractor(aengine, fake, config).run()
    assert fake.calls == 2 * PER_PART
    async with aengine.connect() as conn:
        versions = (await conn.execute(select(extractions.c.prompt_version).order_by(extractions.c.id))).scalars().all()
    assert versions == ["p1-1.0", "p1-1.1"]


# ------------------------------------------------------------------ EXT-3, EXT-12, EXT-13
async def test_ext3_invalid_twice_attempt_fails_three_attempts_llm_failed(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm(["не JSON"] * 6)
    for run_no in range(1, 4):                   # повтор — при следующем запуске обработки
        await extractor(aengine, fake).run()
        v = await vacancy(aengine, vid)
        assert v["llm_attempts"] == run_no
    await extractor(aengine, fake).run()         # после 3 неудач запись больше не берётся
    v = await vacancy(aengine, vid)
    assert v["status"] == "llm_failed" and v["llm_attempts"] == 3 and v["status_reason"] == "invalid"
    assert fake.calls == 6                       # 3 попытки × (вызов + одна повторная)


async def test_ext12_refusal_no_retry_llm_failed_tokens_counted(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm([("{}", "refusal")])
    await extractor(aengine, fake).run()
    v = await vacancy(aengine, vid)
    assert (v["status"], v["status_reason"]) == ("llm_failed", "refusal") and fake.calls == 1
    (entry,) = await ledger(aengine)
    assert entry["state"] == "settled" and entry["stop_reason"] == "refusal" and entry["actual_usd"] > 0


async def test_ext13_truncated_by_max_tokens_invalid_attempt_counted(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm([('{"is_offer": {"value"', "max_tokens"), ('{"is_off', "max_tokens")])
    await extractor(aengine, fake).run()
    v = await vacancy(aengine, vid)
    assert v["llm_attempts"] >= 1 and v["status_reason"] == "stop_max_tokens"


# ------------------------------------------------------------------ EXT-6, EXT-7, EXT-8, EXT-11
def long_text(n: int) -> str:
    base = TEXT + "\n" + ("Дополнительное описание обязанностей и условий работы в компании. " * 3 + "\n") * 400
    return base[:n]


def part_responder(request):
    """Ответ для части: цитаты берутся из самого фрагмента, чтобы проверка прошла."""
    fragment = request.user_text.split('">\n', 1)[1].rsplit("\n</offer_text>", 1)[0]
    quote = fragment.strip().split("\n")[0][:60].strip()
    data = answer(is_offer={"value": "yes", "quote": quote, "confidence": "high"},
                  offer_type={"value": "job", "quote": quote, "confidence": "medium"},
                  direction={"value": "python_backend", "quote": quote, "confidence": "medium"},
                  work_format={"value": "not_specified", "quote": "", "confidence": "low"},
                  experience={"years": 0, "obligation": "not_specified", "quote": "", "confidence": "low"},
                  geo={"kind": "not_specified", "countries": [], "quote": "", "confidence": "low"},
                  skills_required={"items": ["python"], "quote": quote}, skills_optional={"items": [], "quote": ""})
    return data


@pytest.mark.parametrize("length,calls,truncated", [(15_000, 3, False), (30_000, 4, True)])
async def test_ext6_ext7_long_text_parts(aengine, length, calls, truncated):
    text = long_text(length)
    vid = await add_vacancy(aengine, text=text)
    fake = FakeLlm(responder=part_responder)
    await extractor(aengine, fake).run()
    assert fake.calls == calls * PER_PART and len(await ledger(aengine)) == calls * PER_PART
    async with aengine.connect() as conn:
        row = dict((await conn.execute(select(extractions))).mappings().one())
    assert row["status"] == "ok" and row["parts_total"] == calls and row["parts_done"] == calls
    assert ("truncated" in row["flags"]) is truncated
    if truncated:
        assert row["chars_analyzed"] < row["chars_total"] == length
        assert f"из {length} знаков" in row["flags"]["truncated"]
    v = await vacancy(aengine, vid)
    assert v["status"] == "extracted" and v["description_text"] == text          # EXT-11: текст хранится полностью


async def test_ext8_stop_after_second_of_three_parts_only_third_called(aengine):
    text = long_text(15_000)
    vid = await add_vacancy(aengine, text=text)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 2 * PER_PART + 1:
            raise asyncio.CancelledError()        # процесс остановился во время вызова по третьей части
        return part_responder(request)

    fake = FakeLlm(responder=responder)
    with pytest.raises(asyncio.CancelledError):
        await extractor(aengine, fake).extract_one(await vacancy(aengine, vid))
    async with aengine.connect() as conn:
        row = dict((await conn.execute(select(extractions))).mappings().one())
    assert row["status"] == "partial" and row["parts_done"] == 2
    entries = await ledger(aengine)
    assert entries[-1]["state"] == "assumed_spent"           # исход третьего вызова неизвестен

    fake2 = FakeLlm(responder=part_responder)
    await extractor(aengine, fake2).extract_one(await vacancy(aengine, vid))
    assert fake2.calls == PER_PART
    assert "часть: 3 из 3" in fake2.requests[0][1].user_text
    assert (await vacancy(aengine, vid))["status"] == "extracted"


async def test_source_counters_real_it_and_sampled(aengine):
    await add_vacancy(aengine)
    await add_vacancy(aengine, text=TEXT + " вторая")
    fake = FakeLlm(responder=lambda req: answer(direction={"value": "other", "quote": "", "confidence": "low"})
                   if "вторая" in req.user_text else answer())
    await extractor(aengine, fake).run()
    async with aengine.connect() as conn:
        st = dict((await conn.execute(select(source_state).where(source_state.c.code == "tg_a"))).mappings().one())
    assert st["llm_sampled"] == 2 and st["vacancies_real"] == 1


async def test_offer_type_and_not_offer_saved(aengine):
    vid = await add_vacancy(aengine)
    fake = FakeLlm([answer(is_offer={"value": "no", "quote": "Ищем Python-разработчика", "confidence": "high"},
                           not_offer_kind="ad")])
    await extractor(aengine, fake).run()
    async with aengine.connect() as conn:
        flags = (await conn.execute(select(extractions.c.flags))).scalar()
    assert flags["not_offer"] is True
    assert (await vacancy(aengine, vid))["status"] == "extracted"


# ------------------------------------------------------------------ INJ-5
async def test_inj5_telegram_markup_from_model_escaped_when_sent(aengine):
    from hunter_core.notify import Alerts
    from hunter_core.storage.tables import outbox

    alerts = Alerts(aengine, 42, timezone="Asia/Tbilisi", clock=FakeClock(NOW))
    await alerts.send("t", 'Факт: <b>жирный</b> <a href="https://evil.example">ссылка</a> & <i>')
    async with aengine.connect() as conn:
        text = (await conn.execute(select(outbox.c.payload))).scalar()["text"]
    assert "<b>" not in text and "<a " not in text and "&lt;b&gt;" in text and "&amp;" in text
