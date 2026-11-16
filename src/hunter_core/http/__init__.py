"""HTTP-клиент с таймаутами, повторами, паузами по доменам и списком разрешённых доменов."""

from hunter_core.http.client import DomainNotAllowed, HttpClient, HttpPolicy

__all__ = ["DomainNotAllowed", "HttpClient", "HttpPolicy"]
