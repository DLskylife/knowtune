"""知识库级参数覆盖 —— 每个库一套自己的检索口径。

为什么需要它：同一个系统里，"合同条款库"要的是精确匹配 + 少而准的证据，
"技术文档库"要的是语义召回 + 更多上下文。全局参数只能二选一，用户于是被迫
在"调这个库伤那个库"之间来回改。覆盖层让每个库记住自己的偏好。

存储：`kb.config_json` 存**一整套完整值**（不是"与全局不同的项"）。

这是从差异存储改过来的，原因是一次真实的可用性事故：差异存储要求界面上每行都
回答"这一项跟不跟全局"，于是每行多出一个「覆盖」开关，7 组 24 项就是 24 个
开关 + 24 个三态下拉，页面长到没法扫，改一个数要点两下。而它换来的是"全局改了
这个值，本库跟着变"——实际使用中这个特性更常带来困惑：改了全局发现某个库没变，
或者反过来。现在**建库时按预设复制一套独立值，之后全局与本库互不影响**，界面
上不再有"覆盖/跟随"这个概念。

迁移：老库存的是差异，读取时用当前全局值补齐并写回（见 `load_full`）。

哪些参数允许按库覆盖：只有"同一个问题在不同库里答案不同"的那些——分块、
召回宽度、融合权重、精排条数、拒答阈值、生成后端与温度、历史轮数。

嵌入模型**故意不在内**。向量矩阵按 `模型@维度` 建（见 vectors.py 的 join 条件），
整个进程只驻留一个 embedder；允许按库换模型就得同时载多份权重、让向量检索
按库分表，还得给每个库单独标定拒答阈值。代价远大于收益，所以 UI 上如实写
"跟随系统"，而不是给一个点了会出事的选项。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import settings as settings_mod
from .config import Config

# 允许按库覆盖的参数。顺序决定 UI 上的分组顺序。
KB_PARAM_KEYS: tuple[str, ...] = (
    "chunking.strategy",
    "chunking.size",
    "chunking.overlap",
    "chunking.min_chunk",
    "chunking.respect_headings",
    "retrieval.dense.top_k",
    "retrieval.sparse.top_k",
    "retrieval.fusion.method",
    "retrieval.fusion.k",
    "retrieval.fusion.w_dense",
    "retrieval.fusion.w_sparse",
    "retrieval.fusion.top_k",
    "retrieval.rerank.enabled",
    "retrieval.rerank.model",
    "retrieval.rerank.top_k",
    "retrieval.rerank.timeout_s",
    "retrieval.expand.enabled",
    "retrieval.mmr.enabled",
    "retrieval.mmr.lambda_",
    "refusal.enabled",
    "refusal.min_score",
    "refusal.min_margin",
    "refusal.soft_margin",
    "evidence.mid_ratio",
    "generation.provider",
    "generation.temperature",
    "chat.history_turns",
    "chat.history_token_budget",
)

KB_PARAMS = tuple(settings_mod.PARAM_MAP[k] for k in KB_PARAM_KEYS)
KB_PARAM_MAP = {p.path: p for p in KB_PARAMS}

# 建库向导里的三套预设。预设**不碰**拒答阈值：那是"这个嵌入模型的余弦分布"
# 的属性，要标定得到，塞一个手写数字进去只会让拒答要么虚设要么乱拒。
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "general",
        "label": "通用问答",
        "desc": "语义与关键词各一半，适合大多数中文资料。",
        "values": {
            "chunking.strategy": "size",
            "chunking.size": 480,
            "chunking.overlap": 80,
            "retrieval.fusion.w_dense": 0.5,
            "retrieval.fusion.w_sparse": 0.35,
            "retrieval.rerank.top_k": 5,
        },
    },
    {
        "id": "code",
        "label": "代码 / 故障码",
        "desc": "函数名、错误码要原样命中，块切得更碎、BM25 权重更高。",
        "values": {
            "chunking.strategy": "paragraph",
            "chunking.size": 400,
            "chunking.overlap": 60,
            "retrieval.fusion.w_dense": 0.4,
            "retrieval.fusion.w_sparse": 0.6,
            "retrieval.sparse.top_k": 80,
            "retrieval.rerank.top_k": 6,
        },
    },
    {
        "id": "precise",
        "label": "合同 / 条款",
        "desc": "块更小便于逐条引用，候选放宽、最终只留 4 条。",
        "values": {
            "chunking.strategy": "heading",
            "chunking.size": 320,
            "chunking.overlap": 40,
            "chunking.min_chunk": 60,
            "retrieval.fusion.top_k": 30,
            "retrieval.rerank.top_k": 4,
        },
    },
    {
        "id": "blank",
        "label": "完全跟随全局",
        "desc": "不覆盖任何参数，全局改什么这个库就跟着变。",
        "values": {},
    },
)
PRESET_MAP = {p["id"]: p for p in PRESETS}


def preset_values(preset_id: str) -> dict[str, Any]:
    preset = PRESET_MAP.get(preset_id)
    return dict(preset["values"]) if preset else {}


# ------------------------------------------------------------------- 完整值集


FULL_MARK = "__full__"      # 存在 config_json 里标记"这是全量而非差异"


def baseline(cfg: Config) -> dict[str, Any]:
    """当前全局值的一份拷贝 —— 建库、重置、迁移老库都从这里起步。"""
    return {p.path: settings_mod.read_value(cfg, p.path) for p in KB_PARAMS}


def materialize(cfg: Config, values: dict[str, Any] | None = None) -> dict[str, Any]:
    """全局值 + 预设/用户值 → 这个库的一整套参数。"""
    out = baseline(cfg)
    for key, raw in (values or {}).items():
        if key in KB_PARAM_MAP:
            out[key] = raw
    return normalize(cfg, out)


# --------------------------------------------------------------------- 校验


def normalize(cfg: Config, values: dict[str, Any]) -> dict[str, Any]:
    """校验并规整**一组完整的**覆盖值。

    与参数面板共用 `settings.coerce`：边界、枚举、类型只有一份定义。分开写两份
    必然漂移，而漂移的表现是"面板里能调、建库时被拒"这种没法解释的现象。

    跨字段校验针对"这个库最终会生效的配置"，不是全局配置：一个把块长设成 360
    的库，重叠 400 应该被拒，而重叠 300 应该通过——拿全局的 480 去判就全错了。
    """
    if not values:
        return {}
    unknown = [k for k in values if k not in KB_PARAM_MAP]
    if unknown:
        raise ValueError(f"这些参数不支持按库覆盖：{', '.join(sorted(unknown))}")

    staged: dict[str, Any] = {}
    for key, raw in values.items():
        staged[key] = settings_mod.coerce(KB_PARAM_MAP[key], cfg, raw)

    probe = effective(cfg, staged)
    if int(probe.chunking.overlap) >= int(probe.chunking.size):
        raise ValueError(
            f"重叠长度（{probe.chunking.overlap}）必须小于分块长度"
            f"（{probe.chunking.size}）"
        )

    # 与 settings._cross_check 同一条规则，这里必须再写一遍：那边管的是全局
    # 参数，而这条要判的是"这个库最终生效的块长"。库级覆盖把块长设成 1000、
    # 全局仍是 480 时，只有这里拦得住。
    # 拦的是语义稀释不是截断：超窗部分由嵌入器切窗编码再平均，不会丢，但一块
    # 塞太多主题时向量会变成中点，跟哪个问题都不像。
    win = int(getattr(probe.embedding, "max_seq_length", 512) or 512)
    if int(probe.chunking.size) > win * 2:
        raise ValueError(
            f"分块长度（{probe.chunking.size}）超过嵌入窗口（{win} token）的两倍："
            f"一块里塞太多主题，向量会变成它们的中点。请调到 {win * 2} 以下。"
        )
    return staged


def effective(cfg: Config, overrides: dict[str, Any]) -> Config:
    """全局配置 + 覆盖值 → 一次请求要用的配置副本。

    必须是副本：cfg 被 embedder / reranker / engine / pipeline 共享，就地改它
    等于让 A 库的检索参数串到 B 库，而且这种 bug 只在并发时出现。
    """
    if not overrides:
        return cfg
    clone = cfg.model_copy(deep=True)
    for key, value in overrides.items():
        if key not in KB_PARAM_MAP:
            continue
        # 全量快照里常年存着一个空的 "generation.provider": ""。它不是用户
        # "特意不要生成后端"，而是参数面板保存时留下的空字段。真按空串覆盖，
        # 状态栏就会亮一颗写着「未选择」的红点，而问答其实走的是全局后端
        # （2026-11-06 截图里 28020 chunk / bge-reranker-base 那张卡就是它）。
        # 空串一律视为"跟随全局"，只有填了名字才算显式指定。
        if key == "generation.provider" and not str(value or "").strip():
            continue
        try:
            settings_mod.write_value(clone, key, value)
        except AttributeError:
            # 覆盖值来自旧版本的配置文件时必须能启动，不能因为一个过期字段炸整个库
            continue
    return clone


# --------------------------------------------------------------------- 读写


def parse(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(conn: sqlite3.Connection, kb_id: int) -> dict[str, Any]:
    """覆盖值（剥掉全量标记）。给 `effective()` 用，不关心存的是哪种格式。"""
    row = conn.execute("SELECT config_json FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        return {}
    data = parse(row["config_json"])
    data.pop(FULL_MARK, None)
    return data


def save(conn: sqlite3.Connection, kb_id: int, overrides: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(overrides, ensure_ascii=False, sort_keys=True) if overrides else None
    cur = conn.execute("UPDATE kb SET config_json=? WHERE id=?", (payload, kb_id))
    if cur.rowcount == 0:
        raise ValueError("知识库不存在")
    return overrides


def load_full(conn: sqlite3.Connection, kb_id: int, cfg: Config) -> dict[str, Any]:
    """读这个库的完整参数集。

    老库的 config_json 只存了差异（没有 `__full__` 标记）：用当前全局值补齐后
    **写回**，之后就是全量语义了。不写回的话每次读都要重新补一次，而用户
    改完保存时又会存成全量，两种格式混在同一次会话里迟早出事。
    """
    raw = parse(_raw_json(conn, kb_id))
    if raw.pop(FULL_MARK, None):
        return {k: v for k, v in raw.items() if k in KB_PARAM_MAP}
    merged = {**baseline(cfg), **{k: v for k, v in raw.items() if k in KB_PARAM_MAP}}
    try:
        merged = normalize(cfg, merged)
    except ValueError:
        # 老数据里可能有已下线或越界的值：宁可退回全局值，也不能让整个库打不开
        merged = baseline(cfg)
    save_full(conn, kb_id, merged)
    return merged


def _raw_json(conn: sqlite3.Connection, kb_id: int) -> str | None:
    row = conn.execute("SELECT config_json FROM kb WHERE id=?", (kb_id,)).fetchone()
    return row["config_json"] if row else None


def save_full(conn: sqlite3.Connection, kb_id: int, values: dict[str, Any]) -> dict[str, Any]:
    payload = {FULL_MARK: 1, **values}
    cur = conn.execute(
        "UPDATE kb SET config_json=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), kb_id),
    )
    if cur.rowcount == 0:
        raise ValueError("知识库不存在")
    return values


# --------------------------------------------------------------------- 快照


GROUP_ORDER = ("chunking", "recall", "fusion", "rerank", "refusal", "generation", "chat")


def snapshot(cfg: Config, values: dict[str, Any]) -> dict[str, Any]:
    """给 UI 的参数表：每行只给**这个库自己的值**。

    不再输出 global / override 两列："覆盖还是跟随"这个概念已经取消（见文件头）。
    界面上曾经靠这两列才能回答"我现在用的到底是几"，现在每个值就是答案。
    """
    clone = effective(cfg, values)
    groups: list[dict[str, Any]] = []
    for gid in GROUP_ORDER:
        items = []
        for p in KB_PARAMS:
            if p.group != gid:
                continue
            items.append({
                "key": p.path,
                "label": p.label,
                "kind": p.kind,
                "desc": p.desc,
                "lo": p.lo,
                "hi": p.hi,
                "step": p.step,
                "unit": p.unit,
                "options": settings_mod.options_of(p, clone),
                "effect": p.effect,
                "value": settings_mod.read_value(clone, p.path),
                # 档位 chip 里的「默认」：不再表示"跟随全局"，而是"系统默认值"
                # （当前全局值）。本库改乱了有个一键回去的落点。
                "default": settings_mod.read_value(cfg, p.path),
            })
        if items:
            label = dict((g[0], g[1]) for g in settings_mod.GROUPS).get(gid, gid)
            note = dict((g[0], g[2]) for g in settings_mod.GROUPS).get(gid, "")
            groups.append({"id": gid, "label": label, "note": note, "items": items})

    return {
        "groups": groups,
        "values": values,
        "embedding": {
            "model": cfg.embedding.model,
            "dim": cfg.embedding.dim,
            "note": "嵌入模型是系统级：按库换要整库重算，还得同时驻留多份权重。",
        },
    }


def needs_reindex(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """哪些改动必须重建索引才生效（目前只有分块）。

    返回改动的 key 列表，调用方据此决定是否自动重排——检索参数改了立刻生效，
    分块参数改了不重建就是"改了个寂寞"：库里还是旧块，用户却以为生效了。
    """
    return [
        k for k in KB_PARAM_KEYS
        if KB_PARAM_MAP[k].effect == "reindex" and before.get(k) != after.get(k)
    ]
