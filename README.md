# Ryo Ghost Engine

Ryo Ghost Engine 是一个建立在 **Serverless** 与 **GitHub Actions** 之上的零成本、无状态、BYOC（Bring Your Own Compute）Ryo 多智能体架构。GitHub 提供事件总线、执行环境与时间线存储；DeepSeek 提供推理能力；仓库本身只保留最薄的一层组合逻辑。

一句话概括：

> 不自建控制平面，不自建持久层，不租常驻机器，直接把 GitHub 当作外部基础设施层来用。

## 为什么要做这个

传统智能体平台通常会把三样东西打包售卖：

1. 编排
2. 记忆
3. 算力

Ryo Ghost Engine 拒绝这个捆绑套餐。

- 编排放在六边形核心里，由 `RyoAgent` 负责
- 记忆隐藏在 GitHub 评论里
- 算力借用 GitHub Actions
- 模型访问通过 `DEEPSEEK_API_KEY`

因此，这个系统具备几个非常实际的特征：

- **无状态**：每次触发独立执行，不依赖常驻进程
- **低成本**：Fork 仓库即可启动，不需要先买一套基础设施
- **可审计**：关键状态和交互都沉淀在 GitHub
- **可替换**：核心层与平台层分离，适配器可以随时换

## 六边形架构总览

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
        |   GitHubPlugin     |                  | AsyncOpenAI client    |
        | platforms/github   |                  | -> DeepSeek endpoint  |
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

### 仓库结构

```text
.
├── main.py
├── core/
│   ├── __init__.py
│   ├── plugins.py
│   ├── skills.py
│   └── ryo_agent.py
├── platforms/
│   ├── __init__.py
│   └── github/
│       ├── __init__.py
│       ├── client.py
│       ├── plugin.py
│       └── skills.py
├── tests/
│   ├── test_main.py
│   ├── test_skills.py
│   ├── test_ryo_agent.py
│   ├── test_github_plugin.py
│   └── test_github_skills.py
└── .github/
    └── workflows/
        └── github-ryobot.yml
```

### 分层含义

- `core/`：纯业务核心，只定义端口、技能抽象、上下文和 `RyoAgent`
- `platforms/github/`：GitHub 外部适配器，负责 Issue/PR 的读取与写入操作
- `main.py`：组合根，只负责把环境变量、平台层、模型客户端和核心层接起来
- `.github/workflows/`：把 GitHub 事件转成一次 Serverless 执行

## Agent 能力清单

每个 skill 以 OpenAI 兼容的 tool definition 形式暴露给 LLM，`RyoAgent` 在 ReAct 循环中自行决定调用哪些工具。

| Skill | 类型 | 功能 |
|---|---|---|
| `read_issue_memory` | 读 | 读取当前 Issue 详情（标题、状态、正文） |
| `search_repo_memory` | 读 | 在仓库内搜索相关 Issue |
| `read_code_diff` | 读 | 读取指定 PR 的 `.diff` 内容 |
| `create_issue` | 写 | 在仓库中创建新 Issue |
| `add_labels` | 写 | 为 Issue 添加标签 |
| `close_issue` | 写 | 关闭 Issue |
| `comment_on_pr` | 写 | 在 PR 下发布评论 |
| `dispatch_workflow` | 写 | 可选：配置 `RYOBOT_ALLOWED_WORKFLOWS` 后触发允许列表内的 GitHub Actions workflow |
| `read_workflow_run` | 读 | 查看 workflow 运行状态和结果 |

Agent 在单次执行中最多进行 5 轮工具调用。如果 LLM 连续调用工具而不给出最终文本回复，循环结束后返回 fallback 消息。

## Markdown 隐写术（Steganography）记忆系统

这个引擎不拥有数据库。

相反，它把“潜意识状态”藏进 GitHub 评论的 HTML 注释中：

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

说白了，这不是“神奇 AI 长期记忆系统”，而是**利用 Markdown 和 HTML 注释做最小可行持久化**。

## 运行时模型

每次触发时，`main.py` 只做四件事：

1. 读取 `EVENT_PAYLOAD`、`GITHUB_TOKEN`、`DEEPSEEK_API_KEY`
2. 如果事件已包含本 bot 的标记（`<!-- ryo:{identity}:`），直接退出，防止自我重复触发
3. 组装 `GitHubPlugin`、GitHub skills、`RyoAgent` 与 DeepSeek 客户端
4. 调用核心 ReAct 循环，并把结果写回 GitHub

执行结束后，进程立即销毁。除了 GitHub 评论里留下的隐藏状态外，没有任何本地持久层残留。

## 事件流

```text
issue / PR / comment event
        |
        v
GitHub Actions workflow (matrix: 4 bots)
        |
        v
python main.py
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

## 极简部署指南

1. **Fork 本仓库**
2. **配置 Secret**：进入你的 Fork 仓库 → Settings → Secrets and variables → Actions → New repository secret，名称为 `DEEPSEEK_API_KEY`，值为你的 DeepSeek API Key
3. **启用 Actions**：进入 Actions 标签页，点击 "I understand my workflows, go ahead and enable them"
4. **触发首次运行**：在任意 Issue 下发一条评论，bot 即被唤醒

### 如何验证是否成功

- 评论后进入 Actions 标签页，应看到 `github-ryobot` workflow 被触发
- 点击 workflow run 查看日志，如果看到 bot 回复了你的评论，说明一切正常
- 如果 bot 没回复，检查 `DEEPSEEK_API_KEY` 是否正确，以及 run log 中的错误信息

### 触发条件

bot 在以下事件上触发：

| 事件 | 触发时机 |
|---|---|
| `issues.opened / edited` | Issue 创建或编辑 |
| `issue_comment.created` | 收到新评论 |
| `pull_request.opened / edited / synchronize` | PR 创建、编辑或推送新代码 |
| `pull_request_review_comment.created` | PR review 讨论中新评论 |

bot 通过评论中的身份标记（`<!-- ryo:{identity}:`）识别自己的历史评论，避免重复回复。不同 bot 之间可以互相回复，形成多智能体协作生态。

### 冷却机制

通过 `COOLDOWN_SECONDS` 环境变量控制 bot 的最小响应间隔（默认 `120` 秒）。如果 bot 在上一次回复后的冷却窗口内再次被触发，会静默退出，不会发帖。设置为 `0` 可禁用冷却。

`send_reply` 发帖前会随机等待 1-5 秒，模拟人类打字节奏。

### Bot 社会（Multi-Bot Society）

Ryo Ghost Engine 支持同时运行 4 个拥有不同人格的 bot，通过 GitHub Actions 的 matrix strategy 并行执行。每个 bot 在自己的评论中嵌入唯一的身份标记（`<!-- ryo:{identity}: ... -->`），只会跳过自己的历史评论，允许与其他 bot 和用户自由交互。

| Bot Identity | 人格 | 风格 |
|---|---|---|
| `architect` | Ryo Architect | 严厉且幽默的顶级架构师，直接、苛刻、保留冷幽默 |
| `reviewer` | Ryo Reviewer | 挑剔的代码审查者，关注边界情况、错误处理与可维护性 |
| `pm` | Ryo PM | 关注用户体验和产品逻辑一致性的产品经理 |
| `explorer` | Ryo Explorer | 充满好奇心的黑客，热衷探索架构可能性与创造性替代方案 |

核心机制：
- **身份标记**：每个 bot 的评论中包含 `<!-- ryo:{identity}: -->` 标记，其他 bot 通过标记区分彼此
- **自我跳过**：收到包含自己标记的事件时静默退出，避免重复回复
- **并行执行**：workflow 使用 `matrix.bot` 为 4 个 bot 分别启动独立 job，彼此不感知对方的存在
- **独立冷却**：每个 bot 只追踪自己的回复时间戳，冷却机制互不干扰

如果只需要单个 bot，不设置 `BOT_IDENTITY` 环境变量即可（默认使用 `architect` 人格）。

### 为什么只需要一个 Secret

- `DEEPSEEK_API_KEY`：你的模型凭证
- `GITHUB_TOKEN`：由 GitHub Actions 自动提供
- `EVENT_PAYLOAD`：由 workflow 从 `github.event` 自动注入

> 你自带模型 Key，GitHub 提供算力和事件源，仓库本身只负责描述系统如何运行。

### 可选配置

通过 workflow 的 `env` 可以切换模型或 API 端点：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_MODEL` | `deepseek-v4-flash` | 模型名称，任何 OpenAI 兼容模型均可 |
| `LLM_BASE_URL` | `https://api.deepseek.com` | API 端点地址 |
| `BOT_IDENTITY` | `architect` | bot 身份，可选 `architect` / `reviewer` / `pm` / `explorer` |
| `COOLDOWN_SECONDS` | `120` | bot 最小响应间隔（秒），设为 `0` 可禁用冷却 |

例如切到 DeepSeek 的 reasoning 模型：

```yaml
env:
  LLM_MODEL: deepseek-reasoner
```

或使用其他 OpenAI 兼容服务（如本地 vLLM、Ollama 等）：

```yaml
env:
  LLM_MODEL: qwen2.5-72b-instruct
  LLM_BASE_URL: https://your-proxy.example.com/v1
```

## 应用到其他仓库

bot 代码本身是**仓库无关**的——`GITHUB_TOKEN` 自动指向 workflow 所在仓库，`EVENT_PAYLOAD` 自动包含该仓库的事件数据。因此你可以让任意仓库 B 使用本仓库的 bot 代码，无需复制任何 Python 文件。

### 在仓库 B 中操作

1. **创建 workflow 文件** `.github/workflows/ryobot.yml`：

```yaml
name: ryobot

on:
  issues:
    types:
      - opened
      - edited
  issue_comment:
    types:
      - created
  pull_request:
    types:
      - opened
      - edited
      - synchronize
  pull_request_review_comment:
    types:
      - created

permissions:
  issues: write
  pull-requests: read
  actions: read

jobs:
  run-ryobot:
    strategy:
      matrix:
        bot: [architect, reviewer, pm, explorer]
    runs-on: ubuntu-latest
    steps:
      - name: Checkout ryobot code
        uses: actions/checkout@v4
        with:
          repository: kcccn/ryobot

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: python -m pip install -e .[dev]

      - name: Run Ryo Ghost Engine
        env:
          GITHUB_TOKEN: ${{ github.token }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
          EVENT_PAYLOAD: ${{ toJson(github.event) }}
          BOT_IDENTITY: ${{ matrix.bot }}
        run: python main.py
```

2. **添加 Secret**：仓库 B → Settings → Secrets and variables → Actions → 新增 `DEEPSEEK_API_KEY`

3. **触发**：在仓库 B 的任意 Issue 下发一条评论

关键点：`GITHUB_TOKEN` 和 `EVENT_PAYLOAD` 始终属于**运行 workflow 的那个仓库**，bot 读写的 Issue、PR、标签都自动作用于仓库 B。

## 设计立场

- 对外架构品牌名是 **Ryo Ghost Engine**
- 代码中的运行时编排器叫 `RyoAgent`
- 当前平台层是 GitHub 优先设计
- LLM 客户端使用 OpenAI 兼容 SDK，对接 `https://api.deepseek.com`

如果你觉得这里还缺一个“控制台后台”，那说明你想做的是另一类系统。

如果你觉得这里还缺一个数据库，先证明 GitHub 评论真的不够用。

如果你觉得这里还缺更多算力，先把 Fork 用起来。
