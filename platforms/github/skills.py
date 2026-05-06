from __future__ import annotations

import asyncio
import os
import shlex
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


class ReadThreadCommentsArgs(BaseModel):
    issue_number: int = 0
    include_review_comments: bool = True


class CloseIssueArgs(BaseModel):
    issue_number: int = 0


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
            json_body={"body": self._bot_prefix() + args.body},
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
            review_body["body"] = self._bot_prefix() + args.body
        if args.comments:
            review_body["comments"] = [
                {
                    "path": c.path,
                    "line": c.line,
                    "body": c.body,
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
