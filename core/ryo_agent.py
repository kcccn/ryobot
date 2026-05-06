from __future__ import annotations

import json
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any, TypedDict

from pydantic import BaseModel, ValidationError

from .plugins import BasePlugin, HistorySnapshot, PluginEvent
from .skills import BaseSkill, clear_skill_context, set_skill_context

DEFAULT_FALLBACK_MESSAGE = "I'm sorry, but I couldn't complete your request right now."
TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_MAX_TOOL_RESULT_CHARS = 20000


class ChatMessage(TypedDict, total=False):
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]]
    tool_call_id: str
    reasoning_content: str | None


class RyoAgent:
    """Hexagonal application service that runs the bounded RyoAgent ReAct loop."""

    def __init__(
        self,
        *,
        persona: dict[str, Any],
        skills: list[BaseSkill],
        llm_client: Any,
        plugin: BasePlugin,
        cooldown_seconds: int = 0,
        max_iterations: int = 5,
        max_tokens: int = 4096,
    ) -> None:
        if "model" not in persona or "system_prompt" not in persona:
            raise ValueError("persona must include 'model' and 'system_prompt'.")

        self.persona = persona
        self.skills = skills
        self.llm_client = llm_client
        self.plugin = plugin
        self.cooldown_seconds = cooldown_seconds
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self._skills_by_name = {skill.name: skill for skill in skills}

    async def run(self, raw_event: Any) -> None:
        event = self.plugin.parse_event(raw_event)
        history = await self.plugin.fetch_history(event)

        if self.cooldown_seconds > 0 and history.last_bot_comment_at:
            try:
                last_at = datetime.fromisoformat(history.last_bot_comment_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
                if elapsed < self.cooldown_seconds:
                    return
            except (ValueError, TypeError):
                pass

        messages: list[ChatMessage] = [
            {"role": "system", "content": self.persona["system_prompt"]},
            *history.messages,  # type: ignore[typeddict-item]
            {"role": "user", "content": event.message},
        ]
        available_skills = self._available_skills_for_event(event)
        tools = [skill.get_tool_definition() for skill in available_skills]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)

        try:
            for _ in range(self.max_iterations):
                response = await self._create_completion(messages=messages, tools=tools)
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])

                if tool_calls:
                    msg: dict[str, Any] = {
                        "role": "assistant",
                        "content": self._extract_text_content(assistant_message),
                        "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
                    }
                    reasoning = getattr(assistant_message, "reasoning_content", None) or None
                    if reasoning:
                        msg["reasoning_content"] = reasoning
                    messages.append(msg)
                    for tool_call in tool_calls:
                        tool_result = await self._execute_tool_call(tool_call, event=event)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": getattr(tool_call, "id", ""),
                                "content": tool_result,
                            }
                        )
                    continue

                reply_text = self._extract_text_content(assistant_message).strip()
                if reply_text:
                    await self.plugin.send_reply(event, reply_text, subconscious)
                    return

                break

            await self.plugin.send_reply(event, DEFAULT_FALLBACK_MESSAGE, subconscious)
        finally:
            clear_skill_context(context_token)

    async def _create_completion(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
    ) -> Any:
        request: dict[str, Any] = {
            "model": self.persona["model"],
            "messages": messages,
            "max_tokens": self.max_tokens,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        return await self.llm_client.chat.completions.create(**request)

    def _available_skills_for_event(self, event: PluginEvent) -> list[BaseSkill]:
        if _is_trusted_mutation_author(event.author_association):
            return self.skills
        return [skill for skill in self.skills if not skill.mutates_state]

    async def _execute_tool_call(self, tool_call: Any, *, event: PluginEvent) -> str:
        tool_name = getattr(getattr(tool_call, "function", None), "name", "")
        skill = self._skills_by_name.get(tool_name)
        if skill is None:
            return f"Tool error: Unknown tool '{tool_name}'."
        if skill.mutates_state and not _is_trusted_mutation_author(event.author_association):
            return (
                f"Tool error: Tool '{tool_name}' is not available for author "
                f"association '{event.author_association}'."
            )

        try:
            raw_arguments = getattr(getattr(tool_call, "function", None), "arguments", "{}")
            parsed_arguments = self._parse_tool_arguments(raw_arguments)
            if skill.args_model is BaseModel:
                validated_arguments = {}
            else:
                validated_arguments = skill.args_model.model_validate(parsed_arguments).model_dump()
        except JSONDecodeError as exc:
            return f"Tool error: Invalid JSON for tool '{tool_name}': {exc.msg}."
        except ValidationError as exc:
            return f"Tool error: Validation failed for tool '{tool_name}': {exc}."

        try:
            result = await skill.execute(**validated_arguments)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return f"Tool error: Execution failed for tool '{tool_name}': {exc}."

        if isinstance(result, str):
            return _truncate_text(result, _max_chars_from_env("RYOBOT_MAX_TOOL_RESULT_CHARS", DEFAULT_MAX_TOOL_RESULT_CHARS))
        return _truncate_text(
            json.dumps(result, ensure_ascii=False),
            _max_chars_from_env("RYOBOT_MAX_TOOL_RESULT_CHARS", DEFAULT_MAX_TOOL_RESULT_CHARS),
        )

    @staticmethod
    def _parse_tool_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if arguments is None:
            return {}
        if isinstance(arguments, str):
            if not arguments.strip():
                return {}
            parsed = json.loads(arguments)
            if not isinstance(parsed, dict):
                raise JSONDecodeError("Tool arguments must decode to an object.", arguments, 0)
            return parsed
        raise JSONDecodeError("Tool arguments must be a JSON object.", str(arguments), 0)

    @staticmethod
    def _extract_text_content(message: Any) -> str:
        content = getattr(message, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        return str(content)

    @staticmethod
    def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
        function = getattr(tool_call, "function", None)
        return {
            "id": getattr(tool_call, "id", ""),
            "type": getattr(tool_call, "type", "function"),
            "function": {
                "name": getattr(function, "name", ""),
                "arguments": getattr(function, "arguments", "{}"),
            },
        }


def _is_trusted_mutation_author(author_association: str) -> bool:
    return author_association.upper() in TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS


def _max_chars_from_env(name: str, default: int) -> int:
    import os

    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[truncated: {omitted} chars omitted]"
