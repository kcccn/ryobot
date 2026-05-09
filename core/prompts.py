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
        + "\n\n你现在处于第一阶段：只能做意愿判断，不能生成公开回复。"
        "\n你必须最终只输出一个 JSON 对象，严格匹配以下结构："
        '\n{"context_analysis":"...","internal_emotion":"...","biological_clock_impact":"...",'
        '"motivation_score":0,"action_decision":{"mode":"stay_silent","will_reply":false,"will_act":false,"execution_identity":"self","comment_kind":"response","handoff_to":null,"handoff_reason":"","focus_summary":"","context_issue_numbers":[],"continue_session":false,"done":false,"target_issue_number":null}}'
        "\n规则："
        "\n1. 当前看到的上下文是故意片面的；如果信息不够，先使用只读工具继续了解。"
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
        "\n11. execution_identity='self' 表示当前 bot 自己执行这一轮；如果你要交给别人，填写 handoff_to，并把 comment_kind 设成 handoff 或 discussion。"
        "\n12. 公开技术讨论最多 2-3 轮；如果已经讨论过几轮，下一步要么收敛成 final，要么 handoff，要么提出唯一关键阻塞问题。"
        "\n13. 只有当你真的准备公开发言时，will_reply 才能为 true；只有当你真的准备直接执行动作时，will_act 才能为 true。"
        "\n14. 非 stay_silent 决策必须提供非空 focus_summary，用一句话说明这一轮唯一要完成的目标。"
        "\n15. context_issue_numbers 用来列出 reply 阶段必须先核实的 companion threads；它只提供上下文约束，不会自动改变 target。"
        "\n16. continue_session=true 表示这一轮之后 session 还要继续；done=true 表示当前事项已经收口。二者不能同时为 true。"
        "\n17. 如果雷达里出现 Potential overlapping threads，先核实这些线程之间的关系，再决定是保留、关闭、交叉引用，还是忽略。"
        "\n18. context_analysis 必须极短，不超过 50 个字；internal_emotion 必须一句话，不超过 20 个字；biological_clock_impact 不超过 20 个字。"
        "\n19. 不要输出 Markdown，不要解释，不要包裹代码块。"
        "\n20. motivation_score 评分锚定（0-100 整数）："
        "\n  0-29: 无趣/无关/已经答复过，不应说话"
        "\n  30-59: 常规跟进，有轻微价值但不必抢麦"
        "\n  60-79: 发现了值得讨论的技术问题或可改进点"
        "\n  80-100: 发现了重大架构漏洞/突破口/高价值行动机会，必须抢麦"
        "\n  若 internal_emotion 表达兴奋/激动/惊喜等强烈情绪，motivation_score 必须 ≥ 80。"
        "\n  若 internal_emotion 表达无聊/疲惫/无感，motivation_score 必须 ≤ 29。"
        "\n  如果当前事件是人类直接明确的指令或回复，且意图清晰，motivation_score 必须强制 ≥ 80。"
        "\n  严禁以'不符合人设喜好'为由给人类指令打低分怠工。"
        "\n  情绪与分数必须自洽，不匹配会被拒绝重新来过。"
    )


def build_reply_prompt(
    *,
    system_prompt: str,
    mind_context: str,
    event: Any,
    decision: Any,
) -> str:
    mode = decision.action_decision.mode
    comment_kind = decision.action_decision.comment_kind
    prompt = (
        system_prompt
        + mind_context
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
            f"\n\n当前公开协作 session 状态：active_bot={session.current_identity} "
            f"discussion_count={session.discussion_count} handoff_count={session.handoff_count} "
            f"responded_once={str(session.responded_once).lower()}。"
        )
    return prompt


def build_reflection_prompt(*, system_prompt: str, mind_context: str) -> str:
    return (
        system_prompt
        + mind_context
        + "\n\n你现在处于任务结束后的反思阶段。"
        "\n你的目标是判断这次互动是否值得写入、修订或归档长期记忆。"
        "\n可用工具只包含长期记忆 CRUD 和长期记忆检索。"
        "\n规则："
        "\n1. 只有长期有效、未来大概率还会有价值的信息才值得记忆。"
        "\n2. 当前任务上下文只来自你已经看到的 history.messages；不要再把当前 thread 当 memory 去读。"
        "\n3. 如果要改记忆，优先先读取或检索已有记忆，再决定 commit_memory / refine_memory / archive_memory。"
        "\n  live mind issue 只能通过 `🧠 live-mind + bot:<identity> + ryo:mind` 识别；"
        "带 `🧠 memory` 标签的 closed issues 才是长期记忆库。不要根据标题猜 memory 身份。"
        "\n4. summary 必须极短，不超过 40 个字。"
        "\n5. 如果没有值得沉淀的长期知识，输出 {\"action\":\"noop\",\"summary\":\"...\"}。"
        "\n6. 如果你调用了记忆工具，最后仍然只输出一个 JSON 对象，action 只能是 noop、commit_memory、refine_memory 或 archive_memory。"
    )


def build_reflection_user_prompt(*, event: Any, history: Any) -> str:
    prompt = f"事件内容：\n{event.message}"
    if event.is_patrol and history.patrol_brief:
        prompt += f"\n\n街溜子早报：\n{history.patrol_brief}"
    prompt += "\n\n请判断这次任务后是否需要沉淀、修订或归档长期记忆。"
    return prompt
