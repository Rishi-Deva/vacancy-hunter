"""Этап 4 конвейера: правила до LLM (TECHNICAL_SPEC.md, раздел 8; ARCHITECTURE.md, раздел 5).

Правило исключает только при однозначном признаке. При сомнении запись идёт дальше.
Правила НЕ исключают: разовые проекты, слова уровня в названии, отсутствие технологий
профиля, ключевые слова в теле текста; особых условий для отдельных каналов нет.

Развилка после правил (раздел 12.1):
- есть описание → `pending_llm`;
- нет описания, слово уровня относится к названию роли → `awaiting_description`;
- нет описания и слова уровня → предварительная оценка по заголовку (шаг A6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from vacancy_hunter.config.schemas import ScoringConfig

TELEGRAM_HOSTS = {"t.me", "telegram.me"}
PRELIMINARY_PENDING = "preliminary_pending"


@dataclass(frozen=True)
class Decision:
    status: str                 # rejected_rules, pending_llm, awaiting_description, new (предварительная — A6)
    reason: str | None = None


def _words(text: str) -> str:
    """Нижний регистр, дефисы и косые черты как пробелы — для сравнения целыми словами."""
    return " " + re.sub(r"[\s\-/–—,.;:()«»\"'!?|]+", " ", (text or "").lower()) + " "


def _has_phrase(text_words: str, phrase: str) -> bool:
    return _words(phrase) in text_words


def has_vacancy_link(canonical_url: str | None) -> bool:
    host = (urlsplit(canonical_url or "").hostname or "").lower()
    return bool(host) and host not in TELEGRAM_HOSTS


TECH_ROLE_WORDS = ("разработчик", "developer", "программист", "инженер", "engineer", "тестировщик", "tester",
                   "аналитик", "analyst", "devops", "архитектор")


def is_incompatible_role(title: str, rules: ScoringConfig) -> bool:
    """Название прямо называет несовместимую профессию.

    Не исключается, если в названии есть слово профиля («Java/Python-разработчик») или
    одна из ролей через «/» — техническая и не из перечня («Веб-разработчик/Дизайнер»):
    это сомнение, а при сомнении запись идёт на извлечение.
    """
    words = _words(title)
    roles = rules.prefilter.incompatible_roles
    if not any(_has_phrase(words, role) for role in roles):
        return False
    if any(_has_phrase(words, w) for w in rules.prefilter.profile_title_words):
        return False
    role_part = re.split(r"\s+/\s+", title or "", maxsplit=1)[0]      # «Название / Компания»
    for part in role_part.split("/"):
        part_words = _words(part)
        if any(_has_phrase(part_words, w) for w in TECH_ROLE_WORDS) and \
                not any(_has_phrase(part_words, role) for role in roles):
            return False
    return True


def is_not_vacancy(title: str, text: str | None, rules: ScoringConfig) -> bool:
    first = (title or "").strip().lower()
    hashtags = {h.lower() for h in re.findall(r"#([\w_]+)", text or "")}
    for marker in rules.prefilter.not_vacancy_markers:
        m = marker.lower()
        if first.startswith(m) or first.lstrip("#").startswith(m):
            return True
        if m.replace(" ", "") in hashtags or m.replace(" ", "_") in hashtags:
            return True
    return False


def seniority_in_role(title: str, rules: ScoringConfig) -> bool:
    """Слово уровня относится к названию роли («Senior Python Developer», «Ведущий тестировщик»).

    Учитывается только точная форма слова («руководитель», но не «руководителя»)
    и не учитываются сочетания из seniority_exclusions («помощник руководителя», «главный офис»).
    """
    words = _words(title)
    if any(_has_phrase(words, ex) for ex in rules.seniority_exclusions):
        return False
    return any(_has_phrase(words, w) for w in rules.seniority_words)


def decide(vacancy: dict, rules: ScoringConfig, now: datetime) -> Decision:
    p = rules.prefilter
    published = vacancy.get("published_at") or vacancy.get("first_seen_at")
    if published is not None and published < now - timedelta(days=p.max_age_days):
        return Decision("rejected_rules", "too_old")
    title = vacancy.get("title") or ""
    text = vacancy.get("description_text")
    if is_not_vacancy(title, text, rules):
        return Decision("rejected_rules", "not_vacancy")
    if is_incompatible_role(title, rules):
        return Decision("rejected_rules", "incompatible_role")
    if (vacancy.get("work_format_src") or "").lower() == "office":
        return Decision("rejected_rules", "office_only")
    if vacancy.get("has_description"):
        if len((text or "").strip()) < p.min_text_chars and not has_vacancy_link(vacancy.get("canonical_url")):
            return Decision("rejected_rules", "empty")
        return Decision("pending_llm")
    if seniority_in_role(title, rules):
        return Decision("awaiting_description", "level_word_in_title")
    return Decision("new", PRELIMINARY_PENDING)
