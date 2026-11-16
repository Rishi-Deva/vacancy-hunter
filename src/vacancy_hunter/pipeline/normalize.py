"""Нормализация сырой записи источника в вакансию единого формата (ARCHITECTURE.md, раздел 4, этап 2).

Здесь же — приведение ключей дедупликации к единому виду (TECHNICAL_SPEC.md, раздел 7):
каноническая ссылка, хеш нормализованного текста, нормализованные компания и название.
Сама дедупликация — шаг A4.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_.*|yclid|gclid|fbclid|_openstat|from|ref|source|hhtmFrom.*|roistat.*)$", re.I)
DOMAIN_ALIASES = {"www.hh.ru": "hh.ru", "spb.hh.ru": "hh.ru", "m.hh.ru": "hh.ru", "www.hh.kz": "hh.kz",
                  "telegram.me": "t.me", "www.t.me": "t.me", "career.habr.com": "career.habr.com"}
LEGAL_FORMS = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|тоо|llc|ltd|inc|gmbh|corp|co)\b\.?", re.I)
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")
HASHTAG = re.compile(r"#[\w_]+")
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.org"}

MAX_TITLE = 200


@dataclass
class NormalizedVacancy:
    external_id: str
    url: str | None
    values: dict[str, Any]   # поля таблицы vacancies


class NormalizeError(ValueError):
    pass


def canonical_url(url: str | None) -> str | None:
    """Ссылка без параметров отслеживания и фрагмента, с единым доменом и без завершающей косой черты."""
    if not url:
        return None
    parts = urlsplit(html.unescape(url.strip()))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    host = DOMAIN_ALIASES.get(host, host)
    if host.endswith(".hh.ru"):
        host = "hh.ru"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
                             if not TRACKING_PARAMS.match(k)))
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit(("https", host, path, query, ""))


def normalize_text(text: str) -> str:
    """Для хеша: нижний регистр, без эмодзи и хештегов, без различий в пробелах."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = EMOJI.sub(" ", HASHTAG.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip()


def text_hash(text: str | None) -> str | None:
    norm = normalize_text(text or "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else None


def norm_company(name: str | None) -> str | None:
    if not name:
        return None
    value = unicodedata.normalize("NFKC", name).lower()
    value = re.sub(r"[«»\"'“”„`]", " ", value)
    value = LEGAL_FORMS.sub(" ", value)
    value = re.sub(r"[^\w\s+#.-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    return value or None


# Пометки о формате и месте работы не отличают одну должность от другой (TECHNICAL_SPEC.md, раздел 7).
FORMAT_WORDS = re.compile(
    r"\b(удал[её]нн?о|удал[её]нка|удал[её]нная работа|remote|remotely|гибрид|hybrid|"
    r"full[ -]?time|part[ -]?time|полная занятость|частичная занятость|вакансия)\b", re.I)


def norm_title(title: str | None) -> str | None:
    """Название для сравнения: нижний регистр, без текста в скобках, пометок формата, знаков и дефисов."""
    if not title:
        return None
    value = unicodedata.normalize("NFKC", title).lower()
    value = EMOJI.sub(" ", value)
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = FORMAT_WORDS.sub(" ", value)
    value = re.sub(r"[«»\"'“”|/\\,:;!?*_–—-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or None


def _base(external_id: str, url: str | None, *, title: str, company: str | None, description: str | None,
          has_description: bool, link_for_card: str | None, published_at: datetime | None,
          source_code: str, quiet: bool, now: datetime, **extra) -> NormalizedVacancy:
    title = re.sub(r"\s+", " ", title).strip()[:MAX_TITLE]
    if not title:
        raise NormalizeError("нет названия")
    values = {
        "status": "new",
        "title": title,
        "company_name": company or None,
        "company_norm": norm_company(company),
        "title_norm": norm_title(title),
        "description_text": description or None,
        "description_hash": text_hash(description) if description else None,
        "has_description": has_description,
        "canonical_url": canonical_url(link_for_card),
        "published_at": published_at or now,   # если источник не дал дату — дата первого обнаружения
        "first_seen_at": now,
        "last_seen_at": now,
        "primary_source_code": source_code,
        "quiet_only": quiet,
        "offer_type": "unknown",
        "created_at": now,
        "updated_at": now,
        **extra,
    }
    return NormalizedVacancy(external_id=external_id, url=url, values=values)


# ------------------------------------------------------------------ Telegram
def _clean_tg_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = EMOJI.sub("", line).strip(" -—•*#|")
        if stripped:
            return stripped
    return ""


def _external_link(links: list[str]) -> str | None:
    for link in links:
        host = (urlsplit(link).hostname or "").lower()
        if host and host not in TELEGRAM_HOSTS and not host.endswith(".t.me"):
            return link
    return None


def normalize_telegram(payload: dict[str, Any], *, source_code: str, quiet: bool, now: datetime,
                       external_id: str, url: str | None, published_at: datetime | None) -> NormalizedVacancy:
    text = _clean_tg_text(payload.get("text") or "")
    if not text:
        raise NormalizeError("пустой пост")
    link = _external_link(payload.get("links") or []) or url
    return _base(external_id, url, title=_first_line(text) or text[:MAX_TITLE], company=None, description=text,
                 has_description=True, link_for_card=link, published_at=published_at, source_code=source_code,
                 quiet=quiet, now=now)


# ------------------------------------------------------------------ hh RSS
CURRENCIES = {"₽": "RUB", "руб": "RUB", "rur": "RUB", "rub": "RUB", "$": "USD", "usd": "USD", "€": "EUR",
              "eur": "EUR", "₸": "KZT", "kzt": "KZT", "тенге": "KZT", "byn": "BYN", "uzs": "UZS"}


def _hh_fields(description: str) -> dict[str, str]:
    text = re.sub(r"<[^>]+>", "\n", html.unescape(description or "")).replace("\xa0", " ")
    fields = {}
    for line in (l.strip() for l in text.splitlines()):
        key, sep, value = line.partition(":")
        if sep and value.strip():
            fields[key.strip().lower()] = value.strip()
    return fields


def parse_salary(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    t = text.lower().replace("\xa0", " ")

    def num(pattern: str) -> int | None:
        m = re.search(pattern + r"\s*([\d ]+)", t)
        return int(m.group(1).replace(" ", "")) if m and m.group(1).strip() else None

    result: dict[str, Any] = {"salary_from": num(r"от"), "salary_to": num(r"до")}
    if result["salary_from"] is None and result["salary_to"] is None:
        m = re.search(r"([\d ]{3,})", t)
        if m:
            result["salary_from"] = int(m.group(1).replace(" ", ""))
    for mark, code in CURRENCIES.items():
        if mark in t:
            result["salary_currency"] = code
            break
    if "месяч" in t or "в месяц" in t:
        result["salary_period"] = "month"
    return {k: v for k, v in result.items() if v is not None}


def normalize_hh(payload: dict[str, Any], *, source_code: str, quiet: bool, now: datetime,
                 external_id: str, url: str | None, published_at: datetime | None) -> NormalizedVacancy:
    fields = _hh_fields(payload.get("description", ""))
    company = fields.get("вакансия компании")
    # Ключ поля тоже важен: «Предполагаемый уровень месячного дохода» задаёт период.
    salary_text = next((f"{k}: {v}" for k, v in fields.items() if "доход" in k or "зарплат" in k), None)
    return _base(external_id, url, title=html.unescape(payload.get("title", "")), company=company,
                 description=None, has_description=False, link_for_card=url, published_at=published_at,
                 source_code=source_code, quiet=quiet, now=now, location_raw=fields.get("регион"),
                 **parse_salary(salary_text))


# ------------------------------------------------------------------ Хабр Карьера
def normalize_habr(payload: dict[str, Any], *, source_code: str, quiet: bool, now: datetime,
                   external_id: str, url: str | None, published_at: datetime | None) -> NormalizedVacancy:
    raw_title = html.unescape(payload.get("title", ""))
    m = re.search(r"«(.+)»", raw_title)
    title = m.group(1) if m else re.sub(r"^Требуется\s+", "", raw_title)
    description = html.unescape(payload.get("description", "")).strip()
    remote = "удалённо" in description.lower() or "удаленно" in description.lower()
    return _base(external_id, url, title=title, company=html.unescape(payload.get("company", "")) or None,
                 description=description or None, has_description=bool(description), link_for_card=url,
                 published_at=published_at, source_code=source_code, quiet=quiet, now=now,
                 work_format_src="remote" if remote else None)


NORMALIZERS = {
    "telegram_preview": normalize_telegram,
    "hh_rss": normalize_hh,
    "habr_career_rss": normalize_habr,
}
