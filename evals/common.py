"""打分台几个页面共用的底层：端点、生成、题库、文本相似度。

A/B 双模型盲测（arena.py）和单模型多答（sampling.py）都要做同一件事——
「把一段对话喂给模型、拿回一段回答」。这段逻辑只能有一份：

- 两份实现里，超时/重试/错误占位的口径一定会在某次修改后分叉；
- 分叉之后，两个页面打出来的分就不再可比，而那正是"评价"这件事的全部意义。

这里只放**与场景无关**的东西。提示词、评分维度、角色名、题库默认路径全在
`profiles.py` 的评测档案里——预问诊只是其中一份。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

import profiles

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
RUNS.mkdir(exist_ok=True)
DEFAULT_CASES = ROOT / "arena_cases.jsonl"


@dataclass
class Endpoint:
    label: str
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 512
    timeout: float = 180.0

    def url(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label, "base_url": self.base_url, "model": self.model,
            "api_key": self.api_key, "temperature": self.temperature,
            "top_p": self.top_p, "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }


def chat_once(ep: Endpoint, messages: list[dict[str, str]]) -> tuple[str, float, str]:
    """返回 (回复文本, 秒, 错误)。错误非空时文本是给人看的占位。"""
    if not ep.base_url or not ep.model:
        return "", 0.0, "未配置 base_url 或 model"
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    payload = {
        "model": ep.model,
        "messages": messages,
        "temperature": ep.temperature,
        "top_p": ep.top_p,
        "max_tokens": ep.max_tokens,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=ep.timeout, trust_env=False) as client:
            resp = client.post(ep.url(), headers=headers, json=payload)
        cost = time.perf_counter() - started
        if resp.status_code >= 400:
            return "", cost, f"HTTP {resp.status_code}: {resp.text[:180]}"
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if not text.strip():
            return "", cost, "返回内容为空"
        return text.strip(), cost, ""
    except Exception as exc:  # 端点任何异常都不能让页面崩
        return "", time.perf_counter() - started, f"{type(exc).__name__}: {exc}"


def run_dialog(ep: Endpoint, turns: list[str], system: str) -> dict[str, Any]:
    """把一组用户话术依次喂给模型，返回完整对话与耗时。"""
    messages = [{"role": "system", "content": system}] if system.strip() else []
    replies: list[dict[str, Any]] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn})
        text, cost, err = chat_once(ep, messages)
        if err:
            replies.append({"text": f"⚠️ 生成失败：{err}", "ms": int(cost * 1000), "error": err})
            break
        replies.append({"text": text, "ms": int(cost * 1000), "error": ""})
        messages.append({"role": "assistant", "content": text})
    return {
        "turns": list(turns),
        "replies": replies,
        "ms": sum(r["ms"] for r in replies),
        "error": next((r["error"] for r in replies if r["error"]), ""),
    }


def dialog_text(turns: list[str], replies: list[dict[str, Any]],
                user_role: str = "用户", ai_role: str = "助手") -> str:
    """渲染成可读对话。角色名来自档案，所以它和 state 里存的名字必须一致——
    不一致的话 `edited`（人改过没有）会恒为 True，那个标记就废了。"""
    u = (user_role or "用户").strip()
    a = (ai_role or "助手").strip()
    lines: list[str] = []
    for i, turn in enumerate(turns):
        lines.append(f"{u}：{turn}")
        if i < len(replies):
            lines.append(f"{a}：{replies[i]['text']}")
        else:
            lines.append(f"{a}：（未生成）")
        lines.append("")
    return "\n".join(lines).strip()


def resolve_cases(path: str | Path) -> Path:
    p = Path(path or "")
    if not str(p):
        raise FileNotFoundError("题库路径是空的")
    return p if p.is_absolute() else (ROOT / p)


def parse_item(item: dict[str, Any], lineno: int,
               rules: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """把一条题目 dict 规范化成页面内部用的 case。

    jsonl 读盘、粘贴 JSONL、CSV 导入三条路都走这里——否则「多轮怎么表示」
    「expect.keys 还算不算数」会在某一条路上悄悄解释不一样。
    """
    if not isinstance(item, dict):
        raise ValueError(f"第 {lineno} 条不是对象（应为 {{...}}）")
    turns = item.get("turns") or ([item["question"]] if item.get("question") else [])
    if isinstance(turns, str):
        turns = [turns]
    turns = [str(t).strip() for t in turns if str(t).strip()]
    if not turns:
        raise ValueError(f"第 {lineno} 行没有 turns/question")
    case = {
        "id": str(item.get("id") or f"c{lineno:02d}"),
        "tag": str(item.get("tag") or ""),
        "turns": turns,
        "note": str(item.get("note") or ""),
        "must_any": [str(w) for w in (item.get("must_any") or []) if str(w).strip()],
        "must_not": [str(w) for w in (item.get("must_not") or []) if str(w).strip()],
        "reference": str(item.get("reference") or ""),
    }
    # 旧题库用 expect.keys 表达"答案里应该出现这些词"，等价于 must_any
    expect = item.get("expect")
    if isinstance(expect, dict) and expect.get("keys"):
        case["must_any"] = [str(w) for w in expect["keys"]]
    case["checks"] = profiles.build_checks(case, rules)
    return case


def load_cases(path: str | Path, rules: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """读 jsonl 题库。

    一行一题，最少只要一个字段：

        {"id": "q01", "question": "..."}                     # 单轮
        {"id": "q02", "turns": ["...", "..."]}               # 多轮
        {"id": "q03", "question": "...", "must_any": ["北京"],
         "must_not": ["据我所知"], "tag": "事实", "note": "..."}

    `must_any` / `must_not` 是规则体检项（见 profiles.violations）；`rules` 是
    档案里按 tag 生效的规则，会合并到每道题上。
    """
    p = resolve_cases(path)
    if not p.exists():
        raise FileNotFoundError(f"题库不存在：{p}")
    cases: list[dict[str, Any]] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {lineno} 行不是合法 JSON：{exc}") from exc
        cases.append(parse_item(item, lineno, rules))
    if not cases:
        raise ValueError("题库是空的")
    return cases


def cases_summary(cases: list[dict[str, Any]]) -> str:
    """载入后回一句人话：多少题、哪些题带体检、多轮占几条。"""
    n_rules = sum(1 for c in cases if c.get("checks"))
    multi = sum(1 for c in cases if len(c["turns"]) > 1)
    parts = [f"{len(cases)} 条题"]
    if multi:
        parts.append(f"{multi} 条多轮")
    if n_rules:
        parts.append(f"{n_rules} 条带规则体检")
    return " · ".join(parts)


# ---------------------------------------------------------------- 小工具

def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard。中文没有空格分词，bigram 是最省事也够用的口径。

    用途只有一个：**判断这 K 条回答是不是真的不一样**。温度调了但模型每次
    都吐同一句话，DPO 对子里 chosen 和 rejected 几乎相同，训出来的偏好是噪音。
    """
    def grams(s: str) -> set[str]:
        s = "".join((s or "").split())
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))} or ({s} if s else set())

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)
