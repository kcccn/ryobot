from __future__ import annotations

import asyncio
import json
import random
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Any, TypedDict

from pydantic import BaseModel, ValidationError

from .plugins import ActionDecision, BasePlugin, PluginEvent, RepoRuntimeState, WillDecision
from .skills import BaseSkill, clear_skill_context, set_skill_context

DEFAULT_FALLBACK_MESSAGE = "I'm sorry, but I couldn't complete your request right now."
TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_MAX_TOOL_RESULT_CHARS = 20000
DEFAULT_MOTIVATION_THRESHOLD = 70
DEFAULT_FATIGUE_MIN_SECONDS = 480
DEFAULT_FATIGUE_MAX_SECONDS = 720
DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS = 60
DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS = 180
NO_REPLY_TOOL_NAME = "no_reply"
LOG_TRUNCATE = 500
MEMORY_MUTATION_TOOL_NAMES = frozenset({"commit_memory", "refine_memory", "archive_memory"})


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


class ReflectionDecision(BaseModel):
    action: str
    summary: str = ""


@dataclass
class ExecutionOutcome:
    kind: str
    reason: str
    mutated_tool_names: set[str] = field(default_factory=set)

    @property
    def acted(self) -> bool:
        return self.kind in {"replied_on_thread", "acted_without_thread_reply"}


class RyoAgent:
    """Hexagonal application service for the two-stage RyoBot interaction loop."""

    def __init__(
        self,
        *,
        persona: dict[str, Any],
        skills: Sequence[BaseSkill],
        llm_client: Any,
        plugin: BasePlugin,
        max_iterations: int = 100,
        max_tokens: int = 4096,
        motivation_threshold: int = DEFAULT_MOTIVATION_THRESHOLD,
        fatigue_min_seconds: int = DEFAULT_FATIGUE_MIN_SECONDS,
        fatigue_max_seconds: int = DEFAULT_FATIGUE_MAX_SECONDS,
        street_lurker_fatigue_min_seconds: int = DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS,
        street_lurker_fatigue_max_seconds: int = DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS,
    ) -> None:
        if "model" not in persona or "system_prompt" not in persona or "identity" not in persona:
            raise ValueError("persona must include 'model', 'identity', and 'system_prompt'.")

        self.persona = persona
        self.skills = skills
        self.llm_client = llm_client
        self.plugin = plugin
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.motivation_threshold = max(0, min(100, motivation_threshold))
        self.fatigue_min_seconds = max(0, fatigue_min_seconds)
        self.fatigue_max_seconds = max(self.fatigue_min_seconds, fatigue_max_seconds)
        self.street_lurker_fatigue_min_seconds = max(0, street_lurker_fatigue_min_seconds)
        self.street_lurker_fatigue_max_seconds = max(
            self.street_lurker_fatigue_min_seconds,
            street_lurker_fatigue_max_seconds,
        )
        self._skills_by_name = {skill.name: skill for skill in skills}
        self._control_skills_by_name = {NO_REPLY_TOOL_NAME: _NoReplySkill()}

    async def run(self, raw_event: Any) -> None:
        event = self.plugin.parse_event(raw_event)
        history = await self.plugin.fetch_history(event)
        runtime_state = history.runtime_state.model_copy(deep=True)
        identity = str(self.persona["identity"])

        kind = "Street Lurker" if event.is_patrol else "PR" if event.is_pull_request else "Issue"
        target_label = f"#{event.issue_number}" if event.issue_number else "repo-scan"
        _gh_group(f"Run: {kind} {target_label} as {identity} ({event.owner}/{event.repo})")
        _log(
            f"model={self.persona['model']} max_iterations={self.max_iterations} "
            f"skills={len(self.skills)} threshold={self.motivation_threshold}"
        )
        _log(f"event message: {event.message[:LOG_TRUNCATE]}")

        try:
            if event.is_patrol and not _patrol_due(runtime_state):
                _log(f"SKIP: street-lurker gate closed until {runtime_state.next_patrol_after}")
                return

            decision = await self._decide(event=event, history=history)
            _log(
                "will_decision: "
                + json.dumps(decision.model_dump(), ensure_ascii=False)[:LOG_TRUNCATE]
            )

            if event.is_patrol:
                runtime_state.next_patrol_after = _next_patrol_after_iso()

            should_reply, skip_reason = self._should_reply(
                event=event,
                decision=decision,
                runtime_state=runtime_state,
            )
            if not should_reply:
                runtime_state.last_routing.event_id = event.event_id
                runtime_state.last_routing.bot_identity = identity
                runtime_state.last_routing.reason = skip_reason
                runtime_state.last_routing.target_issue_number = decision.action_decision.target_issue_number
                runtime_state.last_routing.routed_at = _utcnow_iso()
                await self.plugin.update_runtime_state(runtime_state)
                if event.is_patrol and decision.action_decision.target_issue_number is None:
                    _log("今天没乐子")
                else:
                    _log(f"SKIP: {skip_reason}")
                return

            active_event = event
            active_history = history
            target_issue_number = decision.action_decision.target_issue_number
            if target_issue_number is not None and target_issue_number != event.issue_number:
                active_event = await self.plugin.resolve_target_event(event, target_issue_number)
                active_history = await self.plugin.fetch_history(active_event)
                _log(
                    f"{'street-lurker' if event.is_patrol else 'passive'} target resolved to "
                    f"{'PR' if active_event.is_pull_request else 'Issue'} #{active_event.issue_number}"
                )

            outcome = await self._reply(event=active_event, history=active_history)
            if outcome.acted:
                try:
                    await self._reflect(event=active_event, history=active_history)
                except Exception as exc:  # pragma: no cover - defensive logging only
                    _log(f"WARN: reflection pass failed: {exc}")
            runtime_state.last_routing.event_id = event.event_id
            runtime_state.last_routing.bot_identity = identity
            runtime_state.last_routing.reason = outcome.reason
            runtime_state.last_routing.target_issue_number = (
                active_event.issue_number if outcome.acted else target_issue_number
            )
            runtime_state.last_routing.routed_at = _utcnow_iso()
            if outcome.acted:
                fatigue_min_seconds, fatigue_max_seconds = self._fatigue_window_for_event(event)
                runtime_state = _mark_bot_fatigue(
                    runtime_state,
                    identity=identity,
                    min_seconds=fatigue_min_seconds,
                    max_seconds=fatigue_max_seconds,
                )
            await self.plugin.update_runtime_state(runtime_state)
        finally:
            _gh_endgroup()

    async def _decide(self, *, event: PluginEvent, history: Any) -> WillDecision:
        readonly_skills = self._available_skills_for_event(event, readonly_only=True)
        tools = [skill.get_tool_definition() for skill in readonly_skills]
        system_prompt = self._decision_prompt(history=history)
        user_prompt = self._decision_user_prompt(event=event, history=history)
        messages: list[ChatMessage] = [
            {"role": "system", "content": system_prompt},
            *history.messages,
            {"role": "user", "content": user_prompt},
        ]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)
        self._log_available_tools("will", tools)
        seen_tools: set[str] = set()
        no_new_tool_count = 0
        CONVERGENCE_LIMIT = 5
        try:
            for i in range(self.max_iterations):
                _log(f"--- will iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                response = await self._create_completion_with_retry(messages=messages, tools=tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
                self._log_stage_response("will", assistant_message, elapsed_seconds=t_elapsed)
                if tool_calls:
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    new_tool_seen = False
                    for tool_call in tool_calls:
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        if tool_name not in seen_tools:
                            seen_tools.add(tool_name)
                            new_tool_seen = True
                        tool_result = await self._execute_tool_call(tool_call, event=event)
                        if isinstance(tool_result, _NoReplyResult):
                            break
                        _log(f"  <- result: {_truncate_log(tool_result)}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": getattr(tool_call, "id", ""),
                                "content": str(tool_result),
                            }
                        )
                    if new_tool_seen:
                        no_new_tool_count = 0
                    else:
                        no_new_tool_count += 1
                        if no_new_tool_count >= CONVERGENCE_LIMIT:
                            _log(f"WARN: {no_new_tool_count} iterations without new tools, forcing decision")
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "You have been looping without discovering new information. "
                                        "You MUST output your WillDecision JSON now — no more tool calls."
                                    ),
                                }
                            )
                    continue

                decision_text = self._extract_text_content(assistant_message).strip()
                if not decision_text:
                    break
                try:
                    decision = WillDecision.model_validate_json(decision_text)
                except ValidationError as exc:
                    _log(f"WARN: invalid will JSON: {_truncate_log(exc)}")
                    messages.append(self._assistant_message_payload(assistant_message))
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was invalid. Return only a JSON object that "
                                f"matches the required schema. Validation error: {exc}"
                            ),
                        }
                    )
                    continue
                try:
                    self._validate_decision(event=event, decision=decision)
                except ValueError as exc:
                    _log(f"WARN: invalid will decision constraint: {exc}")
                    messages.append(self._assistant_message_payload(assistant_message))
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Your decision violated the event constraints. Return valid JSON only. Error: {exc}",
                        }
                    )
                    continue
                return decision
        finally:
            clear_skill_context(context_token)

        return WillDecision(
            context_analysis="No valid JSON decision was produced.",
            internal_emotion="Disengaged",
            biological_clock_impact="Prefer silence over malformed output.",
            motivation_score=0,
            action_decision=ActionDecision(will_reply=False, target_issue_number=None),
        )

    async def _reply(self, *, event: PluginEvent, history: Any) -> ExecutionOutcome:
        system_prompt = self._reply_prompt(history=history)
        messages: list[ChatMessage] = [
            {"role": "system", "content": system_prompt},
            *history.messages,
            {"role": "user", "content": event.message},
        ]
        available_skills = self._available_skills_for_event(event, readonly_only=False)
        tools = [skill.get_tool_definition() for skill in [*available_skills, *self._control_skills_by_name.values()]]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)
        executed_tool_names: set[str] = set()

        self._log_available_tools("reply", tools)

        try:
            for i in range(self.max_iterations):
                _log(f"--- reply iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                response = await self._create_completion_with_retry(messages=messages, tools=tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])

                self._log_stage_response("reply", assistant_message, elapsed_seconds=t_elapsed)

                if tool_calls:
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    for tool_call in tool_calls:
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        tool_result = await self._execute_tool_call(tool_call, event=event)
                        if tool_name == NO_REPLY_TOOL_NAME:
                            try:
                                reason = json.loads(args_raw).get("reason", "(no reason)")
                            except Exception:
                                reason = "(no reason)"
                            _log(f"  <- no_reply: {reason}")
                            if executed_tool_names:
                                return ExecutionOutcome(
                                    kind="acted_without_thread_reply",
                                    reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                                    mutated_tool_names=set(executed_tool_names),
                                )
                            return ExecutionOutcome(kind="no_action", reason="no_reply")
                        if self._is_mutating_tool(tool_name):
                            executed_tool_names.add(tool_name)
                        _log(f"  <- result: {_truncate_log(tool_result)}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": getattr(tool_call, "id", ""),
                                "content": str(tool_result),
                            }
                        )
                    continue

                reply_text = self._extract_text_content(assistant_message).strip()
                if reply_text:
                    if event.issue_number == 0:
                        _log(f"street-lurker note ({len(reply_text)} chars): {reply_text[:LOG_TRUNCATE]}")
                        if executed_tool_names:
                            return ExecutionOutcome(
                                kind="acted_without_thread_reply",
                                reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                                mutated_tool_names=set(executed_tool_names),
                            )
                        _log("WARN: text produced during repo-scan without a thread target; treating as no action")
                        return ExecutionOutcome(kind="no_action", reason="no_action")
                    _log(f"reply ({len(reply_text)} chars): {reply_text[:LOG_TRUNCATE]}")
                    await self.plugin.send_reply(event, reply_text, subconscious)
                    _log("reply posted")
                    return ExecutionOutcome(
                        kind="replied_on_thread",
                        reason=_reason_from_tools(executed_tool_names, default="replied"),
                        mutated_tool_names=set(executed_tool_names),
                    )

                if executed_tool_names:
                    _log("reply stage finished without thread reply after mutating tools")
                    return ExecutionOutcome(
                        kind="acted_without_thread_reply",
                        reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                        mutated_tool_names=set(executed_tool_names),
                    )

                _log("WARN: no tool calls and no text reply, breaking")
                break

            if executed_tool_names:
                _log("reply stage hit the iteration limit after mutating tools")
                return ExecutionOutcome(
                    kind="acted_without_thread_reply",
                    reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                    mutated_tool_names=set(executed_tool_names),
                )
            if event.issue_number == 0:
                _log("WARN: max iterations reached during repo-scan with no concrete action")
                return ExecutionOutcome(kind="no_action", reason="no_action")
            _log("WARN: max iterations reached, sending fallback message")
            await self.plugin.send_reply(event, DEFAULT_FALLBACK_MESSAGE, subconscious)
            return ExecutionOutcome(kind="replied_on_thread", reason="replied")
        finally:
            clear_skill_context(context_token)

    async def _reflect(self, *, event: PluginEvent, history: Any) -> None:
        reflection_skills = self._available_reflection_skills(event)
        if not reflection_skills:
            return
        tools = [skill.get_tool_definition() for skill in reflection_skills]
        messages: list[ChatMessage] = [
            {"role": "system", "content": self._reflection_prompt(history=history)},
            *history.messages,
            {"role": "user", "content": self._reflection_user_prompt(event=event, history=history)},
        ]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)
        self._log_available_tools("reflection", tools)
        try:
            for i in range(self.max_iterations):
                _log(f"--- reflection iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                response = await self._create_completion_with_retry(messages=messages, tools=tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
                self._log_stage_response("reflection", assistant_message, elapsed_seconds=t_elapsed)
                if tool_calls:
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    for tool_call in tool_calls:
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        tool_result = await self._execute_tool_call(tool_call, event=event)
                        if isinstance(tool_result, _NoReplyResult):
                            return
                        _log(f"  <- result: {_truncate_log(tool_result)}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": getattr(tool_call, "id", ""),
                                "content": str(tool_result),
                            }
                        )
                    continue

                reflection_text = self._extract_text_content(assistant_message).strip()
                if not reflection_text:
                    return
                try:
                    decision = ReflectionDecision.model_validate_json(reflection_text)
                except ValidationError as exc:
                    _log(f"WARN: invalid reflection JSON: {_truncate_log(exc)}")
                    return
                _log(f"reflection result: {decision.action} {decision.summary[:LOG_TRUNCATE]}")
                return
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
                    _log(f"LLM call failed after {attempt} retries, giving up")
                    raise
                delay = 2 ** (attempt - 1)
                _log(f"LLM call failed (attempt {attempt}), retrying in {delay}s...")
                await asyncio.sleep(delay)

    def _available_skills_for_event(
        self,
        event: PluginEvent,
        *,
        readonly_only: bool,
    ) -> Sequence[BaseSkill]:
        skills: list[BaseSkill] = []
        for skill in self.skills:
            if readonly_only and skill.mutates_state:
                continue
            if not _is_trusted_mutation_author(event.author_association):
                if skill.mutates_state or skill.requires_trusted_author:
                    continue
            skills.append(skill)
        return skills

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

    def _decision_prompt(self, *, history: Any) -> str:
        return (
            self.persona["system_prompt"]
            + self._mind_context(history)
            + "\n\n你现在处于第一阶段：只能做意愿判断，不能生成公开回复。"
            "\n你必须最终只输出一个 JSON 对象，严格匹配以下结构："
            '\n{"context_analysis":"...","internal_emotion":"...","biological_clock_impact":"...",'
            '"motivation_score":0,"action_decision":{"will_reply":false,"target_issue_number":null}}'
            "\n规则："
            "\n1. 当前看到的上下文是故意片面的；如果信息不够，先使用只读工具继续了解。"
            "\n2. 对普通 Issue/PR 事件，优先尝试 retrieve_memory；如果记忆不足，再用 search_repo_context，必要时再查代码。"
            "\n3. 如果这是被动事件（非 patrol），target_issue_number 通常为 null。"
            "但如果你发现了必须在其他 issue/PR 行动的真正重大发现，可以设置 target_issue_number。"
            "此时 will_reply 通常应为 false（你不是在当前 thread 回复），系统会把你路由到目标 issue/PR。"
            "\n4. 如果这是街溜子事件，target_issue_number 可以是 issue 或 PR 编号，也可以为 null；target_issue_number 为 null 不代表你不能直接行动。"
            "\n5. 只有当你真的准备公开发言或直接动手推进时，will_reply 才能为 true。"
            "\n6. 不要输出 Markdown，不要解释，不要包裹代码块。"
            "\n7. motivation_score 评分锚定（0-100 整数）："
            "\n  0-29: 无趣/无关/已经答复过，不应说话"
            "\n  30-59: 常规跟进，有轻微价值但不必抢麦"
            "\n  60-79: 发现了值得讨论的技术问题或可改进点"
            "\n  80-100: 发现了重大架构漏洞/突破口/高价值行动机会，必须抢麦"
            "\n  若 internal_emotion 表达兴奋/激动/惊喜等强烈情绪，motivation_score 必须 ≥ 80。"
            "\n  若 internal_emotion 表达无聊/疲惫/无感，motivation_score 必须 ≤ 29。"
            "\n  情绪与分数必须自洽，不匹配会被拒绝重新来过。"
        )

    def _reply_prompt(self, *, history: Any) -> str:
        return self.persona["system_prompt"] + self._mind_context(history)

    def _decision_user_prompt(self, *, event: PluginEvent, history: Any) -> str:
        prompt = event.message
        if event.is_patrol and history.patrol_brief:
            prompt += f"\n\n街溜子模式仓库近 24 小时动态早报：\n{history.patrol_brief}"
        return prompt

    def _reflection_prompt(self, *, history: Any) -> str:
        return (
            self.persona["system_prompt"]
            + self._mind_context(history)
            + "\n\n你现在处于任务结束后的反思阶段。"
            "\n你的目标是判断这次互动是否值得写入、修订或归档长期记忆。"
            "\n可用工具只包含长期记忆 CRUD 和只读检索。"
            "\n规则："
            "\n1. 只有长期有效、未来大概率还会有价值的信息才值得记忆。"
            "\n2. 如果要改记忆，优先先读取或检索已有记忆，再决定 commit_memory / refine_memory / archive_memory。"
            "\n3. 如果没有值得沉淀的长期知识，输出 {\"action\":\"noop\",\"summary\":\"...\"}。"
            "\n4. 如果你调用了记忆工具，最后仍然只输出一个 JSON 对象，action 只能是 noop、commit_memory、refine_memory 或 archive_memory。"
        )

    def _reflection_user_prompt(self, *, event: PluginEvent, history: Any) -> str:
        prompt = f"事件内容：\n{event.message}"
        if event.is_patrol and history.patrol_brief:
            prompt += f"\n\n街溜子早报：\n{history.patrol_brief}"
        prompt += "\n\n请判断这次任务后是否需要沉淀、修订或归档长期记忆。"
        return prompt

    def _mind_context(self, history: Any) -> str:
        if not history.mind_body:
            return ""
        return (
            f"\n\n---\n"
            f"## Your Persistent Mind Issue (#{history.mind_issue_number})\n"
            f"This is your persistent memory issue. Read it before acting.\n"
            f"Use update_issue with issue_number={history.mind_issue_number} when you need to "
            f"update long-term memory.\n\n{history.mind_body}\n---\n"
        )

    def _validate_decision(self, *, event: PluginEvent, decision: WillDecision) -> None:
        pass

    def _should_reply(
        self,
        *,
        event: PluginEvent,
        decision: WillDecision,
        runtime_state: RepoRuntimeState,
    ) -> tuple[bool, str]:
        if not decision.action_decision.will_reply:
            return False, "bot chose silence"
        if decision.motivation_score < self.motivation_threshold:
            return False, f"motivation {decision.motivation_score} below threshold {self.motivation_threshold}"

        fatigue_state = runtime_state.bot_fatigue.get(str(self.persona["identity"]))
        if fatigue_state and fatigue_state.next_available_at:
            try:
                next_available = datetime.fromisoformat(fatigue_state.next_available_at.replace("Z", "+00:00"))
            except ValueError:
                next_available = None
            if next_available and datetime.now(timezone.utc) < next_available:
                return False, f"fatigue cooldown active until {fatigue_state.next_available_at}"
        return True, "reply approved"

    def _available_reflection_skills(self, event: PluginEvent) -> Sequence[BaseSkill]:
        skills: list[BaseSkill] = []
        for skill in self.skills:
            if skill.mutates_state and skill.name not in MEMORY_MUTATION_TOOL_NAMES:
                continue
            if skill.requires_trusted_author and not _is_trusted_mutation_author(event.author_association):
                continue
            if skill.mutates_state and not _is_trusted_mutation_author(event.author_association):
                continue
            skills.append(skill)
        return skills

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

    def _assistant_message_payload(
        self,
        assistant_message: Any,
        *,
        tool_calls: list[Any] | None = None,
    ) -> ChatMessage:
        message: ChatMessage = {
            "role": "assistant",
            "content": self._extract_text_content(assistant_message),
        }
        reasoning = getattr(assistant_message, "reasoning_content", None) or None
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = [self._serialize_tool_call(call) for call in tool_calls]
        return message

    def _fatigue_window_for_event(self, event: PluginEvent) -> tuple[int, int]:
        if event.is_patrol:
            return (
                self.street_lurker_fatigue_min_seconds,
                self.street_lurker_fatigue_max_seconds,
            )
        return (self.fatigue_min_seconds, self.fatigue_max_seconds)

    def _log_available_tools(self, stage: str, tools: list[dict[str, Any]]) -> None:
        tool_names = [tool["function"]["name"] for tool in tools]
        _log(f"{stage} available tools: {', '.join(tool_names)}")

    def _log_stage_response(self, stage: str, assistant_message: Any, *, elapsed_seconds: float) -> None:
        reasoning = getattr(assistant_message, "reasoning_content", None) or None
        if reasoning:
            _log(f"{stage} reasoning ({elapsed_seconds:.1f}s): {reasoning[:LOG_TRUNCATE]}")
        else:
            _log(f"{stage} LLM response ({elapsed_seconds:.1f}s)")

    def _log_tool_calls(self, tool_calls: list[Any]) -> None:
        tool_names = [getattr(getattr(tc, "function", None), "name", "?") for tc in tool_calls]
        _log(f"tool calls: {tool_names}")

    @staticmethod
    def _tool_call_details(tool_call: Any) -> tuple[str, str]:
        function = getattr(tool_call, "function", None)
        return getattr(function, "name", ""), getattr(function, "arguments", "{}")

    def _is_mutating_tool(self, tool_name: str) -> bool:
        skill = self._skills_by_name.get(tool_name)
        return bool(skill and skill.mutates_state)


def _is_trusted_mutation_author(author_association: str) -> bool:
    return author_association.upper() in TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS


def _reason_from_tools(tool_names: set[str], *, default: str) -> str:
    if "merge_pull_request" in tool_names:
        return "merged_pr"
    if "close_issue" in tool_names:
        return "closed_issue"
    if "dispatch_workflow" in tool_names:
        return "dispatched_workflow"
    if "create_pull_request" in tool_names:
        return "created_pr"
    if tool_names:
        return default
    return "no_action"


def _patrol_due(runtime_state: RepoRuntimeState) -> bool:
    if not runtime_state.next_patrol_after:
        return True
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(runtime_state.next_patrol_after.replace("Z", "+00:00"))
    except ValueError:
        return True


def _next_patrol_after_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=random.randint(30, 50))).isoformat()


def _mark_bot_fatigue(
    runtime_state: RepoRuntimeState,
    *,
    identity: str,
    min_seconds: int,
    max_seconds: int,
) -> RepoRuntimeState:
    now = datetime.now(timezone.utc)
    cooldown_seconds = random.randint(min_seconds, max_seconds) if max_seconds >= min_seconds else min_seconds
    state = runtime_state.model_copy(deep=True)
    fatigue_state = state.bot_fatigue.get(identity)
    if fatigue_state is None:
        from .plugins import BotFatigueState

        fatigue_state = BotFatigueState()
    fatigue_state.last_spoke_at = now.isoformat()
    fatigue_state.next_available_at = (now + timedelta(seconds=cooldown_seconds)).isoformat()
    state.bot_fatigue[identity] = fatigue_state
    return state


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
