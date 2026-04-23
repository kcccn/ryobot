from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any

from pydantic import BaseModel, ValidationError

from .plugins import BasePlugin, HistorySnapshot, PluginEvent
from .skills import BaseSkill, clear_skill_context, set_skill_context

DEFAULT_FALLBACK_MESSAGE = "I'm sorry, but I couldn't complete your request right now."


class NexusAgent:
    """Hexagonal application service that runs a bounded ReAct loop."""

    def __init__(
        self,
        *,
        persona: dict[str, Any],
        skills: list[BaseSkill],
        llm_client: Any,
        plugin: BasePlugin,
    ) -> None:
        if "model" not in persona or "system_prompt" not in persona:
            raise ValueError("persona must include 'model' and 'system_prompt'.")

        self.persona = persona
        self.skills = skills
        self.llm_client = llm_client
        self.plugin = plugin
        self._skills_by_name = {skill.name: skill for skill in skills}

    async def run(self, raw_event: Any) -> None:
        event = self.plugin.parse_event(raw_event)
        history = await self.plugin.fetch_history(event)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.persona["system_prompt"]},
            *history.messages,
            {"role": "user", "content": event.message},
        ]
        tools = [skill.get_tool_definition() for skill in self.skills]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)

        try:
            for _ in range(5):
                response = await self._create_completion(messages=messages, tools=tools)
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])

                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": self._extract_text_content(assistant_message),
                            "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
                        }
                    )
                    for tool_call in tool_calls:
                        tool_result = await self._execute_tool_call(tool_call)
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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Any:
        request: dict[str, Any] = {
            "model": self.persona["model"],
            "messages": messages,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        return await self.llm_client.chat.completions.create(**request)

    async def _execute_tool_call(self, tool_call: Any) -> str:
        tool_name = getattr(getattr(tool_call, "function", None), "name", "")
        skill = self._skills_by_name.get(tool_name)
        if skill is None:
            return f"Tool error: Unknown tool '{tool_name}'."

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
            return result
        return json.dumps(result, ensure_ascii=False)

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
