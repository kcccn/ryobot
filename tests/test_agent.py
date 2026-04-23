from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from core.agent import DEFAULT_FALLBACK_MESSAGE, NexusAgent
from core.plugins import BasePlugin
from core.skills import BaseSkill


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
        event_id: str = "evt-1",
        message: str = "hello",
        history: list[dict[str, str]] | None = None,
    ) -> None:
        self._event_id = event_id
        self._message = message
        self._history = list(history or [])
        self.sent_replies: list[tuple[str, str]] = []

    def parse_event(self, raw_payload: Any) -> dict[str, Any]:
        return {"event_id": self._event_id, "message": self._message, "raw": raw_payload}

    async def fetch_history(self, event_id: str) -> list[dict[str, str]]:
        assert event_id == self._event_id
        return list(self._history)

    async def send_reply(self, event_id: str, content: str) -> None:
        self.sent_replies.append((event_id, content))


class EchoArgs(BaseModel):
    text: str


class EchoSkill(BaseSkill):
    name = "echo"
    description = "Repeat the provided text."
    args_model = EchoArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        return f"echo:{args.text}"


def build_response(message: FakeMessage) -> FakeResponse:
    return FakeResponse(choices=[FakeChoice(message=message)])


@pytest.mark.asyncio
async def test_run_sends_direct_reply_without_tool_calls() -> None:
    fake_completions = FakeCompletions(
        [build_response(FakeMessage(content="Final answer"))]
    )
    llm_client = SimpleNamespace(chat=SimpleNamespace(completions=fake_completions))
    plugin = FakePlugin(
        history=[{"role": "assistant", "content": "Previous reply"}],
        message="Need help",
    )
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={"payload": "ignored"})

    assert plugin.sent_replies == [("evt-1", "Final answer")]
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

    assert plugin.sent_replies == [("evt-1", "Done")]
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

    assert plugin.sent_replies == [("evt-1", "Recovered")]
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

    assert plugin.sent_replies == [("evt-1", "Recovered")]
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
    plugin = FakePlugin()
    agent = NexusAgent(
        persona={"model": "gpt-4.1-mini", "system_prompt": "You are helpful."},
        skills=[EchoSkill()],
        llm_client=llm_client,
        plugin=plugin,
    )

    await agent.run(raw_event={})

    assert plugin.sent_replies == [("evt-1", DEFAULT_FALLBACK_MESSAGE)]
