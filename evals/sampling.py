#!/usr/bin/env python
"""单模型多答打分台：一个模型、同一个输入、K 次采样，逐条打分。

**它不预设任何场景。** 评预问诊、评写作、评代码、评客服话术，区别只在于挂哪份
「评测档案」（`profiles.py`）：档案决定提示词、评分维度、对话里的角色名、默认
题库、规则体检词。预问诊只是内置的两份档案之一。

和 A/B 盲测（arena.py）的分工：

| | A/B 双模型盲测 | 单模型多答（本页） |
|---|---|---|
| 回答的问题 | 换了模型有没有变好 | 这个模型稳不稳、好在哪、坏在哪 |
| 一次产出 | 两个候选 + 一个胜负 | K 条样本 + K 组分数 |
| 主要产物 | 胜率表、CSV | **DPO 偏好对** + 模型评测报告 |

DPO 对是白捡的：同一道题里分最高的那条当 `chosen`、最低的当 `rejected`，
不需要再找人重写一遍「更好的回答应该是什么样」。样本之间如果一模一样
（温度没生效、模型每次都吐同一句），这一对就是噪音——所以报告里会给出
**同题相似度**，低于阈值时明确提示"这批对子别拿去训"。

模型评测要看的不是平均分（平均分会把"一次很好一次很糟"和"两次都还行"
抹成同一个数），而是：

- 每维度均值 / 标准差 / 高分占比；
- 综合及格率、关键维度（档案指定的 `safety_dim`）合格率；
- **同题极差**——同一条输入 K 次采样里最高分和最低分的差，衡量稳定性；
- **同题相似度**——样本雷同说明采样没起作用，DPO 对子退化；
- **规则体检**——题库/档案声明"必须出现 X、不许出现 Y"，统计违规条数；
- 按题组（tag）分组的表现。

用法：
    evals/.venv/Scripts/python.exe evals/scoreboard.py
    # 打开后切到「单模型多答」页
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

import backends
import casebook
import profiles
from common import (
    RUNS,
    Endpoint,
    cases_summary,
    dialog_text,
    run_dialog,
    similarity,
)

SESSION = RUNS / "_session_sample.json"
MAX_SAMPLES = 6

# 打开页面时先从主项目的后端配置里填一套出来。算在模块级而不是 build() 里，
# 是因为 Gradio 重新构建界面时不该再探一次端口（本地探测有 0.25s 超时）。
_BD_VAL = backends.default_value()
_BD = backends.fill(_BD_VAL) or {"label": "", "base_url": "", "model": "", "api_key": ""}
_BD_NOTE = backends.summary()

VERDICTS = [
    ("有明确优劣（可作 DPO 样本）", "pair"),
    ("几条差不多（不作偏好样本）", "tie"),
    ("都不可用（不能拿来训）", "bad"),
]


# ── 档案 ────────────────────────────────────────────────────────────────────

def _profile(state: dict[str, Any] | None) -> dict[str, Any]:
    prof = (state or {}).get("profile")
    if isinstance(prof, dict) and prof.get("dimensions"):
        return prof
    # 老会话 / 空状态：退回内置预问诊档案，别在恢复时炸掉
    return profiles.get("previsit")


def _dims(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _profile(state)["dimensions"]


def _roles(state: dict[str, Any] | None) -> tuple[str, str]:
    prof = _profile(state)
    return (prof.get("user_role") or "用户", prof.get("ai_role") or "助手")


def _full(dims: list[dict[str, Any]]) -> int:
    return len(dims) * profiles.MAX_SCORE


def _pass_line(dims: list[dict[str, Any]]) -> int:
    return math.ceil(_full(dims) * 0.8)


# ── 状态 ────────────────────────────────────────────────────────────────────

def new_state(cases: list[dict[str, Any]], run_file: Path, ep: Endpoint,
              profile: dict[str, Any], k: int) -> dict[str, Any]:
    return {
        "cases": cases,
        "idx": 0,
        "k": int(k),
        "gen": {},                      # str(idx) -> [run_dialog, ...] 长度 k
        "results": [],
        "run_file": str(run_file),
        "profile": profile,
        "system": profile.get("system", ""),
        "label": ep.label,
        "ep": ep.as_dict(),
    }


def _ep(state: dict[str, Any]) -> Endpoint:
    return Endpoint(**state["ep"])


def save_session(state: dict[str, Any]) -> None:
    SESSION.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def load_session() -> dict[str, Any] | None:
    if not SESSION.exists():
        return None
    try:
        state = json.loads(SESSION.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(state, dict) and not state.get("profile"):
        state["profile"] = profiles.get("previsit")   # 老会话补档案
    return state


def append_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    with open(state["run_file"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 生成 ────────────────────────────────────────────────────────────────────

def run_samples(ep: Endpoint, case: dict[str, Any], system: str, k: int) -> list[dict[str, Any]]:
    """同一条输入跑 K 次。每次都是完整对话，不是只重答最后一句。

    只重答最后一句会让前几轮的历史完全相同，K 条样本从第二轮起就越走越像——
    而多轮场景恰恰是"上一句问得不一样，下一句就完全不同"。
    """
    return [run_dialog(ep, case["turns"], system) for _ in range(k)]


# ── 统计与产物 ──────────────────────────────────────────────────────────────

def _sample_rows(state: dict[str, Any]):
    """把 results 摊平成 (record, 样本号, 样本) 的流。"""
    for rec in state.get("results") or []:
        for i, s in enumerate(rec.get("samples") or []):
            yield rec, i, s


def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[float]) -> float:
    return statistics.pstdev(vals) if len(vals) > 1 else 0.0


def _total(sample: dict[str, Any], dims: list[dict[str, Any]]) -> int:
    return sum(int(sample["score"].get(d["key"]) or 0) for d in dims)


def _diversity(rec: dict[str, Any]) -> float:
    """同题样本两两相似度均值。1 = 完全一样，0 = 毫无重叠。"""
    texts = [s["text"] for s in rec.get("samples") or [] if not s.get("error")]
    if len(texts) < 2:
        return 0.0
    pairs = list(itertools.combinations(texts, 2))
    return _mean([similarity(a, b) for a, b in pairs])


def _spread(rec: dict[str, Any], dims: list[dict[str, Any]]) -> int:
    totals = [_total(s, dims) for s in rec.get("samples") or [] if not s.get("error")]
    return max(totals) - min(totals) if totals else 0


def _tag_group(tag: str) -> str:
    return (tag or "").split("·")[0] or "未分组"


def _rule_hits(rec: dict[str, Any]) -> list[str]:
    """这条记录里被判违规的检查项名（同一项只算一次）。"""
    checks = rec.get("checks") or []
    if not checks:
        return []
    fake_case = {"checks": checks}
    hits: list[str] = []
    for s in rec.get("samples") or []:
        for name in profiles.violations(fake_case, s.get("text") or ""):
            if name not in hits:
                hits.append(name)
    return hits


def stats_markdown(state: dict[str, Any] | None) -> str:
    dims = _dims(state)
    prof = _profile(state)
    full, pass_line = _full(dims), _pass_line(dims)
    rows = (state or {}).get("results") or []
    total_cases = len((state or {}).get("cases") or [])
    if not rows:
        return (f"### 模型评测　·　{prof['label']}\n已评 **0** / {total_cases} 条。"
                "先点「⚡ 全部生成」，再逐条给 K 个样本打分。")

    samples = [s for _, _, s in _sample_rows(state)]
    n = len(samples)
    lines = [
        f"### 模型评测　**{(state or {}).get('label', '模型')}**　·　{prof['label']}",
        f"已评 **{len(rows)}** / {total_cases} 条题 · **{n}** 条样本",
        "",
        "| 维度 | 均值 | 标准差 | 高分占比（≥4） |",
        "|---|---|---|---|",
    ]
    for d in dims:
        vals = [s["score"][d["key"]] for s in samples if s["score"].get(d["key"]) is not None]
        good = sum(1 for v in vals if v >= 4) / len(vals) if vals else 0.0
        lines.append(f"| {d['label']} | {_mean(vals):.2f} | {_std(vals):.2f} | {good:.0%} |")

    totals = [_total(s, dims) for s in samples]
    pass_rate = sum(1 for t in totals if t >= pass_line) / len(totals) if totals else 0.0
    key_dim = prof.get("safety_dim") or ""
    summary = (f"**综合**：均分 {_mean(totals):.2f} / {full}　·　"
               f"及格率（≥{pass_line}）{pass_rate:.0%}")
    if key_dim and any(d["key"] == key_dim for d in dims):
        kv = [s["score"][key_dim] for s in samples if s["score"].get(key_dim) is not None]
        k_rate = sum(1 for v in kv if v >= 4) / len(kv) if kv else 0.0
        label = next(d["label"] for d in dims if d["key"] == key_dim)
        summary += f"　·　{label}合格率（≥4）{k_rate:.0%}"
    lines += ["", summary]

    # 稳定性与多样性：这两个数才是"评价模型"和"挑模型"的分水岭。
    # 每项前面必须留空行：Markdown 会把连续的普通行合并成同一个段落，
    # 四五条统计糊成一整行，右边的字直接被容器裁掉。
    spreads = [_spread(r, dims) for r in rows]
    divs = [_diversity(r) for r in rows]
    dup = sum(1 for d in divs if d >= 0.95)
    lines += ["", f"**稳定性**：同题极差均值 {_mean(spreads):.1f} 分"
                  f"（最大 {max(spreads) if spreads else 0}）"
                  "　*同一条题 K 次采样里最高分与最低分之差*"]
    lines += ["", f"**多样性**：同题相似度均值 {_mean(divs):.2f}"
                  "　*≥0.95 说明采样退化成同一句，这批 DPO 对子是噪音*"
            + (f"　⚠️ {dup} 条题样本几乎一样，DPO 对子别用" if dup else "")]

    pairs = dpo_records(state)
    gaps = [p["meta"]["gap"] for p in pairs]
    skipped = sum(1 for r in rows if r.get("dpo")) - len(pairs)
    line = f"**DPO 素材**：{len(pairs)} 条可用对子"
    if gaps:
        line += f"，chosen−rejected 平均差 {_mean(gaps):.1f} 分"
    if skipped:
        line += f"（另有 {skipped} 条因 chosen 与 rejected 一字不差被剔除）"
    lines += ["", line]

    # 规则体检：不是"安全项"的同义词，而是题库自己声明的硬性约束。
    checked = [r for r in rows if r.get("checks")]
    if checked:
        bad = [r for r in checked if _rule_hits(r)]
        detail: dict[str, int] = {}
        for r in bad:
            for name in _rule_hits(r):
                detail[name] = detail.get(name, 0) + 1
        txt = f"**规则体检**：{len(checked)} 条题带检查项，**{len(bad)}** 条有违规"
        if detail:
            txt += "（" + "、".join(f"{k} {v}" for k, v in detail.items()) + "）"
        txt += "　*规则判断，仅作提示*"
        lines += ["", txt]

    # 按题组拆开看：模型在常见输入上可能很好，在越界诱导上稀烂，平均分看不出来。
    groups: dict[str, list[int]] = {}
    for rec in rows:
        groups.setdefault(_tag_group(rec.get("tag") or ""), []).extend(
            [_total(s, dims) for s in rec.get("samples") or []])
    if len(groups) > 1:
        lines += ["", "| 题组 | 样本数 | 均分 | 最低 |", "|---|---|---|---|"]
        for name, vals in sorted(groups.items()):
            lines.append(f"| {name} | {len(vals)} | {_mean(vals):.2f} | {min(vals)} |")

    ms = [s["ms"] for s in samples if s.get("ms")]
    errs = sum(1 for s in samples if s.get("error"))
    if ms:
        lines += ["", f"**耗时**：单条均值 {_mean(ms) / 1000:.1f}s　·　"
                      f"失败 {errs} 条"]
    if total_cases and len(rows) < total_cases:
        lines += ["", f"还剩 {total_cases - len(rows)} 条题没评。"]
    return "\n".join(lines)


def write_csv(state: dict[str, Any]) -> str | None:
    rows = state.get("results") or []
    if not rows:
        return None
    dims = _dims(state)
    path = RUNS / "sample_latest.csv"
    header = ["case_id", "tag", "sample", "judge", "dpo_role"]
    header += [d["key"] for d in dims] + ["total", "rule_violation", "ms", "error", "text"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for rec in rows:
            best, worst = _pick_pair(rec, dims)
            ok = _exportable(rec, dims)
            hits = "、".join(_rule_hits(rec))
            for i, s in enumerate(rec.get("samples") or []):
                role = "chosen" if i == best else ("rejected" if i == worst else "")
                if not ok:
                    role = ""
                writer.writerow(
                    [rec["case_id"], rec["tag"], i + 1, rec.get("verdict", ""), role]
                    + [s["score"].get(d["key"]) for d in dims]
                    + [_total(s, dims), hits, s["ms"], s.get("error", ""), s["text"]]
                )
    return str(path)


def prompt_messages(rec: dict[str, Any]) -> list[dict[str, str]]:
    """还原模型看到的历史（去掉最后一条 assistant，它就是被比较的那句）。"""
    msgs: list[dict[str, str]] = []
    system = (rec.get("system") or "").strip()
    if system:
        msgs.append({"role": "system", "content": system})
    best = rec["samples"][rec["best"]]
    for i, turn in enumerate(rec["turns"]):
        msgs.append({"role": "user", "content": turn})
        if i < len(best.get("replies") or []):
            msgs.append({"role": "assistant", "content": best["replies"][i]["text"]})
    if msgs and msgs[-1]["role"] == "assistant":
        msgs = msgs[:-1]
    return msgs


def _pick_pair(rec: dict[str, Any], dims: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """按总分挑 chosen / rejected；并列第一或并列最后都返回 (None, None)。

    并列就是没有偏好——宁可丢一条对子，也不要造一条"两个一样好"的偏好样本，
    那等于教模型在无差别的位置上用力。
    """
    samples = rec.get("samples") or []
    if len(samples) < 2:
        return None, None
    totals = [_total(s, dims) for s in samples]
    hi, lo = max(totals), min(totals)
    if totals.count(hi) > 1 or totals.count(lo) > 1:
        return None, None
    return totals.index(hi), totals.index(lo)


def _exportable(rec: dict[str, Any], dims: list[dict[str, Any]]) -> bool:
    """这条记录能不能产出一条真偏好对：结论是 pair、分数不并列、文本不雷同。

    一字不差的一对没有梯度（DPO 的 loss 恒等于 ln2），写进 jsonl 只会让训练
    日志好看，对权重没有任何影响——这种对子必须在导出时就剔除。
    """
    if not rec.get("dpo"):
        return False
    b, w = rec.get("best"), rec.get("worst")
    samples = rec.get("samples") or []
    if b is None or w is None or b >= len(samples) or w >= len(samples):
        return False
    return samples[b]["text"].strip() != samples[w]["text"].strip()


def dpo_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    dims = _dims(state)
    prof = _profile(state)
    out: list[dict[str, Any]] = []
    for rec in state.get("results") or []:
        if not _exportable(rec, dims):
            continue
        best, worst = rec["best"], rec["worst"]
        samples = rec["samples"]
        out.append({
            "prompt": prompt_messages(rec),
            "chosen": samples[best]["text"],
            "rejected": samples[worst]["text"],
            "meta": {
                "case_id": rec["case_id"],
                "tag": rec["tag"],
                "task": prof.get("key", ""),
                "model": state.get("label", ""),
                "temperature": (state.get("ep") or {}).get("temperature"),
                "chosen_score": rec["chosen_total"],
                "rejected_score": rec["rejected_total"],
                "gap": rec["gap"],
                "similarity": round(_diversity(rec), 3),
                "ts": rec.get("ts"),
            },
        })
    return out


def write_dpo(state: dict[str, Any]) -> str | None:
    records = dpo_records(state)
    if not records:
        return None
    stamp = datetime.now().strftime("%m%d-%H%M")
    path = RUNS / f"dpo_{stamp}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(path)


def write_report(state: dict[str, Any]) -> str | None:
    rows = state.get("results") or []
    if not rows:
        return None
    dims = _dims(state)
    prof = _profile(state)
    full, pass_line = _full(dims), _pass_line(dims)
    stamp = datetime.now().strftime("%m%d-%H%M")
    path = RUNS / f"eval_{stamp}.md"
    ep = state.get("ep") or {}
    head = [
        f"# 模型评测 · {state.get('label', '模型')}",
        "",
        f"- 任务档案：**{prof['label']}**（`{prof.get('key', '')}`）",
        f"- 端点：`{ep.get('base_url', '')}`　模型：`{ep.get('model', '')}`",
        f"- 采样：每题 {state.get('k')} 条，temperature={ep.get('temperature')}，"
        f"top_p={ep.get('top_p')}",
        f"- 题数：{len(rows)} / {len(state.get('cases') or [])}，"
        f"生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 评分口径：{len(dims)} 个维度（"
        + "、".join(d["label"] for d in dims)
        + f"）各 {profiles.MIN_SCORE}–{profiles.MAX_SCORE} 分，满分 {full}，"
        f"≥{pass_line} 算及格",
        "",
        stats_markdown(state),
        "",
        "## 逐题明细",
        "",
        "| 题号 | 题组 | 均分 | 极差 | 相似度 | 规则违规 | 是否作 DPO 对 |",
        "|---|---|---|---|---|---|---|",
    ]
    for rec in rows:
        totals = [_total(s, dims) for s in rec.get("samples") or []]
        hits = "、".join(_rule_hits(rec)) or "—"
        head.append(
            f"| {rec['case_id']} | {rec['tag']} | {_mean(totals):.1f} | "
            f"{_spread(rec, dims)} | {_diversity(rec):.2f} | {hits} | "
            f"{'是' if _exportable(rec, dims) else '否'} |")
    head += ["", "> 分数由人工打分产生；规则体检按档案/题库声明的关键词判断，"
                 "只作提示，不能替代人工复核。"]
    path.write_text("\n".join(head), encoding="utf-8")
    return str(path)


# ── 渲染 ────────────────────────────────────────────────────────────────────

ORDER = ["state", "progress", "case_head", "note", "verdict", "comment",
         "stats", "runfile", "case_list"]
for _i in range(MAX_SAMPLES):
    ORDER += [f"g{_i}", f"t{_i}"] + [f"s{_i}_{_j}" for _j in range(profiles.MAX_DIMS)]


def _pack(d: dict[str, Any]) -> list[Any]:
    missing = [k for k in ORDER if k not in d]
    if missing:
        raise KeyError(f"渲染缺字段：{missing}")
    return [d[k] for k in ORDER]


def case_choices(state: dict[str, Any]) -> list[tuple[str, int]]:
    done = {r["case_id"] for r in (state.get("results") or [])}
    out: list[tuple[str, int]] = []
    for i, case in enumerate(state.get("cases") or []):
        mark = "✓" if case["id"] in done else "　"
        tag = f" {case['tag']}" if case["tag"] else ""
        out.append((f"{mark} {i + 1:02d}　{case['id']}{tag}", i))
    return out


def _case_update(state: dict[str, Any], idx: int) -> Any:
    return gr.update(choices=case_choices(state), value=idx if state.get("cases") else None)


def _slots(dims: list[dict[str, Any]], k: int,
           priors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """K 个样本 × 维度槽位的显隐、标签与取值。

    槽位建满 MAX_DIMS 个再按需显隐：Gradio 里动态增删组件会把事件绑定一起
    弄丢，所以"维度可变"只能靠"建满再藏"。标签同时更新——否则换了档案，
    滑块上还写着上一份档案的维度名。
    """
    out: dict[str, Any] = {}
    n = len(dims)
    for i in range(MAX_SAMPLES):
        out[f"g{i}"] = gr.update(visible=i < k)
        for j in range(profiles.MAX_DIMS):
            upd: dict[str, Any] = {
                "visible": i < k and j < n,
                "label": (f"{i + 1} · {profiles.dim_label(dims[j])}" if j < n
                          else f"{i + 1} · —"),
            }
            if j < n:
                if priors is not None and i < len(priors):
                    upd["value"] = int(priors[i].get(dims[j]["key"], 3) or 3)
                else:
                    upd["value"] = 3
            out[f"s{i}_{j}"] = gr.update(**upd)
    return out


def _blank(dims: list[dict[str, Any]], k: int = 2) -> dict[str, Any]:
    out: dict[str, Any] = {"verdict": "pair", "comment": ""}
    out.update(_slots(dims, k))
    for i in range(MAX_SAMPLES):
        out[f"t{i}"] = ""
    return out


def render(state: dict[str, Any] | None) -> list[Any]:
    dims = _dims(state)
    u_role, a_role = _roles(state)

    if not state or not state.get("cases"):
        return _pack({"state": state, "progress": "**已评 0 条**",
                      "case_head": "### 等待载入题库", "note": "",
                      "stats": stats_markdown(None),
                      "runfile": "", "case_list": gr.update(choices=[], value=None),
                      **_blank(dims)})

    idx, total = state["idx"], len(state["cases"])
    k = min(int(state.get("k") or 2), MAX_SAMPLES)
    base = {"state": state, "progress": f"**已评 {len(state['results'])} 条**",
            "runfile": state["run_file"], "case_list": _case_update(state, idx),
            "stats": stats_markdown(state), **_blank(dims, k)}

    if idx >= total:
        base.update({"case_head": "### ✅ 全部评完", "note": ""})
        return _pack(base)

    case = state["cases"][idx]
    gen = (state.get("gen") or {}).get(str(idx)) or []
    tag = f"　`{case['tag']}`" if case["tag"] else ""
    head = f"### 第 {idx + 1} / {total} 题　`{case['id']}`{tag}"
    if gen:
        head += f"　·　{len(gen)} 条样本 · 耗时 {sum(g['ms'] for g in gen) / 1000:.1f}s"

    note = f"💡 {case['note']}" if case["note"] else ""
    if case.get("checks"):
        bits = []
        for chk in case["checks"]:
            if chk["must_any"]:
                bits.append(f"须含 {chk['must_any'][0]} 等")
            if chk["must_not"]:
                bits.append(f"不得含 {chk['must_not'][0]} 等")
        note += ("\n\n" if note else "") + "🔍 规则体检：" + "；".join(bits)

    base.update({"case_head": head, "note": note})

    # 先按未评状态铺一遍
    for i in range(MAX_SAMPLES):
        if i >= k:
            continue
        base[f"t{i}"] = (dialog_text(case["turns"], gen[i]["replies"], u_role, a_role)
                        if i < len(gen) else "（尚未生成，先点「⚡ 全部生成」）")

    # 已评过的题：把当初的分数读回来，方便改判（和 A/B 页同样的做法）
    prior = next((r for r in state["results"] if r["case_id"] == case["id"]), None)
    if prior:
        base["verdict"] = prior.get("verdict", "pair")
        base["comment"] = prior.get("comment", "")
        samples = prior.get("samples") or []
        base.update(_slots(dims, min(k, max(len(samples), 1)),
                           [s.get("score") or {} for s in samples]))
        for i, s in enumerate(samples):
            if i >= MAX_SAMPLES:
                break
            base[f"t{i}"] = s.get("text", "")
    return _pack(base)


# ── 动作 ────────────────────────────────────────────────────────────────────

def _profile_from(dims_text: str, system: str, u_role: str, a_role: str,
                  key: str = "") -> dict[str, Any]:
    dims = profiles.parse_dimensions(dims_text)
    prof = profiles.get(key or "general")
    prof = dict(prof)
    prof["dimensions"] = dims
    prof["system"] = system or ""
    prof["user_role"] = (u_role or "").strip() or "用户"
    prof["ai_role"] = (a_role or "").strip() or "助手"
    return prof


def _backend_hint(got: dict[str, Any] | None) -> str:
    if not got:
        return ("选中的后端在 `config/` 里找不到了（可能刚被删掉或改名）："
                "点「重读配置」刷新列表，或直接在下面手填。")
    if got["key_present"]:
        tail = "密钥已一并填入"
    elif got["local"]:
        tail = "没有密钥（本地后端一般不需要）"
    else:
        tail = ("没有读到密钥：去「模型服务」页给它填一次，之后这里会自动带上；"
                "也可以展开「高级」手填")
    return f"已从 **{got['backend']}** 填入地址与模型 · {tail}"


def do_pick_backend(value):
    """选后端 → 把地址 / 模型 / 密钥填进下面的框（仍然可以手改）。"""
    got = backends.fill(value)
    if not got:
        return gr.update(), gr.update(), gr.update(), gr.update(), _backend_hint(None)
    return (gr.update(value=got["label"]), gr.update(value=got["base_url"]),
            gr.update(value=got["model"]), gr.update(value=got["api_key"]),
            _backend_hint(got))


def do_reload_backends(value):
    """重读 config 与密钥文件。刚在后台填过密钥、刚加过一个后端时用。"""
    backends.refresh()
    ch = backends.choices()
    val = value if value in {v for _, v in ch} else backends.default_value()
    got = backends.fill(val)
    got = got or {"label": "", "base_url": "", "model": "", "api_key": ""}
    return (gr.update(choices=ch, value=val), backends.summary(),
            gr.update(value=got["label"]), gr.update(value=got["base_url"]),
            gr.update(value=got["model"]), gr.update(value=got["api_key"]))


def do_copy_cases(path, profile_key):
    """把当前题库转成手输格式，抄进输入框改两行——比让人从零手打靠谱。"""
    try:
        rules = profiles.get(profile_key or "previsit").get("rules") or []
        return casebook.text_from_path(path, rules)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc


def do_start(profile_key, source, cases_path, upload, cases_text,
             sys_prompt, dims_text, u_role, a_role,
             label, url, model, key, temp, top_p, max_tokens, k):
    try:
        prof = _profile_from(dims_text, sys_prompt, u_role, a_role, profile_key)
        source = casebook.normalize_source(source)
        cases = casebook.resolve(source, cases_path, upload, cases_text,
                                 prof.get("rules") or [])
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    stamp = datetime.now().strftime("%m%d-%H%M")
    safe_label = (label or prof.get("key") or "model").replace(" ", "-")
    run_file = RUNS / f"{stamp}_sample_{safe_label}.jsonl"
    ep = Endpoint(label or prof.get("key") or "model", url, model, key,
                  float(temp), float(top_p), int(max_tokens))
    state = new_state(cases, run_file, ep, prof, int(k))
    save_session(state)
    gr.Info(f"档案「{prof['label']}」· 来源「{source}」· {cases_summary(cases)}，"
            f"每题采样 {int(k)} 次")
    return render(state)


def do_load_profile(key):
    """切档案：提示词 / 维度 / 角色 / 题库 / 滑块标签一起换。

    只换提示词不换维度，是这套东西最容易犯的错——用预问诊的四个维度去评写作，
    分是有的，但没有人知道那个分在说什么。滑块标签尤其不能漏：界面上写着
    "漏红旗"，人就会照着"漏红旗"打分。样本区一并清空，免得旧对话留在新维度下面。
    """
    prof = profiles.get(key or "previsit")
    slots = _slots(prof["dimensions"], 2)
    desc = (f"**{prof['label']}**　{prof.get('desc') or ''}\n\n"
            f"维度：{'、'.join(d['label'] for d in prof['dimensions'])}"
            + (f"　·　安全维度：`{prof['safety_dim']}`" if prof.get("safety_dim") else "")
            + (f"　·　规则 {len(prof['rules'])} 条" if prof.get("rules") else ""))
    out: tuple[Any, ...] = (
        gr.update(value=prof["system"]),
        gr.update(value=profiles.format_dimensions(prof["dimensions"])),
        gr.update(value=prof.get("user_role") or "用户"),
        gr.update(value=prof.get("ai_role") or "助手"),
        gr.update(value=prof.get("cases") or ""),
        gr.update(value=prof.get("temperature", 0.9)),
        gr.update(value=prof.get("max_tokens", 512)),
        desc,
    )
    for i in range(MAX_SAMPLES):
        out += (slots[f"g{i}"], gr.update(value=""))
        out += tuple(slots[f"s{i}_{j}"] for j in range(profiles.MAX_DIMS))
    return out


def do_save_profile(profile_key, save_key, sys_prompt, dims_text, u_role, a_role,
                    source, cases_path, upload, cases_text, temp, max_tokens):
    try:
        dims = profiles.parse_dimensions(dims_text)
        prof = profiles.get(profile_key or "general")
        key = (save_key or "").strip() or profile_key or "custom"
        # 题库不是档案自带的那份时，把它存成档案自己的 jsonl：否则下次切回来，
        # 档案里那句 cases 还指着旧文件，界面上一切正常，评的却是另一批题。
        source = casebook.normalize_source(source)
        if source != "档案自带":
            try:
                cases = casebook.parse_source(source, cases_path, upload, cases_text,
                                              prof.get("rules") or [])
                cases_path = str(casebook.save_named(cases, key))
            except Exception:
                pass  # 题库还没准备好不该挡住存提示词
        payload = {
            "key": key,
            # 覆盖当前档案就沿用原名；起了新名字才标「自定义」
            "label": prof["label"] if not save_key else f"{prof['label']}（自定义）",
            "desc": prof.get("desc", ""),
            "system": sys_prompt,
            "user_role": u_role,
            "ai_role": a_role,
            "dimensions": dims,
            "cases": cases_path,
            "rules": prof.get("rules") or [],
            "safety_dim": prof.get("safety_dim") or "",
            "temperature": float(temp),
            "max_tokens": int(max_tokens),
        }
        path = profiles.save(payload)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    gr.Info(f"已保存档案：{path}（左侧下拉会多出这一项，刷新页面即可看到）")
    return gr.update(value=Path(path).stem)


def do_generate(state, only_current: bool, progress=gr.Progress()):
    if not state or not state.get("cases"):
        raise gr.Error("先点「开始」载入题库")
    ep = _ep(state)
    system = state.get("system", "")
    k = min(int(state.get("k") or 2), MAX_SAMPLES)
    targets = [state["idx"]] if only_current else list(range(len(state["cases"])))
    todo = [i for i in targets if i < len(state["cases"])]
    if not todo:
        return render(state)

    done = 0
    total = len(todo) * k
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(run_samples, ep, state["cases"][i], system, k): i
            for i in todo
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                state.setdefault("gen", {})[str(i)] = fut.result()
            except Exception as exc:
                state.setdefault("gen", {})[str(i)] = [
                    {"turns": state["cases"][i]["turns"], "replies": [], "ms": 0,
                     "error": f"{type(exc).__name__}: {exc}"} for _ in range(k)]
            done += k
            progress(done / total, desc=f"生成中 {done}/{total} 条样本")

    save_session(state)
    errors = sum(1 for v in state["gen"].values() for r in v if r.get("error"))
    if errors:
        gr.Warning(f"{errors} 条样本生成失败，页面上会显示 ⚠️")
    return render(state)


def do_submit(state, *rest):
    """rest = [t0..t5, s0_0..s5_5, verdict, comment]。"""
    if not state or state["idx"] >= len(state["cases"]):
        return render(state)
    dims = _dims(state)
    idx = state["idx"]
    case = state["cases"][idx]
    gen = (state.get("gen") or {}).get(str(idx)) or []
    if not gen:
        gr.Warning("这一题还没生成，先点「⚡ 全部生成」再打分")
        return render(state)

    texts = list(rest[:MAX_SAMPLES])
    flat = list(rest[MAX_SAMPLES:MAX_SAMPLES + MAX_SAMPLES * profiles.MAX_DIMS])
    verdict, comment = rest[-2], rest[-1]
    k = min(int(state.get("k") or 2), len(gen), MAX_SAMPLES)
    u_role, a_role = _roles(state)

    samples = []
    for i in range(k):
        score = {}
        for j, d in enumerate(dims):
            pos = i * profiles.MAX_DIMS + j
            score[d["key"]] = int(flat[pos] if pos < len(flat) else 3)
        raw = dialog_text(case["turns"], gen[i]["replies"], u_role, a_role)
        samples.append({
            "text": texts[i] or raw,
            "raw": raw,
            "edited": (texts[i] or "").strip() != raw.strip(),
            "replies": gen[i]["replies"],
            "score": score,
            "ms": gen[i]["ms"],
            "error": gen[i].get("error", ""),
        })

    totals = [_total(s, dims) for s in samples]
    best = totals.index(max(totals)) if totals else None
    worst = totals.index(min(totals)) if totals else None
    tie = (max(totals) == min(totals)) if totals else True
    pair = (verdict == "pair") and not tie and best is not None

    record = {
        "case_id": case["id"],
        "tag": case["tag"],
        "turns": case["turns"],
        "system": state.get("system", ""),
        "task": (state.get("profile") or {}).get("key", ""),
        "checks": case.get("checks") or [],
        "samples": samples,
        "verdict": verdict,
        "best": best,
        "worst": worst,
        "chosen_total": totals[best] if best is not None else 0,
        "rejected_total": totals[worst] if worst is not None else 0,
        "gap": (totals[best] - totals[worst]) if pair else 0,
        "dpo": bool(pair),
        "comment": comment or "",
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        append_record(state, record)
    except Exception as exc:
        raise gr.Error(f"写文件失败：{exc}") from exc
    state["results"] = [r for r in state["results"] if r["case_id"] != case["id"]] + [record]
    state["idx"] = idx + 1
    save_session(state)
    return render(state)


def do_skip(state):
    if not state or state["idx"] >= len(state["cases"]):
        return render(state)
    state["idx"] = min(state["idx"] + 1, len(state["cases"]))
    save_session(state)
    return render(state)


def do_back(state):
    if not state or state["idx"] == 0:
        return render(state)
    state["idx"] -= 1
    save_session(state)
    return render(state)


def do_goto(state, number):
    if not state or not state.get("cases"):
        raise gr.Error("还没有载入题库")
    total = len(state["cases"])
    state["idx"] = max(0, min(int(number or 1) - 1, total - 1))
    save_session(state)
    return render(state)


def do_pick(state, idx):
    if state is None or idx is None:
        return render(None)
    return do_goto(state, int(idx) + 1)


def do_resume():
    state = load_session()
    if state is None:
        raise gr.Error("没有可恢复的会话")
    gr.Info(f"已恢复：{_profile(state)['label']} · 第 {state['idx'] + 1} 题，"
            f"已评 {len(state['results'])} 条")
    return render(state)


def do_autoload():
    return render(load_session())


def do_export_csv(state):
    path = write_csv(state or {})
    if not path:
        raise gr.Error("还没有打分记录")
    return gr.update(value=path, visible=True), gr.update(visible=True)


def do_export_dpo(state):
    records = dpo_records(state or {})
    if not records:
        raise gr.Error("还没有可用的 DPO 对子（需要「有明确优劣」、分数不并列，"
                       "且 chosen 和 rejected 不能一字不差）")
    path = write_dpo(state or {})
    gr.Info(f"导出 {len(records)} 条偏好对。建议先用 meta.similarity 过滤掉"
            "≥0.95 的近重复对，再拿去做训练。")
    return gr.update(value=path, visible=True), gr.update(visible=True)


def do_export_report(state):
    path = write_report(state or {})
    if not path:
        raise gr.Error("还没有打分记录")
    return gr.update(value=path, visible=True), gr.update(visible=True)


# ── 界面 ────────────────────────────────────────────────────────────────────

def build() -> gr.Blocks:
    first = profiles.get("previsit")
    with gr.Blocks(title="单模型多答打分台") as demo:
        state = gr.State(None)

        gr.Markdown("# 🧪 单模型多答 · 打分 / DPO / 评测")
        gr.Markdown(
            "**顺序**：选一份评测档案（决定提示词、评分维度）→ 定题库（档案自带 / "
            "导入文件 / 自己输入）→ 填端点 → 「开始」→ 「⚡ 全部生成」→ "
            "给每条样本打分 → 提交 → 导出。\n\n"
            "同一道题的 K 条样本里，分最高的当 **chosen**、最低的当 **rejected**，"
            "直接产出 DPO 对子；分数同时汇总成模型评测报告（均值/标准差/及格率/"
            "同题极差/样本相似度/规则体检）。"
        )

        with gr.Accordion("① 任务定义（评测档案 → 提示词 / 维度 / 角色 / 题库）", open=True):
            with gr.Row():
                profile_pick = gr.Dropdown(
                    choices=profiles.choices(), value="previsit",
                    label="评测档案（换档案会重置下面四项）", scale=3, min_width=240)
                save_key = gr.Textbox(value="", label="存为档案 key（空=覆盖当前）", scale=2)
                save_btn = gr.Button("💾 存为档案", scale=1)
            profile_desc = gr.Markdown(
                f"**{first['label']}**　{first['desc']}\n\n"
                f"维度：{'、'.join(d['label'] for d in first['dimensions'])}　·　"
                f"安全维度：`{first['safety_dim']}`　·　规则 {len(first['rules'])} 条")
            sys_prompt = gr.Textbox(value=first["system"], lines=3,
                                    label="system prompt")
            with gr.Row():
                dims_text = gr.Textbox(value=profiles.format_dimensions(first["dimensions"]),
                                       lines=5, label="评分维度（一行一个：键: 名称 | 提示）",
                                       scale=3)
                with gr.Column(scale=2, min_width=200):
                    u_role = gr.Textbox(value=first["user_role"], label="对话里「提问方」的角色名")
                    a_role = gr.Textbox(value=first["ai_role"], label="对话里「回答方」的角色名")
                    gr.Markdown("<span style='font-size:11px;opacity:.7'>"
                                "只影响展示与导出文本，不发给模型。</span>")
            # 三个来源都常驻，不靠服务端改 visible。
            #
            # 试过两版：Group 显隐（导入文件后再切来源，visible 更新会被上一轮
            # 渲染盖掉，点了没反应）、Tabs + select 写 State（内层页签的 select
            # 事件时灵时不灵，切了页签却还评旧题库）。两次都是"界面看着对、
            # 评的是另一批题"，这种错最贵。现在只有 Radio 决定用哪份，
            # 它是输入组件，值一定拿得到；三个 Accordion 只是摆放，折起来不影响。
            with gr.Row():
                with gr.Column(scale=4):
                    case_source = gr.Radio(
                        casebook.SOURCES, value="档案自带", label="题目来源", scale=1,
                        info="下面三份题库都在，开始前选一份——选哪个就用哪个")
                    with gr.Accordion("档案自带 · 用 evals/ 下的 jsonl", open=True):
                        cases_path = gr.Textbox(value=first["cases"],
                                                label="题库 jsonl（相对 evals/）")
                    with gr.Accordion("导入文件 · jsonl / json / csv / tsv / txt", open=False):
                        upload = gr.File(
                            label="拖进来或点击上传",
                            file_types=[".jsonl", ".ndjson", ".json", ".csv",
                                        ".tsv", ".txt", ".md"],
                            type="filepath")
                        gr.Markdown("<span style='font-size:11px;opacity:.72'>"
                                    "CSV 认「题目 / tag / must_any / note」这类表头；"
                                    "没有表头时第一列当题目、第二列当标签。"
                                    ".txt/.md 按「自己输入」那套语法解析。</span>")
                    with gr.Accordion("自己输入 · 一行一题", open=False):
                        cases_text = gr.Textbox(
                            value=casebook.load_custom_text(), lines=10,
                            label="题库（一行一题）",
                            info="「||」分轮；行尾可加 @必须:甲,乙 @不许:丙 "
                                 "@标签:事实 @备注:…；# 开头的整行给下面几题打标签",
                            placeholder="珠穆朗玛峰的海拔是多少米？ @必须:8848",
                            elem_classes="casebox")
                        with gr.Row(elem_classes="case-tools"):
                            tpl_btn = gr.Button("填入示例", size="sm")
                            copy_btn = gr.Button("← 抄档案题库", size="sm")
                            clear_btn = gr.Button("清空", size="sm")
                with gr.Column(scale=1, min_width=170):
                    start_btn = gr.Button("开始 / 新建一轮", variant="primary")
                    resume_btn = gr.Button("恢复上次")

        with gr.Accordion("② 模型端点（一个 OpenAI 兼容地址）", open=True):
            # 地址 / 模型 / 密钥在读「模型服务」页时就配过一遍了，这里不再让人
            # 抄一遍：抄错一个端口，评的就是另一个模型，而报告上看不出来。
            with gr.Row():
                backend_pick = gr.Dropdown(
                    choices=backends.choices(), value=_BD_VAL,
                    label="从已配置的后端填入（改完仍可手改）", scale=4, min_width=260)
                backend_reload = gr.Button("⟳ 重读配置", scale=1)
            backend_note = gr.Markdown(_BD_NOTE)
            with gr.Row():
                label = gr.Textbox(value=_BD["label"], label="模型名（写进结果文件）", scale=2)
                url = gr.Textbox(value=_BD["base_url"], label="base_url", scale=3)
                model = gr.Textbox(value=_BD["model"], label="model", scale=2)
            with gr.Row():
                n_samples = gr.Slider(2, MAX_SAMPLES, 3, step=1, label="每条采样几条")
                temp = gr.Slider(0, 1.5, 0.9, step=0.05,
                                 label="temperature（多答要比 A/B 高，否则样本雷同）")
                top_p = gr.Slider(0.1, 1.0, 0.95, step=0.05, label="top_p")
                max_tokens = gr.Number(value=512, label="max_tokens", precision=0)
            with gr.Accordion("高级", open=False):
                key = gr.Textbox(value=_BD["api_key"], label="api_key", type="password")

        with gr.Row():
            case_head = gr.Markdown("### 等待载入题库", elem_id="samphead", scale=4)
            progress_md = gr.Markdown("**已评 0 条**", scale=1)
        note = gr.Markdown("")

        # K 条样本：组件先按上限建满，渲染时按实际采样数与维度数显隐。
        #
        # 每个样本是「一条通栏回答 + 一排滑块」。试过「左边回答、右边滑块」
        # 的两栏：滑块比文本高，行被撑开后左栏下面会留一大块空白，看起来像
        # 文本域坏了。通栏铺开读起来也更像病历/回答全文。
        texts: list[Any] = []
        sliders: list[list[Any]] = []
        sample_groups: list[Any] = []
        for i in range(MAX_SAMPLES):
            with gr.Group(visible=i < 2) as g:
                gr.Markdown(f"#### 样本 {i + 1}")
                t = gr.Textbox(lines=6, show_label=False, interactive=True,
                               elem_classes="cand")
                with gr.Row():
                    sl = []
                    for j in range(profiles.MAX_DIMS):
                        sl.append(gr.Slider(profiles.MIN_SCORE, profiles.MAX_SCORE, 3,
                                            step=1, visible=j < len(first["dimensions"]),
                                            label=f"{i + 1} · —"))
            sample_groups.append(g)
            texts.append(t)
            sliders.append(sl)

        with gr.Row():
            verdict = gr.Radio(choices=VERDICTS, value="pair", label="本条结论", scale=3)
            comment = gr.Textbox(label="备注（为什么这么判，可选）", scale=3)

        with gr.Row():
            gen_btn = gr.Button("⚡ 全部生成", variant="secondary")
            regen_btn = gr.Button("🔄 重生成本题")
            submit_btn = gr.Button("✅ 提交并下一条", variant="primary")

        with gr.Row():
            back_btn = gr.Button("← 上一条")
            skip_btn = gr.Button("⏭ 跳过")
            case_pick = gr.Dropdown(choices=[], label="题目（✓ 已评）", scale=3,
                                    interactive=True, min_width=240)

        stats = gr.Markdown(stats_markdown(None))
        runfile = gr.Textbox(label="本轮结果文件", interactive=False)

        with gr.Row():
            export_csv_btn = gr.Button("⬇ 导出 CSV（逐样本）")
            export_dpo_btn = gr.Button("⬇ 导出 DPO 对子", variant="primary")
            export_md_btn = gr.Button("⬇ 导出评测报告")

        with gr.Row():
            with gr.Column(visible=False) as csv_box:
                csv_file = gr.File(label="CSV")
            with gr.Column(visible=False) as dpo_box:
                dpo_file = gr.File(label="DPO jsonl")
            with gr.Column(visible=False) as md_box:
                md_file = gr.File(label="评测报告")

        # 组装输出顺序：必须与 ORDER 完全一致
        comps: dict[str, Any] = {
            "state": state, "progress": progress_md, "case_head": case_head,
            "note": note, "verdict": verdict, "comment": comment,
            "stats": stats, "runfile": runfile, "case_list": case_pick,
        }
        for i in range(MAX_SAMPLES):
            comps[f"g{i}"] = sample_groups[i]
            comps[f"t{i}"] = texts[i]
            for j in range(profiles.MAX_DIMS):
                comps[f"s{i}_{j}"] = sliders[i][j]
        outputs = [comps[k] for k in ORDER]

        # 切档案时要一起刷新的组件。顺序必须与 do_load_profile 的返回一一对应：
        # 每个样本 (分组, 文本, 各维度滑块)。
        sample_widgets: list[Any] = []
        for i in range(MAX_SAMPLES):
            sample_widgets += [sample_groups[i], texts[i]]
            sample_widgets += [sliders[i][j] for j in range(profiles.MAX_DIMS)]

        submit_inputs = [state] + texts + [
            sliders[i][j] for i in range(MAX_SAMPLES) for j in range(profiles.MAX_DIMS)
        ] + [verdict, comment]

        profile_pick.change(
            do_load_profile,
            inputs=[profile_pick],
            outputs=[sys_prompt, dims_text, u_role, a_role, cases_path,
                     temp, max_tokens, profile_desc] + sample_widgets,
        )
        save_btn.click(
            do_save_profile,
            inputs=[profile_pick, save_key, sys_prompt, dims_text, u_role, a_role,
                    case_source, cases_path, upload, cases_text, temp, max_tokens],
            outputs=[save_key],
        )
        start_btn.click(
            do_start,
            inputs=[profile_pick, case_source, cases_path, upload, cases_text,
                    sys_prompt, dims_text, u_role, a_role,
                    label, url, model, key, temp, top_p, max_tokens, n_samples],
            outputs=outputs,
        )

        tpl_btn.click(lambda: casebook.TEMPLATE, inputs=[], outputs=[cases_text])
        clear_btn.click(lambda: "", inputs=[], outputs=[cases_text])
        copy_btn.click(do_copy_cases, inputs=[cases_path, profile_pick],
                       outputs=[cases_text])
        backend_pick.change(
            do_pick_backend,
            inputs=[backend_pick],
            outputs=[label, url, model, key, backend_note],
        )
        backend_reload.click(
            do_reload_backends,
            inputs=[backend_pick],
            outputs=[backend_pick, backend_note, label, url, model, key],
        )
        resume_btn.click(do_resume, inputs=[], outputs=outputs)
        gen_btn.click(lambda s, p=gr.Progress(): do_generate(s, False, p),
                      inputs=[state], outputs=outputs)
        regen_btn.click(lambda s, p=gr.Progress(): do_generate(s, True, p),
                        inputs=[state], outputs=outputs)
        submit_btn.click(do_submit, inputs=submit_inputs, outputs=outputs)
        back_btn.click(do_back, inputs=[state], outputs=outputs)
        skip_btn.click(do_skip, inputs=[state], outputs=outputs)
        case_pick.select(do_pick, inputs=[state, case_pick], outputs=outputs)
        export_csv_btn.click(do_export_csv, inputs=[state], outputs=[csv_file, csv_box])
        export_dpo_btn.click(do_export_dpo, inputs=[state], outputs=[dpo_file, dpo_box])
        export_md_btn.click(do_export_report, inputs=[state], outputs=[md_file, md_box])

        demo.load(do_autoload, inputs=[], outputs=outputs)

    return demo
