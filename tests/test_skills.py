from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from core.plugins import PluginEvent
from core.skills import (
    BaseSkill,
    clear_skill_context,
    get_skill_context,
    set_skill_context,
)


class ExampleArgs(BaseModel):
    topic: str
    limit: int = 3


class ExampleSkill(BaseSkill):
    name = "example"
    description = "Demonstrate schema generation."
    args_model = ExampleArgs

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_get_json_schema_returns_pydantic_schema() -> None:
    schema = ExampleSkill().get_json_schema()

    assert schema["type"] == "object"
    assert schema["properties"]["topic"]["type"] == "string"
    assert schema["properties"]["limit"]["default"] == 3
    assert schema["required"] == ["topic"]


def test_get_tool_definition_wraps_schema_for_llm() -> None:
    tool_definition = ExampleSkill().get_tool_definition()

    assert tool_definition == {
        "type": "function",
        "function": {
            "name": "example",
            "description": "Demonstrate schema generation.",
            "parameters": ExampleArgs.model_json_schema(),
        },
    }


def test_skill_context_round_trips_event_and_subconscious() -> None:
    event = PluginEvent(
        event_id="evt-1",
        message="hello",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=21,
        owner="acme",
        repo="widgets",
    )

    token = set_skill_context(event=event, subconscious={"mode": "focus"})
    try:
        context = get_skill_context()
    finally:
        clear_skill_context(token)

    assert context["owner"] == "acme"
    assert context["repo"] == "widgets"
    assert context["issue_number"] == 12
    assert context["subconscious"] == {"mode": "focus"}


def test_get_skill_context_defaults_to_empty_mapping() -> None:
    clear_skill_context()
    assert get_skill_context() == {}


@pytest.mark.asyncio
async def test_skill_context_is_available_inside_execute() -> None:
    class ContextReadingSkill(BaseSkill):
        name = "context_reader"
        description = "Read runtime context."
        args_model = BaseModel

        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            return get_skill_context()

    event = PluginEvent(
        event_id="evt-1",
        message="hello",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=21,
        owner="acme",
        repo="widgets",
    )
    token = set_skill_context(event=event, subconscious={"trail": "on"})
    try:
        result = await ContextReadingSkill().execute()
    finally:
        clear_skill_context(token)

    assert result["owner"] == "acme"
    assert result["subconscious"] == {"trail": "on"}
