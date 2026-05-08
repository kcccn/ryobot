from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from core.plugins import BotFatigueState, PluginEvent, RepoRuntimeState, RoutingRecord
from platforms.github.plugin import GitHubPlugin


def build_issue_comment_payload(*, body: str = "hello", comment_id: int = 99) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"id": 1001, "number": 12},
        "comment": {
            "id": comment_id,
            "body": body,
            "user": {"login": "octocat"},
            "author_association": "NONE",
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
            "author_association": "CONTRIBUTOR",
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def build_plugin(handler: httpx.MockTransport | None = None, *, token: str = "secret-token", identity: str = "architect") -> GitHubPlugin:
    transport = handler or httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = httpx.AsyncClient(transport=transport, base_url="https://api.github.test")
    return GitHubPlugin(token=token, client=client, api_base_url="https://api.github.test", identity=identity)


def coordination_issue_payload() -> dict[str, Any]:
    runtime = RepoRuntimeState(
        next_patrol_after="2026-01-01T00:00:00+00:00",
        bot_fatigue={"architect": BotFatigueState(last_spoke_at="2026-01-01T00:00:00+00:00", next_available_at="2026-01-01T00:10:00+00:00")},
        last_routing=RoutingRecord(event_id="evt", bot_identity="architect", reason="replied", target_issue_number=12, routed_at="2026-01-01T00:00:00+00:00"),
        coordination_issue_number=888,
    )
    return {
        "items": [
            {
                "number": 888,
                "body": f"header\n<!-- ryo:runtime: {json.dumps(runtime.model_dump(), ensure_ascii=False, separators=(',', ':'))} -->",
            }
        ]
    }


def mind_issue_payload() -> dict[str, Any]:
    return {"items": [{"number": 999, "body": "mind memory"}]}


def search_handler(query: str) -> httpx.Response:
    if "🧠" in query:
        return httpx.Response(200, json=mind_issue_payload())
    if "🎙️ RyoBot Coordination" in query:
        return httpx.Response(200, json=coordination_issue_payload())
    return httpx.Response(200, json={"items": []})


def test_parse_event_normalizes_issue_comment_payload() -> None:
    plugin = build_plugin()

    event = plugin.parse_event(build_issue_comment_payload(body="Need help"))

    assert event == PluginEvent(
        event_id="github:acme/widgets:issue:12:comment:99",
        message="[Comment on Issue #12]\n\nNeed help",
        author="octocat",
        issue_id="1001",
        issue_number=12,
        comment_id=99,
        owner="acme",
        repo="widgets",
        author_association="NONE",
    )
    assert event.is_pull_request is False


def test_parse_event_treats_workflow_dispatch_with_empty_inputs_as_patrol() -> None:
    plugin = build_plugin()

    event = plugin.parse_event(
        {
            "inputs": {},
            "repository": {
                "name": "widgets",
                "owner": {"login": "acme"},
            },
        }
    )

    assert event.is_patrol is True
    assert event.issue_number == 0
    assert "Street lurker mode" in event.message


@pytest.mark.asyncio
async def test_fetch_history_returns_partial_context_and_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RYOBOT_INITIAL_HISTORY_COMMENT_LIMIT", "2")
    comments = [
        {"id": 1, "body": "old human", "user": {"login": "human"}, "created_at": "2026-01-01T00:00:01Z"},
        {"id": 2, "body": 'older bot\n<!-- ryo:architect: {"mode":"draft"} -->', "user": {"login": "github-actions[bot]"}, "created_at": "2026-01-01T00:00:02Z"},
        {"id": 3, "body": 'latest bot\n<!-- ryo:architect: {"mode":"final"} -->', "user": {"login": "github-actions[bot]"}, "created_at": "2026-01-01T00:00:03Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            return search_handler(str(request.url.params.get("q", "")))
        if path.endswith("/issues/12/comments"):
            return httpx.Response(200, json=comments)
        return httpx.Response(200, json=[])

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_issue_comment_payload(comment_id=99))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert snapshot.messages[0]["role"] == "system"
    assert snapshot.messages[1:] == [
        {"role": "assistant", "content": "older bot"},
        {"role": "assistant", "content": "latest bot"},
    ]
    assert snapshot.subconscious == {"mode": "final"}
    assert snapshot.runtime_state.coordination_issue_number == 888
    assert snapshot.mind_issue_number == 999


@pytest.mark.asyncio
async def test_fetch_history_includes_pr_review_comments_in_timestamp_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            return search_handler(str(request.url.params.get("q", "")))
        if path.endswith("/issues/42/comments"):
            return httpx.Response(
                200,
                json=[{"id": 1, "body": "issue timeline comment", "user": {"login": "human"}, "created_at": "2026-01-01T00:00:02Z"}],
            )
        if path.endswith("/pulls/42/comments"):
            return httpx.Response(
                200,
                json=[{"id": 2, "body": "inline review comment", "user": {"login": "reviewer"}, "created_at": "2026-01-01T00:00:01Z"}],
            )
        return httpx.Response(200, json=[])

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_pr_payload(number=42))

    snapshot = await plugin.fetch_history(event)
    await plugin.aclose()

    assert snapshot.messages[1:] == [
        {"role": "user", "content": "inline review comment"},
        {"role": "user", "content": "issue timeline comment"},
    ]


@pytest.mark.asyncio
async def test_resolve_target_event_recovers_pr_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/issues/77"):
            return httpx.Response(
                200,
                json={
                    "id": 7701,
                    "number": 77,
                    "title": "Fix cache stampede",
                    "body": "Investigate merged regression",
                    "user": {"login": "maintainer"},
                    "author_association": "OWNER",
                    "pull_request": {"url": "https://api.github.test/prs/77"},
                },
            )
        return httpx.Response(200, json={})

    plugin = build_plugin(httpx.MockTransport(handler))
    source_event = PluginEvent(
        event_id="evt-patrol",
        message="patrol",
        author="system",
        author_association="OWNER",
        issue_id="",
        issue_number=0,
        comment_id=0,
        owner="acme",
        repo="widgets",
        is_patrol=True,
    )

    target = await plugin.resolve_target_event(source_event, 77)
    await plugin.aclose()

    assert target.issue_number == 77
    assert target.is_pull_request is True
    assert "Fix cache stampede" in target.message


@pytest.mark.asyncio
async def test_send_reply_posts_comment_with_hidden_state_marker() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"id": 123})

    plugin = build_plugin(httpx.MockTransport(handler))
    event = plugin.parse_event(build_issue_comment_payload())

    await plugin.send_reply(event, "Visible reply", {"mode": "reflective"})
    await plugin.aclose()

    assert captured["payload"]["body"].endswith('<!-- ryo:architect: {"mode":"reflective"} -->')


@pytest.mark.asyncio
async def test_update_runtime_state_patches_coordination_issue_body() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/issues":
            return search_handler(str(request.url.params.get("q", "")))
        if path.endswith("/issues/888") and request.method == "PATCH":
            captured["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"number": 888})
        return httpx.Response(200, json=[])

    plugin = build_plugin(httpx.MockTransport(handler))
    runtime_state = await plugin._load_runtime_state("acme", "widgets")
    runtime_state.next_patrol_after = "2026-01-01T01:00:00+00:00"
    await plugin.update_runtime_state(runtime_state)
    await plugin.aclose()

    assert "ryo:runtime" in captured["payload"]["body"]
    assert "2026-01-01T01:00:00+00:00" in captured["payload"]["body"]


@pytest.mark.asyncio
async def test_build_patrol_brief_excludes_internal_issues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(
                200,
                json=[
                    {"number": 69, "title": "🎙️ RyoBot Coordination", "updated_at": "2026-01-01T00:00:00Z"},
                    {"number": 63, "title": "🧠 Ryo Coder", "updated_at": "2026-01-01T00:00:00Z"},
                    {"number": 56, "title": "Human-facing tracker", "updated_at": "2026-01-01T00:00:00Z"},
                ],
            )
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    plugin = build_plugin(httpx.MockTransport(handler))
    brief = await plugin._build_patrol_brief("acme", "widgets")
    await plugin.aclose()

    assert "Human-facing tracker" in brief
    assert "RyoBot Coordination" not in brief
    assert "🧠 Ryo Coder" not in brief
