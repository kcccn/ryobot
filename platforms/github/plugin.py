from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from core.plugins import (
    BasePlugin,
    HistorySnapshot,
    PluginEvent,
    RepoRuntimeState,
)

from .client import GitHubApiClient
from .utils import max_chars_from_env, sanitize_mentions, truncate_text

_RYO_ANY_MARKER_PATTERN = re.compile(r"<!--\s*ryo:\w+:.*?-->", re.DOTALL)
DEFAULT_MARKER_AUTHOR_LOGINS = frozenset({"github-actions[bot]"})
DEFAULT_MAX_HISTORY_COMMENT_CHARS = 12000
DEFAULT_MAX_HISTORY_TOTAL_CHARS = 18000
DEFAULT_INITIAL_HISTORY_COMMENT_LIMIT = 12
DEFAULT_INITIAL_HISTORY_TOTAL_CHARS = 16000
_COORDINATION_ISSUE_TITLE = "🎙️ RyoBot Coordination"
_COORDINATION_MARKER_PATTERN = re.compile(
    r"<!--\s*ryo:runtime:\s*(?P<payload>\{.*?\})\s*-->",
    re.DOTALL,
)


class GitHubPlugin(BasePlugin):
    """GitHub issue-comment adapter for the plugin port."""

    _MIND_ISSUE_TITLE = "🧠 {name}"

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
        identity: str = "architect",
        display_name: str = "",
    ) -> None:
        self._api = GitHubApiClient(token=token, client=client, api_base_url=api_base_url)
        self._identity = identity
        self._display_name = display_name or identity
        self._marker_author_logins = _marker_author_logins_from_env()
        self._state_pattern = re.compile(
            rf"<!--\s*ryo:{re.escape(identity)}:\s*(?P<payload>\{{.*?\}})\s*-->",
            re.DOTALL,
        )

    def parse_event(self, raw_payload: Any) -> PluginEvent:
        if not isinstance(raw_payload, dict):
            raise ValueError("GitHub event payload must be a dict.")

        repository = raw_payload.get("repository") or {}
        owner = (repository.get("owner") or {}).get("login")
        repo = repository.get("name")
        action = raw_payload.get("action", "")

        if not owner or not repo:
            raise ValueError("GitHub event payload is missing repository owner or name.")

        is_patrol = (
            "schedule" in raw_payload
            or raw_payload.get("_patrol")
            or isinstance(raw_payload.get("inputs"), dict)
        )
        if is_patrol:
            return self._parse_patrol(raw_payload, owner, repo)
        if "comment" in raw_payload and "pull_request" in raw_payload:
            return self._parse_review_comment(raw_payload, owner, repo, action)
        if "comment" in raw_payload:
            return self._parse_issue_comment(raw_payload, owner, repo)
        if "pull_request" in raw_payload:
            return self._parse_pull_request(raw_payload, owner, repo, action)
        if "issue" in raw_payload:
            return self._parse_issue(raw_payload, owner, repo, action)
        raise ValueError("Unrecognized GitHub event payload shape.")

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        runtime_state = await self._load_runtime_state(event.owner, event.repo)
        mind_body, mind_issue_number = await self._load_mind_issue(event.owner, event.repo)
        patrol_brief = ""
        if event.is_patrol:
            patrol_brief = await self._build_patrol_brief(event.owner, event.repo)

        if event.issue_number == 0:
            return HistorySnapshot(
                messages=[],
                subconscious={},
                mind_body=mind_body,
                mind_issue_number=mind_issue_number,
                runtime_state=runtime_state,
                patrol_brief=patrol_brief,
            )

        comments = await self._fetch_thread_comments(
            event.owner,
            event.repo,
            event.issue_number,
            include_review_comments=event.is_pull_request,
        )
        subconscious = self._extract_latest_subconscious(comments)
        partial_messages = self._build_partial_history_messages(comments, event.comment_id)
        return HistorySnapshot(
            messages=partial_messages,
            subconscious=subconscious,
            mind_body=mind_body,
            mind_issue_number=mind_issue_number,
            runtime_state=runtime_state,
            patrol_brief=patrol_brief,
        )

    async def resolve_target_event(self, event: PluginEvent, issue_number: int) -> PluginEvent:
        issue = await self._api.get_json(
            f"/repos/{event.owner}/{event.repo}/issues/{issue_number}"
        )
        is_pull_request = "pull_request" in issue
        title = str(issue.get("title") or "")
        body = str(issue.get("body") or "")
        kind = "PR" if is_pull_request else "Issue"
        message = f"[Street Lurker target {kind} #{issue_number}]\n\n{title}"
        if body:
            message += f"\n\n{body}"
        message = truncate_text(
            message,
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
        )
        return PluginEvent(
            event_id=f"{event.event_id}:target:{issue_number}",
            message=message,
            author=str((issue.get("user") or {}).get("login") or "unknown"),
            author_association=str(issue.get("author_association") or "NONE"),
            issue_id=str(issue.get("id") or issue_number),
            issue_number=issue_number,
            comment_id=0,
            owner=event.owner,
            repo=event.repo,
            is_pull_request=is_pull_request,
            is_patrol=True,
        )

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        if event.issue_number == 0:
            return
        state_blob = json.dumps(subconscious or {}, ensure_ascii=False, separators=(",", ":"))
        body = (
            f"**{self._display_name}**\n\n"
            f"{sanitize_mentions(content)}\n"
            f"<!-- ryo:{self._identity}: {state_blob} -->"
        )
        await self._api.post_json(
            f"/repos/{event.owner}/{event.repo}/issues/{event.issue_number}/comments",
            json_body={"body": body},
        )

    async def update_runtime_state(self, state: RepoRuntimeState) -> RepoRuntimeState:
        issue_number = state.coordination_issue_number
        if issue_number <= 0:
            raise ValueError("Runtime state is missing coordination_issue_number.")
        body = _coordination_issue_template(state)
        await self._api.patch_json(
            f"/repos/{self._current_owner}/{self._current_repo}/issues/{issue_number}",
            json_body={"body": body},
        )
        return state

    async def aclose(self) -> None:
        await self._api.aclose()

    def _parse_issue_comment(self, raw: dict[str, Any], owner: str, repo: str) -> PluginEvent:
        issue = raw.get("issue") or {}
        comment = raw.get("comment") or {}
        issue_number = issue.get("number")
        comment_id = comment.get("id")
        if not all([issue.get("id"), issue_number, comment_id, comment.get("body"), (comment.get("user") or {}).get("login")]):
            raise ValueError("issue_comment payload is missing required fields.")
        issue_title = str(issue.get("title") or "")
        message = f"[Comment on Issue #{issue_number}"
        if issue_title:
            message += f": {issue_title}"
        message += f"]\n\n{comment['body']}"
        return PluginEvent(
            event_id=f"github:{owner}/{repo}:issue:{issue_number}:comment:{comment_id}",
            message=message,
            author=str((comment.get("user") or {})["login"]),
            author_association=str(comment.get("author_association") or "NONE"),
            issue_id=str(issue["id"]),
            issue_number=int(issue_number),
            comment_id=int(comment_id),
            owner=str(owner),
            repo=str(repo),
        )

    def _parse_issue(self, raw: dict[str, Any], owner: str, repo: str, action: str) -> PluginEvent:
        issue = raw.get("issue") or {}
        issue_number = issue.get("number")
        if not all([issue.get("id"), issue_number, issue.get("title"), (issue.get("user") or {}).get("login")]):
            raise ValueError("issues event payload is missing required fields.")
        label = _action_label(action)
        body = issue.get("body") or ""
        message = f"[Issue #{issue_number} {label}]\n\n{issue['title']}"
        if body:
            message += f"\n\n{body}"
        message = truncate_text(
            message,
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
        )
        return PluginEvent(
            event_id=f"github:{owner}/{repo}:issue:{issue_number}:event:{action}",
            message=message,
            author=str((issue.get("user") or {})["login"]),
            author_association=str(issue.get("author_association") or "NONE"),
            issue_id=str(issue["id"]),
            issue_number=int(issue_number),
            comment_id=0,
            owner=str(owner),
            repo=str(repo),
        )

    def _parse_pull_request(self, raw: dict[str, Any], owner: str, repo: str, action: str) -> PluginEvent:
        pr_ = raw.get("pull_request") or {}
        pr_number = pr_.get("number")
        if not all([pr_.get("id"), pr_number, pr_.get("title"), (pr_.get("user") or {}).get("login")]):
            raise ValueError("pull_request event payload is missing required fields.")
        label = _action_label(action)
        body = pr_.get("body") or ""
        message = f"[PR #{pr_number} {label}]\n\n{pr_['title']}"
        if body:
            message += f"\n\n{body}"
        message = truncate_text(
            message,
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
        )
        return PluginEvent(
            event_id=f"github:{owner}/{repo}:pr:{pr_number}:event:{action}",
            message=message,
            author=str((pr_.get("user") or {})["login"]),
            author_association=str(pr_.get("author_association") or "NONE"),
            issue_id=str(pr_["id"]),
            issue_number=int(pr_number),
            comment_id=0,
            owner=str(owner),
            repo=str(repo),
            is_pull_request=True,
        )

    def _parse_review_comment(self, raw: dict[str, Any], owner: str, repo: str, action: str) -> PluginEvent:
        pr_ = raw.get("pull_request") or {}
        comment = raw.get("comment") or {}
        pr_number = pr_.get("number")
        comment_id = comment.get("id")
        if not all([pr_.get("id"), pr_number, comment_id, comment.get("body"), (comment.get("user") or {}).get("login")]):
            raise ValueError("pull_request_review_comment payload is missing required fields.")
        return PluginEvent(
            event_id=f"github:{owner}/{repo}:pr:{pr_number}:comment:{comment_id}",
            message=str(comment["body"]),
            author=str((comment.get("user") or {})["login"]),
            author_association=str(comment.get("author_association") or "NONE"),
            issue_id=str(pr_["id"]),
            issue_number=int(pr_number),
            comment_id=int(comment_id),
            owner=str(owner),
            repo=str(repo),
            is_pull_request=True,
        )

    def _parse_patrol(self, raw: dict[str, Any], owner: str, repo: str) -> PluginEvent:
        inputs = raw.get("inputs") or {}
        if isinstance(inputs, dict):
            issue_number = int(inputs.get("issue_number", "0") or "0")
            if issue_number > 0:
                dispatcher = inputs.get("dispatcher", "system")
                return PluginEvent(
                    event_id=f"github:{owner}/{repo}:workflow_dispatch:issue:{issue_number}",
                    message=f"[Street Lurker dispatch from {dispatcher}: check issue #{issue_number}]",
                    author="system",
                    author_association="OWNER",
                    issue_id="",
                    issue_number=0,
                    comment_id=0,
                    owner=str(owner),
                    repo=str(repo),
                    is_patrol=True,
                )
        return PluginEvent(
            event_id=f"github:{owner}/{repo}:schedule:{datetime.now(timezone.utc).isoformat()}",
            message=(
                "Street lurker mode: inspect the repo's last 24 hours, decide whether anything is worth "
                "doing or talking about, and if so pick exactly one issue/PR or act directly."
            ),
            author="system",
            author_association="OWNER",
            issue_id="",
            issue_number=0,
            comment_id=0,
            owner=str(owner),
            repo=str(repo),
            is_patrol=True,
        )

    async def _load_mind_issue(self, owner: str, repo: str) -> tuple[str, int]:
        try:
            return await self._find_or_create_mind_issue(owner, repo)
        except Exception:
            return "", 0

    async def _find_or_create_mind_issue(self, owner: str, repo: str) -> tuple[str, int]:
        title = self._MIND_ISSUE_TITLE.format(name=self._display_name)
        search_result = await self._api.get_json(
            "/search/issues",
            params={"q": f'repo:{owner}/{repo} is:issue is:open "{title}" in:title', "per_page": 1},
        )
        items = (search_result.get("items") or []) if isinstance(search_result, dict) else []
        if items:
            number = int(items[0].get("number", 0))
            body = str(items[0].get("body") or "")
            return body, number
        body = _mind_issue_template(self._display_name, self._identity)
        result = await self._api.post_json(
            f"/repos/{owner}/{repo}/issues",
            json_body={"title": title, "body": body},
        )
        return str(result.get("body") or body), int(result.get("number", 0))

    async def _load_runtime_state(self, owner: str, repo: str) -> RepoRuntimeState:
        self._current_owner = owner
        self._current_repo = repo
        issue_number, body = await self._find_or_create_coordination_issue(owner, repo)
        state = RepoRuntimeState(coordination_issue_number=issue_number)
        match = _COORDINATION_MARKER_PATTERN.search(body)
        if match:
            try:
                state = RepoRuntimeState.model_validate_json(match.group("payload"))
            except ValidationError:
                state = RepoRuntimeState(coordination_issue_number=issue_number)
        state.coordination_issue_number = issue_number
        return state

    async def _find_or_create_coordination_issue(self, owner: str, repo: str) -> tuple[int, str]:
        search_result = await self._api.get_json(
            "/search/issues",
            params={"q": f'repo:{owner}/{repo} is:issue is:open "{_COORDINATION_ISSUE_TITLE}" in:title', "per_page": 1},
        )
        items = (search_result.get("items") or []) if isinstance(search_result, dict) else []
        if items:
            return int(items[0].get("number", 0)), str(items[0].get("body") or "")
        state = RepoRuntimeState(
            next_patrol_after=datetime.now(timezone.utc).isoformat(),
            bot_fatigue={},
            coordination_issue_number=0,
        )
        body = _coordination_issue_template(state)
        result = await self._api.post_json(
            f"/repos/{owner}/{repo}/issues",
            json_body={"title": _COORDINATION_ISSUE_TITLE, "body": body},
        )
        if isinstance(result, dict):
            return int(result.get("number", 0)), str(result.get("body") or body)
        return 0, body

    async def _build_patrol_brief(self, owner: str, repo: str) -> str:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        issues = await self._api.get_json(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "open", "sort": "updated", "direction": "asc", "per_page": 10},
        )
        pulls = await self._api.get_json(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "sort": "updated", "direction": "asc", "per_page": 10},
        )
        recent_closed = await self._api.get_json(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "closed", "sort": "updated", "direction": "desc", "per_page": 20},
        )
        merged_recent = [
            pr for pr in recent_closed
            if pr.get("merged_at") and _iso_at_or_after(str(pr.get("merged_at")), since)
        ]
        lines: list[str] = ["1. Open issues nobody touched recently:"]
        issue_count = 0
        for item in issues:
            if "pull_request" in item:
                continue
            issue_count += 1
            if issue_count > 5:
                break
            lines.append(
                f"- Issue #{item['number']}: {item['title']} updated={item.get('updated_at', '')}"
            )
        if issue_count == 0:
            lines.append("- None")
        lines.append("2. Long-stalled open PRs:")
        if pulls:
            for pr in pulls[:5]:
                lines.append(
                    f"- PR #{pr['number']}: {pr['title']} updated={pr.get('updated_at', '')}"
                )
        else:
            lines.append("- None")
        lines.append("3. PRs merged in the last 24 hours:")
        if merged_recent:
            for pr in merged_recent[:5]:
                lines.append(
                    f"- PR #{pr['number']}: {pr['title']} merged_at={pr.get('merged_at', '')}"
                )
        else:
            lines.append("- None")
        return "\n".join(lines)

    async def _fetch_thread_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        include_review_comments: bool,
    ) -> list[dict[str, Any]]:
        comments = await self._fetch_paginated(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100, "sort": "created", "direction": "asc"},
        )
        if include_review_comments:
            review_comments = await self._fetch_paginated(
                f"/repos/{owner}/{repo}/pulls/{issue_number}/comments",
                params={"per_page": 100, "sort": "created", "direction": "asc"},
            )
            comments = [*comments, *review_comments]
        return sorted(comments, key=_comment_sort_key)

    def _extract_latest_subconscious(self, comments: list[dict[str, Any]]) -> dict[str, Any]:
        subconscious: dict[str, Any] = {}
        for comment in comments:
            body = str(comment.get("body") or "")
            match = self._state_pattern.search(body)
            if match and self._is_trusted_marker_comment(comment):
                try:
                    subconscious = json.loads(match.group("payload"))
                except json.JSONDecodeError:
                    continue
        return subconscious

    def _build_partial_history_messages(
        self,
        comments: list[dict[str, Any]],
        trigger_comment_id: int,
    ) -> list[dict[str, str]]:
        filtered = [item for item in comments if int(item.get("id", 0)) != trigger_comment_id]
        limit = max_chars_from_env(
            "RYOBOT_INITIAL_HISTORY_COMMENT_LIMIT",
            DEFAULT_INITIAL_HISTORY_COMMENT_LIMIT,
        )
        if limit > 0:
            filtered = filtered[-limit:]
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are only seeing a recent slice of the thread. If context feels incomplete, "
                    "use read_thread_comments, search_issues, search_repo_memory, search_code, "
                    "read_file, or read_code_diff before deciding."
                ),
            }
        ]
        for comment in filtered:
            body = str(comment.get("body") or "")
            clean_body = _RYO_ANY_MARKER_PATTERN.sub("", body).strip()
            if not clean_body:
                continue
            if _RYO_ANY_MARKER_PATTERN.search(body) and self._is_trusted_marker_comment(comment):
                messages.append({"role": "assistant", "content": clean_body})
            else:
                messages.append({"role": "user", "content": clean_body})
        return _fit_messages_to_initial_budget(messages)

    def _is_trusted_marker_comment(self, comment: dict[str, Any]) -> bool:
        login = str((comment.get("user") or {}).get("login") or "")
        return login in self._marker_author_logins

    async def _fetch_paginated(self, path: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            page_params = dict(params)
            page_params["page"] = page
            items = await self._api.get_json(path, params=page_params)
            if not isinstance(items, list) or not items:
                return results
            results.extend(items)
            if len(items) < int(page_params.get("per_page", 100)):
                return results
            page += 1


_ACTION_LABELS: dict[str, str] = {
    "opened": "opened",
    "edited": "edited",
    "synchronize": "synchronized",
    "created": "created",
}


def _action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


def _marker_author_logins_from_env() -> frozenset[str]:
    raw = os.getenv("RYOBOT_MARKER_AUTHOR_LOGINS")
    if not raw:
        return DEFAULT_MARKER_AUTHOR_LOGINS
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return frozenset(values) if values else DEFAULT_MARKER_AUTHOR_LOGINS


def _fit_messages_to_history_budget(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    max_chars = max_chars_from_env("RYOBOT_MAX_HISTORY_TOTAL_CHARS", DEFAULT_MAX_HISTORY_TOTAL_CHARS)
    if max_chars <= 0:
        return messages
    kept: list[dict[str, str]] = []
    total_chars = 0
    for message in reversed(messages):
        content = str(message.get("content") or "")
        message_chars = len(content)
        if kept and total_chars + message_chars > max_chars:
            continue
        kept.append(message)
        total_chars += message_chars
    kept.reverse()
    return kept


def _fit_messages_to_initial_budget(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    max_chars = max_chars_from_env(
        "RYOBOT_INITIAL_HISTORY_TOTAL_CHARS",
        DEFAULT_INITIAL_HISTORY_TOTAL_CHARS,
    )
    if max_chars <= 0:
        return messages
    kept: list[dict[str, str]] = []
    total_chars = 0
    omitted = 0
    for message in reversed(messages):
        content = str(message.get("content") or "")
        message_chars = len(content)
        if kept and total_chars + message_chars > max_chars:
            omitted += 1
            continue
        kept.append(message)
        total_chars += message_chars
    kept.reverse()
    if omitted:
        kept.insert(
            1,
            {
                "role": "system",
                "content": f"[partial context: {omitted} older comments omitted from initial load]",
            },
        )
    return kept


def _comment_sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    created_at = str(comment.get("created_at") or "")
    try:
        comment_id = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        comment_id = 0
    return (created_at, comment_id)


def _mind_issue_template(display_name: str, identity: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# 🧠 {display_name}\n\n"
        f"> I am **{display_name}** (`{identity}`), a member of the Ryo Bot Society.\n"
        f"> This issue is my persistent memory. I read it at the start of every run.\n\n"
        f"## Long-term Memory\n\n"
        f"(empty)\n\n"
        f"## Active Context\n\n"
        f"(empty)\n\n"
        f"## Recent Activity\n\n"
        f"- Mind issue created ({ts})\n"
    )


def _coordination_issue_template(state: RepoRuntimeState) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state_blob = json.dumps(state.model_dump(), ensure_ascii=False, separators=(",", ":"))
    fatigue_lines = []
    for identity, fatigue in sorted(state.bot_fatigue.items()):
        fatigue_lines.append(
            f"- `{identity}` last_spoke_at={fatigue.last_spoke_at or 'never'} "
            f"next_available_at={fatigue.next_available_at or 'now'}"
        )
    if not fatigue_lines:
        fatigue_lines.append("- none yet")
    return (
        "# 🎙️ RyoBot Coordination\n\n"
        "This issue stores repo-wide runtime state for the single-engine social simulation.\n\n"
        f"- next_patrol_after: {state.next_patrol_after or 'immediately'} (street-lurker gate)\n"
        f"- last_route: {state.last_routing.bot_identity or 'n/a'} / {state.last_routing.reason or 'n/a'}\n\n"
        "## Bot Fatigue\n\n"
        + "\n".join(fatigue_lines)
        + f"\n\n<!-- ryo:runtime: {state_blob} -->\n\n"
        + f"Updated: {ts}\n"
    )


def _iso_at_or_after(value: str, threshold: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed >= threshold
