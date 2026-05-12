from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from core import prompts
from core.plugins import (
    BasePlugin,
    BotFatigueState,
    HistorySnapshot,
    PluginEvent,
    RepoRuntimeState,
    ScoutDecision,
)
from core.ryo_agent import (
    RyoAgent,
    _extract_result_summary,
    _extract_safe_json,
    _looks_like_truncated_json,
)
from core.skills import BaseSkill, clear_skill_context, get_skill_context


@dataclass
class FakeFunction:
    name: str
    arguments: str


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction
    type: str = "function"


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)
    reasoning_content: str | None = None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeResponse:
    choices: list[FakeChoice]


class FakeCompletions:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake responses left for create().")
        return self._responses.pop(0)


class FakePlugin(BasePlugin):
    def __init__(
        self,
        *,
        event: PluginEvent | None = None,
        history_by_issue: dict[int, HistorySnapshot] | None = None,
    ) -> None:
        self._event = event or PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
        self._history_by_issue = history_by_issue or {
            self._event.issue_number: HistorySnapshot(
                messages=[],
                subconscious={},
                runtime_state=RepoRuntimeState(),
            )
        }
        self.sent_replies: list[tuple[PluginEvent, str, dict[str, Any] | None]] = []
        self.updated_runtime_states: list[RepoRuntimeState] = []
        self.resolved_targets: list[int] = []
        self.identity_history: list[tuple[str, str]] = []

    def parse_event(self, raw_payload: Any) -> PluginEvent:
        return self._event

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        return self._history_by_issue.get(
            event.issue_number,
            HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState()),
        )

    async def resolve_target_event(self, event: PluginEvent, issue_number: int) -> PluginEvent:
        self.resolved_targets.append(issue_number)
        return PluginEvent(
            event_id=f"{event.event_id}:target:{issue_number}",
            message=f"target #{issue_number}",
            author="system",
            author_association="OWNER",
            issue_id=str(issue_number),
            issue_number=issue_number,
            comment_id=0,
            owner=event.owner,
            repo=event.repo,
            is_pull_request=(issue_number == 77),
            is_patrol=True,
            head_ref="feat/test-pr" if issue_number == 77 else "",
        )

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        self.sent_replies.append((event, content, subconscious))

    def set_identity(self, identity: str, display_name: str) -> None:
        self.identity_history.append((identity, display_name))

    async def update_runtime_state(self, state: RepoRuntimeState) -> RepoRuntimeState:
        self.updated_runtime_states.append(state)
        return state


class EchoArgs(BaseModel):
    text: str


class PathArgs(BaseModel):
    path: str = ""


class SymbolArgs(BaseModel):
    symbol_name: str


class IssueNumberArgs(BaseModel):
    issue_number: int


class ThreadNumberArgs(BaseModel):
    thread_number: int


class EchoSkill(BaseSkill):
    name = "echo"
    description = "Repeat the provided text."
    args_model = EchoArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"echo:{args.text}"


class MutatingSkill(EchoSkill):
    name = "mutate"
    description = "Mutate external state."
    mutates_state = True


class CreatePullRequestTestSkill(MutatingSkill):
    name = "create_pull_request"


class MergePullRequestTestSkill(MutatingSkill):
    name = "merge_pull_request"


class AddLabelsTestSkill(MutatingSkill):
    name = "add_labels"


class CloseIssueTestSkill(MutatingSkill):
    name = "close_issue"


class DispatchWorkflowTestSkill(MutatingSkill):
    name = "dispatch_workflow"


class CreatePRReviewTestSkill(MutatingSkill):
    name = "create_pr_review"
    terminal_mutation = True


class TrustedReadSkill(EchoSkill):
    name = "trusted_read"
    description = "Read broader trusted-only context."
    requires_trusted_author = True


class ContextAwareSkill(BaseSkill):
    name = "read_context"
    description = "Inspect runtime context."
    args_model = BaseModel

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        context = get_skill_context()
        return {
            "owner": context["owner"],
            "repo": context["repo"],
            "issue_number": context["issue_number"],
            "is_patrol": context["is_patrol"],
        }


class StoreMemoryTestSkill(EchoSkill):
    name = "store_memory"
    description = "Store long-term memory."
    mutates_state = True


class RetrieveMemoryTestSkill(EchoSkill):
    name = "retrieve_memory"
    description = "Read long-term memory."


class SearchRepoContextTestSkill(EchoSkill):
    name = "search_repo_context"
    description = "Search repo-wide issues and PRs."


class SearchRepoMemoryTestSkill(EchoSkill):
    name = "retrieve_memory"
    description = "Search repository memory."


class ReadThreadMetaTestSkill(EchoSkill):
    name = "read_thread_meta"
    description = "Read precise issue or PR metadata."


class ReadThreadMetaFakeSkill(BaseSkill):
    """A read_thread_meta that returns realistic Thread #N output and accepts issue_number args."""
    name = "read_thread_meta"
    description = "Read precise issue or PR metadata."
    args_model = IssueNumberArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        n = args.issue_number
        return (
            f"Thread #{n}: Test Issue #{n} title\n"
            f"Type: Issue\nState: open\nAuthor: bot\n"
            f"Labels: test\n"
            f"Created: 2026-05-10T00:00:00Z\n"
            f"Updated: 2026-05-10T00:00:00Z\n"
            f"Closed: N/A\nMerged: False\n"
        )


class UpdateIssueFakeSkill(BaseSkill):
    """A mutating update_issue that accepts issue_number and body args."""
    name = "update_issue"
    description = "Update an existing issue body."
    args_model = BaseModel
    mutates_state = True
    calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"updated:#{kwargs.get('issue_number', 0)}"


class AddLabelsFakeSkill(BaseSkill):
    """A mutating add_labels that accepts issue_number and labels args."""
    name = "add_labels"
    description = "Add labels to an issue."
    args_model = BaseModel
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        return f"labeled:#{kwargs.get('issue_number', 0)}"


class ReadIssueMemoryTestSkill(EchoSkill):
    name = "read_issue_memory"
    description = "Read the current issue memory."


class ReadThreadContextTestSkill(EchoSkill):
    name = "read_thread_context"
    description = "Read the current thread context."


class ListFilesTestSkill(BaseSkill):
    name = "list_files"
    description = "List a directory."
    args_model = PathArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"listed:{args.path}"


class SearchSymbolTestSkill(BaseSkill):
    name = "search_symbol"
    description = "Locate a symbol."
    args_model = SymbolArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"symbol:{args.symbol_name}"


class RecordingCloseIssueSkill(BaseSkill):
    name = "close_issue"
    description = "Close an issue and record the call order."
    args_model = IssueNumberArgs
    mutates_state = True

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        self.calls.append(args.issue_number)
        return f"closed:{args.issue_number}"


class RecordingCommentOnThreadSkill(BaseSkill):
    name = "comment_on_thread"
    description = "Comment on a thread and record the call order."
    args_model = ThreadNumberArgs
    mutates_state = True

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        self.calls.append(args.thread_number)
        return f"commented:{args.thread_number}"


class ProbeEchoSkill(EchoSkill):
    name = "probe_echo"
    description = "Probe for convergence behavior."


def build_response(message: FakeMessage) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=message)])


def build_agent(
    *,
    plugin: FakePlugin,
    responses: list[FakeResponse],
    skills: list[BaseSkill] | None = None,
    max_tokens: int = 4096,
) -> tuple[RyoAgent, FakeCompletions]:
    fake_completions = FakeCompletions(responses)
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    persona_registry = {
        identity: {
            "identity": identity,
            "display_name": identity.title(),
            "model": "gpt-4.1-mini",
            "system_prompt": f"You are {identity}.",
        }
        for identity in ["architect", "reviewer", "pm", "explorer", "coder"]
    }
    agent = RyoAgent(
        persona={"identity": "architect", "display_name": "Architect", "model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        persona_registry=persona_registry,
        skills=skills or [EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        max_tokens=max_tokens,
        fatigue_min_seconds=480,
        fatigue_max_seconds=480,
    )
    return agent, fake_completions


@pytest.mark.asyncio
async def test_run_skips_when_decision_says_no_reply() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")
        },
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                            {
                                "context_analysis": "nothing new",
                                "internal_emotion": "lazy",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                            }
                        )
                    )
            )
        ],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == []
    assert plugin.updated_runtime_states[-1].last_routing.reason == "bot chose silence"


@pytest.mark.asyncio
async def test_run_replies_after_passing_scout_decision() -> None:
    plugin = FakePlugin(
        history_by_issue={
            12: HistorySnapshot(
                messages=[{"role": "assistant", "content": "Previous reply"}],
                subconscious={"mode": "reflective"},
                runtime_state=RepoRuntimeState(),
            )
        }
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "worth answering",
                            "internal_emotion": "awake",
                            "biological_clock_impact": "daytime",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Post the final answer on the current thread."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Final answer")),
        ],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Final answer", {"mode": "reflective"})
    ]
    assert fake_completions.calls[1]["messages"][-1]["content"] == "hello"
    fatigue = plugin.updated_runtime_states[-1].bot_fatigue["architect"]
    assert fatigue.last_spoke_at is not None


@pytest.mark.asyncio
async def test_passive_event_replies_even_when_motivation_is_below_threshold() -> None:
    plugin = FakePlugin()
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "simple factual answer",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "reply_brief", "will_reply": True, "will_act": False, "target_issue_number": None, "focus_summary": "Answer the factual question briefly."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Short answer anyway")),
        ],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies[0][1] == "Short answer anyway"


@pytest.mark.asyncio
async def test_passive_final_comment_stops_session_without_second_bot() -> None:
    plugin = FakePlugin()
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "cleanup complete",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "reply_with_plan",
                                "will_reply": True,
                                "will_act": False,
                                "execution_identity": "self",
                                "comment_kind": "final",

                                "focus_summary": "Post the final cleanup summary and end the session.",
                                "context_issue_numbers": [],
                                "target_issue_number": None,
                            },
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="整理完毕，当前请求已收口。")),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
            build_response(FakeMessage(content="this extra response should never be consumed")),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill()],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [(plugin.parse_event(None), "整理完毕，当前请求已收口。", {})]
    assert len(fake_completions.calls) == 2


@pytest.mark.asyncio
async def test_pr_review_submission_ends_session_without_extra_signoff() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "review the active PR",
                            "internal_emotion": "focused",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                            "mode": "act_directly",
                            "will_reply": True,
                            "will_act": True,
                            "execution_identity": "self",
                            "comment_kind": "response",
                            "focus_summary": "Submit the PR review on the active target thread.",
                            "context_issue_numbers": [],
                            "target_issue_number": 77,
                            },
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-review",
                            function=FakeFunction(name="create_pr_review", arguments='{"text":"review"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
        ],
        skills=[CreatePRReviewTestSkill()],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == []
    assert plugin.resolved_targets == [77]
    assert len(fake_completions.calls) == 2
    assert plugin.updated_runtime_states[-1].last_routing.reason == "create_pr_review"


@pytest.mark.asyncio
async def test_run_patrol_resolves_target_before_replying() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="patrol",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief"),
            77: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState()),
        },
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "pr needs attention",
                            "internal_emotion": "curious",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": 77, "focus_summary": "Reply on the resolved patrol target."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Patrol reply")),
        ],
    )

    await agent.run(raw_event={})

    assert plugin.resolved_targets == [77]
    assert plugin.sent_replies[0][0].issue_number == 77


@pytest.mark.asyncio
async def test_street_lurker_repo_scan_can_act_without_thread_reply() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")
        },
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "clear fix",
                            "internal_emotion": "itchy",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Act directly from repo-scan without a thread reply."},
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-pr",
                            function=FakeFunction(
                                name="create_pull_request",
                                arguments='{"text":"ship it"}',
                            ),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="")),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
        ],
        skills=[CreatePullRequestTestSkill()],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == []
    assert plugin.updated_runtime_states[-1].last_routing.reason == "created_pr"


@pytest.mark.asyncio
async def test_street_lurker_reply_stage_exposes_full_mutation_tools_for_trusted_repo_scan() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")
        },
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "let's go",
                            "internal_emotion": "ready",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Continue the current follow-up despite fatigue."},
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-no-reply",
                            function=FakeFunction(name="no_reply", arguments='{"reason":"pass"}'),
                        )
                    ]
                )
            ),
        ],
        skills=[
            EchoSkill(),
            CreatePullRequestTestSkill(),
            MergePullRequestTestSkill(),
            AddLabelsTestSkill(),
            CloseIssueTestSkill(),
            DispatchWorkflowTestSkill(),
        ],
    )

    await agent.run(raw_event={})

    tool_names = [tool["function"]["name"] for tool in fake_completions.calls[1]["tools"]]
    assert "create_pull_request" in tool_names
    assert "merge_pull_request" in tool_names
    assert "add_labels" in tool_names
    assert "close_issue" in tool_names
    assert "dispatch_workflow" in tool_names


@pytest.mark.asyncio
async def test_reply_executes_all_terminal_mutations_in_same_batch_before_stopping() -> None:
    close_skill = RecordingCloseIssueSkill()
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "close duplicates",
                            "internal_emotion": "focused",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "act_directly",
                                "will_reply": True,
                                "will_act": True,
                                "execution_identity": "self",
                                "comment_kind": "final",

                                "focus_summary": "Close all duplicate issues in one batch.",
                                "context_issue_numbers": [],
                                "target_issue_number": None,
                            },
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(id="call-1", function=FakeFunction(name="close_issue", arguments='{"issue_number":81}')),
                        FakeToolCall(id="call-2", function=FakeFunction(name="close_issue", arguments='{"issue_number":85}')),
                        FakeToolCall(id="call-3", function=FakeFunction(name="close_issue", arguments='{"issue_number":82}')),
                    ]
                )
            ),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
        ],
        skills=[close_skill],
    )

    await agent.run(raw_event={})

    assert close_skill.calls == [81, 85, 82]
    assert plugin.updated_runtime_states[-1].last_routing.reason == "finalized"


@pytest.mark.asyncio
async def test_reply_executes_mixed_batch_in_order_before_terminal_stop() -> None:
    comment_skill = RecordingCommentOnThreadSkill()
    close_skill = RecordingCloseIssueSkill()
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "comment then close",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "act_directly",
                                "will_reply": True,
                                "will_act": True,
                                "execution_identity": "self",
                                "comment_kind": "final",

                                "focus_summary": "Post one note and close duplicate issues.",
                                "context_issue_numbers": [],
                                "target_issue_number": None,
                            },
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(id="call-comment", function=FakeFunction(name="comment_on_thread", arguments='{"thread_number":12}')),
                        FakeToolCall(id="call-close-1", function=FakeFunction(name="close_issue", arguments='{"issue_number":81}')),
                        FakeToolCall(id="call-close-2", function=FakeFunction(name="close_issue", arguments='{"issue_number":82}')),
                    ]
                )
            ),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
        ],
        skills=[comment_skill, close_skill],
    )

    await agent.run(raw_event={})

    assert comment_skill.calls == [12]
    assert close_skill.calls == [81, 82]
    assert plugin.updated_runtime_states[-1].last_routing.reason == "finalized"


@pytest.mark.asyncio
async def test_passive_events_force_visible_report_after_mutations() -> None:
    close_skill = RecordingCloseIssueSkill()
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "close and report",
                            "internal_emotion": "firm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "act_directly",
                                "will_reply": True,
                                "will_act": True,
                                "execution_identity": "self",
                                "comment_kind": "response",
                                "focus_summary": "Close the issue and report to the thread.",
                                "context_issue_numbers": [],
                                "target_issue_number": None,
                            },
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(id="call-close", function=FakeFunction(name="close_issue", arguments='{"issue_number":81}')),
                        FakeToolCall(id="call-no-reply", function=FakeFunction(name="no_reply", arguments='{"reason":"done"}')),
                    ]
                )
            ),
            build_response(FakeMessage(content="Closed as duplicate.")),
        ],
        skills=[close_skill],
    )

    await agent.run(raw_event={})

    assert close_skill.calls == [81]
    assert plugin.updated_runtime_states[-1].last_routing.reason == "closed_issue"
    assert len(plugin.sent_replies) == 1
    assert plugin.sent_replies[0][1] == "Closed as duplicate."


@pytest.mark.asyncio
async def test_passive_events_ignore_fatigue_and_still_reply() -> None:
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    plugin = FakePlugin(
        history_by_issue={
            12: HistorySnapshot(
                messages=[],
                subconscious={},
                runtime_state=RepoRuntimeState(
                    bot_fatigue={"architect": BotFatigueState(last_spoke_at=None, next_available_at=future)}
                ),
            )
        }
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
                build_response(
                    FakeMessage(
                        content=json.dumps(
                            {
                                "context_analysis": "would reply",
                                "internal_emotion": "ready",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "reply_brief", "will_reply": True, "will_act": False, "target_issue_number": None, "focus_summary": "Acknowledge the runtime context result briefly."},
                            }
                        )
                    )
                ),
                build_response(FakeMessage(content="Still replying despite fatigue")),
            ],
        )

    await agent.run(raw_event={})

    assert plugin.sent_replies[0][1] == "Still replying despite fatigue"


@pytest.mark.asyncio
async def test_stage_one_hides_mutating_tools_from_untrusted_authors() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="Need help",
            author="octocat",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
            author_association="CONTRIBUTOR",
            is_patrol=True,
        )
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "no rights",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            )
        ],
        skills=[EchoSkill(), MutatingSkill(), TrustedReadSkill()],
    )

    await agent.run(raw_event={})

    tool_names = [tool["function"]["name"] for tool in fake_completions.calls[0]["tools"]]
    assert tool_names == ["echo"]


@pytest.mark.asyncio
async def test_invalid_decision_json_retries_until_valid() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")
        },
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(content="not json")),
            build_response(
                FakeMessage(
                    content=json.dumps(
                            {
                                "context_analysis": "valid now",
                                "internal_emotion": "settled",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                            }
                        )
                    )
            ),
        ],
    )

    await agent.run(raw_event={})

    assert len(fake_completions.calls) == 2
    assert plugin.sent_replies == []


@pytest.mark.asyncio
async def test_invalid_decision_json_enters_json_repair_mode_without_more_tools() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={
            0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")
        },
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(content="not json")),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"text":"probe"}'),
                        )
                    ]
                )
            ),
            build_response(
                FakeMessage(
                    content=json.dumps(
                            {
                                "context_analysis": "fixed",
                                "internal_emotion": "steady",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                            }
                        )
                    )
            ),
        ],
    )

    await agent.run(raw_event={})

    assert len(fake_completions.calls) == 3
    assert not any(message.get("role") == "tool" for message in fake_completions.calls[2]["messages"])
    assert "no more tool calls" in fake_completions.calls[2]["messages"][-1]["content"].lower()


@pytest.mark.asyncio
async def test_run_sets_runtime_context_for_skills() -> None:
    clear_skill_context()
    plugin = FakePlugin()
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-ctx",
                            function=FakeFunction(name="read_context", arguments="{}"),
                        )
                    ]
                )
            ),
                build_response(
                    FakeMessage(
                        content=json.dumps(
                            {
                                "context_analysis": "done",
                                "internal_emotion": "done",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "reply_brief", "will_reply": True, "will_act": False, "focus_summary": "Test the context.", "context_issue_numbers": [], "target_issue_number": None},
                            }
                        )
                    )
                ),
                build_response(FakeMessage(content="Context acknowledged")),
            ],
            skills=[ContextAwareSkill()],
        )

    await agent.run(raw_event={})

    tool_result = fake_completions.calls[1]["messages"][-1]["content"]
    assert '"owner": "acme"' in tool_result
    assert '"is_patrol": false' in tool_result.lower()


@pytest.mark.asyncio
async def test_decide_replays_reasoning_content_after_tool_calls() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"text":"probe"}'),
                        )
                    ],
                    reasoning_content="I should inspect one clue first.",
                )
            ),
                build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "after tool",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
    )

    await agent.run(raw_event={})

    assistant_msg = fake_completions.calls[1]["messages"][-2]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["reasoning_content"] == "I should inspect one clue first."
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "echo"


@pytest.mark.asyncio
async def test_scout_stage_logs_reasoning_and_tool_details(capsys: pytest.CaptureFixture[str]) -> None:
    patrol_plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, _ = build_agent(
        plugin=patrol_plugin,
        responses=[
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"text":"probe"}'),
                        )
                    ],
                    reasoning_content="Need one more clue.",
                )
            ),
            build_response(
                FakeMessage(
                    content=json.dumps(
                            {
                                "context_analysis": "done",
                                "internal_emotion": "steady",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                            }
                        )
                    )
            ),
        ],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "scout available tools: echo" in stderr
    assert "scout reasoning" in stderr
    assert "tool calls: [echo]" in stderr
    assert '-> echo({"text":"probe"})' in stderr
    assert "<- result: echo:probe" in stderr


@pytest.mark.asyncio
async def test_decision_prompt_prefers_memory_before_repo_context() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                            {
                                "context_analysis": "checked memory first",
                                "internal_emotion": "calm",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                            }
                        )
                    )
            )
        ],
        skills=[RetrieveMemoryTestSkill(), SearchRepoContextTestSkill()],
    )

    await agent.run(raw_event={})

    prompt = fake_completions.calls[0]["messages"][0]["content"]
    tool_names = [tool["function"]["name"] for tool in fake_completions.calls[0]["tools"]]
    assert "retrieve_memory" in prompt
    assert "search_repo_context" in prompt
    assert tool_names == ["retrieve_memory", "search_repo_context"]


@pytest.mark.asyncio
async def test_repo_scan_hides_read_issue_memory_from_scout_tools() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "nothing to do",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            )
        ],
        skills=[ReadIssueMemoryTestSkill(), ReadThreadContextTestSkill(), RetrieveMemoryTestSkill(), SearchRepoContextTestSkill()],
    )

    await agent.run(raw_event={})

    tool_names = [tool["function"]["name"] for tool in fake_completions.calls[0]["tools"]]
    assert tool_names == ["retrieve_memory", "search_repo_context"]


@pytest.mark.asyncio
async def test_decision_prompt_mentions_read_thread_meta_and_current_human_intent() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-56",
            message="[Comment on Issue #56]\n\n来个人提一个 PR，先看看 #54",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=56,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
                build_response(
                    FakeMessage(
                        content=json.dumps(
                            {
                                "context_analysis": "checked the current ask first",
                                "internal_emotion": "calm",
                                "biological_clock_impact": "neutral",
                                "action_decision": {"mode": "reply_with_plan", "will_reply": True, "will_act": False, "target_issue_number": None, "focus_summary": "Explain the current status on the existing thread references."},
                            }
                        )
                    )
                ),
                build_response(FakeMessage(content="已经看过 #54 了，当前线程先解释现状。")),
            ],
            skills=[RetrieveMemoryTestSkill(), SearchRepoContextTestSkill(), ReadThreadMetaTestSkill()],
        )

    await agent.run(raw_event={})

    prompt = fake_completions.calls[0]["messages"][0]["content"]
    user_prompt = fake_completions.calls[0]["messages"][-1]["content"]
    tool_names = [tool["function"]["name"] for tool in fake_completions.calls[0]["tools"]]
    assert "先解决当前线程的人类意图" in prompt
    assert "read_thread_meta" in prompt
    assert "排除 coordination、mind issue、memory" in prompt
    assert "当前消息显式提到了这些线程：#56, #54" in user_prompt
    assert tool_names == ["retrieve_memory", "search_repo_context", "read_thread_meta"]


@pytest.mark.asyncio
async def test_scout_stage_uses_independent_iteration_budget() -> None:
    plugin = FakePlugin()
    looping_responses = [
        build_response(
            FakeMessage(
                tool_calls=[
                    FakeToolCall(
                        id=f"call-{i}",
                        function=FakeFunction(name="echo", arguments='{"text":"probe"}'),
                    )
                ]
            )
        )
        for i in range(8)
    ]
    agent, fake_completions = build_agent(plugin=plugin, responses=looping_responses)

    await agent.run(raw_event={})

    assert len(fake_completions.calls) == 8
    assert plugin.sent_replies == []


@pytest.mark.asyncio
async def test_scout_tool_budget_exhaustion_immediately_forces_json_repair() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="street lurker",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=patrol_event,
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    tool_calls = [
        FakeToolCall(
            id=f"call-{i}",
            function=FakeFunction(name="echo", arguments=json.dumps({"text": f"probe-{i}"})),
        )
        for i in range(13)
    ]
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(tool_calls=tool_calls)),
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "budget exhausted",
                            "internal_emotion": "firm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
    )

    await agent.run(raw_event={})

    assert len(fake_completions.calls) == 2
    second_call_messages = fake_completions.calls[1]["messages"]
    assert any(
        msg.get("role") == "user" and "tool-call budget is exhausted" in str(msg.get("content", ""))
        for msg in second_call_messages
    )
    assert any(
        msg.get("role") == "tool" and msg.get("content") == "Tool call skipped: budget exhausted."
        for msg in second_call_messages
    )


@pytest.mark.asyncio
@pytest.mark.skip(reason="reflection phase removed")
async def test_reflection_pass_can_commit_memory_after_reply() -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    memory_skill = StoreMemoryTestSkill()
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "worth replying",
                            "internal_emotion": "alert",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Post the public reply before reflecting on memory."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-memory",
                            function=FakeFunction(
                                name="commit_memory",
                                arguments=json.dumps({"text": "durable fact"}),
                            ),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content='{"action":"commit_memory","summary":"stored it"}')),
        ],
        skills=[EchoSkill(), memory_skill],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies[0][1] == "Public reply"
    assert len(fake_completions.calls) == 4
    reflection_tool_names = [tool["function"]["name"] for tool in fake_completions.calls[2]["tools"]]
    assert "commit_memory" in reflection_tool_names
    assert fake_completions.calls[3]["messages"][-1]["content"] == "echo:durable fact"


@pytest.mark.asyncio
@pytest.mark.skip(reason="reflection phase removed")
async def test_reflection_stage_logs_reasoning_and_tool_details(capsys: pytest.CaptureFixture[str]) -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="hello",
            author="octocat",
            author_association="OWNER",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        )
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "reply once",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Post the public reply before checking durable memory."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-memory",
                            function=FakeFunction(
                                name="retrieve_memory",
                                arguments=json.dumps({"text": "durable fact"}),
                            ),
                        )
                    ],
                    reasoning_content="Check whether this is already known.",
                )
            ),
            build_response(FakeMessage(content='{"action":"noop","summary":"nothing new"}')),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill()],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "reflection available tools: retrieve_memory" in stderr
    assert "reflection reasoning" in stderr
    assert "tool calls: [retrieve_memory]" in stderr
    assert '-> retrieve_memory({"text": "durable fact"})' in stderr
    assert "<- result: echo:durable fact" in stderr


@pytest.mark.asyncio
@pytest.mark.skip(reason="reflection phase removed")
async def test_reflection_pass_allows_noop_without_memory_mutation() -> None:
    plugin = FakePlugin()
    agent, fake_completions = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "reply once",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"will_reply": True, "target_issue_number": None, "focus_summary": "Post the public reply and finish without memory mutation."},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(FakeMessage(content='{"action":"noop","summary":"nothing durable"}')),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill(), SearchRepoMemoryTestSkill()],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies[0][1] == "Public reply"
    assert len(fake_completions.calls) == 2
    reflection_tool_names = [tool["function"]["name"] for tool in fake_completions.calls[2]["tools"]]
    assert reflection_tool_names == ["retrieve_memory", "search_repo_memory"]


@pytest.mark.asyncio
@pytest.mark.skip(reason="reflection phase removed")
async def test_stage_specific_max_tokens_floor_for_scout() -> None:
    plugin = FakePlugin()
    agent, fake_completions = build_agent(
        plugin=plugin,
        max_tokens=256,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "reply once",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "reply_brief",
                                "will_reply": True,
                                "will_act": False,
                                "target_issue_number": None,
                                "focus_summary": "Reply once and finish.",
                            },
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(FakeMessage(content='{"action":"noop","summary":"nothing durable"}')),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill()],
    )

    await agent.run(raw_event={})

    assert fake_completions.calls[0]["max_tokens"] == 4096
    assert fake_completions.calls[1]["max_tokens"] == 256
    assert fake_completions.calls[2]["max_tokens"] == 4096


def test_scout_iteration_budget_varies_by_event_type() -> None:
    passive_event = PluginEvent(
        event_id="evt-passive",
        message="hello",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=21,
        owner="acme",
        repo="widgets",
    )
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="patrol",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )

    assert RyoAgent._scout_iteration_budget(passive_event) == 8
    assert RyoAgent._scout_iteration_budget(patrol_event) == 16


@pytest.mark.asyncio
async def test_scout_stage_does_not_treat_same_tool_with_different_args_as_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-1", function=FakeFunction(name="list_files", arguments='{"path":"backend"}'))])),
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-2", function=FakeFunction(name="list_files", arguments='{"path":"backend/app"}'))])),
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-3", function=FakeFunction(name="list_files", arguments='{"path":"backend/tests"}'))])),
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "done",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
        skills=[ListFilesTestSkill()],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "effective_scout_iterations=16" in stderr
    assert "without new tool signatures" not in stderr


@pytest.mark.asyncio
async def test_scout_stage_forces_decision_after_repeating_same_tool_signature(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-1", function=FakeFunction(name="probe_echo", arguments='{"text":"same"}'))])),
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-2", function=FakeFunction(name="probe_echo", arguments='{"text":"same"}'))])),
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-3", function=FakeFunction(name="probe_echo", arguments='{"text":"same"}'))])),
            build_response(FakeMessage(tool_calls=[FakeToolCall(id="call-4", function=FakeFunction(name="probe_echo", arguments='{"text":"same"}'))])),
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "done",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {"mode": "stay_silent", "will_reply": False, "will_act": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
        skills=[ProbeEchoSkill()],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "resource probe limit reached" in stderr
    assert "JSON repair" in stderr


def test_decision_and_reflection_schema_enforce_short_fields() -> None:
    scout_fields = ScoutDecision.model_fields
    assert scout_fields["context_analysis"].description == "环境分析：极简总结，不超过 100 个字。"
    assert scout_fields["internal_emotion"].description == "内心OS：一句话，不超过 60 个字。"
    assert scout_fields["biological_clock_impact"].description == "生理时钟影响：极简描述，不超过 60 个字。"


def test_terminal_visible_mutation_rejects_error_results() -> None:
    patrol_event = PluginEvent(
        event_id="evt-patrol",
        message="",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )
    skill = CreatePRReviewTestSkill()
    assert RyoAgent._is_terminal_visible_mutation(
        event=patrol_event,
        skill=skill,
        tool_result="Submitted APPROVE review on PR #103",
    )
    assert not RyoAgent._is_terminal_visible_mutation(
        event=patrol_event,
        skill=skill,
        tool_result='GitHub API error (422): {"message":"Unprocessable Entity"}',
    )
    assert not RyoAgent._is_terminal_visible_mutation(
        event=patrol_event,
        skill=skill,
        tool_result="GitHub API error (404): Not Found",
    )


def test_scout_decision_accepts_short_english_bug_summaries() -> None:
    decision = ScoutDecision.model_validate(
        {
            "context_analysis": "Ghost bikes found in starter fleet initialization; PR needs review.",
            "internal_emotion": "bug found, time to review",
            "biological_clock_impact": "Fresh catch, best timing",
            "action_decision": {
                "mode": "act_directly",
                "will_reply": True,
                "will_act": True,
                "focus_summary": "Post a blocking review on the PR.",
                "context_issue_numbers": [],
                "target_issue_number": 89,
            },
        }
    )
    assert decision.internal_emotion == "bug found, time to review"
    assert decision.biological_clock_impact == "Fresh catch, best timing"


def test_prompts_require_short_json_fields() -> None:
    decision_prompt = prompts.build_decision_prompt(system_prompt="sys", mind_context="")

    assert "get_project_tree → find_file_paths/search_symbol" in decision_prompt
    assert "context_analysis 必须极短，不超过 100 个字" in decision_prompt
    assert "internal_emotion 必须一句话，不超过 60 个字" in decision_prompt
    assert "biological_clock_impact 不超过 60 个字" in decision_prompt


@pytest.mark.asyncio
async def test_scout_stage_logs_critical_when_json_looks_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-patrol",
            message="street lurker",
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner="acme",
            repo="widgets",
            is_patrol=True,
        ),
        history_by_issue={0: HistorySnapshot(messages=[], subconscious={}, runtime_state=RepoRuntimeState(), patrol_brief="brief")},
    )
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(FakeMessage(content='{"context_analysis":"ok","internal_emotion":"x","biologica')),
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "done",
                            "internal_emotion": "calm",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "stay_silent",
                                "will_reply": False,
                                "will_act": False,
                                "target_issue_number": None,
                            },
                        }
                    )
                )
            ),
        ],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "[CRITICAL] JSON 解析失败！疑似命中 max_tokens 截断" in stderr
    assert "stage=scout" in stderr


@pytest.mark.asyncio
@pytest.mark.skip(reason="reflection phase removed")
async def test_reflection_stage_logs_critical_when_json_looks_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    plugin = FakePlugin()
    agent, _ = build_agent(
        plugin=plugin,
        responses=[
            build_response(
                FakeMessage(
                    content=json.dumps(
                        {
                            "context_analysis": "reply once",
                            "internal_emotion": "steady",
                            "biological_clock_impact": "neutral",
                            "action_decision": {
                                "mode": "reply_brief",
                                "will_reply": True,
                                "will_act": False,
                                "target_issue_number": None,
                                "focus_summary": "Reply once and finish.",
                            },
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(FakeMessage(content='{"action":"noop","summary":"unterminated')),
            build_response(FakeMessage(content='{"action":"noop","summary":"ok"}')),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill()],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "[CRITICAL] JSON 解析失败！疑似命中 max_tokens 截断" in stderr
    assert "stage=reflection" in stderr


def test_truncation_heuristic_detects_unclosed_json_shapes() -> None:
    assert _looks_like_truncated_json('{"context_analysis":"ok","internal_emotion":"x","biologica')
    assert _looks_like_truncated_json('{"key":"value"')
    assert _looks_like_truncated_json('{"key":123')
    assert not _looks_like_truncated_json('{"key":"value"}')


class TestExtractSafeJson:
    def test_pure_json_passes_through(self) -> None:
        result = _extract_safe_json('{"mode":"stay_silent","will_reply":false}')
        assert result == {"mode": "stay_silent", "will_reply": False}

    def test_json_inside_json_code_block(self) -> None:
        result = _extract_safe_json('```json\n{"mode":"stay_silent","will_reply":false}\n```')
        assert result == {"mode": "stay_silent", "will_reply": False}

    def test_json_inside_plain_code_block(self) -> None:
        result = _extract_safe_json('```\n{"key":"value"}\n```')
        assert result == {"key": "value"}

    def test_json_surrounded_by_explanation_text(self) -> None:
        result = _extract_safe_json(
            'Here is my decision:\n{"mode":"reply_brief","will_reply":true}\nI hope this works.'
        )
        assert result == {"mode": "reply_brief", "will_reply": True}

    def test_nested_json_surrounded_by_text(self) -> None:
        result = _extract_safe_json(
            'Sure! {"context_analysis":"done","action_decision":{"mode":"reply_brief","will_reply":true}} Done.'
        )
        assert result == {
            "context_analysis": "done",
            "action_decision": {"mode": "reply_brief", "will_reply": True},
        }

    def test_complete_scout_decision_in_explanation(self) -> None:
        text = (
            "Let me think about this...\n\n"
            'I will output: ```json\n'
            '{"context_analysis":"ready","internal_emotion":"calm","biological_clock_impact":"neutral",'
            '"action_decision":{"mode":"act_directly","will_reply":true,"will_act":true,"execution_identity":"self",'
            '"comment_kind":"response","focus_summary":"fix it","context_issue_numbers":[],'
            '"target_issue_number":null}}\n'
            '```\n\nThat should work.'
        )
        result = _extract_safe_json(text)
        assert result["context_analysis"] == "ready"
        assert result["action_decision"]["mode"] == "act_directly"


class TestExtractResultSummary:
    def test_read_thread_meta_closed_pr(self) -> None:
        result = (
            "Thread #98: [Cleanup] Remove stale async stub classes from services/__init__.py\n"
            "Type: PR\n"
            "State: closed\n"
            "Author: github-actions[bot]\n"
            "Labels: cleanup\n"
            "Created: 2026-05-09T12:12:52Z\n"
            "Updated: 2026-05-10T11:04:39Z\n"
            "Closed: 2026-05-10T11:04:39Z\n"
            "URL: https://github.com/kcccn/sharedBikes/pull/98\n"
            "Draft: False\n"
            "Merged: True\n"
            "Merged at: 2026-05-10T11:04:39Z\n"
            "Base: main\n"
            "Head: cleanup/remove-stub-classes-init"
        )
        summary = _extract_result_summary("read_thread_meta", result)
        assert "[Cleanup] Remove stale async stub classes" in summary
        assert "closed" in summary
        assert "pr" in summary.lower()

    def test_read_thread_meta_open_issue(self) -> None:
        result = (
            "Thread #114: Phase 5 Frontend: Leaflet map init + popup from WS bootstrap\n"
            "Type: Issue\n"
            "State: open\n"
            "Author: github-actions[bot]\n"
            "Labels: enhancement, frontend, phase5\n"
            "Created: 2026-05-10T11:15:13Z\n"
            "Updated: 2026-05-10T11:15:13Z\n"
            "Closed: N/A\n"
            "URL: https://github.com/kcccn/sharedBikes/issues/114\n"
            "Draft: False\n"
            "Merged: False"
        )
        summary = _extract_result_summary("read_thread_meta", result)
        assert "Phase 5 Frontend" in summary
        assert "open" in summary

    def test_read_thread_comments_with_comments(self) -> None:
        result = (
            "Comments for issue/PR #106:\n"
            "github-actions[bot] at 2026-05-09T20:28:46Z: ## Architect review\n\nOption A is correct.\n"
            "\n---\n"
            "user123 at 2026-05-10T08:15:00Z: LGTM"
        )
        summary = _extract_result_summary("read_thread_comments", result)
        assert "2 comment" in summary
        assert "user123" in summary

    def test_read_thread_comments_empty(self) -> None:
        result = "No comments found for issue/PR #114."
        summary = _extract_result_summary("read_thread_comments", result)
        assert "no comments" in summary

    def test_read_code_diff(self) -> None:
        result = (
            "diff --git a/backend/app/core/city.py b/backend/app/core/city.py\n"
            "+station_id: str\n"
            "+position: LatLng\n"
            "diff --git a/backend/app/ws/handler.py b/backend/app/ws/handler.py\n"
            "+await ws.send_json(bootstrap)\n"
            "-pass\n"
        )
        summary = _extract_result_summary("read_code_diff", result)
        assert "2 file" in summary

    def test_read_issue_body(self) -> None:
        result = "# Roadmap\n\n## Phase 5\n\nContent here..."
        summary = _extract_result_summary("read_issue_body", result)
        assert "Roadmap" in summary


class TestScoutBriefWithSummaries:
    """Verify that _build_scout_brief includes result summaries, not just labels."""

    @pytest.fixture
    def agent(self) -> RyoAgent:
        plugin = FakePlugin()
        llm_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions([])))
        agent = RyoAgent(
            persona={"identity": "pm", "system_prompt": "test", "model": "test"},
            persona_registry={},
            skills=[EchoSkill()],
            llm_client=llm_client,
            plugin=plugin,
        )
        return agent

    def test_brief_includes_summary_when_cached_result_exists(self, agent: RyoAgent) -> None:
        agent._scout_results = {
            ("read_thread_meta", "98"): (
                "Thread #98: [Cleanup] Remove stale async stub classes\n"
                "Type: PR\nState: closed\nMerged: True\n"
                "Merged at: 2026-05-10T11:04:39Z\n"
            ),
            ("read_thread_meta", "114"): (
                "Thread #114: Phase 5 Frontend: Leaflet map init\n"
                "Type: Issue\nState: open\nMerged: False\n"
            ),
        }
        resource_uses: dict = {
            ("read_thread_meta", "98"): 1,
            ("read_thread_meta", "114"): 1,
        }
        brief = agent._build_scout_brief(resource_uses)
        # Should contain actual data, not just labels
        assert "[Cleanup] Remove stale async stub" in brief
        assert "Phase 5 Frontend" in brief
        assert "closed" in brief
        assert "open" in brief
        # Should still contain cache markers for code-level dedup fallback
        assert "<!-- ryo:scout_key:read_thread_meta:98 -->" in brief
        assert "<!-- ryo:scout_key:read_thread_meta:114 -->" in brief

    def test_brief_falls_back_to_label_when_no_cached_result(self, agent: RyoAgent) -> None:
        agent._scout_results = {}
        resource_uses: dict = {
            ("read_thread_meta", "42"): 1,
        }
        brief = agent._build_scout_brief(resource_uses)
        # Should still include the label even without cached data
        assert "read_thread_meta(#42)" in brief
        assert "<!-- ryo:scout_key:read_thread_meta:42 -->" in brief

    def test_brief_excludes_non_github_tools(self, agent: RyoAgent) -> None:
        agent._scout_results = {
            ("read_file", "src/main.py"): "print('hello')",
        }
        resource_uses: dict = {
            ("read_file", "src/main.py"): 1,
            ("read_thread_meta", "1"): 1,
        }
        brief = agent._build_scout_brief(resource_uses)
        # read_file should NOT appear (not in _GITHUB_READ_TOOLS)
        assert "read_file" not in brief
        # read_thread_meta SHOULD appear
        assert "read_thread_meta" in brief


async def test_reply_cache_hit_returns_cached_result(capsys: pytest.CaptureFixture[str]) -> None:
    """Reply phase returns cached scout result instead of calling API for a re-read."""
    event = PluginEvent(
        event_id="evt-cache",
        message="test cache",
        author="octocat",
        issue_id="1",
        issue_number=1,
        comment_id=0,
        owner="test-owner",
        repo="test-repo",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=event,
        history_by_issue={
            1: HistorySnapshot(
                messages=[],
                has_own_state=True,
                subconscious={},
                runtime_state=RepoRuntimeState(next_patrol_after="2026-05-10T12:00:00Z"),
                patrol_brief="",
            ),
        },
    )

    # Scout phase will call read_thread_meta(1) and cache the result
    scout_response = build_response(
        FakeMessage(
            reasoning_content="scout thinking",
            tool_calls=[
                FakeToolCall(id="t1", function=FakeFunction(name="read_thread_meta", arguments='{"issue_number": 1}')),
            ],
        )
    )
    # Then immediately return a decision
    decision_response = build_response(
        FakeMessage(
            content='{"context_analysis":"done","internal_emotion":"calm","biological_clock_impact":"ready","action_decision":{"mode":"act_directly","will_reply":false,"will_act":true,"execution_identity":"self","comment_kind":"response","focus_summary":"update roadmap","context_issue_numbers":[],"target_issue_number":1}}',
        )
    )

    # Reply phase: model tries to re-read #1, should get cached result
    reply_response = build_response(
        FakeMessage(
            reasoning_content="reply thinking",
            tool_calls=[
                FakeToolCall(id="t2", function=FakeFunction(name="read_thread_meta", arguments='{"issue_number": 1}')),
            ],
        )
    )
    # Then model acts
    reply_act_response = build_response(
        FakeMessage(
            reasoning_content="acting now",
            tool_calls=[
                FakeToolCall(id="t3", function=FakeFunction(name="update_issue", arguments='{"issue_number": 1, "body": "updated"}')),
            ],
        )
    )
    # Model finishes with text reply (no more tool calls)
    reply_done_response = build_response(
        FakeMessage(
            content="Roadmap updated successfully.",
        )
    )

    fake_completions = FakeCompletions([
        scout_response,
        decision_response,
        reply_response,
        reply_act_response,
        reply_done_response,
    ])
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

    agent = RyoAgent(
        persona={"identity": "pm", "system_prompt": "test", "model": "test"},
        persona_registry={"pm": {"identity": "pm", "system_prompt": "test", "model": "test"}},
        skills=[ReadThreadMetaFakeSkill(), UpdateIssueFakeSkill()],
        llm_client=llm_client,
        plugin=plugin,
        max_iterations=5,
    )

    await agent.run({"event": "test"})

    stderr = capsys.readouterr().err
    # Should log cache HIT for the re-read
    assert "cache HIT: read_thread_meta(1)" in stderr
    # Should log scout brief with summary
    assert "scout brief:" in stderr
    # Should log reply caching info
    assert "reply caching:" in stderr
    # Should log run total time
    assert "run completed in" in stderr


async def test_reply_cache_miss_logs_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """When reply reads a resource not in scout, log cache MISS with reason."""
    event = PluginEvent(
        event_id="evt-miss",
        message="test miss",
        author="octocat",
        issue_id="1",
        issue_number=1,
        comment_id=0,
        owner="test-owner",
        repo="test-repo",
        is_patrol=True,
    )
    plugin = FakePlugin(
        event=event,
        history_by_issue={
            1: HistorySnapshot(
                messages=[],
                has_own_state=True,
                subconscious={},
                runtime_state=RepoRuntimeState(next_patrol_after="2026-05-10T12:00:00Z"),
                patrol_brief="",
            ),
        },
    )

    # Scout reads #1 and caches it
    scout_read = build_response(
        FakeMessage(
            reasoning_content="scout",
            tool_calls=[
                FakeToolCall(id="s1", function=FakeFunction(name="read_thread_meta", arguments='{"issue_number": 1}')),
            ],
        )
    )
    # Scout decides to act on #2 (which was NOT in scout), switching the active event
    scout_decide = build_response(
        FakeMessage(
            content='{"context_analysis":"done","internal_emotion":"calm","biological_clock_impact":"ready","action_decision":{"mode":"act_directly","will_reply":false,"will_act":true,"execution_identity":"self","comment_kind":"response","focus_summary":"check #2","context_issue_numbers":[],"target_issue_number":2}}',
        )
    )
    # Reply tries to read #2 which was NOT scouted → cache MISS
    reply_read = build_response(
        FakeMessage(
            reasoning_content="reply",
            tool_calls=[
                FakeToolCall(id="r1", function=FakeFunction(name="read_thread_meta", arguments='{"issue_number": 2}')),
            ],
        )
    )
    reply_act = build_response(
        FakeMessage(
            tool_calls=[
                FakeToolCall(id="r2", function=FakeFunction(name="add_labels", arguments='{"issue_number": 2, "labels": ["test"]}')),
            ],
        )
    )
    reply_done = build_response(
        FakeMessage(content="Labels added."),
    )

    fake_completions = FakeCompletions([
        scout_read,
        scout_decide,
        reply_read,
        reply_act,
        reply_done,
    ])
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))

    agent = RyoAgent(
        persona={"identity": "pm", "system_prompt": "test", "model": "test"},
        persona_registry={"pm": {"identity": "pm", "system_prompt": "test", "model": "test"}},
        skills=[ReadThreadMetaFakeSkill(), AddLabelsFakeSkill()],
        llm_client=llm_client,
        plugin=plugin,
        max_iterations=5,
    )

    await agent.run({"event": "test"})

    stderr = capsys.readouterr().err
    # #2 was not scouted, should show cache MISS with "not in scout_read_keys"
    assert "cache MISS: read_thread_meta(2)" in stderr
    assert "not in scout_read_keys" in stderr
