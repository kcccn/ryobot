from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ._base import GitHubSkillBase
from ._models import (
    DELETED_MEMORY_LABEL,
    MEMORY_LABEL,
    MEMORY_SCHEMA_VERSION,
    ArchiveMemoryArgs,
    CommitMemoryArgs,
    RefineMemoryArgs,
    RetrieveMemoryArgs,
    SearchRepoMemoryArgs,
)


class SearchRepoMemory(GitHubSkillBase):
    name = "search_repo_memory"
    description = "Search the closed long-term memory issue database in the current GitHub repository."
    args_model = SearchRepoMemoryArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        query = (
            f'repo:{context["owner"]}/{context["repo"]} is:issue is:closed '
            f'label:"{MEMORY_LABEL}" {args.query}'
        )
        result = await self._api.get_json(
            "/search/issues",
            params={"q": query, "per_page": max(args.limit + 5, 5), "sort": "updated", "order": "desc"},
        )
        issue_numbers = [
            int(item["number"])
            for item in result.get("items", [])
            if int(item["number"]) != int(context["issue_number"])
        ]
        if not issue_numbers:
            return "No similar memory records found."
        detailed_items = await asyncio.gather(
            *[
                self._api.get_json(f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}")
                for issue_number in issue_numbers
            ]
        )
        lines: list[str] = []
        for item in detailed_items:
            try:
                self._validated_memory_record(item)
            except RuntimeError:
                continue
            lines.append(f"#{item['number']}: {item['title']} ({item['html_url']})")
            if len(lines) >= args.limit:
                break
        return "\n".join(lines) if lines else "No similar memory records found."


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
            try:
                summary, metadata, _labels = self._validated_memory_record(issue)
            except RuntimeError:
                continue
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
        existing_summary, metadata, labels = self._validated_memory_record(issue)
        if DELETED_MEMORY_LABEL in labels:
            raise RuntimeError("Archived memory records cannot be refined. Create a new memory instead.")
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
                "labels": labels,
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
        summary, metadata, labels = self._validated_memory_record(issue)
        metadata["schema_version"] = MEMORY_SCHEMA_VERSION
        metadata["status"] = "archived"
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        if args.reason.strip():
            metadata["archive_reason"] = args.reason.strip()
        labels = [label for label in labels if label != MEMORY_LABEL and label != DELETED_MEMORY_LABEL]
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
