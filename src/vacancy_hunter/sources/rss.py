"""Общий разбор RSS-лент (hh, Хабр Карьера)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

from hunter_core.sources import SourceParseError


def parse_rss(text: str, where: str) -> list[dict[str, str]]:
    """Список записей ленты: поля элемента <item> как словарь «тег → текст»."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        raise SourceParseError(f"{where}: лента не разобрана ({exc})") from None
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise SourceParseError(f"{where}: ответ не является RSS-лентой")
    return [{child.tag: (child.text or "").strip() for child in item} for item in channel.findall("item")]


def parse_date(value: str) -> datetime | None:
    """Дата RSS: RFC 822 («Fri, 13 Nov 2026 10:42:40 +0300») или ISO 8601."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
