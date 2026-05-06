from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from openai import AsyncOpenAI

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
DEFAULT_MODEL = "deepseek-chat"
PERSONA = {
    "system_prompt": (
        "你是一个严厉且幽默的顶级架构师。"
        "你直接、专业、苛刻，不容忍糟糕抽象、重复劳动和含糊表述。"
        "你会给出清晰可执行的工程建议，同时保留一点冷幽默。"
    ),
}


def main() -> None:
    try:
        payload = _load_event_payload()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    sender_type = str((payload.get("sender") or {}).get("type") or "")
    if sender_type == "Bot":
        raise SystemExit(0)

    try:
        github_token = _require_env("GITHUB_TOKEN")
        deepseek_api_key = _require_env("DEEPSEEK_API_KEY")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        asyncio.run(_run(payload, github_token=github_token, deepseek_api_key=deepseek_api_key))
    except Exception as exc:
        print(f"Fatal entrypoint error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _run(
    payload: dict[str, Any],
    *,
    github_token: str,
    deepseek_api_key: str,
) -> None:
    base_url = os.getenv("LLM_BASE_URL", DEEPSEEK_BASE_URL)
    model = os.getenv("LLM_MODEL", DEFAULT_MODEL)
    http_client = httpx.AsyncClient(base_url="https://api.github.com")
    try:
        plugin = GitHubPlugin(token=github_token, client=http_client)
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
            api_key=deepseek_api_key,
            base_url=base_url,
        )
        ryo_agent = RyoAgent(
            persona={"model": model, **PERSONA},
            skills=skills,
            llm_client=llm_client,
            plugin=plugin,
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
