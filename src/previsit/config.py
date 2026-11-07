"""读取 config/previsit.yaml。

刻意不复用主 Config：这份配置只服务预问诊这一个入口，读成一个普通 dict 就够了。
2026-11-07 起它只剩三样东西——界面文案、模型参数、**一段**提示词。槽位、危险词、
分诊规则、追问知识库、图谱库都已删除：判断交给模型，配置不再承担"教系统怎么问诊"
的职责。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _home() -> Path:
    """定位"项目根"（config/ 和 data/ 都相对它）。

    优先级：环境变量 PREVISIT_HOME > 包路径向上推（src/previsit → 根）>
    当前工作目录。独立部署目录的布局只要保持 `config/previsit.yaml` 存在，
    三种方式任一命中即可。
    """
    env = os.environ.get("PREVISIT_HOME", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for cand in (here.parents[2], Path.cwd()):
        if (cand / "config" / "previsit.yaml").exists():
            return cand
    return here.parents[2]


ROOT = _home()
DEFAULT_PATH = ROOT / "config" / "previsit.yaml"


def _defaults() -> dict[str, Any]:
    """文件缺失时的最小可用配置：哪怕 previsit.yaml 被删了，站点也能起来。"""
    return {
        "enabled": True,
        "title": "预问诊",
        "subtitle": "和医生助手聊聊哪里不舒服",
        "welcome": "您好，先说说哪里不舒服？",
        "disclaimer": "本系统仅用于就诊前信息整理，不构成任何诊断或治疗建议。"
                      "如有剧烈或持续加重的症状，请立即就医或拨打 120。",
        "placeholder": "描述一下哪里不舒服，例如：肚子疼",
        "provider": None,
        "model": None,
        # 维吾尔语会话的翻译模型（只 ug 用）。空 = 用 app.py 里的内置强模型。
        "translator_provider": None,
        "translator_model": None,
        # null = 走主服务 /v1 固定入口；false / "direct" = 只用自己的地址
        "llm_gateway": None,
        "temperature": 0.3,
        "max_tokens": 320,
        "timeout_s": 20,
        "prompts": {
            "chat": (
                "你是门诊的预问诊助手，态度礼貌温和，用\"您\"称呼患者。"
                "开场先问候再问哪里不舒服；每轮先简短接住患者的话，"
                "再只问一个最关键的问题（部位、性质、持续多久、伴随症状、"
                "既往病史、过敏史、用药），口语化、两句话以内，不要列清单。"
                "不下诊断、不判断病情轻重、不推荐药物；患者说过的不要再问。"
                "信息够了先感谢配合，再用两三句话复述了解到的情况，"
                "并建议去哪个科室、要不要尽快就医。"
            ),
        },
    }


def _merge(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in raw.items():
        if v is not None:
            out[k] = v
    return out


def _merge_overrides(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """覆盖层只改单个键（`prompts.chat`、`max_tokens`），必须逐层合并；
    否则改一个温度就会把整个 prompts 段抹掉。"""
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
    让站点起不来。
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


def load_previsit(path: str | Path | None = None, *,
                  use_overrides: bool = True) -> dict[str, Any]:
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
    out = _merge(_defaults(), raw)
    if use_overrides:
        over = _read_overrides(p)
        if over:
            out = _merge_overrides(out, over)

    # 提示词是壳子唯一的"大脑配置"：没写 / 写空了就退回默认那句，
    # 不能让一次手滑把系统提示词变成空字符串。
    # 作答语言跟随界面语言：chat 是中文默认，chat_en 是英语整段翻译；
    # 维语（ug）不走整段翻译——本地 7B 看不懂维语系统指令，所以 ug 喂中文
    # 提示、由外部翻译模型在边界处转维吾尔语（见 chat.py）。chat_ug 保留作
    # 参考，当前运行时不用。
    prompts = out.get("prompts") if isinstance(out.get("prompts"), dict) else {}
    kept: dict[str, str] = {}
    for key in ("chat", "chat_en"):
        val = str(prompts.get(key) or "").strip()
        if val:
            kept[key] = val
    if not kept.get("chat"):
        kept["chat"] = _defaults()["prompts"]["chat"]
    out["prompts"] = kept
    return out
