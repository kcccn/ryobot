from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.skills import BaseSkill, get_skill_context

from .client import GitHubApiClient
from .utils import csv_env, max_chars_from_env, truncate_text


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
    labels: list[str] = Field(default_factory=list)


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
    inputs: dict[str, str] = Field(default_factory=dict)


class ListOpenIssuesArgs(BaseModel):
    state: str = Field(default="open")
    labels: str = Field(default="", description="Comma-separated label names to filter by")
    sort: str = Field(default="updated")
    direction: str = Field(default="desc")
    limit: int = Field(default=10, ge=1, le=30)


class ListFilesArgs(BaseModel):
    path: str = Field(default="", description="Directory path relative to repo root, empty for root")
    limit: int = Field(default=30, ge=1, le=100)


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to repo root")


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


class CreatePullRequestArgs(BaseModel):
    title: str = Field(description="PR title")
    head: str = Field(description="Branch containing the changes")
    base: str = Field(default="", description="Base branch to merge into (empty for repo default)")
    body: str = Field(default="", description="PR description")


DEFAULT_MAX_DIFF_CHARS = 50000
DEFAULT_MAX_ISSUE_BODY_CHARS = 12000


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
                "body": args.body,
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


class ListFiles(GitHubSkillBase):
    name = "list_files"
    description = (
        "List files and directories at a given path in the repository. "
        "Use this to explore the project structure. Pass an empty string for the root directory. "
        "Returns file/directory names, types, and sizes."
    )
    args_model = ListFilesArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        contents = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
        )
        if not isinstance(contents, list):
            return f"Not a directory: {args.path or '/'}"
        if not contents:
            return f"Directory is empty: {args.path or '/'}"

        lines: list[str] = [f"Contents of {args.path or '/'}:"]
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
        "Returns the decoded file content."
    )
    args_model = ReadFileArgs

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        item = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
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
        "Provide the file path, new content (plain text), and a commit message. "
        "Optionally specify a branch to commit to. "
        "If the file already exists it will be updated; if not, it will be created."
    )
    args_model = WriteFileArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        import base64

        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        content_bytes = args.content.encode("utf-8")
        body: dict[str, Any] = {
            "message": args.message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
        }
        if args.branch:
            body["branch"] = args.branch

        # Try to get existing file sha for updates
        try:
            existing = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
                params={"ref": args.branch} if args.branch else None,
            )
            if isinstance(existing, dict) and existing.get("sha"):
                body["sha"] = existing["sha"]
        except Exception:
            pass  # File doesn't exist, will be created

        result = await self._api.put_json(
            f"/repos/{context['owner']}/{context['repo']}/contents/{args.path}",
            json_body=body,
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

        result = await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/git/refs",
            json_body={
                "ref": f"refs/heads/{args.branch}",
                "sha": base_sha,
            },
        )
        return (
            f"Created branch '{args.branch}' from '{base}' (SHA: {base_sha[:7]})\n"
            f"URL: {result.get('url', '')}"
        )


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
            body["body"] = args.body

        result = await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/pulls",
            json_body=body,
        )
        return (
            f"Created PR #{result['number']}: {result['title']}\n"
            f"URL: {result.get('html_url', '')}\n"
            f"State: {result.get('state', 'unknown')}"
        )


class RunCommandArgs(BaseModel):
    command: str = Field(..., description="Shell command to execute. Working directory is the repository root.")


class RunCommand(GitHubSkillBase):
    name = "run_command"
    description = (
        "Execute a shell command in the repository workspace and return stdout/stderr. "
        "Use this to run tests, linters, build scripts, or any other development tool. "
        "The command runs in the repository root directory with a 5-minute timeout. "
        "All standard development tools (pytest, ruff, mypy, npm, cargo, go, etc.) are available."
    )
    args_model = RunCommandArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        workspace = os.getenv("GITHUB_WORKSPACE", ".")

        try:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=300,
            )
        except asyncio.TimeoutError:
            return f"Command timed out after 300s:\n  {args.command}"

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