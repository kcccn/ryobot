from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BasePlugin(ABC):
    """Port for inbound event parsing and outbound message delivery."""

    @abstractmethod
    def parse_event(self, raw_payload: Any) -> dict[str, Any]:
        """Normalize a platform payload into the agent's event shape."""

    @abstractmethod
    async def fetch_history(self, event_id: str) -> list[dict[str, str]]:
        """Return prior OpenAI-compatible chat messages for an event."""

    @abstractmethod
    async def send_reply(self, event_id: str, content: str) -> None:
        """Deliver the final assistant reply back through the platform."""
