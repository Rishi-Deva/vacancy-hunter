"""Подставные источники: страницы t.me/s и ленты RSS без сети."""

from __future__ import annotations

import html
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from hunter_core.http import HttpClient, HttpPolicy
from tests.fakes import FakeSleep
from vacancy_hunter.config import AppConfig, load_app_config
from vacancy_hunter.config.schemas import SourcesConfig

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def tg_page(channel: str, posts: list[tuple[int, datetime, str]], links: dict[int, list[str]] | None = None) -> str:
    """Страница предпросмотра канала в разметке t.me/s."""
    links = links or {}
    blocks = []
    for number, published, text in posts:
        anchors = "".join(f'<a href="{html.escape(u)}">ссылка</a>' for u in links.get(number, []))
        body = html.escape(text).replace("\n", "<br/>")
        blocks.append(
            '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message js-widget_message" data-post="{channel}/{number}">'
            '<div class="tgme_widget_message_bubble">'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{body}{anchors}</div>'
            '<div class="tgme_widget_message_footer"><span class="tgme_widget_message_meta">'
            f'<a class="tgme_widget_message_date" href="https://t.me/{channel}/{number}">'
            f'<time datetime="{published.isoformat()}" class="time">00:00</time></a></span></div>'
            "</div></div></div>"
        )
    return ('<html><body><div class="tgme_channel_info"></div><section class="tgme_channel_history">'
            + "".join(blocks) + "</section></body></html>")


class Router:
    """HTTP без сети: адрес → ответ (или список ответов по очереди). Запоминает запросы."""

    def __init__(self, routes: dict[str, object] | None = None):
        self.routes: dict[str, list] = {}
        self.requests: list[str] = []
        for url, value in (routes or {}).items():
            self.set(url, value)

    def set(self, url: str, value) -> None:
        self.routes[url] = list(value) if isinstance(value, list) else [value]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        queue = self.routes.get(url)
        if not queue:
            return httpx.Response(404, text="not found")
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return httpx.Response(200, text=item)


def http_for(router: Router, domains=("t.me", "telegram.me", "hh.kz", "hh.ru", "career.habr.com")) -> HttpClient:
    sleep = FakeSleep()
    return HttpClient(domains, user_agent="test", transport=httpx.MockTransport(router),
                      policy=HttpPolicy(), sleep=sleep, monotonic=sleep.monotonic)


def config_with_sources(entries: list[dict]) -> AppConfig:
    base = load_app_config()
    sources = SourcesConfig.model_validate({
        "user_agent": "test",
        "defaults": {**base.sources.defaults.model_dump(), "domain_pause_seconds": 0},
        "sources": entries,
    })
    return replace(base, sources=sources)


def tg_entry(code: str, channel: str, mode: str = "active") -> dict:
    return {"code": code, "kind": "telegram", "adapter": "telegram_preview", "channel": channel, "mode": mode}
