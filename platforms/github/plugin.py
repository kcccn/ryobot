from __future__ import annotations

import json
import re
from typing import Any

import httpx

from core.plugins import BasePlugin, HistorySnapshot, PluginEvent

from .client import GitHubApiClient

GITHUB_COMMENT_STATE_PATTERN = re.compile(
    r"<!--\s*nexus_state:\s*(?P<payload>\{.*?\})\s*-->",
    re.DOTALL,
)
GITHUB_COMMENT_MARKER_PATTERN = re.compile(
    r"<!--\s*nexus_state:.*?-->",
    re.DOTALL,
)


class GitHubPlugin(BasePlugin):
    """GitHub issue-comment adapter for the plugin port."""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self._api = GitHubApiClient(token=token, client=client, api_base_url=api_base_url)

    def parse_event(self, raw_payload: Any) -> PluginEvent:
        if not isinstance(raw_payload, dict):
            raise ValueError("GitHub issue_comment payload must be a dict.")

        issue = raw_payload.get("issue") or {}
        comment = raw_payload.get("comment") or {}
        repository = raw_payload.get("repository") or {}
        owner = (repository.get("owner") or {}).get("login")
        repo = repository.get("name")
        issue_number = issue.get("number")
        comment_id = comment.get("id")

        if not all([owner, repo, issue.get("id"), issue_number, comment_id, comment.get("body"), (comment.get("user") or {}).get("login")]):
            raise ValueError("GitHub issue_comment payload is missing required fields.")

        return PluginEvent(
            event_id=f"github:{owner}/{repo}:issue:{issue_number}:comment:{comment_id}",
            message=str(comment["body"]),
            author=str((comment.get("user") or {})["login"]),
            issue_id=str(issue["id"]),
            issue_number=int(issue_number),
            comment_id=int(comment_id),
            owner=str(owner),
            repo=str(repo),
        )

    async def fetch_history(self, event: PluginEvent) -> HistorySnapshot:
        comments = await self._api.get_json(
            f"/repos/{event.owner}/{event.repo}/issues/{event.issue_number}/comments",
            params={"per_page": 10},
        )
        messages: list[dict[str, str]] = []
        subconscious: dict[str, Any] = {}

        for comment in comments:
            if int(comment.get("id", 0)) == event.comment_id:
                continue

            body = str(comment.get("body") or "")
            match = GITHUB_COMMENT_STATE_PATTERN.search(body)
            if match:
                visible_content = GITHUB_COMMENT_MARKER_PATTERN.sub("", body).strip()
                messages.append({"role": "assistant", "content": visible_content})
                try:
                    subconscious = json.loads(match.group("payload"))
                except json.JSONDecodeError:
                    pass
                continue

            messages.append({"role": "user", "content": GITHUB_COMMENT_MARKER_PATTERN.sub("", body).strip()})

        return HistorySnapshot(messages=messages, subconscious=subconscious)

    async def send_reply(
        self,
        event: PluginEvent,
        content: str,
        subconscious: dict[str, Any] | None = None,
    ) -> None:
        state_blob = json.dumps(subconscious or {}, ensure_ascii=False, separators=(",", ":"))
        body = f"{content}\n<!-- nexus_state: {state_blob} -->"
        await self._api.post_json(
            f"/repos/{event.owner}/{event.repo}/issues/{event.issue_number}/comments",
            json_body={"body": body},
        )

    async def aclose(self) -> None:
        await self._api.aclose()
