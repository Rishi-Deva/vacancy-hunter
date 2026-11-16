"""Этап 5 конвейера: LLM-извлечение фактов (ARCHITECTURE.md, разделы 4, 7, 8; AGENT_PROMPTS.md, раздел 5).

Очередь: вакансии `pending_llm` без действующей аренды, `llm_attempts` < 3. Сначала записи
активных источников (от свежих к старым), затем тихих (TECHNICAL_SPEC.md, 21.6).

Для каждой записи:
1. кэш: строка `extractions` с тем же хешем текста, моделью и версией промпта и статусом `ok`
   — вызова нет (EXT-1); строка `partial` — продолжение с первой необработанной части (EXT-8);
2. текст делится на части до 6000 знаков, не более 4; каждая часть — отдельный вызов и резерв;
3. ответ проверяется кодом; недействительный ответ (в том числе обрезанный по max_tokens) —
   одна повторная попытка; затем попытка записи неудачна; после 3 неудачных — `llm_failed`;
4. отказ модели (`refusal`) — `llm_failed` сразу, без повторов (EXT-12);
5. бюджет исчерпан — извлечение останавливается, записи остаются в `pending_llm` (BUD-2);
6. тихий канал: не более 30 постов за период через LLM, остальные учитываются только правилами (BUD-13).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.llm import (
    Budget,
    BudgetExhausted,
    BudgetLimits,
    LlmGateway,
    LlmRequest,
    RoleSettings,
    TokenPrices,
)
from hunter_core.storage.tables import source_state
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.pipeline import p1
from vacancy_hunter.pipeline.merge_parts import merge_parts
from vacancy_hunter.pipeline.validate_facts import InvalidResponse, validate_p1
from vacancy_hunter.storage.tables import extractions, vacancies

log = logging.getLogger("vacancy_hunter.extract")

QUIET_LIMIT_REASON = "quiet_sample_limit"
LEASE = timedelta(minutes=15)
BATCH = 200


def role_settings(config: AppConfig, role: str = "extraction") -> RoleSettings:
    r = getattr(config.llm.roles, role)
    price = config.llm.prices[r.model]
    return RoleSettings(model=r.model, prompt_version=r.prompt_version, max_tokens=r.max_tokens, effort=r.effort,
                        thinking=r.thinking,
                        prices=TokenPrices(Decimal(str(price.input)), Decimal(str(price.cache_write)),
                                           Decimal(str(price.cache_read)), Decimal(str(price.output))))


def budget_limits(config: AppConfig) -> BudgetLimits:
    b = config.llm.budget
    return BudgetLimits(monthly=Decimal(str(b.monthly_limit_usd)), letters_reserve=Decimal(str(b.letters_reserve_usd)),
                        daily_extraction=Decimal(str(b.daily_extraction_limit_usd)),
                        quiet=Decimal(str(b.quiet_limit_usd)), warn1=Decimal(str(b.warn1_usd)),
                        warn2=Decimal(str(b.warn2_usd)))


def canonical_skills(config: AppConfig) -> list[str]:
    return [s.canonical for s in config.skills.skills]


@dataclass
class PartOutcome:
    facts: dict | None
    quote_check: dict | None
    flags: dict | None
    ledger_id: int | None
    failure: str | None = None       # invalid, refusal, llm_error


class _Stop(Exception):
    """Бюджет исчерпан — дальнейшие вызовы в этом запуске не выполняются."""


class Extractor:
    def __init__(self, engine: AsyncEngine, gateway: LlmGateway, config: AppConfig, *, clock: Clock = utcnow):
        self.engine = engine
        self.gateway = gateway
        self.config = config
        self.clock = clock
        self.role = role_settings(config)
        self.limits = config.llm.limits
        self.system = p1.system_blocks(canonical_skills(config))
        self.stats: Counter = Counter()
        self.stop_reason: str | None = None
        self._seen: set[int] = set()   # за один запуск каждая запись — не больше одной попытки

    # ---------------------------------------------------------------- очередь
    async def _queue(self) -> list[dict]:
        now = self.clock()
        async with self.engine.begin() as conn:
            rows = (await conn.execute(
                select(vacancies)
                .where(vacancies.c.status == "pending_llm", vacancies.c.llm_attempts < self.limits.max_attempts,
                       or_(vacancies.c.lease_until.is_(None), vacancies.c.lease_until < now),
                       or_(vacancies.c.status_reason.is_(None), vacancies.c.status_reason != QUIET_LIMIT_REASON),
                       vacancies.c.id.notin_(self._seen) if self._seen else vacancies.c.id.isnot(None))
                .order_by(vacancies.c.quiet_only, vacancies.c.first_seen_at.desc(), vacancies.c.id)
                .limit(BATCH).with_for_update(skip_locked=True)
            )).mappings().all()
            if rows:
                await conn.execute(update(vacancies).where(vacancies.c.id.in_([r["id"] for r in rows]))
                                   .values(lease_until=now + LEASE))
                self._seen.update(r["id"] for r in rows)
        return [dict(r) for r in rows]

    async def _quiet_allowed(self, vacancy: dict) -> bool:
        """Тихий канал: не более N постов за период через LLM (SOURCES.md, 4.4)."""
        if not vacancy["quiet_only"]:
            return True
        async with self.engine.connect() as conn:
            sampled = (await conn.execute(select(source_state.c.llm_sampled)
                                          .where(source_state.c.code == vacancy["primary_source_code"]))).scalar()
        return (sampled or 0) < self.limits.quiet_posts_per_channel

    # ---------------------------------------------------------------- кэш и части
    async def _extraction_row(self, vacancy: dict, parts_total: int, chars_total: int, chars_analyzed: int) -> dict:
        async with self.engine.begin() as conn:
            await conn.execute(pg_insert(extractions).values(
                text_hash=vacancy["description_hash"], model=self.role.model, prompt_version=self.role.prompt_version,
                status="partial", parts_total=parts_total, parts_done=0, parts=[], chars_total=chars_total,
                chars_analyzed=chars_analyzed, created_at=self.clock(),
            ).on_conflict_do_nothing(index_elements=[extractions.c.text_hash, extractions.c.model,
                                                     extractions.c.prompt_version]))
            return dict((await conn.execute(select(extractions).where(
                extractions.c.text_hash == vacancy["description_hash"], extractions.c.model == self.role.model,
                extractions.c.prompt_version == self.role.prompt_version))).mappings().one())

    async def _call_part(self, vacancy: dict, text_part: str, part_no: int, parts_total: int,
                         attempt_no: int, purpose: str) -> PartOutcome:
        """Одна часть текста: два вызова по половине схемы; при недействительном ответе — одна повторная попытка."""
        last_failure = "invalid"
        last_ledger = None
        for _ in range(2):
            message, _tag = p1.user_message(text_part, title=vacancy["title"], company=vacancy["company_name"],
                                             part_no=part_no, parts_total=parts_total)
            combined: dict = {}
            failure = None
            for schema in p1.P1_SCHEMA_PARTS:
                request = LlmRequest(system_blocks=self.system, user_text=message, schema=schema)
                try:
                    result = await self.gateway.call(self.role, request, purpose=purpose, attempt_no=attempt_no,
                                                     part_no=part_no, ledger_extra={"vacancy_id": vacancy["id"]})
                except BudgetExhausted as exc:
                    self.stop_reason = exc.scope
                    raise _Stop(str(exc)) from None
                last_ledger = result.ledger_id
                self.stats["llm_calls"] += 1
                if result.error is not None:
                    return PartOutcome(None, None, None, last_ledger, "llm_error")
                if result.stop_reason == "refusal":
                    return PartOutcome(None, None, None, last_ledger, "refusal")
                if result.stop_reason != "end_turn":
                    failure = f"stop_{result.stop_reason}"
                    break
                try:
                    parsed = json.loads(result.text)
                except ValueError:
                    failure = "invalid"
                    break
                if not isinstance(parsed, dict):
                    failure = "invalid"
                    break
                combined.update({k: parsed[k] for k in schema["properties"] if k in parsed})
            if failure is not None:
                last_failure = failure
                continue
            try:
                validated = validate_p1(json.dumps(combined, ensure_ascii=False), text_part,
                                        strict_offer=part_no == 1)
            except InvalidResponse as exc:
                log.warning("p1_invalid", extra={"vacancy_id": vacancy["id"], "error": str(exc)[:300]})
                last_failure = "invalid"
                continue
            return PartOutcome(validated.facts, validated.quote_check, validated.flags, last_ledger)
        return PartOutcome(None, None, None, last_ledger, last_failure)

    async def _fail_attempt(self, vacancy: dict, reason: str, *, final: bool = False) -> None:
        attempts = vacancy["llm_attempts"] + 1
        failed = final or attempts >= self.limits.max_attempts
        async with self.engine.begin() as conn:
            await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
                llm_attempts=attempts, lease_until=None, updated_at=self.clock(),
                status="llm_failed" if failed else "pending_llm", status_reason=reason,
            ))
        self.stats["llm_failed" if failed else "attempt_failed"] += 1

    async def _mark_extracted(self, vacancy: dict, extraction: dict, facts: dict, *, cached: bool) -> None:
        offer_type = facts["offer_type"]["value"]
        main_or_related = set(self.config.scoring.direction.main_codes) | set(self.config.scoring.direction.related_codes)
        real_it = facts["is_offer"]["value"] == "yes" and facts["direction"]["value"] in main_or_related
        async with self.engine.begin() as conn:
            await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
                status="extracted", status_reason=None, lease_until=None, updated_at=self.clock(),
                offer_type=offer_type if offer_type in ("job", "internship", "project") else "unknown",
            ))
            # Счётчики источника: сколько постов прошло через LLM и сколько из них реальные IT-предложения.
            await conn.execute(update(source_state).where(source_state.c.code == vacancy["primary_source_code"]).values(
                llm_sampled=source_state.c.llm_sampled + (0 if cached else 1),
                vacancies_real=source_state.c.vacancies_real + (1 if real_it else 0),
            ))
        self.stats["extracted_cached" if cached else "extracted"] += 1

    async def extract_one(self, vacancy: dict) -> None:
        text = vacancy["description_text"] or ""
        purpose = "quiet_check" if vacancy["quiet_only"] else "extraction"
        if not text or not vacancy["description_hash"]:
            await self._fail_attempt(vacancy, "no_text", final=True)
            return
        parts, analyzed = p1.split_text(text, max_chars=self.config.llm.roles.extraction.max_input_chars,
                                        overlap=self.limits.part_overlap_chars, max_parts=self.limits.max_parts)
        row = await self._extraction_row(vacancy, len(parts), len(text), analyzed)
        if row["status"] == "ok":
            await self._mark_extracted(vacancy, row, row["facts"], cached=True)
            return
        if row["status"] == "invalid":
            # Строка invalid кэшем не считается: начинаем заново.
            async with self.engine.begin() as conn:
                await conn.execute(update(extractions).where(extractions.c.id == row["id"]).values(
                    status="partial", parts_done=0, parts=[]))
            row.update(status="partial", parts_done=0, parts=[])
        if not await self._quiet_allowed(vacancy):
            async with self.engine.begin() as conn:
                await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
                    status_reason=QUIET_LIMIT_REASON, lease_until=None))
            self.stats["quiet_limit"] += 1
            return
        done_parts: list[dict] = list(row["parts"] or [])
        attempt_no = vacancy["llm_attempts"] + 1
        for index in range(row["parts_done"], len(parts)):
            outcome = await self._call_part(vacancy, parts[index], index + 1, len(parts), attempt_no, purpose)
            if outcome.failure is not None:
                await self._fail_attempt(vacancy, outcome.failure, final=outcome.failure == "refusal")
                return
            done_parts.append({"facts": outcome.facts, "quote_check": outcome.quote_check, "flags": outcome.flags,
                               "ledger_id": outcome.ledger_id})
            async with self.engine.begin() as conn:     # результат части сохраняется сразу (продолжение после сбоя)
                await conn.execute(update(extractions).where(extractions.c.id == row["id"]).values(
                    parts=done_parts, parts_done=len(done_parts), ledger_id=outcome.ledger_id))
        facts, conflicts = merge_parts([p["facts"] for p in done_parts])
        quote_check = {
            "quotes_total": sum(p["quote_check"]["quotes_total"] for p in done_parts),
            "quotes_unverified": sum(p["quote_check"]["quotes_unverified"] for p in done_parts),
            "downgraded": sorted({d for p in done_parts for d in p["quote_check"]["downgraded"]}),
            "conflicts": conflicts,
        }
        flags = {k: any(p["flags"][k] for p in done_parts) for k in done_parts[0]["flags"]}
        flags["not_offer"] = facts["is_offer"]["value"] != "yes"
        if row["chars_analyzed"] < row["chars_total"]:
            flags["truncated"] = f"проанализировано {row['chars_analyzed']} из {row['chars_total']} знаков"
        async with self.engine.begin() as conn:
            await conn.execute(update(extractions).where(extractions.c.id == row["id"]).values(
                status="ok", facts=facts, quote_check=quote_check, flags=flags))
        await self._mark_extracted(vacancy, row, facts, cached=False)

    async def run(self) -> dict:
        while self.stop_reason is None:
            queue = await self._queue()
            if not queue:
                break
            semaphore = asyncio.Semaphore(self.limits.max_concurrency)

            async def worker(v: dict) -> None:
                async with semaphore:
                    if self.stop_reason is not None:
                        await self._release(v)
                        return
                    try:
                        await self.extract_one(v)
                    except _Stop:
                        await self._release(v)

            await asyncio.gather(*(worker(v) for v in queue))
        result = dict(self.stats)
        if self.stop_reason:
            result["stopped_by_budget"] = self.stop_reason
        return result

    async def _release(self, vacancy: dict) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(lease_until=None))


def build_budget(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow, alert=None) -> Budget:
    return Budget(engine, budget_limits(config), clock=clock, alert=alert)

