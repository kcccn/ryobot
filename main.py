from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from openai import AsyncOpenAI

from bots import get_bot
from core import RyoAgent
from platforms.github import (
    AddLabels,
    CloseIssue,
    CommentOnPR,
    CreateIssue,
    DispatchWorkflow,
    GitHubPlugin,
    ReadCodeDiff,
    ReadIssueMemory,
    ReadWorkflowRun,
    SearchRepoMemory,
)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_COOLDOWN_SECONDS = 120

BOT_IDENTITY = os.getenv("BOT_IDENTITY", "architect")


def _contains_own_marker(payload: dict[str, Any], identity: str) -> bool:
    """Return True if any body field in the payload contains this bot's marker."""
    marker = f"<!-- ryo:{identity}:"
    bodies: list[str] = []
    if "comment" in payload:
        bodies.append(str((payload.get("comment") or {}).get("body") or ""))
    if "issue" in payload:
        bodies.append(str((payload.get("issue") or {}).get("body") or ""))
    if "pull_request" in payload:
        bodies.append(str((payload.get("pull_request") or {}).get("body") or ""))
    return any(marker in body for body in bodies)


def main() -> None:
    try:
        payload = _load_event_payload()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if _contains_own_marker(payload, BOT_IDENTITY):
        raise SystemExit(0)

    try:
        github_token = _require_env("GITHUB_TOKEN")
        bot = get_bot(BOT_IDENTITY)
        api_key_env = bot.api_key_env or "DEEPSEEK_API_KEY"
        api_key = _require_env(api_key_env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        asyncio.run(_run(payload, github_token=github_token, api_key=api_key))
    except Exception as exc:
        print(f"Fatal entrypoint error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _run(
    payload: dict[str, Any],
    *,
    github_token: str,
    api_key: str,
) -> None:
    bot = get_bot(BOT_IDENTITY)
    base_url = bot.base_url or os.getenv("LLM_BASE_URL", DEEPSEEK_BASE_URL)
    model = bot.model or os.getenv("LLM_MODEL", DEFAULT_MODEL)
    cooldown_seconds = int(os.getenv("COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
    max_iterations = int(os.getenv("MAX_ITERATIONS", "5"))
    http_client = httpx.AsyncClient(
        base_url="https://api.github.com",
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    try:
        plugin = GitHubPlugin(token=github_token, client=http_client, identity=BOT_IDENTITY)
        skills = [
            ReadIssueMemory(token=github_token, client=http_client),
            SearchRepoMemory(token=github_token, client=http_client),
            ReadCodeDiff(token=github_token, client=http_client),
            CreateIssue(token=github_token, client=http_client),
            AddLabels(token=github_token, client=http_client),
            CloseIssue(token=github_token, client=http_client),
            CommentOnPR(token=github_token, client=http_client),
            DispatchWorkflow(token=github_token, client=http_client),
            ReadWorkflowRun(token=github_token, client=http_client),
        ]
        llm_client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        ryo_agent = RyoAgent(
            persona={"model": model, "system_prompt": bot.system_prompt},
            skills=skills,
            llm_client=llm_client,
            plugin=plugin,
            cooldown_seconds=cooldown_seconds,
            max_iterations=max_iterations,
        )
        await ryo_agent.run(payload)
    finally:
        await http_client.aclose()


def _load_event_payload() -> dict[str, Any]:
    raw_payload = _require_env("EVENT_PAYLOAD")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("EVENT_PAYLOAD must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("EVENT_PAYLOAD must decode to a JSON object.")
    return payload


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


if __name__ == "__main__":
    main()
