"""Создание адаптеров по записям sources.yaml и HTTP-клиентов для них."""

from __future__ import annotations

from hunter_core.http import HttpClient, HttpPolicy
from hunter_core.sources import SourceAdapter, get_factory
from vacancy_hunter.config.schemas import SourcesConfig


def build_adapters(config: SourcesConfig, *, include_inactive: bool = False) -> list[tuple[SourceAdapter, str]]:
    """Пары (адаптер, режим из конфигурации). Источники waiting/disabled не опрашиваются (SRC-10)."""
    result = []
    for entry in config.sources:
        if not include_inactive and entry.mode not in ("active", "quiet"):
            continue
        result.append((get_factory(entry.adapter)(entry, config.defaults), entry.mode))
    return result


def build_http(adapters: list[tuple[SourceAdapter, str]], config: SourcesConfig, transport=None) -> HttpClient:
    """Один клиент на все источники: паузы по доменам общие, домены — только объявленные адаптерами."""
    domains = sorted({d for adapter, _ in adapters for d in adapter.domains if d})
    pauses = {d: config.defaults.domain_pause_seconds for d in domains}
    return HttpClient(domains, user_agent=config.user_agent, transport=transport,
                      policy=HttpPolicy(domain_pauses=pauses))
