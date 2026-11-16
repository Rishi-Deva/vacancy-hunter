"""Поставщик Claude через официальный SDK `anthropic` (AGENT_PROMPTS.md, раздел 2).

Параметры вызова:
- структурированный ответ через `output_config.format` (json_schema);
- `output_config.effort` из конфигурации роли; temperature/top_p/top_k не передаются;
- `thinking` не передаётся (адаптивный режим по умолчанию) или `{"type": "disabled"}` по настройке;
- точка кэширования `cache_control: ephemeral` на последнем блоке системной части;
- инструменты не передаются; повторы SDK выключены (повторы и их оплату ведёт шлюз).
"""

from __future__ import annotations

import anthropic

from hunter_core.llm.gateway import (
    LlmRequest,
    ProviderRejected,
    ProviderResponse,
    ProviderUnknownOutcome,
    RoleSettings,
)
from hunter_core.llm.pricing import Usage


class AnthropicProvider:
    def __init__(self, api_key: str, *, timeout: float = 120.0):
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)

    def build_params(self, role: RoleSettings, request: LlmRequest) -> dict:
        system = [{"type": "text", "text": block} for block in request.system_blocks]
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        output_config: dict = {"format": {"type": "json_schema", "schema": request.schema}}
        if role.effort:
            output_config["effort"] = role.effort
        params = {
            "model": role.model,
            "max_tokens": role.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": request.user_text}],
            "output_config": output_config,
        }
        if role.thinking == "disabled":
            params["thinking"] = {"type": "disabled"}
        return params

    async def complete(self, role: RoleSettings, request: LlmRequest) -> ProviderResponse:
        try:
            response = await self._client.messages.create(**self.build_params(role, request))
        except anthropic.APIStatusError as exc:
            raise ProviderRejected(f"HTTP {exc.status_code}: {exc.message}"[:500]) from None
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise ProviderUnknownOutcome(type(exc).__name__) from None
        text = "".join(block.text for block in response.content if block.type == "text")
        u = response.usage
        usage = Usage(input_tokens=u.input_tokens or 0,
                      cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
                      cache_read_input_tokens=u.cache_read_input_tokens or 0,
                      output_tokens=u.output_tokens or 0)
        return ProviderResponse(text=text, stop_reason=response.stop_reason, usage=usage, model=response.model,
                                raw_blocks=[b.type for b in response.content])

    async def check_model(self, model: str) -> str:
        """Проверка ключа и модели без платного запроса (models.retrieve)."""
        info = await self._client.models.retrieve(model)
        return info.id

    async def aclose(self) -> None:
        await self._client.close()
