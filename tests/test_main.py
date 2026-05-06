from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
README_PATH = _PROJECT_ROOT / "README.md"
WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "github-ryobot.yml"


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


def test_main_does_not_skip_human_forged_own_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = payload_with_comment_body('forged marker\n<!-- ryo:architect: {"mode":"skip"} -->')
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("BOT_IDENTITY", "architect")

    async def fake_run(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "_run", fake_run)
    main.main()


def test_main_proceeds_when_event_contains_other_bot_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = payload_with_comment_body('other bot reply\n<!-- ryo:reviewer: {} -->')
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("BOT_IDENTITY", "architect")

    # Should not exit — other bot's marker is not ours
    async def fake_run(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "_run", fake_run)
    main.main()  # no SystemExit


def test_main_proceeds_when_no_marker_present(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(valid_payload()))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("BOT_IDENTITY", "architect")

    # Should not exit — no marker at all
    async def fake_run(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "_run", fake_run)
    main.main()


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
    monkeypatch.setattr(main, "ListOpenIssues", FakeSkill)
    monkeypatch.setattr(main, "ListOpenPullRequests", FakeSkill)
    monkeypatch.setattr(main, "ListRepoLabels", FakeSkill)
    monkeypatch.setattr(main, "MergePullRequest", FakeSkill)
    monkeypatch.setattr(main, "ReadThreadComments", FakeSkill)
    monkeypatch.setattr(main, "ListFiles", FakeSkill)
    monkeypatch.setattr(main, "ReadFile", FakeSkill)
    monkeypatch.setattr(main, "SearchCode", FakeSkill)
    monkeypatch.setattr(main, "ReadCodeDiff", FakeSkill)
    monkeypatch.setattr(main, "CreateIssue", FakeSkill)
    monkeypatch.setattr(main, "WriteFile", FakeSkill)
    monkeypatch.setattr(main, "CreateBranch", FakeSkill)
    monkeypatch.setattr(main, "CreatePullRequest", FakeSkill)
    monkeypatch.setattr(main, "CreatePRReview", FakeSkill)
    monkeypatch.setattr(main, "AddLabels", FakeSkill)
    monkeypatch.setattr(main, "CloseIssue", FakeSkill)
    monkeypatch.setattr(main, "CommentOnPR", FakeSkill)
    monkeypatch.setattr(main, "DispatchWorkflow", FakeSkill)
    monkeypatch.setattr(main, "ReadWorkflowRun", FakeSkill)
    monkeypatch.setattr(main, "RunCommand", FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    assert captured["run_payload"] == payload
    assert captured["openai_kwargs"]["api_key"] == "ds-token"
    assert captured["openai_kwargs"]["base_url"] == "https://api.deepseek.com"
    assert captured["plugin_kwargs"]["token"] == "gh-token"
    assert captured["plugin_kwargs"]["identity"] == "architect"
    assert len(captured["skill_kwargs"]) == 21
    assert captured["ryo_agent_kwargs"]["persona"]["model"] == "deepseek-v4-flash"
    assert "严厉且幽默的顶级架构师" in captured["ryo_agent_kwargs"]["persona"]["system_prompt"]
    assert captured["http_client_closed"] is True


def test_main_includes_dispatch_workflow_only_when_allowlist_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = valid_payload()
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeGitHubPlugin:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeSkill:
        def __init__(self, **kwargs: Any) -> None:
            captured.setdefault("skill_count", 0)
            captured["skill_count"] += 1

    class FakeRyoAgent:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, raw_event: Any) -> None:
            pass

    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setenv("RYOBOT_ALLOWED_WORKFLOWS", "ci.yml")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(main, "GitHubPlugin", FakeGitHubPlugin)
    for name in ("ReadIssueMemory", "SearchRepoMemory", "ListOpenIssues",
                 "ListOpenPullRequests", "ListRepoLabels", "ReadThreadComments",
                 "ListFiles", "ReadFile", "SearchCode",
                 "ReadCodeDiff", "CreateIssue", "WriteFile", "CreateBranch",
                 "CreatePullRequest", "CreatePRReview", "AddLabels",
                 "CloseIssue", "CommentOnPR", "MergePullRequest",
                 "DispatchWorkflow", "ReadWorkflowRun", "RunCommand"):
        monkeypatch.setattr(main, name, FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    assert captured["skill_count"] == 22


def test_readme_brands_project_as_ryo_ghost_engine() -> None:
    content = README_PATH.read_text(encoding="utf-8")

    assert "Ryo Ghost Engine" in content
    assert "Serverless" in content
    assert "GitHub Actions" in content
    assert "<!-- ryo:{identity}: {...} -->" in content
    assert "DEEPSEEK_API_KEY" in content


def test_all_bots_use_deepseek_v4_flash() -> None:
    from bots import list_bots

    for bot in list_bots():
        assert bot.model == "deepseek-v4-flash"
        assert bot.provider == "openai"
        assert bot.base_url is None
        assert bot.api_key_env is None


def test_reviewer_uses_deepseek_openai_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = valid_payload()
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["openai_kwargs"] = kwargs

    class FakeAnthropicAdapter:
        def __init__(self, **kwargs: Any) -> None:
            captured["anthropic_kwargs"] = kwargs

    class FakeGitHubPlugin:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeSkill:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeRyoAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["ryo_agent_kwargs"] = kwargs

        async def run(self, raw_event: Any) -> None:
            pass

    monkeypatch.setattr(main, "BOT_IDENTITY", "reviewer")
    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr(main, "AnthropicAdapter", FakeAnthropicAdapter)
    monkeypatch.setattr(main, "GitHubPlugin", FakeGitHubPlugin)
    for name in ("ReadIssueMemory", "SearchRepoMemory", "ListOpenIssues",
                 "ListOpenPullRequests", "ListRepoLabels", "ReadThreadComments",
                 "ListFiles", "ReadFile", "SearchCode",
                 "ReadCodeDiff", "CreateIssue", "WriteFile", "CreateBranch",
                 "CreatePullRequest", "CreatePRReview", "AddLabels",
                 "CloseIssue", "CommentOnPR", "MergePullRequest",
                 "DispatchWorkflow", "ReadWorkflowRun"):
        monkeypatch.setattr(main, name, FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    assert "anthropic_kwargs" not in captured
    assert captured["openai_kwargs"]["api_key"] == "ds-token"
    assert captured["openai_kwargs"]["base_url"] == "https://api.deepseek.com"
    assert captured["ryo_agent_kwargs"]["persona"]["model"] == "deepseek-v4-flash"
    assert captured["ryo_agent_kwargs"]["max_tokens"] == 4096


def test_workflow_passes_github_event_payload_and_secret() -> None:
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "issue_comment" in content
    assert "GITHUB_TOKEN: ${{ github.token }}" in content
    assert "DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}" in content
    assert "ANTHROPIC_API_KEY" not in content
    assert "EVENT_PAYLOAD: ${{ toJson(github.event) }}" in content
    assert "for bot in architect reviewer pm explorer coder" in content


def test_workflow_grants_actions_write_for_patrol_dispatch() -> None:
    content = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "actions: write" in content
    assert "contents: write" in content
    assert "schedule" in content
    assert "GITHUB_REPOSITORY: ${{ github.repository }}" in content
    assert "RYOBOT_ALLOWED_WORKFLOWS: github-ryobot.yml" in content


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


def test_detect_fix_command_false_without_fix() -> None:
    main = import_main_module()
    payload = valid_payload()
    payload["comment"]["body"] = "please fix this bug"  # type: ignore[index]
    payload["comment"]["author_association"] = "OWNER"  # type: ignore[index]

    assert main._detect_fix_command(payload) is False


def test_detect_fix_command_in_issue_body() -> None:
    main = import_main_module()
    payload = valid_payload()
    del payload["comment"]
    payload["issue"]["body"] = "/fix the login redirect"  # type: ignore[index]
    payload["issue"]["author_association"] = "MEMBER"  # type: ignore[index]

    assert main._detect_fix_command(payload) is True


def test_fix_mode_injects_directive(monkeypatch: pytest.MonkeyPatch) -> None:
    main = import_main_module()
    payload = valid_payload()
    payload["comment"]["body"] = "/fix this please"  # type: ignore[index]
    payload["comment"]["author_association"] = "OWNER"  # type: ignore[index]
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeGitHubPlugin:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeSkill:
        def __init__(self, **kwargs: Any) -> None:
            pass

    class FakeRyoAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["ryo_agent_kwargs"] = kwargs

        async def run(self, raw_event: Any) -> None:
            pass

    monkeypatch.setenv("EVENT_PAYLOAD", json.dumps(payload))
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-token")
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(main, "GitHubPlugin", FakeGitHubPlugin)
    for name in ("ReadIssueMemory", "SearchRepoMemory", "ListOpenIssues",
                 "ListOpenPullRequests", "ListRepoLabels", "ReadThreadComments",
                 "ListFiles", "ReadFile", "SearchCode",
                 "ReadCodeDiff", "CreateIssue", "WriteFile", "CreateBranch",
                 "CreatePullRequest", "CreatePRReview", "AddLabels",
                 "CloseIssue", "CommentOnPR", "MergePullRequest",
                 "DispatchWorkflow", "ReadWorkflowRun", "RunCommand"):
        monkeypatch.setattr(main, name, FakeSkill)
    monkeypatch.setattr(main, "RyoAgent", FakeRyoAgent)

    main.main()

    prompt = captured["ryo_agent_kwargs"]["persona"]["system_prompt"]
    assert "/FIX MODE ACTIVE" in prompt
    assert "可信维护者发出了 /fix 命令" in prompt
