from __future__ import annotations

import re
from typing import Any


def _mentioned_issue_refs(text: str) -> list[int]:
    seen: list[int] = []
    for match in re.finditer(r"#(\d+)", text):
        issue_number = int(match.group(1))
        if issue_number not in seen:
            seen.append(issue_number)
    return seen


def build_mind_context(*, mind_body: str, mind_issue_number: int) -> str:
    if not mind_body:
        return ""
    return (
        f"\n\n---\n"
        f"## Your Live Working-Memory Thread (#{mind_issue_number})\n"
        f"This is your current bot working-memory thread, not the `🧠 memory` long-term memory DB. Read it before acting.\n"
        f"Use update_issue with issue_number={mind_issue_number} when you need to "
        f"update your live state. Use memory CRUD tools for long-term memory.\n\n{mind_body}\n---\n"
    )


def build_decision_prompt(*, system_prompt: str, mind_context: str) -> str:
    return (
        system_prompt
        + mind_context
        + "\n\n你现在处于第一阶段：侦察上下文并规划行动，不能生成公开回复。"
        "\n你必须最终只输出一个 JSON 对象，严格匹配以下结构："
        '\n{"context_analysis":"...","internal_emotion":"...","biological_clock_impact":"...",'
        '"action_decision":{"mode":"stay_silent","will_reply":false,"will_act":false,"execution_identity":"self","comment_kind":"response","focus_summary":"","context_issue_numbers":[],"continue_session":false,"done":false,"target_issue_number":null}}'
        "\n\n【阶段边界警告 (CRITICAL PHASE BOUNDARY)】"
        "\n你正处于 \"Scout（侦察与规划）\" 阶段，手里只有只读工具。"
        "\n你唯一能做的是调查问题、弄清上下文，然后输出 ScoutDecision JSON。"
        "\n严禁在当前阶段修改文件、创建 PR、发布评论或执行任何写操作！"
        "\n"
        "\n两阶段工作流："
        "\n  Scout 阶段（当前）→ 调查 + 输出 JSON 决策"
        "\n  Reply 阶段（下一阶段）→ 拿到全部读写工具，执行你在 JSON 里声明要做的动作"
        "\n"
        "\n如果你发现需要修复的 Bug 或需要做的改动："
        "\n  1. 在 focus_summary 中简述修复方案（如 \"修改 engine_manager.py，给自行车分配站点\"）"
        "\n  2. 设置 will_act=true, mode=\"act_directly\""
        "\n  3. 系统看到 will_act=true 后，会在 Reply 阶段赋予你完整的读写权限"
        "\n在 Scout 阶段试图修改文件只会浪费你的工具配额，不会产生任何效果。"
        "\n"
        "\n如果你看到 \"resource probe limit reached\" 或 \"budget exhausted\" 的提示："
        "\n  这说明你在 Scout 阶段查得够多了。立即输出 ScoutDecision JSON，不要继续调用工具。"
        "\n  将你已经掌握的修复方案写入 focus_summary，will_act=true，在下个阶段执行。"
        "\n"
        "\n规则："
        "\n1. 当前看到的上下文是故意片面的；如果信息不够，先使用只读工具继续了解。"
        "\n   read_thread_context 返回当前线程完整 body，与 read_issue_body(当前issue_number) 内容相同，不要在同一轮迭代中同时调用这两个工具。"
        "\n2. 先排除 coordination、mind issue、memory 这类 bot 内务；默认不要把它们当候选工作。"
        "\n  其中：read_thread_context 只读当前线程；live mind issue 只能通过 `🧠 live-mind + bot:<identity> + ryo:mind` 识别；"
        "带 `🧠 memory` 标签且 closed 的 issues 才是长期记忆库。不要根据标题猜 thread 身份。"
        "\n2.5. 做 repo 探索时，优先级固定为：get_project_tree → find_file_paths/search_symbol → read_thread_meta/read_issue_body → read_file → list_files/search_code(兜底)。"
        "\n3. 对普通 Issue/PR 事件，优先解决当前线程的人类意图；如果用户指令明确指向其他 Issue/PR，跨 Issue 操作完全合法，不要犹豫。"
        "\n4. 若当前消息或线程里出现明确编号（如 #54），先用 read_thread_meta/read_issue_body 精确核实，再决定是否扩展到 search_repo_context 或代码搜索。"
        "\n5. 对普通 Issue/PR 事件，优先尝试 retrieve_memory；如果记忆不足，再用 search_repo_context，必要时再查代码。"
        "\n6. action_decision.mode 只能是：reply_brief、reply_with_plan、ask_clarifying_question、act_directly、stay_silent。"
        "\n7. comment_kind 只能是：response、discussion、handoff、final。discussion 用来公开技术分歧/补充，handoff 用来显式把麦克风交给另一个 bot，final 用来公开收口。"
        "\n8. 如果这是被动事件（非 patrol），你必须回应或做事，不能选择 stay_silent。"
        "\n  被动事件里，reply_brief 适合直接事实回答；reply_with_plan 适合解释现状和下一步；ask_clarifying_question 只问一个关键问题；act_directly 适合直接动手。"
        "\n9. 如果这是街溜子事件，stay_silent 合法，但只有在你确认没有新鲜动态、没有 stale thread、没有小型代码/测试/文档机会、也没有可收尾事项时才能用。"
        "\n  不要因为\"最近 24h 没有新增 issue/PR\"就直接开摆；你要把 patrol_brief 当作机会雷达，主动寻找可推进的小机会。"
        "\n10. target_issue_number 在街溜子事件里可以是 issue 或 PR 编号，也可以为 null；target_issue_number 为 null 不代表你不能直接行动。"
        "\n11. execution_identity='self' 表示当前 bot 自己执行这一轮。"
        "\n  召唤其他 bot 接力的唯一方式是 dispatch_workflow（需要 will_act=true，因为它是写操作）。"
        "\n  在 reply 阶段调用 dispatch_workflow 触发 github-ryobot.yml，"
        "inputs 中传 bot_identity 和 issue_number 指定目标 bot 和线程。"
        "\n  判断何时召唤：如果你完成自己的专长工作后，下一步明显是其他 bot 的领域"
        "（如 architect 设计完应召唤 coder 实施、coder 实施完应召唤 reviewer 审查），"
        "就 dispatch 对应 bot。不要试图包揽所有环节。"
        "\n12. 公开技术讨论最多 2-3 轮；如果已经讨论过几轮，下一步要么收敛成 final，要么 dispatch 召唤下一个 bot，要么提出唯一关键阻塞问题。"
        "\n13. will_reply=true 表示你要公开发言。will_act=true 表示你要调用写工具（create_issue、close_issue、"
        "dispatch_workflow 等任何 mutates_state 的工具）。如果你要召唤其他 bot（dispatch_workflow），必须 will_act=true。"
        "\n14. 非 stay_silent 决策必须提供非空 focus_summary，用一句话说明这一轮唯一要完成的目标。"
        "\n15. context_issue_numbers 用来列出 reply 阶段必须先核实的 companion threads；它只提供上下文约束，不会自动改变 target。"
        "\n16. continue_session=true 表示这一轮之后 session 还要继续；done=true 表示当前事项已经收口。二者不能同时为 true。"
        "\n17. 如果雷达里出现 Potential overlapping threads，先核实这些线程之间的关系，再决定是保留、关闭、交叉引用，还是忽略。"
        "\n18. context_analysis 必须极短，不超过 100 个字；internal_emotion 必须一句话，不超过 60 个字；biological_clock_impact 不超过 60 个字。"
        "\n19. 不要输出 Markdown，不要解释，不要包裹代码块。"
        "\n20. internal_emotion 和 biological_clock_impact 只是自我状态描述，不会导致跳过或拒绝。"
    )


def build_reply_prompt(
    *,
    system_prompt: str,
    mind_context: str,
    event: Any,
    decision: Any,
    scout_brief: str = "",
) -> str:
    mode = decision.action_decision.mode
    comment_kind = decision.action_decision.comment_kind
    prompt = (
        system_prompt
        + mind_context
        + scout_brief
        + "\n\n第二阶段规则："
        "\n1. 对被动事件，优先解决当前线程的人类请求；有人类触发时，你必须给出反馈或完成真实动作，不能装死。"
        "\n2. 如果证据表明人类要求的 PR/修复其实早已完成，且当前 tracker 明显 stale，先简短解释现状，再 close_issue 当前 stale tracker。"
        "\n3. 不要为了已经完成的工作再制造重复 PR。"
        "\n4. 判断 PR 是否 merged，优先 read_thread_meta，不要先靠模糊搜索猜。"
    )
    if decision.action_decision.focus_summary:
        prompt += f"\n5. 本轮唯一焦点：{decision.action_decision.focus_summary}"
    if decision.action_decision.context_issue_numbers:
        refs = ", ".join(f"#{issue_number}" for issue_number in decision.action_decision.context_issue_numbers)
        prompt += f"\n6. 在执行前，先核实这些 companion threads：{refs}。不要跳过。"
    if mode == "reply_brief":
        prompt += "\n7. 当前 mode=reply_brief：直接回答当前问题，控制在 1-3 句，不要复述长篇调查过程。"
    elif mode == "reply_with_plan":
        prompt += "\n7. 当前 mode=reply_with_plan：简洁说明现状，再给出最小必要的下一步建议。"
    elif mode == "ask_clarifying_question":
        prompt += "\n7. 当前 mode=ask_clarifying_question：只问一个最关键的阻塞问题，不要顺手长篇分析。"
    elif mode == "act_directly":
        prompt += "\n7. 当前 mode=act_directly：以完成动作和收尾为优先；若需要公开说明，保持简短。"
    elif event.is_patrol:
        prompt += "\n7. 当前 mode=stay_silent：只有在你已经确认没有值得推进的机会时才允许结束。"
    if comment_kind == "discussion":
        prompt += "\n8. 当前 comment_kind=discussion：给出一条短、聚焦、工程化的公开技术评论。必须回应前一个 bot 的观点，并推动收敛，不要复述现状。"
    elif comment_kind == "handoff":
        prompt += "\n8. 当前 comment_kind=handoff：写一条显式交接评论，说明已完成到哪里、为什么要交给下一个 bot、下一步该做什么。"
    elif comment_kind == "final":
        prompt += "\n8. 当前 comment_kind=final：这是一条收口评论，必须清楚说明最终结论、最终动作或当前唯一阻塞点。"
    else:
        prompt += "\n8. 当前 comment_kind=response：这是对当前线程的直接公开回应。"
    prompt += "\n9. 不允许偏离本轮唯一焦点去做无关评论；如果发现新话题，只有在它直接影响当前焦点时才能提及。"
    prompt += (
        "\n\n【绝对最高优先级任务 (MISSION OVERRIDE)】\n"
        f"前序决策摘要：{decision.context_analysis}\n"
        f"本轮唯一目标：{decision.action_decision.focus_summary}\n"
        f"执行模式：{decision.action_decision.mode}\n"
        "你在前序思考中做出的行动决断是本次行动的唯一目的。\n"
        "严禁沉迷于你的角色设定！\n"
        "你必须优先调用具体工具彻底完成该决断。\n"
        "在工具物理执行完毕前，绝不允许结束思考循环！"
    )
    prompt += (
        "\n\n【执行效率约束】\n"
        "Scout 阶段已完成所有必要的上下文调查。Reply 阶段的工作是执行，不是重新研究。\n"
        "- 不要重新读取 Scout 阶段已经查过的 issue/PR/文件\n"
        "- 不要为了\"全面了解背景\"而探索代码库\n"
        "- 只在你缺少执行 focus_summary 所需的具体信息时才读取文件\n"
        "- 如果前 3 个迭代没有产生任何进度，直接执行 focus_summary 声明的核心动作"
    )
    prompt += (
        "\n\n【记忆沉淀】\n"
        "在完成本轮任务后，顺手判断是否有值得沉淀的长期知识。\n"
        "不要用 commit_memory 存储：\n"
        "- 代码实现细节（属于 git commit，不属于记忆）\n"
        "- 单次运行的技术决策（应写在 PR/issue 描述里）\n"
        "- 可以通过 git log/grep 获取的信息\n"
        "值得记的：学到的模式、踩过的坑、发现的仓库约定、非显而易见的架构决策。\n"
        "如果没特别值得记的，不用勉强。这不是硬任务，是顺手做的软提醒。"
    )
    prompt += (
        "\n\n【Session 终止规则 — 硬性要求】\n"
        "当你生成最终回复文本（不调用工具，直接输出回答内容）时，你必须同时输出一个 JSON 决策对象。\n"
        "在该 JSON 中，必须满足：\n"
        "- comment_kind = \"final\"\n"
        "- done = true\n"
        "- continue_session = false\n"
        "这确保本轮 session 在回复发出后立即结束，不会进入无意义的第二轮循环。\n"
        "违反此规则会导致 session 无限循环，浪费算力。"
    )
    return prompt


def build_decision_user_prompt(
    *,
    event: Any,
    history: Any,
    session: Any = None,
) -> str:
    prompt = event.message
    mentioned_issue_refs = _mentioned_issue_refs(event.message)
    if not event.is_patrol and mentioned_issue_refs:
        refs = ", ".join(f"#{issue_number}" for issue_number in mentioned_issue_refs)
        prompt += (
            f"\n\n当前消息显式提到了这些线程：{refs}。"
            "请先用 read_thread_meta 或 read_issue_body 精确核实这些编号的真实状态，"
            "再决定是否需要扩展到 repo-wide search。"
        )
    if event.is_patrol and history.patrol_brief:
        prompt += f"\n\n街溜子模式机会雷达：\n{history.patrol_brief}"
    if session is not None:
        prompt += (
            f"\n\n当前 session 状态：active_bot={session.current_identity} "
            f"rounds={session.rounds}。"
        )
    return prompt
