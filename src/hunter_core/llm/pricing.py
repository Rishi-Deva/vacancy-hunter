"""Стоимость вызова LLM (TECHNICAL_SPEC.md, 21.3, 21.5; AGENT_PROMPTS.md, 2.2).

Верхняя граница (резерв): один токен на каждый знак запроса по цене записи в кэш
(самая высокая цена входа) плюс max_tokens по цене выхода. Фактическая стоимость
не может её превысить. Фактическая — по четырём видам токенов из ответа API.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

MILLION = Decimal(1_000_000)
QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class TokenPrices:
    """Цены за миллион токенов, доллары."""

    input: Decimal
    cache_write: Decimal
    cache_read: Decimal
    output: Decimal


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0


def _round_up(value: Decimal) -> Decimal:
    return value.quantize(QUANT, rounding=ROUND_CEILING)


def upper_bound_usd(request_chars: int, max_tokens: int, prices: TokenPrices) -> Decimal:
    entry_price = max(prices.cache_write, prices.input, prices.cache_read)
    return _round_up((Decimal(request_chars) * entry_price + Decimal(max_tokens) * prices.output) / MILLION)


def actual_usd(usage: Usage, prices: TokenPrices) -> Decimal:
    total = (Decimal(usage.input_tokens) * prices.input
             + Decimal(usage.cache_creation_input_tokens) * prices.cache_write
             + Decimal(usage.cache_read_input_tokens) * prices.cache_read
             + Decimal(usage.output_tokens) * prices.output)
    return _round_up(total / MILLION)
