from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
README_PATH = _PROJECT_ROOT / "README.md"
WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "github-ryobot.yml"
REUSABLE_WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "ryobot.yml"


def valid_payload() -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"id": 1001, "number": 12},
        "comment": {
            "id": 99,
            "body": "Need help",
            "user": {"login": "octocat"},
            "author_association": "NONE",
        },
        "repository": {
            "name": "widgets",
            "owner": {"login": "acme"},
        },
    }


def payload_with_comment_body(body: str) -> dict[str, Any]:
    p = valid_payload()
    p["comment"]["body"] = body  # type: ignore[index]
    return p


def import_main_module():
    return importlib.import_module("main")


def test_main_exits_zero_when_event_contains_own_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = payload_with_comment_body('previous bot reply\n<!-- ryo:architect: {} -->')
    payload["comment"]["user"]["login"] = "github-actions[bot]"  # type: ignore[index]
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("BOT_IDENTITY", "architect")

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0


def test_main_constructs_runtime_and_runs_ryobot(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = valid_payload()
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["http_client_kwargs"] = kwargs

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
            captured.setdefault("skill_count", 0)
            captured["skill_count"] += 1

    class FakeRyoAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["ryo_agent_kwargs"] = kwargs

        async def run(self, raw_event: Any) -> None:
            captured["run_payload"] = raw_event

    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("BOT_IDENTITY", "architect")
    monkeypatch.setenv("RYOBOT_FATIGUE_MIN_SECONDS", "500")
    monkeypatch.setenv("RYOBOT_FATIGUE_MAX_SECONDS", "800")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(main, "GitHubPlugin", FakeGitHubPlugin)
    for name in (
        "ReadThreadContext", "ReadIssueMemory", "SearchRepoMemory", "ListOpenIssues", "ListOpenPullRequests",
        "ListRepoLabels", "MergePullRequest", "ReadThreadComments", "ListFiles", "ReadFile",
        "SearchCode", "ReadCodeDiff", "ReadThreadMeta", "CreateIssue", "WriteFile", "CreateBranch",
        "DeleteBranch", "CreatePullRequest", "CreatePRReview", "AddLabels", "CloseIssue", "ReopenIssue",
        "CommentOnThread", "CommentOnPR", "ReadWorkflowRun", "RunCommand", "SearchIssues", "UpdateIssue",
    ):
        monkeypatch.setattr(main, name, FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    assert captured["run_payload"] == payload
    assert captured["plugin_kwargs"]["identity"] == "architect"
    assert captured["openai_kwargs"]["api_key"] == "ds-token"
    assert captured["ryo_agent_kwargs"]["persona"]["identity"] == "architect"
    assert captured["ryo_agent_kwargs"]["fatigue_min_seconds"] == 500
    assert captured["ryo_agent_kwargs"]["fatigue_max_seconds"] == 800
    assert captured["skill_count"] == 28
    assert captured["http_client_closed"] is True


def test_selected_bot_identity_prefers_explicit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.setenv("BOT_IDENTITY", "reviewer")
    assert main._selected_bot_identity(valid_payload()) == "reviewer"


def test_bot_activity_weights_parses_key_value_env(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.setenv("RYOBOT_BOT_ACTIVITY_WEIGHTS", "architect=3,reviewer=1")
    weights = main._bot_activity_weights(["architect", "reviewer", "pm"])
    assert weights == [3.0, 1.0, 1.0]


def test_detect_fix_command_true_for_trusted_author() -> None:
    main = import_main_module()
    payload = valid_payload()
    payload["comment"]["body"] = "please /fix this bug"  # type: ignore[index]
    payload["comment"]["author_association"] = "OWNER"  # type: ignore[index]
    assert main._detect_fix_command(payload) is True


def test_detect_fix_command_false_for_untrusted_author() -> None:
    main = import_main_module()
    payload = valid_payload()
    payload["comment"]["body"] = "please /fix this bug"  # type: ignore[index]
    payload["comment"]["author_association"] = "CONTRIBUTOR"  # type: ignore[index]
    assert main._detect_fix_command(payload) is False


def test_workflows_use_single_engine_and_repo_concurrency() -> None:
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    reusable = REUSABLE_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'cron: "*/10 * * * *"' in content
    assert "uses: ./.github/workflows/ryobot.yml" in content
    assert "allowed_workflows: github-ryobot.yml" in content
    assert "python main.py" not in content
    assert "for bot in architect reviewer pm explorer coder" not in content

    assert "group: ryobot-${{ github.repository }}" in reusable
    assert "cancel-in-progress: false" in reusable
    assert "RYOBOT_FATIGUE_MIN_SECONDS" in reusable
    assert "vars.RYOBOT_ALLOWED_WORKFLOWS" in reusable
    assert "ryobot" in reusable
    assert "for bot in architect reviewer pm explorer coder" not in reusable


def test_readme_brands_project_as_single_engine_social_simulation() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "全局麦克风" in content
    assert "两段式意愿决策" in content
    assert "仓库级疲劳" in content
    assert "RYOBOT_STREET_LURKER_FATIGUE_MIN_SECONDS" in content
    assert "Actions Variable" in content
