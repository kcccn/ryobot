from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from core.plugins import (
    BasePlugin,
    BotFatigueState,
    HistorySnapshot,
    PluginEvent,
    RepoRuntimeState,
)
from core.ryo_agent import RyoAgent
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
        )

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        self.sent_replies.append((event, content, subconscious))

    async def update_runtime_state(self, state: RepoRuntimeState) -> RepoRuntimeState:
        self.updated_runtime_states.append(state)
        return state


class EchoArgs(BaseModel):
    text: str


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


class CommitMemoryTestSkill(EchoSkill):
    name = "commit_memory"
    description = "Store long-term memory."
    mutates_state = True


class RetrieveMemoryTestSkill(EchoSkill):
    name = "retrieve_memory"
    description = "Read long-term memory."


class SearchRepoContextTestSkill(EchoSkill):
    name = "search_repo_context"
    description = "Search repo-wide issues and PRs."


class ReadThreadMetaTestSkill(EchoSkill):
    name = "read_thread_meta"
    description = "Read precise issue or PR metadata."


def build_response(message: FakeMessage) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=message)])


def build_agent(
    *,
    plugin: FakePlugin,
    responses: list[FakeResponse],
    skills: list[BaseSkill] | None = None,
    threshold: int = 70,
    street_lurker_fatigue_min_seconds: int = 60,
    street_lurker_fatigue_max_seconds: int = 180,
) -> tuple[RyoAgent, FakeCompletions]:
    fake_completions = FakeCompletions(responses)
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    agent = RyoAgent(
        persona={"identity": "architect", "model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=skills or [EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        motivation_threshold=threshold,
        fatigue_min_seconds=480,
        fatigue_max_seconds=480,
        street_lurker_fatigue_min_seconds=street_lurker_fatigue_min_seconds,
        street_lurker_fatigue_max_seconds=street_lurker_fatigue_max_seconds,
    )
    return agent, fake_completions


@pytest.mark.asyncio
async def test_run_skips_when_decision_says_no_reply() -> None:
    plugin = FakePlugin()
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
                            "motivation_score": 20,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
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
async def test_run_replies_after_passing_will_decision() -> None:
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
                            "motivation_score": 90,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
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
                            "motivation_score": 88,
                            "action_decision": {"will_reply": True, "target_issue_number": 77},
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
    assert plugin.updated_runtime_states[-1].next_patrol_after is not None


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
                            "motivation_score": 91,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
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
    fatigue = plugin.updated_runtime_states[-1].bot_fatigue["architect"]
    assert fatigue.last_spoke_at is not None


@pytest.mark.asyncio
async def test_street_lurker_actions_use_street_lurker_fatigue_window() -> None:
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
                            "motivation_score": 91,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
                        }
                    )
                )
            ),
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-pr",
                            function=FakeFunction(name="create_pull_request", arguments='{"text":"ship it"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="")),
            build_response(FakeMessage(content='{"action":"noop","summary":"done"}')),
        ],
        skills=[CreatePullRequestTestSkill()],
        street_lurker_fatigue_min_seconds=0,
        street_lurker_fatigue_max_seconds=0,
    )

    await agent.run(raw_event={})

    fatigue = plugin.updated_runtime_states[-1].bot_fatigue["architect"]
    assert fatigue.last_spoke_at == fatigue.next_available_at


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
                            "motivation_score": 95,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
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
async def test_run_skips_when_bot_is_fatigued() -> None:
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
                            "motivation_score": 95,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
                        }
                    )
                )
            )
        ],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == []
    assert "fatigue cooldown active" in plugin.updated_runtime_states[-1].last_routing.reason


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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
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
    plugin = FakePlugin()
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
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
    plugin = FakePlugin()
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
    )

    await agent.run(raw_event={})

    assert len(fake_completions.calls) == 3
    assert not any(message.get("role") == "tool" for message in fake_completions.calls[2]["messages"])
    assert "No more tool calls" in fake_completions.calls[2]["messages"][-1]["content"]


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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
        skills=[ContextAwareSkill()],
    )

    await agent.run(raw_event={})

    tool_result = fake_completions.calls[1]["messages"][-1]["content"]
    assert '"owner": "acme"' in tool_result
    assert '"is_patrol": false' in tool_result.lower()


@pytest.mark.asyncio
async def test_decide_replays_reasoning_content_after_tool_calls() -> None:
    plugin = FakePlugin()
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
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
async def test_will_stage_logs_reasoning_and_tool_details(capsys: pytest.CaptureFixture[str]) -> None:
    plugin = FakePlugin()
    agent, _ = build_agent(
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
                        }
                    )
                )
            ),
        ],
    )

    await agent.run(raw_event={})

    stderr = capsys.readouterr().err
    assert "will available tools: echo" in stderr
    assert "will reasoning" in stderr
    assert "tool calls: ['echo']" in stderr
    assert '-> echo({"text":"probe"})' in stderr
    assert "<- result: echo:probe" in stderr


@pytest.mark.asyncio
async def test_decision_prompt_prefers_memory_before_repo_context() -> None:
    plugin = FakePlugin()
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
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
                            "motivation_score": 0,
                            "action_decision": {"will_reply": False, "target_issue_number": None},
                        }
                    )
                )
            )
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
async def test_will_stage_uses_independent_iteration_budget() -> None:
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
    memory_skill = CommitMemoryTestSkill()
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
                            "motivation_score": 95,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
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
                            "motivation_score": 90,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
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
    assert "reflection available tools: echo, retrieve_memory" in stderr
    assert "reflection reasoning" in stderr
    assert "tool calls: ['retrieve_memory']" in stderr
    assert '-> retrieve_memory({"text": "durable fact"})' in stderr
    assert "<- result: echo:durable fact" in stderr


@pytest.mark.asyncio
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
                            "motivation_score": 90,
                            "action_decision": {"will_reply": True, "target_issue_number": None},
                        }
                    )
                )
            ),
            build_response(FakeMessage(content="Public reply")),
            build_response(FakeMessage(content='{"action":"noop","summary":"nothing durable"}')),
        ],
        skills=[EchoSkill(), RetrieveMemoryTestSkill(), SearchRepoContextTestSkill()],
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies[0][1] == "Public reply"
    assert len(fake_completions.calls) == 3
    reflection_tool_names = [tool["function"]["name"] for tool in fake_completions.calls[2]["tools"]]
    assert reflection_tool_names == ["echo", "retrieve_memory", "search_repo_context"]
