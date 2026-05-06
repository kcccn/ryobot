from __future__ import annotations

import json
from typing import Any


class AnthropicAdapter:
    """Wraps anthropic.AsyncAnthropic to expose an OpenAI-compatible
    ``chat.completions`` interface so RyoAgent works with both providers
    without changes to the ReAct loop.
    """

    def __init__(self, *, api_key: str, base_url: str | None = None) -> None:
        import anthropic

        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    @property
    def chat(self) -> _AnthropicChat:
        return _AnthropicChat(self._client)


class _AnthropicChat:

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def completions(self) -> _AnthropicCompletions:
        return _AnthropicCompletions(self._client)


class _AnthropicCompletions:

    def __init__(self, client: Any) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> _FakeResponse:
        model: str = kwargs["model"]
        messages: list[dict[str, Any]] = kwargs.get("messages", [])
        tools: list[dict[str, Any]] = kwargs.get("tools", [])

        system_parts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if role == "system":
                system_parts.append(content)
            elif role == "user":
                anthropic_messages.append({"role": "user", "content": content})
            elif role == "assistant":
                anthropic_messages.append(_convert_assistant(msg))
            elif role == "tool":
                anthropic_messages.append(_convert_tool(msg))

        request: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
        }
        if system_parts:
            request["system"] = "\n\n".join(system_parts)
        if tools:
            request["tools"] = [_convert_tool_def(t) for t in tools]

        response = await self._client.messages.create(**request)
        return _convert_response(response)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic request converters
# ---------------------------------------------------------------------------

def _convert_assistant(msg: dict[str, Any]) -> dict[str, Any]:
    tool_calls = msg.get("tool_calls") or []
    content = msg.get("content") or ""
    if not tool_calls:
        return {"role": "assistant", "content": content}

    blocks: list[dict[str, Any]] = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            inp = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": inp,
        })
    return {"role": "assistant", "content": blocks}


def _convert_tool(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": msg.get("tool_call_id", ""),
            "content": msg.get("content", ""),
        }],
    }


def _convert_tool_def(t: dict[str, Any]) -> dict[str, Any]:
    fn = t.get("function", {})
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


# ---------------------------------------------------------------------------
# Anthropic → OpenAI response converters
# ---------------------------------------------------------------------------

def _convert_response(response: Any) -> "_FakeResponse":
    content_blocks: list[Any] = getattr(response, "content", []) or []
    text_parts: list[str] = []
    tool_calls: list[_FakeToolCall] = []

    for block in content_blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            text_parts.append(getattr(block, "text", ""))
        elif block_type == "tool_use":
            tool_calls.append(_FakeToolCall(
                id=getattr(block, "id", ""),
                name=getattr(block, "name", ""),
                input_obj=getattr(block, "input", {}),
            ))
        elif block_type == "thinking":
            continue

    text = "\n".join(text_parts) if text_parts else ""
    message = _FakeMessage(
        content=text or None,
        tool_calls=tool_calls or None,
    )
    return _FakeResponse(
        choices=[_FakeChoice(message=message)],
        stop_reason=getattr(response, "stop_reason", None),
        usage=getattr(response, "usage", None),
    )


# ---------------------------------------------------------------------------
# Fake OpenAI-shape response objects
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, *, choices: list["_FakeChoice"], stop_reason: str | None = None, usage: Any = None) -> None:
        self.choices = choices
        self.stop_reason = stop_reason
        self.usage = usage


class _FakeChoice:
    def __init__(self, *, message: "_FakeMessage") -> None:
        self.message = message


class _FakeMessage:
    def __init__(self, *, content: str | None, tool_calls: list["_FakeToolCall"] | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeToolCall:
    def __init__(self, *, id: str, name: str, input_obj: dict[str, Any]) -> None:
        self.id = id
        self.type = "function"
        self.function = _FakeFunction(name=name, arguments=json.dumps(input_obj, ensure_ascii=False))


class _FakeFunction:
    def __init__(self, *, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments
