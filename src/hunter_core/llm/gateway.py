"""LLM-шлюз (ARCHITECTURE.md, раздел 7). Прямые вызовы API из других модулей запрещены.

Каждый вызов: верхняя граница стоимости → резерв бюджета → запрос → закрытие резерва.
- Не более N одновременных запросов (по умолчанию 4).
- Повторы SDK отключены: каждая попытка резервируется отдельно (BUD-9).
- Модель берётся только из конфигурации роли; автоматической смены модели нет (BUD-11).
- Ошибка с ответом сервера — запрос не оплачен, резерв возвращается.
  Сетевая ошибка или таймаут — исход неизвестен, резерв списывается как расход.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from hunter_core.llm.budget import Budget
from hunter_core.llm.pricing import TokenPrices, Usage, actual_usd, upper_bound_usd

log = logging.getLogger("hunter_core.llm")


@dataclass(frozen=True)
class RoleSettings:
    model: str
    prompt_version: str
    max_tokens: int
    effort: str | None
    thinking: str            # adaptive (не передаётся) или disabled
    prices: TokenPrices


@dataclass(frozen=True)
class LlmRequest:
    system_blocks: list[str]          # неизменная часть; точка кэширования — на последнем блоке
    user_text: str                    # изменяемая часть
    schema: dict[str, Any]


@dataclass
class ProviderResponse:
    text: str
    stop_reason: str | None
    usage: Usage
    model: str
    raw_blocks: list[str] = field(default_factory=list)


class ProviderRejected(Exception):
    """Сервер ответил ошибкой: запрос не выполнен и не оплачен."""


class ProviderUnknownOutcome(Exception):
    """Сетевая ошибка или таймаут: неизвестно, был ли запрос выполнен."""


class Provider(Protocol):
    async def complete(self, role: RoleSettings, request: LlmRequest) -> ProviderResponse: ...


@dataclass
class LlmResult:
    ok: bool                     # ответ получен и завершён обычно (end_turn)
    text: str
    stop_reason: str | None
    usage: Usage
    cost: Decimal
    ledger_id: int
    error: str | None = None


def request_chars(request: LlmRequest) -> int:
    return sum(len(b) for b in request.system_blocks) + len(request.user_text) + len(json.dumps(request.schema))


class LlmGateway:
    def __init__(self, budget: Budget, provider: Provider, *, max_concurrency: int = 4):
        self.budget = budget
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def call(self, role: RoleSettings, request: LlmRequest, *, purpose: str, attempt_no: int,
                   part_no: int | None = None, ledger_extra: dict | None = None) -> LlmResult:
        """Выполнить один вызов. BudgetExhausted поднимается до запроса — тогда вызова нет."""
        reserve = upper_bound_usd(request_chars(request), role.max_tokens, role.prices)
        ledger_id = await self.budget.reserve(purpose=purpose, model=role.model, amount=reserve,
                                              attempt_no=attempt_no, part_no=part_no, extra=ledger_extra)
        async with self._semaphore:
            try:
                response = await self.provider.complete(role, request)
            except ProviderRejected as exc:
                await self.budget.release(ledger_id, str(exc))
                log.warning("llm_rejected", extra={"model": role.model, "purpose": purpose, "error": str(exc)[:300]})
                return LlmResult(False, "", None, Usage(), Decimal(0), ledger_id, error=str(exc))
            except (ProviderUnknownOutcome, asyncio.CancelledError) as exc:
                await asyncio.shield(self.budget.assume_spent(ledger_id, f"{type(exc).__name__}: {exc}"))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                log.warning("llm_unknown_outcome", extra={"model": role.model, "purpose": purpose})
                return LlmResult(False, "", None, Usage(), reserve, ledger_id, error=str(exc))
        cost = actual_usd(response.usage, role.prices)
        await self.budget.settle(ledger_id, cost, response.usage, response.stop_reason)
        log.info("llm_call", extra={
            "model": response.model, "purpose": purpose, "stop_reason": response.stop_reason,
            "tokens_in": response.usage.input_tokens, "tokens_cache_write": response.usage.cache_creation_input_tokens,
            "tokens_cache_read": response.usage.cache_read_input_tokens, "tokens_out": response.usage.output_tokens,
            "cost_usd": str(cost),
        })
        return LlmResult(response.stop_reason == "end_turn", response.text, response.stop_reason, response.usage,
                         cost, ledger_id)
