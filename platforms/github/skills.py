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


class CreateIssueArgs(BaseModel):
    title: str
    body: str = ""
    labels: list[str] = []


class AddLabelsArgs(BaseModel):
    labels: list[str]
    issue_number: int = 0


class CloseIssueArgs(BaseModel):
    issue_number: int = 0


class CommentOnPRArgs(BaseModel):
    pr_number: int = 0
    body: str


class DispatchWorkflowArgs(BaseModel):
    workflow_id: str
    ref: str = "main"
    inputs: dict[str, str] = {}


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


class CreateIssue(GitHubSkillBase):
    name = "create_issue"
    description = "Create a new GitHub issue in the current repository."
    args_model = CreateIssueArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        result = await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues",
            json_body={
                "title": args.title,
                "body": args.body,
                "labels": args.labels,
            },
        )
        return f"Created issue #{result['number']}: {result['title']}"


class AddLabels(GitHubSkillBase):
    name = "add_labels"
    description = "Add labels to a GitHub issue."
    args_model = AddLabelsArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}/labels",
            json_body={"labels": args.labels},
        )
        return f"Added labels {args.labels} to issue #{issue_number}"


class CloseIssue(GitHubSkillBase):
    name = "close_issue"
    description = "Close a GitHub issue."
    args_model = CloseIssueArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}",
            json_body={"state": "closed"},
        )
        return f"Closed issue #{issue_number}"


class CommentOnPR(GitHubSkillBase):
    name = "comment_on_pr"
    description = "Post a comment on a GitHub pull request."
    args_model = CommentOnPRArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        pr_number = args.pr_number if args.pr_number else context["issue_number"]
        await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{pr_number}/comments",
            json_body={"body": args.body},
        )
        return f"Commented on PR #{pr_number}"


class DispatchWorkflow(GitHubSkillBase):
    name = "dispatch_workflow"
    description = (
        "Trigger a GitHub Actions workflow by its filename (e.g. 'ci.yml') "
        "or numeric ID. The workflow must have a workflow_dispatch trigger. "
        "Use this to run tests, lint, deploy, or any CI pipeline already "
        "defined in the repository."
    )
    args_model = DispatchWorkflowArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
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
