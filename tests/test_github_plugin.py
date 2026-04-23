from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.plugins import PluginEvent
from platforms.github.plugin import GITHUB_COMMENT_STATE_PATTERN, GitHubPlugin


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
) -> GitHubPlugin:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://api.github.test",
    )
    return GitHubPlugin(token=token, client=client, api_base_url="https://api.github.test")


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
        '<!-- nexus_state: {"mode":"reflective"} -->'
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
            "body": 'RyoBot reply\n<!-- nexus_state: {"mode":"draft"} -->',
            "user": {"login": "ryobot"},
        },
        {
            "id": 99,
            "body": "current inbound comment",
            "user": {"login": "octocat"},
        },
        {
            "id": 3,
            "body": 'new RyoBot reply\n<!-- nexus_state: {"mode":"final","step":2} -->',
            "user": {"login": "ryobot"},
        },
        {
            "id": 4,
            "body": 'broken marker\n<!-- nexus_state: not-json -->',
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
        {"role": "user", "content": "broken marker"},
    ]
    assert snapshot.subconscious == {"mode": "final", "step": 2}


def test_comment_state_pattern_matches_expected_marker() -> None:
    body = 'hello\n<!-- nexus_state: {"mode":"focus"} -->'
    match = GITHUB_COMMENT_STATE_PATTERN.search(body)

    assert match is not None
    assert match.group("payload") == '{"mode":"focus"}'
