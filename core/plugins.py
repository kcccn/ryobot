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
    is_workflow_dispatch: bool = False
    mission: str = ""
    head_ref: str = ""


class OplogEntry(BaseModel):
    """A single patrol action record stored in RepoRuntimeState.oplog."""

    ts: str = ""
    bot: str = ""
    action: str = ""
    result: str = ""  # success | failed | blocked | silent
    blocked_by: str = ""
    issue: int = 0


class HistorySnapshot(BaseModel):
    """Conversation history plus persisted subconscious state."""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    subconscious: dict[str, Any] = Field(default_factory=dict)
    mind_body: str = ""
    mind_issue_number: int = 0
    runtime_state: RepoRuntimeState = Field(default_factory=lambda: RepoRuntimeState())
    patrol_brief: str = ""
    memory_index: str = ""
    operational_log: list[OplogEntry] = Field(default_factory=list)


class BotFatigueState(BaseModel):
    last_spoke_at: str | None = None
    next_available_at: str | None = None


class RoutingRecord(BaseModel):
    event_id: str = ""
    bot_identity: str = ""
    dispatcher_identity: str = ""
    reason: str = ""
    target_issue_number: int | None = None
    routed_at: str | None = None


class RepoRuntimeState(BaseModel):
    next_patrol_after: str | None = None
    bot_fatigue: dict[str, BotFatigueState] = Field(default_factory=dict)
    last_routing: RoutingRecord = Field(default_factory=RoutingRecord)
    coordination_issue_number: int = 0
    oplog: list[OplogEntry] = Field(default_factory=list)


class ActionDecision(BaseModel):
    mode: str = "reply_brief"
    will_reply: bool
    will_act: bool = False
    execution_identity: str = "self"
    comment_kind: str = "response"
    focus_summary: str = ""
    context_issue_numbers: list[int] = Field(default_factory=list)
    target_issue_number: int | None = None


class ScoutDecision(BaseModel):
    context_analysis: str = Field(
        description="环境分析：极简总结，不超过 100 个字。",
    )
    internal_emotion: str = Field(
        description="内心OS：一句话，不超过 60 个字。",
    )
    biological_clock_impact: str = Field(
        description="生理时钟影响：极简描述，不超过 60 个字。",
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
