from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


README_PATH = Path("D:/ryobot/README.md")
WORKFLOW_PATH = Path("D:/ryobot/.github/workflows/github-ryobot.yml")


def valid_payload(*, sender_type: str = "User") -> dict[str, Any]:
    return {
        "sender": {"type": sender_type},
        "action": "created",
        "issue": {"id": 1001, "number": 12},
        "comment": {
            "id": 99,
            "body": "Need help",
            "user": {"login": "octocat"},
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def import_main_module():
    return importlib.import_module("main")


def test_main_exits_zero_for_ryobot_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(valid_payload(sender_type="Bot")))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0


def test_main_fails_when_required_env_vars_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.delenv("EVENT_PAYLOAD", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 1


def test_main_rejects_malformed_event_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.setenv("EVENT_PAYLOAD", "{not-json")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 1


def test_main_constructs_runtime_and_runs_ryobot(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = valid_payload()
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.setdefault("http_clients", []).append(kwargs)

        async def aclose(self) -> None:
            captured["http_client_closed"] = True

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["openai_kwargs"] = kwargs

    class FakeGitHubPlugin:
        def __init__(self, **kwargs: Any) -> None:
            captured["plugin_kwargs"] = kwargs

    class FakeSkill:
        def __init__(self, **kwargs: Any) -> None:
            captured.setdefault("skill_kwargs", []).append(kwargs)

    class FakeRyoAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["ryo_agent_kwargs"] = kwargs

        async def run(self, raw_event: Any) -> None:
            captured["run_payload"] = raw_event

    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(main, "GitHubPlugin", FakeGitHubPlugin)
    monkeypatch.setattr(main, "ReadIssueMemory", FakeSkill)
    monkeypatch.setattr(main, "SearchRepoMemory", FakeSkill)
    monkeypatch.setattr(main, "ReadCodeDiff", FakeSkill)
    monkeypatch.setattr(main, "CreateIssue", FakeSkill)
    monkeypatch.setattr(main, "AddLabels", FakeSkill)
    monkeypatch.setattr(main, "CloseIssue", FakeSkill)
    monkeypatch.setattr(main, "CommentOnPR", FakeSkill)
    monkeypatch.setattr(main, "DispatchWorkflow", FakeSkill)
    monkeypatch.setattr(main, "ReadWorkflowRun", FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    assert captured["run_payload"] == payload
    assert captured["openai_kwargs"]["api_key"] == "ds-token"
    assert captured["openai_kwargs"]["base_url"] == "https://api.deepseek.com"
    assert captured["plugin_kwargs"]["token"] == "gh-token"
    assert len(captured["skill_kwargs"]) == 9
    assert captured["ryo_agent_kwargs"]["persona"]["model"] == "deepseek-chat"
    assert "严厉且幽默的顶级架构师" in captured["ryo_agent_kwargs"]["persona"]["system_prompt"]
    assert captured["http_client_closed"] is True


def test_readme_brands_project_as_ryo_ghost_engine() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "Ryo Ghost Engine" in content
    assert "Serverless" in content
    assert "GitHub Actions" in content
    assert "<!-- ryo_state: {...} -->" in content
    assert "DEEPSEEK_API_KEY" in content


def test_workflow_passes_github_event_payload_and_secret() -> None:
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "issue_comment" in content
    assert "GITHUB_TOKEN: ${{ github.token }}" in content
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in content
    assert "EVENT_PAYLOAD: ${{ toJson(github.event) }}" in content
