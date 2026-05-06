from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from core.plugins import BasePlugin, HistorySnapshot, PluginEvent

from .client import GitHubApiClient

_RYO_ANY_MARKER_PATTERN = re.compile(
    r"<!--\s*ryo:\w+:.*?-->",
    re.DOTALL,
)
DEFAULT_MARKER_AUTHOR_LOGINS = frozenset({"github-actions[bot]"})
DEFAULT_MAX_HISTORY_COMMENT_CHARS = 12000


class GitHubPlugin(BasePlugin):
    """GitHub issue-comment adapter for the plugin port."""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
        identity: str = "architect",
    ) -> None:
        self._api = GitHubApiClient(token=token, client=client, api_base_url=api_base_url)
        self._identity = identity
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

        # pull_request_review_comment: has both "comment" and "pull_request"
        if "comment" in raw_payload and "pull_request" in raw_payload:
            return self._parse_review_comment(raw_payload, owner, repo, action)

        # issue_comment: has "comment" and "issue" but no "pull_request"
        if "comment" in raw_payload:
            return self._parse_issue_comment(raw_payload, owner, repo)

        # pull_request event: has "pull_request" but no "comment"
        if "pull_request" in raw_payload:
            return self._parse_pull_request(raw_payload, owner, repo, action)

        # issues event
        if "issue" in raw_payload:
            return self._parse_issue(raw_payload, owner, repo, action)

        raise ValueError("Unrecognized GitHub event payload shape.")

    def _parse_issue_comment(self, raw: dict[str, Any], owner: str, repo: str) -> PluginEvent:
        issue = raw.get("issue") or {}
        comment = raw.get("comment") or {}
        issue_number = issue.get("number")
        comment_id = comment.get("id")

        if not all([issue.get("id"), issue_number, comment_id, comment.get("body"), (comment.get("user") or {}).get("login")]):
            raise ValueError("issue_comment payload is missing required fields.")

        return PluginEvent(
            event_id=f"github:{owner}/{repo}:issue:{issue_number}:comment:{comment_id}",
            message=str(comment["body"]),
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
        message = _truncate_text(
            message,
            _max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
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
        message = _truncate_text(
            message,
            _max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
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
        )

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        comments = await self._api.get_json(
            f"/repos/{event.owner}/{event.repo}/issues/{event.issue_number}/comments",
            params={"per_page": 10, "sort": "created", "direction": "desc"},
        )
        messages: list[dict[str, str]] = []
        subconscious: dict[str, Any] = {}
        last_bot_comment_at: str | None = None

        for comment in reversed(comments):
            if int(comment.get("id", 0)) == event.comment_id:
                continue

            body = str(comment.get("body") or "")
            clean_body = _truncate_text(
                _RYO_ANY_MARKER_PATTERN.sub("", body).strip(),
                _max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_HISTORY_COMMENT_CHARS),
            )
            is_trusted_marker = self._is_trusted_marker_comment(comment)

            our_match = self._state_pattern.search(body)
            if our_match and is_trusted_marker:
                messages.append({"role": "assistant", "content": clean_body})
                try:
                    subconscious = json.loads(our_match.group("payload"))
                except json.JSONDecodeError:
                    pass
                last_bot_comment_at = str(comment.get("created_at") or "")
                continue

            if _RYO_ANY_MARKER_PATTERN.search(body) and is_trusted_marker:
                messages.append({"role": "assistant", "content": clean_body})
                continue

            messages.append({"role": "user", "content": clean_body})

        return HistorySnapshot(
            messages=messages,
            subconscious=subconscious,
            last_bot_comment_at=last_bot_comment_at or None,
        )

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.sleep(random.uniform(1, 5))
        state_blob = json.dumps(subconscious or {}, ensure_ascii=False, separators=(",", ":"))
        body = f"{content}\n<!-- ryo:{self._identity}: {state_blob} -->"
        await self._api.post_json(
            f"/repos/{event.owner}/{event.repo}/issues/{event.issue_number}/comments",
            json_body={"body": body},
        )

    async def aclose(self) -> None:
        await self._api.aclose()

    def _is_trusted_marker_comment(self, comment: dict[str, Any]) -> bool:
        login = str((comment.get("user") or {}).get("login") or "")
        return login in self._marker_author_logins


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


def _max_chars_from_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, 0)


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n[truncated: {omitted} chars omitted]"
