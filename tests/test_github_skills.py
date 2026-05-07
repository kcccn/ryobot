from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core.plugins import PluginEvent
from core.skills import clear_skill_context, set_skill_context
from platforms.github.skills import (
    AddLabels,
    ArchiveMemory,
    CloseIssue,
    CommentOnPR,
    CommitMemory,
    CreateBranch,
    CreateIssue,
    CreatePullRequest,
    DispatchWorkflow,
    ListFiles,
    ListOpenIssues,
    ListRepoLabels,
    ReadCodeDiff,
    ReadFile,
    ReadIssueMemory,
    ReadThreadComments,
    ReadWorkflowRun,
    RefineMemory,
    RetrieveMemory,
    RunCommand,
    SearchCode,
    SearchRepoContext,
    SearchRepoMemory,
    WriteFile,
)


def with_context() -> Any:
    event = PluginEvent(
        event_id="evt-1",
        message="hello",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=21,
        owner="acme",
        repo="widgets",
    )
    return set_skill_context(event=event, subconscious={"mode": "focus"})


def with_pr_context() -> Any:
    event = PluginEvent(
        event_id="evt-pr",
        message="hello",
        author="octocat",
        issue_id="2001",
        issue_number=12,
        comment_id=21,
        owner="acme",
        repo="widgets",
        is_pull_request=True,
    )
    return set_skill_context(event=event, subconscious={"mode": "focus"})


@pytest.mark.asyncio
async def test_read_issue_memory_uses_current_issue_context() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "number": 12,
                "title": "Bug in widget flow",
                "state": "open",
                "body": "Steps to reproduce",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = ReadIssueMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute()
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["url"].endswith("/repos/acme/widgets/issues/12")
    assert "Bug in widget flow" in result
    assert "Steps to reproduce" in result


@pytest.mark.asyncio
async def test_search_repo_memory_scopes_search_to_current_repo_and_respects_limit() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"number": 12, "title": "Current issue", "html_url": "https://github.test/12"},
                    {"number": 9, "title": "Similar bug", "html_url": "https://github.test/9"},
                    {"number": 8, "title": "Another bug", "html_url": "https://github.test/8"},
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = SearchRepoMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(query="widget failure", limit=1)
    finally:
        clear_skill_context(token)
        await client.aclose()

    query = parse_qs(urlparse(captured["url"]).query)["q"][0]
    assert "repo:acme/widgets" in query
    assert "widget failure" in query
    assert "Similar bug" in result
    assert "Current issue" not in result
    assert "Another bug" not in result


@pytest.mark.asyncio
async def test_commit_memory_creates_labeled_closed_issue_with_metadata() -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        requests.append((request.method, str(request.url), body))
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "bug"}])
        if request.method == "POST" and request.url.path.endswith("/labels"):
            return httpx.Response(201, json={"name": "🧠 memory"})
        if request.method == "POST" and request.url.path.endswith("/issues"):
            return httpx.Response(201, json={"number": 88, "title": "NPU user preference"})
        if request.method == "PATCH" and request.url.path.endswith("/issues/88"):
            return httpx.Response(200, json={"number": 88, "state": "closed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = CommitMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_pr_context()
    try:
        result = await skill.execute(
            title="NPU user preference",
            summary="月月鸟在 #486 PR 里持续关注 Ascend NPU 算子优化。",
            tags=["user:月月鸟", "module:ascend-npu"],
        )
    finally:
        clear_skill_context(token)
        await client.aclose()

    create_issue = next(body for method, url, body in requests if method == "POST" and url.endswith("/issues"))
    assert create_issue is not None
    assert create_issue["labels"] == ["🧠 memory"]
    assert "### 记忆摘要" in create_issue["body"]
    assert "<!-- ryo:memory:" in create_issue["body"]
    assert '"is_pull_request":true' in create_issue["body"]
    assert result == "Committed memory issue #88: NPU user preference"


@pytest.mark.asyncio
async def test_retrieve_memory_only_returns_active_memory_results() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"number": 30},
                        {"number": 31},
                    ]
                },
            )
        if path.endswith("/issues/30"):
            return httpx.Response(
                200,
                json={
                    "number": 30,
                    "title": "Ascend focus",
                    "body": "### 记忆摘要\n月月鸟关注 NPU 算子优化。\n\n---\n<!-- ryo:memory: {\"schema_version\":1,\"status\":\"active\",\"tags\":[\"user:月月鸟\",\"module:npu\"],\"updated_at\":\"2026-05-01T00:00:00+00:00\"} -->",
                    "labels": [{"name": "🧠 memory"}],
                    "updated_at": "2026-05-01T00:00:00Z",
                    "html_url": "https://github.test/30",
                },
            )
        if path.endswith("/issues/31"):
            return httpx.Response(
                200,
                json={
                    "number": 31,
                    "title": "Archived noise",
                    "body": "### 记忆摘要\n旧噪声。\n\n---\n<!-- ryo:memory: {\"schema_version\":1,\"status\":\"archived\",\"tags\":[\"noise\"],\"updated_at\":\"2026-04-01T00:00:00+00:00\"} -->",
                    "labels": [{"name": "🧠 memory"}, {"name": "🗑️ deleted"}],
                    "updated_at": "2026-04-01T00:00:00Z",
                    "html_url": "https://github.test/31",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = RetrieveMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(query="月月鸟 NPU", candidate_limit=20, limit=3)
    finally:
        clear_skill_context(token)
        await client.aclose()

    query = parse_qs(urlparse(captured["url"]).query)["q"][0]
    assert 'label:"🧠 memory"' in query
    assert "is:closed" in query
    assert "#30" in result
    assert "Ascend focus" in result
    assert "#31" not in result


@pytest.mark.asyncio
async def test_refine_memory_updates_existing_body_and_metadata() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "🧠 memory"}])
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 45,
                    "title": "Old title",
                    "body": "### 记忆摘要\n旧总结\n\n---\n<!-- ryo:memory: {\"schema_version\":1,\"status\":\"active\",\"tags\":[\"old\"],\"created_at\":\"2026-05-01T00:00:00+00:00\",\"updated_at\":\"2026-05-01T00:00:00+00:00\"} -->",
                    "labels": [{"name": "🧠 memory"}],
                },
            )
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"number": 45, "title": captured["body"]["title"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = RefineMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(
            memory_issue_number=45,
            title="New title",
            summary="新总结",
            tags=["user:moonbird"],
        )
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["body"]["title"] == "New title"
    assert captured["body"]["state"] == "closed"
    assert "新总结" in captured["body"]["body"]
    assert '"tags":["user:moonbird"]' in captured["body"]["body"]
    assert result == "Refined memory issue #45: New title"


@pytest.mark.asyncio
async def test_archive_memory_removes_memory_label_and_marks_deleted() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "🧠 memory"}])
        if request.method == "POST" and path.endswith("/labels"):
            return httpx.Response(201, json={"name": "🗑️ deleted"})
        if request.method == "GET" and path.endswith("/issues/55"):
            return httpx.Response(
                200,
                json={
                    "number": 55,
                    "title": "Stale memory",
                    "body": "### 记忆摘要\n旧总结\n\n---\n<!-- ryo:memory: {\"schema_version\":1,\"status\":\"active\",\"tags\":[\"legacy\"],\"created_at\":\"2026-05-01T00:00:00+00:00\",\"updated_at\":\"2026-05-01T00:00:00+00:00\"} -->",
                    "labels": [{"name": "🧠 memory"}, {"name": "legacy"}],
                    "created_at": "2026-05-01T00:00:00Z",
                },
            )
        if request.method == "PATCH":
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"number": 55, "title": "Stale memory"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ArchiveMemory(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(memory_issue_number=55, reason="no longer useful")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["body"]["labels"] == ["legacy", "🗑️ deleted"]
    assert '"status":"archived"' in captured["body"]["body"]
    assert '"archive_reason":"no longer useful"' in captured["body"]["body"]
    assert result == "Archived memory issue #55: Stale memory"


@pytest.mark.asyncio
async def test_search_repo_context_excludes_memory_and_deleted_labels() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {
                        "number": 10,
                        "title": "Open bug",
                        "state": "open",
                        "labels": [{"name": "bug"}],
                        "html_url": "https://github.test/10",
                        "updated_at": "2026-05-01T00:00:00Z",
                    },
                    {
                        "number": 11,
                        "title": "Fix bug",
                        "state": "open",
                        "labels": [{"name": "enhancement"}],
                        "pull_request": {"url": "https://api.github.test/pulls/11"},
                        "html_url": "https://github.test/11",
                        "updated_at": "2026-05-02T00:00:00Z",
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = SearchRepoContext(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(query="npu bug", limit=10, kind="all")
    finally:
        clear_skill_context(token)
        await client.aclose()

    query = parse_qs(urlparse(captured["url"]).query)["q"][0]
    assert '-label:"🧠 memory"' in query
    assert '-label:"🗑️ deleted"' in query
    assert "[Issue]" in result
    assert "[PR]" in result


@pytest.mark.asyncio
async def test_read_code_diff_requests_diff_media_type() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["accept"] = request.headers["Accept"]
        captured["url"] = str(request.url)
        return httpx.Response(200, text="diff --git a/a.py b/a.py")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = ReadCodeDiff(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(pr_number=55)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["accept"] == "application/vnd.github.v3.diff"
    assert captured["url"].endswith("/repos/acme/widgets/pulls/55")
    assert result == "diff --git a/a.py b/a.py"


@pytest.mark.asyncio
async def test_read_code_diff_truncates_large_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_MAX_DIFF_CHARS", "8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="diff-content")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = ReadCodeDiff(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(pr_number=55)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert result.startswith("diff-con")
    assert "[truncated:" in result


def test_github_skills_expose_complete_tool_definitions() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="https://api.github.test",
    )
    try:
        issue_tool = ReadIssueMemory(token="secret-token", client=client).get_tool_definition()
        search_tool = SearchRepoMemory(token="secret-token", client=client).get_tool_definition()
        commit_memory_tool = CommitMemory(token="secret-token", client=client).get_tool_definition()
        retrieve_memory_tool = RetrieveMemory(token="secret-token", client=client).get_tool_definition()
        refine_memory_tool = RefineMemory(token="secret-token", client=client).get_tool_definition()
        archive_memory_tool = ArchiveMemory(token="secret-token", client=client).get_tool_definition()
        repo_context_tool = SearchRepoContext(token="secret-token", client=client).get_tool_definition()
        diff_tool = ReadCodeDiff(token="secret-token", client=client).get_tool_definition()
        create_tool = CreateIssue(token="secret-token", client=client).get_tool_definition()
        labels_tool = AddLabels(token="secret-token", client=client).get_tool_definition()
        close_tool = CloseIssue(token="secret-token", client=client).get_tool_definition()
        comment_tool = CommentOnPR(token="secret-token", client=client).get_tool_definition()
        dispatch_tool = DispatchWorkflow(token="secret-token", client=client).get_tool_definition()
        run_tool = ReadWorkflowRun(token="secret-token", client=client).get_tool_definition()
        labels_catalog_tool = ListRepoLabels(token="secret-token", client=client).get_tool_definition()
        thread_tool = ReadThreadComments(token="secret-token", client=client).get_tool_definition()
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert issue_tool["function"]["name"] == "read_issue_memory"
    assert issue_tool["function"]["parameters"]["type"] == "object"
    assert search_tool["function"]["parameters"]["properties"]["query"]["type"] == "string"
    assert commit_memory_tool["function"]["name"] == "commit_memory"
    assert retrieve_memory_tool["function"]["parameters"]["properties"]["candidate_limit"]["type"] == "integer"
    assert refine_memory_tool["function"]["parameters"]["properties"]["memory_issue_number"]["type"] == "integer"
    assert archive_memory_tool["function"]["parameters"]["properties"]["reason"]["type"] == "string"
    assert repo_context_tool["function"]["name"] == "search_repo_context"
    assert diff_tool["function"]["parameters"]["properties"]["pr_number"]["type"] == "integer"
    assert create_tool["function"]["name"] == "create_issue"
    assert create_tool["function"]["parameters"]["properties"]["title"]["type"] == "string"
    assert labels_tool["function"]["parameters"]["properties"]["labels"]["type"] == "array"
    assert close_tool["function"]["parameters"]["properties"]["issue_number"]["type"] == "integer"
    assert comment_tool["function"]["parameters"]["properties"]["body"]["type"] == "string"
    assert dispatch_tool["function"]["name"] == "dispatch_workflow"
    assert dispatch_tool["function"]["parameters"]["properties"]["workflow_id"]["type"] == "string"
    assert run_tool["function"]["name"] == "read_workflow_run"
    assert run_tool["function"]["parameters"]["properties"]["run_id"]["type"] == "integer"
    assert labels_catalog_tool["function"]["name"] == "list_repo_labels"
    assert thread_tool["function"]["name"] == "read_thread_comments"


@pytest.mark.asyncio
async def test_create_issue_posts_to_repo_issues_endpoint() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            201,
            json={"number": 42, "title": "Refactor auth module"},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = CreateIssue(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(title="Refactor auth module", body="Needs cleanup", labels=["tech-debt"])
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/acme/widgets/issues")
    body = json.loads(captured["body"])
    assert body["title"] == "Refactor auth module"
    assert body["body"] == "Needs cleanup"
    assert body["labels"] == ["tech-debt"]
    assert "Created issue #42" in result
    assert "Refactor auth module" in result


@pytest.mark.asyncio
async def test_add_labels_uses_context_issue_when_zero() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "bug"}])
        return httpx.Response(200, json={"labels": [{"name": "bug"}]})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = AddLabels(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(labels=["bug"], issue_number=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/acme/widgets/issues/12/labels")
    body = json.loads(captured["body"])
    assert body["labels"] == ["bug"]
    assert "Added labels ['bug'] to issue #12" == result


@pytest.mark.asyncio
async def test_add_labels_uses_explicit_issue_number() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "enhancement"}])
        return httpx.Response(200, json={"labels": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = AddLabels(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(labels=["enhancement"], issue_number=99)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["url"].endswith("/repos/acme/widgets/issues/99/labels")
    assert "Added labels ['enhancement'] to issue #99" == result


@pytest.mark.asyncio
async def test_add_labels_rejects_missing_repo_label() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"name": "bug"}])
        raise AssertionError("missing labels should not be posted")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = AddLabels(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(labels=["missing"], issue_number=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Labels do not exist in repo: missing" == result


@pytest.mark.asyncio
async def test_close_issue_uses_patch_and_context_when_zero() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"state": "closed"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = CloseIssue(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(issue_number=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/repos/acme/widgets/issues/12")
    body = json.loads(captured["body"])
    assert body["state"] == "closed"
    assert "Closed issue #12" == result


@pytest.mark.asyncio
async def test_close_issue_uses_explicit_issue_number() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"state": "closed"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = CloseIssue(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(issue_number=77)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["url"].endswith("/repos/acme/widgets/issues/77")
    assert "Closed issue #77" == result


@pytest.mark.asyncio
async def test_comment_on_pr_uses_context_when_pr_number_is_zero() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(201, json={"id": 500})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = CommentOnPR(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(body="LGTM!", pr_number=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/acme/widgets/issues/12/comments")
    body = json.loads(captured["body"])
    assert body["body"] == "**bot**\n\nLGTM!"
    assert "Commented on PR #12" == result


@pytest.mark.asyncio
async def test_comment_on_pr_uses_explicit_pr_number() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(201, json={"id": 501})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = CommentOnPR(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(body="Ship it!", pr_number=200)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["url"].endswith("/repos/acme/widgets/issues/200/comments")
    assert "Commented on PR #200" == result


@pytest.mark.asyncio
async def test_dispatch_workflow_posts_to_dispatches_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_ALLOWED_WORKFLOWS", "ci.yml")
    monkeypatch.setenv("RYOBOT_ALLOWED_WORKFLOW_REFS", "feature/branch")
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = DispatchWorkflow(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(workflow_id="ci.yml", ref="feature/branch")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/acme/widgets/actions/workflows/ci.yml/dispatches")
    body = json.loads(captured["body"])
    assert body["ref"] == "feature/branch"
    assert body["inputs"] == {}
    assert "Dispatched workflow 'ci.yml'" in result
    assert "read_workflow_run" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_passes_inputs_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_ALLOWED_WORKFLOWS", "test.yml")
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = DispatchWorkflow(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        await skill.execute(workflow_id="test.yml", ref="main", inputs={"suite": "unit"})
    finally:
        clear_skill_context(token)
        await client.aclose()

    body = json.loads(captured["body"])
    assert body["inputs"] == {"suite": "unit"}


@pytest.mark.asyncio
async def test_dispatch_workflow_is_disabled_without_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dispatch endpoint should not be called")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = DispatchWorkflow(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(workflow_id="ci.yml", ref="main")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Workflow dispatch is disabled" in result


@pytest.mark.asyncio
async def test_dispatch_workflow_rejects_disallowed_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_ALLOWED_WORKFLOWS", "ci.yml")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dispatch endpoint should not be called")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = DispatchWorkflow(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(workflow_id="ci.yml", ref="feature/branch")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "ref 'feature/branch' is not allowed" in result


@pytest.mark.asyncio
async def test_read_workflow_run_by_run_id() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "created_at": "2025-01-01T00:00:00Z",
                "html_url": "https://github.test/acme/widgets/actions/runs/999",
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = ReadWorkflowRun(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(run_id=999, workflow_id="")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["url"].endswith("/repos/acme/widgets/actions/runs/999")
    assert "Status: completed" in result
    assert "Conclusion: success" in result
    assert "https://github.test/acme/widgets/actions/runs/999" in result


@pytest.mark.asyncio
async def test_read_workflow_run_latest_by_workflow_id() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "total_count": 3,
                "workflow_runs": [
                    {
                        "name": "CI",
                        "status": "in_progress",
                        "conclusion": None,
                        "created_at": "2025-06-01T12:00:00Z",
                        "html_url": "https://github.test/acme/widgets/actions/runs/42",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.test",
    )
    skill = ReadWorkflowRun(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(workflow_id="ci.yml", run_id=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "/repos/acme/widgets/actions/workflows/ci.yml/runs" in captured["url"]
    assert "per_page=1" in captured["url"]
    assert "Status: in_progress" in result
    assert "Conclusion: N/A" in result


@pytest.mark.asyncio
async def test_read_workflow_run_returns_error_when_no_identifier() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="https://api.github.test",
    )
    skill = ReadWorkflowRun(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(workflow_id="", run_id=0)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Must provide either workflow_id or run_id." == result


# ---- list_open_issues ----


@pytest.mark.asyncio
async def test_list_open_issues_returns_open_issues() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "Bug fix",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "user": {"login": "dev1"},
                    "updated_at": "2025-01-01T00:00:00Z",
                    "html_url": "https://github.test/acme/widgets/issues/1",
                },
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ListOpenIssues(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute()
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "/repos/acme/widgets/issues" in captured["url"]
    assert "#1: Bug fix [open]" in result
    assert "labels: bug" in result


@pytest.mark.asyncio
async def test_list_open_issues_skips_pull_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"number": 1, "title": "Issue", "state": "open", "labels": [], "user": {"login": "a"}, "updated_at": "", "html_url": ""},
                {"number": 2, "title": "PR", "pull_request": {}, "labels": [], "user": {"login": "b"}, "updated_at": "", "html_url": ""},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ListOpenIssues(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute()
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "#1: Issue" in result
    assert "PR" not in result


@pytest.mark.asyncio
async def test_list_repo_labels_returns_available_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/repos/acme/widgets/labels?per_page=100&page=1")
        return httpx.Response(
            200,
            json=[
                {"name": "bug", "description": "Something is broken", "color": "d73a4a"},
                {"name": "enhancement", "description": "", "color": "a2eeef"},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ListRepoLabels(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute()
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "bug (#d73a4a): Something is broken" in result
    assert "enhancement (#a2eeef)" in result


@pytest.mark.asyncio
async def test_read_thread_comments_reads_issue_comments_and_review_comments() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/issues/77/comments" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "body": "issue comment",
                        "user": {"login": "human"},
                        "created_at": "2026-01-01T00:00:02Z",
                    }
                ],
            )
        if "/pulls/77/comments" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "body": "review comment",
                        "user": {"login": "reviewer"},
                        "created_at": "2026-01-01T00:00:01Z",
                        "path": "main.py",
                        "line": 10,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ReadThreadComments(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_pr_context()
    try:
        result = await skill.execute(issue_number=77, include_review_comments=True)
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert any("/issues/77/comments" in call for call in calls)
    assert any("/pulls/77/comments" in call for call in calls)
    assert "reviewer at 2026-01-01T00:00:01Z [main.py:10]: review comment" in result
    assert "human at 2026-01-01T00:00:02Z: issue comment" in result


# ---- list_files ----


@pytest.mark.asyncio
async def test_list_files_returns_directory_contents() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json=[
                {"name": "core", "type": "dir", "size": 0},
                {"name": "main.py", "type": "file", "size": 1024},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ListFiles(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute()
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "/repos/acme/widgets/contents/" in captured["url"]
    assert "core" in result
    assert "main.py" in result


# ---- read_file ----


@pytest.mark.asyncio
async def test_read_file_returns_decoded_content() -> None:
    import base64

    content = "def hello():\n    return 'world'\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "hello.py",
                "type": "file",
                "size": len(content),
                "content": encoded,
                "encoding": "base64",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ReadFile(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(path="src/hello.py")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "def hello():" in result
    assert "world" in result


@pytest.mark.asyncio
async def test_read_file_rejects_directories() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "x", "type": "file", "size": 0}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = ReadFile(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(path="src")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Cannot read" in result


# ---- search_code ----


@pytest.mark.asyncio
async def test_search_code_returns_results() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "total_count": 2,
                "items": [
                    {"path": "src/main.py", "html_url": "https://github.test/acme/widgets/blob/main/src/main.py", "repository": {"full_name": "acme/widgets"}},
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = SearchCode(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(query="hello")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "hello" in captured["url"]
    assert "widgets" in captured["url"]
    assert "src/main.py" in result


@pytest.mark.asyncio
async def test_search_code_returns_empty_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "items": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = SearchCode(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(query="nonexistent")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "No code results found" in result


# ---- write_file ----


@pytest.mark.asyncio
async def test_write_file_creates_new_file() -> None:
    import base64

    captured: dict[str, Any] = {}
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_order.append(request.method)
        if request.method == "GET" and "/contents/" not in str(request.url):
            # Repo info request
            return httpx.Response(200, json={"default_branch": "main"})
        if request.method == "GET" and "/contents/" in str(request.url):
            # Existing file check — file doesn't exist
            return httpx.Response(404)
        captured["method"] = request.method
        captured["url"] = str(request.url)
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(
            201,
            json={
                "content": {"html_url": "https://github.test/acme/widgets/blob/feat/new-thing/new.py"},
                "commit": {"sha": "abc123"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = WriteFile(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(
            path="new.py", content="print('hi')", message="Add new.py", branch="feat/new-thing",
        )
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert captured["method"] == "PUT"
    assert base64.b64decode(captured["body"]["content"]).decode("utf-8") == "print('hi')"
    assert captured["body"]["message"] == "Add new.py"
    assert captured["body"]["branch"] == "feat/new-thing"
    assert "Created file" in result


@pytest.mark.asyncio
async def test_write_file_updates_existing_file() -> None:
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            # First call: repo info
            return httpx.Response(200, json={"default_branch": "main"})
        if call_count[0] == 2:
            # Second call: get existing file
            return httpx.Response(200, json={"sha": "oldsha", "type": "file", "size": 10})
        # Third call: PUT with sha
        return httpx.Response(
            200,
            json={
                "content": {"html_url": "https://github.test/acme/widgets/blob/feat/update/existing.py"},
                "commit": {"sha": "def456"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = WriteFile(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(
            path="existing.py", content="updated", message="Update", branch="feat/update",
        )
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert call_count[0] == 3
    assert "Updated file" in result


@pytest.mark.asyncio
async def test_write_file_refuses_default_branch() -> None:
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            # Repo info request
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(500)  # should not reach here

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = WriteFile(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(
            path="foo.py", content="bar", message="Should fail", branch="main",
        )
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Refusing to write directly to the default branch" in result


# ---- create_branch ----


@pytest.mark.asyncio
async def test_create_branch_creates_new_branch() -> None:
    captured: dict[str, Any] = {}
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        call_order.append(url)
        body = json.loads(request.content) if request.content else None
        captured.setdefault("bodies", []).append(body)

        if url.endswith("/repos/acme/widgets") and request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if "/git/refs/heads/main" in url:
            return httpx.Response(200, json={"object": {"sha": "basesha123"}})
        if "/git/refs" in url:
            return httpx.Response(201, json={"url": "https://api.github.test/repos/acme/widgets/git/refs/heads/feature-x"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = CreateBranch(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(branch="feature-x")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Created branch 'feature-x'" in result


# ---- create_pull_request ----


@pytest.mark.asyncio
async def test_create_pull_request_creates_pr() -> None:
    captured: dict[str, Any] = {}
    call_order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        call_order.append(url)
        body = json.loads(request.content) if request.content else None
        captured.setdefault("bodies", []).append(body)

        if url.endswith("/repos/acme/widgets") and request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if "/pulls" in url:
            return httpx.Response(
                201,
                json={
                    "number": 42,
                    "title": "Add feature X",
                    "html_url": "https://github.test/acme/widgets/pull/42",
                    "state": "open",
                },
            )
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    skill = CreatePullRequest(token="secret-token", client=client, api_base_url="https://api.github.test")
    token = with_context()
    try:
        result = await skill.execute(title="Add feature X", head="feature-x")
    finally:
        clear_skill_context(token)
        await client.aclose()

    assert "Created PR #42" in result
    assert "https://github.test/acme/widgets/pull/42" in result


@pytest.mark.asyncio
async def test_run_command_allows_default_pytest_command() -> None:
    client = httpx.AsyncClient(base_url="https://api.github.test")
    skill = RunCommand(token="secret-token", client=client, api_base_url="https://api.github.test")
    try:
        result = await skill.execute(command="python -m pytest --version")
    finally:
        await client.aclose()
    assert "Exit code: 0" in result
    assert "pytest" in result


@pytest.mark.asyncio
async def test_run_command_rejects_disallowed_command() -> None:
    client = httpx.AsyncClient(base_url="https://api.github.test")
    skill = RunCommand(token="secret-token", client=client, api_base_url="https://api.github.test")
    try:
        result = await skill.execute(command="echo hello")
    finally:
        await client.aclose()
    assert "Command is not allowed" in result


@pytest.mark.asyncio
async def test_run_command_rejects_shell_metacharacters() -> None:
    client = httpx.AsyncClient(base_url="https://api.github.test")
    skill = RunCommand(token="secret-token", client=client, api_base_url="https://api.github.test")
    try:
        result = await skill.execute(command="python -m pytest --version && echo leaked")
    finally:
        await client.aclose()
    assert "Shell metacharacters are not allowed" in result


@pytest.mark.asyncio
async def test_run_command_rejects_broad_python_c_by_default() -> None:
    client = httpx.AsyncClient(base_url="https://api.github.test")
    skill = RunCommand(token="secret-token", client=client, api_base_url="https://api.github.test")
    try:
        result = await skill.execute(command='python -c "print(1)"')
    finally:
        await client.aclose()
    assert "Command is not allowed" in result


@pytest.mark.asyncio
async def test_run_command_strips_secret_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_ALLOWED_COMMANDS", "python -c")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-gh-token")
    client = httpx.AsyncClient(base_url="https://api.github.test")
    skill = RunCommand(token="secret-token", client=client, api_base_url="https://api.github.test")
    try:
        result = await skill.execute(command='python -c "print(__import__(\'os\').environ.get(\'GITHUB_TOKEN\'))"')
    finally:
        await client.aclose()
    assert "Exit code: 0" in result
    assert "None" in result
    assert "secret-gh-token" not in result
