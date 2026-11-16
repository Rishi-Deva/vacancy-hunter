"""Отправка сообщений через Telegram Bot API.

Разделение исходов (ARCHITECTURE.md, раздел 12):
- соединение не установлено или Telegram ответил отказом → SendRefused (можно повторить);
- запрос ушёл, но ответа нет (таймаут чтения, обрыв) → SendOutcomeUnknown (не повторять).
Токен бота входит в адрес запроса, поэтому в журнал адрес пишется без него.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from hunter_core.http import HttpClient, HttpPolicy
from hunter_core.notify.outbox import SendOutcomeUnknown, SendRefused

API_HOST = "api.telegram.org"
ALLOWED_FIELDS = {"text", "parse_mode", "disable_notification", "reply_markup", "link_preview_options",
                  "disable_web_page_preview"}


def _hide_token(url: str) -> str:
    return f"https://{API_HOST}/bot***/" + url.rsplit("/", 1)[-1]


class TelegramBotApiSender:
    def __init__(self, token: str, *, user_agent: str = "AIVacancyHunter",
                 transport: httpx.AsyncBaseTransport | None = None, document_dir: Path | None = None):
        self._document_dir = document_dir.resolve() if document_dir else None
        self._base = f"https://{API_HOST}/bot{token}"
        # Повторы делает outbox (до 5 попыток), поэтому здесь их нет: иначе неясен номер попытки.
        self._http = HttpClient([API_HOST], user_agent=user_agent, transport=transport,
                                policy=HttpPolicy(max_retries=0), safe_url=_hide_token)

    async def call(self, method: str, params: Mapping[str, Any]) -> dict:
        try:
            response = await self._http.post(f"{self._base}/{method}", json=dict(params))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise SendRefused(f"нет соединения с Telegram: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise SendOutcomeUnknown(f"ответ Telegram не получен: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return body.get("result") or {}
        retry_after = (body.get("parameters") or {}).get("retry_after")
        description = str(body.get("description") or f"HTTP {response.status_code}")[:300]
        raise SendRefused(f"Telegram отказал: {description}", retry_after=retry_after)

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> int:
        """Файл владельцу (резервная копия). Ошибки — как у sendMessage: отказ или неизвестный исход.

        Отправляются только файлы из разрешённого каталога (каталог резервных копий).
        """
        if self._document_dir is None or not path.resolve().is_relative_to(self._document_dir):
            raise SendRefused("файл вне каталога, разрешённого для отправки")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SendRefused(f"файл недоступен: {type(exc).__name__}") from None
        try:
            response = await self._http.post(
                f"{self._base}/sendDocument", data={"chat_id": str(chat_id), "caption": caption[:1000],
                                                    "disable_notification": "true"},
                files={"document": (path.name, content, "application/octet-stream")})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise SendRefused(f"нет соединения с Telegram: {type(exc).__name__}") from None
        except httpx.TransportError as exc:
            raise SendOutcomeUnknown(f"ответ Telegram не получен: {type(exc).__name__}") from None
        return self._result(response)

    def _result(self, response: httpx.Response) -> int:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return int((body.get("result") or {}).get("message_id", 0))
        retry_after = (body.get("parameters") or {}).get("retry_after")
        description = str(body.get("description") or f"HTTP {response.status_code}")[:300]
        raise SendRefused(f"Telegram отказал: {description}", retry_after=retry_after)

    async def send(self, chat_id: int, payload: Mapping[str, Any]) -> int:
        if "document_path" in payload:
            return await self.send_document(chat_id, Path(payload["document_path"]), payload.get("caption") or "")
        params = {k: v for k, v in payload.items() if k in ALLOWED_FIELDS}
        params["chat_id"] = chat_id
        result = await self.call("sendMessage", params)
        return int(result.get("message_id", 0))

    async def get_me(self) -> dict:
        return await self.call("getMe", {})

    async def aclose(self) -> None:
        await self._http.aclose()
