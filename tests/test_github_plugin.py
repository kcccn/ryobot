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
    display_names = {
        "architect": "Ryo Architect",
        "reviewer": "Ryo Reviewer",
        "pm": "Ryo PM",
        "explorer": "Ryo Explorer",
        "coder": "Ryo Coder",
    }
    return GitHubPlugin(
        token=token,
        client=client,
        api_base_url="https://api.github.test",
        identity=identity,
        display_name=display_names.get(identity, identity),
    )


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


def live_mind_issue_detail(number: int = 999) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "title": "🧠 Ryo Architect",
        "body": (
            "# 🧠 Ryo Architect\n\n"
            "<!-- ryo:mind: {\"schema_version\":1,\"identity\":\"architect\"} -->\n\n"
            "## Working Notes\n\n(empty)\n\n"
            "## Active Context\n\nInvestigating runtime\n\n"
            "## Recent Activity\n\n- seeded\n"
        ),
        "labels": [{"name": "🧠 live-mind"}, {"name": "bot:architect"}],
        "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
    }


def coordination_issue_detail(number: int = 888) -> dict[str, Any]:
    runtime = RepoRuntimeState(
        next_patrol_after="2026-01-01T00:00:00+00:00",
        bot_fatigue={"architect": BotFatigueState(last_spoke_at="2026-01-01T00:00:00+00:00", next_available_at="2026-01-01T00:10:00+00:00")},
        last_routing=RoutingRecord(event_id="evt", bot_identity="architect", reason="replied", target_issue_number=12, routed_at="2026-01-01T00:00:00+00:00"),
        coordination_issue_number=888,
    )
    return {
        "number": number,
        "state": "open",
        "title": "🎙️ RyoBot Coordination",
        "body": f"header\n<!-- ryo:runtime: {json.dumps(runtime.model_dump(), ensure_ascii=False, separators=(',', ':'))} -->",
        "labels": [{"name": "🎙️ coordination"}],
        "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
    }


def search_handler(query: str) -> httpx.Response:
    if 'label:"🧠 live-mind"' in query and 'label:"bot:architect"' in query:
        return httpx.Response(200, json=mind_issue_payload())
    if 'label:"🎙️ coordination"' in query:
        return httpx.Response(200, json=coordination_issue_payload())
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
        if path.endswith("/issues/999"):
            return httpx.Response(200, json=live_mind_issue_detail())
        if path.endswith("/issues/888"):
            return httpx.Response(200, json=coordination_issue_detail())
        if path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "🧠 live-mind"}, {"name": "bot:architect"}, {"name": "🎙️ coordination"}])
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
        if path.endswith("/issues/999"):
            return httpx.Response(200, json=live_mind_issue_detail())
        if path.endswith("/issues/888"):
            return httpx.Response(200, json=coordination_issue_detail())
        if path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "🧠 live-mind"}, {"name": "bot:architect"}, {"name": "🎙️ coordination"}])
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
        if request.method == "GET" and path.endswith("/issues/888"):
            return httpx.Response(200, json=coordination_issue_detail())
        if request.method == "GET" and path.endswith("/issues/999"):
            return httpx.Response(200, json=live_mind_issue_detail())
        if path.endswith("/labels"):
            return httpx.Response(200, json=[{"name": "🧠 live-mind"}, {"name": "bot:architect"}, {"name": "🎙️ coordination"}])
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
                    {"number": 69, "title": "🎙️ RyoBot Coordination", "labels": [{"name": "🎙️ coordination"}], "updated_at": "2026-01-01T00:00:00Z"},
                    {"number": 63, "title": "🧠 Ryo Coder", "labels": [{"name": "🧠 live-mind"}, {"name": "bot:coder"}], "updated_at": "2026-01-01T00:00:00Z"},
                    {"number": 56, "title": "Human-facing tracker", "updated_at": "2026-01-01T00:00:00Z"},
                ],
            )
        if path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    plugin = build_plugin(httpx.MockTransport(handler))
    brief = await plugin._build_patrol_brief("acme", "widgets")
    await plugin.aclose()

    assert "Street-lurker opportunity radar" in brief
    assert "Human-facing tracker" in brief
    assert "RyoBot Coordination" not in brief
    assert "🧠 Ryo Coder" not in brief
    assert "not sufficient by itself to stay silent" in brief


@pytest.mark.asyncio
async def test_find_or_create_mind_issue_migrates_legacy_explorer_issue_without_reopening_closed_memory() -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/repos/acme/widgets/labels":
            return httpx.Response(200, json=[{"name": "duplicate"}])
        if request.method == "POST" and request.url.path == "/repos/acme/widgets/labels":
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and request.url.path == "/search/issues":
            query = str(request.url.params.get("q", ""))
            if 'label:"🧠 live-mind"' in query:
                return httpx.Response(200, json={"items": []})
            if '"🧠 Ryo Explorer" in:title' in query:
                return httpx.Response(200, json={"items": [{"number": 82}]})
            return httpx.Response(200, json={"items": []})
        if request.method == "GET" and request.url.path.endswith("/issues/82"):
            return httpx.Response(
                200,
                json={
                    "number": 82,
                    "state": "open",
                    "title": "🧠 Ryo Explorer",
                    "body": "# 🧠 Ryo Explorer\n\n> This issue is my persistent memory.\n\n## Long-term Memory\n\n(empty)\n\n## Active Context\n\n(empty)\n\n## Recent Activity\n\n- legacy\n",
                    "labels": [{"name": "duplicate"}],
                    "updated_at": "2026-05-08T14:00:00Z",
                    "created_at": "2026-05-08T13:00:00Z",
                },
            )
        if request.method == "PATCH" and request.url.path.endswith("/issues/82"):
            return httpx.Response(
                200,
                json={
                    "number": 82,
                    "state": "open",
                    "title": body["title"],
                    "body": body["body"],
                    "labels": [{"name": label} for label in body["labels"]],
                    "updated_at": "2026-05-08T14:01:00Z",
                    "created_at": "2026-05-08T13:00:00Z",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    plugin = build_plugin(httpx.MockTransport(handler), identity="explorer")

    body, issue_number = await plugin._find_or_create_mind_issue("acme", "widgets")
    await plugin.aclose()

    assert issue_number == 82
    assert "live working-memory thread" in body
    assert "persistent memory" not in body
    patch_payload = next(payload for method, path, payload in requests if method == "PATCH" and path.endswith("/issues/82"))
    assert patch_payload["labels"] == ["🧠 live-mind", "bot:explorer"]
    assert "<!-- ryo:mind:" in patch_payload["body"]
    assert "## Working Notes" in patch_payload["body"]
    assert "## Long-term Memory" not in patch_payload["body"]
    assert not any(method == "POST" and path.endswith("/issues") for method, path, _payload in requests)


@pytest.mark.asyncio
async def test_find_or_create_mind_issue_selects_canonical_and_closes_legacy_architect_duplicates() -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/repos/acme/widgets/labels":
            return httpx.Response(200, json=[{"name": "duplicate"}])
        if request.method == "POST" and request.url.path == "/repos/acme/widgets/labels":
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and request.url.path == "/search/issues":
            query = str(request.url.params.get("q", ""))
            if 'label:"🧠 live-mind"' in query:
                return httpx.Response(200, json={"items": []})
            if '"🧠 Ryo Architect" in:title' in query:
                return httpx.Response(200, json={"items": [{"number": 85}, {"number": 86}]})
            return httpx.Response(200, json={"items": []})
        if request.method == "GET" and request.url.path.endswith("/issues/85"):
            return httpx.Response(
                200,
                json={
                    "number": 85,
                    "state": "open",
                    "title": "🧠 Ryo Architect",
                    "body": "# 🧠 Ryo Architect\n\n> This issue is my persistent memory.\n\n## Active Context\n\n(empty)\n",
                    "labels": [],
                    "updated_at": "2026-05-08T07:00:00Z",
                    "created_at": "2026-05-08T07:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/issues/86"):
            return httpx.Response(
                200,
                json={
                    "number": 86,
                    "state": "open",
                    "title": "🧠 Ryo Architect",
                    "body": "# 🧠 Ryo Architect\n\n> This issue is my persistent memory.\n\n## Active Context\n\nWorking on repo hygiene\n",
                    "labels": [],
                    "updated_at": "2026-05-08T08:00:00Z",
                    "created_at": "2026-05-08T08:00:00Z",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/issues/85/comments"):
            return httpx.Response(201, json={"id": 5001})
        if request.method == "PATCH" and request.url.path.endswith("/issues/86"):
            return httpx.Response(
                200,
                json={
                    "number": 86,
                    "state": "open",
                    "title": body["title"],
                    "body": body["body"],
                    "labels": [{"name": label} for label in body["labels"]],
                    "updated_at": "2026-05-08T08:01:00Z",
                    "created_at": "2026-05-08T08:00:00Z",
                },
            )
        if request.method == "PATCH" and request.url.path.endswith("/issues/85"):
            return httpx.Response(200, json={"number": 85, "state": "closed"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    plugin = build_plugin(httpx.MockTransport(handler), identity="architect")

    body, issue_number = await plugin._find_or_create_mind_issue("acme", "widgets")
    await plugin.aclose()

    assert issue_number == 86
    assert "live working-memory thread" in body
    canonical_patch = next(payload for method, path, payload in requests if method == "PATCH" and path.endswith("/issues/86"))
    assert canonical_patch["labels"] == ["🧠 live-mind", "bot:architect"]
    duplicate_patch = next(payload for method, path, payload in requests if method == "PATCH" and path.endswith("/issues/85"))
    assert duplicate_patch["state"] == "closed"
    assert "duplicate" in duplicate_patch["labels"]
    assert "bot:architect" in duplicate_patch["labels"]
    assert not any(label == "🧠 live-mind" for label in duplicate_patch["labels"])
