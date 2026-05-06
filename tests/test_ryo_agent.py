from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from core.ryo_agent import DEFAULT_FALLBACK_MESSAGE, RyoAgent
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
        last_bot_comment_at: str | None = None,
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
        self._last_bot_comment_at = last_bot_comment_at
        self.sent_replies: list[tuple[PluginEvent, str, dict[str, Any] | None]] = []

    def parse_event(self, raw_payload: Any) -> PluginEvent:
        return self._event

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        assert event == self._event
        return HistorySnapshot(
            messages=list(self._history_messages),
            subconscious=dict(self._subconscious),
            last_bot_comment_at=self._last_bot_comment_at,
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


class MutatingSkill(EchoSkill):
    name = "mutate"
    description = "Mutate external state."
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"mutated:{args.text}"


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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={"payload": "ignored"})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Final answer", {"mode": "reflective"})
    ]
    assert fake_completions.calls[0]["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "assistant", "content": "Previous reply"},
        {"role": "user", "content": "Need help"},
    ]


@pytest.mark.asyncio
async def test_run_hides_mutating_tools_from_untrusted_authors() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Read-only answer"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
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
        ),
    )
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill(), MutatingSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

    tool_names = [
        tool["function"]["name"]
        for tool in fake_completions.calls[0]["tools"]
    ]
    assert tool_names == ["echo"]


@pytest.mark.asyncio
async def test_run_exposes_mutating_tools_to_trusted_authors() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Can act"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="Please label this",
            author="maintainer",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
            author_association="MEMBER",
        ),
    )
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill(), MutatingSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

    tool_names = [
        tool["function"]["name"]
        for tool in fake_completions.calls[0]["tools"]
    ]
    assert tool_names == ["echo", "mutate"]


@pytest.mark.asyncio
async def test_run_rejects_mutating_tool_calls_from_untrusted_authors() -> None:
    fake_completions = FakeCompletions(
        [
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="mutate", arguments='{"text":"close it"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="Recovered")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(
        event=PluginEvent(
            event_id="evt-1",
            message="Close this",
            author="octocat",
            issue_id="1001",
            issue_number=12,
            comment_id=21,
            owner="acme",
            repo="widgets",
            author_association="NONE",
        ),
    )
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[MutatingSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

    assert fake_completions.calls[1]["messages"][-1]["content"] == (
        "Tool error: Tool 'mutate' is not available for author association 'NONE'."
    )


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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={"payload": "ignored"})

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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        max_iterations=5,
    )

    await ryo_agent.run(raw_event={})

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
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[ContextAwareSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

    tool_message = fake_completions.calls[1]["messages"][-1]
    assert '"owner": "acme"' in tool_message["content"]
    assert '"repo": "widgets"' in tool_message["content"]
    assert '"issue_number": 12' in tool_message["content"]
    assert '"memory": "sticky"' in tool_message["content"]
    assert get_skill_context() == {}


@pytest.mark.asyncio
async def test_run_truncates_large_tool_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_MAX_TOOL_RESULT_CHARS", "10")
    fake_completions = FakeCompletions(
        [
            build_response(
                FakeMessage(
                    tool_calls=[
                        FakeToolCall(
                            id="call-1",
                            function=FakeFunction(name="echo", arguments='{"text":"abcdefghijklmnop"}'),
                        )
                    ]
                )
            ),
            build_response(FakeMessage(content="Done")),
        ]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin()
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await ryo_agent.run(raw_event={})

    tool_message = fake_completions.calls[1]["messages"][-1]["content"]
    assert tool_message.startswith("echo:abcde")
    assert "[truncated:" in tool_message


@pytest.mark.asyncio
async def test_run_skips_when_within_cooldown() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Should not be called"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    plugin = FakePlugin(last_bot_comment_at=recent_ts)
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        cooldown_seconds=120,
    )

    await ryo_agent.run(raw_event={})

    # No reply sent and no LLM call made
    assert plugin.sent_replies == []
    assert fake_completions.calls == []


@pytest.mark.asyncio
async def test_run_proceeds_when_cooldown_expired() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Final answer"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    plugin = FakePlugin(last_bot_comment_at=old_ts)
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        cooldown_seconds=120,
    )

    await ryo_agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "Final answer", {})
    ]


@pytest.mark.asyncio
async def test_run_proceeds_when_no_bot_history() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="First response"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    # No prior bot comments — should proceed regardless of cooldown config
    plugin = FakePlugin(last_bot_comment_at=None)
    ryo_agent = RyoAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
        cooldown_seconds=120,
    )

    await ryo_agent.run(raw_event={})

    assert plugin.sent_replies == [
        (plugin.parse_event(None), "First response", {})
    ]
