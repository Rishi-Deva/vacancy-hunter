"""Промпт P1 и схема ответа (AGENT_PROMPTS.md, разделы 5–6).

Системная часть неизменна в пределах версии промпта: инструкция из p1_extraction.md
и три перечня (направления, типы задач, канонические навыки). Изменяемые сведения
записи идут только в сообщение пользователя, текст — внутри блока с случайной меткой.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from vacancy_hunter.config.schemas import DIRECTION_CODES, TASK_TYPES

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "p1_extraction.md"

CONFIDENCE = ["high", "medium", "low"]
DIRECTION_TEXT = {
    "python_backend": "Python-разработка и backend",
    "api_fastapi": "FastAPI, REST API",
    "telegram_bots": "Telegram-боты",
    "ai_llm": "AI-разработка, AI-ассистенты, LLM-интеграции",
    "integrations": "Интеграции с внешними сервисами и API",
    "automation_dev": "Автоматизация процессов и техническая автоматизация разработкой",
    "qa": "QA, тестирование API, автоматизация тестирования",
    "scraping": "Сбор данных и скрапинг",
    "support_tech": "Техническая поддержка с Python или SQL",
    "implementation": "Внедрение и настройка готовых систем",
    "other_tech": "Прочая техническая роль с долей разработки",
    "other": "Всё остальное",
}
TASK_TEXT = {
    "telegram_bots": "Telegram-боты", "backend_api": "Backend и REST API",
    "api_integration": "Интеграции с внешними API", "gov_api_integration": "Интеграции с государственным API",
    "llm_integration": "LLM-интеграции и AI-ассистенты", "rag": "RAG", "scraping": "Сбор данных и скрапинг",
    "internal_api_research": "Поиск внутренних API", "documents_processing": "Обработка документов, Excel и CSV",
    "database_sql": "Работа с базой данных и SQL", "auth": "Авторизация", "payments": "Платежи и подписки",
    "crm_features": "CRM-функции", "schedulers": "Планировщики и фоновые задачи",
    "manual_testing": "Ручное тестирование", "api_testing": "Тестирование API",
    "python_autotests": "Автотесты на Python", "deploy_linux": "Деплой на Linux, контейнеризация",
    "monitoring": "Мониторинг и оповещения", "computer_vision": "Компьютерное зрение: штрихкоды, фото",
    "tech_docs": "Техническая документация", "frontend": "Frontend и вёрстка", "mobile": "Мобильная разработка",
    "ml_training": "Обучение моделей машинного обучения", "ui_autotests": "Автотесты пользовательского интерфейса",
    "load_testing": "Нагрузочное тестирование",
    "cloud_orchestration": "Оркестрация контейнеров и облачная инфраструктура",
    "bi_analytics": "Аналитика и отчётность BI", "people_management": "Управление людьми",
    "sales_clients": "Продажи и работа с клиентами", "user_support": "Техническая поддержка пользователей",
    "network_admin": "Системное администрирование сетей", "other": "Иное",
}


OBLIGATION = ["required", "preferred", "unclear"]

# Допустимые значения всех перечней. Код проверяет ответ по ним всегда (validate_facts.py).
ENUMS: dict[str, list[str]] = {
    "is_offer": ["yes", "no"],
    "not_offer_kind": ["none", "resume", "ad", "digest", "other"],
    "offer_type": ["job", "internship", "project", "unknown"],
    "direction": DIRECTION_CODES,
    "seniority": ["intern", "junior", "middle", "senior", "lead", "not_specified"],
    "commercial_required": ["yes", "no", "not_specified"],
    "work_format": ["remote", "hybrid", "office", "not_specified"],
    "relocation": ["required", "not_required", "not_specified"],
    "employment": ["full_time", "part_time", "project", "internship", "not_specified"],
    "sales_share": ["main", "partial", "none", "unclear"],
    "experience.obligation": [*OBLIGATION, "not_specified"],
    "geo.kind": ["anywhere", "country_list", "must_be_located", "not_specified"],
    "languages.level": ["reading", "written", "spoken", "fluent", "not_specified"],
    "obligation": OBLIGATION,
    "legal.kind": ["citizenship", "work_permit", "local_phone", "local_bank", "self_employed", "sole_proprietor",
                   "employment_contract", "other"],
    "salary.period": ["month", "hour", "project", "other"],
    "tasks.type": TASK_TYPES,
    "other_requirements.kind": ["education", "certificate", "domain", "schedule", "equipment", "other"],
    "risk_signs.kind": ["candidate_pays", "performer_deposit", "payment_data", "income_no_duties", "other"],
    "confidence": CONFIDENCE,
}

# Перечни, которые не передаются в схему API, а проверяются только кодом: с ними скомпилированная
# грамматика схемы превышает предел API (проверка V2, 13.11.2026). Значения остаются в тексте промпта.
LOOSE_ENUMS = {"direction", "tasks.type"}


def _enum(key: str) -> dict:
    if key in LOOSE_ENUMS:
        return {"type": "string"}
    return {"type": "string", "enum": ENUMS[key]}


def _fact(key: str) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["value", "quote", "confidence"],
            "properties": {"value": _enum(key), "quote": {"type": "string"}, "confidence": _enum("confidence")}}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _list(item: dict) -> dict:
    return {"type": "array", "items": item}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

# Все поля обязательны, объединений типов и null нет (AGENT_PROMPTS.md, 2.1).
P1_SCHEMA: dict = _obj({
    "is_offer": _fact("is_offer"),
    "not_offer_kind": _enum("not_offer_kind"),
    "title": S,
    "company": S,
    "offer_type": _fact("offer_type"),
    "direction": _fact("direction"),
    "seniority": _fact("seniority"),
    "experience": _obj({"years": I, "obligation": _enum("experience.obligation"), "quote": S,
                        "confidence": _enum("confidence")}),
    "commercial_required": _fact("commercial_required"),
    "work_format": _fact("work_format"),
    "relocation": _fact("relocation"),
    "geo": _obj({"kind": _enum("geo.kind"), "countries": _list(S), "quote": S, "confidence": _enum("confidence")}),
    "employer_country": _obj({"code": S, "quote": S, "confidence": _enum("confidence")}),
    "languages": _list(_obj({"language": S, "level": _enum("languages.level"), "obligation": _enum("obligation"),
                             "quote": S})),
    "legal": _list(_obj({"kind": _enum("legal.kind"), "detail": S, "obligation": _enum("obligation"), "quote": S})),
    "employment": _fact("employment"),
    "project_terms": _obj({"budget": S, "deadline": S, "payment": S}),
    "salary": _obj({"present": B, "amount_from": I, "amount_to": I, "currency": S, "period": _enum("salary.period")}),
    "skills_required": _obj({"items": _list(S), "quote": S}),
    "skills_optional": _obj({"items": _list(S), "quote": S}),
    "tasks": _list(_obj({"text": S, "type": _enum("tasks.type"), "core": B})),
    "sales_share": _fact("sales_share"),
    "other_requirements": _list(_obj({"text": S, "kind": _enum("other_requirements.kind"),
                                      "obligation": _enum("obligation"), "quote": S})),
    "risk_signs": _list(_obj({"kind": _enum("risk_signs.kind"), "quote": S})),
    "apply_deadline": _obj({"date": S, "quote": S}),
    "apply_instructions": _list(_obj({"text": S, "quote": S})),
    "ai_directed": _obj({"present": B, "quote": S}),
})

def _part_schema(names: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(names),
            "properties": {n: P1_SCHEMA["properties"][n] for n in names}}


# Схема целиком не проходит компиляцию API («compiled grammar is too large», проверка V2, 13.11.2026).
# Решение владельца от 13.11.2026: два структурированных вызова на часть текста, по половине полей.
# Состав полей не меняется; ответы объединяются и проверяются кодом как единый ответ.
_NAMES = list(P1_SCHEMA["properties"])
P1_SCHEMA_PARTS: list[dict] = [_part_schema(_NAMES[:14]), _part_schema(_NAMES[14:])]

FACT_FIELDS = {"is_offer", "offer_type", "direction", "seniority", "commercial_required", "work_format",
               "relocation", "employment", "sales_share"}


def system_blocks(canonical_skills: list[str]) -> list[str]:
    """Инструкция и три неизменных перечня. Точка кэширования — на последнем блоке."""
    instruction = PROMPT_PATH.read_text(encoding="utf-8")
    directions = "ПЕРЕЧЕНЬ НАПРАВЛЕНИЙ (direction)\n" + "\n".join(f"- {k} — {v}" for k, v in DIRECTION_TEXT.items())
    tasks = "ПЕРЕЧЕНЬ ТИПОВ ЗАДАЧ (tasks.type)\n" + "\n".join(f"- {k} — {v}" for k, v in TASK_TEXT.items())
    skills = ("ПЕРЕЧЕНЬ КАНОНИЧЕСКИХ НАВЫКОВ\n" + (", ".join(sorted(canonical_skills)) if canonical_skills
                                                 else "(перечень пуст — пиши навыки как в тексте)"))
    return [instruction, directions + "\n\n" + tasks + "\n\n" + skills]


def new_tag(text: str) -> str:
    """Случайное значение метки блока; если оно встретилось в тексте — генерируется заново (S2)."""
    while True:
        tag = secrets.token_hex(8)
        if tag not in text:
            return tag


def user_message(text_part: str, *, title: str | None, company: str | None, part_no: int, parts_total: int,
                 tag: str | None = None) -> tuple[str, str]:
    tag = tag or new_tag(text_part)
    message = (
        "Сведения источника:\n"
        f"название: {title or 'не указано'}\n"
        f"компания: {company or 'не указано'}\n"
        f"часть: {part_no} из {parts_total}\n\n"
        f'<offer_text id="{tag}">\n{text_part}\n</offer_text>'
    )
    return message, tag


def split_text(text: str, *, max_chars: int = 6000, overlap: int = 300, max_parts: int = 4) -> tuple[list[str], int]:
    """Части до max_chars по границам абзацев с перекрытием. Возвращает части и число проанализированных знаков."""
    if len(text) <= max_chars:
        return [text], len(text)
    parts: list[str] = []
    start = 0
    while True:
        end = min(start + max_chars, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + max_chars // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        parts.append(text[start:end])
        if end >= len(text):
            return parts, len(text)
        if len(parts) == max_parts:
            return parts, end          # остаток не анализируется — это записывается явно
        start = end - overlap
