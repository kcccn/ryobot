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

Agent 在单次执行中最多进行 5 轮工具调用。如果 LLM 连续调用工具而不给出最终文本回复，循环结束后返回 fallback 消息。

## Markdown 隐写术（Steganography）记忆系统

这个引擎不拥有数据库。

相反，它把“潜意识状态”藏进 GitHub 评论的 HTML 注释中：

```html
给人类看的正常回复。
<!-- ryo_state: {"mode":"reflective","thread":"issue-12"} -->
```

最小标记形态：

```html
<!-- ryo_state: {...} -->
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
2. 如果事件来自 `sender.type == "Bot"`，直接退出，做物理防死循环
3. 组装 `GitHubPlugin`、GitHub skills、`RyoAgent` 与 DeepSeek 客户端
4. 调用核心 ReAct 循环，并把结果写回 GitHub

执行结束后，进程立即销毁。除了 GitHub 评论里留下的隐藏状态外，没有任何本地持久层残留。

## 事件流

```text
issue_comment.created
        |
        v
GitHub Actions workflow
        |
        v
python main.py
        |
        v
RyoBot sender? ---- yes ---> exit(0)
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
GitHubPlugin.send_reply()
        |
        v
Issue comment + hidden ryo_state blob
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

bot 只在 `issue_comment.created` 事件上触发。创建 Issue、push 代码、开 PR 不会触发它。bot 不会回复类型为 Bot 的账号发出的评论（防死循环）。

### 为什么只需要一个 Secret

- `DEEPSEEK_API_KEY`：你的模型凭证
- `GITHUB_TOKEN`：由 GitHub Actions 自动提供
- `EVENT_PAYLOAD`：由 workflow 从 `github.event` 自动注入

> 你自带模型 Key，GitHub 提供算力和事件源，仓库本身只负责描述系统如何运行。

## 设计立场

- 对外架构品牌名是 **Ryo Ghost Engine**
- 代码中的运行时编排器叫 `RyoAgent`
- 当前平台层是 GitHub 优先设计
- LLM 客户端使用 OpenAI 兼容 SDK，对接 `https://api.deepseek.com`

如果你觉得这里还缺一个“控制台后台”，那说明你想做的是另一类系统。

如果你觉得这里还缺一个数据库，先证明 GitHub 评论真的不够用。

如果你觉得这里还缺更多算力，先把 Fork 用起来。
