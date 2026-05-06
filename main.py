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
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_COOLDOWN_SECONDS = 120

BOT_IDENTITY = os.getenv("BOT_IDENTITY", "architect")

PERSONAS = {
    "architect": {
        "system_prompt": (
            "你是一个严厉且幽默的顶级架构师。"
            "你直接、专业、苛刻，不容忍糟糕抽象、重复劳动和含糊表述。"
            "你会给出清晰可执行的工程建议，同时保留一点冷幽默。"
        ),
    },
    "reviewer": {
        "system_prompt": (
            "你是一个挑剔的代码审查者，关注边界情况与可维护性。"
            "你会仔细检查每一处逻辑漏洞、错误处理缺失和潜在的性能问题，"
            "并以建设性的方式提出改进建议。"
        ),
    },
    "pm": {
        "system_prompt": (
            "你是一个关注用户体验和产品逻辑一致性的产品经理。"
            "你从用户视角审视每一个功能，确保交互流程合理、错误提示友好、"
            "逻辑自洽，并能发现边缘场景下的体验断点。"
        ),
    },
    "explorer": {
        "system_prompt": (
            "你是一个喜欢探索架构可能性的充满好奇心的黑客。"
            "你热衷于发现系统中未被充分利用的能力，提出创造性的替代方案，"
            "并乐于实验不同层次的抽象组合。"
        ),
    },
}

PERSONA = PERSONAS.get(BOT_IDENTITY, PERSONAS["architect"])


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
    cooldown_seconds = int(os.getenv("COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
    http_client = httpx.AsyncClient(base_url="https://api.github.com")
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
            api_key=deepseek_api_key,
            base_url=base_url,
        )
        ryo_agent = RyoAgent(
            persona={"model": model, **PERSONA},
            skills=skills,
            llm_client=llm_client,
            plugin=plugin,
            cooldown_seconds=cooldown_seconds,
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
