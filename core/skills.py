from __future__ import annotations

from abc import ABC, abstractmethod
from contextvars import ContextVar, Token
from typing import Any, ClassVar

from pydantic import BaseModel

from .plugins import PluginEvent

_SKILL_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("skill_context", default={})


def set_skill_context(
    *,
    event: PluginEvent,
    subconscious: dict[str, Any] | None = None,
) -> Token[dict[str, Any]]:
    """Seed request-scoped context for skill execution."""

    context = {
        "event_id": event.event_id,
        "message": event.message,
        "author": event.author,
        "issue_id": event.issue_id,
        "issue_number": event.issue_number,
        "comment_id": event.comment_id,
        "owner": event.owner,
        "repo": event.repo,
        "subconscious": dict(subconscious or {}),
    }
    return _SKILL_CONTEXT.set(context)


def get_skill_context() -> dict[str, Any]:
    """Read the current request-scoped skill context."""

    return dict(_SKILL_CONTEXT.get())


def clear_skill_context(token: Token[dict[str, Any]] | None = None) -> None:
    """Reset context after request handling or tests."""

    if token is None:
        _SKILL_CONTEXT.set({})
        return
    _SKILL_CONTEXT.reset(token)


class BaseSkill(ABC):
    """Port for agent tools and memory capabilities."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    args_model: ClassVar[type[BaseModel]] = BaseModel

    def get_json_schema(self) -> dict[str, Any]:
        """Expose a vendor-neutral JSON Schema derived from Pydantic."""

        if self.args_model is BaseModel:
            return {"type": "object", "properties": {}}
        return self.args_model.model_json_schema()

    def get_tool_definition(self) -> dict[str, Any]:
        """Return a full OpenAI-compatible tool definition."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_json_schema(),
            },
        }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str | dict[str, Any]:
        """Execute the tool asynchronously with validated arguments."""
