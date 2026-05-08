from __future__ import annotations

from typing import Any

import httpx

from ..utils import max_chars_from_env, sanitize_mentions, truncate_text
from ._base import GitHubSkillBase
from ._models import (
    DEFAULT_MAX_ISSUE_BODY_CHARS,
    CommentOnThreadArgs,
    ReadThreadCommentsArgs,
    ReadThreadContextArgs,
    ReadThreadMetaArgs,
)
from ._utils import _comment_location


class ReadThreadContext(GitHubSkillBase):
    name = "read_thread_context"
    description = (
        "Read the current issue or pull-request thread context. "
        "This only reads the current thread; it is not the bot's live mind issue and not the long-term memory database."
    )
    args_model = ReadThreadContextArgs

    async def execute(self, **kwargs: Any) -> str:
        context = self._require_context()
        issue_number = int(context.get("issue_number") or 0)
        issue = await self._fetch_visible_issue_thread(issue_number=issue_number, include_internal=True)
        if isinstance(issue, str):
            return issue
        body = truncate_text(
            str(issue.get("body") or ""),
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_ISSUE_BODY_CHARS),
        )
        labels = ", ".join(lb.get("name", "") for lb in issue.get("labels", [])) or "none"
        return "\n".join(
            [
                "Thread context (current issue/PR, not bot memory):",
                f"Issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}",
                f"Author: {(issue.get('user') or {}).get('login', 'unknown')}",
                f"Labels: {labels}",
                f"Body: {body}",
            ]
        )


class ReadIssueMemory(GitHubSkillBase):
    name = "read_issue_memory"
    description = (
        "Deprecated alias for current thread context. "
        "This reads the current issue or PR thread; it does NOT read the bot's live mind issue "
        "and it does NOT read the long-term `🧠 memory` database."
    )
    args_model = ReadThreadContextArgs

    async def execute(self, **kwargs: Any) -> str:
        context = self._require_context()
        issue_number = int(context.get("issue_number") or 0)
        issue = await self._fetch_visible_issue_thread(issue_number=issue_number, include_internal=True)
        if isinstance(issue, str):
            return (
                "No current thread in repo-scan. read_issue_memory is unavailable here; "
                "use retrieve_memory or search_repo_context instead."
            )
        body = truncate_text(
            str(issue.get("body") or ""),
            max_chars_from_env("RYOBOT_MAX_HISTORY_COMMENT_CHARS", DEFAULT_MAX_ISSUE_BODY_CHARS),
        )
        labels = ", ".join(lb.get("name", "") for lb in issue.get("labels", [])) or "none"
        context_result = "\n".join(
            [
                "Thread context (current issue/PR, not bot memory):",
                f"Issue #{issue['number']}: {issue['title']}",
                f"State: {issue['state']}",
                f"Author: {(issue.get('user') or {}).get('login', 'unknown')}",
                f"Labels: {labels}",
                f"Body: {body}",
            ]
        )
        return (
            "Deprecated alias notice: read_issue_memory reads the current thread context only. "
            "It is not the bot's live mind issue and not the `🧠 memory` long-term memory DB.\n"
            f"{context_result}"
        )


class ReadThreadMeta(GitHubSkillBase):
    name = "read_thread_meta"
    description = (
        "Read concise metadata for a specific issue or pull request by number. "
        "For pull requests, this includes merged status, merged_at, base/head branches, and draft status. "
        "Use this before broader searches when you need a low-ambiguity status check."
    )
    args_model = ReadThreadMetaArgs

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        issue_number = args.issue_number if args.issue_number else context["issue_number"]
        issue = await self._fetch_visible_issue_thread(issue_number=issue_number, include_internal=args.include_internal)
        if isinstance(issue, str):
            return issue
        labels = ", ".join(lb.get("name", "") for lb in issue.get("labels", [])) or "none"
        is_pull_request = "pull_request" in issue
        lines = [
            f"Thread #{issue['number']}: {issue['title']}",
            f"Type: {'PR' if is_pull_request else 'Issue'}",
            f"State: {issue.get('state', '')}",
            f"Author: {(issue.get('user') or {}).get('login', 'unknown')}",
            f"Labels: {labels}",
            f"Created: {issue.get('created_at', '')}",
            f"Updated: {issue.get('updated_at', '')}",
            f"Closed: {issue.get('closed_at', '') or 'N/A'}",
            f"URL: {issue.get('html_url', '')}",
        ]
        if not is_pull_request:
            return "\n".join(lines)

        pr = await self._api.get_json(
            f"/repos/{context['owner']}/{context['repo']}/pulls/{issue_number}"
        )
        lines.extend(
            [
                f"Draft: {bool(pr.get('draft'))}",
                f"Merged: {bool(pr.get('merged'))}",
                f"Merged at: {pr.get('merged_at') or 'N/A'}",
                f"Base: {(pr.get('base') or {}).get('ref', '?')}",
                f"Head: {(pr.get('head') or {}).get('ref', '?')}",
            ]
        )
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
        issue = await self._fetch_visible_issue_thread(issue_number=issue_number, include_internal=args.include_internal)
        if isinstance(issue, str):
            return issue
        comments = await self._fetch_paginated(
            f"/repos/{context['owner']}/{context['repo']}/issues/{issue_number}/comments",
            params={"per_page": 100, "sort": "created", "direction": "asc"},
        )
        if args.include_review_comments:
            try:
                review_comments = await self._fetch_paginated(
                    f"/repos/{context['owner']}/{context['repo']}/pulls/{issue_number}/comments",
                    params={"per_page": 100, "sort": "created", "direction": "asc"},
                )
                comments = [*comments, *review_comments]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (403, 404):
                    pass
                else:
                    raise
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


class CommentOnThread(GitHubSkillBase):
    name = "comment_on_thread"
    description = "Post a comment on a GitHub issue or pull-request thread."
    args_model = CommentOnThreadArgs
    mutates_state = True

    async def execute(self, **kwargs: Any) -> str:
        args = self.args_model.model_validate(kwargs)
        context = self._require_context()
        thread_number = args.thread_number if args.thread_number else context["issue_number"]
        await self._api.post_json(
            f"/repos/{context['owner']}/{context['repo']}/issues/{thread_number}/comments",
            json_body={"body": self._bot_prefix() + sanitize_mentions(args.body)},
        )
        return f"Commented on thread #{thread_number}"
