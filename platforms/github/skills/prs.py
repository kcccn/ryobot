from __future__ import annotations

from typing import Any

import httpx

from ..utils import max_chars_from_env, sanitize_mentions, truncate_text
from ._base import GitHubSkillBase
from ._models import (
    DEFAULT_MAX_DIFF_CHARS,
    CommentOnPRArgs,
    CreatePRReviewArgs,
    CreatePullRequestArgs,
    ListOpenPullRequestsArgs,
    MergePullRequestArgs,
    ReadCodeDiffArgs,
)


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


class CommentOnPR(GitHubSkillBase):
    name = "comment_on_pr"
    description = "Backward-compatible alias for thread comments. Posts on an issue or pull-request thread."
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
        return f"Commented on thread #{pr_number}"


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
        effective_event = args.event
        if args.event == "REQUEST_CHANGES":
            pr = await self._api.get_json(
                f"/repos/{context['owner']}/{context['repo']}/pulls/{args.pr_number}",
            )
            pr_author = str((pr.get("user") or {}).get("login") or "")
            viewer_login = await self._current_authenticated_login()
            if pr_author and viewer_login and pr_author == viewer_login:
                effective_event = "COMMENT"

        review_body: dict[str, Any] = {
            "event": effective_event,
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
        if effective_event != args.event:
            return (
                f"GitHub disallows REQUEST_CHANGES on self-authored PRs; submitted {effective_event} instead.\n"
                f"State: {result.get('state', '?')}\n"
                f"ID: {result.get('id', '?')}"
            )
        return (
            f"Submitted {effective_event} review on PR #{args.pr_number}\n"
            f"State: {result.get('state', '?')}\n"
            f"ID: {result.get('id', '?')}"
        )


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
