"""上下文占用核算 —— "这一轮还剩多少地方给历史"。

抽成独立模块而不是塞在 app.py 里，有两个原因：

1. 这是个**规则**，不是 HTTP 胶水。它决定前端什么时候该弹"上下文已满"，
   而这条判断如果写错，表现是"模型突然开始遗忘前面说过的约束"，用户会去
   怪模型，不会来查这段代码。
2. app.py 为了拿 torch 要 import 4.5 秒，规则不该被这个代价绑住。这里只
   依赖标准库，单测能毫秒级跑完。

计费口径与 generate/prompt.py 保持一致：按**字符数**粗略当 token 用
（`build_context` 里 `budget = max(200, token_budget - history_text_tokens)`
也是字符口径）。两处口径必须一样，否则前端提示的"还剩多少"和真正装填时
的裁剪结果会对不上，这种不一致比没有提示更糟。
"""

from __future__ import annotations

# build_context 在一分钱预算都不剩时仍然会给模型留一段地板，见
# prompt.py 的 `budget = max(floor, ...)`。实际取值来自
# generation.min_context_chars，这里只留出厂默认给旧调用路径兜底。
MIN_CONTEXT_CHARS = 200


def trim_history(history: list[dict], *, budget: int) -> list[dict]:
    """按 chat.history_token_budget 从最早一轮开始整轮丢弃。

    与 build_context 一样按**字符**计（中文场景下字符≈token 的粗糙近似，
    两边口径必须一致，否则前端说的"还能带几轮"和实际装进去的对不上）。

    `budget <= 0` 表示不带历史，返回空列表——这是"设 0 即单轮问答"的实现，
    比在调用方散落 `if budget:` 更好查。

    三条规则：
    1. **按轮丢，不按条丢。** 只丢 assistant 会留下一串没有提问的回答，
       模型会把它们当成"用户说过的话"，指代全乱。
    2. **最后一轮无条件保留。** 追问的指代对象就在最近一轮里；为省几十个
       字符把它丢掉，等于让多轮对话退化成单轮。超支由 usage() 的 exhausted
       提示和 build_context 的地板共同兜住。
    3. 裁剪后若开头是 assistant（理论上不会，规则 1 已排除），一并丢弃，
       保证第一轮总是从 user 开始。
    """
    if not history or budget <= 0:
        return []

    # 先按"提问+回答"切成轮；孤立的 assistant（上一轮被截断）自成一组
    pairs: list[list[dict]] = []
    i = len(history)
    while i > 0:
        if i >= 2 and history[i - 2].get("role") == "user":
            pairs.append(history[i - 2 : i])
            i -= 2
        else:
            pairs.append(history[i - 1 : i])
            i -= 1
    pairs.reverse()

    kept_pairs: list[list[dict]] = []
    spent = 0
    for pair in reversed(pairs):
        cost = sum(len(m.get("content") or "") for m in pair)
        if kept_pairs and spent + cost > budget:
            break
        kept_pairs.append(pair)
        spent += cost
    kept_pairs.reverse()

    kept = [m for pair in kept_pairs for m in pair]
    while kept and kept[0].get("role") != "user":
        kept.pop(0)
    return kept


def usage(
    history: list[dict],
    *,
    total_messages: int,
    budget: int,
    turns_limit: int,
    min_chars: int = MIN_CONTEXT_CHARS,
) -> dict:
    """算这一轮的历史占用。

    history       —— 真正会进 prompt 的那些消息（已被 turns_limit 与
                     history_token_budget 截过）
    total_messages—— 会话里一共存了多少条消息
    budget        —— generation.context_token_budget（字符口径）
    turns_limit   —— chat.history_turns，界面要显示"最近 N 轮"
    min_chars     —— generation.min_context_chars，与 prompt.py 的地板同源
    """
    floor = max(1, int(min_chars))
    kept = len(history)
    history_chars = sum(len(h.get("content") or "") for h in history)
    total_messages = max(0, int(total_messages))
    budget = max(floor, int(budget))

    left = max(floor, budget - history_chars)
    truncated = total_messages > kept
    saturated = history_chars >= budget

    return {
        "turns_kept": (kept + 1) // 2,
        "turns_total": (total_messages + 1) // 2,
        "truncated": truncated,
        "dropped_messages": max(0, total_messages - kept),
        "history_chars": history_chars,
        "budget": budget,
        "budget_left": left,
        # 只剩地板那点字符：文档片段基本装不进去了，必须提示。
        "saturated": saturated,
        "exhausted": left <= floor and history_chars > 0,
        "history_turns_limit": int(turns_limit),
    }


def notice(ctx: dict | None) -> str | None:
    """给人看的一句话。没问题就返回 None。

    分三档而不是一句"上下文已达上限"：轮次被截、字符预算被吃满、片段被裁，
    是三种不同的处境，用户要做的事也不同（前两种清会话，第三种调预算）。
    """
    if not ctx:
        return None
    if ctx.get("exhausted"):
        return (
            f"上下文已满：历史 {ctx['turns_kept']} 轮占用了约 "
            f"{ctx['history_chars']} / {ctx['budget']} 字符，本轮基本装不进文档片段。"
            "建议刷新会话重新开始。"
        )
    if ctx.get("truncated"):
        return (
            f"早期对话已不再带入：当前只保留最近 {ctx['turns_kept']} 轮"
            f"（共 {ctx['turns_total']} 轮，上限 {ctx['history_turns_limit']} 轮）。"
        )
    return None
