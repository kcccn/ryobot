from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class PluginEvent(BaseModel):
    """Normalized inbound event consumed by the application layer."""

    event_id: str
    message: str
    author: str
    author_association: str = "NONE"
    issue_id: str
    issue_number: int
    comment_id: int
    owner: str
    repo: str
    is_pull_request: bool = False
    is_patrol: bool = False
    head_ref: str = ""


class HistorySnapshot(BaseModel):
    """Conversation history plus persisted subconscious state."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    subconscious: dict[str, Any] = Field(default_factory=dict)
    mind_body: str = ""
    mind_issue_number: int = 0
    runtime_state: RepoRuntimeState = Field(default_factory=lambda: RepoRuntimeState())
    patrol_brief: str = ""


class BotFatigueState(BaseModel):
    last_spoke_at: str | None = None
    next_available_at: str | None = None


class RoutingRecord(BaseModel):
    event_id: str = ""
    bot_identity: str = ""
    dispatcher_identity: str = ""
    reason: str = ""
    target_issue_number: int | None = None
    handoff_to: str | None = None
    handoff_reason: str = ""
    discussion_count: int = 0
    handoff_count: int = 0
    routed_at: str | None = None


class RepoRuntimeState(BaseModel):
    next_patrol_after: str | None = None
    bot_fatigue: dict[str, BotFatigueState] = Field(default_factory=dict)
    last_routing: RoutingRecord = Field(default_factory=RoutingRecord)
    coordination_issue_number: int = 0


class ActionDecision(BaseModel):
    mode: str = "stay_silent"
    will_reply: bool
    will_act: bool = False
    execution_identity: str = "self"
    comment_kind: str = "response"
    handoff_to: str | None = None
    handoff_reason: str = ""
    focus_summary: str = ""
    context_issue_numbers: list[int] = Field(default_factory=list)
    continue_session: bool = False
    done: bool = False
    target_issue_number: int | None = None


class WillDecision(BaseModel):
    context_analysis: str = Field(
        description="环境分析：极简总结，不超过 100 个字。",
        max_length=100,
    )
    internal_emotion: str = Field(
        description="内心OS：一句话，不超过 60 个字。",
        max_length=60,
    )
    biological_clock_impact: str = Field(
        description="生理时钟影响：极简描述，不超过 60 个字。",
        max_length=60,
    )
    action_decision: ActionDecision


class BasePlugin(ABC):
    """Port for inbound event parsing and outbound message delivery."""

    @abstractmethod
    def parse_event(self, raw_payload: Any) -> PluginEvent:
        """Normalize a platform payload into the RyoAgent event shape."""

    @abstractmethod
    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        """Return prior chat messages plus structured subconscious state."""

    @abstractmethod
    async def resolve_target_event(self, event: PluginEvent, issue_number: int) -> PluginEvent:
        """Resolve another issue or pull request within the same repository."""

    @abstractmethod
    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        """Deliver the final assistant reply back through the platform."""

    @abstractmethod
    def set_identity(self, identity: str, display_name: str) -> None:
        """Switch the active bot identity used for mind issue lookups and comment markers."""

    @abstractmethod
    async def update_runtime_state(self, state: RepoRuntimeState) -> RepoRuntimeState:
        """Persist repo-wide runtime state used for routing and fatigue tracking."""
