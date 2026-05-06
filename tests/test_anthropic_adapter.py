from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from platforms.llm.anthropic import (
    AnthropicAdapter,
    _convert_assistant,
    _convert_response,
    _convert_tool,
    _convert_tool_def,
)

# ---------------------------------------------------------------------------
# Unit tests — request converters (no network)
# ---------------------------------------------------------------------------


def test_convert_tool_def_basic() -> None:
    result = _convert_tool_def({
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the repo",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    })
    assert result["name"] == "search"
    assert result["description"] == "Search the repo"
    assert result["input_schema"]["properties"]["q"]["type"] == "string"


def test_convert_tool_def_missing_properties_defaults() -> None:
    result = _convert_tool_def({"function": {"name": "noop", "description": ""}})
    assert result["input_schema"] == {"type": "object", "properties": {}}


def test_convert_assistant_text_only() -> None:
    result = _convert_assistant({"role": "assistant", "content": "hello"})
    assert result == {"role": "assistant", "content": "hello"}


def test_convert_assistant_tool_call_without_text() -> None:
    result = _convert_assistant({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
        }],
    })
    assert result["role"] == "assistant"
    blocks = result["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["name"] == "read"
    assert blocks[0]["input"] == {"path": "a.txt"}


def test_convert_assistant_mixed_text_and_tool_calls() -> None:
    result = _convert_assistant({
        "role": "assistant",
        "content": "Let me check that.",
        "tool_calls": [{
            "id": "call_2",
            "type": "function",
            "function": {"name": "lookup", "arguments": '{"key":"val"}'},
        }],
    })
    assert result["role"] == "assistant"
    blocks = result["content"]
    assert len(blocks) == 2
    assert blocks[0] == {"type": "text", "text": "Let me check that."}
    assert blocks[1]["type"] == "tool_use"


def test_convert_assistant_malformed_json_arguments() -> None:
    result = _convert_assistant({
        "role": "assistant",
        "tool_calls": [{
            "id": "call_3",
            "function": {"name": "f", "arguments": "{not valid}"},
        }],
    })
    assert result["content"][0]["input"] == {}


def test_convert_assistant_multiple_tool_calls() -> None:
    result = _convert_assistant({
        "role": "assistant",
        "tool_calls": [
            {"id": "a", "function": {"name": "f1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "f2", "arguments": '{"x":1}'}},
        ],
    })
    blocks = result["content"]
    assert len(blocks) == 2
    assert blocks[0]["name"] == "f1"
    assert blocks[1]["name"] == "f2"


def test_convert_tool_result() -> None:
    result = _convert_tool({
        "role": "tool",
        "tool_call_id": "call_9",
        "content": "result text",
    })
    assert result["role"] == "user"
    assert result["content"] == [{
        "type": "tool_result",
        "tool_use_id": "call_9",
        "content": "result text",
    }]


# ---------------------------------------------------------------------------
# Response conversion tests
# ---------------------------------------------------------------------------

_MockTextBlock = lambda text: SimpleNamespace(type="text", text=text)  # noqa: E731
_MockToolUse = lambda id, name, inp: SimpleNamespace(type="tool_use", id=id, name=name, input=inp)  # noqa: E731
_MockThinking = lambda text: SimpleNamespace(type="thinking", thinking=text)  # noqa: E731


def _mock_response(blocks: list[Any], **kwargs: Any) -> Any:
    return SimpleNamespace(
        content=blocks,
        stop_reason=kwargs.get("stop_reason", "end_turn"),
        usage=kwargs.get("usage", SimpleNamespace(input_tokens=10, output_tokens=5)),
    )


def test_convert_response_text_only() -> None:
    resp = _mock_response([_MockTextBlock("Hello world")])
    result = _convert_response(resp)

    assert result.choices[0].message.content == "Hello world"
    assert result.choices[0].message.tool_calls is None
    assert result.stop_reason == "end_turn"
    assert result.usage.input_tokens == 10


def test_convert_response_single_tool_use() -> None:
    resp = _mock_response([_MockToolUse("id1", "get_weather", {"city": "NYC"})])
    result = _convert_response(resp)

    msg = result.choices[0].message
    assert msg.content is None
    assert len(msg.tool_calls) == 1
    assert msg.tool_calls[0].id == "id1"
    assert msg.tool_calls[0].function.name == "get_weather"
    assert json.loads(msg.tool_calls[0].function.arguments) == {"city": "NYC"}


def test_convert_response_mixed_text_and_tools() -> None:
    resp = _mock_response([
        _MockTextBlock("Sure, let me do that."),
        _MockToolUse("t1", "search", {"q": "bug"}),
    ])
    result = _convert_response(resp)

    msg = result.choices[0].message
    assert "Sure, let me do that." in (msg.content or "")
    assert len(msg.tool_calls) == 1


def test_convert_response_multiple_tool_uses() -> None:
    resp = _mock_response([
        _MockToolUse("a1", "read", {"path": "x.py"}),
        _MockToolUse("a2", "search", {"q": "test"}),
    ])
    result = _convert_response(resp)

    msg = result.choices[0].message
    assert len(msg.tool_calls) == 2
    assert msg.tool_calls[0].function.name == "read"
    assert msg.tool_calls[1].function.name == "search"


def test_convert_response_thinking_block() -> None:
    resp = _mock_response([
        _MockThinking("hmm, let me analyze this"),
        _MockTextBlock("Here is my conclusion."),
    ])
    result = _convert_response(resp)

    content = result.choices[0].message.content or ""
    assert "hmm, let me analyze this" not in content
    assert "Here is my conclusion." in content


def test_convert_response_thinking_only() -> None:
    resp = _mock_response([_MockThinking("deep thought")], stop_reason="end_turn")
    result = _convert_response(resp)

    assert result.choices[0].message.content is None
    assert result.stop_reason == "end_turn"


def test_convert_response_stop_reason_tool_use() -> None:
    resp = _mock_response(
        [_MockToolUse("t99", "dispatch", {"wf": "ci"})],
        stop_reason="tool_use",
    )
    result = _convert_response(resp)
    assert result.stop_reason == "tool_use"


# ---------------------------------------------------------------------------
# Integration tests — use the full AnthropicAdapter.create() with mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_create_text_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full adapter call with text response."""

    async def fake_create(**kwargs: Any) -> Any:
        return _mock_response([_MockTextBlock("I suggest refactoring.")])

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic",
        lambda **kw: fake_client,
    )

    adapter = AnthropicAdapter(api_key="sk-test")
    response = await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What do you think?"},
        ],
        max_tokens=500,
    )

    assert response.choices[0].message.content == "I suggest refactoring."


@pytest.mark.asyncio
async def test_adapter_create_passes_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured["system"] = kwargs.get("system", "")
        captured["messages"] = kwargs["messages"]
        return _mock_response([_MockTextBlock("ok")])

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic",
        lambda **kw: fake_client,
    )

    adapter = AnthropicAdapter(api_key="sk-test")
    await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "system", "content": "You are a reviewer."},
            {"role": "user", "content": "Check this PR."},
        ],
    )

    assert captured["system"] == [{"type": "text", "text": "You are a reviewer.", "cache_control": {"type": "ephemeral"}}]
    assert captured["messages"] == [{"role": "user", "content": "Check this PR."}]


@pytest.mark.asyncio
async def test_adapter_create_forwards_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured["max_tokens"] = kwargs.get("max_tokens", 0)
        return _mock_response([_MockTextBlock("ok")])

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic",
        lambda **kw: fake_client,
    )

    adapter = AnthropicAdapter(api_key="sk-test")
    await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=2048,
    )

    assert captured["max_tokens"] == 2048


@pytest.mark.asyncio
async def test_adapter_create_converts_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        captured["tools"] = kwargs.get("tools", [])
        return _mock_response([_MockTextBlock("done")])

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic",
        lambda **kw: fake_client,
    )

    adapter = AnthropicAdapter(api_key="sk-test")
    await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "search for bugs"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "find_bugs",
                "description": "Find bugs in the codebase",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )

    assert len(captured["tools"]) == 1
    assert captured["tools"][0]["name"] == "find_bugs"
    assert captured["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_adapter_create_round_trip_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a tool-use round trip: assistant requests tool → tool result → final text."""

    call_count = 0

    async def fake_create(**kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Assistant requests two tools
            return _mock_response([
                _MockToolUse("t10", "read_file", {"path": "a.py"}),
                _MockToolUse("t11", "search_code", {"pattern": "TODO"}),
            ], stop_reason="tool_use")
        else:
            return _mock_response([_MockTextBlock("Found 3 TODOs.")])

    fake_client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    monkeypatch.setattr(
        "anthropic.AsyncAnthropic",
        lambda **kw: fake_client,
    )

    adapter = AnthropicAdapter(api_key="sk-test")

    # Turn 1
    resp1 = await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "check code quality"}],
    )
    msg1 = resp1.choices[0].message
    assert len(msg1.tool_calls) == 2
    assert msg1.tool_calls[0].function.name == "read_file"
    assert msg1.tool_calls[1].function.name == "search_code"

    # Turn 2 — feed tool results back
    resp2 = await adapter.chat.completions.create(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "user", "content": "check code quality"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t10", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                {"id": "t11", "type": "function", "function": {"name": "search_code", "arguments": '{"pattern":"TODO"}'}},
            ]},
            {"role": "tool", "tool_call_id": "t10", "content": "content of a.py"},
            {"role": "tool", "tool_call_id": "t11", "content": "found 3 matches"},
        ],
    )
    assert resp2.choices[0].message.content == "Found 3 TODOs."
