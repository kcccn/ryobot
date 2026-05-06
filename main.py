from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
from typing import Any

import httpx
from openai import AsyncOpenAI

from bots import get_bot, list_bots
from core import RyoAgent
from platforms.github import (
    AddLabels,
    CloseIssue,
    CommentOnPR,
    CreateBranch,
    CreateIssue,
    CreatePullRequest,
    DispatchWorkflow,
    GitHubPlugin,
    ListFiles,
    ListOpenIssues,
    ListRepoLabels,
    ReadCodeDiff,
    ReadFile,
    ReadIssueMemory,
    ReadThreadComments,
    ReadWorkflowRun,
    RunCommand,
    SearchCode,
    SearchRepoMemory,
    WriteFile,
)
from platforms.llm import AnthropicAdapter

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_MARKER_AUTHOR_LOGINS = frozenset({"github-actions[bot]"})

BOT_IDENTITY = os.getenv("BOT_IDENTITY", "architect")


def _contains_own_marker(payload: dict[str, Any], identity: str) -> bool:
    """Return True if any body field in the payload contains this bot's marker."""
    marker = f"<!-- ryo:{identity}:"
    trusted_marker_authors = _marker_author_logins_from_env()
    body_sources: list[tuple[str, str]] = []
    if "comment" in payload:
        comment = payload.get("comment") or {}
        body_sources.append((
            str(comment.get("body") or ""),
            str((comment.get("user") or {}).get("login") or ""),
        ))
    if "issue" in payload:
        issue = payload.get("issue") or {}
        body_sources.append((
            str(issue.get("body") or ""),
            str((issue.get("user") or {}).get("login") or ""),
        ))
    if "pull_request" in payload:
        pull_request = payload.get("pull_request") or {}
        body_sources.append((
            str(pull_request.get("body") or ""),
            str((pull_request.get("user") or {}).get("login") or ""),
        ))
    return any(marker in body and login in trusted_marker_authors for body, login in body_sources)


_TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _detect_fix_command(payload: dict[str, Any]) -> bool:
    """Return True if a trusted author issued the /fix command."""
    bodies: list[tuple[str, str]] = []
    if "comment" in payload:
        comment = payload.get("comment") or {}
        bodies.append((
            str(comment.get("body") or ""),
            str(comment.get("author_association") or ""),
        ))
    if "issue" in payload:
        issue = payload.get("issue") or {}
        bodies.append((
            str(issue.get("body") or ""),
            str(issue.get("author_association") or ""),
        ))
    return any(
        re.search(r"(?<!\w)/fix\b", body, re.IGNORECASE)
        and author_assoc in _TRUSTED_AUTHOR_ASSOCIATIONS
        for body, author_assoc in bodies
    )


def main() -> None:
    try:
        payload = _load_event_payload()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    if _contains_own_marker(payload, BOT_IDENTITY):
        raise SystemExit(0)

    if "repository" not in payload:
        if "schedule" in payload:
            repo_full = os.getenv("GITHUB_REPOSITORY", "")
            if "/" not in repo_full:
                print("Skipping schedule event: GITHUB_REPOSITORY not set.", file=sys.stderr)
                raise SystemExit(0)
            owner, repo = repo_full.split("/", 1)
            payload["repository"] = {"owner": {"login": owner}, "name": repo}
            payload["_patrol"] = True
        else:
            print("Skipping event without repository context.", file=sys.stderr)
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
        asyncio.run(_run(payload, github_token=github_token, api_key=api_key, bot=bot))
    except Exception as exc:
        print(f"Fatal entrypoint error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _run(
    payload: dict[str, Any],
    *,
    github_token: str,
    api_key: str,
    bot: Any,
) -> None:
    base_url = os.getenv("LLM_BASE_URL") or bot.base_url or DEEPSEEK_BASE_URL
    model = os.getenv("LLM_MODEL") or bot.model or DEFAULT_MODEL
    cooldown_seconds = int(os.getenv("COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
    max_iterations = int(os.getenv("MAX_ITERATIONS", "100"))
    roster_lines = [
        f"- {b.display_name}（{b.identity}）：{b.description}"
        for b in list_bots()
    ]
    roster = "当前 Bot 社会成员：\n" + "\n".join(roster_lines)
    system_prompt = f"{roster}\n\n{bot.system_prompt}"
    if _detect_fix_command(payload):
        system_prompt = (
            "/FIX MODE ACTIVE: 可信维护者发出了 /fix 命令。"
            "你必须立即切换到实现模式。不要讨论、不要分析、不要提问。"
            "直接读取 Issue → 读代码 → 写修复 → 创建分支 → 提交 PR。"
            "如果有不确定的实现细节，按最佳判断进行，在 PR 描述中记录假设。"
            "此指令覆盖你正常的讨论/巡逻行为。\n\n"
            + system_prompt
        )
    http_client = httpx.AsyncClient(
        base_url="https://api.github.com",
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    try:
        plugin = GitHubPlugin(token=github_token, client=http_client, identity=BOT_IDENTITY, display_name=bot.display_name)
        all_skills = [
            ReadIssueMemory(token=github_token, client=http_client),
            SearchRepoMemory(token=github_token, client=http_client),
            ListOpenIssues(token=github_token, client=http_client),
            ListRepoLabels(token=github_token, client=http_client),
            ReadThreadComments(token=github_token, client=http_client),
            ListFiles(token=github_token, client=http_client),
            ReadFile(token=github_token, client=http_client),
            SearchCode(token=github_token, client=http_client),
            ReadCodeDiff(token=github_token, client=http_client),
            CreateIssue(token=github_token, client=http_client),
            WriteFile(token=github_token, client=http_client),
            CreateBranch(token=github_token, client=http_client),
            CreatePullRequest(token=github_token, client=http_client),
            AddLabels(token=github_token, client=http_client),
            CloseIssue(token=github_token, client=http_client),
            CommentOnPR(token=github_token, client=http_client),
            ReadWorkflowRun(token=github_token, client=http_client),
            RunCommand(token=github_token, client=http_client),
        ]
        if _workflow_dispatch_enabled():
            all_skills.append(DispatchWorkflow(token=github_token, client=http_client))
        allow = bot.skill_filter
        skills = [s for s in all_skills if allow is None or s.name in allow]
        llm_client: Any
        if bot.provider == "anthropic":
            llm_client = AnthropicAdapter(api_key=api_key, base_url=base_url)
        else:
            llm_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
            )
        ryo_agent = RyoAgent(
            persona={"model": model, "system_prompt": system_prompt},
            skills=skills,
            llm_client=llm_client,
            plugin=plugin,
            cooldown_seconds=cooldown_seconds,
            max_iterations=max_iterations,
            max_tokens=bot.max_tokens,
        )
        is_fix = _detect_fix_command(payload)
        is_patrol = (
            payload.get("_patrol", False)
            or "schedule" in payload
            or isinstance(payload.get("inputs"), dict)
        )
        if not is_fix and not is_patrol:
            if random.random() > bot.response_probability:
                return
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


def _marker_author_logins_from_env() -> frozenset[str]:
    raw = os.getenv("RYOBOT_MARKER_AUTHOR_LOGINS")
    if not raw:
        return DEFAULT_MARKER_AUTHOR_LOGINS
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return frozenset(values) if values else DEFAULT_MARKER_AUTHOR_LOGINS


def _workflow_dispatch_enabled() -> bool:
    return bool(os.getenv("RYOBOT_ALLOWED_WORKFLOWS", "").strip())


if __name__ == "__main__":
    main()
