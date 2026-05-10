from __future__ import annotations

import re

from pydantic import BaseModel, Field


class EmptyArgs(BaseModel):
    pass


class ReadThreadContextArgs(BaseModel):
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
    include_internal: bool = False


class ReadCodeDiffArgs(BaseModel):
    pr_number: int = Field(ge=1)


class CreateIssueArgs(BaseModel):
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)


class AddLabelsArgs(BaseModel):
    labels: list[str]
    issue_number: int = 0


class ReadIssueBodyArgs(BaseModel):
    issue_number: int = Field(default=0, description="Issue number to read (0 = current context issue)")
    include_internal: bool = False


class ReadThreadMetaArgs(BaseModel):
    issue_number: int = Field(default=0, description="Issue or pull request number to inspect (0 = current context thread)")
    include_internal: bool = False


class ReadThreadCommentsArgs(BaseModel):
    issue_number: int = 0
    include_review_comments: bool = True
    include_internal: bool = False


class CloseIssueArgs(BaseModel):
    issue_number: int = 0


class SearchIssuesArgs(BaseModel):
    query: str = Field(description="Search query (same syntax as GitHub issue search)")
    limit: int = Field(default=10, ge=1, le=30)
    include_internal: bool = False


class UpdateIssueArgs(BaseModel):
    issue_number: int = Field(description="Issue number to update")
    title: str = Field(default="", description="New title (empty to keep unchanged)")
    body: str = Field(default="", description="New body (empty to keep unchanged)")


class ReopenIssueArgs(BaseModel):
    issue_number: int = 0


class CommentOnThreadArgs(BaseModel):
    thread_number: int = 0
    body: str


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
    include_internal: bool = False


class ListFilesArgs(BaseModel):
    path: str = Field(default="", description="Directory path relative to repo root, empty for root")
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for default branch)")
    limit: int = Field(default=30, ge=1, le=100)


class GetProjectTreeArgs(BaseModel):
    max_depth: int = Field(default=4, ge=1, le=8)
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for contextual default)")


class FindFilePathsArgs(BaseModel):
    keyword: str = Field(description="Case-insensitive path keyword to search for")
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for contextual default)")


class SearchSymbolArgs(BaseModel):
    symbol_name: str = Field(description="Python symbol name to locate")
    ref: str = Field(default="", description="Branch, tag, or commit SHA (empty for contextual default)")


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


class ReplaceInFileArgs(BaseModel):
    path: str = Field(description="File path relative to repo root")
    old_str: str = Field(description="Exact text to find and replace — must be unique in the file")
    new_str: str = Field(description="Replacement text")
    message: str = Field(default="Replace text in file", description="Commit message")
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


class ListOpenPullRequestsArgs(BaseModel):
    state: str = Field(default="open")
    sort: str = Field(default="updated")
    direction: str = Field(default="desc")
    limit: int = Field(default=10, ge=1, le=30)


class ReadWorkflowRunArgs(BaseModel):
    workflow_id: str = ""
    run_id: int = 0


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


class RunCommandArgs(BaseModel):
    command: str = Field(..., description="Shell command to execute. Working directory is the repository root.")


# --- constants ---

DEFAULT_MAX_DIFF_CHARS = 50000
DEFAULT_MAX_ISSUE_BODY_CHARS = 12000
MEMORY_LABEL = "🧠 memory"
DELETED_MEMORY_LABEL = "🗑️ deleted"
MEMORY_SCHEMA_VERSION = 1
_MEMORY_MARKER_RE = re.compile(r"<!--\s*ryo:memory:\s*(\{.*?\})\s*-->", re.DOTALL)
