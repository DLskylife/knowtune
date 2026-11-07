"""预问诊的「运行时可改参数」。

后台「站点 → 预问诊」页改的就是这里定义的那批字段：模型、生成上限、轮次、
知识图谱追问、语义召回、界面文案、提示词。改完不用重启进程——引擎提供了
`apply_config()`，把新的配置字典灌进去即可。

**为什么不直接改 previsit.yaml**

那份文件是手写文档：几乎每个字段上面都压着一段"为什么是这个值"的注释。
用 `yaml.safe_dump` 回写会把这些注释连同顺序一起铲平，改一次参数、丢一份
设计说明，不划算。

所以这里维护一层独立的覆盖文件 `config/previsit.overrides.yaml`：

- 只落"和默认值不一样"的键。把某个字段改回默认值 → 那一行自动消失；
- 全空就删文件，磁盘上不留一具空壳；
- `previsit.yaml` 保持原样，随时可以拿它当"出厂设置"对照；
- 覆盖文件写坏了只是退回默认，站点照常启动（`load_previsit` 会打印并忽略）。

字段的默认值永远以 `previsit.yaml` 为准，这里不重复写第二份默认——两份
默认值迟早会分叉，然后没人说得清哪个才算数。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .config import DEFAULT_PATH, ROOT, load_previsit

OVERRIDES_NAME = "previsit.overrides.yaml"

# ---------------------------------------------------------------- 字段定义
# type 决定控件与校验：
#   text / textarea —— 字符串，maxlen 限长（提示词写太长会把上下文吃干）
#   int / float     —— 数值，min/max 是硬边界，越界直接拒
#   bool            —— 开关
#   select          —— 枚举，options 写死
#   provider        —— 后端下拉，选项来自运行环境（主项目的 models.yaml）
#   model           —— 模型名，前端按选中的后端给 datalist
#   password        —— 密钥类，界面上不回显
#
# `hot` 表示改完必须重建引擎内部的组件（模型客户端、知识图谱连接、检索器）。
# 目前没有需要重启进程的字段——这是这套面板的前提，破例之前先想清楚。
GROUPS: list[dict[str, str]] = [
    {"key": "model", "label": "模型与生成",
     "desc": "预问诊就是这段提示词加模型本身，这一组决定由谁来说、说多长。"},
    {"key": "flow", "label": "开关",
     "desc": "总开关。关掉后入口还在，但不会开新会话。"},
    {"key": "copy", "label": "界面文案",
     "desc": "站点标题、欢迎语、免责声明。改的是患者看到的那一层。"},
    {"key": "prompts", "label": "提示词",
     "desc": "整场问诊只有这一段系统提示词：怎么问、问几轮、何时收尾，全交给模型。"},
]

FIELDS: list[dict[str, Any]] = [
    # ------------------------------------------------------------ 模型与生成
    {"path": "provider", "group": "model", "type": "provider", "label": "模型后端",
     "help": "跟随后台默认 = 后台当前选中的后端；也可以给预问诊单配一个。",
     "empty": "跟随后台默认"},
    {"path": "model", "group": "model", "type": "model", "label": "模型",
     "help": "留空 = 用该后端的默认模型。可手填服务上真实存在的模型名。", "maxlen": 120},
    {"path": "temperature", "group": "model", "type": "float", "label": "温度",
     "help": "问诊要稳，0.1~0.4 就够；调高只会让问法更花哨，不会更准。",
     "min": 0.0, "max": 2.0, "step": 0.05},
    {"path": "max_tokens", "group": "model", "type": "int", "label": "单次生成上限（token）",
     "help": "一句追问 320 足够。本地 7B 给 1024 只是让患者多等。",
     "min": 64, "max": 4096, "step": 32},
    {"path": "timeout_s", "group": "model", "type": "int", "label": "模型超时（秒）",
     "help": "超时即报错，不编一句话糊过去。交互场景别给太大。",
     "min": 3, "max": 120, "step": 1},
    # 维吾尔语会话的翻译链路单独一条：问诊推理仍走上面那个模型，翻译要用
    # 能真写维语的模型（本地 7B 写不出来，会混成阿拉伯/拉丁/西里尔乱码）。
    # 这两项只对 ug 会话生效，中英文会话一次都不会用到。
    {"path": "translator_provider", "group": "model", "type": "provider",
     "label": "维语翻译后端",
     "help": "只给维吾尔语会话用；留空 = 跟随后台当前后端。",
     "empty": "跟随后台默认"},
    {"path": "translator_model", "group": "model", "type": "model",
     "label": "维语翻译模型", "maxlen": 120,
     "help": "留空 = DeepSeek-V4.1-Flash。必须是能写维语的模型；"
             "本地 7B 翻不出来，维语会话会直接报错（不会退回中文）。"},

    # ------------------------------------------------------------ 开关
    {"path": "enabled", "group": "flow", "type": "bool", "label": "启用预问诊",
     "help": "关掉后开新会话会被拒绝。"},

    # ------------------------------------------------------------ 文案
    {"path": "title", "group": "copy", "type": "text", "label": "站点标题", "maxlen": 60},
    {"path": "subtitle", "group": "copy", "type": "text", "label": "副标题", "maxlen": 120},
    {"path": "welcome", "group": "copy", "type": "textarea", "label": "欢迎语", "rows": 2,
     "maxlen": 300},
    {"path": "placeholder", "group": "copy", "type": "text", "label": "输入框占位提示",
     "maxlen": 80},
    {"path": "disclaimer", "group": "copy", "type": "textarea", "label": "免责声明", "rows": 3,
     "maxlen": 500},

    # ------------------------------------------------------------ 提示词
    {"path": "prompts.chat", "group": "prompts", "type": "textarea", "label": "系统提示词",
     "help": "整场问诊的唯一提示词。没有占位符，模型看得到全部对话历史。",
     "rows": 10, "maxlen": 4000},
]

_FIELD_BY_PATH: dict[str, dict[str, Any]] = {f["path"]: f for f in FIELDS}

# 这些键保存后必须重建客户端/连接，不能只换个字典了事。
RESTART_KEYS = ("provider", "model", "timeout_s")


# ---------------------------------------------------------------- 路径与读写
def overrides_path(cfg_path: str | Path | None = None) -> Path:
    """覆盖文件永远和它覆盖的那份配置放在一起。

    独立部署目录有自己的 `config/`，主项目也有自己的；把覆盖写到固定位置
    会让两个部署共用一份参数——那是最难查的一种串味。
    """
    p = Path(cfg_path) if cfg_path else DEFAULT_PATH
    return p.parent / OVERRIDES_NAME


def read_overrides(cfg_path: str | Path | None = None) -> dict[str, Any]:
    p = overrides_path(cfg_path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"[previsit] {p.name} 读不出来，已忽略：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _write_overrides(data: dict[str, Any],
                     cfg_path: str | Path | None = None) -> Path:
    p = overrides_path(cfg_path)
    if not data:
        # 没有覆盖项就删文件：一份空 YAML 会让人以为"这里配了点什么"。
        p.unlink(missing_ok=True)
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    text = (
        "# 预问诊运行时参数（后台「站点 → 预问诊」页维护）。\n"
        "#\n"
        "# 这里只记「和 config/previsit.yaml 不一样」的键，默认值仍以那份文件为准。\n"
        "# 想把某一项改回出厂值：在界面上改回去保存，或者直接删掉对应的行。\n"
        "# 手改也有效，下次保存时会被整理成同样的格式。\n"
        "#\n"
        "# 不写密钥：密钥走环境变量 PREVISIT_LLM_API_KEY（独立部署）或后台的\n"
        "# 模型服务页（主项目），不落在这个文件里。\n"
        f"{body}"
    )
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)                       # 原子替换：写一半断电不会留下半份配置
    return p


# ---------------------------------------------------------------- 点路径工具
def get_path(cfg: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(cfg: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _same(a: Any, b: Any) -> bool:
    """"算不算同一个值"。空串 / None / 空列表都当作同一件事。

    前端把所有字段都发一遍：一个从没配过的 `provider` 会变成空串，而 yaml 里
    写的是 `null`；一个没改过的提示词会被 textarea 顺带削掉结尾换行。严格
    相等会让这些"其实没改"的字段全被写进覆盖层，从此改默认值不再生效。
    """
    empty = (None, "", [], {})
    if a in empty and b in empty:
        return True
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    return a == b


def _prune(override: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """把和 base 相同的叶子去掉，只留真正改过的。

    不 prune 的话，用户在界面上点一次保存就会把二十几个字段全写进覆盖层，
    从此以后改 `previsit.yaml` 的默认值再也不生效——排查起来像撞鬼。
    """
    out: dict[str, Any] = {}
    for k, v in override.items():
        b = base.get(k) if isinstance(base, dict) else None
        if isinstance(v, dict):
            sub = _prune(v, b if isinstance(b, dict) else {})
            if sub:
                out[k] = sub
            continue
        if _same(b, v):
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------- 校验与保存
def _fail(path: str, msg: str) -> ValueError:
    label = (_FIELD_BY_PATH.get(path) or {}).get("label") or path
    return ValueError(f"「{label}」{msg}")


def _coerce(field: dict[str, Any], raw: Any) -> Any:
    """把前端送来的值收成配置想要的类型，越界就报错。

    前端已经做了一遍限位，但保存接口是公开的：从 devtools 里塞一个
    `max_turns: 100000` 进来，问诊就会把患者按在椅子上问到天亮。校验必须在
    后端再走一遍，这是"配置驱动的面板"唯一的护栏。
    """
    path = field["path"]
    kind = field["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        if s in ("true", "1", "on", "yes", "是"):
            return True
        if s in ("false", "0", "off", "no", "否", ""):
            return False
        raise _fail(path, "只能是开或关")

    if kind in ("int", "float"):
        if isinstance(raw, bool) or raw is None or str(raw).strip() == "":
            raise _fail(path, "必须填一个数字")
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise _fail(path, "必须填一个数字") from None
        if kind == "int":
            if abs(num - round(num)) > 1e-9:
                raise _fail(path, "必须是整数")
            num = int(round(num))
        lo, hi = field.get("min"), field.get("max")
        if lo is not None and num < lo:
            raise _fail(path, f"不能小于 {lo}")
        if hi is not None and num > hi:
            raise _fail(path, f"不能大于 {hi}")
        return num

    if kind in ("select",):
        val = str(raw).strip()
        allowed = [str(o[1] if isinstance(o, (list, tuple)) else o)
                   for o in (field.get("options") or [])]
        if val not in allowed:
            raise _fail(path, f"只能是 {allowed} 之一")
        return val

    # text / textarea / provider / model
    val = "" if raw is None else str(raw).strip()
    maxlen = int(field.get("maxlen") or 200)
    if len(val) > maxlen:
        raise _fail(path, f"不能超过 {maxlen} 个字符")
    return val


def apply(values: dict[str, Any],
          cfg_path: str | Path | None = None) -> dict[str, Any]:
    """校验并落盘。只接受 FIELDS 里声明过的键——白名单之外的一律丢弃。

    返回 {"saved": [...], "overridden": [...], "path": ...}；
    校验失败抛 ValueError，前端直接把 message 弹给用户。
    """
    if not isinstance(values, dict):
        raise ValueError("参数格式不对：需要 {字段路径: 值}")
    clean: dict[str, Any] = {}
    for path, raw in values.items():
        field = _FIELD_BY_PATH.get(path)
        if field is None:
            continue                         # 不认识的键静默丢弃，别写进文件
        clean[path] = _coerce(field, raw)

    current = read_overrides(cfg_path)
    for path, val in clean.items():
        _set_path(current, path, val)

    base = load_previsit(cfg_path, use_overrides=False)  # type: ignore[call-arg]
    pruned = _prune(current, base)
    p = _write_overrides(pruned, cfg_path)
    return {
        "saved": sorted(clean),
        "overridden": _flatten_paths(pruned),
        "path": str(p),
    }


def reset(cfg_path: str | Path | None = None) -> dict[str, Any]:
    """删掉整层覆盖，回到 previsit.yaml 的出厂值。"""
    p = _write_overrides({}, cfg_path)
    return {"saved": [], "overridden": [], "path": str(p)}


def _flatten_paths(cfg: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in cfg.items():
        full = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.extend(_flatten_paths(v, full + "."))
        else:
            out.append(full)
    return sorted(out)


# ---------------------------------------------------------------- 模型目录
def catalog_from_file() -> dict[str, Any]:
    """没有主项目时（独立部署），从 config/models.yaml 里读后端清单。

    models.yaml 只存地址和模型名，**不存密钥**——那份文件是可以进 git 的。
    所以独立部署的自定义端点还需要环境变量提供密钥，界面上会写这句话。
    """
    p = ROOT / "config" / "models.yaml"
    if not p.exists():
        return {"source": "env", "providers": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"source": "env", "providers": []}
    provs = data.get("providers") if isinstance(data, dict) else None
    out = []
    for name, item in (provs or {}).items():
        if not isinstance(item, dict):
            continue
        # 2026-11-04 起主项目把清单写成 `models:`（每条带 id/path/port）；
        # 老文件还叫 `model_list`，里面只有 id。两种都认，别让参数面板在
        # 新版配置下变成空的。
        raw = item.get("models")
        if raw is None:
            raw = item.get("model_list")
        models = [str(m.get("id")) for m in (raw or [])
                  if isinstance(m, dict) and m.get("id")]
        if item.get("model"):
            models.insert(0, str(item["model"]))
        out.append({
            "name": str(name),
            "label": str(item.get("kind") or name),
            "base_url": str(item.get("base_url") or ""),
            "model": str(item.get("model") or ""),
            "local": "127.0.0.1" in str(item.get("base_url") or "")
                     or "localhost" in str(item.get("base_url") or ""),
            "models": list(dict.fromkeys(models)),
            "ready": True,
        })
    source = "file" if out else "env"
    return {"source": source, "providers": out}


# ---------------------------------------------------------------- 描述给前端
def describe(catalog: dict[str, Any] | None = None,
             cfg_path: str | Path | None = None) -> dict[str, Any]:
    pv = load_previsit(cfg_path)
    base = load_previsit(cfg_path, use_overrides=False)  # type: ignore[call-arg]
    over = read_overrides(cfg_path)

    fields = []
    for f in FIELDS:
        item = {k: v for k, v in f.items() if k not in ("options",)}
        item["value"] = get_path(pv, f["path"])
        item["default"] = get_path(base, f["path"])
        item["overridden"] = get_path(over, f["path"], None) is not None
        if f.get("options"):
            item["options"] = [{"label": str(o[0]), "value": str(o[1])}
                               if isinstance(o, (list, tuple)) else {"label": str(o), "value": str(o)}
                               for o in f["options"]]
        fields.append(item)

    return {
        "groups": GROUPS,
        "fields": fields,
        "values": {f["path"]: get_path(pv, f["path"]) for f in FIELDS},
        "defaults": {f["path"]: get_path(base, f["path"]) for f in FIELDS},
        "overridden": _flatten_paths(over),
        "overrides_path": str(overrides_path(cfg_path)),
        "config_path": str(Path(cfg_path) if cfg_path else DEFAULT_PATH),
        "catalog": catalog or catalog_from_file(),
    }
