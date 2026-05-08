from __future__ import annotations

from typing import Any

from ..utils import is_internal_issue_artifact, max_chars_from_env, sanitize_mentions, truncate_text
from ._base import GitHubSkillBase
from ._models import (
    DEFAULT_MAX_ISSUE_BODY_CHARS,
    AddLabelsArgs,
    CloseIssueArgs,
    CreateIssueArgs,
    EmptyArgs,
    ListOpenIssuesArgs,
    ReadIssueBodyArgs,
    ReopenIssueArgs,
    SearchIssuesArgs,
    UpdateIssueArgs,
)
from ._utils import _repo_label_names


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


class ReadIssueBody(GitHubSkillBase):
    name = "read_issue_body"
    description = (
        "Read the full body of a specific GitHub issue by number. "
        "Use this when you need to understand what an issue is about before commenting or acting. "
        "Pass issue_number=0 to read the current context issue."
    )
    args_model = ReadIssueBodyArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        issue = await self._fetch_visible_issue_thread(issue_number=issue_number, include_internal=args.include_internal)
        if isinstance(issue, str):
            return issue
        body = truncate_text(
            str(issue.get("body") or ""),
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_ISSUE_BODY_CHARS),
        )
        labels = ", ".join(
            lb.get("name", "") for lb in issue.get("labels", [])
        ) or "none"
        return "\n".join(
            [
                f"Issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}",
                f"Author: {(issue.get('user') or {}).get('login', 'unknown')}",
                f"Labels: {labels}",
                f"Body:\n{body}",
            ]
        )


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


class ReopenIssue(GitHubSkillBase):
    name = "reopen_issue"
    description = "Reopen a previously closed GitHub issue."
    args_model = ReopenIssueArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        await self._api.patch_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}",
            json_body={"state": "open"},
        )
        return f"Reopened issue #{issue_number}"


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


class ListOpenIssues(GitHubSkillBase):
    name = "list_open_issues"
    description = (
        "List issues in the current repository. "
        "Use state='open' (default) for active issues, 'closed' for completed ones, or 'all' for both. "
        "By default this hides internal bot-maintenance artifacts such as coordination and mind issues. "
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
            if not args.include_internal and is_internal_issue_artifact(issue):
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
        if not args.include_internal:
            items = [
                issue
                for issue in items
                if not is_internal_issue_artifact(issue)
                or self._is_current_thread(context, int(issue.get("number") or 0))
            ]
        if not items:
            return f"No results for: {args.query}"

        lines: list[str] = [f"Search results for '{args.query}' ({len(items)} visible):"]
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
