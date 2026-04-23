from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from core.skills import BaseSkill


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
