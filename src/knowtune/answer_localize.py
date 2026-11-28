"""问答答案的维语补译（前台 8800 / 后台 8013 共用）。

切语言的定义只有一条：**同一句话换文本，不是让模型重答一遍**。预问诊那边早就有
`ChatEngine.localize()`，问答板块以前只有"中文原稿 + 语种戳"，中文界面问出来的
那条切到维语就只剩中文。这里把同一条翻译链开放给问答板块，两个 app 都挂路由。

翻译器复用预问诊 `ensure_engine()` 的进程级单例——模型地址、密钥、强模型选择都
只有一份来源；不新建客户端，也不读系统级 translate 角色（项目里已删除）。

2026-11-27 起所有译文经过 `translation_cache`：同一句话只翻一次。提问时后端
后台预译写进缓存，切语言时命中即返回（毫秒级），不再让用户对着一屏中文等
12～20 秒。缓存是纯派生产物，删掉只导致重翻，不丢数据。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from typing import Any

from . import translation_cache

# 翻译模型按行切成小段分别翻，既保住换行结构，也让多条能并发。
# 维语译文约为中文的 5 倍长（语言特性，非模型失控），实测 100 字块译 542 字、
# 150 字块译 767 字，单次网关调用 15~20 秒；块太小只会切出更多块、总耗时更长，
# 块太大（300 字以上）则输出逼近 max_tokens 被截断判废、触发重试风暴。
# 180 是实测的折中：长正文约 3~5 块，6 路并发 3 轮内完成。
_CHUNK = 180

# 单次翻译的块数上限：挡的是"一次请求无上限地打云端"这种失控，不是正常用量。
# 一轮答案的主包 = 正文几块 + 每条引用的标题/小节各一块，8 条引用也就 25 块上下，
# 48 留足余量；以前设 24 时，引用一多就会把最后几条标题挤到上限外，那几条就
# 永远停在中文——用户说的"每次都有不翻译的"有一部分正是这么来的。
_MAX_JOBS = 48

# 排版符号属于提示词契约：答案靠 ### / - / [n] 在前端排出层次，
# 译文把这些吃掉就变成一大片等重的字，等于白改。
_SYS = (
    "你是严谨的医学翻译。把下面的中文答案逐行翻译成维吾尔语（阿拉伯字母）。"
    "必须保留原文的换行、`### ` 小标题、`- ` 列表符号和 `[数字]` 引用编号，"
    "一行对一行；只输出译文，不要解释、不要引号、不要改写结构。\n\n"
)

# 同一句话同时只翻一次：后台预译和切语言补译经常撞在同一批文本上。
_INFLIGHT: dict[str, threading.Event] = {}
_INFLIGHT_LOCK = threading.Lock()


def _claim(texts: list[str]) -> tuple[dict[str, threading.Event],
                                     list[tuple[str, threading.Event]]]:
    """登记在飞翻译。返回（本路负责翻的，别的请求正在翻的）。"""
    mine: dict[str, threading.Event] = {}
    others: list[tuple[str, threading.Event]] = []
    with _INFLIGHT_LOCK:
        for t in texts:
            ev = _INFLIGHT.get(t)
            if ev is None:
                ev = threading.Event()
                _INFLIGHT[t] = ev
                mine[t] = ev
            else:
                others.append((t, ev))
    return mine, others


def _release(mine: dict[str, threading.Event]) -> None:
    """放开登记并叫醒等在后面的请求。先摘牌再叫醒，别让新请求抢到已完成的牌。"""
    with _INFLIGHT_LOCK:
        for t, ev in mine.items():
            if _INFLIGHT.get(t) is ev:
                _INFLIGHT.pop(t, None)
    for ev in mine.values():
        ev.set()


def _steal(texts: list[str]) -> dict[str, threading.Event]:
    """把还挂在别人名下、但已经等超时的牌夺过来自己翻。

    上游请求异常退出时可能漏放牌；不夺回来的话，后面每一次切语言都会白等
    45 秒再空手而归——表现就是"整屏中文，怎么切都不翻"。
    """
    mine: dict[str, threading.Event] = {}
    with _INFLIGHT_LOCK:
        for t in texts:
            ev = _INFLIGHT.get(t)
            if ev is not None and not ev.is_set():
                new = threading.Event()
                _INFLIGHT[t] = new
                mine[t] = new
    return mine


def localize_texts(texts: list[str], *, limit: int = 64) -> dict[str, Any]:
    """把中文答案补译成维语。返回 `{"translations": [...], "error": ""}`。

    每条与输入按位置一一对应；翻不出来的回空串，界面保留原文 + 语种戳，
    和预问诊的降级口径完全一致。命中缓存的条不重翻，翻成的条写回缓存。
    """
    clean = [str(x or "")[:12000] for x in texts[:limit]]
    if not clean:
        return {"translations": [], "error": ""}

    # ① 批内去重：同一批 texts 里常有完全相同的句子——问题重复两遍、
    #    同一个文档名在多条引用里出现五次（实测一轮 13 条里重复占 6 条）。
    #    下面 _claim 用文本本身做 key 登记在飞：重复句第一条登记后，其余进
    #    others 等同一个 Event，而那个 Event 要等本次调用翻译到 finally 才
    #    set——于是在【同一次调用里自己等自己】，45 秒超时后 steal 夺回的
    #    还是自己的牌，反复循环直到雪崩。这就是"切语言 180 秒不返回、正文
    #    永远中文"真正的根因，和翻译器、网关都无关。
    #    去重后只翻唯一文本，返回时按位置展开，重复句共用一份译文。
    uniq: list[str] = []
    uniq_seen: set[str] = set()
    for t in clean:
        if t not in uniq_seen:
            uniq_seen.add(t)
            uniq.append(t)

    # ① 先查缓存：预译、上次补译、别的会话翻过的，这里直接命中。
    cached = translation_cache.get(uniq)
    todo = [t for t in uniq if t.strip() and t not in cached]
    if not todo:
        out = [cached.get(t, "") for t in uniq]
        return {"translations": [out[uniq.index(t)] for t in clean], "error": ""}

    # ①' 同一句话只翻一次：提问收尾的后台预译和用户切语言的补译经常同时
    #     打到同一批文本上，两边都翻等于把云端请求量翻倍，冷启动从 25 秒拖到
    #     50 秒，还更容易撞限流。这里按文本登记在飞任务，后到的一路等前一路
    #     落缓存，再从缓存取。
    mine, others = _claim(todo)
    try:
        if others:
            # 等前一路落缓存。上限 45 秒：两路文本集合交错时理论上会互相等，
            # 超时后各自把剩下的翻掉，不会真的卡死。
            deadline = time.time() + 45.0
            for _t, ev in others:
                ev.wait(max(0.0, deadline - time.time()))
            got = translation_cache.get([t for t, _ in others])
            cached.update(got)
            # 前一路没翻出来的自己补上；牌还挂在别人名下的（上游异常没释放）
            # 直接夺回来，否则这一路会空手而归、整屏中文。
            still = [t for t, _ in others if t not in got]
            if still:
                mine.update(_steal(still))
            todo = [t for t in todo if t not in cached and t in mine]
        if not todo:
            out = {t: cached.get(t, "") for t in uniq}
            return {"translations": [out[t] for t in clean], "error": ""}
        result = _translate(uniq, cached, todo)
    finally:
        _release(mine)          # 任何路径都要放行等在同一批文本上的另一路
    # _translate 按 uniq 位置返回，展开回 clean 的原始位置（重复句共用译文）。
    uniq_out = result["translations"]
    if uniq_out:
        pos = {t: i for i, t in enumerate(uniq)}
        result["translations"] = [uniq_out[pos[t]] for t in clean]
    return result


def _translate(clean: list[str], cached: dict[str, str],
               todo: list[str]) -> dict[str, Any]:
    """真正调模型的部分。`cached` 已有译文，`todo` 是本路负责翻的文本。"""
    from .previsit.app import ensure_engine
    from previsit.chat import looks_uyghur

    try:
        eng = ensure_engine()
        tr = eng._translator()
    except Exception as exc:
        return {"translations": [cached.get(t, "") for t in clean], "error": str(exc)}
    if tr is None:
        return {"translations": [cached.get(t, "") for t in clean],
                "error": getattr(eng, "translator_error", "") or "翻译器未装配"}

    clients = list(getattr(tr, "clients", None) or [tr])

    def one(text: str, deadline: float | None = None) -> tuple[str, str]:
        # 总时间预算：网关抖动时一次 complete 最长 20 秒，2 clients × 2 attempts
        # 就是 80 秒/块，再叠加串行补漏的另一轮 160 秒——累计三分钟，用户切
        # 语言就死等"整体切换中"。加预算后超时的块快速返回空、整条降级留原文，
        # 让整页切换尽快收敛，而不是把所有人钉在转圈上。
        last = "没有可用翻译客户端"
        for client in clients:
            if deadline is not None and time.time() >= deadline:
                return "", "翻译超时（超出时间预算）"
            for attempt in range(2):
                if attempt:
                    time.sleep(0.6)     # 云端限流是瞬时的，立刻重试大概率还撞
                if deadline is not None and time.time() >= deadline:
                    return "", "翻译超时（超出时间预算）"
                try:
                    out = client.complete(
                        [{"role": "system", "content": _SYS},
                         {"role": "user", "content": text}],
                        temperature=0.1, max_tokens=3000,
                    )
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"[:160]
                    continue
                out = (out or "").strip().strip("\"'“”‘’ ")
                if looks_uyghur(out) and len(out) > 8:
                    return out, ""
                last = f"译文不合格：{out[:80]!r}"
        return "", last

    # 按行成块：### 小标题和 - 要点各自带着换行进译文，回来照样是一行对一行。
    # 块数封顶：正文优先（前面的 text 先切），来源预览这种长文本排在后面，
    # 超出的块直接丢弃——那条整条判废留原文，不会半中半维。
    jobs: list[tuple[int, str]] = []
    truncated: set[int] = set()          # 切到一半被块数上限掐断的，整条判废
    for i, text in enumerate(todo):
        if len(jobs) >= _MAX_JOBS:
            truncated.add(i)
            continue
        cur: list[str] = []
        size = 0
        lines = text.split("\n")
        for li, line in enumerate(lines):
            if len(jobs) >= _MAX_JOBS:
                truncated.add(i)
                break
            if cur and size + len(line) > _CHUNK:
                jobs.append((i, "\n".join(cur)))
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
        if i in truncated:
            continue
        if cur and len(jobs) < _MAX_JOBS:
            jobs.append((i, "\n".join(cur)))
        elif cur:
            truncated.add(i)

    # 并发度 + 整体预算。以前每块在被调度到时才起算 25 秒，排在后面的块
    # 光等调度就要几十秒，十几块累计 95 秒还没返回——前端 75 秒就 abort 了，
    # 用户看到的就是"切维语一直转圈、最后还是中文"。
    # 现在所有块共享一个 60 秒 deadline：到点还没译出的整条降级留原文，
    # 保证整批有上界。并发 6→10，实测网关扛得住，排队轮次少一半。
    _BATCH_DEADLINE = time.time() + 60.0
    with ThreadPoolExecutor(max_workers=min(10, len(jobs))) as pool:
        pieces = list(pool.map(
            lambda job: one(job[1], _BATCH_DEADLINE), jobs))

    done: list[str] = []
    for i in range(len(todo)):
        seg = [(p, err) for (idx, _), (p, err) in zip(jobs, pieces) if idx == i]
        # 一段没译出来、或切块被上限掐断，就整条留原文 + 戳：
        # 半中半维比全中文更难读。
        ok = seg and i not in truncated and all(p for p, _ in seg)
        done.append("\n".join(p for p, _ in seg) if ok else "")

    # 补漏：并发翻的时候偶尔有一块撞 429，整条就被判废——用户看到的正是
    # "正文还是中文"。这里把失败的条**串行**再翻一遍（一次一块、留间隔），
    # 并发压力归零，成功率接近 100%。只补失败的，正常路径不多花一毫秒。
    # 补漏和并发阶段共用同一个 deadline，不是另起 45 秒——否则整批总时长
    # 还是能冲到 100 秒以上，前端早就 abort 了。没译出的块留给前端重试链。
    _PATCH_DEADLINE = _BATCH_DEADLINE
    for i in range(len(todo)):
        if done[i] or i in truncated:
            continue
        if time.time() >= _PATCH_DEADLINE:
            break
        chunks = [text for idx, text in jobs if idx == i]
        if not chunks:
            continue
        outs: list[str] = []
        for chunk in chunks:
            got = ""
            for attempt in range(2):
                if attempt:
                    time.sleep(1.2)
                out, _err = one(chunk, _PATCH_DEADLINE)
                if out:
                    got = out
                    break
            if not got:
                outs = []
                break
            outs.append(got)
        if outs:
            done[i] = "\n".join(outs)

    # ③ 翻成的写缓存：下次（含刷新、换设备、别的会话）直接命中。
    fresh = {text: out for text, out in zip(todo, done) if out}
    if fresh:
        try:
            translation_cache.put(fresh)
        except Exception:
            pass                      # 缓存写不进去只是退化成"每次重翻"，不影响正文

    merged = {**cached, **fresh}
    ok = sum(1 for t in clean if merged.get(t))
    errs = [err for _, err in pieces if err]
    return {"translations": [merged.get(t, "") for t in clean],
            "error": "" if ok == len(clean)
            else f"{len(clean) - ok} 条未能翻译；" + (errs[0] if errs else "未知原因")}


def warm(texts: list[str]) -> None:
    """后台预译，不阻塞调用方。调用方在提问收尾时把答案 / 问题 / 标题丢进来，
    翻译结果落缓存；用户随后切语言时命中即返。失败静默——前端补译链会兜底。
    """
    todo = [t for t in texts if t and t.strip()]
    if not todo:
        return
    if all(t in translation_cache.get(todo) for t in todo):
        return
    # 线程池独立于单次请求：请求结束不该掐断正在进行的预译。
    _WARM_POOL.submit(_warm_job, todo)


# 预热是"加速"，不是"抢占"：单 worker 排队翻，绝不和前台切语言时的补译
# 抢翻译器 / 撞云端限流。来源文本长，排队慢一点无所谓——它本来就显示在
# 折叠的来源面板里，不是用户第一眼要读的正文。
_WARM_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="localize-warm")


def _warm_job(todo: list[str]) -> None:
    try:
        localize_texts(todo)
    except Exception:
        pass                          # 预译是加速，失败由前端补译链兜底
