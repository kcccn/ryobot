from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any, TypedDict

from pydantic import BaseModel, ValidationError

from .plugins import BasePlugin, PluginEvent
from .skills import BaseSkill, clear_skill_context, set_skill_context

DEFAULT_FALLBACK_MESSAGE = "I'm sorry, but I couldn't complete your request right now."
TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_MAX_TOOL_RESULT_CHARS = 20000
NO_REPLY_TOOL_NAME = "no_reply"
LOG_TRUNCATE = 500


def _log(msg: str, *, end: str = "\n") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", end=end, file=sys.stderr, flush=True)


def _gh_group(title: str) -> None:
    print(f"::group::{title}", file=sys.stderr, flush=True)


def _gh_endgroup() -> None:
    print("::endgroup::", file=sys.stderr, flush=True)


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
        skills: Sequence[BaseSkill],
        llm_client: Any,
        plugin: BasePlugin,
        cooldown_seconds: int = 0,
        max_iterations: int = 100,
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
        self._control_skills_by_name = {NO_REPLY_TOOL_NAME: _NoReplySkill()}

    async def run(self, raw_event: Any) -> None:
        event = self.plugin.parse_event(raw_event)
        history = await self.plugin.fetch_history(event)

        kind = "PR" if event.is_pull_request else "Issue"
        _gh_group(f"Run: {kind} #{event.issue_number} by {event.author} "
                  f"({event.owner}/{event.repo})")
        _log(f"model={self.persona['model']} cooldown={self.cooldown_seconds}s "
             f"max_iterations={self.max_iterations} skills={len(self.skills)}")
        _log(f"event message: {event.message[:LOG_TRUNCATE]}")
        if history.subconscious:
            _log(f"subconscious: {json.dumps(history.subconscious, ensure_ascii=False)[:LOG_TRUNCATE]}")
        _log(f"history messages: {len(history.messages)}")
        if history.mind_body:
            _log(f"mind issue #{history.mind_issue_number}: {len(history.mind_body)} chars")

        # Inject mind issue content into system prompt
        mind_context = ""
        if history.mind_body:
            mind_context = (
                f"\n\n---\n"
                f"## Your Persistent Mind Issue (#{history.mind_issue_number})\n"
                f"This is your persistent memory issue. Its current content is shown below. "
                f"It contains your accumulated memories, learnings, active context, "
                f"and recent activity. Read it carefully at the start of every run.\n"
                f"After completing your work, use update_issue with "
                f"issue_number={history.mind_issue_number} to update your mind issue "
                f"with new learnings, changing context, or anything worth remembering.\n"
                f"\n{history.mind_body}\n"
                f"---\n"
            )

        if self.cooldown_seconds > 0 and history.last_bot_comment_at:
            try:
                last_at = datetime.fromisoformat(history.last_bot_comment_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - last_at).total_seconds()
                jitter = random.uniform(0.5, 1.5)
                effective_cooldown = self.cooldown_seconds * jitter
                if elapsed < effective_cooldown:
                    _log(f"SKIP: within cooldown (elapsed={elapsed:.0f}s < "
                         f"effective_cooldown={effective_cooldown:.0f}s)")
                    _gh_endgroup()
                    return
                _log(f"cooldown passed (elapsed={elapsed:.0f}s >= "
                     f"effective_cooldown={effective_cooldown:.0f}s)")
            except (ValueError, TypeError):
                pass
        elif history.last_bot_comment_at:
            _log("cooldown disabled or no prior bot comment")

        system_prompt = self.persona["system_prompt"] + mind_context
        messages: list[ChatMessage] = [
            {"role": "system", "content": system_prompt},
            *history.messages,  # type: ignore[typeddict-item,list-item]
            {"role": "user", "content": event.message},
        ]
        available_skills = self._available_skills_for_event(event)
        tools = [skill.get_tool_definition() for skill in [*available_skills, *self._control_skills_by_name.values()]]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)

        _log(f"available tools: {', '.join(t['function']['name'] for t in tools)}")

        try:
            for i in range(self.max_iterations):
                _log(f"--- iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                response = await self._create_completion_with_retry(messages=messages, tools=tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])

                reasoning = getattr(assistant_message, "reasoning_content", None) or None
                if reasoning:
                    _log(f"reasoning ({t_elapsed:.1f}s): {reasoning[:LOG_TRUNCATE]}")
                else:
                    _log(f"LLM response ({t_elapsed:.1f}s)")

                if tool_calls:
                    tool_names = [
                        getattr(getattr(tc, "function", None), "name", "?")
                        for tc in tool_calls
                    ]
                    _log(f"tool calls: {tool_names}")
                    msg: ChatMessage = {
                        "role": "assistant",
                        "content": self._extract_text_content(assistant_message),
                        "tool_calls": [self._serialize_tool_call(call) for call in tool_calls],
                    }
                    if reasoning:
                        msg["reasoning_content"] = reasoning
                    messages.append(msg)
                    for tool_call in tool_calls:
                        tool_name = getattr(getattr(tool_call, "function", None), "name", "")
                        args_raw = getattr(getattr(tool_call, "function", None), "arguments", "{}")
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        tool_result = await self._execute_tool_call(tool_call, event=event)
                        if tool_name == NO_REPLY_TOOL_NAME:
                            # Extract reason from args
                            try:
                                reason = json.loads(args_raw).get("reason", "(no reason)")
                            except Exception:
                                reason = "(no reason)"
                            _log(f"  <- no_reply: {reason}")
                            _gh_endgroup()
                            return
                        _log(f"  <- result: {_truncate_log(tool_result)}")
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
                    _log(f"reply ({len(reply_text)} chars): {reply_text[:LOG_TRUNCATE]}")
                    await self.plugin.send_reply(event, reply_text, subconscious)
                    _log("reply posted")
                    _gh_endgroup()
                    return

                _log("WARN: no tool calls and no text reply, breaking")
                break

            _log("WARN: max iterations reached, sending fallback message")
            await self.plugin.send_reply(event, DEFAULT_FALLBACK_MESSAGE, subconscious)
            _gh_endgroup()
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

    async def _create_completion_with_retry(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        max_retries: int = 3,
    ) -> Any:
        attempt = 0
        while True:
            try:
                return await self._create_completion(messages=messages, tools=tools)
            except Exception:
                attempt += 1
                if attempt > max_retries:
                    _log("LLM call failed after {attempt} retries, giving up")
                    raise
                delay = 2 ** (attempt - 1)
                _log(f"LLM call failed (attempt {attempt}), retrying in {delay}s...")
                await asyncio.sleep(delay)

    def _available_skills_for_event(self, event: PluginEvent) -> Sequence[BaseSkill]:
        if _is_trusted_mutation_author(event.author_association):
            return self.skills
        return [
            skill
            for skill in self.skills
            if not skill.mutates_state and not skill.requires_trusted_author
        ]

    async def _execute_tool_call(self, tool_call: Any, *, event: PluginEvent) -> str | _NoReplyResult:
        tool_name = getattr(getattr(tool_call, "function", None), "name", "")
        if tool_name in self._control_skills_by_name:
            control_skill = self._control_skills_by_name[tool_name]
            return await self._execute_validated_skill(control_skill, tool_call)

        skill: BaseSkill | None = self._skills_by_name.get(tool_name)
        if skill is None:
            return f"Tool error: Unknown tool '{tool_name}'."
        if (
            (skill.mutates_state or skill.requires_trusted_author)
            and not _is_trusted_mutation_author(event.author_association)
        ):
            return (
                f"Tool error: Tool '{tool_name}' is not available for author "
                f"association '{event.author_association}'."
            )

        return await self._execute_validated_skill(skill, tool_call)

    async def _execute_validated_skill(self, skill: BaseSkill, tool_call: Any) -> str | _NoReplyResult:
        tool_name = skill.name
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
        if isinstance(result, _NoReplyResult):
            return result
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


def _truncate_log(result: Any) -> str:
    text = result if isinstance(result, str) else str(result)
    return _truncate_text(text, LOG_TRUNCATE)


class _NoReplyArgs(BaseModel):
    reason: str


class _NoReplyResult:
    pass


class _NoReplySkill(BaseSkill):
    name = NO_REPLY_TOOL_NAME
    description = (
        "Use this when you intentionally should not post a public reply, "
        "because you have no meaningful new contribution or the discussion is outside your role."
    )
    args_model = _NoReplyArgs

    async def execute(self, **kwargs: Any) -> _NoReplyResult:
        self.args_model.model_validate(kwargs)
        return _NoReplyResult()
