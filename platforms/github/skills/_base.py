from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from core.skills import BaseSkill, get_skill_context

from ..client import GitHubApiClient
from ..utils import (
    is_internal_issue_artifact,
)
from ._models import (
    _MEMORY_MARKER_RE,
)


class GitHubSkillBase(BaseSkill):
    """Shared GitHub client plumbing for skills."""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self._api = GitHubApiClient(token=token, client=client, api_base_url=api_base_url)

    async def aclose(self) -> None:
        await self._api.aclose()

    _BOT_DISPLAY_NAMES: dict[str, str] = {
        "architect": "Ryo Architect",
        "reviewer": "Ryo Reviewer",
        "pm": "Ryo PM",
        "explorer": "Ryo Explorer",
        "coder": "Ryo Coder",
    }

    @staticmethod
    def _bot_prefix() -> str:
        identity = os.getenv("BOT_IDENTITY", "bot")
        display = GitHubSkillBase._BOT_DISPLAY_NAMES.get(identity, identity)
        return f"**{display}**\n\n"

    @staticmethod
    def _require_context() -> dict[str, Any]:
        context = get_skill_context()
        if not context:
            raise RuntimeError("GitHub skill context is not available.")
        return context

    @staticmethod
    def _default_ref_for_context(context: dict[str, Any]) -> str:
        if context.get("is_pull_request") and context.get("head_ref"):
            return str(context["head_ref"])
        return ""

    @classmethod
    def _effective_ref(cls, context: dict[str, Any], requested_ref: str) -> str:
        requested = str(requested_ref or "").strip()
        if requested:
            return requested
        return cls._default_ref_for_context(context)

    async def _current_authenticated_login(self) -> str:
        profile = await self._api.get_json("/user")
        return str(profile.get("login") or "")

    async def _fetch_paginated(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            page_params = dict(params or {})
            page_params["per_page"] = page_params.get("per_page", 100)
            page_params["page"] = page
            items = await self._api.get_json(path, params=page_params)
            if not isinstance(items, list) or not items:
                return results
            results.extend(items)
            if len(items) < int(page_params.get("per_page", 100)):
                return results
            page += 1

    @staticmethod
    def _thread_context_unavailable_message() -> str:
        return (
            "No current thread in repo-scan. Thread-context tools are unavailable here; "
            "use retrieve_memory or search_repo_context instead."
        )

    @staticmethod
    def _internal_artifact_hidden_message() -> str:
        return (
            "Internal artifact: hidden by default. This thread is bot-maintenance or long-term memory, "
            "not normal project context. Re-run with include_internal=true only when you intentionally need it."
        )

    @staticmethod
    def _is_current_thread(context: dict[str, Any], issue_number: int) -> bool:
        return int(context.get("issue_number") or 0) > 0 and issue_number == int(context.get("issue_number") or 0)

    async def _fetch_visible_issue_thread(
        self,
        *,
        issue_number: int,
        include_internal: bool,
    ) -> dict[str, Any] | str:
        context = self._require_context()
        if issue_number <= 0:
            return self._thread_context_unavailable_message()
        issue = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}"
        )
        if not include_internal and not self._is_current_thread(context, issue_number) and is_internal_issue_artifact(issue):
            return self._internal_artifact_hidden_message()
        return issue

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in tags:
            value = str(raw).strip()
            if not value:
                continue
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(value)
        return normalized

    @staticmethod
    def _memory_source(context: dict[str, Any]) -> dict[str, Any]:
        return {
            "issue_number": int(context.get("issue_number") or 0),
            "comment_id": int(context.get("comment_id") or 0),
            "is_pull_request": bool(context.get("is_pull_request")),
        }

    @staticmethod
    def _memory_body(summary: str, metadata: dict[str, Any]) -> str:
        cleaned_summary = summary.strip() or "(empty summary)"
        return "\n".join(
            [
                "### 记忆摘要",
                cleaned_summary,
                "",
                "---",
                f"<!-- ryo:memory: {json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))} -->",
            ]
        )

    @staticmethod
    def _parse_memory_body(body: str) -> tuple[str, dict[str, Any]]:
        marker = _MEMORY_MARKER_RE.search(body or "")
        metadata: dict[str, Any] = {}
        visible_body = body or ""
        if marker:
            visible_body = visible_body[: marker.start()].rstrip()
            try:
                loaded = json.loads(marker.group(1))
                if isinstance(loaded, dict):
                    metadata = loaded
            except json.JSONDecodeError:
                metadata = {}
        if visible_body.startswith("### 记忆摘要"):
            visible_body = visible_body[len("### 记忆摘要") :].strip()
        visible_body = re.sub(r"\n?---\s*$", "", visible_body).strip()
        return visible_body, metadata

    @staticmethod
    def _labels_from_issue(issue: dict[str, Any]) -> list[str]:
        labels: list[str] = []
        for item in issue.get("labels", []):
            if isinstance(item, dict):
                value = str(item.get("name") or "").strip()
            else:
                value = str(item).strip()
            if value:
                labels.append(value)
        return labels

    @staticmethod
    def _score_memory_candidate(query: str, issue: dict[str, Any], summary: str, metadata: dict[str, Any]) -> int:
        query_text = query.strip().casefold()
        terms = [part for part in re.split(r"\s+", query_text) if part]
        title = str(issue.get("title") or "")
        tags = [str(tag) for tag in metadata.get("tags", []) if str(tag).strip()]
        haystack = "\n".join([title, summary, " ".join(tags)]).casefold()
        score = 0
        if query_text and query_text in haystack:
            score += 50
        score += sum(8 for term in terms if term in haystack)
        score += sum(12 for term in terms if any(term in tag.casefold() for tag in tags))
        timestamp = str(issue.get("updated_at") or metadata.get("updated_at") or issue.get("created_at") or "")
        try:
            updated_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            updated_at = None
        if updated_at is not None:
            age_days = max((datetime.now(timezone.utc) - updated_at).days, 0)
            score += max(0, 10 - min(age_days // 30, 10))
        return score

    async def _ensure_repo_label(
        self,
        *,
        owner: str,
        repo: str,
        name: str,
        color: str,
        description: str,
    ) -> None:
        labels = await self._fetch_paginated(
            f"/repos/{owner}/{repo}/labels",
            params={"per_page": 100},
        )
        if any(str(label.get("name") or "") == name for label in labels):
            return
        await self._api.post_json(
            f"/repos/{owner}/{repo}/labels",
            json_body={"name": name, "color": color, "description": description},
        )
