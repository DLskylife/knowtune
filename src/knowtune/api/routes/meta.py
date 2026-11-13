"""基础路由：首页、预问诊配置面板、服务状态、系统总览、全局参数面板。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ... import services as services_mod
from ... import settings as settings_mod
from ...config import Config, calibration_of, calibration_path, refusal_mode
from ...dashboard import build as build_dashboard
from ..common import STATIC_DIR
from ..state import state

router = APIRouter()


@router.get("/favicon.ico")
async def favicon() -> Response:
    # 浏览器会自动请求 favicon；没图标就回 204，避免控制台 404 噪音。
    return Response(status_code=204)


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


class PrevisitConfIn(BaseModel):
    # {字段路径: 值}，路径形如 "max_tokens" / "prompts.chat"。
    # 只认 previsit.settings.FIELDS 里声明过的键，其余静默丢弃——面板是白名单。
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/previsit/config")
def previsit_config() -> dict:
    from ...previsit.app import config_describe

    return config_describe()


@router.put("/api/previsit/config")
def previsit_config_save(req: PrevisitConfIn) -> dict:
    from ...previsit.app import config_save

    try:
        return config_save(req.values)
    except ValueError as exc:      # 越界 / 类型不对：告诉用户是哪一项
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/previsit/config/reset")
def previsit_config_reset() -> dict:
    from ...previsit.app import config_reset

    return config_reset()


def _refusal_note(cfg: Config, kb_id: int | None) -> str | None:
    """为什么当前是这个阈值。界面需要一句能读懂的话，而不是一个数字。"""
    if cfg.refusal.calibrated_at:
        return None
    if cfg.refusal.verdict == "invalid":
        return (f"标定完成，但 AUC {cfg.refusal.auc:.2f} 说明这个信号分不开"
                f"「有答案」和「没答案」，已停用该判据")
    # 带了 kb_id 就只谈这个库。旧的全局标定文件（22 条样本、reliable:false）
    # 跟按库标定是两套东西，混进来会让"本库没标定"被误报成"标定样本不足"。
    if kb_id:
        if calibration_path(cfg, kb_id).exists():
            return "标定文件是另一个嵌入模型做的，阈值不通用，需重跑标定"
        return "本库尚未标定，阈值是出厂值"
    if calibration_of(cfg) is not None:
        return "全局标定样本不足，已回退出厂阈值"
    return "尚未标定，使用出厂阈值"


@router.get("/api/status")
def status(kb_id: int | None = None) -> dict:
    conn = state.conn
    # 给了 kb_id 就报该库的生效值：标定是按库做的，拿全局配置回答
    # "标定过没有"会答错一整类问题（A 库标过、B 库没标）。
    cfg = state.effective_cfg(kb_id) if kb_id else state.cfg
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM kb) AS kbs,"
        " (SELECT count(*) FROM document) AS docs,"
        " (SELECT count(*) FROM document WHERE status='ready') AS ready,"
        # 只数活跃代：重索引会保留上一代用于回滚（storage.keep_generations），
        # 不带这个条件，界面上的块数会凭空翻倍，用户以为索引写重了
        " (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id"
        "   WHERE c.generation=d.active_gen) AS chunks"
    ).fetchone()
    # 只读"加载完了没"，绝不在这里触发加载（见 Embedder.is_loaded 的注释）
    dim_ok = state.embedder.is_loaded
    return {
        "kbs": counts["kbs"],
        "docs": counts["docs"],
        "ready_docs": counts["ready"],
        "chunks": counts["chunks"],
        "embedder": {
            "model": cfg.embedding.model,
            # 标定文件按「模型@维度」认亲，换模型后旧阈值一律作废。把指纹直接
            # 给出去，标定脚本就不用自己拼（拼错了会写出一个永远匹配不上的文件）
            "fingerprint": f"{cfg.embedding.model}@{cfg.embedding.dim}",
            "available": bool(dim_ok),
            "error": state.embedder.error,
            "warmup": {
                "state": state.warm["state"],          # pending | loading | ready | failed
                "took_ms": state.warm["took_ms"],
                "elapsed_ms": int((time.time() - state.warm["started_at"]) * 1000)
                if state.warm["state"] in ("pending", "loading") else None,
                "error": state.warm["error"],
            },
        },
        "vectors": state.vectors.stats,
        "providers": {
            "active": cfg.generation.provider,
            "available": {
                name: {"model": p.model, "base_url": p.base_url}
                for name, p in cfg.generation.providers.items()
            },
        },
        # 库参数里写的后端名可能压根不存在（参数面板允许先填名字、后加端点）。
        # 那一步不会报错，报错发生在用户提问的那一刻——所以这里提前说出来。
        "generation": {
            "provider": cfg.generation.provider,
            "configured": bool(cfg.generation.provider)
            and cfg.generation.provider in cfg.generation.providers,
            "known": sorted(cfg.generation.providers),
            # 库参数里没填后端名时跟随全局。界面要说清这一点，否则用户看到一个
            # 自己没选过的名字会以为串库了；反过来，空字段也不该亮红点说"未选择"。
            "follows_global": bool(kb_id) and not str(
                (state.overrides(kb_id) or {}).get("generation.provider") or ""
            ).strip(),
        },
        "rerank": {
            "enabled": cfg.retrieval.rerank.enabled,
            # 全局值也报一份：库参数是独立快照，"全局开着、本库关着"是常态，
            # 只给本库值的话，界面说"已关闭"却查不到是在哪儿关的。
            "global_enabled": state.cfg.retrieval.rerank.enabled,
            "default_model": cfg.retrieval.rerank.model,
            "last_error": state.reranker.last_error,
            "max_length": cfg.retrieval.rerank.max_length,
            "candidates": cfg.retrieval.fusion.top_k,
        },
        "refusal_calibrated": bool(cfg.refusal.calibrated_at),
        "refusal": {
            "min_score": cfg.refusal.min_score,
            "min_margin": cfg.refusal.min_margin,
            "calibrated_at": cfg.refusal.calibrated_at,
            "samples_ok": cfg.refusal.calibrated_samples_ok,
            # off / calibrated / advisory / invalid。前端据此决定那颗 pill 显示
            # 阈值、还是直接说「无有效判定」——显示一个永不触发的数字比不显示更误导。
            "mode": refusal_mode(cfg),
            # AUC 是比阈值更根本的东西：阈值可以调，信号有没有区分力不能调。
            # 界面写「AUC 0.85 · 65 样本」比写「阈值 0.62」诚实得多。
            "auc": cfg.refusal.auc,
            "verdict": cfg.refusal.verdict,
            "n_samples": cfg.refusal.n_samples,
            # 有标定文件但样本不足时，界面上要说清"为什么现在用的是出厂阈值"，
            # 否则用户只会看到阈值和他上次标定的数字不一样，以为程序没保存。
            "note": _refusal_note(cfg, kb_id),
        },
        "support": {
            "enabled": cfg.support.enabled,
            "threshold": cfg.support.threshold,
            "weak_ratio": cfg.support.weak_ratio,
            # 语义层：句子和它引用的块是不是同一件事。这一层能标定，也确实分得开。
            "auc": cfg.support.auc,
            # 事实层：块没变、句子里的事实被换掉。余弦对这事近乎无感，
            # 所以这个数天生接近 0.5——它不是缺陷报告，是分工说明。
            "auc_fact": cfg.support.auc_fact,
            "anchor_catch": cfg.support.anchor_catch,
            "verdict": cfg.support.verdict,
            "n_samples": cfg.support.n_samples,
            "calibrated": bool(cfg.support.auc is not None),
        },
        "uptime_s": int(time.time() - state.started_at),
        "params": {
            "chunk_size": cfg.chunking.size,
            "chunk_overlap": cfg.chunking.overlap,
            "dense_top_k": cfg.retrieval.dense.top_k,
            "sparse_top_k": cfg.retrieval.sparse.top_k,
            "rrf_k": cfg.retrieval.fusion.k,
            "fusion_method": cfg.retrieval.fusion.method,
            "w_dense": cfg.retrieval.fusion.w_dense,
            "w_sparse": cfg.retrieval.fusion.w_sparse,
            "rerank_top_k": cfg.retrieval.rerank.top_k,
        },
    }


@router.get("/api/dashboard")
def dashboard(request: Request) -> dict:
    """首页一次拿全。服务状态只读缓存，不在这里发网络请求。"""
    return build_dashboard(
        state.cfg, state.conn, started_at=state.started_at, port=request.url.port
    )


@router.get("/api/settings")
def get_settings() -> dict:
    return settings_mod.snapshot(state.cfg)


class SettingsPatch(BaseModel):
    values: dict[str, object] = Field(min_length=1)
    model_config = {"extra": "forbid"}


@router.patch("/api/settings")
def patch_settings(req: SettingsPatch) -> dict:
    try:
        applied = settings_mod.apply(state.cfg, req.values)
    except ValueError as exc:
        # 参数越界是用户输入问题，不是服务器故障，必须回 400 带人话
        raise HTTPException(400, str(exc)) from exc
    snap = settings_mod.snapshot(state.cfg)
    # 全局参数一变，所有库的"生效配置副本"都过期了（阈值、权重都可能是继承来的）
    state.invalidate_kb_cfg()
    return {
        "applied": applied,
        "modified": snap["modified"],
        "unsaved": snap["unsaved"],
        "refusal": {
            "min_score": state.cfg.refusal.min_score,
            "min_margin": state.cfg.refusal.min_margin,
            "calibrated_at": state.cfg.refusal.calibrated_at,
        },
    }


@router.post("/api/settings/save")
def save_settings() -> dict:
    result = settings_mod.save_local(state.cfg)
    if result["removed"]:
        result["message"] = "所有参数都等于出厂值，已删除 local.yaml"
    else:
        result["message"] = f"已写入 {len(result['written'])} 项到 config/local.yaml"
    return result


@router.post("/api/settings/reset")
def reset_settings() -> dict:
    result = settings_mod.reset(state.cfg)
    state.invalidate_kb_cfg()
    result["message"] = (
        f"已恢复 {len(result['restored'])} 项默认值（尚未保存）"
        if result["restored"] else "当前已经是默认值"
    )
    return result


@router.post("/api/settings/reload")
def reload_settings() -> dict:
    result = settings_mod.reload_from_disk(state.cfg)
    state.invalidate_kb_cfg()
    services_mod.invalidate()
    result["message"] = (
        f"已从磁盘重读，{len(result['reloaded'])} 项变化"
        if result["reloaded"] else "磁盘配置与内存一致"
    )
    return result