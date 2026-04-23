from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class PluginEvent(BaseModel):
    """Normalized inbound event consumed by the application layer."""

    event_id: str
    message: str
    author: str
    issue_id: str
    issue_number: int
    comment_id: int
    owner: str
    repo: str


class HistorySnapshot(BaseModel):
    """Conversation history plus persisted subconscious state."""

    messages: list[dict[str, str]] = Field(default_factory=list)
    subconscious: dict[str, Any] = Field(default_factory=dict)


class BasePlugin(ABC):
    """Port for inbound event parsing and outbound message delivery."""

    @abstractmethod
    def parse_event(self, raw_payload: Any) -> PluginEvent:
        """Normalize a platform payload into the agent's event shape."""

    @abstractmethod
    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        """Return prior chat messages plus structured subconscious state."""

    @abstractmethod
    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        """Deliver the final assistant reply back through the platform."""
