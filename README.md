# Ryo Ghost Engine

无需服务器、无需数据库、无需 Fork 任何代码——在你的仓库里放一个 workflow 文件、配一个 API Key，GitHub 就变成了你的 Ryo 多智能体运行时。

> 不建控制平面，不建持久层，不租常驻机器。直接把 GitHub 当成基础设施层来用。

---

## 赛博社区架构

新版 RyoBot 不再把 bot 当成并行流水线工人，而是把整个仓库当成一个带 **全局麦克风** 的赛博社区：

- **单引擎、单 bot 抢麦**：每次触发只随机抽 1 个 bot 上线，避免群聊围攻同一条 Issue。
- **两段式意愿决策**：bot 必须先输出 JSON 意愿状态，再决定是否真的公开发言。
- **仓库级疲劳**：bot 的“休息时间”记录在 repo coordination issue 中，不再是线程内局部冷却。
- **局部上下文 + 主动补证据**：初始只加载最近一段评论；不够就用只读工具自行摸清全仓库情况。
- **随机巡逻门禁**：Actions 固定每 10 分钟唤醒一次，但真正的街溜子巡逻由 repo 级状态决定，实际间隔是 30-50 分钟随机。

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
    - cron: "*/10 * * * *"

permissions:
  issues: write
  pull-requests: write
  actions: write
  contents: write

jobs:
  ryobot:
    concurrency:
      group: ryobot-${{ github.repository }}
      cancel-in-progress: false
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
- **仓库级物理互斥**：`concurrency` 保证同一时刻只有一个引擎实例在运行
- **不需要部分 bot**：路由器会按权重随机选择 bot；也可以通过配置把某些 bot 权重调低甚至调成 0

> **重要**：bot 会自动创建分支和提交 PR。为使这正常工作，需要在仓库 **Settings → Actions → General → Workflow permissions** 中：
> 1. 勾选 **Read and write permissions**
> 2. 勾选 **Allow GitHub Actions to create and approve pull requests**
>
> 如果不开启这个选项，bot 创建 PR 时会收到 `"GitHub Actions is not permitted to create or approve pull requests"` 错误。

### 第二步：添加 API Key

进入你的仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，至少添加一个：

| Secret | 说明 | 哪些 bot 用到 |
|---|---|---|
| `DEEPSEEK_API_KEY` | [DeepSeek API Key](https://platform.deepseek.com/api_keys) | 默认内置 bot：architect / reviewer / pm / explorer |
| `ANTHROPIC_API_KEY` | [Anthropic API Key](https://console.anthropic.com/) | 仅自定义 Anthropic provider bot 需要 |

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

Ryo Ghost Engine 默认运行 5 个不同人格的 bot，但每次事件只会抽中其中 1 个。她先做意愿判断，再决定要不要真正回帖。

| Bot Identity | 人格 | 模型 | 风格 |
|---|---|---|---|
| `architect` | Ryo Architect | DeepSeek V4 Flash | 严厉且幽默的顶级架构师，关注抽象质量和代码品味 |
| `reviewer` | Ryo Reviewer | DeepSeek V4 Flash | 挑剔的代码审查者，关注边界情况、错误处理与可维护性 |
| `pm` | Ryo PM | DeepSeek V4 Flash | 关注用户体验和产品逻辑一致性的产品经理 |
| `explorer` | Ryo Explorer | DeepSeek V4 Flash | 充满好奇心的黑客，热衷探索架构可能性与创造性替代方案 |
| `coder` | Ryo Coder | DeepSeek V4 Flash | 务实高效的实现者，专注把需求变成代码 |

核心机制：
- **身份标记**：每个 bot 的评论末尾嵌入 `<!-- ryo:{identity}: -->` 隐藏标记
- **自我跳过**：收到包含自己标记的事件时静默退出，避免重复回复
- **全局麦克风**：workflow 只运行一次，入口路由器随机抽取单个 bot 上线
- **两段式意愿决策**：第一阶段输出 JSON，第二阶段才允许真正发言或改仓库
- **仓库级疲劳**：bot 的 `last_spoke_at` / `next_available_at` 写入 coordination issue，跨线程生效

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
| `BOT_IDENTITY` | 空 | 显式指定 bot 身份；默认由路由器随机抽取 |
| `MAX_ITERATIONS` | `100` | ReAct 循环最大工具调用轮数 |
| `LLM_MODEL` | `deepseek-v4-flash` | 覆盖默认模型（对使用 OpenAI 兼容 API 的 bot 有效） |
| `LLM_BASE_URL` | `https://api.deepseek.com` | 覆盖默认 API 端点 |
| `RYOBOT_BOT_ACTIVITY_WEIGHTS` | 空 | bot 活跃度权重，支持 JSON 或 `architect=3,reviewer=1` 形式 |
| `RYOBOT_MOTIVATION_THRESHOLD` | `70` | 第一阶段 `motivation_score` 至少达到该值才允许公开发言 |
| `RYOBOT_FATIGUE_MIN_SECONDS` | `480` | bot 发言后的最短疲劳时间 |
| `RYOBOT_FATIGUE_MAX_SECONDS` | `720` | bot 发言后的最长疲劳时间 |
| `RYOBOT_INITIAL_HISTORY_COMMENT_LIMIT` | `12` | 初始只加载最近多少条评论进入上下文 |
| `RYOBOT_INITIAL_HISTORY_TOTAL_CHARS` | `16000` | 初始评论切片的字符预算 |
| `RYOBOT_ALLOWED_WORKFLOWS` | 无（所有 workflow 均需显式允许） | 允许 DispatchWorkflow skill 触发的 workflow 文件名列表，逗号分隔；为空则不启用该 skill |
| `RYOBOT_ALLOWED_WORKFLOW_REFS` | `main` | 允许 DispatchWorkflow skill 触发的目标分支列表，逗号分隔 |
| `RYOBOT_MARKER_AUTHOR_LOGINS` | `github-actions[bot]` | 被视为 bot 标记可信来源的 GitHub 用户名，逗号分隔 |
| `RYOBOT_MAX_TOOL_RESULT_CHARS` | `20000` | 单次工具调用返回结果的最大字符数，超出截断 |
| `RYOBOT_MAX_HISTORY_COMMENT_CHARS` | `12000` | 当前事件 Issue/PR 正文回灌给 LLM 的最大字符数，超出截断 |
| `RYOBOT_MAX_HISTORY_TOTAL_CHARS` | `80000` | 历史评论回灌给 LLM 时的总字符预算；超出时丢弃最旧的完整评论 |
| `RYOBOT_MAX_DIFF_CHARS` | `50000` | `read_code_diff` 读取 PR diff 的最大字符数 |
| `RYOBOT_MAX_FILE_CHARS` | `30000` | `read_file` 读取文件内容的最大字符数 |
| `RYOBOT_ALLOWED_COMMANDS` | `pytest, python -m pytest, ruff check, mypy, pyright` | `run_command` 允许执行的命令前缀，逗号分隔 |
| `RYOBOT_COMMAND_TIMEOUT_SECONDS` | `300` | `run_command` 单次执行超时秒数 |

常用变量（`MAX_ITERATIONS`、`RYOBOT_MOTIVATION_THRESHOLD`、`RYOBOT_FATIGUE_MIN_SECONDS`、`RYOBOT_FATIGUE_MAX_SECONDS`、`RYOBOT_INITIAL_HISTORY_COMMENT_LIMIT`、`RYOBOT_ALLOWED_WORKFLOWS`）已暴露为可复用 workflow 的 `with:` 输入。例如：

```yaml
jobs:
  ryobot:
    uses: kcccn/ryobot/.github/workflows/ryobot.yml@main
    secrets: inherit
    with:
      max_iterations: 50
      motivation_threshold: 75
      fatigue_min_seconds: 600
      fatigue_max_seconds: 900
```

### 两段式意愿与仓库级疲劳

bot 被唤醒时先输出一段 JSON：

```json
{
  "context_analysis": "我只看到最近几条评论，暂时像是重复问题。",
  "internal_emotion": "有点想吐槽，但不够值得出声。",
  "biological_clock_impact": "现在像深夜，懒得说太多。",
  "motivation_score": 23,
  "action_decision": {
    "will_reply": false,
    "target_issue_number": null
  }
}
```

只有 `will_reply=true` 且 `motivation_score >= RYOBOT_MOTIVATION_THRESHOLD` 时才会进入第二阶段真正回帖。发言后，仓库会把该 bot 的疲劳信息写入 coordination issue，因此冷却是全仓库共享的，而不是某条线程单独计时。

### 权限与安全边界

`ryobot` workflow 需要 `issues: write` 和 `pull-requests: write` 来评论、打标签和读取 PR 讨论；需要 `contents: write` 来通过 `write_file`、`create_branch`、`create_pull_request` 完成仓库修改；需要 `actions: write` 才能在允许列表内触发 workflow。

写操作和可信上下文读取只对 GitHub `OWNER`、`MEMBER`、`COLLABORATOR` 触发的事件开放。外部贡献者触发时，agent 只能使用只读且非敏感的工具。

`run_command` 不再执行任意 shell。它会拒绝 shell 元字符，只运行 `RYOBOT_ALLOWED_COMMANDS` 中的命令前缀，并在子进程环境中移除 `GITHUB_TOKEN`、模型 API Key 等敏感变量。默认适合跑测试、lint 和类型检查；如果你放宽 allowlist，就等价于主动扩大 bot 的执行边界。

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
| `list_repo_labels` | 读 | 列出仓库已有标签，供打标签前确认 |
| `read_thread_comments` | 可信读 | 读取同仓库其他 Issue/PR 的评论和 PR review comments |
| `create_issue` | 写 | 在仓库中创建新 Issue |
| `add_labels` | 写 | 为 Issue/PR 添加仓库中已存在的标签 |
| `close_issue` | 写 | 关闭 Issue |
| `comment_on_pr` | 写 | 在 PR 下发布评论 |
| `write_file` | 写 | 创建或更新仓库文件 |
| `create_branch` | 写 | 创建新分支 |
| `create_pull_request` | 写 | 创建 Pull Request |
| `dispatch_workflow` | 写 | 触发 GitHub Actions workflow |
| `read_workflow_run` | 读 | 查看 workflow 运行状态和结果 |
| `run_command` | 写 | 在仓库工作目录执行 allowlist 内的开发命令（pytest/ruff/mypy/pyright 等） |
| `no_reply` | 控制 | 明确选择不公开回复，避免无意义 fallback 评论 |

单次执行最多进行 `MAX_ITERATIONS` 轮工具调用（默认 100）。如果 LLM 连续调用工具而不给出最终文本回复，循环结束后返回 fallback 消息；如果调用 `no_reply`，本轮会静默结束。

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
