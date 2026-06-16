"""
core/llm_client.py — провайдер-агностичный клиент для LLM.

Переключение между Anthropic и Gemini через LLM_PROVIDER в .env.
Оба клиента поддерживают tool-use (structured output) и text-only вызовы.
Extended thinking доступен только в Anthropic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Any

import structlog

log = structlog.get_logger()


class LLMAPIError(Exception):
    """Ошибка при вызове LLM API (сетевая или от провайдера)."""


class LLMNoToolBlockError(LLMAPIError):
    """LLM не вернул function-call / tool_use блок."""


@dataclass
class LLMResult:
    tool_input: dict[str, Any] | None = None   # None если text-only вызов
    text: str | None = None                    # None если tool-call
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning: str | None = None              # thinking/CoT (только Anthropic)


class BaseLLMClient(ABC):
    """Единый интерфейс для всех LLM-провайдеров."""

    @property
    def supports_thinking(self) -> bool:
        """True если провайдер поддерживает extended thinking."""
        return False

    @abstractmethod
    async def call_tool(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        tool_name: str,
        max_tokens: int,
        temperature: float,
        use_thinking: bool = False,
    ) -> LLMResult:
        """
        Вызов с принудительным tool/function-use.
        Возвращает LLMResult.tool_input — распарсенный dict аргументов.
        """

    @abstractmethod
    async def call_text(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        """
        Вызов для свободного текстового ответа (CoT, chunking, CARVEOUT-парсинг).
        Возвращает LLMResult.text.
        """


class AnthropicLLMClient(BaseLLMClient):
    """Клиент Anthropic Claude API."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic as _anthropic
        self._anthropic = _anthropic
        self._client = _anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    @property
    def supports_thinking(self) -> bool:
        return True

    async def call_tool(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        tool_name: str,
        max_tokens: int,
        temperature: float,
        use_thinking: bool = False,
    ) -> LLMResult:
        from core.config import get_settings
        settings = get_settings()

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "system": system,
            "tools": [tool],
            "messages": messages,
        }

        if use_thinking:
            # adaptive thinking: temperature опускается, tool_choice=auto
            create_kwargs["max_tokens"] = settings.claude_decision_max_tokens_thinking
            create_kwargs["thinking"] = {"type": "adaptive"}
            create_kwargs["tool_choice"] = {"type": "auto"}
        else:
            create_kwargs["max_tokens"] = max_tokens
            create_kwargs["temperature"] = temperature
            create_kwargs["tool_choice"] = {"type": "tool", "name": tool_name}

        try:
            response = await self._client.messages.create(**create_kwargs)
        except self._anthropic.APIError as e:
            raise LLMAPIError(str(e)) from e

        reasoning: str | None = None
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                reasoning = getattr(block, "thinking", None) or None
                break

        tool_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # thinking + tool_choice=auto иногда возвращает только текст — повторяем с force
        if tool_block is None and use_thinking:
            log.warning("llm_thinking_no_tool_block_retry", model=self._model, tool=tool_name)
            try:
                retry = await self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool_name},
                    messages=messages,
                )
                input_tokens += retry.usage.input_tokens
                output_tokens += retry.usage.output_tokens
                tool_block = next(
                    (b for b in retry.content if getattr(b, "type", None) == "tool_use"),
                    None,
                )
            except self._anthropic.APIError as e:
                raise LLMAPIError(str(e)) from e

        if tool_block is None:
            raise LLMNoToolBlockError(f"Anthropic did not return tool_use block for {tool_name!r}")

        return LLMResult(
            tool_input=tool_block.input,
            text=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning=reasoning,
        )

    async def call_text(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=messages,
            )
        except self._anthropic.APIError as e:
            raise LLMAPIError(str(e)) from e

        text = "\n".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        )
        return LLMResult(
            tool_input=None,
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


class GeminiLLMClient(BaseLLMClient):
    """Клиент Google Gemini API (через google-genai SDK v2+)."""

    # Ключи JSON Schema, которые Gemini принимает в FunctionDeclaration.parameters
    _GEMINI_SCHEMA_KEYS = frozenset(
        {"type", "description", "properties", "items", "required", "nullable", "enum", "format"}
    )

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai
        from google.genai import types as gtypes
        self._client = genai.Client(api_key=api_key)
        self._gtypes = gtypes
        self._model_name = model

    @staticmethod
    def _normalize_schema(schema: Any) -> Any:
        """Рекурсивно приводит JSON Schema к формату Gemini.

        Gemini принимает только ограниченный набор полей.
        Всё лишнее (minimum, maximum, additionalProperties, $ref…) удаляется.
        "type": ["string", "null"] → "type": "string", "nullable": true
        """
        if not isinstance(schema, dict):
            return schema

        result: dict[str, Any] = {}
        nullable = False

        for k, v in schema.items():
            if k not in GeminiLLMClient._GEMINI_SCHEMA_KEYS:
                continue

            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                result[k] = non_null[0] if non_null else "string"
                if "null" in v:
                    nullable = True
            elif k == "properties" and isinstance(v, dict):
                result[k] = {
                    pk: GeminiLLMClient._normalize_schema(pv)
                    for pk, pv in v.items()
                }
            elif k == "items" and isinstance(v, dict):
                result[k] = GeminiLLMClient._normalize_schema(v)
            elif k == "required" and isinstance(v, list):
                result[k] = [str(r) for r in v]
            elif k == "enum" and isinstance(v, list):
                result[k] = [str(e) for e in v]
            else:
                result[k] = v

        if nullable:
            result["nullable"] = True
        return result

    def _to_gemini_contents(self, messages: list[dict[str, Any]]) -> list:
        """Claude message format → google-genai Content list."""
        types = self._gtypes
        result = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            content = m["content"]
            if isinstance(content, str):
                parts = [types.Part(text=content)]
            elif isinstance(content, list):
                parts = [
                    types.Part(text=item["text"])
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
            else:
                parts = [types.Part(text=str(content))]
            result.append(types.Content(role=role, parts=parts))
        return result

    async def call_tool(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        tool_name: str,
        max_tokens: int,
        temperature: float,
        use_thinking: bool = False,  # игнорируется: Gemini thinking отключается для tool-use
    ) -> LLMResult:
        types = self._gtypes

        fn_decl = types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters=self._normalize_schema(tool["input_schema"]),
        )

        config_kwargs: dict[str, Any] = dict(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            tools=[types.Tool(function_declarations=[fn_decl])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="ANY",
                    allowed_function_names=[tool_name],
                )
            ),
        )
        # Отключаем thinking чтобы не мешало function calling
        try:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except (AttributeError, TypeError):
            pass

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=self._to_gemini_contents(messages),
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as e:
            raise LLMAPIError(f"Gemini API error: {e}") from e

        tool_input: dict | None = None
        for candidate in response.candidates:
            for part in candidate.content.parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    tool_input = dict(fc.args)
                    break
            if tool_input is not None:
                break

        if tool_input is None:
            raise LLMNoToolBlockError(f"Gemini did not return function call for {tool_name!r}")

        meta = response.usage_metadata
        return LLMResult(
            tool_input=tool_input,
            text=None,
            input_tokens=getattr(meta, "prompt_token_count", 0),
            output_tokens=getattr(meta, "candidates_token_count", 0),
        )

    async def call_text(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        types = self._gtypes

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model_name,
                contents=self._to_gemini_contents(messages),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                ),
            )
        except Exception as e:
            raise LLMAPIError(f"Gemini API error: {e}") from e

        meta = response.usage_metadata
        return LLMResult(
            tool_input=None,
            text=response.text,
            input_tokens=getattr(meta, "prompt_token_count", 0),
            output_tokens=getattr(meta, "candidates_token_count", 0),
        )


def get_llm_client() -> BaseLLMClient:
    """Фабрика: возвращает LLM-клиент согласно LLM_PROVIDER в .env."""
    from core.config import get_settings
    settings = get_settings()

    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiLLMClient(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
        )

    return AnthropicLLMClient(
        api_key=settings.anthropic_api_key,
        model=settings.claude_model,
    )
