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
    CloseIssue,
    CommentOnPR,
    CreateIssue,
    DispatchWorkflow,
    ReadCodeDiff,
    ReadIssueMemory,
    ReadWorkflowRun,
    SearchRepoMemory,
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
        diff_tool = ReadCodeDiff(token="secret-token", client=client).get_tool_definition()
        create_tool = CreateIssue(token="secret-token", client=client).get_tool_definition()
        labels_tool = AddLabels(token="secret-token", client=client).get_tool_definition()
        close_tool = CloseIssue(token="secret-token", client=client).get_tool_definition()
        comment_tool = CommentOnPR(token="secret-token", client=client).get_tool_definition()
        dispatch_tool = DispatchWorkflow(token="secret-token", client=client).get_tool_definition()
        run_tool = ReadWorkflowRun(token="secret-token", client=client).get_tool_definition()
    finally:
        import asyncio

        asyncio.run(client.aclose())

    assert issue_tool["function"]["name"] == "read_issue_memory"
    assert issue_tool["function"]["parameters"]["type"] == "object"
    assert search_tool["function"]["parameters"]["properties"]["query"]["type"] == "string"
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
    assert body["body"] == "LGTM!"
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
