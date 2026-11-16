"""Подставной LLM: заранее заданные ответы, учёт вызовов, без сети и без оплаты."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from hunter_core.llm import (
    LlmRequest,
    ProviderRejected,
    ProviderResponse,
    ProviderUnknownOutcome,
    RoleSettings,
    Usage,
)

TEXT = ("Ищем Python-разработчика для поддержки Telegram-ботов. Обязательно: Python, aiogram, PostgreSQL. "
        "Опыт от 1 года. Работа удалённая, можно из любой страны. Будет плюсом Redis.")


def fact(value: str, quote: str = "", confidence: str = "high") -> dict:
    return {"value": value, "quote": quote, "confidence": confidence}


def answer(**overrides) -> dict:
    """Корректный ответ P1 для TEXT; поля можно заменить."""
    data = {
        "is_offer": fact("yes", "Ищем Python-разработчика"),
        "not_offer_kind": "none",
        "title": "Python-разработчик",
        "company": "",
        "offer_type": fact("job", "Ищем Python-разработчика"),
        "direction": fact("telegram_bots", "поддержки Telegram-ботов"),
        "seniority": fact("not_specified"),
        "experience": {"years": 1, "obligation": "required", "quote": "Опыт от 1 года", "confidence": "high"},
        "commercial_required": fact("not_specified"),
        "work_format": fact("remote", "Работа удалённая"),
        "relocation": fact("not_specified"),
        "geo": {"kind": "anywhere", "countries": [], "quote": "можно из любой страны", "confidence": "high"},
        "employer_country": {"code": "", "quote": "", "confidence": "low"},
        "languages": [],
        "legal": [],
        "employment": fact("not_specified"),
        "project_terms": {"budget": "", "deadline": "", "payment": ""},
        "salary": {"present": False, "amount_from": 0, "amount_to": 0, "currency": "", "period": "other"},
        "skills_required": {"items": ["python", "aiogram", "postgresql"],
                            "quote": "Обязательно: Python, aiogram, PostgreSQL"},
        "skills_optional": {"items": ["redis"], "quote": "Будет плюсом Redis"},
        "tasks": [{"text": "Поддержка Telegram-ботов", "type": "telegram_bots", "core": True}],
        "sales_share": fact("none"),
        "other_requirements": [],
        "risk_signs": [],
        "apply_deadline": {"date": "", "quote": ""},
        "apply_instructions": [],
        "ai_directed": {"present": False, "quote": ""},
    }
    data.update(overrides)
    return data


class FakeLlm:
    """Сценарий — список элементов: dict (ответ JSON), str (сырой текст), исключение,
    кортеж (ответ, stop_reason). Пустой сценарий — каждый раз корректный ответ."""

    def __init__(self, script: list | None = None, *, usage: Usage | None = None,
                 delay: float = 0.0, responder: Callable[[LlmRequest], object] | None = None):
        self.script = list(script or [])
        self.usage = usage or Usage(input_tokens=1500, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                    output_tokens=600)
        self.delay = delay
        self.responder = responder
        self.requests: list[tuple[RoleSettings, LlmRequest]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, role: RoleSettings, request: LlmRequest) -> ProviderResponse:
        self.requests.append((role, request))
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if self.script else (self.responder(request) if self.responder else answer())
        stop = "end_turn"
        if isinstance(item, tuple):
            item, stop = item
        if isinstance(item, (ProviderRejected, ProviderUnknownOutcome)):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return ProviderResponse(text=text, stop_reason=stop, usage=self.usage, model=role.model)


__all__ = ["TEXT", "FakeLlm", "ProviderRejected", "ProviderUnknownOutcome", "answer", "fact"]
