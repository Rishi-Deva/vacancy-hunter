"""Команда probe-sources: пробный запрос к каждому источнику (SOURCES.md, раздел 7).

Для каждого источника из конфигурации — один запрос и разбор ответа. Итог: «доступен»
с числом записей или причина недоступности. Недоступный источник не роняет команду.
Ничего не сохраняет в базу. С ключом --save-samples сохраняет ответы для образцов тестов.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hunter_core.http import HttpClient, HttpPolicy
from hunter_core.sources import SourceError, get_factory
from vacancy_hunter.config.schemas import SourcesConfig
from vacancy_hunter.sources.habr_career import HabrCareerAdapter
from vacancy_hunter.sources.hh_rss import HhRssAdapter
from vacancy_hunter.sources.rss import parse_rss
from vacancy_hunter.sources.telegram_preview import TelegramPreviewAdapter, parse_preview_page


@dataclass
class ProbeResult:
    code: str
    mode: str
    ok: bool
    detail: str


async def probe_one(adapter, http: HttpClient, save_dir: Path | None) -> tuple[bool, str]:
    if isinstance(adapter, TelegramPreviewAdapter):
        url = adapter.page_url()
    elif isinstance(adapter, (HhRssAdapter, HabrCareerAdapter)):
        url = adapter.url_for(adapter.queries[0])
    else:
        return False, "неизвестный тип адаптера"
    try:
        response = await http.get(url)
    except Exception as exc:  # noqa: BLE001
        return False, f"нет соединения: {type(exc).__name__}"
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        ext = "html" if isinstance(adapter, TelegramPreviewAdapter) else "xml"
        (save_dir / f"{adapter.code}.{ext}").write_text(response.text, encoding="utf-8")
    if response.status_code in (401, 403, 451):
        return False, f"доступ запрещён (HTTP {response.status_code})"
    if response.status_code != 200:
        return False, f"ответ HTTP {response.status_code}"
    try:
        if isinstance(adapter, TelegramPreviewAdapter):
            posts = parse_preview_page(response.text)
            if not posts:
                return False, "страница без постов: предпросмотр выключен или канал пуст"
            newest = max(p.published_at for p in posts if p.published_at)
            return True, f"{len(posts)} постов на странице, последний {newest:%d.%m.%Y}"
        entries = parse_rss(response.text, adapter.code)
        return True, f"{len(entries)} записей в ленте"
    except (SourceError, ValueError) as exc:
        return False, f"ответ не разобран: {exc}"


async def probe_sources(config: SourcesConfig, *, save_dir: Path | None = None, transport=None) -> list[ProbeResult]:
    adapters = [(get_factory(e.adapter)(e, config.defaults), e.mode) for e in config.sources]
    domains = sorted({d for a, _ in adapters for d in a.domains if d})
    http = HttpClient(domains, user_agent=config.user_agent, transport=transport,
                      policy=HttpPolicy(max_retries=1, domain_pauses={d: config.defaults.domain_pause_seconds
                                                                      for d in domains}))
    results = []
    try:
        for adapter, mode in adapters:
            ok, detail = await probe_one(adapter, http, save_dir)
            results.append(ProbeResult(adapter.code, mode, ok, detail))
    finally:
        await http.aclose()
    return results

