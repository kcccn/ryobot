# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 交流约定

- 所有代码审查、计划、讨论均使用**中文**
- 代码注释和变量名保持英文
- commit message 使用英文（遵循 conventional commits 风格）

## Commands

```bash
# Install with dev dependencies
pip install -e .[dev]

# Run all tests (pytest-asyncio auto-mode, no extra config needed)
pytest

# Run lint and type checks
ruff check .
mypy main.py bots core platforms

# Run a specific test file
pytest tests/test_ryo_agent.py

# Run a single test
pytest tests/test_ryo_agent.py::test_run_sends_direct_reply_without_tool_calls
```

## Architecture

Ryo Ghost Engine is a **serverless hexagonal architecture** that treats GitHub as infrastructure. No databases, no persistent servers, no control plane — GitHub Issues provide the event bus and timeline storage, GitHub Actions provide the runtime, and DeepSeek provides reasoning.

```
main.py (composition root)
  ├── GitHubPlugin       → platforms/github/plugin.py   (BasePlugin port)
  ├── default GitHub skills → platforms/github/skills.py    (BaseSkill port)
  │   ├── ReadIssueMemory    读：当前 Issue 详情
  │   ├── SearchRepoMemory   读：仓库内搜索相关 Issue
  │   ├── ReadCodeDiff       读：PR diff 内容
  │   ├── ReadThreadComments 可信读：读取其他 Issue/PR 评论
  │   ├── ListRepoLabels     读：列出仓库标签
  │   ├── CreateIssue        写：创建新 Issue
  │   ├── AddLabels          写：为 Issue 添加标签
  │   ├── CloseIssue         写：关闭 Issue
  │   ├── CommentOnPR        写：在 PR 下发布评论
  │   ├── DispatchWorkflow   写：可选，配置允许列表后触发 GitHub Actions workflow
  │   ├── ReadWorkflowRun    读：查看 workflow 运行状态
  │   └── RunCommand         写：执行 allowlist 内的开发命令
  └── RyoAgent           → core/ryo_agent.py            (ReAct loop)
       └── AsyncOpenAI    → api.deepseek.com
```

### Ports (abstract, in `core/`)

- **`BasePlugin`** (`core/plugins.py`): Parses platform events into `PluginEvent`, fetches history as `HistorySnapshot`, sends replies. GitHub is the only implementation, but the port is platform-agnostic.
- **`BaseSkill`** (`core/skills.py`): Tools the agent can call. Each skill has a `name`, `description`, `args_model` (Pydantic), `mutates_state`, optional `requires_trusted_author`, and `execute(**kwargs)`. Skills auto-generate OpenAI-compatible tool definitions via `get_tool_definition()`.
- **`RyoAgent`** (`core/ryo_agent.py`): Bounded ReAct loop — up to 5 iterations of LLM call → tool execution → tool result → next LLM call. Falls back to `DEFAULT_FALLBACK_MESSAGE` if no text reply after max iterations.
- **Skill context** (`core/skills.py`): `ContextVar`-based request-scoped context seeded with event metadata + subconscious state. Skills read it via `get_skill_context()`.

### 适配器（具体实现，位于 `platforms/github/`）

- **`GitHubPlugin`**: 解析 4 种 webhook payload（issue_comment / issues / pull_request / pull_request_review_comment），拉取评论历史，发帖。构造函数接受 `identity` 参数，构建身份特定的正则模式。`fetch_history()` 只追踪本 bot 的 cooldown 时间戳。`send_reply()` 嵌入 `<!-- ryo:{identity}: ... -->` 标记。
- **`GitHubApiClient`** (`platforms/github/client.py`): 轻量异步 REST 客户端，封装 `httpx.AsyncClient`，包含 GitHub API 版本头和 Bearer 认证。
- **Bot 社会**：通过 GitHub Actions matrix strategy 并行运行 4 个 bot（architect / reviewer / pm / explorer），各自嵌入唯一身份标记。`_contains_own_marker()` 检查事件 body 中是否已有本 bot 的标记来实现自我跳过。

### 状态持久化（Markdown 隐写术）

无数据库。Agent 将 JSON 状态嵌入 GitHub 评论的隐藏 HTML 注释中：

```
Visible reply text.
<!-- ryo:architect: {"mode":"reflective","thread":"issue-12"} -->
```

`GitHubPlugin` 内部使用 `self._state_pattern`（身份感知的正则）提取本 bot 的状态。评论时间线中最新一条有效 `ryo:{identity}` blob 成为下次运行的 `subconscious` 字典。追加式写入，与对话历史同位存储，零基础设施依赖。

### 事件流

1. `issues` / `issue_comment` / `pull_request` / `pull_request_review_comment` webhook 触发 workflow（`.github/workflows/github-ryobot.yml`）
2. Workflow 通过 matrix strategy 并行启动 4 个 bot job，注入 `BOT_IDENTITY`、`GITHUB_TOKEN`、`DEEPSEEK_API_KEY`、`EVENT_PAYLOAD`
3. `main.py` 载入 payload，若事件 body 中已包含本 bot 的身份标记（`<!-- ryo:{identity}:`）则 `exit(0)` 跳过
4. 组装 `GitHubPlugin`（传入 identity）、默认 skills（配置 workflow allowlist 后增加 `dispatch_workflow`）、`AsyncOpenAI` 客户端、`RyoAgent`，调用 `ryo_agent.run(payload)`
5. `RyoAgent` 拉取评论历史，运行 ReAct 循环，将回复（含隐藏状态）写回 Issue/PR

### Test patterns

- Tests use `httpx.MockTransport` with inline handler functions to fake GitHub API responses — never mock at the `GitHubPlugin` level.
- `RyoAgent` tests use fake dataclass-based `FakeCompletions`/`FakeResponse`/`FakeToolCall` objects rather than mocking the OpenAI SDK.
- `main.py` tests use `monkeypatch` to replace every dependency at the module level, then verify the assembly was correct.
- `pytest-asyncio` is configured with `asyncio_mode = "auto"` in `pyproject.toml` — no `@pytest.mark.asyncio` decorator needed, but it's used explicitly for clarity.
- Skill context must be explicitly cleared in tests with `clear_skill_context()` to avoid cross-test leakage.
