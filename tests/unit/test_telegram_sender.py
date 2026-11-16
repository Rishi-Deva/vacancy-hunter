"""Отправщик Telegram: различение «отказ до отправки» и «исход неизвестен» (ARCHITECTURE.md, раздел 12)."""

from __future__ import annotations

import json
from datetime import time

import httpx
import pytest

from hunter_core.notify import SendOutcomeUnknown, SendRefused, TelegramBotApiSender, is_quiet
from tests.fakes import scripted_transport

TOKEN = "123456:SECRET-token-value"


def sender(responses, seen=None):
    return TelegramBotApiSender(TOKEN, transport=scripted_transport(responses, seen))


async def test_successful_send_returns_message_id_and_sends_only_allowed_fields():
    seen = []
    s = sender([httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})], seen)
    assert await s.send(42, {"text": "привет", "parse_mode": "HTML", "evil": "x"}) == 77
    body = json.loads(seen[0].content)
    assert body == {"chat_id": 42, "text": "привет", "parse_mode": "HTML"}
    assert seen[0].url.host == "api.telegram.org"
    await s.aclose()


@pytest.mark.parametrize("error", [httpx.ConnectError("no route"), httpx.ConnectTimeout("slow")])
async def test_connection_not_established_is_refusal(error):
    s = sender([error])
    with pytest.raises(SendRefused):
        await s.send(1, {"text": "x"})
    await s.aclose()


@pytest.mark.parametrize("error", [httpx.ReadTimeout("no answer"), httpx.RemoteProtocolError("dropped")])
async def test_no_answer_after_sending_is_unknown_outcome(error):
    s = sender([error])
    with pytest.raises(SendOutcomeUnknown):
        await s.send(1, {"text": "x"})
    await s.aclose()


async def test_explicit_rate_limit_refusal_carries_retry_after():
    s = sender([httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 30}})])
    with pytest.raises(SendRefused) as exc:
        await s.send(1, {"text": "x"})
    assert exc.value.retry_after == 30
    await s.aclose()


async def test_token_never_logged(caplog):
    s = sender([httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})])
    with caplog.at_level("INFO"):
        await s.send(1, {"text": "x"})
    assert "SECRET-token-value" not in caplog.text
    urls = [getattr(r, "url", "") for r in caplog.records]
    assert "https://api.telegram.org/bot***/sendMessage" in urls
    assert not any("SECRET" in u for u in urls)
    await s.aclose()


@pytest.mark.parametrize("local,quiet", [(time(22, 59), False), (time(23, 0), True), (time(3, 0), True),
                                         (time(6, 59), True), (time(7, 0), False)])
def test_alerts_quiet_at_night(local, quiet):
    assert is_quiet(local) is quiet
