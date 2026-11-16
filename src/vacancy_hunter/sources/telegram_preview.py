"""Telegram-каналы через публичную страницу предпросмотра t.me/s/<канал> (SOURCES.md, раздел 4; решение D6).

- На странице около 20 последних постов. Более старые — по адресу `?before=<номер>`.
- Курсор — номер последнего сохранённого поста. Если между курсором и самым старым постом
  страницы есть разрыв, запрашиваются более старые страницы, но не глубже 14 дней.
- Посты старше 14 дней не возвращаются (свежесть проверяется на этапе сбора).
- Из поста берутся: номер, дата, текст, ссылки из текста.
Аккаунт Telegram не используется (правило R8).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from hunter_core.sources import (
    FetchContext,
    FetchResult,
    RawItem,
    SourceParseError,
    SourceTemporaryError,
    classify_status,
    register,
)

HOST = "t.me"
MAX_PAGES = 10


@dataclass
class TgPost:
    channel: str
    number: int
    published_at: datetime | None
    text: str
    links: list[str] = field(default_factory=list)
    service: bool = False


class _PreviewParser(HTMLParser):
    """Разбор страницы t.me/s без сторонних библиотек."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[TgPost] = []
        self._post: TgPost | None = None
        self._post_depth = 0
        self._text_depth = 0
        self._text_parts: list[str] = []
        self._depth = 0
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            self._depth += 1
            if "tgme_widget_message " in cls + " " and a.get("data-post") and self._post is None:
                channel, _, number = a["data-post"].partition("/")
                if number.isdigit():
                    self._post = TgPost(channel=channel, number=int(number), published_at=None, text="",
                                        service="service_message" in cls)
                    self._post_depth = self._depth
            elif (self._post is not None and "tgme_widget_message_text" in cls and not self._text_depth
                  and "js-message_reply_text" not in cls and not self._post.text):
                # Текст цитируемого сообщения (ответа) пропускаем: нужен текст самого поста.
                self._text_depth = self._depth
                self._text_parts = []
        elif self._post is not None:
            if tag == "br" and self._text_depth:
                self._text_parts.append("\n")
            elif tag == "a" and self._text_depth and a.get("href"):
                self._post.links.append(html.unescape(a["href"]))
            elif tag == "time" and a.get("datetime") and self._post.published_at is None:
                try:
                    self._post.published_at = datetime.fromisoformat(a["datetime"])
                except ValueError:
                    pass

    def handle_endtag(self, tag):
        if tag != "div":
            return
        if self._text_depth and self._depth == self._text_depth:
            self._post.text = "".join(self._text_parts).strip()
            self._text_depth = 0
        if self._post is not None and self._depth == self._post_depth:
            self.posts.append(self._post)
            self._post = None
        self._depth -= 1

    def handle_data(self, data):
        if self._text_depth:
            self._text_parts.append(data)


def parse_preview_page(page: str) -> list[TgPost]:
    parser = _PreviewParser()
    parser.feed(page)
    parser.close()
    # Подписи эмодзи на странице дублируют символ картинкой — убираем лишние пробелы.
    for post in parser.posts:
        post.text = re.sub(r"[ \t]+\n", "\n", post.text)
        post.links = list(dict.fromkeys(post.links))
    return parser.posts


class TelegramPreviewAdapter:
    kind = "telegram"
    domains = (HOST, "telegram.me")

    def __init__(self, code: str, channel: str, pause_seconds: float = 3.0):
        self.code = code
        self.channel = channel
        self.pause_seconds = pause_seconds

    def page_url(self, before: int | None = None) -> str:
        url = f"https://{HOST}/s/{self.channel}"
        return f"{url}?before={before}" if before else url

    async def _page(self, ctx: FetchContext, before: int | None) -> list[TgPost]:
        try:
            response = await ctx.http.get(self.page_url(before))
        except Exception as exc:  # noqa: BLE001 — сеть после всех повторов
            raise SourceTemporaryError(f"{self.code}: {type(exc).__name__}") from None
        classify_status(response.status_code, self.code)
        if "tgme_channel_info" not in response.text and "tgme_widget_message" not in response.text:
            # Канал без предпросмотра или требование входа: страница без постов и без описания канала.
            raise SourceParseError(f"{self.code}: страница не похожа на предпросмотр канала")
        return parse_preview_page(response.text)

    async def fetch(self, cursor: dict[str, Any] | None, ctx: FetchContext) -> FetchResult:
        last_seen = int((cursor or {}).get("last_post", 0))
        collected: dict[int, TgPost] = {}
        requests = 0
        seen_on_pages = 0
        before: int | None = None
        for _ in range(MAX_PAGES):
            posts = await self._page(ctx, before)
            requests += 1
            seen_on_pages += len(posts)
            if not posts:
                break
            for post in posts:
                if post.number > last_seen and not post.service and not ctx.too_old(post.published_at):
                    collected[post.number] = post
            oldest = min(posts, key=lambda p: p.number)
            reached_cursor = oldest.number <= last_seen + 1
            too_old = ctx.too_old(oldest.published_at)
            if reached_cursor or too_old or oldest.number <= 1:
                break
            before = oldest.number
        items = [
            RawItem(
                external_id=f"{post.channel}/{post.number}",
                url=f"https://{HOST}/{post.channel}/{post.number}",
                published_at=post.published_at,
                payload={"channel": post.channel, "number": post.number, "text": post.text, "links": post.links,
                         "published_at": post.published_at.isoformat() if post.published_at else None},
            )
            for post in sorted(collected.values(), key=lambda p: p.number)
        ]
        new_last = max([last_seen, *collected], default=last_seen)
        return FetchResult(items=items, cursor={"last_post": new_last}, http_requests=requests,
                           seen_on_pages=seen_on_pages)


@register("telegram_preview")
def make_telegram(entry, defaults) -> TelegramPreviewAdapter:
    return TelegramPreviewAdapter(entry.code, entry.channel, pause_seconds=defaults.domain_pause_seconds)
