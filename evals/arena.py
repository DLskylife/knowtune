#!/usr/bin/env python
"""A/B 双模型盲测打分台：同一批输入喂两个端点，左右随机、人只打分不认人。

**它不预设任何场景。** 评什么由「评测档案」决定（`profiles.py`）：提示词、
评分维度、对话里的角色名、题库、规则体检词。预问诊只是内置档案之一，评写作、
评代码、评客服话术只要换一份档案，或者改一下维度文本。

为什么必须盲测：人会下意识偏袒自己刚训完的那一边，而且这个偏向在打分时是
**感觉不到**的。骰子掷到哪边、分数就记给哪边，提交后才落盘。

用法：
    evals/.venv/Scripts/python.exe evals/scoreboard.py
    # 打开后停在「A/B 双模型盲测」页
"""

from __future__ import annotations

import csv
import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

import backends
import casebook
import profiles
import skin
from common import (
    RUNS,
    Endpoint,
    cases_summary,
    dialog_text,
    run_dialog,
)

# 会话文件沿用旧名字：改了名，用户昨天评到一半的那一轮就"恢复"不回来了。
SESSION = RUNS / "_session.json"

# 打开页面时先从主项目的后端配置里填一套。放模块级：界面重建时不必再探端口。
_A_VAL, _B_VAL = backends.default_pair()
_BLANK = {"label": "", "base_url": "", "model": "", "api_key": ""}
_A = backends.fill(_A_VAL) or dict(_BLANK)
_B = backends.fill(_B_VAL) or dict(_BLANK)

WINNERS = [
    ("① 更好", "L"),
    ("② 更好", "R"),
    ("不相上下", "tie"),
    ("都不可用", "bad"),
]


# ── 档案 ────────────────────────────────────────────────────────────────────

def _profile(state: dict[str, Any] | None) -> dict[str, Any]:
    prof = (state or {}).get("profile")
    if isinstance(prof, dict) and prof.get("dimensions"):
        return prof
    return profiles.get("previsit")           # 老会话 / 空状态


def _dims(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _profile(state)["dimensions"]


def _roles(state: dict[str, Any] | None) -> tuple[str, str]:
    prof = _profile(state)
    return (prof.get("user_role") or "用户", prof.get("ai_role") or "助手")


# ── 状态 ────────────────────────────────────────────────────────────────────

def new_state(cases: list[dict[str, Any]], run_file: Path, cfg_a: Endpoint,
              cfg_b: Endpoint, profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "cases": cases,
        "idx": 0,
        "flip": [random.random() < 0.5 for _ in cases],
        "gen": {},                      # idx -> {"A": run_dialog, "B": run_dialog}
        "results": [],                  # 提交后的记录
        "run_file": str(run_file),
        "profile": profile,
        "system": profile.get("system", ""),
        "A": cfg_a.label,
        "B": cfg_b.label,
        "cfgA": _ep_dict(cfg_a),
        "cfgB": _ep_dict(cfg_b),
    }


def _ep_dict(ep: Endpoint) -> dict[str, Any]:
    return {
        "label": ep.label, "base_url": ep.base_url, "model": ep.model,
        "api_key": ep.api_key, "temperature": ep.temperature, "top_p": ep.top_p,
        "max_tokens": ep.max_tokens, "timeout": ep.timeout,
    }


def _ep(state: dict[str, Any], side: str) -> Endpoint:
    return Endpoint(**state[f"cfg{side}"])


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
        state["profile"] = profiles.get("previsit")
    return state


def append_record(state: dict[str, Any], record: dict[str, Any]) -> None:
    with open(state["run_file"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── 统计 ────────────────────────────────────────────────────────────────────

def stats_markdown(state: dict[str, Any]) -> str:
    dims = _dims(state)
    prof = _profile(state)
    rows = state.get("results") or []
    total = len(state.get("cases") or [])
    if not rows:
        return (f"### 统计　·　{prof['label']}\n"
                f"已评 **0** / {total} 条。先点「批量生成」，再逐条打分。")

    def avg(side: str, key: str) -> float:
        vals = [r[side]["score"].get(key) for r in rows
                if r[side]["score"].get(key) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def wins(side: str) -> int:
        return sum(1 for r in rows if r["winner"] == side)

    ties = sum(1 for r in rows if r["winner"] == "tie")
    bads = sum(1 for r in rows if r["winner"] == "bad")
    n = len(rows)
    a_name, b_name = state.get("A", "A"), state.get("B", "B")

    lines = [
        f"### 统计　已评 **{n}** / {total}　·　{prof['label']}",
        "",
        f"| 维度 | {a_name} | {b_name} |",
        "|---|---|---|",
    ]
    for d in dims:
        a, b = avg("A", d["key"]), avg("B", d["key"])
        mark = "**" if abs(a - b) >= 0.5 else ""
        lines.append(f"| {d['label']} | {mark}{a:.2f}{mark} | {mark}{b:.2f}{mark} |")
    mean_a = sum(avg("A", d["key"]) for d in dims) / len(dims)
    mean_b = sum(avg("B", d["key"]) for d in dims) / len(dims)
    lines.append(f"| **综合** | **{mean_a:.2f}** | **{mean_b:.2f}** |")

    def avg_ms(side: str) -> float:
        vals = [r[side]["ms"] for r in rows]
        return sum(vals) / len(vals) / 1000 if vals else 0.0

    lines.append(f"| 平均耗时 | {avg_ms('A'):.1f}s | {avg_ms('B'):.1f}s |")
    lines.append("")
    dec = n - ties - bads
    a_w, b_w = wins("A"), wins("B")
    rate = f"A 胜率 {a_w / dec:.0%} / B 胜率 {b_w / dec:.0%}" if dec else "还没有可判定的对比"
    lines.append(f"**胜负**：{a_name} {a_w} 胜 · {b_name} {b_w} 胜 · 平 {ties} · 都不可用 {bads}"
                 f"　（{rate}）")
    if n < total:
        lines.append("")
        lines.append(f"还剩 {total - n} 条。")
    return "\n".join(lines)


def write_csv(state: dict[str, Any]) -> str | None:
    dims = _dims(state)
    rows = state.get("results") or []
    if not rows:
        return None
    path = RUNS / "latest.csv"
    header = ["case_id", "tag", "winner", "a_label", "b_label"]
    header += [f"a_{d['key']}" for d in dims] + [f"b_{d['key']}" for d in dims]
    header += ["a_ms", "b_ms", "comment", "turns", "a_text", "b_text"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for r in rows:
            writer.writerow(
                [r["case_id"], r["tag"], r["winner"], r["A"]["label"], r["B"]["label"]]
                + [r["A"]["score"].get(d["key"]) for d in dims]
                + [r["B"]["score"].get(d["key"]) for d in dims]
                + [r["A"]["ms"], r["B"]["ms"], r["comment"],
                   " / ".join(r["turns"]), r["A"]["text"], r["B"]["text"]]
            )
    return str(path)


# ── UI 渲染 ─────────────────────────────────────────────────────────────────

ORDER = ["state", "progress", "case_head", "note", "left", "right"]
ORDER += [f"l{j}" for j in range(profiles.MAX_DIMS)]
ORDER += [f"r{j}" for j in range(profiles.MAX_DIMS)]
ORDER += ["winner", "comment", "stats", "runfile", "case_list"]


def _pack(d: dict[str, Any]) -> list[Any]:
    missing = [k for k in ORDER if k not in d]
    if missing:
        raise KeyError(f"渲染缺字段：{missing}")
    return [d[k] for k in ORDER]


def case_choices(state: dict[str, Any]) -> list[tuple[str, int]]:
    """题目导航：✓ 表示已评，方便一眼看出进度和回看某条。"""
    done = {r["case_id"] for r in (state.get("results") or [])}
    out: list[tuple[str, int]] = []
    for i, case in enumerate(state.get("cases") or []):
        mark = "✓" if case["id"] in done else "　"
        tag = f" {case['tag']}" if case["tag"] else ""
        out.append((f"{mark} {i + 1:02d}　{case['id']}{tag}", i))
    return out


def _case_update(state: dict[str, Any], idx: int) -> Any:
    return gr.update(choices=case_choices(state), value=idx if state.get("cases") else None)


def _slots(dims: list[dict[str, Any]],
           left_scores: dict[str, Any] | None = None,
           right_scores: dict[str, Any] | None = None) -> dict[str, Any]:
    """两列滑块：维度变了要连标签一起换，否则换档案后还写着上一份的名字。"""
    out: dict[str, Any] = {}
    n = len(dims)
    for j in range(profiles.MAX_DIMS):
        name = profiles.dim_label(dims[j]) if j < n else "—"
        for side, prefix, scores in (("l", "①", left_scores), ("r", "②", right_scores)):
            upd: dict[str, Any] = {
                "visible": j < n,
                "label": f"{prefix} {name}",
                "value": 3,
            }
            if j < n and scores is not None:
                upd["value"] = int(scores.get(dims[j]["key"], 3) or 3)
            out[f"{side}{j}"] = gr.update(**upd)
    return out


def render(state: dict[str, Any] | None) -> list[Any]:
    if not state or not state.get("cases"):
        dims = _dims(state)
        return _pack({
            "state": state,
            "progress": "**已评 0 条**",
            "case_head": "### 等待载入题库",
            "note": "",
            "left": "",
            "right": "",
            **_slots(dims),
            "winner": "tie",
            "comment": "",
            "stats": stats_markdown({"results": [], "cases": []}),
            "runfile": "",
            "case_list": gr.update(choices=[], value=None),
        })

    dims = _dims(state)
    u_role, a_role = _roles(state)
    idx, total = state["idx"], len(state["cases"])
    base = {
        "state": state,
        "progress": f"**已评 {len(state['results'])} 条**",
        "note": "",
        "winner": "tie",
        "comment": "",
        "stats": stats_markdown(state),
        "runfile": state["run_file"],
        "case_list": _case_update(state, idx),
        **_slots(dims),
    }

    if idx >= total:
        base.update({"case_head": "### ✅ 全部评完", "left": "（无更多题目）",
                     "right": "（无更多题目）"})
        return _pack(base)

    case = state["cases"][idx]
    flip = state["flip"][idx]
    gen = (state.get("gen") or {}).get(str(idx)) or state.get("gen", {}).get(idx)
    # 左= B 当且仅当这条掷到 flip；名字全程不出现，避免评分被标签带偏
    left_side = "B" if flip else "A"
    right_side = "A" if flip else "B"
    tag = f"　`{case['tag']}`" if case["tag"] else ""

    if not gen:
        left_txt = right_txt = "（尚未生成，先点「批量生成」）"
        head = f"### 第 {idx + 1} / {total} 题　`{case['id']}`{tag}"
    else:
        left_txt = dialog_text(case["turns"], gen[left_side]["replies"], u_role, a_role)
        right_txt = dialog_text(case["turns"], gen[right_side]["replies"], u_role, a_role)
        head = (f"### 第 {idx + 1} / {total} 题　`{case['id']}`{tag}"
                f"　·　耗时 {gen[left_side]['ms'] / 1000:.1f}s / {gen[right_side]['ms'] / 1000:.1f}s")

    base.update({
        "case_head": head,
        "note": f"💡 {case['note']}" if case["note"] else "",
        "left": left_txt,
        "right": right_txt,
    })
    return _pack(base)


# ── 动作 ────────────────────────────────────────────────────────────────────

def _profile_from(dims_text: str, system: str, u_role: str, a_role: str,
                  key: str = "") -> dict[str, Any]:
    dims = profiles.parse_dimensions(dims_text)
    prof = dict(profiles.get(key or "general"))
    prof["dimensions"] = dims
    prof["system"] = system or ""
    prof["user_role"] = (u_role or "").strip() or "用户"
    prof["ai_role"] = (a_role or "").strip() or "助手"
    return prof


def _side_updates(value: str):
    got = backends.fill(value)
    if not got:
        return (gr.update(), gr.update(), gr.update(), gr.update())
    return (gr.update(value=got["label"]), gr.update(value=got["base_url"]),
            gr.update(value=got["model"]), gr.update(value=got["api_key"]))


def do_pick_a(value):
    return _side_updates(value)


def do_pick_b(value):
    return _side_updates(value)


def do_reload_backends(a_val, b_val):
    """重读 config 与密钥文件：刚在后台加过后端 / 填过密钥时点。"""
    backends.refresh()
    ch = backends.choices()
    vals = {v for _, v in ch}
    av = a_val if a_val in vals else backends.default_value()
    bv = b_val if b_val in vals else av
    return (gr.update(choices=ch, value=av), gr.update(choices=ch, value=bv),
            backends.summary(), *_side_updates(av), *_side_updates(bv))


def do_copy_cases(path, profile_key):
    """把当前题库转成手输格式，抄进输入框改两行——比让人从零手打靠谱。"""
    try:
        rules = profiles.get(profile_key or "previsit").get("rules") or []
        return casebook.text_from_path(path, rules)
    except Exception as exc:
        raise gr.Error(str(exc)) from exc


def do_start(profile_key, source, cases_path, upload, cases_text,
             sys_prompt, dims_text, u_role, a_role,
             a_label, a_url, a_model, a_key, a_temp, a_top_p, a_max,
             b_label, b_url, b_model, b_key, b_temp, b_top_p, b_max):
    try:
        prof = _profile_from(dims_text, sys_prompt, u_role, a_role, profile_key)
        source = casebook.normalize_source(source)
        cases = casebook.resolve(source, cases_path, upload, cases_text,
                                 prof.get("rules") or [])
    except Exception as exc:
        raise gr.Error(str(exc)) from exc
    stamp = datetime.now().strftime("%m%d-%H%M")
    run_file = RUNS / f"{stamp}_{a_label or 'A'}__vs__{b_label or 'B'}.jsonl".replace(" ", "-")
    cfg_a = Endpoint(a_label or "A", a_url, a_model, a_key, a_temp, a_top_p, int(a_max))
    cfg_b = Endpoint(b_label or "B", b_url, b_model, b_key, b_temp, b_top_p, int(b_max))
    state = new_state(cases, run_file, cfg_a, cfg_b, prof)
    save_session(state)
    gr.Info(f"档案「{prof['label']}」· {cases_summary(cases)}，结果写入 {run_file.name}")
    return render(state)


def do_load_profile(key):
    """切档案：提示词 / 维度 / 角色 / 题库 / 滑块标签一起换。

    只换提示词不换维度，是这套东西最容易犯的错——用预问诊的四个维度去评写作，
    分是有的，但没有人知道那个分在说什么。滑块标签不跟着改更糟：界面上写着
    "漏红旗"，人就会照着"漏红旗"打分。
    """
    prof = profiles.get(key or "previsit")
    slots = _slots(prof["dimensions"])
    desc = (f"**{prof['label']}**　{prof.get('desc') or ''}\n\n"
            f"维度：{'、'.join(d['label'] for d in prof['dimensions'])}"
            + (f"　·　规则 {len(prof['rules'])} 条" if prof.get("rules") else ""))
    return (
        gr.update(value=prof["system"]),
        gr.update(value=profiles.format_dimensions(prof["dimensions"])),
        gr.update(value=prof.get("user_role") or "用户"),
        gr.update(value=prof.get("ai_role") or "助手"),
        gr.update(value=prof.get("cases") or ""),
        desc,
    ) + tuple(slots[f"l{j}"] for j in range(profiles.MAX_DIMS)) \
      + tuple(slots[f"r{j}"] for j in range(profiles.MAX_DIMS))


def do_generate(state, only_current: bool, progress=gr.Progress()):
    if not state or not state.get("cases"):
        raise gr.Error("先点「开始」载入题库")
    cfg_a, cfg_b = _ep(state, "A"), _ep(state, "B")
    system = state.get("system", "")
    targets = [state["idx"]] if only_current else list(range(len(state["cases"])))
    targets = [i for i in targets if i < len(state["cases"])]
    todo = [(i, side) for i in targets for side in ("A", "B")]
    if not todo:
        return render(state)

    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(run_dialog, cfg_a if side == "A" else cfg_b,
                        state["cases"][i]["turns"], system): (i, side)
            for i, side in todo
        }
        for fut in as_completed(futures):
            i, side = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                result = {"turns": state["cases"][i]["turns"], "replies": [],
                          "ms": 0, "error": f"{type(exc).__name__}: {exc}"}
            state.setdefault("gen", {})[str(i)] = state["gen"].get(str(i), {})
            state["gen"][str(i)][side] = result
            done += 1
            progress(done / len(todo), desc=f"生成中 {done}/{len(todo)}")

    save_session(state)
    errors = sum(1 for v in state["gen"].values() for r in v.values() if r.get("error"))
    if errors:
        gr.Warning(f"{errors} 处生成失败，页面上会显示 ⚠️，可改端点后重生成当前题")
    return render(state)


def do_submit(state, left_txt, right_txt, *rest):
    """rest = [l0..l5, r0..r5, winner, comment]。"""
    if not state or state["idx"] >= len(state["cases"]):
        return render(state)
    dims = _dims(state)
    u_role, a_role = _roles(state)
    idx = state["idx"]
    case = state["cases"][idx]
    flip = state["flip"][idx]
    left_side, right_side = ("B", "A") if flip else ("A", "B")
    gen = (state.get("gen") or {}).get(str(idx))

    if not gen:
        gr.Warning("这一题还没生成，先点「批量生成」再评")
        return render(state)

    flat = list(rest)
    left_scores = {dims[j]["key"]: int(flat[j]) for j in range(len(dims))}
    right_scores = {dims[j]["key"]: int(flat[profiles.MAX_DIMS + j]) for j in range(len(dims))}
    winner, comment = flat[-2], flat[-1]

    def side_record(side: str, text: str, score: dict[str, int]) -> dict[str, Any]:
        raw = dialog_text(case["turns"], gen[side]["replies"], u_role, a_role)
        return {
            "label": state[side],
            "text": text,
            "edited": text.strip() != raw.strip(),
            "score": score,
            "ms": gen[side]["ms"],
            "error": gen[side].get("error", ""),
        }

    record = {
        "case_id": case["id"],
        "tag": case["tag"],
        "turns": case["turns"],
        "system": state.get("system", ""),
        "task": (state.get("profile") or {}).get("key", ""),
        "A": side_record("A", left_txt if left_side == "A" else right_txt,
                         left_scores if left_side == "A" else right_scores),
        "B": side_record("B", left_txt if left_side == "B" else right_txt,
                         left_scores if left_side == "B" else right_scores),
        "winner": {"L": left_side, "R": right_side}.get(winner, winner),
        "flipped": flip,
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


def _apply_prior(out: list[Any], state: dict[str, Any]) -> list[Any]:
    """回退/跳转到一条已评过的题时，把当初的分数读回控件，方便改判。"""
    idx = state["idx"]
    if idx >= len(state["cases"]):
        return out
    case = state["cases"][idx]
    prior = next((r for r in state["results"] if r["case_id"] == case["id"]), None)
    if not prior:
        return out
    dims = _dims(state)
    flip = state["flip"][idx]
    ls, rs = ("B", "A") if flip else ("A", "B")
    for j, d in enumerate(dims):
        out[ORDER.index(f"l{j}")] = prior[ls]["score"].get(d["key"], 3)
        out[ORDER.index(f"r{j}")] = prior[rs]["score"].get(d["key"], 3)
    out[ORDER.index("winner")] = {
        "A": "L" if ls == "A" else "R",
        "B": "L" if ls == "B" else "R",
        "tie": "tie", "bad": "bad",
    }[prior["winner"]]
    out[ORDER.index("comment")] = prior["comment"]
    return out


def do_pick(state, idx):
    """题目下拉框选中即跳转（用 select 事件，避免 render 回填时自触发）。"""
    if state is None or idx is None:
        return render(None)
    return do_goto(state, int(idx) + 1)


def do_back(state):
    if not state or state["idx"] == 0:
        return render(state)
    state["idx"] -= 1
    save_session(state)
    return _apply_prior(render(state), state)


def do_goto(state, number):
    if not state or not state.get("cases"):
        raise gr.Error("还没有载入题库")
    total = len(state["cases"])
    state["idx"] = max(0, min(int(number or 1) - 1, total - 1))
    save_session(state)
    return _apply_prior(render(state), state)


def do_resume():
    state = load_session()
    if state is None:
        raise gr.Error("没有可恢复的会话")
    gr.Info(f"已恢复：{_profile(state)['label']} · 第 {state['idx'] + 1} 题，"
            f"已评 {len(state['results'])} 条")
    return render(state)


def do_autoload():
    """页面打开时静默恢复：没会话不等于错，别一进来就弹红字。"""
    return render(load_session())


def do_export(state):
    path = write_csv(state or {})
    if not path:
        raise gr.Error("还没有打分记录")
    return gr.update(value=path, visible=True), gr.update(visible=True)


def do_generate_all(state, progress=gr.Progress()):
    return do_generate(state, False, progress)


def do_generate_one(state, progress=gr.Progress()):
    return do_generate(state, True, progress)


# ── 界面 ────────────────────────────────────────────────────────────────────

# 皮肤统一在 skin.py：两个页面各写一份，改一次必然漏一次。


def _build() -> gr.Blocks:
    first = profiles.get("previsit")
    with gr.Blocks(title="A/B 盲测打分台") as demo:
        state = gr.State(None)

        gr.Markdown("# ⚖️ A/B 双模型盲测打分台")
        gr.Markdown(
            "**顺序**：选评测档案 → 定题库（档案自带 / 导入文件 / 自己输入）→ "
            "填 A / B 两个端点 → 「开始」→ 「⚡ 全部生成」→ 逐条打分 → 「⬇ 导出 CSV」。\n\n"
            "左右两条回答**顺序随机**，打分时看不到哪边是哪边，提交后才落盘——不盲测的话，"
            "人会下意识偏袒自己刚训完的那一边。中途关掉页面，「恢复上次」接着评。"
        )

        with gr.Accordion("① 任务定义（评测档案 → 提示词 / 维度 / 角色 / 题库）", open=True):
            with gr.Row():
                profile_pick = gr.Dropdown(choices=profiles.choices(), value="previsit",
                                           label="评测档案（换档案会重置下面四项）",
                                           scale=3, min_width=240)
            profile_desc = gr.Markdown(
                f"**{first['label']}**　{first['desc']}\n\n"
                f"维度：{'、'.join(d['label'] for d in first['dimensions'])}")
            sys_prompt = gr.Textbox(value=first["system"], lines=3,
                                    label="system prompt（两边共用）")
            with gr.Row():
                dims_text = gr.Textbox(value=profiles.format_dimensions(first["dimensions"]),
                                       lines=5, label="评分维度（一行一个：键: 名称 | 提示）",
                                       scale=3)
                with gr.Column(scale=2, min_width=200):
                    u_role = gr.Textbox(value=first["user_role"], label="对话里「提问方」的角色名")
                    a_role = gr.Textbox(value=first["ai_role"], label="对话里「回答方」的角色名")
                    gr.Markdown("<span style='font-size:11px;opacity:.7'>"
                                "只影响展示与导出文本，不发给模型。</span>")
            # 三个来源常驻，只由 Radio 决定用哪份。
            # 显隐版和页签版都试过：前者在导入文件后切来源会失效，后者的 select
            # 事件时灵时不灵——界面看着对、评的是另一批题，最难发现。
            case_source = gr.Radio(
                casebook.SOURCES, value="档案自带", label="题目来源",
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

        with gr.Accordion("② 两个端点（A / B）", open=True):
            # A/B 的地址与模型在「模型服务」页配过：这里读进来直接填，不再手敲。
            # 两个模型选反了（A 是 base、B 是微调，界面写反）是最难发现的错误，
            # 因为盲测提交后只留下分数，没有人能回头看出"当时 A 到底是哪个"。
            with gr.Row():
                a_pick = gr.Dropdown(choices=backends.choices(), value=_A_VAL,
                                     label="A 从已配置的后端填入", scale=3, min_width=220)
                b_pick = gr.Dropdown(choices=backends.choices(), value=_B_VAL,
                                     label="B 从已配置的后端填入", scale=3, min_width=220)
                backend_reload = gr.Button("⟳ 重读配置", scale=1)
            backend_note = gr.Markdown(backends.summary())
            with gr.Row():
                with gr.Column():
                    a_label = gr.Textbox(value=_A["label"], label="A 的名字")
                    a_url = gr.Textbox(value=_A["base_url"], label="A base_url")
                    a_model = gr.Textbox(value=_A["model"], label="A model")
                    with gr.Accordion("A 高级参数", open=False):
                        a_key = gr.Textbox(value=_A["api_key"], label="A api_key", type="password")
                        with gr.Row():
                            a_temp = gr.Slider(0, 1.5, 0.7, step=0.05, label="temp")
                            a_top_p = gr.Slider(0.1, 1.0, 0.9, step=0.05, label="top_p")
                        a_max = gr.Number(value=512, label="max_tokens", precision=0)
                with gr.Column():
                    b_label = gr.Textbox(value=_B["label"], label="B 的名字")
                    b_url = gr.Textbox(value=_B["base_url"], label="B base_url")
                    b_model = gr.Textbox(value=_B["model"], label="B model")
                    with gr.Accordion("B 高级参数", open=False):
                        b_key = gr.Textbox(value=_B["api_key"], label="B api_key", type="password")
                        with gr.Row():
                            b_temp = gr.Slider(0, 1.5, 0.7, step=0.05, label="temp")
                            b_top_p = gr.Slider(0.1, 1.0, 0.9, step=0.05, label="top_p")
                        b_max = gr.Number(value=512, label="max_tokens", precision=0)
            with gr.Row():
                start_btn = gr.Button("开始 / 新建一轮", variant="primary", scale=1)
                resume_btn = gr.Button("恢复上次", scale=1)

        with gr.Row():
            case_head = gr.Markdown("### 等待载入题库", elem_id="casehead", scale=4)
            progress_md = gr.Markdown("**已评 0 条**", scale=1)
        note = gr.Markdown("")

        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown("### 候选 ①")
                left = gr.Textbox(lines=9, show_label=False, interactive=True, elem_classes="cand")
                l_sliders = []
                with gr.Group():
                    for j in range(profiles.MAX_DIMS):
                        n = len(first["dimensions"])
                        l_sliders.append(gr.Slider(
                            profiles.MIN_SCORE, profiles.MAX_SCORE, 3, step=1,
                            visible=j < n,
                            label=f"① {profiles.dim_label(first['dimensions'][j]) if j < n else '—'}"))
            with gr.Column(scale=1):
                gr.Markdown("### 候选 ②")
                right = gr.Textbox(lines=9, show_label=False, interactive=True, elem_classes="cand")
                r_sliders = []
                with gr.Group():
                    for j in range(profiles.MAX_DIMS):
                        n = len(first["dimensions"])
                        r_sliders.append(gr.Slider(
                            profiles.MIN_SCORE, profiles.MAX_SCORE, 3, step=1,
                            visible=j < n,
                            label=f"② {profiles.dim_label(first['dimensions'][j]) if j < n else '—'}"))

        with gr.Row():
            winner = gr.Radio(choices=WINNERS, value="tie", label="结论", scale=2)
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
            export_btn = gr.Button("⬇ 导出 CSV")

        stats = gr.Markdown(stats_markdown({"results": [], "cases": []}))
        runfile = gr.Textbox(label="本轮结果文件", interactive=False)
        with gr.Column(visible=False) as csv_box:
            csv_file = gr.File(label="CSV 下载")

        comps: dict[str, Any] = {
            "state": state, "progress": progress_md, "case_head": case_head,
            "note": note, "left": left, "right": right,
            "winner": winner, "comment": comment, "stats": stats,
            "runfile": runfile, "case_list": case_pick,
        }
        for j in range(profiles.MAX_DIMS):
            comps[f"l{j}"] = l_sliders[j]
            comps[f"r{j}"] = r_sliders[j]
        outputs = [comps[k] for k in ORDER]

        submit_inputs = [state, left, right] + l_sliders + r_sliders + [winner, comment]

        profile_pick.change(
            do_load_profile,
            inputs=[profile_pick],
            outputs=[sys_prompt, dims_text, u_role, a_role, cases_path,
                     profile_desc] + l_sliders + r_sliders,
        )
        start_btn.click(
            do_start,
            inputs=[profile_pick, case_source, cases_path, upload, cases_text,
                    sys_prompt, dims_text, u_role, a_role,
                    a_label, a_url, a_model, a_key, a_temp, a_top_p, a_max,
                    b_label, b_url, b_model, b_key, b_temp, b_top_p, b_max],
            outputs=outputs,
        )

        tpl_btn.click(lambda: casebook.TEMPLATE, inputs=[], outputs=[cases_text])
        clear_btn.click(lambda: "", inputs=[], outputs=[cases_text])
        copy_btn.click(do_copy_cases, inputs=[cases_path, profile_pick],
                       outputs=[cases_text])
        resume_btn.click(do_resume, inputs=[], outputs=outputs)
        a_pick.change(do_pick_a, inputs=[a_pick],
                      outputs=[a_label, a_url, a_model, a_key])
        b_pick.change(do_pick_b, inputs=[b_pick],
                      outputs=[b_label, b_url, b_model, b_key])
        backend_reload.click(
            do_reload_backends, inputs=[a_pick, b_pick],
            outputs=[a_pick, b_pick, backend_note,
                     a_label, a_url, a_model, a_key,
                     b_label, b_url, b_model, b_key])
        gen_btn.click(do_generate_all, inputs=[state], outputs=outputs)
        regen_btn.click(do_generate_one, inputs=[state], outputs=outputs)
        submit_btn.click(do_submit, inputs=submit_inputs, outputs=outputs)
        back_btn.click(do_back, inputs=[state], outputs=outputs)
        skip_btn.click(do_skip, inputs=[state], outputs=outputs)
        # 用 select 而不是 input：input 每敲一个字符都触发跳转，输 "18" 会先跳到第 2 题
        case_pick.select(do_pick, inputs=[state, case_pick], outputs=outputs)
        export_btn.click(do_export, inputs=[state], outputs=[csv_file, csv_box])

        demo.load(do_autoload, inputs=[], outputs=outputs)

    return demo


app = _build()

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app.launch(server_name=args.host, server_port=args.port, inbrowser=True,
               theme=skin.THEME, css=skin.CSS)
