"""Интерфейс адаптера, реестр, запуск сбора и контроль состояния источников."""

from hunter_core.sources.base import (
    FetchContext,
    FetchResult,
    RawItem,
    SourceAdapter,
    SourceBlocked,
    SourceError,
    SourceParseError,
    SourceTemporaryError,
    classify_status,
)
from hunter_core.sources.registry import get_factory, register, registered_types
from hunter_core.sources.runner import ControlPolicy, RunOutcome, SourceRunner, ensure_state

__all__ = [
    "ControlPolicy",
    "FetchContext",
    "FetchResult",
    "RawItem",
    "RunOutcome",
    "SourceAdapter",
    "SourceBlocked",
    "SourceError",
    "SourceParseError",
    "SourceRunner",
    "SourceTemporaryError",
    "classify_status",
    "ensure_state",
    "get_factory",
    "register",
    "registered_types",
]
