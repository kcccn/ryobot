from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    identity: str
    display_name: str
    system_prompt: str
    description: str
    skill_filter: frozenset[str] | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    provider: str = "openai"
    max_tokens: int = 4096
