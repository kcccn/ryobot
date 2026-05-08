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
MEMORY_REFLECTION_TOOL_NAMES = frozenset(
    {"retrieve_memory", "search_repo_memory", *MEMORY_MUTATION_TOOL_NAMES}
)
PASSIVE_EXECUTION_MODES = frozenset({"reply_brief", "reply_with_plan", "ask_clarifying_question", "act_directly"})
ALL_EXECUTION_MODES = PASSIVE_EXECUTION_MODES | {"stay_silent"}
VISIBLE_COMMENT_KINDS = frozenset({"response", "discussion", "handoff", "final"})
WILL_MAX_ITERATIONS = 8
WILL_MAX_TOOL_CALLS = 12
WILL_CONVERGENCE_LIMIT = 3
WILL_MAX_REPEAT_PER_RESOURCE = 2
MAX_SESSION_ROUNDS = 6
MAX_PUBLIC_DISCUSSION_COMMENTS = 3
MAX_HANDOFFS = 3


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
    continue_session: bool = False
    done: bool = False
    next_identity: str | None = None
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
    discussion_count: int = 0
    handoff_count: int = 0
    responded_once: bool = False
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
        self.persona_registry = dict(persona_registry or {str(persona["identity"]): dict(persona)})
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

    def _activate_identity(self, identity: str) -> None:
        persona = self.persona_registry.get(identity)
        if persona is None:
            raise ValueError(f"Unknown persona identity for session handoff: {identity}")
        self.persona = dict(persona)
        self.plugin.set_identity(str(persona["identity"]), str(persona.get("display_name") or persona["identity"]))

    async def run(self, raw_event: Any) -> None:
        event = self.plugin.parse_event(raw_event)
        identity = str(self.persona["identity"])
        self._activate_identity(identity)
        history = await self.plugin.fetch_history(event)
        runtime_state = history.runtime_state.model_copy(deep=True)

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

            if event.is_patrol:
                runtime_state.next_patrol_after = _next_patrol_after_iso()
            session = SessionState(current_identity=identity, current_event=event)
            final_outcome = ExecutionOutcome(kind="no_action", reason="no_action")
            final_event = event
            final_target_issue_number: int | None = None
            final_dispatcher_identity = identity
            skip_reason = ""

            for _ in range(MAX_SESSION_ROUNDS):
                session.rounds += 1
                self._activate_identity(session.current_identity)
                active_event = session.current_event
                active_history = await self.plugin.fetch_history(active_event)
                decision = await self._decide(event=active_event, history=active_history, session=session)
                _log("will_decision: " + json.dumps(decision.model_dump(), ensure_ascii=False)[:LOG_TRUNCATE])

                should_reply, skip_reason = self._should_reply(
                    event=active_event,
                    decision=decision,
                    runtime_state=runtime_state,
                    ignore_fatigue=session.rounds > 1,
                )
                if not should_reply:
                    final_outcome = ExecutionOutcome(kind="no_action", reason=skip_reason, done=True)
                    final_event = active_event
                    final_target_issue_number = decision.action_decision.target_issue_number
                    break

                target_issue_number = decision.action_decision.target_issue_number
                if target_issue_number is not None and target_issue_number != active_event.issue_number:
                    active_event = await self.plugin.resolve_target_event(active_event, target_issue_number)
                    active_history = await self.plugin.fetch_history(active_event)
                    _log(
                        f"{'street-lurker' if event.is_patrol else 'passive'} target resolved to "
                        f"{'PR' if active_event.is_pull_request else 'Issue'} #{active_event.issue_number}"
                    )

                outcome = await self._reply(event=active_event, history=active_history, decision=decision, session=session)
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
                if decision.action_decision.comment_kind == "discussion" and outcome.visible_comment_posted:
                    session.discussion_count += 1
                if decision.action_decision.comment_kind == "handoff" and outcome.visible_comment_posted:
                    session.handoff_count += 1
                if outcome.visible_comment_posted:
                    session.responded_once = True

                if (
                    not active_event.is_patrol
                    and outcome.visible_comment_posted
                    and decision.action_decision.comment_kind == "final"
                    and decision.action_decision.comment_kind not in {"discussion", "handoff"}
                ):
                    outcome.continue_session = False
                    outcome.done = True

                if outcome.kind == "handoff_posted" and outcome.next_identity:
                    if session.handoff_count >= MAX_HANDOFFS:
                        final_outcome = ExecutionOutcome(
                            kind="final_posted",
                            reason="handoff_limit_reached",
                            done=True,
                            visible_comment_posted=outcome.visible_comment_posted,
                            mutated_tool_names=set(outcome.mutated_tool_names),
                        )
                        break
                    session.current_identity = outcome.next_identity
                    session.current_event = active_event
                    continue

                if outcome.continue_session and not outcome.done:
                    session.current_identity = outcome.next_identity or session.current_identity
                    session.current_event = active_event
                    continue

                break

            if final_outcome.acted and final_outcome.done:
                try:
                    await self._reflect(event=final_event, history=await self.plugin.fetch_history(final_event))
                except Exception as exc:  # pragma: no cover - defensive logging only
                    _log(f"WARN: reflection pass failed: {exc}")

            runtime_state.last_routing.event_id = event.event_id
            runtime_state.last_routing.bot_identity = session.current_identity
            runtime_state.last_routing.dispatcher_identity = final_dispatcher_identity
            runtime_state.last_routing.reason = final_outcome.reason
            runtime_state.last_routing.target_issue_number = (
                final_event.issue_number if final_outcome.acted else final_target_issue_number
            )
            runtime_state.last_routing.handoff_to = final_outcome.next_identity
            runtime_state.last_routing.handoff_reason = ""
            runtime_state.last_routing.discussion_count = session.discussion_count
            runtime_state.last_routing.handoff_count = session.handoff_count
            runtime_state.last_routing.routed_at = _utcnow_iso()
            await self.plugin.update_runtime_state(runtime_state)
            if final_outcome.kind == "no_action":
                if event.is_patrol and final_target_issue_number is None:
                    _log("今天没乐子")
                else:
                    _log(f"SKIP: {skip_reason or final_outcome.reason}")
        finally:
            _gh_endgroup()

    async def _decide(self, *, event: PluginEvent, history: Any, session: SessionState) -> WillDecision:
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
        self._log_available_tools("will", tools)
        seen_tools: set[str] = set()
        no_new_tool_count = 0
        total_tool_calls = 0
        json_repair_only = False
        resource_uses: dict[tuple[str, str], int] = {}
        try:
            for i in range(WILL_MAX_ITERATIONS):
                _log(f"--- will iteration {i + 1}/{WILL_MAX_ITERATIONS} ---")
                t_start = time.monotonic()
                active_tools: list[dict[str, Any]] = [] if json_repair_only else tools
                response = await self._create_completion_with_retry(messages=messages, tools=active_tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
                self._log_stage_response("will", assistant_message, elapsed_seconds=t_elapsed)
                if tool_calls:
                    if json_repair_only:
                        _log("WARN: tool calls are no longer allowed during JSON repair")
                        # Don't append the assistant message with tool_calls — the API
                        # requires every tool_call to have a corresponding tool message.
                        # Reject inline by turning each call into a user-nudge message.
                        self._append_rejected_tool_calls(messages, tool_calls, reason="JSON repair mode — no more tool calls allowed")
                        json_repair_only = True
                        continue
                    if total_tool_calls >= WILL_MAX_TOOL_CALLS:
                        _log(f"WARN: will tool-call budget exhausted at {total_tool_calls}, forcing decision")
                        self._append_rejected_tool_calls(messages, tool_calls, reason="tool-call budget exhausted")
                        self._append_will_budget_exhausted_nudge(messages)
                        json_repair_only = True
                        continue
                    self._log_tool_calls(tool_calls)
                    messages.append(self._assistant_message_payload(assistant_message, tool_calls=tool_calls))
                    new_tool_seen = False
                    executed_tc_ids: set[str] = set()
                    for tool_call in tool_calls:
                        if total_tool_calls >= WILL_MAX_TOOL_CALLS:
                            _log(f"WARN: reached will tool-call budget {WILL_MAX_TOOL_CALLS}, skipping remaining tool calls")
                            json_repair_only = True
                            break
                        tool_name, args_raw = self._tool_call_details(tool_call)
                        _log(f"  -> {tool_name}({args_raw[:LOG_TRUNCATE]})")
                        if tool_name not in seen_tools:
                            seen_tools.add(tool_name)
                            new_tool_seen = True
                        total_tool_calls += 1
                        tool_result = await self._execute_will_tool_call(
                            tool_call,
                            event=event,
                            resource_uses=resource_uses,
                        )
                        if isinstance(tool_result, _NoReplyResult):
                            break
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
                    if json_repair_only:
                        self._append_will_budget_exhausted_nudge(messages)
                    if new_tool_seen:
                        no_new_tool_count = 0
                    else:
                        no_new_tool_count += 1
                        if no_new_tool_count >= WILL_CONVERGENCE_LIMIT:
                            _log(f"WARN: {no_new_tool_count} iterations without new tools, forcing decision")
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "You have been looping without discovering new information. "
                                        "You MUST output your WillDecision JSON NOW. "
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
                    decision = WillDecision.model_validate(cleaned)
                except (ValidationError, JSONDecodeError) as exc:
                    _log(f"WARN: invalid will JSON: {_truncate_log(exc)}")
                    messages.append(self._assistant_message_payload(assistant_message))
                    detail = _hallucinated_tool_call_hint(decision_text)
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous response was invalid. {detail}"
                                "Return ONLY a bare JSON object matching the required schema "
                                f"— no markdown, no code blocks, no tool-call syntax. Validation error: {exc}"
                            ),
                        }
                    )
                    json_repair_only = True
                    continue
                try:
                    self._validate_decision(event=event, decision=decision, session=session)
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
            action_decision=ActionDecision(
                mode="stay_silent",
                will_reply=False,
                will_act=False,
                target_issue_number=None,
                comment_kind="final",
                done=True,
            ),
        )

    async def _reply(
        self,
        *,
        event: PluginEvent,
        history: Any,
        decision: WillDecision,
        session: SessionState,
    ) -> ExecutionOutcome:
        system_prompt = self._reply_prompt(history=history, event=event, decision=decision)
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
                        tool_result: str | _NoReplyResult = self._session_mutation_preflight(
                            session=session,
                            event=event,
                            tool_name=tool_name,
                            args_raw=args_raw,
                        )
                        if tool_result is None:
                            tool_result = await self._execute_tool_call(tool_call, event=event)
                        if tool_name == NO_REPLY_TOOL_NAME:
                            try:
                                reason = json.loads(args_raw).get("reason", "(no reason)")
                            except Exception:
                                reason = "(no reason)"
                            _log(f"  <- no_reply: {reason}")
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": getattr(tool_call, "id", ""),
                                    "content": f"no_reply executed: {reason}",
                                }
                            )
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
                                    done=decision.action_decision.done or not decision.action_decision.continue_session,
                                    continue_session=decision.action_decision.continue_session,
                                    next_identity=decision.action_decision.handoff_to,
                                )
                            return ExecutionOutcome(kind="no_action", reason="no_reply")
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
                            tool_name=tool_name,
                            decision=decision,
                        ):
                            _log("Terminal visible mutation completed; ending this passive session now")
                            return ExecutionOutcome(
                                kind="final_posted",
                                reason=tool_name,
                                mutated_tool_names=set(executed_tool_names),
                                continue_session=False,
                                done=True,
                                visible_comment_posted=True,
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
                                done=decision.action_decision.done or not decision.action_decision.continue_session,
                                continue_session=decision.action_decision.continue_session,
                                next_identity=decision.action_decision.handoff_to,
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
                        continue_session=False
                        if (not event.is_patrol and decision.action_decision.comment_kind == "final")
                        else decision.action_decision.continue_session,
                        done=True
                        if (not event.is_patrol and decision.action_decision.comment_kind == "final")
                        else (decision.action_decision.done or not decision.action_decision.continue_session),
                        next_identity=decision.action_decision.handoff_to,
                        visible_comment_posted=True,
                    )

                if executed_tool_names:
                    _log("reply stage finished without thread reply after mutating tools")
                    return ExecutionOutcome(
                        kind="acted_without_thread_reply",
                        reason=_reason_from_tools(executed_tool_names, default="acted_without_comment"),
                        mutated_tool_names=set(executed_tool_names),
                        continue_session=decision.action_decision.continue_session,
                        done=decision.action_decision.done or not decision.action_decision.continue_session,
                        next_identity=decision.action_decision.handoff_to,
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
            return ExecutionOutcome(kind="final_posted", reason="replied", done=True, visible_comment_posted=True)
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
        json_repair_only = False
        repair_attempts = 0
        try:
            for i in range(self.max_iterations):
                _log(f"--- reflection iteration {i + 1}/{self.max_iterations} ---")
                t_start = time.monotonic()
                active_tools: list[dict[str, Any]] = [] if json_repair_only else tools
                response = await self._create_completion_with_retry(messages=messages, tools=active_tools)
                t_elapsed = time.monotonic() - t_start
                assistant_message = response.choices[0].message
                tool_calls = list(getattr(assistant_message, "tool_calls", []) or [])
                self._log_stage_response("reflection", assistant_message, elapsed_seconds=t_elapsed)
                if tool_calls:
                    if json_repair_only:
                        _log("WARN: tool calls are no longer allowed during reflection JSON repair")
                        self._append_rejected_tool_calls(
                            messages,
                            tool_calls,
                            reason="Reflection JSON repair mode — no more tool calls allowed",
                            output_name="ReflectionDecision",
                            fallback_name="noop",
                        )
                        continue
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
                    cleaned = _extract_safe_json(reflection_text)
                    decision = ReflectionDecision.model_validate(cleaned)
                except (ValidationError, JSONDecodeError) as exc:
                    _log(f"WARN: invalid reflection JSON: {_truncate_log(exc)}")
                    repair_attempts += 1
                    if repair_attempts >= 2:
                        decision = ReflectionDecision(
                            action="noop",
                            summary="reflection JSON repair exhausted",
                        )
                        _log(f"reflection result: {decision.action} {decision.summary[:LOG_TRUNCATE]}")
                        return
                    json_repair_only = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous reflection output was invalid JSON. "
                                "On the next turn, output ONLY the raw ReflectionDecision JSON object. "
                                "Do not call tools. Do not wrap the JSON in markdown or explanation."
                            ),
                        }
                    )
                    continue
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
            if event.issue_number == 0 and skill.name in {"read_issue_memory", "read_thread_context"}:
                continue
            if not _is_trusted_mutation_author(event.author_association):
                if skill.mutates_state or skill.requires_trusted_author:
                    continue
            skills.append(skill)
        return skills

    @staticmethod
    def _append_will_budget_exhausted_nudge(messages: list[ChatMessage]) -> None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your will-stage tool-call budget is exhausted. "
                    "You MUST output the raw WillDecision JSON on the next turn. "
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

    async def _execute_will_tool_call(
        self,
        tool_call: Any,
        *,
        event: PluginEvent,
        resource_uses: dict[tuple[str, str], int],
    ) -> str | _NoReplyResult:
        tool_name, args_raw = self._tool_call_details(tool_call)
        resource_key = self._will_resource_key(tool_name, args_raw)
        if resource_key is not None:
            uses = resource_uses.get(resource_key, 0)
            if uses >= WILL_MAX_REPEAT_PER_RESOURCE:
                return (
                    f"Tool skipped: resource probe limit reached for {resource_key[0]} "
                    f"'{resource_key[1]}'. Output the WillDecision JSON instead of repeating this check."
                )
            resource_uses[resource_key] = uses + 1
        return await self._execute_tool_call(tool_call, event=event)

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
            '"motivation_score":0,"action_decision":{"mode":"stay_silent","will_reply":false,"will_act":false,"execution_identity":"self","comment_kind":"response","handoff_to":null,"handoff_reason":"","focus_summary":"","context_issue_numbers":[],"continue_session":false,"done":false,"target_issue_number":null}}'
            "\n规则："
            "\n1. 当前看到的上下文是故意片面的；如果信息不够，先使用只读工具继续了解。"
            "\n2. 先排除 coordination、mind issue、memory 这类 bot 内务；默认不要把它们当候选工作。"
            "\n  其中：read_thread_context 只读当前线程；live mind issue 是你自己的 open working-memory thread；"
            "带 `🧠 memory` 标签的 closed issues 是长期记忆库。不要把它们混为一谈。"
            "\n3. 对普通 Issue/PR 事件，优先解决当前线程的人类意图；如果用户指令明确指向其他 Issue/PR，跨 Issue 操作完全合法，不要犹豫。"
            "\n4. 若当前消息或线程里出现明确编号（如 #54），先用 read_thread_meta/read_issue_body 精确核实，再决定是否扩展到 search_repo_context 或代码搜索。"
            "\n5. 对普通 Issue/PR 事件，优先尝试 retrieve_memory；如果记忆不足，再用 search_repo_context，必要时再查代码。"
            "\n6. action_decision.mode 只能是：reply_brief、reply_with_plan、ask_clarifying_question、act_directly、stay_silent。"
            "\n7. comment_kind 只能是：response、discussion、handoff、final。discussion 用来公开技术分歧/补充，handoff 用来显式把麦克风交给另一个 bot，final 用来公开收口。"
            "\n8. 如果这是被动事件（非 patrol），你必须回应或做事，不能选择 stay_silent。"
            "\n  被动事件里，reply_brief 适合直接事实回答；reply_with_plan 适合解释现状和下一步；ask_clarifying_question 只问一个关键问题；act_directly 适合直接动手。"
            "\n9. 如果这是街溜子事件，stay_silent 合法，但只有在你确认没有新鲜动态、没有 stale thread、没有小型代码/测试/文档机会、也没有可收尾事项时才能用。"
            "\n  不要因为“最近 24h 没有新增 issue/PR”就直接开摆；你要把 patrol_brief 当作机会雷达，主动寻找可推进的小机会。"
            "\n10. target_issue_number 在街溜子事件里可以是 issue 或 PR 编号，也可以为 null；target_issue_number 为 null 不代表你不能直接行动。"
            "\n11. execution_identity='self' 表示当前 bot 自己执行这一轮；如果你要交给别人，填写 handoff_to，并把 comment_kind 设成 handoff 或 discussion。"
            "\n12. 公开技术讨论最多 2-3 轮；如果已经讨论过几轮，下一步要么收敛成 final，要么 handoff，要么提出唯一关键阻塞问题。"
            "\n13. 只有当你真的准备公开发言时，will_reply 才能为 true；只有当你真的准备直接执行动作时，will_act 才能为 true。"
            "\n14. 非 stay_silent 决策必须提供非空 focus_summary，用一句话说明这一轮唯一要完成的目标。"
            "\n15. context_issue_numbers 用来列出 reply 阶段必须先核实的 companion threads；它只提供上下文约束，不会自动改变 target。"
            "\n16. continue_session=true 表示这一轮之后 session 还要继续；done=true 表示当前事项已经收口。二者不能同时为 true。"
            "\n17. 如果雷达里出现 Potential overlapping threads，先核实这些线程之间的关系，再决定是保留、关闭、交叉引用，还是忽略。"
            "\n18. 不要输出 Markdown，不要解释，不要包裹代码块。"
            "\n19. motivation_score 评分锚定（0-100 整数）："
            "\n  0-29: 无趣/无关/已经答复过，不应说话"
            "\n  30-59: 常规跟进，有轻微价值但不必抢麦"
            "\n  60-79: 发现了值得讨论的技术问题或可改进点"
            "\n  80-100: 发现了重大架构漏洞/突破口/高价值行动机会，必须抢麦"
            "\n  若 internal_emotion 表达兴奋/激动/惊喜等强烈情绪，motivation_score 必须 ≥ 80。"
            "\n  若 internal_emotion 表达无聊/疲惫/无感，motivation_score 必须 ≤ 29。"
            "\n  如果当前事件是人类直接明确的指令或回复，且意图清晰，motivation_score 必须强制 ≥ 80。"
            "\n  严禁以'不符合人设喜好'为由给人类指令打低分怠工。"
            "\n  情绪与分数必须自洽，不匹配会被拒绝重新来过。"
        )

    def _reply_prompt(self, *, history: Any, event: PluginEvent, decision: WillDecision) -> str:
        mode = decision.action_decision.mode
        comment_kind = decision.action_decision.comment_kind
        prompt = (
            self.persona["system_prompt"]
            + self._mind_context(history)
            + "\n\n第二阶段规则："
            "\n1. 对被动事件，优先解决当前线程的人类请求；有人类触发时，你必须给出反馈或完成真实动作，不能装死。"
            "\n2. 如果证据表明人类要求的 PR/修复其实早已完成，且当前 tracker 明显 stale，先简短解释现状，再 close_issue 当前 stale tracker。"
            "\n3. 不要为了已经完成的工作再制造重复 PR。"
            "\n4. 判断 PR 是否 merged，优先 read_thread_meta，不要先靠模糊搜索猜。"
        )
        if decision.action_decision.focus_summary:
            prompt += f"\n5. 本轮唯一焦点：{decision.action_decision.focus_summary}"
        if decision.action_decision.context_issue_numbers:
            refs = ", ".join(f"#{issue_number}" for issue_number in decision.action_decision.context_issue_numbers)
            prompt += f"\n6. 在执行前，先核实这些 companion threads：{refs}。不要跳过。"
        if mode == "reply_brief":
            prompt += "\n7. 当前 mode=reply_brief：直接回答当前问题，控制在 1-3 句，不要复述长篇调查过程。"
        elif mode == "reply_with_plan":
            prompt += "\n7. 当前 mode=reply_with_plan：简洁说明现状，再给出最小必要的下一步建议。"
        elif mode == "ask_clarifying_question":
            prompt += "\n7. 当前 mode=ask_clarifying_question：只问一个最关键的阻塞问题，不要顺手长篇分析。"
        elif mode == "act_directly":
            prompt += "\n7. 当前 mode=act_directly：以完成动作和收尾为优先；若需要公开说明，保持简短。"
        elif event.is_patrol:
            prompt += "\n7. 当前 mode=stay_silent：只有在你已经确认没有值得推进的机会时才允许结束。"
        if comment_kind == "discussion":
            prompt += "\n8. 当前 comment_kind=discussion：给出一条短、聚焦、工程化的公开技术评论。必须回应前一个 bot 的观点，并推动收敛，不要复述现状。"
        elif comment_kind == "handoff":
            prompt += "\n8. 当前 comment_kind=handoff：写一条显式交接评论，说明已完成到哪里、为什么要交给下一个 bot、下一步该做什么。"
        elif comment_kind == "final":
            prompt += "\n8. 当前 comment_kind=final：这是一条收口评论，必须清楚说明最终结论、最终动作或当前唯一阻塞点。"
        else:
            prompt += "\n8. 当前 comment_kind=response：这是对当前线程的直接公开回应。"
        prompt += "\n9. 不允许偏离本轮唯一焦点去做无关评论；如果发现新话题，只有在它直接影响当前焦点时才能提及。"
        prompt += (
            "\n\n【绝对最高优先级任务 (MISSION OVERRIDE)】\n"
            f"前序决策摘要：{decision.context_analysis}\n"
            f"本轮唯一目标：{decision.action_decision.focus_summary}\n"
            f"执行模式：{decision.action_decision.mode}\n"
            "你在前序思考中做出的行动决断是本次行动的唯一目的。\n"
            "严禁沉迷于你的角色设定！\n"
            "你必须优先调用具体工具彻底完成该决断。\n"
            "在工具物理执行完毕前，绝不允许结束思考循环！"
        )
        return prompt

    def _decision_user_prompt(self, *, event: PluginEvent, history: Any, session: SessionState | None = None) -> str:
        prompt = event.message
        mentioned_issue_refs = _mentioned_issue_refs(event.message)
        if not event.is_patrol and mentioned_issue_refs:
            refs = ", ".join(f"#{issue_number}" for issue_number in mentioned_issue_refs)
            prompt += (
                f"\n\n当前消息显式提到了这些线程：{refs}。"
                "请先用 read_thread_meta 或 read_issue_body 精确核实这些编号的真实状态，"
                "再决定是否需要扩展到 repo-wide search。"
            )
        if event.is_patrol and history.patrol_brief:
            prompt += f"\n\n街溜子模式机会雷达：\n{history.patrol_brief}"
        if session is not None:
            prompt += (
                f"\n\n当前公开协作 session 状态：active_bot={session.current_identity} "
                f"discussion_count={session.discussion_count} handoff_count={session.handoff_count} "
                f"responded_once={str(session.responded_once).lower()}。"
            )
        return prompt

    def _reflection_prompt(self, *, history: Any) -> str:
        return (
            self.persona["system_prompt"]
            + self._mind_context(history)
            + "\n\n你现在处于任务结束后的反思阶段。"
            "\n你的目标是判断这次互动是否值得写入、修订或归档长期记忆。"
            "\n可用工具只包含长期记忆 CRUD 和长期记忆检索。"
            "\n规则："
            "\n1. 只有长期有效、未来大概率还会有价值的信息才值得记忆。"
            "\n2. 当前任务上下文只来自你已经看到的 history.messages；不要再把当前 thread 当 memory 去读。"
            "\n3. 如果要改记忆，优先先读取或检索已有记忆，再决定 commit_memory / refine_memory / archive_memory。"
            "\n  live mind issue 不是长期记忆库；带 `🧠 memory` 标签的 closed issues 才是长期记忆库。"
            "\n4. 如果没有值得沉淀的长期知识，输出 {\"action\":\"noop\",\"summary\":\"...\"}。"
            "\n5. 如果你调用了记忆工具，最后仍然只输出一个 JSON 对象，action 只能是 noop、commit_memory、refine_memory 或 archive_memory。"
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
            f"## Your Live Working-Memory Thread (#{history.mind_issue_number})\n"
            f"This is your current bot working-memory thread, not the `🧠 memory` long-term memory DB. Read it before acting.\n"
            f"Use update_issue with issue_number={history.mind_issue_number} when you need to "
            f"update your live state. Use memory CRUD tools for long-term memory.\n\n{history.mind_body}\n---\n"
        )

    def _validate_decision(self, *, event: PluginEvent, decision: WillDecision, session: SessionState) -> None:
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
        if decision.action_decision.done and decision.action_decision.continue_session:
            raise ValueError("done and continue_session cannot both be true.")
        if decision.action_decision.comment_kind == "discussion":
            if session.discussion_count >= MAX_PUBLIC_DISCUSSION_COMMENTS:
                raise ValueError("discussion comment limit reached; you must conclude or hand off.")
            if decision.action_decision.done:
                raise ValueError("discussion comments must continue the session instead of finishing it immediately.")
        if decision.action_decision.comment_kind == "final" and decision.action_decision.continue_session:
            raise ValueError("final comments must conclude the current session.")
        if decision.action_decision.comment_kind == "handoff":
            if not decision.action_decision.handoff_to:
                raise ValueError("handoff comments must specify handoff_to.")
            if decision.action_decision.handoff_to == session.current_identity:
                raise ValueError("handoff_to must point to a different bot identity.")
            if session.handoff_count >= MAX_HANDOFFS:
                raise ValueError("handoff limit reached; you must conclude in the current bot.")
        elif decision.action_decision.handoff_to:
            raise ValueError("handoff_to is only valid when comment_kind is 'handoff'.")
        if decision.action_decision.execution_identity not in {"self", *self.persona_registry.keys()}:
            raise ValueError(f"Unknown execution_identity: {decision.action_decision.execution_identity!r}")
        if decision.action_decision.handoff_to and decision.action_decision.handoff_to not in self.persona_registry:
            raise ValueError(f"Unknown handoff_to identity: {decision.action_decision.handoff_to!r}")
        if event.is_patrol and mode == "stay_silent" and not decision.action_decision.done:
            decision.action_decision.done = True

    def _should_reply(
        self,
        *,
        event: PluginEvent,
        decision: WillDecision,
        runtime_state: RepoRuntimeState,
        ignore_fatigue: bool = False,
    ) -> tuple[bool, str]:
        if decision.action_decision.mode == "stay_silent":
            return False, "bot chose silence"
        if not event.is_patrol:
            return True, "passive event requires response"
        if not (decision.action_decision.will_reply or decision.action_decision.will_act):
            return False, "street-lurker found no concrete action"
        if decision.motivation_score < self.motivation_threshold:
            return False, f"street-lurker motivation {decision.motivation_score} below threshold {self.motivation_threshold}"

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

    def _comment_reason(self, *, decision: WillDecision, executed_tool_names: set[str]) -> str:
        if executed_tool_names:
            return _reason_from_tools(executed_tool_names, default="acted_without_comment")
        if decision.action_decision.comment_kind == "discussion":
            return "discussion_posted"
        if decision.action_decision.comment_kind == "handoff":
            return "handoff_posted"
        if decision.action_decision.comment_kind == "final":
            return "finalized"
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
    def _is_terminal_visible_mutation(
        *,
        event: PluginEvent,
        tool_name: str,
        decision: WillDecision,
    ) -> bool:
        if event.is_patrol:
            return (
                tool_name == "create_pr_review"
                and decision.action_decision.comment_kind not in {"discussion", "handoff"}
                and not decision.action_decision.continue_session
            )
        if decision.action_decision.comment_kind in {"discussion", "handoff"}:
            return False
        if decision.action_decision.continue_session:
            return False
        return tool_name in {"close_issue", "merge_pull_request", "create_pr_review", "reopen_issue"}

    def _available_reflection_skills(self, event: PluginEvent) -> Sequence[BaseSkill]:
        skills: list[BaseSkill] = []
        for skill in self.skills:
            if skill.name not in MEMORY_REFLECTION_TOOL_NAMES:
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

    @staticmethod
    def _append_rejected_tool_calls(
        messages: list[ChatMessage],
        tool_calls: list[Any],
        *,
        reason: str,
        output_name: str = "WillDecision",
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

    def _will_resource_key(self, tool_name: str, args_raw: str) -> tuple[str, str] | None:
        try:
            args = self._parse_tool_arguments(args_raw)
        except JSONDecodeError:
            return None
        if tool_name in {"read_issue_body", "read_thread_comments", "read_thread_meta"}:
            return (tool_name, str(args.get("issue_number", 0) or 0))
        if tool_name in {"read_file", "list_files"}:
            return (tool_name, str(args.get("path", "")).strip())
        if tool_name in {"search_issues", "search_repo_context", "retrieve_memory", "search_code"}:
            return (tool_name, _normalize_query_key(str(args.get("query", "")).strip()))
        return None


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


def _mentioned_issue_refs(text: str) -> list[int]:
    seen: list[int] = []
    for match in re.finditer(r"#(\d+)", text):
        issue_number = int(match.group(1))
        if issue_number not in seen:
            seen.append(issue_number)
    return seen


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
