from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from core.agent import DEFAULT_FALLBACK_MESSAGE, NexusAgent
from core.plugins import BasePlugin, HistorySnapshot, PluginEvent
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
        history_messages: list[dict[str, str]] | None = None,
        subconscious: dict[str, Any] | None = None,
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
        self._history_messages = list(history_messages or [])
        self._subconscious = dict(subconscious or {})
        self.sent_replies: list[tuple[PluginEvent, str, dict[str, Any] | None]] = []

    def parse_event(self, raw_payload: Any) -> PluginEvent:
        return self._event

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        assert event == self._event
        return HistorySnapshot(
            messages=list(self._history_messages),
            subconscious=dict(self._subconscious),
        )

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        self.sent_replies.append((event, content, subconscious))


class EchoArgs(BaseModel):
    text: str


class EchoSkill(BaseSkill):
    name = "echo"
    description = "Repeat the provided text."
    args_model = EchoArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"echo:{args.text}"


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
            "subconscious": context["subconscious"],
        }


def build_response(message: FakeMessage) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=message)])


@pytest.mark.asyncio
async def test_run_sends_direct_reply_without_tool_calls() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Final answer"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(
        history_messages=[{"role": "assistant", "content": "Previous reply"}],
        subconscious={"mode": "reflective"},
        event=PluginEvent(
            event_id="evt-1",
            message="Need help",
            author="octocat",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
        ),
    )
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={"payload": "ignored"})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Final answer", {"mode": "reflective"})
    ]
    assert fake_completions.calls[0]["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "assistant", "content": "Previous reply"},
        {"role": "user", "content": "Need help"},
    ]


@pytest.mark.asyncio
async def test_run_executes_tool_call_then_sends_final_reply() -> None:
    fake_completions = FakeCompletions(
        [
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"text":"ping"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="Done")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin()
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={"payload": "ignored"})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Done", {})
    ]
    second_call_messages = fake_completions.calls[1]["messages"]
    assert second_call_messages[-2]["tool_calls"][0]["function"]["name"] == "echo"
    assert second_call_messages[-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "echo:ping",
    }


@pytest.mark.asyncio
async def test_run_surfaces_unknown_tool_errors_back_into_loop() -> None:
    fake_completions = FakeCompletions(
        [
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="missing", arguments="{}"),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="Recovered")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin()
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Recovered", {})
    ]
    assert fake_completions.calls[1]["messages"][-1]["content"].startswith("Tool error:")
    assert "Unknown tool 'missing'" in fake_completions.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_run_surfaces_validation_errors_back_into_loop() -> None:
    fake_completions = FakeCompletions(
        [
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"wrong":"shape"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="Recovered")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin()
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Recovered", {})
    ]
    assert "Tool error:" in fake_completions.calls[1]["messages"][-1]["content"]
    assert "Validation failed for tool 'echo'" in fake_completions.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_run_sends_fallback_after_max_iterations() -> None:
    repeated_tool_call = build_response(
        FakeMessage(
            tool_calls=[
                FakeToolCall(
                    id="call-1",
                    function=FakeFunction(name="echo", arguments='{"text":"loop"}'),
                )
            ]
        )
    )
    fake_completions = FakeCompletions([repeated_tool_call for _ in range(5)])
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(subconscious={"mode": "loop"})
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), DEFAULT_FALLBACK_MESSAGE, {"mode": "loop"})
    ]


@pytest.mark.asyncio
async def test_run_sets_runtime_context_for_skills() -> None:
    clear_skill_context()
    fake_completions = FakeCompletions(
        [
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
            build_response(FakeMessage(content="Done")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(subconscious={"memory": "sticky"})
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[ContextAwareSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={})

    tool_message = fake_completions.calls[1]["messages"][-1]
    assert '"owner": "acme"' in tool_message["content"]
    assert '"repo": "widgets"' in tool_message["content"]
    assert '"issue_number": 12' in tool_message["content"]
    assert '"memory": "sticky"' in tool_message["content"]
    assert get_skill_context() == {}
