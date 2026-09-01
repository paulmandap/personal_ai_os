"""Ollama wire-format translation.

The payloads below are copied from a real Ollama 0.33.0 exchange, so these
tests pin the translation against what the server actually sends rather than
against what the adapter happens to expect.
"""

from __future__ import annotations

import json

import httpx
import pytest

from personal_ai_os.core.errors import (
    ModelProtocolError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from personal_ai_os.core.types import FinishReason, Message, Role, ToolCall, ToolSchema
from personal_ai_os.models.ollama import OllamaModel

# --- Captured from Ollama 0.33.0 -------------------------------------------

PLAIN_REPLY = {
    "model": "qwen2.5:7b-instruct",
    "created_at": "2026-08-27T12:33:11.7151441Z",
    "message": {"role": "assistant", "content": "Ready"},
    "done": True,
    "done_reason": "stop",
    "total_duration": 18712255600,
    "load_duration": 18441622500,
    "prompt_eval_count": 35,
    "eval_count": 2,
    "eval_duration": 61802000,
}

TOOL_REPLY = {
    "model": "qwen2.5:7b-instruct",
    "message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_vbgkcqae",
                "function": {
                    "index": 0,
                    "name": "get_weather",
                    "arguments": {"city": "Manila"},
                },
            }
        ],
    },
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 162,
    "eval_count": 21,
    "eval_duration": 518032000,
}

TAGS_REPLY = {
    "models": [
        {"model": "qwen2.5:7b-instruct", "name": "qwen2.5:7b-instruct"},
        {"model": "qwen2.5:3b-instruct", "name": "qwen2.5:3b-instruct"},
    ]
}

WEATHER_TOOL = ToolSchema(
    name="get_weather",
    description="Get the weather.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
)


def build_model(handler, **kwargs) -> OllamaModel:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaModel("qwen2.5:7b-instruct", client=client, **kwargs)


def static(payload, status: int = 200):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        if request.content:
            captured["body"] = json.loads(request.content)
        return httpx.Response(status, json=payload)

    handler.captured = captured  # type: ignore[attr-defined]
    return handler


class TestResponseParsing:
    def test_parses_a_plain_reply(self):
        model = build_model(static(PLAIN_REPLY))
        response = model.generate([Message.user("hi")])
        assert response.message.content == "Ready"
        assert response.message.role is Role.ASSISTANT
        assert response.finish_reason is FinishReason.STOP
        assert response.provider == "ollama"

    def test_durations_convert_nanoseconds_to_milliseconds(self):
        model = build_model(static(PLAIN_REPLY))
        usage = model.generate([Message.user("hi")]).usage
        assert usage is not None
        # eval_duration 61_802_000 ns -> 61.802 ms
        assert usage.total_duration_ms == pytest.approx(61.802)
        assert usage.load_duration_ms == pytest.approx(18441.6225)

    def test_throughput_excludes_model_load_time(self):
        """total_duration includes an 18s cold start; using it would make a
        fast model look catastrophically slow."""
        model = build_model(static(PLAIN_REPLY))
        usage = model.generate([Message.user("hi")]).usage
        assert usage is not None and usage.tokens_per_second is not None
        assert usage.tokens_per_second > 10  # not 2 tokens / 18.7 s

    def test_parses_tool_calls_and_keeps_the_server_id(self):
        model = build_model(static(TOOL_REPLY))
        response = model.generate([Message.user("weather?")], tools=[WEATHER_TOOL])
        assert response.has_tool_calls
        call = response.message.tool_calls[0]
        assert call.id == "call_vbgkcqae"
        assert call.name == "get_weather"
        assert call.arguments == {"city": "Manila"}

    def test_finish_reason_is_tool_calls_even_when_done_reason_is_stop(self):
        """Ollama reports done_reason='stop' alongside tool calls."""
        model = build_model(static(TOOL_REPLY))
        response = model.generate([Message.user("x")], tools=[WEATHER_TOOL])
        assert response.finish_reason is FinishReason.TOOL_CALLS

    def test_arguments_as_a_json_string_are_also_accepted(self):
        """OpenAI-compatible servers return arguments as a string."""
        payload = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "f", "arguments": '{"a": 1}'},
                    }
                ],
            },
        }
        model = build_model(static(payload))
        call = model.generate([Message.user("x")]).message.tool_calls[0]
        assert call.arguments == {"a": 1}

    def test_missing_tool_call_id_is_synthesised(self):
        payload = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "f", "arguments": {}}}],
            },
        }
        model = build_model(static(payload))
        call = model.generate([Message.user("x")]).message.tool_calls[0]
        assert call.id.startswith("call_")

    def test_malformed_arguments_do_not_raise_here(self):
        """Bad JSON from a model is recoverable; the loop reports it back."""
        payload = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c", "function": {"name": "f", "arguments": "{not json"}}
                ],
            },
        }
        model = build_model(static(payload))
        call = model.generate([Message.user("x")]).message.tool_calls[0]
        assert call.arguments == {"__unparsed__": "{not json"}

    def test_nameless_tool_call_is_dropped(self):
        payload = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "hi",
                "tool_calls": [{"id": "c", "function": {"arguments": {}}}],
            },
        }
        model = build_model(static(payload))
        assert model.generate([Message.user("x")]).message.tool_calls == []

    def test_a_dropped_call_survives_in_the_raw_payload(self):
        """The drop is right; losing the evidence of it was not (ADR-059).

        A nameless call plus no content parses to a turn indistinguishable from
        silence, and the agent loop then records "nothing was produced". `raw`
        is what lets `model.empty_payload` say otherwise, so stripping the
        payload before storing it would re-create the blind spot this test
        exists to pin.
        """
        payload = {
            "model": "m",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c", "function": {"arguments": {"a": 1}}}],
            },
            "eval_count": 15,
        }
        response = build_model(static(payload)).generate([Message.user("x")])

        # The parsed view is empty in both channels -- this is exactly what an
        # `empty_response` run looks like from above the seam.
        assert response.message.tool_calls == []
        assert response.message.content == ""
        # ...and the payload still holds what was dropped.
        assert response.raw["message"]["tool_calls"][0]["function"] == {
            "arguments": {"a": 1}
        }
        assert response.usage is not None
        assert response.usage.completion_tokens == 15


class TestRequestBuilding:
    def test_sends_stream_false_and_configured_options(self):
        handler = static(PLAIN_REPLY)
        model = build_model(handler, temperature=0.7, num_ctx=2048)
        model.generate([Message.user("hi")])
        body = handler.captured["body"]
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0.7
        assert body["options"]["num_ctx"] == 2048

    def test_tools_are_sent_in_openai_function_format(self):
        handler = static(PLAIN_REPLY)
        build_model(handler).generate([Message.user("x")], tools=[WEATHER_TOOL])
        tool = handler.captured["body"]["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"

    def test_no_tools_key_when_none_are_offered(self):
        handler = static(PLAIN_REPLY)
        build_model(handler).generate([Message.user("x")])
        assert "tools" not in handler.captured["body"]

    def test_assistant_tool_calls_are_echoed_back_correctly(self):
        handler = static(PLAIN_REPLY)
        build_model(handler).generate(
            [
                Message.user("x"),
                Message.assistant(
                    "", tool_calls=[ToolCall(id="c1", name="f", arguments={"a": 1})]
                ),
            ]
        )
        echoed = handler.captured["body"]["messages"][1]["tool_calls"][0]
        assert echoed["id"] == "c1"
        assert echoed["function"] == {"name": "f", "arguments": {"a": 1}}

    def test_tool_results_carry_both_accepted_spellings(self):
        """Ollama 0.33 accepts tool_name or tool_call_id+name; send both."""
        handler = static(PLAIN_REPLY)
        build_model(handler).generate(
            [Message.tool("result", name="get_weather", tool_call_id="c1")]
        )
        wire = handler.captured["body"]["messages"][0]
        assert wire["role"] == "tool"
        assert wire["tool_name"] == "get_weather"
        assert wire["name"] == "get_weather"
        assert wire["tool_call_id"] == "c1"

    def test_json_schema_becomes_the_format_field(self):
        handler = static(PLAIN_REPLY)
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        build_model(handler).generate([Message.user("x")], json_schema=schema)
        assert handler.captured["body"]["format"] == schema

    def test_max_tokens_becomes_num_predict(self):
        handler = static(PLAIN_REPLY)
        build_model(handler).generate([Message.user("x")], max_tokens=64)
        assert handler.captured["body"]["options"]["num_predict"] == 64


class TestErrorTranslation:
    def test_connection_failure_becomes_model_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(ModelUnavailableError, match="cannot reach ollama"):
            build_model(handler).generate([Message.user("x")])

    def test_timeout_becomes_model_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(ModelTimeoutError):
            build_model(handler).generate([Message.user("x")])

    def test_404_tells_you_to_pull_the_model(self):
        with pytest.raises(ModelUnavailableError, match="ollama pull"):
            build_model(static({"error": "not found"}, status=404)).generate(
                [Message.user("x")]
            )

    def test_server_error_becomes_protocol_error(self):
        with pytest.raises(ModelProtocolError, match="500"):
            build_model(static({"error": "boom"}, status=500)).generate(
                [Message.user("x")]
            )

    def test_reply_without_a_message_is_a_protocol_error(self):
        with pytest.raises(ModelProtocolError, match="no 'message'"):
            build_model(static({"done": True})).generate([Message.user("x")])


class TestHealth:
    def test_reports_available_when_installed(self):
        model = build_model(static(TAGS_REPLY))
        health = model.health()
        assert health.ok
        assert health.server_reachable

    def test_reports_missing_model_without_raising(self):
        model = build_model(static({"models": []}))
        health = model.health()
        assert health.server_reachable
        assert not health.model_available
        assert "ollama pull" in health.detail

    def test_unreachable_server_is_a_result_not_an_exception(self):
        """paios doctor depends on health() never raising."""

        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        health = build_model(handler).health()
        assert not health.server_reachable
        assert not health.ok
        assert "cannot reach" in health.detail


class TestRuntimeVersionOnHealth:
    """ADR-041: the seam's way to ask a server about itself.

    `ModelHealth.runtime_version` exists so `evaluation/` can record which
    runtime produced a result without importing a provider class.
    """

    def test_it_defaults_empty_so_other_providers_need_no_change(self):
        from personal_ai_os.core.types import ModelHealth

        h = ModelHealth(provider="p", model="m", server_reachable=True,
                        model_available=True)
        assert h.runtime_version == ""

    def test_the_fake_model_reports_no_version(self):
        """Correct, not a gap: a scripted model has no server behind it."""
        from personal_ai_os.models.fake import ScriptedModel

        assert ScriptedModel([]).health().runtime_version == ""

    def test_health_reports_the_server_version(self):
        """Read from /api/version, alongside the /api/tags reachability check."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/version":
                return httpx.Response(200, json={"version": "0.33.2"})
            return httpx.Response(
                200, json={"models": [{"model": "qwen2.5:7b-instruct"}]}
            )

        health = build_model(handler).health()
        assert health.ok
        assert health.runtime_version == "0.33.2"

    def test_a_version_endpoint_that_fails_does_not_make_the_server_unhealthy(self):
        """Provenance is optional; health is not.

        `health()` is contractually forbidden from raising, and a server that
        will not name itself is still a working server.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/version":
                return httpx.Response(500, text="nope")
            return httpx.Response(
                200, json={"models": [{"model": "qwen2.5:7b-instruct"}]}
            )

        health = build_model(handler).health()
        assert health.ok
        assert health.runtime_version == ""

    def test_an_unreachable_server_reports_no_version_and_does_not_raise(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        health = build_model(handler).health()
        assert not health.server_reachable
        assert health.runtime_version == ""
