from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.skills import BaseSkill, get_skill_context

from .client import GitHubApiClient
from .utils import csv_env, max_chars_from_env, sanitize_mentions, truncate_text


class EmptyArgs(BaseModel):
    pass


class SearchRepoMemoryArgs(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=10)


class CommitMemoryArgs(BaseModel):
    title: str = Field(description="Memory issue title")
    summary: str = Field(description="Human-readable summary of the durable memory")
    tags: list[str] = Field(default_factory=list, description="Optional memory tags such as user:alice or module:api")


class RetrieveMemoryArgs(BaseModel):
    query: str = Field(description="Keywords for searching the memory issue database")
    candidate_limit: int = Field(default=20, ge=1, le=20)
    limit: int = Field(default=3, ge=1, le=10)


class RefineMemoryArgs(BaseModel):
    memory_issue_number: int = Field(ge=1, description="Closed memory issue number to update")
    title: str = Field(default="", description="Replacement memory title, empty to keep unchanged")
    summary: str = Field(default="", description="Replacement human-readable summary, empty to keep unchanged")
    tags: list[str] = Field(default_factory=list, description="Replacement tag list, empty to keep unchanged")


class ArchiveMemoryArgs(BaseModel):
    memory_issue_number: int = Field(ge=1, description="Memory issue number to archive")
    reason: str = Field(default="", description="Why this memory is being archived")


class SearchRepoContextArgs(BaseModel):
    query: str = Field(description="Keywords or GitHub issue search syntax for repo context lookup")
    limit: int = Field(default=10, ge=1, le=20)
    kind: str = Field(default="all", description="One of: all, issues, prs")


class ReadCodeDiffArgs(BaseModel):
    pr_number: int = Field(ge=1)


class CreateIssueArgs(BaseModel):
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)


class AddLabelsArgs(BaseModel):
    labels: list[str]
    issue_number: int = 0


class ReadThreadCommentsArgs(BaseModel):
    issue_number: int = 0
    include_review_comments: bool = True


class CloseIssueArgs(BaseModel):
    issue_number: int = 0


class SearchIssuesArgs(BaseModel):
    query: str = Field(description="Search query (same syntax as GitHub issue search)")
    limit: int = Field(default=10, ge=1, le=30)


class UpdateIssueArgs(BaseModel):
    issue_number: int = Field(description="Issue number to update")
    title: str = Field(default="", description="New title (empty to keep unchanged)")
    body: str = Field(default="", description="New body (empty to keep unchanged)")


class CommentOnPRArgs(BaseModel):
    pr_number: int = 0
    body: str


class ReviewComment(BaseModel):
    path: str = Field(description="File path being commented on")
    line: int = Field(description="Line number in the file to comment on")
    body: str = Field(description="The review comment text")


class CreatePRReviewArgs(BaseModel):
    pr_number: int = Field(description="Pull request number")
    event: str = Field(
        default="COMMENT",
        description="Review action: COMMENT (neutral), APPROVE, or REQUEST_CHANGES",
    )
    body: str = Field(
        default="",
        description="Overall review summary (required for APPROVE/REQUEST_CHANGES)",
    )
    comments: list[ReviewComment] = Field(
        default_factory=list,
        description="Inline line-specific comments to attach to this review",
    )


class DispatchWorkflowArgs(BaseModel):
    workflow_id: str
    ref: str = "main"
    inputs: dict[str, str] = Field(default_factory=dict)


class ListOpenIssuesArgs(BaseModel):
    state: str = Field(default="open")
    labels: str = Field(default="", description="Comma-separated label names to filter by")
    sort: str = Field(default="updated")
    direction: str = Field(default="desc")
    limit: int = Field(default=10, ge=1, le=30)


class ListFilesArgs(BaseModel):
    path: str = Field(default="", description="Directory path relative to repo root, empty for root")
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for default branch)")
    limit: int = Field(default=30, ge=1, le=100)


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to repo root")
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for default branch)")


class SearchCodeArgs(BaseModel):
    query: str = Field(description="Code search query")
    limit: int = Field(default=5, ge=1, le=10)


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path relative to repo root")
    content: str = Field(description="New file content (plain text)")
    message: str = Field(default="Update file", description="Commit message")
    branch: str = Field(default="", description="Branch to commit to (empty for default branch)")


class CreateBranchArgs(BaseModel):
    branch: str = Field(description="Name of the new branch")
    base_branch: str = Field(default="", description="Branch to create from (empty for repo default)")


class DeleteBranchArgs(BaseModel):
    branch: str = Field(description="Name of the branch to delete")


class CreatePullRequestArgs(BaseModel):
    title: str = Field(description="PR title")
    head: str = Field(description="Branch containing the changes")
    base: str = Field(default="", description="Base branch to merge into (empty for repo default)")
    body: str = Field(default="", description="PR description")


DEFAULT_MAX_DIFF_CHARS = 50000
DEFAULT_MAX_ISSUE_BODY_CHARS = 12000
MEMORY_LABEL = "🧠 memory"
DELETED_MEMORY_LABEL = "🗑️ deleted"
MEMORY_SCHEMA_VERSION = 1
_MEMORY_MARKER_RE = re.compile(r"<!--\s*ryo:memory:\s*(\{.*?\})\s*-->", re.DOTALL)


class ReadWorkflowRunArgs(BaseModel):
    workflow_id: str = ""
    run_id: int = 0


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


class ReadIssueMemory(GitHubSkillBase):
    name = "read_issue_memory"
    description = "Read the current GitHub issue details for working memory."
    args_model = EmptyArgs

    async def execute(self, **kwargs: Any) -> str:
        context = self._require_context()
        issue = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{context['issue_number']}"
        )
        body = truncate_text(
            str(issue.get("body") or ""),
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_ISSUE_BODY_CHARS),
        )
        return "\n".join(
            [
                f"Issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}",
                f"Body: {body}",
            ]
        )


class SearchRepoMemory(GitHubSkillBase):
    name = "search_repo_memory"
    description = "Search similar issues in the current GitHub repository."
    args_model = SearchRepoMemoryArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        query = f"{args.query} repo:{context['owner']}/{context['repo']} is:issue"
        result = await self._api.get_json(
            "/search/issues",
            params={"q": query, "per_page": max(args.limit + 1, 2)},
        )
        lines: list[str] = []
        for item in result.get("items", []):
            if int(item["number"]) == int(context["issue_number"]):
                continue
            lines.append(f"#{item['number']}: {item['title']} ({item['html_url']})")
            if len(lines) >= args.limit:
                break
        return "\n".join(lines) if lines else "No similar issues found."


class CommitMemory(GitHubSkillBase):
    name = "commit_memory"
    description = (
        "Commit a durable repo memory into the closed GitHub issue memory database. "
        "Use this only for long-lived facts worth remembering later."
    )
    args_model = CommitMemoryArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        await self._ensure_repo_label(
            owner=context["owner"],
            repo=context["repo"],
            name=MEMORY_LABEL,
            color="5319e7",
            description="RyoBot long-term memory records",
        )
        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "status": "active",
            "tags": self._normalize_tags(args.tags),
            "source": self._memory_source(context),
            "created_at": now,
            "updated_at": now,
        }
        created = await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues",
            json_body={
                "title": args.title.strip(),
                "body": self._memory_body(args.summary, metadata),
                "labels": [MEMORY_LABEL],
            },
        )
        issue_number = int(created["number"])
        await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}",
            json_body={"state": "closed"},
        )
        return f"Committed memory issue #{issue_number}: {args.title.strip()}"


class RetrieveMemory(GitHubSkillBase):
    name = "retrieve_memory"
    description = (
        "Search the closed GitHub issue memory database for durable prior knowledge. "
        "Uses keyword search plus lightweight reranking over memory tags and recency."
    )
    args_model = RetrieveMemoryArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        query = (
            f'repo:{context["owner"]}/{context["repo"]} is:issue is:closed '
            f'label:"{MEMORY_LABEL}" {args.query}'
        )
        result = await self._api.get_json(
            "/search/issues",
            params={"q": query, "per_page": args.candidate_limit, "sort": "updated", "order": "desc"},
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            return f"No memory results for: {args.query}"

        issue_numbers = [int(item["number"]) for item in items]
        detailed_items = await asyncio.gather(
            *[
                self._api.get_json(f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}")
                for issue_number in issue_numbers
            ]
        )

        ranked: list[tuple[int, dict[str, Any], str, dict[str, Any]]] = []
        for issue in detailed_items:
            labels = self._labels_from_issue(issue)
            if MEMORY_LABEL not in labels or DELETED_MEMORY_LABEL in labels:
                continue
            summary, metadata = self._parse_memory_body(str(issue.get("body") or ""))
            score = self._score_memory_candidate(args.query, issue, summary, metadata)
            ranked.append((score, issue, summary, metadata))

        if not ranked:
            return f"No memory results for: {args.query}"

        ranked.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("updated_at") or item[1].get("created_at") or ""),
            ),
            reverse=True,
        )
        lines = [f"Memory results for '{args.query}' ({len(ranked)} candidates):"]
        for score, issue, summary, metadata in ranked[: args.limit]:
            tags = ", ".join(str(tag) for tag in metadata.get("tags", [])) or "none"
            lines.append(
                f"  #{issue['number']} score={score} title={issue['title']} "
                f"tags={tags} updated={issue.get('updated_at', '')} "
                f"url={issue.get('html_url', '')}\n    {summary}"
            )
        return "\n".join(lines)


class RefineMemory(GitHubSkillBase):
    name = "refine_memory"
    description = "Refine an existing closed memory issue when the stored long-term memory is incomplete or inaccurate."
    args_model = RefineMemoryArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        await self._ensure_repo_label(
            owner=context["owner"],
            repo=context["repo"],
            name=MEMORY_LABEL,
            color="5319e7",
            description="RyoBot long-term memory records",
        )
        issue = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{args.memory_issue_number}"
        )
        existing_summary, metadata = self._parse_memory_body(str(issue.get("body") or ""))
        if not metadata:
            metadata = {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "status": "active",
                "tags": [],
                "source": self._memory_source(context),
                "created_at": str(issue.get("created_at") or datetime.now(timezone.utc).isoformat()),
            }
        metadata["schema_version"] = MEMORY_SCHEMA_VERSION
        metadata["status"] = "active"
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        if args.tags:
            metadata["tags"] = self._normalize_tags(args.tags)
        new_title = args.title.strip() or str(issue.get("title") or "")
        new_summary = args.summary.strip() or existing_summary
        updated = await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{args.memory_issue_number}",
            json_body={
                "title": new_title,
                "body": self._memory_body(new_summary, metadata),
                "state": "closed",
                "labels": self._labels_from_issue(issue) or [MEMORY_LABEL],
            },
        )
        return f"Refined memory issue #{updated['number']}: {updated['title']}"


class ArchiveMemory(GitHubSkillBase):
    name = "archive_memory"
    description = "Archive a noisy or stale memory issue by removing the memory label and marking it deleted."
    args_model = ArchiveMemoryArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        await self._ensure_repo_label(
            owner=context["owner"],
            repo=context["repo"],
            name=DELETED_MEMORY_LABEL,
            color="8c8c8c",
            description="Archived or deleted memory records",
        )
        issue = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{args.memory_issue_number}"
        )
        summary, metadata = self._parse_memory_body(str(issue.get("body") or ""))
        if not metadata:
            metadata = {
                "schema_version": MEMORY_SCHEMA_VERSION,
                "tags": [],
                "source": self._memory_source(context),
                "created_at": str(issue.get("created_at") or datetime.now(timezone.utc).isoformat()),
            }
        metadata["schema_version"] = MEMORY_SCHEMA_VERSION
        metadata["status"] = "archived"
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        if args.reason.strip():
            metadata["archive_reason"] = args.reason.strip()
        labels = [label for label in self._labels_from_issue(issue) if label != MEMORY_LABEL and label != DELETED_MEMORY_LABEL]
        labels.append(DELETED_MEMORY_LABEL)
        updated = await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{args.memory_issue_number}",
            json_body={
                "title": str(issue.get("title") or ""),
                "body": self._memory_body(summary, metadata),
                "state": "closed",
                "labels": labels,
            },
        )
        return f"Archived memory issue #{updated['number']}: {updated['title']}"


class SearchRepoContext(GitHubSkillBase):
    name = "search_repo_context"
    description = (
        "Search non-memory issues and pull requests across the repository. "
        "This excludes archived memory records and is useful when the memory database is empty or weak."
    )
    args_model = SearchRepoContextArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        kind = args.kind.strip().lower()
        if kind not in {"all", "issues", "prs"}:
            return "Invalid kind. Use one of: all, issues, prs."
        context = self._require_context()
        filters = [f"repo:{context['owner']}/{context['repo']}", f"-label:\"{MEMORY_LABEL}\"", f"-label:\"{DELETED_MEMORY_LABEL}\""]
        if kind == "issues":
            filters.append("is:issue")
        elif kind == "prs":
            filters.append("is:pr")
        q = " ".join([*filters, args.query])
        result = await self._api.get_json(
            "/search/issues",
            params={"q": q, "per_page": args.limit, "sort": "updated", "order": "desc"},
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            return f"No repo context results for: {args.query}"
        lines = [f"Repo context results for '{args.query}' ({result.get('total_count', len(items))} total):"]
        for item in items:
            issue_type = "PR" if "pull_request" in item else "Issue"
            labels = ", ".join(self._labels_from_issue(item)) or "none"
            lines.append(
                f"  #{item['number']} [{issue_type}] {item['title']} "
                f"state={item.get('state', '')} labels={labels} "
                f"updated={item.get('updated_at', '')} url={item.get('html_url', '')}"
            )
        return "\n".join(lines)


class ReadCodeDiff(GitHubSkillBase):
    name = "read_code_diff"
    description = "Read the raw .diff content for a GitHub pull request."
    args_model = ReadCodeDiffArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        diff = await self._api.get_text(
            f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        return truncate_text(
            diff,
            max_chars_from_env("RYOBOT_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS),
        )


class CreateIssue(GitHubSkillBase):
    name = "create_issue"
    description = "Create a new GitHub issue in the current repository."
    args_model = CreateIssueArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        result = await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues",
            json_body={
                "title": args.title,
                "body": sanitize_mentions(args.body),
                "labels": args.labels,
            },
        )
        return f"Created issue #{result['number']}: {result['title']}"


class AddLabels(GitHubSkillBase):
    name = "add_labels"
    description = "Add labels to a GitHub issue."
    args_model = AddLabelsArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        existing_labels = await _repo_label_names(self, context)
        missing = [label for label in args.labels if label not in existing_labels]
        if missing:
            return f"Labels do not exist in repo: {', '.join(missing)}"
        await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}/labels",
            json_body={"labels": args.labels},
        )
        return f"Added labels {args.labels} to issue #{issue_number}"


class CloseIssue(GitHubSkillBase):
    name = "close_issue"
    description = "Close a GitHub issue."
    args_model = CloseIssueArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}",
            json_body={"state": "closed"},
        )
        return f"Closed issue #{issue_number}"


class UpdateIssue(GitHubSkillBase):
    name = "update_issue"
    description = (
        "Update the title and/or body of an existing issue. "
        "Use this to persist learnings, update your mind issue, "
        "or refine an issue's description based on new findings. "
        "Pass only the fields you want to change; empty strings keep the current value."
    )
    args_model = UpdateIssueArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        body: dict[str, Any] = {}
        if args.title:
            body["title"] = args.title
        if args.body:
            body["body"] = sanitize_mentions(args.body)
        if not body:
            return "Nothing to update: both title and body are empty."
        await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{args.issue_number}",
            json_body=body,
        )
        parts: list[str] = [f"Updated issue #{args.issue_number}"]
        if "title" in body:
            parts.append("title")
        if "body" in body:
            parts.append("body")
        return ": ".join([parts[0], ", ".join(parts[1:])])


class CommentOnPR(GitHubSkillBase):
    name = "comment_on_pr"
    description = "Post a comment on a GitHub pull request."
    args_model = CommentOnPRArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        pr_number = args.pr_number if args.pr_number else context["issue_number"]
        await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{pr_number}/comments",
            json_body={"body": self._bot_prefix() + sanitize_mentions(args.body)},
        )
        return f"Commented on PR #{pr_number}"


class CreatePRReview(GitHubSkillBase):
    name = "create_pr_review"
    description = (
        "Submit a review on a pull request. Use this to do line-by-line code review. "
        "First use read_code_diff to see what changed, then read_file to see full file context, "
        "then submit your review with inline comments at specific lines. "
        "event can be COMMENT (neutral feedback), APPROVE, or REQUEST_CHANGES. "
        "Include a brief overall summary in body, and put detailed per-line feedback in comments "
        "(each with file path, line number, and comment text). "
        "ALWAYS use this instead of comment_on_pr when reviewing code — "
        "inline comments on specific lines are far more useful than a generic overall comment."
    )
    args_model = CreatePRReviewArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        review_body: dict[str, Any] = {
            "event": args.event,
        }
        if args.body:
            review_body["body"] = self._bot_prefix() + sanitize_mentions(args.body)
        if args.comments:
            review_body["comments"] = [
                {
                    "path": c.path,
                    "line": c.line,
                    "body": sanitize_mentions(c.body),
                    "side": "RIGHT",
                }
                for c in args.comments
            ]

        try:
            result = await self._api.post_json(
                f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}/reviews",
                json_body=review_body,
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return (
            f"Submitted {args.event} review on PR #{args.pr_number}\n"
            f"State: {result.get('state', '?')}\n"
            f"ID: {result.get('id', '?')}"
        )


class DispatchWorkflow(GitHubSkillBase):
    name = "dispatch_workflow"
    description = (
        "Trigger a GitHub Actions workflow by its filename (e.g. 'ci.yml') "
        "or numeric ID. The workflow must have a workflow_dispatch trigger. "
        "Use this to run tests, lint, deploy, or any CI pipeline already "
        "defined in the repository."
    )
    args_model = DispatchWorkflowArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        # Prevent infinite dispatch loops
        if "workflow_dispatch" in str(context.get("event_id", "")):
            return (
                "Workflow dispatch is not available within a patrol-dispatched run "
                "to prevent infinite dispatch loops."
            )

        allowed_workflows = csv_env("RYOBOT_ALLOWED_WORKFLOWS")
        if not allowed_workflows:
            return "Workflow dispatch is disabled because RYOBOT_ALLOWED_WORKFLOWS is not configured."
        if args.workflow_id not in allowed_workflows:
            return f"Workflow '{args.workflow_id}' is not allowed for dispatch."
        allowed_refs = csv_env("RYOBOT_ALLOWED_WORKFLOW_REFS") or {"main"}
        if args.ref not in allowed_refs:
            return f"Workflow ref '{args.ref}' is not allowed for dispatch."
        inputs = args.inputs if isinstance(args.inputs, dict) else {}
        await self._api.post_no_content(
            f"/repos/{context['owner']}/{context['repo']}/actions/workflows/{args.workflow_id}/dispatches",
            json_body={"ref": args.ref, "inputs": inputs},
        )
        return (
            f"Dispatched workflow '{args.workflow_id}' on ref '{args.ref}'. "
            f"Use read_workflow_run with workflow_id='{args.workflow_id}' to check the result."
        )


class ReadWorkflowRun(GitHubSkillBase):
    name = "read_workflow_run"
    description = (
        "Read the status of a GitHub Actions workflow run. "
        "If run_id is provided, reads that specific run. "
        "Otherwise reads the latest run for the given workflow_id. "
        "Returns status, conclusion, duration, and a link to the run."
    )
    args_model = ReadWorkflowRunArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        if args.run_id > 0:
            run = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/actions/runs/{args.run_id}"
            )
        elif args.workflow_id:
            runs = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/actions/workflows/{args.workflow_id}/runs",
                params={"per_page": 1},
            )
            if not runs.get("workflow_runs"):
                return f"No runs found for workflow '{args.workflow_id}'."
            run = runs["workflow_runs"][0]
        else:
            return "Must provide either workflow_id or run_id."

        return "\n".join(
            [
                f"Workflow: {run.get('name') or args.workflow_id}",
                f"Status: {run.get('status')}",
                f"Conclusion: {run.get('conclusion') or 'N/A'}",
                f"Created: {run.get('created_at')}",
                f"URL: {run.get('html_url')}",
            ]
        )


class ListOpenIssues(GitHubSkillBase):
    name = "list_open_issues"
    description = (
        "List issues in the current repository. "
        "Use state='open' (default) for active issues, 'closed' for completed ones, or 'all' for both. "
        "Filter by labels (comma-separated). "
        "Sort by 'created', 'updated', or 'comments'. "
        "Returns issue number, title, state, labels, author, created/updated timestamps, and URL."
    )
    args_model = ListOpenIssuesArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {
            "state": args.state,
            "sort": args.sort,
            "direction": args.direction,
            "per_page": min(args.limit, 30),
        }
        if args.labels:
            params["labels"] = args.labels

        issues = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues",
            params=params,
        )
        if not issues:
            return f"No {args.state} issues found in {context['owner']}/{context['repo']}."

        lines: list[str] = []
        for issue in issues:
            if "pull_request" in issue:
                continue
            labels_list = [lb.get("name", "") for lb in issue.get("labels", [])]
            labels_str = ", ".join(labels_list) if labels_list else "none"
            lines.append(
                f"#{issue['number']}: {issue['title']} "
                f"[{issue['state']}] "
                f"labels: {labels_str} "
                f"author: {issue.get('user', {}).get('login', 'unknown')} "
                f"updated: {issue.get('updated_at', '')} "
                f"url: {issue.get('html_url', '')}"
            )
        if not lines:
            return f"No {args.state} issues found (excluding PRs)."
        return "\n".join(lines)


class ListOpenPullRequestsArgs(BaseModel):
    state: str = Field(default="open")
    sort: str = Field(default="updated")
    direction: str = Field(default="desc")
    limit: int = Field(default=10, ge=1, le=30)


class ListOpenPullRequests(GitHubSkillBase):
    name = "list_open_pull_requests"
    description = (
        "List pull requests in the current repository. "
        "Use state='open' (default) for active PRs, 'closed' for merged/closed ones, or 'all' for both. "
        "Sort by 'created', 'updated', 'popularity', or 'long-running'. "
        "Returns PR number, title, state, draft status, author, head/base branches, created/updated timestamps, and URL."
    )
    args_model = ListOpenPullRequestsArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {
            "state": args.state,
            "sort": args.sort,
            "direction": args.direction,
            "per_page": min(args.limit, 30),
        }

        prs = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/pulls",
            params=params,
        )
        if not prs:
            return f"No {args.state} pull requests found in {context['owner']}/{context['repo']}."

        lines: list[str] = []
        for pr in prs:
            draft = " [DRAFT]" if pr.get("draft") else ""
            lines.append(
                f"#{pr['number']}: {pr['title']}{draft} "
                f"[{pr['state']}] "
                f"author: {pr.get('user', {}).get('login', 'unknown')} "
                f"branch: {pr.get('head', {}).get('ref', '?')} → {pr.get('base', {}).get('ref', '?')} "
                f"updated: {pr.get('updated_at', '')} "
                f"url: {pr.get('html_url', '')}"
            )
        return "\n".join(lines)


class SearchIssues(GitHubSkillBase):
    name = "search_issues"
    description = (
        "Search issues and pull requests in the repository using GitHub's search syntax. "
        "You can search by keywords in title/body, filter by state/labels/author, or search "
        "for exact title matches. Examples: 'is:issue is:open label:bug' or 'bot-mind in:title'. "
        "Returns issue number, title, state, labels, author, and URL."
    )
    args_model = SearchIssuesArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        q = f"repo:{context['owner']}/{context['repo']} {args.query}"
        result = await self._api.get_json(
            "/search/issues",
            params={"q": q, "per_page": min(args.limit, 30), "sort": "updated", "order": "desc"},
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            return f"No results for: {args.query}"

        lines: list[str] = [f"Search results for '{args.query}' ({result.get('total_count', 0)} total):"]
        for issue in items:
            labels_str = ", ".join(lb.get("name", "") for lb in issue.get("labels", [])) or "none"
            issue_type = "PR" if "pull_request" in issue else "Issue"
            lines.append(
                f"  #{issue['number']} [{issue_type}] {issue['title']} "
                f"state={issue['state']} labels={labels_str} "
                f"author={issue.get('user', {}).get('login', '?')} "
                f"url={issue.get('html_url', '')}"
            )
        return "\n".join(lines)


class ListRepoLabels(GitHubSkillBase):
    name = "list_repo_labels"
    description = "List existing labels in the current repository before applying labels to issues or pull requests."
    args_model = EmptyArgs

    async def execute(self, **kwargs: Any) -> str:
        context = self._require_context()
        labels = await self._fetch_paginated(
            f"/repos/{context['owner']}/{context['repo']}/labels",
            params={"per_page": 100},
        )
        if not labels:
            return f"No labels found in {context['owner']}/{context['repo']}."
        lines: list[str] = []
        for label in labels:
            name = str(label.get("name") or "")
            color = str(label.get("color") or "")
            description = str(label.get("description") or "")
            suffix = f": {description}" if description else ""
            lines.append(f"{name} (#{color}){suffix}")
        return "\n".join(lines)


class ReadThreadComments(GitHubSkillBase):
    name = "read_thread_comments"
    description = (
        "Read comments from another issue or pull request in the same repository. "
        "Use this to understand related discussions before deciding whether to reply or label."
    )
    args_model = ReadThreadCommentsArgs
    requires_trusted_author = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        comments = await self._fetch_paginated(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}/comments",
            params={"per_page": 100, "sort": "created", "direction": "asc"},
        )
        if args.include_review_comments:
            review_comments = await self._fetch_paginated(
                f"/repos/{context['owner']}/{context['repo']}/pulls/{issue_number}/comments",
                params={"per_page": 100, "sort": "created", "direction": "asc"},
            )
            comments = [*comments, *review_comments]
        comments = sorted(comments, key=lambda item: str(item.get("created_at") or ""))
        if not comments:
            return f"No comments found for issue/PR #{issue_number}."

        lines: list[str] = [f"Comments for issue/PR #{issue_number}:"]
        for comment in comments:
            author = str((comment.get("user") or {}).get("login") or "unknown")
            created_at = str(comment.get("created_at") or "")
            body = str(comment.get("body") or "").strip()
            location = _comment_location(comment)
            lines.append(f"{author} at {created_at}{location}: {body}")
        return "\n".join(lines)


class ListFiles(GitHubSkillBase):
    name = "list_files"
    description = (
        "List files and directories at a given path in the repository. "
        "Use this to explore the project structure. Pass an empty string for the root directory. "
        "Optionally provide a ref (branch, tag, or commit SHA) to list files on a non-default branch. "
        "Returns file/directory names, types, and sizes."
    )
    args_model = ListFilesArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {}
        if args.ref:
            params["ref"] = args.ref
        contents = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
            params=params if params else None,
        )
        if not isinstance(contents, list):
            return f"Not a directory: {args.path or '/'}"
        if not contents:
            return f"Directory is empty: {args.path or '/'}"

        ref_note = f" (ref: {args.ref})" if args.ref else ""
        lines: list[str] = [f"Contents of {args.path or '/'}{ref_note}:"]
        for item in contents[: args.limit]:
            item_type = item.get("type", "?")
            prefix = "📁" if item_type == "dir" else "📄"
            lines.append(f"  {prefix} {item['name']} ({item_type}, {item.get('size', 0)} bytes)")
        if len(contents) > args.limit:
            lines.append(f"  ... and {len(contents) - args.limit} more entries")
        return "\n".join(lines)


class ReadFile(GitHubSkillBase):
    name = "read_file"
    description = (
        "Read the content of a file in the repository. "
        "Provide the full path relative to the repository root. "
        "Optionally provide a ref (branch, tag, or commit SHA) to read from a "
        "non-default branch. Returns the decoded file content."
    )
    args_model = ReadFileArgs

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        params: dict[str, Any] = {}
        if args.ref:
            params["ref"] = args.ref
        item = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
            params=params if params else None,
        )
        if not isinstance(item, dict):
            return f"Cannot read: {args.path}"
        if item.get("type") != "file":
            return f"Not a file: {args.path} (type: {item.get('type', 'unknown')})"
        size = item.get("size", 0)
        if size > 500_000:
            return f"File too large to read: {args.path} ({size} bytes, limit 500KB)"

        content_b64 = item.get("content", "")
        if not content_b64:
            return f"File is empty: {args.path}"
        try:
            decoded = base64.b64decode(content_b64).decode("utf-8")
        except Exception:
            return f"File content could not be decoded as UTF-8: {args.path}"
        max_chars = max_chars_from_env("RYOBOT_MAX_FILE_CHARS", 30000)
        lines = decoded.split("\n")
        truncated = truncate_text(decoded, max_chars)
        if len(decoded) > max_chars:
            header = f"File: {args.path} ({len(lines)} lines, {size} bytes, truncated to {max_chars} chars)\n\n"
        else:
            header = f"File: {args.path} ({len(lines)} lines, {size} bytes)\n\n"
        return header + truncated


class SearchCode(GitHubSkillBase):
    name = "search_code"
    description = (
        "Search for code in the repository using GitHub's code search. "
        "Use keywords, regex patterns, or function names. "
        "Returns matching file paths and code snippets."
    )
    args_model = SearchCodeArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        q = f"{args.query} repo:{context['owner']}/{context['repo']}"
        result = await self._api.get_json(
            "/search/code",
            params={"q": q, "per_page": args.limit},
        )
        items = result.get("items", [])
        if not items:
            return f"No code results found for '{args.query}'."

        total = result.get("total_count", len(items))
        lines: list[str] = [f"Code search results for '{args.query}' ({total} total, showing top {len(items)}):"]
        for item in items:
            repo_info = item.get("repository", {})
            lines.append(
                f"\n  {item['path']} ({repo_info.get('full_name', '')})\n"
                f"    {item.get('html_url', '')}"
            )
        return "\n".join(lines)


class WriteFile(GitHubSkillBase):
    name = "write_file"
    description = (
        "Create or update a file in the repository. "
        "Provide the file path, new content (plain text), a commit message, "
        "and a branch name to commit to. "
        "If the file already exists it will be updated; if not, it will be created. "
        "Writing directly to the default branch is not allowed — you must create a "
        "feature branch first, write to it, and then open a pull request."
    )
    args_model = WriteFileArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        repo_info = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}",
        )
        default_branch = repo_info.get("default_branch", "main")
        effective_branch = args.branch or default_branch
        if effective_branch == default_branch:
            return (
                f"Refusing to write directly to the default branch '{default_branch}'. "
                "All code changes must go through the PR workflow:\n"
                "1. Use create_branch to create a feature branch\n"
                "2. Use write_file with branch=<your-branch> to commit changes\n"
                "3. Use create_pull_request to open a PR for review\n"
                "You must never commit directly to the default branch."
            )

        content_bytes = args.content.encode("utf-8")
        body: dict[str, Any] = {
            "message": args.message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": args.branch,
        }

        # Try to get existing file sha for updates
        try:
            existing = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
                params={"ref": args.branch},
            )
            if isinstance(existing, dict) and existing.get("sha"):
                body["sha"] = existing["sha"]
        except Exception:
            pass  # File doesn't exist, will be created

        try:
            result = await self._api.put_json(
                f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        action = "Updated" if body.get("sha") else "Created"
        return (
            f"{action} file {args.path}\n"
            f"Commit: {result.get('commit', {}).get('sha', '')}\n"
            f"URL: {result.get('content', {}).get('html_url', '')}"
        )


class CreateBranch(GitHubSkillBase):
    name = "create_branch"
    description = (
        "Create a new branch in the repository. "
        "Provide the new branch name. Optionally specify a base branch to branch from "
        "(defaults to the repository's default branch)."
    )
    args_model = CreateBranchArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        # Get the SHA of the base branch
        base = args.base_branch
        if not base:
            repo_info = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}",
            )
            base = repo_info.get("default_branch", "main")

        ref_info = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/git/refs/heads/{base}",
        )
        base_sha = ref_info.get("object", {}).get("sha", "")
        if not base_sha:
            return f"Could not get SHA for branch '{base}'."

        try:
            result = await self._api.post_json(
                f"/repos/{context['owner']}/{context['repo']}/git/refs",
                json_body={
                    "ref": f"refs/heads/{args.branch}",
                    "sha": base_sha,
                },
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return (
            f"Created branch '{args.branch}' from '{base}' (SHA: {base_sha[:7]})\n"
            f"URL: {result.get('url', '')}"
        )


class DeleteBranch(GitHubSkillBase):
    name = "delete_branch"
    description = (
        "Delete a branch from the repository. "
        "Use this to clean up stale feature/fix branches after their PR has been merged or closed. "
        "You cannot delete the default branch."
    )
    args_model = DeleteBranchArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        try:
            await self._api.delete_json(
                f"/repos/{context['owner']}/{context['repo']}/git/refs/heads/{args.branch}",
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return f"Deleted branch '{args.branch}'"


class CreatePullRequest(GitHubSkillBase):
    name = "create_pull_request"
    description = (
        "Create a new pull request. "
        "Provide the PR title, the head branch (where your changes are), "
        "and optionally the base branch to merge into (defaults to repo default). "
        "Optionally include a PR description body."
    )
    args_model = CreatePullRequestArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        base = args.base
        if not base:
            repo_info = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}",
            )
            base = repo_info.get("default_branch", "main")

        body: dict[str, Any] = {
            "title": args.title,
            "head": args.head,
            "base": base,
        }
        if args.body:
            body["body"] = sanitize_mentions(args.body)

        try:
            result = await self._api.post_json(
                f"/repos/{context['owner']}/{context['repo']}/pulls",
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return (
            f"Created PR #{result['number']}: {result['title']}\n"
            f"URL: {result.get('html_url', '')}\n"
            f"State: {result.get('state', 'unknown')}"
        )


class MergePullRequestArgs(BaseModel):
    pr_number: int = Field(description="Pull request number to merge")
    merge_method: str = Field(
        default="merge",
        description="Merge method: 'merge', 'squash', or 'rebase'",
    )
    commit_title: str = Field(
        default="",
        description="Title for the merge commit (only for squash/rebase)",
    )


class MergePullRequest(GitHubSkillBase):
    name = "merge_pull_request"
    description = (
        "Merge a pull request. "
        "Provide the PR number and optionally the merge method "
        "('merge', 'squash', or 'rebase', default 'merge'). "
        "Only PRs with a clean mergeable state can be merged. "
        "Use this to land reviewed and approved PRs — never merge "
        "without prior review."
    )
    args_model = MergePullRequestArgs
    mutates_state = True
    requires_trusted_author = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()

        # Check PR state first
        pr_info = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}",
        )
        if pr_info.get("state") != "open":
            return f"PR #{args.pr_number} is not open (state={pr_info.get('state')})."
        if pr_info.get("merged"):
            return f"PR #{args.pr_number} is already merged."
        if pr_info.get("draft"):
            return f"PR #{args.pr_number} is still a draft. Mark it ready for review first."
        if pr_info.get("mergeable") is False:
            return f"PR #{args.pr_number} has merge conflicts and cannot be merged."
        if pr_info.get("mergeable_state") == "blocked":
            return (
                f"PR #{args.pr_number} is blocked from merging: "
                f"{pr_info.get('mergeable_state', 'unknown')}"
            )

        body: dict[str, Any] = {
            "merge_method": args.merge_method,
        }
        if args.commit_title:
            body["commit_title"] = args.commit_title

        try:
            result = await self._api.put_json(
                f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}/merge",
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            return (
                f"GitHub API error ({exc.response.status_code}): "
                f"{exc.response.text[:1000]}"
            )
        return (
            f"Merged PR #{args.pr_number}: {pr_info.get('title', '')}\n"
            f"Method: {args.merge_method}\n"
            f"SHA: {result.get('sha', '')}\n"
            f"Merged: {result.get('merged', False)}"
        )


class RunCommandArgs(BaseModel):
    command: str = Field(..., description="Shell command to execute. Working directory is the repository root.")


class RunCommand(GitHubSkillBase):
    name = "run_command"
    description = (
        "Execute an allowlisted development command in the repository workspace and return stdout/stderr. "
        "Default allowed commands are pytest, python -m pytest, ruff check, mypy, and pyright. "
        "Shell metacharacters are rejected and secrets are stripped from the subprocess environment."
    )
    args_model = RunCommandArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        workspace = os.getenv("GITHUB_WORKSPACE", ".")
        parsed = _parse_safe_command(args.command)
        if isinstance(parsed, str):
            return parsed
        if not _is_allowed_command(parsed):
            return (
                "Command is not allowed. Allowed command prefixes: "
                + ", ".join(sorted(_allowed_command_prefixes()))
            )
        timeout = _command_timeout_seconds()

        try:
            proc = await asyncio.create_subprocess_exec(
                *parsed,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env=_safe_subprocess_env(),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"Command timed out after {timeout}s:\n  {args.command}"
        except FileNotFoundError as exc:
            return f"Command failed to start: {exc}"

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        parts: list[str] = [f"Exit code: {exit_code}"]
        if stdout:
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr:
            parts.append(f"stderr:\n{stderr.rstrip()}")
        result = "\n\n".join(parts)

        max_chars = max_chars_from_env("RYOBOT_MAX_TOOL_RESULT_CHARS", 20000)
        return truncate_text(result, max_chars)


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


def _repo_label_names(skill: GitHubSkillBase, context: dict[str, Any]) -> Any:
    async def _load() -> set[str]:
        labels = await skill._fetch_paginated(
            f"/repos/{context['owner']}/{context['repo']}/labels",
            params={"per_page": 100},
        )
        return {str(label.get("name") or "") for label in labels}

    return _load()


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
