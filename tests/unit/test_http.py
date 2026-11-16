"""HTTP-1…HTTP-3: HTTP-клиент (TEST_PLAN.md, раздел 4.2)."""

from __future__ import annotations

import httpx
import pytest

from hunter_core.http import DomainNotAllowed, HttpClient, HttpPolicy
from tests.fakes import FakeSleep, scripted_transport


def client(responses, *, allowed=("t.me",), policy=None, seen=None, sleep=None):
    sleep = sleep or FakeSleep()
    return HttpClient(allowed, user_agent="test", policy=policy or HttpPolicy(),
                      transport=scripted_transport(responses, seen), sleep=sleep, monotonic=sleep.monotonic), sleep


# ------------------------------------------------------------------ HTTP-1
@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "https://evil-t.me/s/channel",       # похожее имя — не поддомен
    "https://t.me.evil.com/s/channel",
])
async def test_http1_undeclared_domain_rejected_before_sending(url):
    seen = []
    http, _ = client([httpx.Response(200)], seen=seen)
    with pytest.raises(DomainNotAllowed):
        await http.get(url)
    assert seen == []
    await http.aclose()


async def test_http1_declared_domain_and_subdomain_allowed():
    http, _ = client([httpx.Response(200), httpx.Response(200)])
    assert (await http.get("https://t.me/s/it_interns")).status_code == 200
    assert (await http.get("https://cdn.t.me/file")).status_code == 200
    await http.aclose()


# ------------------------------------------------------------------ HTTP-2
async def test_http2_network_error_429_500_retried_with_growing_pause():
    seen = []
    http, sleep = client([
        httpx.ConnectError("boom"),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(200, text="ok"),
    ], seen=seen)
    response = await http.get("https://t.me/s/x")
    assert response.status_code == 200
    assert len(seen) == 4            # первая попытка и три повтора
    assert sleep.pauses == [2.0, 4.0, 8.0]
    await http.aclose()


async def test_http2_at_most_three_retries_then_last_response():
    seen = []
    http, sleep = client([httpx.Response(503)] * 5, seen=seen)
    response = await http.get("https://t.me/s/x")
    assert response.status_code == 503
    assert len(seen) == 4
    await http.aclose()


async def test_http2_network_error_after_retries_raises():
    http, _ = client([httpx.ConnectError("down")] * 4)
    with pytest.raises(httpx.ConnectError):
        await http.get("https://t.me/s/x")
    await http.aclose()


@pytest.mark.parametrize("status", [404, 403, 400])
async def test_http2_client_errors_not_retried(status):
    seen = []
    http, sleep = client([httpx.Response(status), httpx.Response(200)], seen=seen)
    assert (await http.get("https://t.me/s/x")).status_code == status
    assert len(seen) == 1 and sleep.pauses == []
    await http.aclose()


async def test_http2_retry_after_header_respected():
    http, sleep = client([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    await http.get("https://t.me/s/x")
    assert sleep.pauses == [7.0]
    await http.aclose()


# ------------------------------------------------------------------ HTTP-3
async def test_http3_pause_between_requests_to_same_domain():
    policy = HttpPolicy(domain_pauses={"t.me": 3.0})
    http, sleep = client([httpx.Response(200)] * 3, allowed=("t.me", "habr.com"), policy=policy)
    await http.get("https://t.me/s/a")
    await http.get("https://t.me/s/b")
    assert sleep.pauses == [3.0]
    await http.get("https://habr.com/x")   # другой домен — без паузы
    assert sleep.pauses == [3.0]
    await http.aclose()


async def test_http3_no_extra_pause_when_enough_time_passed():
    policy = HttpPolicy(domain_pauses={"t.me": 3.0})
    sleep = FakeSleep()
    http, _ = client([httpx.Response(200)] * 2, policy=policy, sleep=sleep)
    await http.get("https://t.me/s/a")
    sleep.t += 10
    await http.get("https://t.me/s/b")
    assert sleep.pauses == []
    await http.aclose()


async def test_http_query_parameters_not_logged(caplog):
    http, _ = client([httpx.Response(200)])
    with caplog.at_level("INFO"):
        await http.get("https://t.me/s/x?key=SECRET123")
    assert "SECRET123" not in caplog.text
    await http.aclose()
