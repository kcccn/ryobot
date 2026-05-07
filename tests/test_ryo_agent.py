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


def build_response(message: FakeMessage) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=message)])


def build_agent(
    *,
    plugin: FakePlugin,
    responses: list[FakeResponse],
    skills: list[BaseSkill] | None = None,
    threshold: int = 70,
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
