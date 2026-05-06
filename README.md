# Ryo Ghost Engine

无需服务器、无需数据库、无需 Fork 任何代码——在你的仓库里放一个 workflow 文件、配一个 API Key，GitHub 就变成了你的 Ryo 多智能体运行时。

> 不建控制平面，不建持久层，不租常驻机器。直接把 GitHub 当成基础设施层来用。

---

## 快速开始

> 整个过程只需要在你的目标仓库操作，**不需要克隆或 Fork ryobot 仓库**。

### 第一步：在你的仓库创建 workflow 文件

进入你的目标仓库，创建 `.github/workflows/ryobot.yml`，粘贴以下内容：

```yaml
name: ryobot

on:
  issues:
    types: [opened, edited]
  issue_comment:
    types: [created]
  pull_request:
    types: [opened, edited, synchronize, closed]
  pull_request_review_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      issue_number:
        description: 'Issue/PR number to trigger bots on'
        required: false
        type: string
  schedule:
    - cron: "*/30 * * * *"

permissions:
  issues: write
  pull-requests: write
  actions: write
  contents: write

jobs:
  ryobot:
    uses: kcccn/ryobot/.github/workflows/ryobot.yml@main
    secrets: inherit
```

这个文件做了什么：

- **定义触发条件**（`on`）：当 Issue/PR/评论事件发生时自动触发
- **声明权限**（`permissions`）：允许 workflow 读写 Issue、PR、Contents，以及触发其他 workflow
- **调用 ryobot 可复用 workflow**（`uses:`）：一行引用，由 ryobot 仓库提供运行逻辑

关键要点：
- **无需 checkout、无需 pip install、无需 matrix strategy**：这些都封装在 ryobot 可复用 workflow 内部
- **版本自动跟随**：`@main` 始终使用最新版；也可以 pin 到 `@v0.1.0` 固定版本
- **无需 Fork**：你不需要复制任何 Python 文件到你的仓库
- **`uses:` 引用自动在 caller 仓库上下文中运行**：bot 读写的 Issue、PR、标签都作用于你的仓库
- **不需要部分 bot**：只要去掉不需要的 bot 即可，移除 `uses:` 改为直接写 job（见下方高级配置）

### 第二步：添加 API Key

进入你的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，至少添加一个：

| Secret | 说明 | 哪些 bot 用到 |
|---|---|---|
| `DEEPSEEK_API_KEY` | [DeepSeek API Key](https://platform.deepseek.com/api_keys) | architect / pm / explorer |
| `ANTHROPIC_API_KEY` | [Anthropic API Key](https://console.anthropic.com/) | reviewer |

未配置对应 Key 的 bot 会在运行时报错退出，但不影响其他 bot 正常工作。

### 第三步：启用 Actions

如果你的仓库是新建的，GitHub 可能默认禁用了 Actions。进入 **Actions** 标签页，点击 **"I understand my workflows, go ahead and enable them"**。

### 第四步：触发并验证

1. 在你的仓库创建一个 Issue，发一条评论
2. 进入 **Actions** 标签页，应该能看到 `ryobot` workflow 正在运行
3. 点击 workflow run，展开每个 bot job 的日志，确认安装和运行步骤正常
4. 几十秒后刷新 Issue 页面，应该能看到 bot 的回复

如果没有看到 bot 回复：
- 检查对应 Secret 是否正确添加
- 点击失败的 workflow run 查看具体错误信息
- 确认 bot 不在冷却窗口内（默认 120 秒内只回复一次）

---

## Bot 社会

Ryo Ghost Engine 默认运行 4 个不同人格的 bot，通过 GitHub Actions matrix strategy 并行执行。每个 bot 在自己的评论中嵌入唯一的身份标记，只会跳过自己的历史评论，允许与其他 bot 和用户自由交互。

| Bot Identity | 人格 | 模型 | 风格 |
|---|---|---|---|
| `architect` | Ryo Architect | DeepSeek V4 Flash | 严厉且幽默的顶级架构师，关注抽象质量和代码品味 |
| `reviewer` | Ryo Reviewer | DeepSeek V4 Flash | 挑剔的代码审查者，关注边界情况、错误处理与可维护性 |
| `pm` | Ryo PM | DeepSeek V4 Flash | 关注用户体验和产品逻辑一致性的产品经理 |
| `explorer` | Ryo Explorer | DeepSeek V4 Flash | 充满好奇心的黑客，热衷探索架构可能性与创造性替代方案 |

核心机制：
- **身份标记**：每个 bot 的评论末尾嵌入 `<!-- ryo:{identity}: -->` 隐藏标记
- **自我跳过**：收到包含自己标记的事件时静默退出，避免重复回复
- **并行执行**：workflow 使用 `matrix.bot` 为 4 个 bot 分别启动独立 job
- **独立冷却**：每个 bot 只追踪自己的回复时间戳，冷却机制互不干扰

---

## 触发条件

| 事件 | 触发时机 |
|---|---|
| `issues.opened / edited` | Issue 创建或编辑 |
| `issue_comment.created` | 收到新评论 |
| `pull_request.opened / edited / synchronize / closed` | PR 创建、编辑、推送新代码或关闭 |
| `pull_request_review_comment.created` | PR review 讨论中新评论 |
| `workflow_dispatch` | 手动触发，可选填入 `issue_number` |

---

## 可选配置

所有配置均为可选，默认值即可满足绝大多数场景。

### 环境变量

以下环境变量由 `ryobot` 命令读取，可通过 workflow 的 `with:` 或 `env:` 设置。

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `BOT_IDENTITY` | `architect` | bot 身份，可选 `architect` / `reviewer` / `pm` / `explorer` |
| `COOLDOWN_SECONDS` | `120` | bot 最小响应间隔（秒），设为 `0` 禁用 |
| `MAX_ITERATIONS` | `100` | ReAct 循环最大工具调用轮数 |
| `LLM_MODEL` | `deepseek-v4-flash` | 覆盖默认模型（对使用 OpenAI 兼容 API 的 bot 有效） |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 覆盖默认 API 端点 |
| `RYOBOT_ALLOWED_WORKFLOWS` | 无（所有 workflow 均需显式允许） | 允许 DispatchWorkflow skill 触发的 workflow 文件名列表，逗号分隔；为空则不启用该 skill |
| `RYOBOT_ALLOWED_WORKFLOW_REFS` | `main` | 允许 DispatchWorkflow skill 触发的目标分支列表，逗号分隔 |
| `RYOBOT_MARKER_AUTHOR_LOGINS` | `github-actions[bot]` | 被视为 bot 标记可信来源的 GitHub 用户名，逗号分隔 |
| `RYOBOT_MAX_TOOL_RESULT_CHARS` | `20000` | 单次工具调用返回结果的最大字符数，超出截断 |
| `RYOBOT_MAX_HISTORY_COMMENT_CHARS` | `12000` | 历史评论回灌给 LLM 时每条的最大字符数，超出截断 |
| `RYOBOT_MAX_DIFF_CHARS` | `50000` | `read_code_diff` 读取 PR diff 的最大字符数 |
| `RYOBOT_MAX_FILE_CHARS` | `30000` | `read_file` 读取文件内容的最大字符数 |

常用变量（`COOLDOWN_SECONDS`、`MAX_ITERATIONS`、`RYOBOT_ALLOWED_WORKFLOWS`、`RYOBOT_MARKER_AUTHOR_LOGINS`）已暴露为可复用 workflow 的 `with:` 输入。例如：

```yaml
jobs:
  ryobot:
    uses: kcccn/ryobot/.github/workflows/ryobot.yml@main
    secrets: inherit
    with:
      cooldown_seconds: 60
      max_iterations: 50
```

### 冷却机制

通过 `COOLDOWN_SECONDS` 控制 bot 的最小响应间隔（默认 120 秒）。如果 bot 在上一次回复后的冷却窗口内再次被触发，会静默退出，不会发帖。设为 `0` 可禁用。

`send_reply` 发帖前会随机等待 1-5 秒，模拟人类打字节奏。

---

## 为什么要做这个

传统智能体平台通常把三样东西打包售卖：

1. 编排
2. 记忆
3. 算力

Ryo Ghost Engine 拒绝这个捆绑套餐。

- 编排放在六边形核心里，由 `RyoAgent` 负责
- 记忆隐藏在 GitHub 评论里
- 算力借用 GitHub Actions
- 模型访问通过你自带的 API Key

因此，这个系统具备几个非常实际的特征：

- **无状态**：每次触发独立执行，不依赖常驻进程
- **零成本**：放一个 workflow 文件即可启动，不需要先买基础设施
- **可审计**：关键状态和交互都沉淀在 GitHub Issue/PR 时间线中
- **可替换**：核心层与平台层分离，适配器可以随时换

---

## Agent 能力清单

每个 skill 以 OpenAI 兼容的 tool definition 形式暴露给 LLM，`RyoAgent` 在 ReAct 循环中自行决定调用哪些工具。

| Skill | 类型 | 功能 |
|---|---|---|
| `read_issue_memory` | 读 | 读取当前 Issue 详情（标题、状态、正文） |
| `search_repo_memory` | 读 | 在仓库内搜索相关 Issue |
| `read_code_diff` | 读 | 读取指定 PR 的 `.diff` 内容 |
| `list_files` | 读 | 列出仓库目录结构 |
| `read_file` | 读 | 读取仓库中任意文件内容 |
| `search_code` | 读 | 在仓库代码中搜索关键词或模式 |
| `list_open_issues` | 读 | 列出仓库中的 Issue 并过滤状态/标签 |
| `create_issue` | 写 | 在仓库中创建新 Issue |
| `add_labels` | 写 | 为 Issue 添加标签 |
| `close_issue` | 写 | 关闭 Issue |
| `comment_on_pr` | 写 | 在 PR 下发布评论 |
| `write_file` | 写 | 创建或更新仓库文件 |
| `create_branch` | 写 | 创建新分支 |
| `create_pull_request` | 写 | 创建 Pull Request |
| `dispatch_workflow` | 写 | 触发 GitHub Actions workflow |
| `read_workflow_run` | 读 | 查看 workflow 运行状态和结果 |

单次执行最多进行 `MAX_ITERATIONS` 轮工具调用（默认 30）。如果 LLM 连续调用工具而不给出最终文本回复，循环结束后返回 fallback 消息。

---

## Markdown 隐写术记忆系统

这个引擎不拥有数据库。

相反，它把"潜意识状态"藏进 GitHub 评论的 HTML 注释中：

```html
给人类看的正常回复。
<!-- ryo:architect: {"mode":"reflective","thread":"issue-12"} -->
```

最小标记形态：

```html
<!-- ryo:{identity}: {...} -->
```

对人类来说，这是一条普通评论；对 `GitHubPlugin` 来说，它包含两层信息：

```text
GitHub 评论时间线
        |
        +--> 可见 Markdown  ---> 回灌为会话历史
        |
        +--> 隐藏 HTML 注释 ---> 提取为 subconscious state
```

这套机制的价值在于：

- **追加式**：天然适合时间线系统
- **同位存储**：记忆与对话就在同一个 Issue 里
- **低耦合**：无需额外 Redis / Vector DB / RDBMS
- **低维护**：状态托管给 GitHub，而不是新造一个状态系统

说白了，这不是"神奇 AI 长期记忆系统"，而是**利用 Markdown 和 HTML 注释做最小可行持久化**。

---

## 六边形架构

```text
                    +-----------------------------+
                    |   GitHub Actions Runner     |
                    |   Serverless 执行入口        |
                    +-------------+---------------+
                                  |
                                  v
                          +-------+-------+
                          |     main.py   |
                          |   组合根 /    |
                          | Composition   |
                          +-------+-------+
                                  |
             +--------------------+--------------------+
             |                                         |
             v                                         v
   +---------+----------+                  +-----------+-----------+
   |   GitHubPlugin     |                  | AsyncOpenAI /         |
   | platforms/github   |                  | AnthropicAdapter      |
   +---------+----------+                  +-----------+-----------+
             |                                         |
             +--------------------+--------------------+
                                  |
                                  v
                         +--------+--------+
                         |    RyoAgent     |
                         |  核心六边形内部   |
                         +--------+--------+
                                  |
              +------------------+------------------+
              |                  |                  |
              v                  v                  v
       ReadIssueMemory   SearchRepoMemory    ReadCodeDiff
       CreateIssue       AddLabels    CloseIssue  CommentOnPR
```

### 分层含义

- `core/`：纯业务核心，只定义端口、技能抽象、上下文和 `RyoAgent`
- `platforms/github/`：GitHub 外部适配器，负责 Issue/PR 的读取与写入操作
- `platforms/llm/`：LLM 提供商适配器（OpenAI 兼容 / Anthropic）
- `main.py`：组合根，只负责把环境变量、平台层、模型客户端和核心层接起来

### 仓库结构

```text
.
├── main.py
├── bots/
│   ├── config.py           ← BotConfig 数据类
│   ├── architect.py / reviewer.py / pm.py / explorer.py
├── core/
│   ├── plugins.py          ← BasePlugin 端口
│   ├── skills.py           ← BaseSkill 端口 + ContextVar
│   └── ryo_agent.py        ← ReAct 循环
├── platforms/
│   ├── github/             ← GitHub 适配器
│   └── llm/                ← LLM 提供商适配器
├── tests/
└── .github/workflows/
    └── github-ryobot.yml   ← 本仓库自身的 bot workflow
```

---

## 运行时模型

每次触发时，`ryobot` 命令只做四件事：

1. 读取 `EVENT_PAYLOAD`、`GITHUB_TOKEN` 和对应 bot 的 API Key
2. 如果事件已包含本 bot 的标记，直接退出，防止自我重复触发
3. 组装平台插件、GitHub skills、LLM 客户端与 `RyoAgent`
4. 调用核心 ReAct 循环，并把结果写回 GitHub

执行结束后，进程立即销毁。除了 GitHub 评论里留下的隐藏状态外，没有任何本地持久层残留。

### 事件流

```text
issue / PR / comment event
        |
        v
GitHub Actions workflow (matrix: 4 bots)
        |
        v
ryobot 命令
        |
        v
Contains own marker? ---- yes ---> exit(0)
        |
        no
        |
        v
GitHubPlugin.parse_event()
        |
        v
RyoAgent.run()
        |
        v
Within cooldown? ---- yes ---> exit(0)
        |
        no
        |
        v
GitHubPlugin.send_reply()
        |
        v
Issue comment + hidden ryo:{identity} blob
```

---

## 设计立场

- 对外架构品牌名是 **Ryo Ghost Engine**
- 代码中的运行时编排器叫 `RyoAgent`
- 当前平台层是 GitHub 优先设计
- LLM 层通过 provider 字段支持 OpenAI 兼容 API 和 Anthropic 原生 API

如果你觉得这里还缺一个"控制台后台"，那说明你想做的是另一类系统。

如果你觉得这里还缺一个数据库，先证明 GitHub 评论真的不够用。

如果你觉得这里还缺更多算力，直接把 workflow 文件放到更多仓库里。
