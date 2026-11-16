"""Карточка вакансии и подробный вид для Telegram (TECHNICAL_SPEC.md, 13.1, 13.3, 13.4, 16.3).

- весь текст из вакансии и фактов экранируется (CRD-1);
- цитаты обрезаются до 200 знаков;
- сообщение не длиннее предела Telegram: сначала убираются строки 7 и 6, затем 5 (CRD-2);
- ссылка — только из записи источника, не из ответа модели (CRD-3).
"""

from __future__ import annotations

import html

from vacancy_hunter.matching.explain import card_parts
from vacancy_hunter.matching.gates import GATE_TITLE, GATES

TELEGRAM_LIMIT = 4096
SHORTEN_ORDER = ("requirements", "missing", "matched")
QUOTE_LIMIT = 200
STATE_TEXT = {"yes": "да", "no": "нет", "unknown": "неизвестно"}
SKILL_STATUS = {"have": "есть", "related": "смежный", "missing": "нет", "unknown": "нет (вне словаря)",
                "unconfirmed": "требует подтверждения"}
SCORE_STATE = {"pending_recalc": "ожидает пересчёта", "recalc_failed": "ошибка пересчёта, действует прежний расчёт"}
TITLE_ONLY_MARK = "Оценка только по заголовку, полное описание не проверено."


def _e(text) -> str:
    return html.escape(str(text), quote=False)


def keyboard(vacancy_id: int) -> dict:
    v = vacancy_id
    return {"inline_keyboard": [
        [{"text": "Интересно", "callback_data": f"fb:interested:{v}"},
         {"text": "Откликнулся", "callback_data": f"fb:applied:{v}"},
         {"text": "Отклонить", "callback_data": f"rj:{v}"}],
        [{"text": "Подробнее", "callback_data": f"more:{v}"},
         {"text": "Письмо", "callback_data": f"later:letter:{v}"},
         {"text": "Пересмотреть", "callback_data": f"later:review:{v}"}],
        [{"text": "Снять отметку", "callback_data": f"fb:cleared:{v}"}],
    ]}


def render_card(vacancy: dict, score: dict, url: str | None, notes: list[str] | None = None,
                limit: int = TELEGRAM_LIMIT) -> str:
    parts = card_parts(vacancy, score, url, notes)
    if score["kind"] == "preliminary" and not any(k == "conclusion" and TITLE_ONLY_MARK in t for k, t in parts):
        parts.append(("conclusion", TITLE_ONLY_MARK))

    def build(items) -> str:
        lines = []
        for key, text in items:
            if key == "head":
                lines.append(f"<b>{_e(text)}</b>")
            elif key == "url":
                lines.append(f'<a href="{html.escape(text, quote=True)}">Открыть вакансию</a>')
            else:
                lines.append(_e(text))
        return "\n".join(lines)

    text = build(parts)
    for key in SHORTEN_ORDER:
        if len(text) <= limit:
            break
        parts = [p for p in parts if p[0] != key]
        text = build(parts)
    # Длинные название или пометки: самая длинная из прочих строк укорачивается вдвое, пока не поместится.
    protected = {"total", "url"}
    for _ in range(40):
        if len(text) <= limit:
            break
        index = max((i for i, p in enumerate(parts) if p[0] not in protected), key=lambda i: len(parts[i][1]))
        key, value = parts[index]
        parts[index] = (key, value[: max(1, len(value) // 2)] + "…")
        text = build(parts)
    return text


def card_payload(vacancy: dict, score: dict, url: str | None, notes: list[str] | None = None) -> dict:
    return {"text": render_card(vacancy, score, url, notes), "parse_mode": "HTML", "disable_notification": True,
            "link_preview_options": {"is_disabled": True}, "reply_markup": keyboard(vacancy["id"])}


def _quote(text: str) -> str:
    text = text or ""
    return _e(text[:QUOTE_LIMIT] + ("…" if len(text) > QUOTE_LIMIT else ""))


def render_details(vacancy: dict, score: dict, *, versions: tuple[int, int] | None, sources: list[str],
                   facts: dict | None, limit: int = TELEGRAM_LIMIT) -> str:
    """Подробный вид (13.3)."""
    lines = [f"<b>{_e(vacancy.get('title') or 'Без названия')}</b> — подробно", ""]
    lines.append("<b>Фильтры</b>")
    for name in GATES:
        gate = score["gates"].get(name, {"state": "unknown", "reason": "", "quote": ""})
        line = f"{GATE_TITLE[name]}: {STATE_TEXT[gate['state']]}"
        if gate.get("reason"):
            line += f" — {_e(gate['reason'])}"
        if gate.get("quote"):
            line += f" («{_quote(gate['quote'])}»)"
        lines.append(line)
    e = score["explanation"]
    if score["kind"] == "full":
        lines += ["", "<b>Навыки</b>"]
        for title, key in (("обязательные", "required"), ("желательные", "optional")):
            skills = e["skills"][key]
            if skills:
                lines.append(f"{title}: " + ", ".join(
                    f"{_e(s['name'])} — {SKILL_STATUS[s['status']]}" + (f" ({_e(s['via'])})" if s.get("via") else "")
                    for s in skills))
        if e["tasks"]:
            lines += ["", "<b>Задачи</b>"]
            for t in e["tasks"]:
                closed = ", ".join(t["projects"][:2]) if t["projects"] else ("покрыто профилем" if t["covered"]
                                                                              else "не покрыто")
                lines.append(f"{'• ' if t['core'] else '◦ '}{_e(t['text'])} — {_e(closed)}")
        level = e["level"]
        lines += ["", f"<b>Стаж</b>: штраф {level['penalty']} — {_e(level['basis'])}"]
        if level["unmet"]:
            lines.append(f"Невыполнимые требования (−{level['unmet_penalty']}): " + _e("; ".join(level["unmet"])))
        if e["apply_instructions"]:
            lines.append("Особые требования к отклику: " + _e("; ".join(e["apply_instructions"])))
        if e["ai_directed"].get("present"):
            lines.append("⚠️ В тексте есть обращение к ИИ" + (f": «{_quote(e['ai_directed']['quote'])}»"
                                                             if e["ai_directed"].get("quote") else ""))
        c = e["confidence"]
        lines.append(f"Уверенность извлечения: высокая {c['high']}, средняя {c['medium']}, низкая {c['low']}; "
                     f"понижено до «неизвестно» {c['downgraded']}")
    else:
        lines += ["", TITLE_ONLY_MARK]
    if facts and (facts.get("apply_deadline") or {}).get("date"):
        lines.append(f"Срок отклика: {_e(facts['apply_deadline']['date'])}")
    lines.append("")
    if versions:
        lines.append(f"Версия профиля {versions[0]}, правил {versions[1]}")
    if score.get("state") in SCORE_STATE:
        lines.append(f"Состояние оценки: {SCORE_STATE[score['state']]}")
    if sources:
        lines.append("Источники: " + _e(", ".join(sources)))
    # Сокращение по целым строкам: каждая строка — законченный HTML, разметка не рвётся.
    text = "\n".join(lines)
    while len(text) > limit:
        index = max(range(1, len(lines)), key=lambda i: len(lines[i]))
        if lines[index] == "…":
            lines = lines[:1] + ["…"]
            text = "\n".join(lines)
            break
        lines[index] = "…"
        text = "\n".join(lines)
    return text
