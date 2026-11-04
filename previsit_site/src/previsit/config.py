"""读取 config/previsit.yaml。

刻意不复用主 Config：预问诊的槽位/危险词/分诊规则是一份**独立的领域配置**，
塞进主 Config 会让参数面板、模型页、KB 覆盖那套逻辑都跟着膨胀。这里读成一个
普通 dict，engine 直接消费——"只加配置，不动核心"就落在这里。
"""

from __future__ import annotations

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _home() -> Path:
    """定位"项目根"（config/ 和 data/ 都相对它）。

    优先级：环境变量 PREVISIT_HOME > 包路径向上推（src/previsit → 根）>
    当前工作目录。独立部署目录的布局只要保持 `config/previsit.yaml` 存在，
    三种方式任一命中即可；都不满足时退回包推根，行为和主项目一致。
    """
    env = os.environ.get("PREVISIT_HOME", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # src/previsit/config.py → parents[2] = 项目根（独立部署目录同布局）
    for cand in (here.parents[2], Path.cwd()):
        if (cand / "config" / "previsit.yaml").exists():
            return cand
    return here.parents[2]


ROOT = _home()
DEFAULT_PATH = ROOT / "config" / "previsit.yaml"
# 知识库单独一个文件：它会越写越长（几十个症状域、上百条问诊要点），
# 混在 previsit.yaml 里会把"改一个标题"和"加一条医学知识"挤在同一屏。
DEFAULT_KB_PATH = ROOT / "config" / "previsit_kb.yaml"


def _defaults() -> dict[str, Any]:
    """文件缺失时的最小可用配置：哪怕 previsit.yaml 被删了，站点也能起来。"""
    return {
        "enabled": True,
        "title": "智能预问诊",
        "subtitle": "结构化采集症状，给出就诊科室建议 · 不构成诊断",
        "welcome": "您好，我帮您把症状整理成一份就诊摘要。先说说您哪里不舒服？",
        "disclaimer": "本系统仅用于就诊前信息整理，不构成任何诊断或治疗建议。如有剧烈或持续加重的症状，请立即就医或拨打 120。",
        "placeholder": "描述一下哪里不舒服，例如：肚子疼",
        "provider": None,
        "model": None,
        # null = 走主服务 /v1 固定入口；false / "direct" = 只用自己的地址
        "llm_gateway": None,
        "temperature": 0.2,
        "max_tokens": 320,
        "timeout_s": 20,
        "max_turns": 10,
        "slots": [
            {"key": "site", "label": "部位", "hint": "具体是哪个位置", "multi": False},
            {"key": "nature", "label": "性质", "hint": "胀痛/绞痛/刺痛/隐痛", "multi": False},
            {"key": "companions", "label": "伴随症状", "hint": "有没有恶心、呕吐、发热等", "multi": True},
            {"key": "duration", "label": "持续时间", "hint": "大概持续了多久", "multi": False},
            {"key": "migration", "label": "转移性疼痛", "hint": "疼痛是否从别处转移过来", "multi": False},
            {"key": "temperature", "label": "体温", "hint": "最高多少度", "multi": False},
            {"key": "trigger", "label": "诱因", "hint": "什么情况下加重或缓解", "multi": False},
            {"key": "pattern", "label": "规律", "hint": "发作频次或时间规律", "multi": False},
            {"key": "history", "label": "相关病史", "hint": "既往史、用药、接触史", "multi": False},
            {"key": "allergy", "label": "过敏史", "hint": "对药物、食物或其它东西过敏吗",
             "multi": False, "always": True},
            {"key": "past_history", "label": "既往病史",
             "hint": "有没有高血压、糖尿病等慢性病，或做过手术",
             "multi": False, "always": True},
        ],
        "danger_rules": [],
        "triage_rules": [],
        "default": {"dept": "全科医学科", "urgency": "门诊就诊"},
        "forbidden": ["确诊", "诊断为", "建议服用", "处方"],
        "kb": [],
        "domains": [],
        # 基本信息表单：没有配置时也要能开张，只问性别和年龄这两项
        # 真正影响病历抬头与分诊判断的。
        "profile_fields": [
            {"key": "gender", "label": "性别", "type": "select",
             "options": ["男", "女"], "required": True},
            {"key": "age", "label": "年龄", "type": "number",
             "min": 0, "max": 120, "unit": "岁", "required": True},
        ],
        "record": {
            "title": "预问诊病历",
            "sections": [
                {"key": "basic", "label": "基本信息"},
                {"key": "chief", "label": "主诉"},
                {"key": "present", "label": "现病史"},
                {"key": "past", "label": "既往史 / 过敏 / 用药"},
                {"key": "triage", "label": "分诊建议"},
                {"key": "summary", "label": "就诊摘要"},
            ],
            "note": "本简历由 AI 辅助整理，仅用于就诊前信息汇总，不构成诊断或治疗建议。",
        },
        "prompts": {
            "extract": "从患者描述里抽取症状信息，只输出 JSON。字段：{fields}。已知信息（不要重复问）：{profile}",
            "ask": "你是预问诊助手。已知：{known}；基本情况：{profile}。请就「{label}」（{hint}）问一句自然的问题。参考：{tips}。推荐问法：{ask}。{retry}只输出这一句，不下诊断、不推荐药物。",
            "report": "根据以下信息写一段不超过120字的就诊摘要：{known}。基本情况：{profile}。建议科室：{dept}，{urgency}。最后一句写「以上信息由 AI 辅助整理，请以医生面诊为准」。",
        },
    }


def _deep_fill(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """只补顶层缺失键，不做深合并：slots / kb 这类列表应当整体替换，
    合并会让"我删掉一条危险词"在配置里看不出来。"""
    out = dict(base)
    for k, v in raw.items():
        if v is not None:
            out[k] = v
    return out


def _merge_overrides(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """把运行时覆盖层叠到配置上。

    和 `_deep_fill` 的区别：覆盖层只改**单个键**（`kg.ros_max`、`retrieval.top_k`），
    必须逐层合并；否则改一个温度就会把整个 `kg` 段抹掉。列表仍然整体替换——
    配置里没有"往危险词表里追加一条"这种语义，只有"这张表长这样"。
    """
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_overrides(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def _read_overrides(p: Path) -> dict[str, Any]:
    """读 previsit.overrides.yaml（后台「站点 → 预问诊」写的那一层）。

    文件不存在 / 写坏了 / 根节点不是 dict —— 一律当没配。参数面板坏掉不该
    让站点起不来，这条和 `slots` 清洗是同一个立场。
    """
    op = p.parent / "previsit.overrides.yaml"
    if not op.exists():
        return {}
    try:
        data = yaml.safe_load(op.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"[previsit] {op.name} 解析失败，已忽略覆盖层：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _clean_domains(raw: Any, slot_keys: set[str]) -> list[dict[str, Any]]:
    """清洗症状域。

    这是知识库唯一的入口，所以脏数据必须在这里挡住：一个域的 slots 里
    写了不存在的槽位 key，或者 entry 挂到了域内未声明的槽位上，都会让
    状态机找不到槽位定义而报 KeyError。宁可丢一条配置，不能让站点挂掉。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in raw or []:
        if not (isinstance(d, dict) and d.get("key")):
            continue
        key = str(d["key"]).strip()
        if not key or key in seen:
            continue
        slots = [str(s) for s in (d.get("slots") or []) if str(s) in slot_keys]
        if not slots:
            continue                       # 一个槽位都没有的域问不出任何东西
        entries: list[dict[str, Any]] = []
        for e in d.get("entries") or []:
            if not isinstance(e, dict):
                continue
            slot = str(e.get("slot") or "")
            text = str(e.get("text") or "").strip()
            if slot not in slots or not text:
                continue
            entries.append({
                "slot": slot,
                "keywords": [str(k) for k in (e.get("keywords") or []) if str(k).strip()],
                "text": text,
                "ask": str(e.get("ask") or "").strip(),
            })
        if not entries:
            continue
        hints = {str(k): str(v) for k, v in (d.get("slot_hints") or {}).items()
                 if str(k) in slots}
        seen.add(key)
        out.append({
            "key": key,
            "label": str(d.get("label") or key),
            "dept": str(d.get("dept") or ""),
            "keywords": [str(k) for k in (d.get("keywords") or []) if str(k).strip()],
            "slots": slots,
            "slot_hints": hints,
            "entries": entries,
        })
    return out


def load_previsit(path: str | Path | None = None,
                  kb_path: str | Path | None = None,
                  *, use_overrides: bool = True) -> dict[str, Any]:
    """读配置。`use_overrides=False` 拿到的是"出厂值"（不含后台改过的那层），
    参数面板用它来显示每一项的默认值、判断该项有没有被改过。"""
    p = Path(path) if path else DEFAULT_PATH
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                raw = data
        except yaml.YAMLError as exc:      # 配置写坏了也要能启动
            print(f"[previsit] {p.name} 解析失败，已退回默认配置：{exc}")
    out = _deep_fill(_defaults(), raw)
    if use_overrides:
        over = _read_overrides(p)
        if over:
            out = _merge_overrides(out, over)

    # 槽位必须有 key / label，坏条目直接丢，不让一行配置把站点弄挂。
    slots = []
    for s in out.get("slots") or []:
        if isinstance(s, dict) and s.get("key") and s.get("label"):
            slots.append({
                "key": str(s["key"]),
                "label": str(s["label"]),
                "hint": str(s.get("hint") or s["label"]),
                "multi": bool(s.get("multi")),
                # always：全域必问（过敏史、既往病史这类不受症状域管辖的槽位）
                "always": bool(s.get("always")),
            })
    out["slots"] = slots

    # 表单字段同样做清洗：key/label 缺一个就丢，type 不认识的一律当 text。
    # 前端是按 type 渲染控件的，写错一个字母不能让整张表单渲染成空白。
    kinds = {"text", "number", "textarea", "select"}
    fields = []
    for f in out.get("profile_fields") or []:
        if not (isinstance(f, dict) and f.get("key") and f.get("label")):
            continue
        kind = str(f.get("type") or "text").lower()
        item = {
            "key": str(f["key"]),
            "label": str(f["label"]),
            "type": kind if kind in kinds else "text",
            "required": bool(f.get("required")),
            "placeholder": str(f.get("placeholder") or ""),
        }
        if item["type"] == "select":
            opts = [str(x) for x in (f.get("options") or []) if str(x).strip()]
            if not opts:                 # 没有选项的 select 是坏配置，退化成输入框
                item["type"] = "text"
            else:
                item["options"] = opts
        if item["type"] == "number":
            for k in ("min", "max"):
                if isinstance(f.get(k), (int, float)):
                    item[k] = f[k]
            if f.get("unit"):
                item["unit"] = str(f["unit"])
        if isinstance(f.get("show_if"), dict) and f["show_if"].get("field"):
            item["show_if"] = {"field": str(f["show_if"]["field"]),
                               "value": str(f["show_if"].get("value") or "")}
        fields.append(item)
    out["profile_fields"] = fields

    rec = out.get("record") if isinstance(out.get("record"), dict) else {}
    out["record"] = {
        "title": str(rec.get("title") or "预问诊病历"),
        "sections": [s for s in (rec.get("sections") or []) if isinstance(s, dict) and s.get("key")],
        "note": str(rec.get("note") or ""),
    }

    # 知识库独立文件：症状域 → 槽位 → {知识点, 现成问法}。
    # 读不到或解析失败都只是"没有分域知识"，engine 会退回全局槽位 + 旧 kb 条目，
    # 预问诊照常能跑——知识库是增强，不是启动前提。
    kp = Path(kb_path) if kb_path else DEFAULT_KB_PATH
    kb_raw: dict[str, Any] = {}
    if kp.exists():
        try:
            data = yaml.safe_load(kp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                kb_raw = data
        except yaml.YAMLError as exc:
            print(f"[previsit] {kp.name} 解析失败，已跳过症状域知识：{exc}")
    out["domains"] = _clean_domains(kb_raw.get("domains"), {s["key"] for s in slots})
    out["default_domain"] = str(kb_raw.get("default_domain") or "general")
    return out
