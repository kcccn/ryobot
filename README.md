# 📜 ryobot 架构白皮书

## 🧠 核心架构哲学 (Core Philosophy)
Nexus Ghost Engine (NGE) 是一套基于 **无服务器状态机 (Serverless State Machine)** 理念构建的多智能体 GitHub 自治框架。
它摒弃了传统 SaaS 的重度依赖（无需外置数据库、无需常驻服务器、无需复杂的 App 鉴权），全面拥抱 **BYOC (Bring Your Own Compute)** 模式。通过极致压榨 GitHub Actions 的原生能力与大模型的推理边界，实现真正的“零运维、一键寄生”。

---

## 🗺️ 系统拓扑流 (Data & Execution Flow)

系统采用纯粹的单向事件驱动架构：

```text
[人类/外部动作] 
      │ (触发 Issue Comment / Opened)
      ▼
[GitHub 事件总线] ──(Webhooks 内部路由)──┐
                                       │ 
┌──────────────────────────────────────┼──────────────────────────────────────┐
│ GitHub Actions Runner (瞬态虚拟机)   │                                      │
│                                      ▼                                      │
│  1. 拦截器 (Concurrency Group)  <-- [检查是否正在处理当前 Issue，防止并发]  │
│                                      │                                      │
│  2. 状态机 (engine.js)          <-- [读取 Issue 上下文，判断防死循环锁]     │
│                                      │                                      │
│  3. 人格路由 (nexus.config)     <-- [抽取随机 Agent，拼装 System Prompt]    │
│                                      │                                      │
│  4. 算力请求 ────────────────────────┼─────────> [ DeepSeek API ]           │
│                                      │           (流式/同步推理)            │
│  5. 伪装回写 (Octokit) <─────────────┘                                      │
└─────────┬───────────────────────────────────────────────────────────────────┘
          │ (通过原生 secrets.GITHUB_TOKEN 免密写入)
          ▼
[GitHub Issue 物理时间线] -> (幽灵发言落地，触发新一轮沉睡)
```

---

## 🛠️ 核心模块解析 (Module Breakdown)

### 1. 触发与算力分配层 (Action Trigger)
* **组件**: `.github/workflows/ghost-engine.yml`
* **职责**: 充当系统的“心脏起搏器”。仅在监听范围内（如 Issue 创建、新评论）被唤醒。
* **机制**: 每次唤醒，GitHub 会免费分配一台 Ubuntu 虚拟机（Runner），注入运行时环境变量（包含触发事件的 JSON 负载），执行完毕后立刻销毁，实现绝对的无状态安全隔离。

### 2. 状态与并发控制层 (Concurrency & State Management)
这是本架构最精妙的“减法”，彻底砍掉了对 Redis 等外部缓存的依赖。
* **物理防并发**: 利用 GitHub Actions 的 `concurrency: group` 语法。同一 Issue 内的连续高频评论会被强制排队或取消，从根源上杜绝了大模型 API 被瞬时高并发击穿。
* **历史防抖锁**: 利用 GitHub 自身的 API（读取当前 Issue 的 Timeline）。通过校验“最后一条评论的发送者”及“时间戳”，实现天然的冷却校验（例如：判断距离上次 Bot 发言是否超过 60 秒）。

### 3. 人格与策略路由层 (Persona Router)
* **组件**: `nexus.config.json` 与 `engine.js` 中的抽卡逻辑。
* **职责**: 系统的“灵魂容器”。将配置化的 Agent 数据（包括模型选择、温度值、系统提示词、Markdown 视觉伪装头部）转化为大模型可读的上下文。
* **扩展性**: 支持动态增删 Agent，支持为不同 Agent 挂载不同的 LLM 提供商（目前默认对接 DeepSeek，通过更改 `baseURL` 极易扩展至 OpenAI/OpenRouter）。

### 4. 执行与回写层 (Execution & Write-back)
* **组件**: `@octokit/rest`
* **职责**: 充当系统的“赛博机械臂”。
* **机制**: 利用 GitHub Action 原生提供的临时高权限凭证 `secrets.GITHUB_TOKEN`，无需复杂的 JWT/App 私钥签名，直接以 `github-actions[bot]` 的官方合法身份将 DeepSeek 的回复物理写入代码仓库。

---

## 🛡️ 极客级防御机制 (Security & Defenses)

在通用部署环境下，引擎默认开启以下“阿西莫夫定律”级别的熔断保护：

1. **同族互斥定律 (Bot Exclusion)**：引擎读取到触发者 `sender.type === 'Bot'` 时，将强制无条件休眠，彻底掐断 AI 与 AI 之间的无限对话套娃（Model Collapse）。
2. **算力熔断阀 (Cost Firewall)**：由于采用 BYOC 模式，开发者需自行在仓库配置 `DEEPSEEK_API_KEY`。即使遭受恶意流量攻击，损失也严格限制在开发者自行设置的 DeepSeek 账单阈值内，不会造成不可控的经济灾难。
3. **最小特权原则 (PoLP)**：Action 仅被授予 `issues: write` 级别的权限，引擎无法修改核心业务代码或更改仓库结构，保证主库绝对安全。

---

## 🚀 交付与部署矩阵 (Deployment Matrix)

对于最终的开源生态用户，整个引擎的接入被压缩到了极致的 **Two-Step Protocol (两步协议)**：

1. **Fork 仓库 / 拷贝模板**：将 `nexus.config.json` 和 `engine.js` 及 Action 脚本放入目标仓库。
2. **注入灵魂 (配置 Secret)**：在目标仓库的 Actions Secrets 中填入 `DEEPSEEK_API_KEY`。

**完成。** 没有任何外部 SaaS 平台的注册，没有任何域名的绑定。代码提交的瞬间，赛博幽灵即刻在代码的阴影中苏醒。