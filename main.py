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
    ArchiveMemory,
    CloseIssue,
    CommentOnPR,
    CommentOnThread,
    CommitMemory,
    CreateBranch,
    CreateIssue,
    CreatePRReview,
    CreatePullRequest,
    DeleteBranch,
    DispatchWorkflow,
    FindFilePaths,
    GetProjectTree,
    GitHubPlugin,
    ListFiles,
    ListOpenIssues,
    ListOpenPullRequests,
    ListRepoLabels,
    MergePullRequest,
    ReadCodeDiff,
    ReadFile,
    ReadIssueBody,
    ReadIssueMemory,
    ReadThreadComments,
    ReadThreadContext,
    ReadThreadMeta,
    ReadWorkflowRun,
    RefineMemory,
    ReopenIssue,
    RetrieveMemory,
    RunCommand,
    SearchCode,
    SearchIssues,
    SearchRepoContext,
    SearchRepoMemory,
    SearchSymbol,
    UpdateIssue,
    WriteFile,
)
from platforms.llm import AnthropicAdapter

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MARKER_AUTHOR_LOGINS = frozenset({"github-actions[bot]"})
DEFAULT_MOTIVATION_THRESHOLD = 70
DEFAULT_FATIGUE_MIN_SECONDS = 480
DEFAULT_FATIGUE_MAX_SECONDS = 720
DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS = 60
DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS = 180
_TRUSTED_AUTHOR_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def main() -> None:
    try:
        payload = _load_event_payload()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    payload = _ensure_repository_context(payload)
    bot_identity = _selected_bot_identity(payload)
    _log_event(payload, bot_identity)

    if _contains_own_marker(payload, bot_identity):
        print(f"SKIP: event already contains {bot_identity} marker", file=sys.stderr)
        raise SystemExit(0)

    try:
        github_token = _require_env("GITHUB_TOKEN")
        bot = get_bot(bot_identity)
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
    config = _load_config(bot)
    all_bots = list_bots()
    persona_registry = _build_persona_registry(
        all_bots=all_bots,
        bot=bot,
        model=config["model"],
        payload=payload,
    )

    http_client = httpx.AsyncClient(
        base_url="https://api.github.com",
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    try:
        plugin = GitHubPlugin(
            token=github_token,
            client=http_client,
            identity=bot.identity,
            display_name=bot.display_name,
        )
        all_skills = _build_github_skills(github_token, http_client)
        allow = bot.skill_filter
        skills = [s for s in all_skills if allow is None or s.name in allow]
        llm_client: Any
        if bot.provider == "anthropic":
            llm_client = AnthropicAdapter(api_key=api_key, base_url=config["base_url"])
        else:
            llm_client = AsyncOpenAI(api_key=api_key, base_url=config["base_url"])
        ryo_agent = RyoAgent(
            persona={**persona_registry[bot.identity]},
            persona_registry=persona_registry,
            skills=skills,
            llm_client=llm_client,
            plugin=plugin,
            max_iterations=config["max_iterations"],
            max_tokens=bot.max_tokens,
            motivation_threshold=config["motivation_threshold"],
            fatigue_min_seconds=config["fatigue_min_seconds"],
            fatigue_max_seconds=config["fatigue_max_seconds"],
            street_lurker_fatigue_min_seconds=config["street_lurker_fatigue_min_seconds"],
            street_lurker_fatigue_max_seconds=config["street_lurker_fatigue_max_seconds"],
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


def _ensure_repository_context(payload: dict[str, Any]) -> dict[str, Any]:
    if "repository" in payload:
        return payload
    if "schedule" in payload or isinstance(payload.get("inputs"), dict):
        repo_full = os.getenv("GITHUB_REPOSITORY", "")
        if "/" not in repo_full:
            raise ValueError("Skipping schedule/workflow_dispatch event: GITHUB_REPOSITORY not set.")
        owner, repo = repo_full.split("/", 1)
        payload = dict(payload)
        payload["repository"] = {"owner": {"login": owner}, "name": repo}
        payload["_patrol"] = True
        return payload
    raise ValueError("Skipping event without repository context.")


def _selected_bot_identity(payload: dict[str, Any]) -> str:
    explicit_identity = os.getenv("BOT_IDENTITY", "").strip()
    if explicit_identity:
        return explicit_identity
    identities = [bot.identity for bot in list_bots()]
    weights = _bot_activity_weights(identities)
    return random.choices(identities, weights=weights, k=1)[0]


def _bot_activity_weights(identities: list[str]) -> list[float]:
    configured = os.getenv("RYOBOT_BOT_ACTIVITY_WEIGHTS", "").strip()
    if not configured:
        return [1.0] * len(identities)
    parsed: dict[str, float] = {}
    try:
        if configured.startswith("{"):
            loaded = json.loads(configured)
            if isinstance(loaded, dict):
                for key, value in loaded.items():
                    parsed[str(key)] = max(float(value), 0.0)
        else:
            for chunk in configured.split(","):
                if "=" not in chunk:
                    continue
                key, raw_value = chunk.split("=", 1)
                parsed[key.strip()] = max(float(raw_value.strip()), 0.0)
    except (ValueError, json.JSONDecodeError):
        return [1.0] * len(identities)
    weights = [parsed.get(identity, 1.0) for identity in identities]
    return weights if any(weight > 0 for weight in weights) else [1.0] * len(identities)


def _log_event(payload: dict[str, Any], identity: str) -> None:
    kind = (
        "schedule" if "schedule" in payload
        else "workflow_dispatch" if isinstance(payload.get("inputs"), dict)
        else "issue_comment" if "comment" in payload
        else "issues" if "issue" in payload
        else "pull_request" if "pull_request" in payload
        else "unknown"
    )
    number = ""
    if "issue" in payload:
        number = f" #{(payload['issue'] or {}).get('number', '?')}"
    elif "pull_request" in payload:
        number = f" #{(payload['pull_request'] or {}).get('number', '?')}"
    print(f"[main] event={kind}{number} bot={identity}", file=sys.stderr)


def _contains_own_marker(payload: dict[str, Any], identity: str) -> bool:
    marker = f"<!-- ryo:{identity}:"
    trusted_marker_authors = _marker_author_logins_from_env()
    body_sources: list[tuple[str, str]] = []
    if "comment" in payload:
        comment = payload.get("comment") or {}
        body_sources.append((str(comment.get("body") or ""), str((comment.get("user") or {}).get("login") or "")))
    if "issue" in payload:
        issue = payload.get("issue") or {}
        body_sources.append((str(issue.get("body") or ""), str((issue.get("user") or {}).get("login") or "")))
    if "pull_request" in payload:
        pull_request = payload.get("pull_request") or {}
        body_sources.append((str(pull_request.get("body") or ""), str((pull_request.get("user") or {}).get("login") or "")))
    return any(marker in body and login in trusted_marker_authors for body, login in body_sources)


def _detect_fix_command(payload: dict[str, Any]) -> bool:
    bodies: list[tuple[str, str]] = []
    if "comment" in payload:
        comment = payload.get("comment") or {}
        bodies.append((str(comment.get("body") or ""), str(comment.get("author_association") or "")))
    if "issue" in payload:
        issue = payload.get("issue") or {}
        bodies.append((str(issue.get("body") or ""), str(issue.get("author_association") or "")))
    return any(
        re.search(r"(?<!\w)/fix\b", body, re.IGNORECASE)
        and author_assoc in _TRUSTED_AUTHOR_ASSOCIATIONS
        for body, author_assoc in bodies
    )


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


def _load_config(bot: Any) -> dict[str, Any]:
    base_url = os.getenv("LLM_BASE_URL") or bot.base_url or DEEPSEEK_BASE_URL
    model = os.getenv("LLM_MODEL") or bot.model or DEFAULT_MODEL
    return {
        "base_url": base_url,
        "model": model,
        "max_iterations": int(os.getenv("MAX_ITERATIONS", "100")),
        "motivation_threshold": int(os.getenv("RYOBOT_MOTIVATION_THRESHOLD", str(DEFAULT_MOTIVATION_THRESHOLD))),
        "fatigue_min_seconds": int(os.getenv("RYOBOT_FATIGUE_MIN_SECONDS", str(DEFAULT_FATIGUE_MIN_SECONDS))),
        "fatigue_max_seconds": int(os.getenv("RYOBOT_FATIGUE_MAX_SECONDS", str(DEFAULT_FATIGUE_MAX_SECONDS))),
        "street_lurker_fatigue_min_seconds": int(
            os.getenv(
                "RYOBOT_STREET_LURKER_FATIGUE_MIN_SECONDS",
                str(DEFAULT_STREET_LURKER_FATIGUE_MIN_SECONDS),
            )
        ),
        "street_lurker_fatigue_max_seconds": int(
            os.getenv(
                "RYOBOT_STREET_LURKER_FATIGUE_MAX_SECONDS",
                str(DEFAULT_STREET_LURKER_FATIGUE_MAX_SECONDS),
            )
        ),
    }


def _build_persona_registry(
    *,
    all_bots: list[Any],
    bot: Any,
    model: str,
    payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    roster_lines = [f"- {b.display_name}（{b.identity}）：{b.description}" for b in all_bots]
    roster = "当前 Bot 社会成员：\n" + "\n".join(roster_lines)
    fix_signal = ""
    if _detect_fix_command(payload):
        fix_signal = (
            "\n\n补充信号：可信维护者触发了 /fix。"
            "这会显著提高你对直接实现或推动修复的意愿，但不会跳过意愿评估、全局麦克风、或疲劳机制。"
        )
    return {
        b.identity: {
            "identity": b.identity,
            "display_name": b.display_name,
            "model": model,
            "system_prompt": f"{roster}\n\n{b.system_prompt}{fix_signal}",
        }
        for b in all_bots
    }


def _build_github_skills(token: str, client: httpx.AsyncClient) -> list[Any]:
    skills: list[Any] = [
        ReadThreadContext(token=token, client=client),
        ReadIssueMemory(token=token, client=client),
        ReadIssueBody(token=token, client=client),
        SearchRepoMemory(token=token, client=client),
        CommitMemory(token=token, client=client),
        RetrieveMemory(token=token, client=client),
        RefineMemory(token=token, client=client),
        ArchiveMemory(token=token, client=client),
        SearchRepoContext(token=token, client=client),
        ListOpenIssues(token=token, client=client),
        ListOpenPullRequests(token=token, client=client),
        ListRepoLabels(token=token, client=client),
        MergePullRequest(token=token, client=client),
        ReadThreadComments(token=token, client=client),
        GetProjectTree(token=token, client=client),
        FindFilePaths(token=token, client=client),
        SearchSymbol(token=token, client=client),
        ListFiles(token=token, client=client),
        ReadFile(token=token, client=client),
        SearchCode(token=token, client=client),
        ReadCodeDiff(token=token, client=client),
        ReadThreadMeta(token=token, client=client),
        CreateIssue(token=token, client=client),
        WriteFile(token=token, client=client),
        CreateBranch(token=token, client=client),
        DeleteBranch(token=token, client=client),
        CreatePullRequest(token=token, client=client),
        CreatePRReview(token=token, client=client),
        AddLabels(token=token, client=client),
        CloseIssue(token=token, client=client),
        ReopenIssue(token=token, client=client),
        CommentOnThread(token=token, client=client),
        CommentOnPR(token=token, client=client),
        ReadWorkflowRun(token=token, client=client),
        RunCommand(token=token, client=client),
        SearchIssues(token=token, client=client),
        UpdateIssue(token=token, client=client),
    ]
    if _workflow_dispatch_enabled():
        skills.append(DispatchWorkflow(token=token, client=client))
    return skills


if __name__ == "__main__":
    main()
