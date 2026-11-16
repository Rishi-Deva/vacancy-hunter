"""Реестр адаптеров: тип адаптера из конфигурации → фабрика. Новый источник того же типа
подключается записью в конфигурации без изменения кода."""

from __future__ import annotations

from hunter_core.sources.base import AdapterFactory

_REGISTRY: dict[str, AdapterFactory] = {}


def register(adapter_type: str):
    def decorator(factory: AdapterFactory) -> AdapterFactory:
        _REGISTRY[adapter_type] = factory
        return factory

    return decorator


def get_factory(adapter_type: str) -> AdapterFactory:
    try:
        return _REGISTRY[adapter_type]
    except KeyError:
        raise KeyError(f"неизвестный тип адаптера: {adapter_type}") from None


def registered_types() -> list[str]:
    return sorted(_REGISTRY)
