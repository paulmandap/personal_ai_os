"""Ollama provider.

Talks to Ollama's REST API directly over httpx rather than through a vendor
SDK (docs/decisions.md, ADR-003). The wire format is small enough that owning
it costs little, and it keeps the provider swap honest: llama.cpp's server,
vLLM and LM Studio all speak a near-identical dialect.

The wire details below were verified empirically against Ollama 0.33.0 rather
than assumed:

* ``message.tool_calls[].id`` **is** returned (e.g. ``"call_vbgkcqae"``).
  We still synthesise an id when it is missing, since that is a per-model
  template behaviour and not a guarantee.
* ``function.arguments`` arrives as a **dict**, not the JSON *string* the
  OpenAI API returns. Both are handled.
* Tool results are accepted with either ``tool_name`` or
  ``tool_call_id`` + ``name``. We send both.
* All durations are **nanoseconds**, and ``total_duration`` includes model
  load time -- an 18 s cold start on this hardware -- so throughput is
  computed from ``eval_duration`` alone.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from personal_ai_os.core.errors import (
    ModelProtocolError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from personal_ai_os.core.ids import new_tool_call_id
from personal_ai_os.core.model import AgentModel
from personal_ai_os.core.types import (
    FinishReason,
    Message,
    ModelHealth,
    ModelResponse,
    Role,
    ToolCall,
    ToolSchema,
    Usage,
)
from personal_ai_os.observability.logging import get_logger

log = get_logger("ollama")

PROVIDER = "ollama"
_NS_PER_MS = 1_000_000


def _ns_to_ms(value: Any) -> float | None:
    return value / _NS_PER_MS if isinstance(value, (int, float)) else None


class OllamaModel(AgentModel):
    """One configured Ollama model."""

    provider = PROVIDER

    def __init__(
        self,
        name: str,
        *,
        base_url: str = "http://localhost:11434",
        temperature: float = 0.3,
        num_ctx: int = 8192,
        request_timeout_s: float = 120.0,
        keep_alive: str = "5m",
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.request_timeout_s = request_timeout_s
        self.keep_alive = keep_alive
        self._client = client or httpx.Client(timeout=request_timeout_s)

    # --- outbound translation ---------------------------------------------

    def _message_to_wire(self, msg: Message) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": msg.role.value, "content": msg.content}

        if msg.role is Role.ASSISTANT and msg.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": tc.id,
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in msg.tool_calls
            ]

        if msg.role is Role.TOOL:
            # Ollama 0.33 accepts either spelling; sending both keeps us
            # compatible across versions and chat templates.
            wire["tool_name"] = msg.name
            wire["name"] = msg.name
            if msg.tool_call_id:
                wire["tool_call_id"] = msg.tool_call_id

        return wire

    # --- inbound translation ----------------------------------------------

    @staticmethod
    def _parse_arguments(raw: Any) -> dict[str, Any]:
        """Normalise tool arguments to a dict.

        Ollama returns a dict; OpenAI-compatible servers return a JSON string.
        A model can also emit malformed JSON, which is a *recoverable* problem:
        we surface it as an argument the agent loop will fail validation on and
        report back to the model, rather than raising here.
        """
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {"__unparsed__": raw}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        return {}

    def _parse_tool_calls(self, raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls):
            fn = raw.get("function") or {}
            name = fn.get("name")
            if not name:
                log.warning("dropping tool call with no function name: %r", raw)
                continue
            calls.append(
                ToolCall(
                    id=raw.get("id") or new_tool_call_id(index),
                    name=name,
                    arguments=self._parse_arguments(fn.get("arguments")),
                )
            )
        return calls

    def _parse_response(self, payload: dict[str, Any]) -> ModelResponse:
        raw_message = payload.get("message")
        if not isinstance(raw_message, dict):
            raise ModelProtocolError(
                f"ollama reply has no 'message' object: {str(payload)[:300]}"
            )

        tool_calls = self._parse_tool_calls(raw_message.get("tool_calls") or [])
        message = Message(
            role=Role.ASSISTANT,
            content=raw_message.get("content") or "",
            tool_calls=tool_calls,
        )

        eval_ms = _ns_to_ms(payload.get("eval_duration"))
        usage = Usage(
            prompt_tokens=payload.get("prompt_eval_count"),
            completion_tokens=payload.get("eval_count"),
            # Generation time only. total_duration includes model load, which
            # would make a cold start look like a slow model.
            total_duration_ms=eval_ms,
            load_duration_ms=_ns_to_ms(payload.get("load_duration")),
        )

        done_reason = payload.get("done_reason") or "stop"
        if tool_calls:
            finish = FinishReason.TOOL_CALLS
        elif done_reason == "length":
            finish = FinishReason.LENGTH
        else:
            finish = FinishReason.STOP

        return ModelResponse(
            message=message,
            model=payload.get("model") or self.name,
            provider=PROVIDER,
            usage=usage,
            finish_reason=finish,
            raw=payload,
        )

    # --- AgentModel --------------------------------------------------------

    def generate(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        timeout_s: float | None = None,
    ) -> ModelResponse:
        options: dict[str, Any] = {
            "temperature": self.temperature if temperature is None else temperature,
            "num_ctx": self.num_ctx,
        }
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload: dict[str, Any] = {
            "model": self.name,
            "messages": [self._message_to_wire(m) for m in messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
        }
        if tools:
            payload["tools"] = [t.to_openai_format() for t in tools]
        if json_schema is not None:
            payload["format"] = json_schema

        try:
            response = self._client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=timeout_s or self.request_timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"{self.name} did not respond within "
                f"{timeout_s or self.request_timeout_s}s"
            ) from exc
        except httpx.RequestError as exc:
            raise ModelUnavailableError(
                f"cannot reach ollama at {self.base_url}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise ModelUnavailableError(
                f"model '{self.name}' is not installed. Run: ollama pull {self.name}"
            )
        if response.status_code >= 400:
            raise ModelProtocolError(
                f"ollama returned {response.status_code}: {response.text[:300]}"
            )

        try:
            payload_out = response.json()
        except ValueError as exc:
            raise ModelProtocolError(
                f"ollama returned non-JSON: {response.text[:300]}"
            ) from exc

        return self._parse_response(payload_out)

    def health(self) -> ModelHealth:
        """Check reachability and installation without generating anything."""
        try:
            response = self._client.get(f"{self.base_url}/api/tags", timeout=10.0)
            response.raise_for_status()
            payload = response.json()
        except httpx.RequestError as exc:
            return ModelHealth(
                provider=PROVIDER,
                model=self.name,
                server_reachable=False,
                model_available=False,
                detail=f"cannot reach {self.base_url}: {exc}",
            )
        except (httpx.HTTPStatusError, ValueError) as exc:
            return ModelHealth(
                provider=PROVIDER,
                model=self.name,
                server_reachable=True,
                model_available=False,
                detail=f"unexpected /api/tags reply: {exc}",
            )

        installed = {
            m.get("model") or m.get("name") for m in payload.get("models", [])
        }
        installed.discard(None)
        available = self.name in installed or f"{self.name}:latest" in installed

        return ModelHealth(
            provider=PROVIDER,
            model=self.name,
            server_reachable=True,
            model_available=available,
            detail="" if available else f"not installed; run: ollama pull {self.name}",
        )

    def close(self) -> None:
        self._client.close()
