from __future__ import annotations

import os
from typing import Any

MEMORY_LABEL = "🧠 memory"
DELETED_MEMORY_LABEL = "🗑️ deleted"
LIVE_MIND_LABEL = "🧠 live-mind"
COORDINATION_LABEL = "🎙️ coordination"
BOT_LABEL_PREFIX = "bot:"
COORDINATION_ISSUE_TITLE = "🎙️ RyoBot Coordination"
MIND_ISSUE_TITLE_PREFIX = "🧠 "


def max_chars_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[truncated: {omitted} chars omitted]"


def csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def sanitize_mentions(text: str) -> str:
    """Insert a zero-width space after every @ to prevent GitHub mention notifications."""
    return text.replace("@", "@​")


def is_internal_issue_artifact(issue: dict[str, Any]) -> bool:
    labels = {str(label.get("name") or "") for label in issue.get("labels", [])}
    if (
        MEMORY_LABEL in labels
        or DELETED_MEMORY_LABEL in labels
        or LIVE_MIND_LABEL in labels
        or COORDINATION_LABEL in labels
        or any(label.startswith(BOT_LABEL_PREFIX) for label in labels)
    ):
        return True
    return False
