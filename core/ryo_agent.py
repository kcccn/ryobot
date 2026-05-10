from __future__ import annotations

import asyncio
import json
import random
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Any, TypedDict

from pydantic import BaseModel, ValidationError

from . import prompts
from .plugins import ActionDecision, BasePlugin, PluginEvent, RepoRuntimeState, ScoutDecision
from .skills import BaseSkill, clear_skill_context, set_skill_context

DEFAULT_FALLBACK_MESSAGE = "I'm sorry, but I couldn't complete your request right now."
TRUSTED_MUTATION_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_MAX_TOOL_RESULT_CHARS = 20000
DEFAULT_FATIGUE_MIN_SECONDS = 480
DEFAULT_FATIGUE_MAX_SECONDS = 720
DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS = 60
DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS = 180
NO_REPLY_TOOL_NAME = "no_reply"
LOG_TRUNCATE = 500
PASSIVE_EXECUTION_MODES = frozenset({"reply_brief", "reply_with_plan", "ask_clarifying_question", "act_directly"})
ALL_EXECUTION_MODES = PASSIVE_EXECUTION_MODES | {"stay_silent"}
VISIBLE_COMMENT_KINDS = frozenset({"response", "discussion", "handoff", "final"})
SCOUT_MAX_ITERATIONS = 8
SCOUT_MAX_TOOL_CALLS = 12
SCOUT_CONVERGENCE_LIMIT = 3
SCOUT_MAX_REPEAT_PER_RESOURCE = 2
REPLY_MAX_READONLY_ITERATIONS = 6
REPLY_CONVERGENCE_LIMIT = 4
# 在 Reply 阶段对 Scout 已读资源进行去重时，只拦截 GitHub API 读取
#（慢、有 rate limit），本地文件工具（read_file 等）放行。
_GITHUB_READ_TOOLS = frozenset({
    "read_thread_meta", "read_issue_body", "read_thread_context",
    "read_thread_comments", "read_code_diff",
})


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


@dataclass
class ExecutionOutcome:
    kind: str
    reason: str
    mutated_tool_names: set[str] = field(default_factory=set)
    visible_comment_posted: bool = False

    @property
    def acted(self) -> bool:
        return self.kind in {
            "replied_on_thread",
            "acted_without_thread_reply",
            "discussion_posted",
            "handoff_posted",
            "final_posted",
        }


@dataclass
class SessionState:
    current_identity: str
    current_event: PluginEvent
    rounds: int = 0
    created_issue_titles: set[str] = field(default_factory=set)
    closed_issue_numbers: set[int] = field(default_factory=set)


class RyoAgent:
    """Hexagonal application service for the two-stage RyoBot interaction loop."""

    def __init__(
        self,
        *,
        persona: dict[str, Any],
        persona_registry: dict[str, dict[str, Any]] | None = None,
        skills: Sequence[BaseSkill],
        llm_client: Any,
        plugin: BasePlugin,
        max_iterations: int = 50,
        max_tokens: int = 4096,
        fatigue_min_seconds: int = DEFAULT_FATIGUE_MIN_SECONDS,
        fatigue_max_seconds: int = DEFAULT_FATIGUE_MAX_SECONDS,
        street_lurker_fatigue_min_seconds: int = DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS,
        street_lurker_fatigue_max_seconds: int = DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS,
    ) -> None:
        if "model" not in persona or "system_prompt" not in persona or "identity" not in persona:
            raise ValueError("persona must include 'model', 'identity', and 'system_prompt'.")

        self.persona = persona
        self.persona_registry = dict(persona_registry or {str(persona["identity"]): dict(persona)})
        self.skills = skills
        self.llm_client = llm_client
        self.plugin = plugin
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
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
        identity = str(self.persona["identity"])
        history = await self.plugin.fetch_history(event)
        runtime_state = history.runtime_state.model_copy(deep=True)

        kind = "Street Lurker" if event.is_patrol else "PR" if event.is_pull_request else "Issue"
        target_label = f"#{event.issue_number}" if event.issue_number else "repo-scan"
        _gh_group(f"Run: {kind} {target_label} as {identity} ({event.owner}/{event.repo})")
        _log(
            f"model={self.persona['model']} max_iterations={self.max_iterations} "
            f"skills={len(self.skills)}"
        )
        _log(f"event message: {event.message[:LOG_TRUNCATE]}")

        try:
            if event.is_patrol and not event.is_workflow_dispatch and not _patrol_due(runtime_state):
                _log(f"SKIP: street-lurker gate closed until {runtime_state.next_patrol_after}")
                return

            if event.is_patrol and not event.is_workflow_dispatch:
                runtime_state.next_patrol_after = _next_patrol_after_iso()
            session = SessionState(current_identity=identity, current_event=event)
            session.rounds += 1
            final_outcome = ExecutionOutcome(kind="no_action", reason="no_action")
            final_event = event
            final_target_issue_number: int | None = None
            final_dispatcher_identity = identity
            skip_reason = ""

            active_event = session.current_event
            active_history = await self.plugin.fetch_history(active_event)
            decision, scout_brief = await self._decide(event=active_event, history=active_history, session=session)
            _log("scout_decision: " + json.dumps(decision.model_dump(), ensure_ascii=False)[:LOG_TRUNCATE])

            should_reply, skip_reason = self._should_reply(
                event=active_event,
                decision=decision,
                runtime_state=runtime_state,
                ignore_fatigue=False,
            )
            if not should_reply:
                final_outcome = ExecutionOutcome(kind="no_action", reason=skip_reason)
                final_event = active_event
                final_target_issue_number = decision.action_decision.target_issue_number
            else:
                target_issue_number = decision.action_decision.target_issue_number
                if target_issue_number is not None and target_issue_number != active_event.issue_number:
                    active_event = await self.plugin.resolve_target_event(active_event, target_issue_number)
                    active_history = await self.plugin.fetch_history(active_event)
                    _log(
                        f"{'street-lurker' if event.is_patrol else 'passive'} target resolved to "
                        f"{'PR' if active_event.is_pull_request else 'Issue'} #{active_event.issue_number}"
                    )

                outcome = await self._reply(event=active_event, history=active_history, decision=decision, session=session, scout_brief=scout_brief)
                final_outcome = outcome
                final_event = active_event
                final_target_issue_number = target_issue_number
                final_dispatcher_identity = session.current_identity

                if outcome.acted:
                    fatigue_min_seconds, fatigue_max_seconds = self._fatigue_window_for_event(active_event)
                    runtime_state = _mark_bot_fatigue(
                        runtime_state,
                        identity=session.current_identity,
                        min_seconds=fatigue_min_seconds,
                        max_seconds=fatigue_max_seconds,
                    )

            runtime_state.last_routing.event_id = event.event_id
            runtime_state.last_routing.bot_identity = session.current_identity
            runtime_state.last_routing.dispatcher_identity = final_dispatcher_identity
            runtime_state.last_routing.reason = final_outcome.reason
            runtime_state.last_routing.target_issue_number = (
                final_event.issue_number if final_outcome.acted else final_target_issue_number
            )
            runtime_state.last_routing.routed_at = _utcnow_iso()
            await self.plugin.update_runtime_state(runtime_state)
            if final_outcome.kind == "no_action":
                if event.is_patrol and final_target_issue_number is None:
                    _log("今天没乐子")
                else:
                    _log(f"SKIP: {skip_reason or final_outcome.reason}")
        finally:
            _gh_endgroup()

    def _build_scout_brief(self, resource_uses: dict[tuple[str, str], int]) -> str:
        if not resource_uses:
            return ""
        api_items = {
            k: v for k, v in resource_uses.items()
            if k[0] in _GITHUB_READ_TOOLS
        }
        if not api_items:
            return ""
        lines = ["【Scout 阶段已读取的 GitHub API 资源，无需重复调用 API】"]
        for (tool_name, resource_key), _count in sorted(api_items.items()):
            label = _scout_resource_label(tool_name, resource_key)
            lines.append(f"- {label}")
            lines.append(f"<!-- ryo:scout_key:{tool_name}:{resource_key} -->")
        return "\n".join(lines)

    async def _decide(self, *, event: PluginEvent, history: Any, session: SessionState) -> tuple[ScoutDecision, str]:
        readonly_skills = self._available_skills_for_event(event, readonly_only=True)
        tools = [skill.get_tool_definition() for skill in readonly_skills]
        system_prompt = self._decision_prompt(history=history)
        user_prompt = self._decision_user_prompt(event=event, history=history, session=session)
        messages: list[ChatMessage] = [
            {"role": "system", "content": system_prompt},
            *history.messages,
            {"role": "user", "content": user_prompt},
        ]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)
        scout_iterations = self._scout_iteration_budget(event)
        self._log_available_tools("scout", tools)
        _log(f"effective_scout_iterations={scout_iterations}")
        seen_signatures: set[str] = set()
        no_new_tool_count = 0
        total_tool_calls = 0
        json_repair_only = False
        resource_uses: dict[tuple[str, str], int] = {}
        self._scout_results: dict[tuple[str, str], str] = {}
        try:
            for i in range(scout_iterations):
                _log(f"--- scout iteration {i + 1}/{scout_iterations} ---")
                per_iteration_read_keys: set[tuple[str, str]] = set()
                t_start = time.monotonic()
                active_tools: list[dict[str, Any]] = [] if json_repair_only else tools
                response = await self._create_completion_with_retry(
                    messages=messages,
                    tools=active_tools,
                    stage="scout",
                )
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
                self._log_stage_response("scout", assistant_message, elapsed_seconds=t_elapsed)
                if tool_calls:
                    if json_repair_only:
                        _log("WARN: tool calls are no longer allowed during JSON repair")
                        # Don't append the assistant message with tool_calls — the API
                        # requires every tool_call to have a corresponding tool message.
                        # Reject inline by turning each call into a user-nudge message.
                        self._append_rejected_tool_calls(messages, tool_calls, reason="JSON repair mode — no more tool calls allowed")
                        json_repair_only = True
                        continue
                    if total_tool_calls >= SCOUT_MAX_TOOL_CALLS:
                        _log(f"WARN: scout tool-call budget exhausted at {total_tool_calls}, forcing decision")
                        self._append_rejected_tool_calls(messages, tool_calls, reason="tool-call budget exhausted")
                        self._append_scout_budget_exhausted_nudge(messages)
                        json_repair_only = True
                        continue
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    new_signature_seen = False
                    resource_limit_hit = False
                    executed_tc_ids: set[str] = set()
                    for tool_call in tool_calls:
                        if total_tool_calls >= SCOUT_MAX_TOOL_CALLS:
                            _log(f"WARN: reached scout tool-call budget {SCOUT_MAX_TOOL_CALLS}, skipping remaining tool calls")
                            json_repair_only = True
                            break
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        tool_signature = self._scout_tool_signature(tool_name, args_raw)
                        if tool_signature not in seen_signatures:
                            seen_signatures.add(tool_signature)
                            new_signature_seen = True
                        total_tool_calls += 1
                        tool_result = await self._execute_scout_tool_call(
                            tool_call,
                            event=event,
                            resource_uses=resource_uses,
                            per_iteration_read_keys=per_iteration_read_keys,
                        )
                        if isinstance(tool_result, _NoReplyResult):
                            break
                        if "Tool skipped: resource probe limit reached" in str(tool_result):
                            resource_limit_hit = True
                        _log(f"  <- result: {_truncate_log(tool_result)}")
                        tc_id = getattr(tool_call, "id", "")
                        executed_tc_ids.add(tc_id)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": str(tool_result),
                            }
                        )
                    # Ensure every tool_call has a tool message so the API never
                    # sees a dangling tool_calls message without responses.
                    for tool_call in tool_calls:
                        tc_id = getattr(tool_call, "id", "")
                        if tc_id and tc_id not in executed_tc_ids:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "content": "Tool call skipped: budget exhausted.",
                                }
                            )
                    if resource_limit_hit:
                        json_repair_only = True
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "One or more of your tool calls were rejected because you hit the resource "
                                    "probe limit. You have already read the same information more than once. "
                                    "You MUST output your ScoutDecision JSON NOW — no more tools. "
                                    "If you fail to produce valid JSON, you will fall back to stay_silent "
                                    "and all your analysis will be lost."
                                ),
                            }
                        )
                    if json_repair_only:
                        self._append_scout_budget_exhausted_nudge(messages)
                    if new_signature_seen:
                        no_new_tool_count = 0
                    else:
                        no_new_tool_count += 1
                        if no_new_tool_count >= SCOUT_CONVERGENCE_LIMIT:
                            _log(f"WARN: {no_new_tool_count} iterations without new tool signatures, forcing decision")
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "You have been looping without discovering new information. "
                                        "You MUST output your ScoutDecision JSON NOW. "
                                        "If you fail to produce valid JSON this iteration, you will fall back to "
                                        '"stay_silent" and all your analysis will be lost. '
                                        "No more tool calls."
                                    ),
                                }
                            )
                            json_repair_only = True
                    continue

                decision_text = self._extract_text_content(assistant_message).strip()
                if not decision_text:
                    break
                try:
                    cleaned = _extract_safe_json(decision_text)
                    decision = ScoutDecision.model_validate(cleaned)
                except (ValidationError, JSONDecodeError) as exc:
                    _log(f"WARN: invalid scout JSON: {_truncate_log(exc)}")
                    truncated = self._log_possible_json_truncation(
                        stage="scout",
                        raw_text=decision_text,
                        exc=exc,
                    )
                    messages.append(self._assistant_message_payload(assistant_message))
                    detail = _hallucinated_tool_call_hint(decision_text)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response was invalid. {detail}"
                                "Return ONLY a bare JSON object matching the required schema "
                                "— no markdown, no code blocks, no tool-call syntax. "
                                f"{'Keep it shorter and prioritize a complete JSON object. ' if truncated else ''}"
                                f"Validation error: {exc}"
                            ),
                        }
                    )
                    json_repair_only = True
                    continue
                try:
                    self._validate_decision(event=event, decision=decision, session=session)
                except ValueError as exc:
                    _log(f"WARN: invalid scout decision constraint: {exc}")
                    messages.append(self._assistant_message_payload(assistant_message))
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Your decision violated the event constraints. Return valid JSON only. Error: {exc}",
                        }
                    )
                    continue
                scout_brief = self._build_scout_brief(resource_uses)
                return decision, scout_brief
        finally:
            clear_skill_context(context_token)

        return ScoutDecision(
            context_analysis="No valid JSON.",
            internal_emotion="Quiet",
            biological_clock_impact="Prefer silence.",
            action_decision=ActionDecision(
                mode="stay_silent",
                will_reply=False,
                will_act=False,
                target_issue_number=None,
                comment_kind="final",
            ),
        ), ""

    async def _reply(
        self,
        *,
        event: PluginEvent,
        history: Any,
        decision: ScoutDecision,
        session: SessionState,
        scout_brief: str = "",
    ) -> ExecutionOutcome:
        system_prompt = self._reply_prompt(history=history, event=event, decision=decision, scout_brief=scout_brief)
        messages: list[ChatMessage] = [
            {"role": "system", "content": system_prompt},
            *history.messages,
            {"role": "user", "content": event.message},
        ]
        available_skills = self._available_skills_for_event(event, readonly_only=False)
        tools = [skill.get_tool_definition() for skill in [*available_skills, *self._control_skills_by_name.values()]]
        subconscious = dict(history.subconscious)
        context_token = set_skill_context(event=event, subconscious=subconscious)
        scout_read_keys: set[tuple[str, str]] = _parse_scout_brief_keys(scout_brief)
        executed_tool_names: set[str] = set()

        self._log_available_tools("reply", tools)
        no_new_mutation_count = 0
        _prev_mutated: set[str] = set()

        try:
            for i in range(self.max_iterations):
                _log(f"--- reply iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                response = await self._create_completion_with_retry(
                    messages=messages,
                    tools=tools,
                    stage="reply",
                )
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])

                self._log_stage_response("reply", assistant_message, elapsed_seconds=t_elapsed)

                if tool_calls:
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    has_terminal_mutation = False
                    terminal_tool_names: list[str] = []
                    no_reply_requested = False
                    no_reply_reason = "(no reason)"
                    for tool_call in tool_calls:
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        tool_result: str | _NoReplyResult = self._session_mutation_preflight(
                            session=session,
                            event=event,
                            tool_name=tool_name,
                            args_raw=args_raw,
                        )
                        if tool_result is None and tool_name in _GITHUB_READ_TOOLS:
                            resource_key = self._scout_resource_key(tool_name, args_raw)
                            if resource_key is not None and resource_key in scout_read_keys:
                                cached = getattr(self, '_scout_results', {}).get(resource_key)
                                if cached is not None:
                                    tool_result = cached
                                else:
                                    tool_result = (
                                        f"Tool skipped: resource {resource_key[0]}({resource_key[1]}) "
                                        "was already read during the Scout phase. "
                                        "Use the scout brief above instead of re-reading."
                                    )
                        if tool_result is None:
                            tool_result = await self._execute_tool_call(tool_call, event=event)
                        if tool_name == NO_REPLY_TOOL_NAME:
                            try:
                                no_reply_reason = json.loads(args_raw).get("reason", "(no reason)")
                            except Exception:
                                no_reply_reason = "(no reason)"
                            _log(f"  <- no_reply: {no_reply_reason}")
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": getattr(tool_call, "id", ""),
                                    "content": f"no_reply executed: {no_reply_reason}",
                                }
                            )
                            no_reply_requested = True
                            continue
                        if self._is_mutating_tool(tool_name):
                            executed_tool_names.add(tool_name)
                            self._record_session_mutation(
                                session=session,
                                event=event,
                                tool_name=tool_name,
                                args_raw=args_raw,
                            )
                        _log(f"  <- result: {_truncate_log(tool_result)}")
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": getattr(tool_call, "id", ""),
                                "content": str(tool_result),
                            }
                        )
                        if self._is_terminal_visible_mutation(
                            event=event,
                            skill=self._skills_by_name.get(tool_name),
                            tool_result=str(tool_result),
                        ):
                            has_terminal_mutation = True
                            terminal_tool_names.append(tool_name)
                    if has_terminal_mutation:
                        _log(
                            "Terminal visible mutation batch completed; ending this passive session now "
                            f"(executed={len(tool_calls)} terminal={terminal_tool_names})"
                        )
                        return ExecutionOutcome(
                            kind="final_posted",
                            reason=_reason_from_tools(executed_tool_names, default=terminal_tool_names[0] if terminal_tool_names else "acted_without_comment"),
                            mutated_tool_names=set(executed_tool_names),
                            visible_comment_posted=True,
                        )
                    if no_reply_requested:
                        if not event.is_patrol and not executed_tool_names:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "This is a passive human-triggered event. You must respond or do real work here. "
                                        "no_reply is not allowed unless you already completed a mutating action. "
                                        "Continue and produce a visible response or action."
                                    ),
                                }
                            )
                            continue
                        if executed_tool_names:
                            return ExecutionOutcome(
                                kind="acted_without_thread_reply",
                                reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                                mutated_tool_names=set(executed_tool_names),
                            )
                        return ExecutionOutcome(kind="no_action", reason="no_reply")

                    any_new_mutation = bool(executed_tool_names - _prev_mutated)
                    _prev_mutated = set(executed_tool_names)
                    if any_new_mutation:
                        no_new_mutation_count = 0
                    else:
                        no_new_mutation_count += 1
                        if no_new_mutation_count >= REPLY_CONVERGENCE_LIMIT:
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        f"You have had {no_new_mutation_count} iterations without new progress "
                                        "toward your focus_summary. Execute the core action now. "
                                        "Produce visible text output or a terminal mutation immediately."
                                    ),
                                }
                            )
                    if not executed_tool_names and i + 1 >= REPLY_MAX_READONLY_ITERATIONS:
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"You've spent {i + 1} iterations reading but haven't performed any action. "
                                    "Stop researching and execute your focus_summary now."
                                ),
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
                    outcome_kind = "replied_on_thread"
                    if decision.action_decision.comment_kind == "discussion":
                        outcome_kind = "discussion_posted"
                    elif decision.action_decision.comment_kind == "handoff":
                        outcome_kind = "handoff_posted"
                    elif decision.action_decision.comment_kind == "final":
                        outcome_kind = "final_posted"
                    return ExecutionOutcome(
                        kind=outcome_kind,
                        reason=self._comment_reason(decision=decision, executed_tool_names=executed_tool_names),
                        mutated_tool_names=set(executed_tool_names),
                        visible_comment_posted=True,
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
            return ExecutionOutcome(kind="final_posted", reason="replied", visible_comment_posted=True)
        finally:
            clear_skill_context(context_token)

    async def _create_completion(
        self,
        *,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        stage: str,
    ) -> Any:
        max_tokens = self.max_tokens
        if stage == "scout":
            max_tokens = max(max_tokens, 4096)
        request: dict[str, Any] = {
            "model": self.persona["model"],
            "messages": messages,
            "max_tokens": max_tokens,
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
        stage: str,
        max_retries: int = 3,
    ) -> Any:
        attempt = 0
        while True:
            try:
                return await self._create_completion(messages=messages, tools=tools, stage=stage)
            except Exception:
                attempt += 1
                if attempt > max_retries:
                    _log(f"LLM call failed after {attempt} retries, giving up")
                    raise
                delay = 2 ** (attempt - 1)
                _log(f"LLM call failed (attempt {attempt}), retrying in {delay}s...")
                await asyncio.sleep(delay)

    @staticmethod
    def _scout_iteration_budget(event: PluginEvent) -> int:
        return 16 if event.is_patrol else 8

    def _scout_tool_signature(self, tool_name: str, args_raw: str) -> str:
        try:
            parsed_args = self._parse_tool_arguments(args_raw)
        except JSONDecodeError:
            return f"{tool_name}:<invalid-json>"
        normalized_args = json.dumps(parsed_args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"{tool_name}:{normalized_args}"

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
            if event.issue_number == 0 and skill.name in {"read_issue_memory", "read_thread_context"}:
                continue
            if not _is_trusted_mutation_author(event.author_association):
                if skill.mutates_state or skill.requires_trusted_author:
                    continue
            skills.append(skill)
        return skills

    @staticmethod
    def _append_scout_budget_exhausted_nudge(messages: list[ChatMessage]) -> None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your scout-stage tool-call budget is exhausted. "
                    "You MUST output the raw ScoutDecision JSON on the next turn. "
                    "Do not call any more tools. Do not wrap the JSON in markdown or explanation."
                ),
            }
        )

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

    async def _execute_scout_tool_call(
        self,
        tool_call: Any,
        *,
        event: PluginEvent,
        resource_uses: dict[tuple[str, str], int],
        per_iteration_read_keys: set[tuple[str, str]] | None = None,
    ) -> str | _NoReplyResult:
        tool_name, args_raw = self._tool_call_details(tool_call)
        resource_key = self._scout_resource_key(tool_name, args_raw)
        # Prevent redundant reads within the same iteration
        if per_iteration_read_keys is not None and resource_key is not None:
            # Also check sibling keys for same-iteration dedup
            any_hit = resource_key in per_iteration_read_keys
            if not any_hit and resource_key[0] == "read_thread_context" and event.issue_number > 0:
                any_hit = ("read_issue_body", str(event.issue_number)) in per_iteration_read_keys
            if not any_hit and resource_key[0] == "read_issue_body":
                try:
                    parsed = self._parse_tool_arguments(args_raw)
                    if int(parsed.get("issue_number", 0) or 0) == event.issue_number:
                        any_hit = ("read_thread_context", "0") in per_iteration_read_keys
                except Exception:
                    pass
            if any_hit:
                return (
                    f"Tool skipped: already read {resource_key[0]}({resource_key[1]}) "
                    "in this iteration. Use the information you already have."
                )
        # Cross-reference: read_thread_context and read_issue_body on the same
        # thread return the same content, so share their resource quota.
        sibling_keys: list[tuple[str, str]] = []
        if resource_key is not None:
            if resource_key[0] == "read_thread_context" and event.issue_number > 0:
                sibling_keys.append(("read_issue_body", str(event.issue_number)))
            elif resource_key[0] == "read_issue_body":
                try:
                    parsed = self._parse_tool_arguments(args_raw)
                    if int(parsed.get("issue_number", 0) or 0) == event.issue_number:
                        sibling_keys.append(("read_thread_context", "0"))
                except Exception:
                    pass
        keys_to_check = [resource_key] + sibling_keys if resource_key is not None else []
        if any(
            resource_uses.get(key, 0) >= SCOUT_MAX_REPEAT_PER_RESOURCE
            for key in keys_to_check
        ):
            return (
                f"Tool skipped: resource probe limit reached for {resource_key[0] if resource_key else tool_name} "
                f"'{resource_key[1] if resource_key else ''}'. Output the ScoutDecision JSON instead of repeating this check."
            )
        if resource_key is not None:
            resource_uses[resource_key] = resource_uses.get(resource_key, 0) + 1
            for sibling in sibling_keys:
                resource_uses[sibling] = resource_uses.get(sibling, 0) + 1
            if per_iteration_read_keys is not None:
                per_iteration_read_keys.add(resource_key)
                for sibling in sibling_keys:
                    per_iteration_read_keys.add(sibling)
        result = await self._execute_tool_call(tool_call, event=event)
        if resource_key is not None and tool_name in _GITHUB_READ_TOOLS and not isinstance(result, _NoReplyResult):
            self._scout_results[resource_key] = result
        return result

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
        return prompts.build_decision_prompt(
            system_prompt=self.persona["system_prompt"],
            mind_context=self._mind_context(history),
        )

    def _reply_prompt(self, *, history: Any, event: PluginEvent, decision: ScoutDecision, scout_brief: str = "") -> str:
        return prompts.build_reply_prompt(
            system_prompt=self.persona["system_prompt"],
            mind_context=self._mind_context(history),
            event=event,
            decision=decision,
            scout_brief=scout_brief,
        )

    def _decision_user_prompt(self, *, event: PluginEvent, history: Any, session: SessionState | None = None) -> str:
        return prompts.build_decision_user_prompt(
            event=event,
            history=history,
            session=session,
        )

    def _mind_context(self, history: Any) -> str:
        return prompts.build_mind_context(
            mind_body=history.mind_body,
            mind_issue_number=history.mind_issue_number,
        )

    def _validate_decision(self, *, event: PluginEvent, decision: ScoutDecision, session: SessionState) -> None:
        mode = decision.action_decision.mode
        if mode == "stay_silent" and (decision.action_decision.will_reply or decision.action_decision.will_act):
            inferred_mode = "act_directly" if decision.action_decision.will_act and not decision.action_decision.will_reply else "reply_with_plan"
            decision.action_decision.mode = inferred_mode
            mode = inferred_mode
        if mode not in ALL_EXECUTION_MODES:
            raise ValueError(
                f"action_decision.mode must be one of {sorted(ALL_EXECUTION_MODES)}, got {mode!r}"
            )
        if decision.action_decision.comment_kind not in VISIBLE_COMMENT_KINDS:
            raise ValueError(
                f"comment_kind must be one of {sorted(VISIBLE_COMMENT_KINDS)}, got {decision.action_decision.comment_kind!r}"
            )
        if mode == "stay_silent":
            if decision.action_decision.will_reply or decision.action_decision.will_act:
                raise ValueError("stay_silent decisions cannot also set will_reply or will_act true.")
            if not event.is_patrol:
                raise ValueError("Passive human-triggered events may not choose stay_silent.")
            return
        if not (decision.action_decision.will_reply or decision.action_decision.will_act):
            raise ValueError("Non-silent decisions must set will_reply or will_act.")
        if not decision.action_decision.focus_summary.strip():
            raise ValueError("Non-silent decisions must provide a non-empty focus_summary.")
        if any(issue_number <= 0 for issue_number in decision.action_decision.context_issue_numbers):
            raise ValueError("context_issue_numbers may only contain positive issue numbers.")
        if decision.action_decision.execution_identity not in {"self", *self.persona_registry.keys()}:
            raise ValueError(f"Unknown execution_identity: {decision.action_decision.execution_identity!r}")

    def _should_reply(
        self,
        *,
        event: PluginEvent,
        decision: ScoutDecision,
        runtime_state: RepoRuntimeState,
        ignore_fatigue: bool = False,
    ) -> tuple[bool, str]:
        if decision.action_decision.mode == "stay_silent":
            return False, "bot chose silence"
        if not event.is_patrol:
            return True, "passive event requires response"
        if not (decision.action_decision.will_reply or decision.action_decision.will_act):
            return False, "street-lurker found no concrete action"

        if ignore_fatigue:
            return True, "session continuation approved"

        fatigue_state = runtime_state.bot_fatigue.get(str(self.persona["identity"]))
        if fatigue_state and fatigue_state.next_available_at:
            try:
                next_available = datetime.fromisoformat(fatigue_state.next_available_at.replace("Z", "+00:00"))
            except ValueError:
                next_available = None
            if next_available and datetime.now(timezone.utc) < next_available:
                return False, f"fatigue cooldown active until {fatigue_state.next_available_at}"
        return True, "reply approved"

    def _comment_reason(self, *, decision: ScoutDecision, executed_tool_names: set[str]) -> str:
        if decision.action_decision.comment_kind == "discussion":
            return "discussion_posted"
        if decision.action_decision.comment_kind == "handoff":
            return "handoff_posted"
        if decision.action_decision.comment_kind == "final":
            return "finalized"
        if executed_tool_names:
            return _reason_from_tools(executed_tool_names, default="acted_without_comment")
        return "replied"

    def _session_mutation_preflight(
        self,
        *,
        session: SessionState,
        event: PluginEvent,
        tool_name: str,
        args_raw: str,
    ) -> str | None:
        if tool_name == "create_issue":
            try:
                parsed = self._parse_tool_arguments(args_raw)
            except JSONDecodeError:
                return None
            title = str(parsed.get("title") or "").strip()
            if title and title in session.created_issue_titles:
                return f"Tool blocked: this session already created an issue titled '{title}'."
        if tool_name == "close_issue":
            try:
                parsed = self._parse_tool_arguments(args_raw)
            except JSONDecodeError:
                return None
            issue_number = int(parsed.get("issue_number") or event.issue_number or 0)
            if issue_number > 0 and issue_number in session.closed_issue_numbers:
                return f"Tool blocked: issue #{issue_number} was already closed earlier in this session."
        return None

    def _record_session_mutation(
        self,
        *,
        session: SessionState,
        event: PluginEvent,
        tool_name: str,
        args_raw: str,
    ) -> None:
        try:
            parsed = self._parse_tool_arguments(args_raw)
        except JSONDecodeError:
            return
        if tool_name == "create_issue":
            title = str(parsed.get("title") or "").strip()
            if title:
                session.created_issue_titles.add(title)
        elif tool_name == "close_issue":
            issue_number = int(parsed.get("issue_number") or event.issue_number or 0)
            if issue_number > 0:
                session.closed_issue_numbers.add(issue_number)

    @staticmethod
    def _is_tool_error_result(tool_result: str) -> bool:
        return (
            tool_result.startswith("Tool error:")
            or tool_result.startswith("GitHub API error")
        )

    @staticmethod
    def _is_terminal_visible_mutation(
        *,
        event: PluginEvent,
        skill: BaseSkill | None,
        tool_result: str = "",
    ) -> bool:
        if not event.is_patrol:
            return False
        if skill is None or not skill.terminal_mutation:
            return False
        return not RyoAgent._is_tool_error_result(tool_result)

    @staticmethod
    def _log_possible_json_truncation(*, stage: str, raw_text: str, exc: Exception) -> bool:
        if not _looks_like_truncated_json(raw_text):
            return False
        tail = _truncate_log(raw_text[-160:]) if raw_text else ""
        _log(
            "[CRITICAL] JSON 解析失败！疑似命中 max_tokens 截断，请检查 LLM 的 token 上限配置或要求模型更精简！ "
            f"stage={stage} tail={tail}"
        )
        return True

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

    @staticmethod
    def _append_rejected_tool_calls(
        messages: list[ChatMessage],
        tool_calls: list[Any],
        *,
        reason: str,
        output_name: str = "ScoutDecision",
        fallback_name: str = "stay_silent",
    ) -> None:
        """Append a user message explaining that tool calls were rejected.

        Does NOT append the assistant message with tool_calls because the API
        requires every tool_call_id to have a corresponding tool message.
        """
        names = [RyoAgent._tool_call_details(tc)[0] for tc in tool_calls]
        rejected = ", ".join(names)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your tool calls ({rejected}) were rejected: {reason}. "
                    "ALL tools have been permanently removed — you CANNOT call any more tools, "
                    "regardless of what the system prompt says about gathering information. "
                    "You must synthesize what you already know. "
                    f"Output ONLY the {output_name} JSON object, with no surrounding text. "
                    f"If you fail to produce valid JSON, you will fall back to {fallback_name} "
                    "and all your analysis will be lost."
                ),
            }
        )

    def _is_mutating_tool(self, tool_name: str) -> bool:
        skill = self._skills_by_name.get(tool_name)
        return bool(skill and skill.mutates_state)

    def _scout_resource_key(self, tool_name: str, args_raw: str) -> tuple[str, str] | None:
        try:
            args = self._parse_tool_arguments(args_raw)
        except JSONDecodeError:
            return None
        if tool_name in {"read_issue_body", "read_thread_comments", "read_thread_meta", "read_code_diff"}:
            return (tool_name, str(args.get("issue_number", args.get("pr_number", 0)) or 0))
        if tool_name in {"read_file", "list_files"}:
            return (tool_name, str(args.get("path", "")).strip())
        if tool_name == "get_project_tree":
            ref = str(args.get("ref", "")).strip()
            depth = str(args.get("max_depth", 4))
            return (tool_name, f"{ref}|{depth}")
        if tool_name == "find_file_paths":
            ref = str(args.get("ref", "")).strip()
            keyword = _normalize_query_key(str(args.get("keyword", "")).strip())
            return (tool_name, f"{ref}|{keyword}")
        if tool_name == "search_symbol":
            ref = str(args.get("ref", "")).strip()
            symbol_name = str(args.get("symbol_name", "")).strip().lower()
            return (tool_name, f"{ref}|{symbol_name}")
        if tool_name in {"search_issues", "search_repo_context", "retrieve_memory", "search_code"}:
            return (tool_name, _normalize_query_key(str(args.get("query", "")).strip()))
        normalized_args = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return (tool_name, normalized_args)


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


def _normalize_query_key(query: str) -> str:
    return " ".join(query.lower().split())


def _scout_resource_label(tool_name: str, resource_key: str) -> str:
    if tool_name == "read_issue_body":
        return f"read_issue_body(#{resource_key})"
    if tool_name == "read_thread_comments":
        return f"read_thread_comments(#{resource_key})"
    if tool_name == "read_code_diff":
        return f"read_code_diff(PR #{resource_key})"
    if tool_name == "read_thread_meta":
        return f"read_thread_meta(#{resource_key})"
    if tool_name == "get_project_tree":
        parts = resource_key.split("|")
        depth = parts[1] if len(parts) > 1 and parts[1] else "4"
        return f"get_project_tree (depth={depth})"
    if tool_name == "find_file_paths":
        parts = resource_key.split("|")
        keyword = parts[1] if len(parts) > 1 else ""
        return f"find_file_paths(keyword={keyword})"
    if tool_name == "search_symbol":
        parts = resource_key.split("|")
        symbol = parts[1] if len(parts) > 1 else resource_key
        return f"search_symbol({symbol})"
    if tool_name in {"search_issues", "search_repo_context", "retrieve_memory", "search_code"}:
        return f"{tool_name}(query={resource_key[:60]})"
    if tool_name == "read_thread_context":
        return "read_thread_context (current thread body)"
    if tool_name in {"read_file", "list_files"}:
        return f"{tool_name}({resource_key})"
    return f"{tool_name}({resource_key[:80]})"




_SCOUT_KEY_RE = re.compile(r"<!--\s*ryo:scout_key:(\w+):(.+?)\s*-->")


def _parse_scout_brief_keys(scout_brief: str) -> set[tuple[str, str]]:
    """Parse structured scout key markers from scout_brief text."""
    if not scout_brief:
        return set()
    return {
        (match.group(1), match.group(2).strip())
        for match in _SCOUT_KEY_RE.finditer(scout_brief)
    }


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


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OUTER_BRACES_RE = re.compile(r"(\{.*\})", re.DOTALL)


def _extract_safe_json(raw_text: str) -> Any:
    """Extract a JSON object from raw LLM text that may contain markdown fences or explanation."""
    m = _JSON_FENCE_RE.search(raw_text)
    if m:
        return json.loads(m.group(1))
    m = _OUTER_BRACES_RE.search(raw_text)
    if m:
        return json.loads(m.group(1))
    return json.loads(raw_text)


def _looks_like_truncated_json(raw_text: str) -> bool:
    text = raw_text.rstrip()
    if not text:
        return False
    if text.count("{") != text.count("}"):
        return True
    if len(re.findall(r'(?<!\\)"', text)) % 2 == 1:
        return True
    if text.endswith(("}", "]")):
        return False
    if re.search(r'"[^"]*$', text):
        return True
    if re.search(r':\s*"[^"]*$', text):
        return True
    if re.search(r',\s*"[^"]*$', text):
        return True
    if re.search(r':\s*[^,\]\}\s]+$', text) and not text.endswith(("}", "]")):
        return True
    return False


_HALLUCINATED_TOOL_PATTERNS = (
    "tool_calls",
    "CDATA",
    "！！！",
    "<function_call>",
    "<tool_call>",
    "</function_call>",
    "</tool_call>",
)


def _hallucinated_tool_call_hint(text: str) -> str:
    """Return a targeted hint if the text looks like hallucinated tool calls."""
    try:
        _extract_safe_json(text)
        return ""
    except (JSONDecodeError, ValueError):
        pass
    lower = text.lower()
    if any(p.lower() in lower for p in _HALLUCINATED_TOOL_PATTERNS):
        return (
            "You appear to be trying to emit tool calls as text — tools are NOT available "
            "and raw tool-call syntax will never be accepted. "
        )
    return ""


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
