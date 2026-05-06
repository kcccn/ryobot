from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.plugins import PluginEvent
from platforms.github.plugin import GitHubPlugin


def build_issue_comment_payload(*, body: str = "hello", comment_id: int = 99) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"id": 1001, "number": 12},
        "comment": {
            "id": comment_id,
            "body": body,
            "user": {"login": "octocat"},
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def build_plugin(
    handler: httpx.MockTransport | None = None,
    *,
    token: str = "secret-token",
    identity: str = "architect",
) -> GitHubPlugin:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://api.github.test",
    )
    return GitHubPlugin(token=token, client=client, api_base_url="https://api.github.test", identity=identity)


def test_parse_event_normalizes_issue_comment_payload() -> None:
    plugin = build_plugin()

    event = plugin.parse_event(build_issue_comment_payload(body="Need help"))

    assert event == PluginEvent(
        event_id="github:acme/widgets:issue:12:comment:99",
        message="Need help",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=99,
        owner="acme",
        repo="widgets",
    )


@pytest.mark.asyncio
async def test_send_reply_posts_comment_with_hidden_state_marker() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["accept"] = request.headers["Accept"]
        captured["auth"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"id": 123})

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_issue_comment_payload())

    await plugin.send_reply(event, "Visible reply", {"mode": "reflective"})
    await plugin.aclose()

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/acme/widgets/issues/12/comments")
    assert captured["accept"] == "application/vnd.github+json"
    assert captured["auth"] == "Bearer secret-token"
    assert captured["payload"]["body"].endswith(
        '<!-- ryo:architect: {"mode":"reflective"} -->'
    )


@pytest.mark.asyncio
async def test_fetch_history_extracts_latest_valid_subconscious_and_skips_trigger_comment() -> None:
    comments = [
        {
            "id": 1,
            "body": "first user comment",
            "user": {"login": "human"},
        },
        {
            "id": 2,
            "body": 'RyoBot reply\n<!-- ryo:architect: {"mode":"draft"} -->',
            "user": {"login": "ryobot"},
        },
        {
            "id": 99,
            "body": "current inbound comment",
            "user": {"login": "octocat"},
        },
        {
            "id": 3,
            "body": 'new RyoBot reply\n<!-- ryo:architect: {"mode":"final","step":2} -->',
            "user": {"login": "ryobot"},
        },
        {
            "id": 4,
            "body": 'broken marker\n<!-- ryo:architect: not-json -->',
            "user": {"login": "ryobot"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_issue_comment_payload(comment_id=99))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert snapshot.messages == [
        {"role": "user", "content": "first user comment"},
        {"role": "assistant", "content": "RyoBot reply"},
        {"role": "assistant", "content": "new RyoBot reply"},
        {"role": "assistant", "content": "broken marker"},
    ]
    assert snapshot.subconscious == {"mode": "final", "step": 2}


def test_state_pattern_matches_own_identity_marker() -> None:
    plugin = build_plugin(identity="architect")
    body = 'hello\n<!-- ryo:architect: {"mode":"focus"} -->'
    match = plugin._state_pattern.search(body)

    assert match is not None
    assert match.group("payload") == '{"mode":"focus"}'


def test_state_pattern_ignores_other_identity_marker() -> None:
    plugin = build_plugin(identity="architect")
    body = 'hello\n<!-- ryo:reviewer: {"mode":"focus"} -->'
    match = plugin._state_pattern.search(body)

    assert match is None


def build_issue_payload(*, action: str = "opened", number: int = 12, title: str = "Bug found", body: str = "Steps") -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "id": 1001,
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": "octocat", "type": "User"},
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def build_pr_payload(*, action: str = "opened", number: int = 42, title: str = "Refactor", body: str = "Cleanup") -> dict[str, Any]:
    return {
        "action": action,
        "pull_request": {
            "id": 2001,
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": "dev", "type": "User"},
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def build_review_comment_payload(*, body: str = "LGTM", pr_number: int = 42, comment_id: int = 55) -> dict[str, Any]:
    return {
        "action": "created",
        "comment": {
            "id": comment_id,
            "body": body,
            "user": {"login": "reviewer", "type": "User"},
        },
        "pull_request": {
            "id": 2001,
            "number": pr_number,
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def test_parse_event_handles_issue_opened() -> None:
    plugin = build_plugin()
    event = plugin.parse_event(build_issue_payload(action="opened", title="Segfault on startup", body="Happens every time"))

    assert event.issue_number == 12
    assert event.comment_id == 0
    assert event.author == "octocat"
    assert event.owner == "acme"
    assert event.repo == "widgets"
    assert "[Issue #12 opened]" in event.message
    assert "Segfault on startup" in event.message
    assert "Happens every time" in event.message


def test_parse_event_handles_issue_edited() -> None:
    plugin = build_plugin()
    event = plugin.parse_event(build_issue_payload(action="edited", title="Updated title", body=""))

    assert "[Issue #12 edited]" in event.message
    assert "Updated title" in event.message


def test_parse_event_handles_pr_opened() -> None:
    plugin = build_plugin()
    event = plugin.parse_event(build_pr_payload(action="opened", title="Add caching layer", body="Uses Redis"))

    assert event.issue_number == 42
    assert event.comment_id == 0
    assert event.author == "dev"
    assert "[PR #42 opened]" in event.message
    assert "Add caching layer" in event.message
    assert "Uses Redis" in event.message


def test_parse_event_handles_pr_synchronize() -> None:
    plugin = build_plugin()
    event = plugin.parse_event(build_pr_payload(action="synchronize", title="Add caching layer", body=""))

    assert "[PR #42 synchronized]" in event.message


def test_parse_event_handles_pr_review_comment() -> None:
    plugin = build_plugin()
    event = plugin.parse_event(build_review_comment_payload(body="Please add a test", pr_number=77, comment_id=10))

    assert event.issue_number == 77
    assert event.comment_id == 10
    assert event.author == "reviewer"
    assert event.message == "Please add a test"


@pytest.mark.asyncio
async def test_fetch_history_tracks_bot_timestamp() -> None:
    comments = [
        {"id": 1, "body": "human comment", "user": {"login": "human", "type": "User"}},
        {
            "id": 2,
            "body": "bot reply\n<!-- ryo:architect: {} -->",
            "user": {"login": "ryobot[bot]", "type": "Bot"},
            "created_at": "2025-06-15T10:30:00Z",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_issue_comment_payload(comment_id=99))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert snapshot.last_bot_comment_at == "2025-06-15T10:30:00Z"


@pytest.mark.asyncio
async def test_fetch_history_only_tracks_own_bot_for_cooldown() -> None:
    comments = [
        {
            "id": 1,
            "body": 'msg\n<!-- ryo:reviewer: {} -->',
            "user": {"login": "ryobot[bot]", "type": "Bot"},
            "created_at": "2025-06-15T10:00:00Z",
        },
        {
            "id": 2,
            "body": 'msg\n<!-- ryo:architect: {} -->',
            "user": {"login": "ryobot[bot]", "type": "Bot"},
            "created_at": "2025-06-15T10:30:00Z",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    plugin = build_plugin(httpx.MockTransport(handler), identity="architect")
    event = plugin.parse_event(build_issue_comment_payload(comment_id=99))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert snapshot.last_bot_comment_at == "2025-06-15T10:30:00Z"


@pytest.mark.asyncio
async def test_fetch_history_includes_other_bot_messages() -> None:
    comments = [
        {
            "id": 1,
            "body": 'reviewer reply\n<!-- ryo:reviewer: {} -->',
            "user": {"login": "ryobot[bot]", "type": "Bot"},
        },
        {
            "id": 2,
            "body": 'architect reply\n<!-- ryo:architect: {} -->',
            "user": {"login": "ryobot[bot]", "type": "Bot"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=comments)

    plugin = build_plugin(httpx.MockTransport(handler), identity="architect")
    event = plugin.parse_event(build_issue_comment_payload(comment_id=99))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert len(snapshot.messages) == 2
    assert all(m["role"] == "assistant" for m in snapshot.messages)
    assert snapshot.messages[0]["content"] == "reviewer reply"
    assert snapshot.messages[1]["content"] == "architect reply"


@pytest.mark.asyncio
async def test_send_reply_embeds_correct_identity_marker() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"id": 123})

    plugin = build_plugin(httpx.MockTransport(handler), identity="reviewer")
    event = plugin.parse_event(build_issue_comment_payload())

    await plugin.send_reply(event, "Review done", {"mode": "done"})
    await plugin.aclose()

    assert '<!-- ryo:reviewer: {"mode":"done"} -->' in captured["payload"]["body"]
