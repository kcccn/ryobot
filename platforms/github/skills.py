from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.skills import BaseSkill, get_skill_context

from .client import GitHubApiClient


class EmptyArgs(BaseModel):
    pass


class SearchRepoMemoryArgs(BaseModel):
    query: str
    limit: int = Field(default=5, ge=1, le=10)


class ReadCodeDiffArgs(BaseModel):
    pr_number: int = Field(ge=1)


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

    @staticmethod
    def _require_context() -> dict[str, Any]:
        context = get_skill_context()
        if not context:
            raise RuntimeError("GitHub skill context is not available.")
        return context


class ReadIssueMemory(GitHubSkillBase):
    name = "read_issue_memory"
    description = "Read the current GitHub issue details for working memory."
    args_model = EmptyArgs

    async def execute(self, **kwargs: Any) -> str:
        context = self._require_context()
        issue = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{context['issue_number']}"
        )
        return "\n".join(
            [
                f"Issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}",
                f"Body: {issue.get('body') or ''}",
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


class ReadCodeDiff(GitHubSkillBase):
    name = "read_code_diff"
    description = "Read the raw .diff content for a GitHub pull request."
    args_model = ReadCodeDiffArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        return await self._api.get_text(
            f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}",
            accept="application/vnd.github.v3.diff",
        )
