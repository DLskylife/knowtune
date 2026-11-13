"""知识库路由：建库/删库/改库、参数面板、文档列表。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import kbconfig, logbus
from ...config import calibration_of_kb
from ...db import Database
from ...ingest.cleaner import plan_from_dict
from ..common import _source_missing
from .docs import _reindex_kb
from ..state import state

router = APIRouter()


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    # 建库向导：可以是预设 id，也可以直接给一组覆盖值（向导里的滑杆）
    preset: str | None = None
    config: dict[str, object] | None = None


class KbPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None


class KbConfigPatch(BaseModel):
    values: dict[str, object] = Field(default_factory=dict)
    # replace=True 时整组替换（前端"保存"用），False 时只合并改动的那几项
    replace: bool = False


@router.get("/api/kb")
def list_kb() -> list[dict]:
    rows = state.conn.execute(
        """
        SELECT k.id, k.name, k.description, k.created_at, k.config_json,
               (SELECT count(*) FROM document d WHERE d.kb_id=k.id) AS docs,
               (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
                 WHERE d.kb_id=k.id AND c.generation=d.active_gen) AS chunks
        FROM kb k ORDER BY k.id
        """
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        raw = kbconfig.parse(item.pop("config_json"))
        raw.pop(kbconfig.FULL_MARK, None)
        baseline = kbconfig.baseline(state.cfg)
        item["tuned"] = sum(1 for k, v in raw.items() if baseline.get(k) != v)
        out.append(item)
    return out


@router.get("/api/kb/presets")
def kb_presets() -> list[dict]:
    """建库向导用的预设。放在 /api/kb/{kb_id} 之前声明，避免被路径参数吃掉。"""
    return [
        {"id": p["id"], "label": p["label"], "desc": p["desc"], "values": p["values"]}
        for p in kbconfig.PRESETS
    ]


@router.post("/api/kb")
def create_kb(req: KbCreate) -> dict:
    """建库可以直接带参数。

    先校验再插入：建完发现"重叠比块长还大"再把库删掉重来，是让用户替我们
    收拾烂摊子。预设只提供起点，向导里改过的值优先。
    """
    values = kbconfig.preset_values(req.preset) if req.preset else {}
    if req.preset and not values and req.preset not in {p["id"] for p in kbconfig.PRESETS}:
        raise HTTPException(400, f"未知预设：{req.preset}")
    if req.config:
        values.update(req.config)
    try:
        # 建库时就把一整套值固化下来：之后全局与本库互不影响，"覆盖/跟随"这套
        # 概念不再出现在界面上。预设只提供起点，向导里改过的值优先。
        overrides = kbconfig.materialize(state.cfg, values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    conn = state.conn
    try:
        cur = conn.execute(
            "INSERT INTO kb (name, description, config_json) VALUES (?,?,?)",
            (
                req.name.strip(),
                req.description.strip(),
                json.dumps({kbconfig.FULL_MARK: 1, **overrides},
                           ensure_ascii=False, sort_keys=True),
            ),
        )
    except Exception as exc:
        raise HTTPException(400, f"创建失败（名称可能重复）：{exc}") from exc
    kb_id = int(cur.lastrowid)
    logbus.note(
        f"创建知识库「{req.name}」#{kb_id}"
        + (f"，预设 {req.preset}" if req.preset else "，默认参数"),
        source="kb",
    )
    return {"id": kb_id, "name": req.name, "overrides": overrides}


@router.delete("/api/kb/{kb_id}")
def delete_kb(kb_id: int) -> dict:
    """删库。级联删除由外键负责（document/chunk 都带 ON DELETE CASCADE），
    但两件事它管不到，必须自己动手：

    1. chunk_fts 是独立虚表，chunk 的级联删不到它 —— 不管就留下一批搜得到、
       却指向已消失 chunk 的幽灵行（doc 删除走的是同一条规则）。
    2. 会话按设计保留（session.kb_id 是 ON DELETE SET NULL）：库没了，历史
       问答本身还有价值，只是解除绑定。这一点要在确认框里说清楚，不能偷偷删。

    返回值带上删掉了多少，好让确认框和完成提示报的是实数，而不是"已删除"三个字。
    """
    conn = state.conn
    kb = conn.execute("SELECT id, name FROM kb WHERE id=?", (kb_id,)).fetchone()
    if kb is None:
        raise HTTPException(404, "知识库不存在")
    docs = conn.execute(
        "SELECT COUNT(*) AS c FROM document WHERE kb_id=?", (kb_id,)).fetchone()["c"]
    chunks = conn.execute(
        "SELECT COUNT(*) AS c FROM chunk WHERE doc_id IN"
        " (SELECT id FROM document WHERE kb_id=?)", (kb_id,)).fetchone()["c"]
    sessions = conn.execute(
        "SELECT COUNT(*) AS c FROM session WHERE kb_id=?", (kb_id,)).fetchone()["c"]

    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunk WHERE doc_id IN (SELECT id FROM document WHERE kb_id=?)",
        (kb_id,))]
    for i in range(0, len(ids), 800):
        state.sparse.delete(conn, ids[i:i + 800])

    with Database.tx(conn):
        conn.execute("DELETE FROM kb WHERE id = ?", (kb_id,))
    state.vectors.invalidate()
    state.invalidate_kb_cfg(kb_id)
    logbus.note(
        f"删除知识库「{kb['name']}」#{kb_id}（{docs} 篇文档 / {chunks} 个 chunk"
        f" / {sessions} 条会话解除绑定）", level="WARNING", source="kb",
    )
    return {"deleted": kb_id, "name": kb["name"],
            "docs": docs, "chunks": chunks, "sessions": sessions}


@router.patch("/api/kb/{kb_id}")
def patch_kb(kb_id: int, req: KbPatch) -> dict:
    sets, params = [], []
    if req.name is not None:
        sets.append("name=?")
        params.append(req.name.strip())
    if req.description is not None:
        sets.append("description=?")
        params.append(req.description.strip())
    if not sets:
        raise HTTPException(400, "没有要改的字段")
    params.append(kb_id)
    try:
        cur = state.conn.execute(f"UPDATE kb SET {', '.join(sets)} WHERE id=?", params)
    except Exception as exc:
        raise HTTPException(400, f"修改失败（名称可能重复）：{exc}") from exc
    if cur.rowcount == 0:
        raise HTTPException(404, "知识库不存在")
    return {"ok": True, "id": kb_id}


@router.get("/api/kb/{kb_id}/detail")
def kb_detail(kb_id: int) -> dict:
    """知识库详情页一次的取数：基本信息 + 内容盘点 + 参数覆盖数。

    平均块长与向量覆盖率放在这里，是因为它们最能说明"这个库的参数是否合适"：
    平均 480 字说明分块参数按预期生效，平均 90 字多半是被标题切碎了。
    """
    conn = state.conn
    row = conn.execute(
        "SELECT id, name, description, created_at, config_json FROM kb WHERE id=?", (kb_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")

    # 全量存储之后 config_json 里每一项都有值，"覆盖了几项"必须改成
    # "与系统默认值不同的有几项"——直接数字典长度会永远显示 25。
    values = kbconfig.load_full(state.conn, kb_id, state.cfg)
    baseline = kbconfig.baseline(state.cfg)
    tuned = {k: v for k, v in values.items() if baseline.get(k) != v}
    model_key = f"{state.cfg.embedding.model}@{state.cfg.embedding.dim}"
    stats = conn.execute(
        """
        SELECT
          (SELECT count(*) FROM document WHERE kb_id=?) AS docs,
          (SELECT count(*) FROM document WHERE kb_id=? AND status='ready') AS docs_ready,
          (SELECT count(*) FROM document WHERE kb_id=? AND status='failed') AS docs_failed,
          (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
            WHERE d.kb_id=? AND c.generation=d.active_gen) AS chunks,
          (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
            WHERE d.kb_id=? AND c.generation=d.active_gen AND c.is_hidden=1) AS hidden_chunks,
          (SELECT COALESCE(round(avg(c.token_count)),0) FROM chunk c
            JOIN document d ON d.id=c.doc_id
            WHERE d.kb_id=? AND c.generation=d.active_gen) AS avg_tokens,
          (SELECT COALESCE(max(c.token_count),0) FROM chunk c
            JOIN document d ON d.id=c.doc_id
            WHERE d.kb_id=? AND c.generation=d.active_gen) AS max_tokens,
          (SELECT COALESCE(sum(c.token_count),0) FROM chunk c
            JOIN document d ON d.id=c.doc_id
            WHERE d.kb_id=? AND c.generation=d.active_gen) AS total_tokens,
          (SELECT count(*) FROM session WHERE kb_id=?) AS sessions,
          (SELECT max(updated_at) FROM document WHERE kb_id=?) AS last_import,
          (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
             JOIN embedding e ON e.content_hash=c.content_hash AND e.model=?
            WHERE d.kb_id=? AND c.generation=d.active_gen) AS embedded
        """,
        (kb_id,) * 10 + (model_key, kb_id),
    ).fetchone()

    sources = [
        dict(r)
        for r in conn.execute(
            "SELECT source_type, count(*) AS n FROM document WHERE kb_id=? GROUP BY source_type",
            (kb_id,),
        ).fetchall()
    ]

    return {
        "kb": {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "created_at": row["created_at"],
        },
        "stats": dict(stats),
        "sources": sources,
        "tuned": tuned,
        "tuned_count": len(tuned),
        # 旧字段保留：还有别处按名字取，语义从"覆盖项"变成"与默认值不同的项"
        "overrides": tuned,
        "override_count": len(tuned),
        "embedding": {"model": state.cfg.embedding.model, "dim": state.cfg.embedding.dim},
    }


@router.get("/api/kb/{kb_id}/config")
def kb_config(kb_id: int) -> dict:
    row = state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")
    data = kbconfig.snapshot(
        state.cfg, kbconfig.load_full(state.conn, kb_id, state.cfg)
    )
    # 面板里显示的是"这个库存的参数值"，而标定是叠在上面的另一层：存的可能是
    # 出厂 0.35，实际跑的是标定出来的 0.692。两个数不一样却都不显示，用户
    # 打开面板只会更困惑。所以把"实际生效值"作为独立标记挂上去——参数说明是
    # 单行省略的，把这段统计拼进 desc 只会被截成半句，等于没写。
    calib = calibration_of_kb(state.cfg, kb_id)
    if calib:
        ref = calib.get("refusal") or {}
        sup = calib.get("support") or {}
        notes = {}
        if ref.get("min_score") is not None and ref.get("verdict") != "invalid":
            notes["refusal.min_score"] = {
                "value": ref["min_score"],
                "detail": (
                    f"AUC {ref.get('auc')} · {ref.get('n_samples')} 组探针 · "
                    f"误拒 {ref.get('false_refusal_rate', 0):.1%} / "
                    f"漏拒 {ref.get('missed_refusal_rate', 0):.1%}。"
                    "改动这里的值会覆盖标定结果。"),
            }
        if sup.get("threshold") is not None and sup.get("verdict") != "invalid":
            notes["support.threshold"] = {
                "value": sup["threshold"],
                "detail": (
                    f"AUC {sup.get('auc')} · 事实层 AUC {sup.get('auc_fact')}"
                    "（那一层靠锚点兜）。改动这里的值会覆盖标定结果。"),
            }
        marked = set()
        for g in data.get("groups", []):
            for it in g.get("items", []):
                extra = notes.get(it.get("key"))
                if extra:
                    it["calib"] = extra
                    it["calibrated"] = True
                    marked.add(it["key"])
        data["calibrated_keys"] = sorted(marked)
    return data


@router.patch("/api/kb/{kb_id}/config")
def patch_kb_config(kb_id: int, req: KbConfigPatch) -> dict:
    row = state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")

    current = kbconfig.load_full(state.conn, kb_id, state.cfg)
    merged = dict(req.values) if req.replace else {**current, **req.values}
    # null 表示"这一项没传"，不是"清空"；缺失的键沿用库里已有的值
    merged = {k: v for k, v in merged.items() if v is not None and k in kbconfig.KB_PARAM_MAP}
    merged = {**current, **merged}

    try:
        values = kbconfig.normalize(state.cfg, merged)
        kbconfig.save_full(state.conn, kb_id, values)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    state.invalidate_kb_cfg(kb_id)

    # 分块参数改了必须重建索引才生效。不自动做的话，用户改完立刻提问，拿到的
    # 还是旧块的结果，只会以为"改了没用"。
    changed = [k for k in kbconfig.KB_PARAM_KEYS if current.get(k) != values.get(k)]
    rebuild = kbconfig.needs_reindex(current, values)
    task = _reindex_kb(kb_id) if rebuild else None

    logbus.note(
        f"知识库 #{kb_id} 参数更新：{len(changed)} 项"
        + (f"，已自动重建索引（{len(rebuild)} 项分块参数变更）" if rebuild else ""),
        source="kb",
    )
    return {
        "ok": True,
        "changed": changed,
        "reindex": {
            "task_id": task["task_id"] if task else None,
            "docs": task["docs"] if task else 0,
            "keys": rebuild,
        },
        "snapshot": kbconfig.snapshot(state.cfg, values),
    }


@router.delete("/api/kb/{kb_id}/config")
def reset_kb_config(kb_id: int) -> dict:
    """恢复为系统默认 —— 即"当前全局值的一份拷贝"。

    不是"清空覆盖回到跟随全局"：本库持有独立一套值之后就不存在跟随关系了，
    重置的语义只能是"照着现在的默认值重新抄一份"。
    """
    row = state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")
    current = kbconfig.load_full(state.conn, kb_id, state.cfg)
    values = kbconfig.materialize(state.cfg)
    kbconfig.save_full(state.conn, kb_id, values)
    state.invalidate_kb_cfg(kb_id)
    rebuild = kbconfig.needs_reindex(current, values)
    task = _reindex_kb(kb_id) if rebuild else None
    logbus.note(f"知识库 #{kb_id} 参数已恢复为系统默认", source="kb")
    return {
        "ok": True,
        "reindex": {"task_id": task["task_id"] if task else None,
                    "docs": task["docs"] if task else 0, "keys": rebuild},
        "snapshot": kbconfig.snapshot(state.cfg, values),
    }


@router.get("/api/kb/{kb_id}/docs")
def list_docs(kb_id: int) -> list[dict]:
    rows = state.conn.execute(
        """
        SELECT id, source_type, source_uri, title, status, fail_reason,
               n_chunks, active_gen, imported_at, updated_at, extractor, clean_json
        FROM document WHERE kb_id = ? ORDER BY updated_at DESC
        """,
        (kb_id,),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        # 前端靠这个标记决定"重索引"还是"重新上传"：让用户点了才失败是很差的体验
        d["missing_source"] = _source_missing(d["source_type"], d["source_uri"] or "")
        d["stale_extractor"] = _stale_extractor(d["extractor"], d["source_uri"] or "")
        # 洗过的文档要能一眼看出来（"这篇只有源文件的 87%"），否则以后对着
        # 检索结果怀疑"是不是没导全"时，没人记得当初开过清洗。
        d["clean"] = _clean_summary(d.pop("clean_json", None))
        items.append(d)
    return items


def _clean_summary(raw: str | None) -> dict | None:
    """文档行上的一小段清洗摘要。解析不出来就当没洗过，别让脏 JSON 拖垮列表。"""
    if not raw:
        return None
    try:
        plan = plan_from_dict(json.loads(raw))
    except Exception:
        return None
    return {"rules": plan.active_ids(), "count": len(plan.active_ids())}


# 抽取器升级后旧文档的内容是"当时那版抽取器"的快照，不重索引就永远缺内容。
# 与其等用户偶然发现"某段话搜不到"，不如在文档卡片上明说。
_STALE_EXTRACTORS = {"python-docx": {".docx"}}


def _stale_extractor(extractor: str | None, source_uri: str) -> bool:
    if not extractor:
        return False
    exts = _STALE_EXTRACTORS.get((extractor or "").strip())
    if not exts:
        return False
    return Path(source_uri).suffix.lower() in exts


@router.post("/api/kb/{kb_id}/ensure")
def ensure_kb(kb_id: int) -> dict:
    row = state.conn.execute("SELECT id, name FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")
    return dict(row)