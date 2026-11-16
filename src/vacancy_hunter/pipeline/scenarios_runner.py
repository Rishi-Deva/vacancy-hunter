"""Команда prompt-scenarios: сценарии P1 на реальной модели (TEST_PLAN.md, раздел 5).

Вызовы идут через тот же шлюз, бюджет, деление на части и проверку кодом, что и в конвейере;
в базу записывается только журнал расходов. Итог — таблица «сценарий, ожидалось, получено»,
расход прогона и замеры токенов (AGENT_PROMPTS.md, раздел 12, V3–V5).
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.llm import LlmGateway
from hunter_core.storage.tables import llm_ledger
from vacancy_hunter.config import AppConfig
from vacancy_hunter.pipeline import p1
from vacancy_hunter.pipeline.extract import Extractor
from vacancy_hunter.pipeline.merge_parts import merge_parts

SCENARIOS_FILE = Path(__file__).resolve().parents[3] / "tests" / "prompt_scenarios" / "scenarios.py"


def load_scenarios():
    spec = importlib.util.spec_from_file_location("prompt_scenarios", SCENARIOS_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # нужно для dataclass внутри модуля
    spec.loader.exec_module(module)
    return module.SCENARIOS


async def analyze_text(extractor: Extractor, text: str, title: str) -> dict:
    limits = extractor.limits
    parts, analyzed = p1.split_text(text, max_chars=extractor.config.llm.roles.extraction.max_input_chars,
                                    overlap=limits.part_overlap_chars, max_parts=limits.max_parts)
    vacancy = {"id": None, "title": title, "company_name": None}
    results = []
    for index, part in enumerate(parts):
        outcome = await extractor._call_part(vacancy, part, index + 1, len(parts), 1, "extraction")
        if outcome.failure is not None:
            return {"_failure": outcome.failure}
        results.append(outcome)
    facts, conflicts = merge_parts([r.facts for r in results])
    facts["_parts"] = len(parts)
    facts["_truncated"] = analyzed < len(text)
    facts["_quote_check"] = {"quotes_unverified": sum(r.quote_check["quotes_unverified"] for r in results),
                             "conflicts": conflicts}
    return facts


async def run_scenarios(engine: AsyncEngine, gateway: LlmGateway, config: AppConfig,
                        only: list[str] | None = None) -> dict:
    extractor = Extractor(engine, gateway, config)
    async with engine.connect() as conn:
        start_id = (await conn.execute(select(func.coalesce(func.max(llm_ledger.c.id), 0)))).scalar_one()
    rows = []
    for scenario in load_scenarios():
        if only and scenario.code not in only:
            continue
        facts = await analyze_text(extractor, scenario.text, scenario.title)
        if "_failure" in facts:
            rows.append((scenario.code, scenario.expect, f"ОШИБКА: {facts['_failure']}", False))
            continue
        errors = scenario.check(facts)
        rows.append((scenario.code, scenario.expect, "; ".join(errors) or "совпадает", not errors))
    async with engine.connect() as conn:
        agg = (await conn.execute(
            select(func.count(), func.coalesce(func.sum(llm_ledger.c.actual_usd), 0),
                   func.coalesce(func.sum(llm_ledger.c.tokens_out), 0),
                   func.coalesce(func.sum(llm_ledger.c.tokens_cache_write), 0),
                   func.coalesce(func.sum(llm_ledger.c.tokens_cache_read), 0),
                   func.coalesce(func.sum(llm_ledger.c.tokens_in), 0))
            .where(llm_ledger.c.id > start_id)
        )).one()
    calls, cost, out, cache_w, cache_r, tokens_in = agg
    return {"rows": rows, "passed": sum(r[3] for r in rows), "total": len(rows), "calls": calls,
            "cost_usd": Decimal(cost), "avg_output_tokens": (out // calls) if calls else 0,
            "cache_write_tokens": cache_w, "cache_read_tokens": cache_r, "input_tokens": tokens_in,
            "avg_cost_per_call": (Decimal(cost) / calls) if calls else Decimal(0)}
