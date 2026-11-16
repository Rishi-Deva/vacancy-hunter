"""CRD-1…CRD-3: карточка вакансии (TEST_PLAN.md, 4.7) и проверка значений настроек (UI-2)."""

from __future__ import annotations

import copy

import pytest

from tests.fakes.report import FULL
from tests.fakes.scoring import load_case
from vacancy_hunter.reporting.cards import TELEGRAM_LIMIT, card_payload, render_card, render_details
from vacancy_hunter.reporting.settings import SettingError, parse

V, _, _ = load_case("example1")


def test_crd1_markup_and_special_chars_escaped():
    v = dict(V, title="<b>Python</b> & <script>alert(1)</script>", company_name="ООО «A&B» <i>")
    text = render_card(v, FULL, "https://example.com/v?a=1&b=2")
    assert "<script>" not in text and "&lt;script&gt;" in text and "A&amp;B" in text
    assert text.count("<b>") == 1                                   # жирное — только наш заголовок
    assert 'href="https://example.com/v?a=1&amp;b=2"' in text


def test_crd2_long_card_shortened_within_limit():
    score = copy.deepcopy(FULL)
    score["explanation"]["requirements"] = ["очень длинное требование " * 40] * 3
    score["explanation"]["missing"] = ["навык" + str(i) * 300 for i in range(4)]
    score["explanation"]["matched"] = ["совпало" + str(i) * 300 for i in range(6)]
    score["unknowns"] = [{"topic": "оформление", "reason": "длинная причина " * 20}]
    text = render_card(V, score, "https://example.com/v/1")
    assert len(text) <= TELEGRAM_LIMIT
    assert "Требования:" not in text and "Не хватает:" in text       # убрана только строка 7: дальше помещается
    score["explanation"]["missing"] = ["навык" + str(i) * 900 for i in range(4)]
    text = render_card(V, score, "https://example.com/v/1")
    assert len(text) <= TELEGRAM_LIMIT and "Не хватает:" not in text and "Совпало:" in text   # затем строка 6
    assert "Открыть вакансию" in text and "90 из 100" in text


def test_crd2_extreme_title_still_within_limit():
    v = dict(V, title="Очень длинное название " * 400)
    text = render_card(v, FULL, "https://example.com/v/1")
    assert len(text) <= TELEGRAM_LIMIT and "Открыть вакансию" in text


def test_crd3_link_from_source_record_and_buttons():
    payload = card_payload(dict(V, id=7), FULL, "https://career.habr.com/vacancies/1")
    assert 'href="https://career.habr.com/vacancies/1"' in payload["text"]
    assert payload["disable_notification"] is True and payload["link_preview_options"] == {"is_disabled": True}
    data = [b["callback_data"] for row in payload["reply_markup"]["inline_keyboard"] for b in row]
    assert data == ["fb:interested:7", "fb:applied:7", "rj:7", "more:7", "later:letter:7", "later:review:7",
                    "fb:cleared:7"]
    assert all(len(d.encode()) <= 64 for d in data)


def test_details_view_has_gates_skills_versions():
    text = render_details(V, dict(FULL, state="pending_recalc"), versions=(2, 3), sources=["habr_career"],
                          facts={"apply_deadline": {"date": "2026-11-20"}})
    for part in ("Формат: да", "Документы: да", "python — есть", "redis — нет", "Версия профиля 2, правил 3",
                 "ожидает пересчёта", "Источники: habr_career", "Срок отклика: 2026-11-20", "Уверенность извлечения"):
        assert part in text


@pytest.mark.parametrize("name,raw,ok", [
    ("threshold", "49", False), ("threshold", "50", True), ("threshold", "90", True), ("threshold", "91", False),
    ("threshold", "abc", False), ("max_cards", "0", False), ("max_cards", "1", True), ("max_cards", "15", True),
    ("max_cards", "16", False), ("report_time", "07:00", True), ("report_time", "7:35", True),
    ("report_time", "07:03", False), ("report_time", "24:00", False),
])
def test_ui2_setting_ranges(name, raw, ok):
    if ok:
        parse(name, raw)
    else:
        with pytest.raises(SettingError, match="Прежнее значение сохранено"):
            parse(name, raw)
