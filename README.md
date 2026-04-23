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
```

### 仓库结构

```text
.
├── main.py
├── core/
│   ├── plugins.py
│   ├── skills.py
│   └── ryo_agent.py
├── platforms/
│   └── github/
│       ├── client.py
│       ├── plugin.py
│       └── skills.py
└── .github/
    └── workflows/
        └── github-ryobot.yml
```

### 分层含义

- `core/`：纯业务核心，只定义端口、技能抽象、上下文和 `RyoAgent`
- `platforms/github/`：GitHub 外部适配器，负责 Issue 评论、搜索、Diff 读取
- `main.py`：组合根，只负责把环境变量、平台层、模型客户端和核心层接起来
- `.github/workflows/`：把 GitHub 事件转成一次 Serverless 执行

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

这是完整的部署流程，没有隐藏步骤：

1. Fork 本仓库
2. 进入你的 Fork 仓库：**Settings -> Secrets and variables -> Actions**
3. 新增一个 Secret：`DEEPSEEK_API_KEY`
4. 启用 GitHub Actions

到这里就够了。

为什么只需要这一个 Secret？

- `DEEPSEEK_API_KEY`：你的模型凭证
- `GITHUB_TOKEN`：由 GitHub Actions 自动提供
- `EVENT_PAYLOAD`：由 workflow 从 `github.event` 自动注入

换句话说：

> 你自带模型 Key，GitHub 提供算力和事件源，仓库本身只负责描述系统如何运行。

## 设计立场

- 对外架构品牌名是 **Ryo Ghost Engine**
- 代码中的运行时编排器叫 `RyoAgent`
- 当前平台层是 GitHub 优先设计
- LLM 客户端使用 OpenAI 兼容 SDK，对接 `https://api.deepseek.com`

如果你觉得这里还缺一个“控制台后台”，那说明你想做的是另一类系统。

如果你觉得这里还缺一个数据库，先证明 GitHub 评论真的不够用。

如果你觉得这里还缺更多算力，先把 Fork 用起来。
