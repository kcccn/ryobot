from __future__ import annotations

import os
import shlex
from typing import Any

from ..utils import csv_env
from ._base import GitHubSkillBase

DEFAULT_ALLOWED_COMMANDS = frozenset({
    "pytest",
    "python -m pytest",
    "ruff check",
    "mypy",
    "pyright",
    "python -m pip install",
    "pip install",
})
SHELL_METACHARS = frozenset({"|", "&", ";", "<", ">", "`", "$", "\n", "\r"})
SAFE_ENV_KEYS = frozenset({
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "GITHUB_WORKSPACE",
    "PYTHONPATH",
})


async def _repo_label_names(skill: GitHubSkillBase, context: dict[str, Any]) -> set[str]:
    labels = await skill._fetch_paginated(
        f"/repos/{context['owner']}/{context['repo']}/labels",
        params={"per_page": 100},
    )
    return {str(label.get("name") or "") for label in labels}


def _comment_location(comment: dict[str, Any]) -> str:
    path = str(comment.get("path") or "")
    line = comment.get("line") or comment.get("original_line")
    if path and line:
        return f" [{path}:{line}]"
    if path:
        return f" [{path}]"
    return ""


def _parse_safe_command(command: str) -> list[str] | str:
    if any(char in command for char in SHELL_METACHARS):
        return "Shell metacharacters are not allowed in run_command."
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return f"Invalid command syntax: {exc}"
    if not parts:
        return "Command is empty."
    return parts


def _allowed_command_prefixes() -> set[str]:
    configured = csv_env("RYOBOT_ALLOWED_COMMANDS")
    return configured or set(DEFAULT_ALLOWED_COMMANDS)


def _is_allowed_command(parts: list[str]) -> bool:
    for prefix in _allowed_command_prefixes():
        prefix_parts = shlex.split(prefix)
        if parts[: len(prefix_parts)] == prefix_parts:
            return True
    return False


def _command_timeout_seconds() -> int:
    raw = os.getenv("RYOBOT_COMMAND_TIMEOUT_SECONDS", "300")
    try:
        value = int(raw)
    except ValueError:
        return 300
    return max(1, value)


def _safe_subprocess_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in SAFE_ENV_KEYS
    }
