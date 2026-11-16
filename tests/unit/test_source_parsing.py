"""SRC-1, SRC-2: разбор образцов источников и нормализация (TEST_PLAN.md, раздел 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hunter_core.sources import SourceParseError
from tests.fakes.sources import sample, tg_page
from vacancy_hunter.pipeline.normalize import (
    canonical_url,
    norm_company,
    normalize_habr,
    normalize_hh,
    normalize_telegram,
    parse_salary,
    text_hash,
)
from vacancy_hunter.sources.rss import parse_rss
from vacancy_hunter.sources.telegram_preview import parse_preview_page

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


# ------------------------------------------------------------------ SRC-1
def test_src1_telegram_sample_all_posts_with_number_date_text_links():
    posts = parse_preview_page(sample("telegram_it_interns.html"))
    assert len(posts) == 20
    assert [p.number for p in posts] == sorted(p.number for p in posts)
    assert all(p.channel == "it_interns" for p in posts)
    assert all(p.published_at is not None and p.published_at.tzinfo is not None for p in posts)
    assert all(p.text for p in posts)
    assert sum(bool(p.links) for p in posts) >= 10
    assert "&amp;" not in "".join(link for p in posts for link in p.links)


def test_src1_second_channel_sample_parses():
    posts = parse_preview_page(sample("telegram_pythonpythonjobs.html"))
    assert len(posts) == 20 and all(p.text for p in posts)


def test_src1_reply_quote_and_line_breaks():
    page = tg_page("chan", [(5, NOW, "Python-разработчик\nУдалённо")])
    page = page.replace('<div class="tgme_widget_message_text js-message_text"',
                        '<div class="tgme_widget_message_reply"><div class="tgme_widget_message_text '
                        'js-message_reply_text">цитата</div></div><div class="tgme_widget_message_text js-message_text"')
    (post,) = parse_preview_page(page)
    assert post.text == "Python-разработчик\nУдалённо"


def test_telegram_normalization():
    posts = parse_preview_page(sample("telegram_it_interns.html"))
    post = next(p for p in posts if any("t.me" not in link for link in p.links))
    v = normalize_telegram({"text": post.text, "links": post.links}, source_code="tg_it_interns", quiet=False,
                           now=NOW, external_id=f"it_interns/{post.number}",
                           url=f"https://t.me/it_interns/{post.number}", published_at=post.published_at).values
    assert v["title"] and len(v["title"]) <= 200
    assert v["has_description"] is True and v["description_text"]
    assert v["description_hash"] == text_hash(post.text)
    assert "utm_" not in (v["canonical_url"] or "")
    assert v["status"] == "new" and v["offer_type"] == "unknown" and v["quiet_only"] is False


# ------------------------------------------------------------------ SRC-2
def test_src2_hh_rss_sample_fields_and_no_description():
    entries = parse_rss(sample("hh_kz.xml"), "hh_rss_kz")
    assert len(entries) == 20
    e = next(e for e in entries if "$" in e["description"] or "₸" in e["description"] or "₽" in e["description"])
    v = normalize_hh({"title": e["title"], "description": e["description"]}, source_code="hh_rss_kz", quiet=False,
                     now=NOW, external_id="1", url=e["link"], published_at=datetime.fromisoformat(e["pubDate"])).values
    assert v["title"] == e["title"]
    assert v["canonical_url"].startswith("https://hh.kz/vacancy/")
    assert v["company_name"].startswith("Компания ")
    assert v["location_raw"]
    assert v["salary_from"] or v["salary_to"]
    assert v["salary_currency"] in {"USD", "KZT", "RUB"} and v["salary_period"] == "month"
    assert v["has_description"] is False and v["description_text"] is None
    assert v["published_at"].year == 2026


def test_habr_rss_sample():
    entries = parse_rss(sample("habr_career.xml"), "habr_career")
    assert len(entries) == 50
    e = entries[0]
    v = normalize_habr({"title": e["title"], "company": e["author"], "description": e["description"]},
                       source_code="habr_career", quiet=False, now=NOW, external_id=e["guid"], url=e["link"],
                       published_at=None).values
    assert not v["title"].startswith("Требуется")
    assert v["company_name"].startswith("Компания")
    assert v["has_description"] is True
    assert v["canonical_url"].startswith("https://career.habr.com/vacancies/")


def test_non_rss_answer_is_parse_error():
    with pytest.raises(SourceParseError):
        parse_rss(sample("hh_ru_451.xml"), "hh_rss_ru")


# ------------------------------------------------------------------ ключи дедупликации
@pytest.mark.parametrize("raw,expected", [
    ("https://youngjunior.ru/project/jobs/1?utm_source=telegram&amp;utm_medium=x", "https://youngjunior.ru/project/jobs/1"),
    ("http://www.hh.ru/vacancy/123/?from=share", "https://hh.ru/vacancy/123"),
    ("https://spb.hh.ru/vacancy/123", "https://hh.ru/vacancy/123"),
    ("https://telegram.me/chan/5#x", "https://t.me/chan/5"),
    ("https://Example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("mailto:x@y.z", None),
])
def test_canonical_url(raw, expected):
    assert canonical_url(raw) == expected


def test_text_hash_ignores_case_spaces_emoji_hashtags():
    assert text_hash("Python  разработчик 🔥\n#вакансия #python") == text_hash("python разработчик")


@pytest.mark.parametrize("raw,expected", [("ООО «Пример»", "пример"), ('"Example" LLC', "example"),
                                          ("ТОО Ромашка", "ромашка")])
def test_norm_company(raw, expected):
    assert norm_company(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("от 2 500 $", {"salary_from": 2500, "salary_currency": "USD"}),
    ("от 100 000 до 150 000 ₽ в месяц", {"salary_from": 100000, "salary_to": 150000, "salary_currency": "RUB",
                                        "salary_period": "month"}),
    ("до 600 000 тенге", {"salary_to": 600000, "salary_currency": "KZT"}),
])
def test_parse_salary(raw, expected):
    assert parse_salary(raw) == expected
