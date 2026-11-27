"""问答答案的维语补译（前台 8800 / 后台 8013 共用）。

切语言的定义只有一条：**同一句话换文本，不是让模型重答一遍**。预问诊那边早就有
`ChatEngine.localize()`，问答板块以前只有"中文原稿 + 语种戳"，中文界面问出来的
那条切到维语就只剩中文。这里把同一条翻译链开放给问答板块，两个 app 都挂路由。

翻译器复用预问诊 `ensure_engine()` 的进程级单例——模型地址、密钥、强模型选择都
只有一份来源；不新建客户端，也不读系统级 translate 角色（项目里已删除）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

# 翻译模型在 250 字以上就开始漏译/回原文（实测 150/250 字过，294 字整条被
# 质量闸门判废）。按行切成小段分别翻，既保住换行结构，也让多条能并发。
_CHUNK = 180

# 排版符号属于提示词契约：答案靠 ### / - / [n] 在前端排出层次，
# 译文把这些吃掉就变成一大片等重的字，等于白改。
_SYS = (
    "你是严谨的医学翻译。把下面的中文答案逐行翻译成维吾尔语（阿拉伯字母）。"
    "必须保留原文的换行、`### ` 小标题、`- ` 列表符号和 `[数字]` 引用编号，"
    "一行对一行；只输出译文，不要解释、不要引号、不要改写结构。\n\n"
)


def localize_texts(texts: list[str], *, limit: int = 8) -> dict[str, Any]:
    """把中文答案补译成维语。返回 `{"translations": [...], "error": ""}`。

    每条与输入按位置一一对应；翻不出来的回空串，界面保留原文 + 语种戳，
    和预问诊的降级口径完全一致。同一句只翻几次由调用方保证（存 `ug` 字段）。
    """
    from .previsit.app import ensure_engine
    from previsit.chat import looks_uyghur

    clean = [str(x or "")[:12000] for x in texts[:limit]]
    clean = [x for x in clean if x.strip()]
    if not clean:
        return {"translations": [], "error": ""}

    try:
        eng = ensure_engine()
        tr = eng._translator()
    except Exception as exc:
        return {"translations": [""] * len(clean), "error": str(exc)}
    if tr is None:
        return {"translations": [""] * len(clean),
                "error": getattr(eng, "translator_error", "") or "翻译器未装配"}

    clients = list(getattr(tr, "clients", None) or [tr])

    def one(text: str) -> tuple[str, str]:
        last = "没有可用翻译客户端"
        for client in clients:
            for _ in range(2):
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
    jobs: list[tuple[int, str]] = []
    for i, text in enumerate(clean):
        cur: list[str] = []
        size = 0
        for line in text.split("\n"):
            if cur and size + len(line) > _CHUNK:
                jobs.append((i, "\n".join(cur)))
                cur, size = [], 0
            cur.append(line)
            size += len(line) + 1
        if cur:
            jobs.append((i, "\n".join(cur)))

    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        pieces = list(pool.map(lambda job: one(job[1]), jobs))

    done: list[str] = []
    for i in range(len(clean)):
        seg = [(p, err) for (idx, _), (p, err) in zip(jobs, pieces) if idx == i]
        # 一段没译出来就整条留原文 + 戳：半中半维比全中文更难读。
        done.append("\n".join(p for p, _ in seg) if seg and all(p for p, _ in seg) else "")
    ok = sum(1 for x in done if x)
    errs = [err for _, err in pieces if err]
    return {"translations": done,
            "error": "" if ok == len(done)
            else f"{len(done) - ok} 条未能翻译；" + (errs[0] if errs else "未知原因")}
