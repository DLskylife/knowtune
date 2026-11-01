"""HTTP 接口 + 静态页面。

S1 范围：知识库/文档管理、导入（路径/上传/粘贴）、检索调试、带引用问答（SSE）、
chunk 回查、请求日志。不做多用户、不做鉴权（明确不在第一阶段范围）。
"""

from __future__ import annotations

import json
import hashlib
import os
import queue as _queue
import re
import shutil
import threading
import time
import uuid
from typing import Any
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Config, ProviderCfg, calibration_of, load_config
from ..db import Database
from ..generate import cite as cite_mod
from ..generate import llm as llm_mod
from ..generate.llm import LLMClient, LLMError
from ..generate.prompt import REFUSE_TEMPLATE, build_context, build_direct
from ..index.embedder import Embedder
from ..index.sparse import SparseIndex
from ..index.vectors import VectorStore
from ..frontsite import instcfg
from ..frontsite.runner import InstanceManager
from ..ingest import loader as loader_mod
from ..ingest.chunker import repair_chunk_primitives
from ..ingest.cleaner import clean_loaded, plan_from_dict, rules_payload
from ..ingest.pipeline import IngestPipeline, display_title
from ..retrieve.engine import RetrievalEngine, RetrievalResult
from ..retrieve.rerank import Reranker
from .. import chatctx, kbconfig, logbus, modelcfg, promptcfg
from .. import paths as paths_mod
from .. import services as services_mod
from .. import settings as settings_mod
from .. import secrets as secrets_mod
from ..dashboard import build as build_dashboard

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = Path(__file__).resolve().parents[3] / "data" / "uploads"

# 把 root / uvicorn 的日志分流一份进内存环形缓冲区，首页「系统日志」直接读它。
logbus.install()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_missing(source_type: str, source_uri: str) -> bool:
    """文件来源的原始文件是否还在（判定逻辑见 ..paths）。"""
    return paths_mod.source_missing(source_type, source_uri)


def _inside(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（防 `../` 之类写到别处）。"""
    return paths_mod.inside(path, root)


_UPLOAD_CHUNK = 1 << 20  # 1MiB，够大不碎、够小不吃内存


async def _save_upload(file: UploadFile, target: Path, limit_mb: int) -> int:
    """把上传边读边写，返回字节数。

    不用 `await file.read()` 一次吃进内存：那样 `max_file_mb` 拦的是"读完
    之后的拒绝"，不是"不读超过限制的部分"。一个 5GB 的请求进来，进程会先
    把 5GB 装进内存再回 413——限制形同虚设，而且这种请求本身就能把机器打趴。

    两道闸（任一命中就拒）：
      1. `file.size`（starlette 从 Content-Length 或已缓冲部分算出来的）；
      2. 逐块累加计数。
    第二道是必须的：分块传输（chunked）没有 Content-Length，`size` 为 None，
    只靠第一道等于对这类请求完全不设防。

    超限时**删掉半截文件**再抛错：uploads 里留一个残缺副本比不留更糟，后续
    "补原始文件 / 重索引"会把它当成有效来源，导进来一堆截断的内容。

    写的是 `*.part` 临时文件、成功后才 `os.replace` 成目标名。这不只是洁癖：
    "补原始文件"会覆盖已有的落盘副本，如果直接往目标文件写、写到一半断了，
    旧副本就没了，而这个文档的重索引全靠它——一次失败的上传顺手毁掉一份
    本来完好的原始文件，是最难排查的那类损坏。
    """
    limit = max(1, int(limit_mb)) * 1024 * 1024
    declared = getattr(file, "size", None)
    if declared is not None and int(declared) > limit:
        raise HTTPException(413, f"文件超过 {limit_mb}MB")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    written = 0
    try:
        with tmp.open("wb") as fh:
            while True:
                block = await file.read(_UPLOAD_CHUNK)
                if not block:
                    break
                written += len(block)
                if written > limit:
                    raise HTTPException(413, f"文件超过 {limit_mb}MB")
                fh.write(block)
        os.replace(tmp, target)
    except BaseException:
        # HTTPException、客户端中途断开、磁盘满都走这里：临时文件一律不留，
        # target 保持原样（可能是上一次成功上传的那份）
        _quiet_unlink(tmp)
        raise
    return written


# --------------------------------------------------------------- 分片上传
#
# 单请求上传的天花板不在服务端逻辑，而在链路上：浏览器对长时间无进展的请求
# 会判超时，反向代理有 request body 上限，HTTP/1.1 还得整个 body 传完才开始
# 处理。几十 MB 就开始不稳，几百 MB 基本必挂——这时 `max_file_mb` 设多大都
# 没意义，因为根本传不到判大小的那一步。
#
# 分片把"一次大传输"拆成"多次小传输"，换来三件事：
#   ① 失败代价从"整个文件重来"降到"重传一片"；
#   ② 单片小到不会触发中间层的体积/超时限制；
#   ③ 进度是真实字节累计，不是猜的。
#
# upload_id 由前端的 fingerprint（名字+大小+修改时间）决定，所以浏览器刷新、
# 甚至明天再来传同一个文件，已收的分片还在，能接着传。这不是花活——中断在
# 大文件上传里是常态，不能续传的分片上传只是把一次失败拆成十次失败。
_PARTS_DIR = UPLOAD_DIR / ".parts"
_CHUNK_SIZE = 4 * 1024 * 1024     # 4MiB：请求数不至于太多，重传又不至于太贵
_PART_TTL_H = 24                  # 未完成的分片留一天；再久就是垃圾
_UID_RE = re.compile(r"^[0-9a-f]{16,64}$")


def _upload_dir(upload_id: str) -> Path:
    """分片目录。id 必须先过白名单——它被拼进路径，不校验等于任意目录读写。"""
    if not _UID_RE.match(upload_id or ""):
        raise HTTPException(400, "上传标识非法")
    return _PARTS_DIR / upload_id


def _quiet_unlink(p: Path) -> bool:
    """删单个文件，删不掉就认了。

    两条原则。一，删除是收尾动作：文件已经合并落地、请求已经成功，这时删不掉
    一块临时文件只该留点垃圾，不该把成功改判成 500。二，除了 OSError 还要接
    SystemExit——宿主环境（沙箱、审计钩子、企业管控）会在删除时直接抛它硬拦，
    它不属于 Exception，`ignore_errors=True` 也盖不住，真见过把整个服务带走。
    """
    try:
        p.unlink(missing_ok=True)
    except (OSError, SystemExit):
        return False
    return True


def _discard_parts(folder: Path) -> bool:
    """丢掉一整个分片目录，返回是否真的清掉了。

    逐个删，不用 shutil.rmtree：rmtree 会先把整棵树数一遍再整体交给回收站，
    几十片就直接撞上批量删除守卫；逐个删每个目标只有一个文件，既不会误伤，
    失败也只损失一个分片。而且任何失败都不外抛（见 _quiet_unlink）。
    """
    try:
        if not folder.exists():
            return True
        for p in folder.iterdir():
            _quiet_unlink(p)
        folder.rmdir()
    except (OSError, SystemExit):
        return False
    return not folder.exists()


def _read_meta(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(folder: Path, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _received_parts(folder: Path) -> list[int]:
    """已收的分片序号。按文件名取，不信任 meta 里记的那份——meta 是缓存，
    磁盘才是事实；两者不一致时以磁盘为准，否则续传会漏掉一片而 finish 才报错。"""
    got: list[int] = []
    try:
        for p in folder.iterdir():
            if p.suffix == ".part" and p.stem.isdigit():
                got.append(int(p.stem))
    except OSError:
        return []
    return sorted(got)


def _sweep_stale_parts() -> int:
    """清掉太久的残片。

    半途放弃的传输（用户关页面、换文件）在磁盘上留一堆 4MB 碎块，不清就只涨
    不降。按目录 mtime 判定：每次写分片都会刷新它，所以"24 小时没动过"等于
    "确实没人再传了"。
    """
    if not _PARTS_DIR.exists():
        return 0
    cutoff = time.time() - _PART_TTL_H * 3600
    removed = 0
    for folder in _PARTS_DIR.iterdir():
        try:
            if not folder.is_dir() or folder.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if _discard_parts(folder):
            removed += 1
    return removed


def _warmup(state: "AppState") -> None:
    """后台预热嵌入模型。

    冷启动要 import torch + 读 92MB 权重，约 40 秒。放在第一个用户的提问上，
    体验就是"点了发送转圈一分钟"。放到后台线程里，启动慢一点但提问立刻能用。

    注意：预热期间**不能让别的接口去碰 `embedder.available`**，那会触发另一次
    加载并把读状态变成 40 秒阻塞。所以这里维护一份显式状态，/api/status 只读它。
    """
    state.warm["state"] = "loading"
    started = time.perf_counter()
    try:
        state.sparse.warmup()  # jieba 建词典很快，不值得开线程
    except Exception as exc:
        print(f"[warmup] 分词器预热失败：{type(exc).__name__}: {exc}")
        logbus.note(f"分词器预热失败：{exc}", level="WARNING", source="warmup")
    try:
        state.embedder.load()
        state.vectors.ensure(state.db.conn())
        took = int((time.perf_counter() - started) * 1000)
        state.warm.update(state="ready", took_ms=took, error=None)
        print(f"[warmup] 嵌入模型与向量矩阵就绪（{took}ms）")
        logbus.note(f"嵌入模型与向量矩阵就绪（{took}ms）", source="warmup")
    except Exception as exc:
        state.warm.update(state="failed", error=f"{type(exc).__name__}: {exc}",
                          took_ms=int((time.perf_counter() - started) * 1000))
        print(f"[warmup] 嵌入模型预热失败：{type(exc).__name__}: {exc}")
        logbus.note(f"嵌入模型预热失败：{exc}", level="ERROR", source="warmup")


def _sweep_orphan_imports() -> None:
    """收拾上次没跑完的导入。

    `ingest/pipeline.py` 里每条失败路径都调 `_fail()` 落 failed，唯独"进程被
    kill / 断电"没有代码可执行——那些文档会永久停在 indexing，界面显示"索引
    中"，而重索引入口按状态过滤时还会把它们当成在跑。启动是**唯一**能确定
    "此刻没有导入在跑"的时刻（`_IMPORT_TASKS` 必然是空的），就在这里收尾。

    只清 pending/indexing：ready 和 failed 是终态，`_fail` 写下的原因比"服务
    重启"更具体，覆盖掉等于把有效信息换成废话。
    """
    try:
        conn = state.db.conn()
        row = conn.execute(
            "SELECT count(*) AS c FROM document WHERE status IN ('pending','indexing')"
        ).fetchone()
        n = int(row["c"]) if row else 0
        if n <= 0:
            return
        conn.execute(
            "UPDATE document SET status='failed', fail_reason=?, updated_at=?"
            " WHERE status IN ('pending','indexing')",
            ("上次导入未完成（服务重启中断），点「重索引」可重来", _utcnow()),
        )
        logbus.note(f"清理了 {n} 个中断的导入，已标记为失败，请按需重索引",
                    level="WARNING", source="warmup")
        print(f"[sweep] {n} 个中断的导入已置 failed")
    except Exception as exc:  # 清理失败不该拦住服务启动
        print(f"[sweep] 清理中断导入失败：{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 只有之前勾过"记住密钥"才会有这个文件；没有就是纯内存，重启即清空。
    if secrets_mod.attach(state.cfg.storage.data_dir):
        logbus.note(
            f"已从本机密文文件恢复密钥（{secrets_mod.backend_label()}；你之前勾了『记住密钥』）",
            source="models")
        # 换账户/换机器后密文解不开：这时候界面上"密钥已记住"是假的，
        # 提示"去模型页重填"才是有用的信息。
        if secrets_mod.last_error():
            logbus.note(secrets_mod.last_error(), level="WARNING", source="models")
    _sweep_orphan_imports()
    stale = _sweep_stale_parts()
    if stale:
        logbus.note(f"清理了 {stale} 份超过 {_PART_TTL_H} 小时的未完成分片", source="ingest")
    # 上次的探测结论要读回内存：首屏走的是 cached()，它不会去加载文件。
    # 这一步漏了，模型页重启后就是整页"还没探测"，而盘上明明有记录。
    services_mod.warm_cache(state.cfg)
    threading.Thread(target=_warmup, args=(state,), daemon=True, name="warmup").start()
    # 标了 autostart 的前台实例随后台一起起来。放在线程外面同步做：它们只是
    # 端口监听，不阻塞，而放到后台线程里会让"启动失败"变得没有反馈。
    for name in state.sites.start_autostart():
        print(f"[sites] 已自动启动站点 {name}")
    yield
    state.sites.stop_all()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_id: int | None = None
    session_id: str | None = None
    provider: str | None = None
    rerank_model: str | None = None
    rerank: bool | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    # F2.7 元数据过滤：只在这几类文档里检索
    source_types: list[str] | None = None
    doc_ids: list[int] | None = None
    imported_after: str | None = None
    imported_before: str | None = None

    # ---- 本次提问临时生效的覆盖（不写盘，只影响这一条请求）----
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=64, le=8192)
    stream: bool | None = None
    mmr: bool | None = None
    # 点分路径 → 值的临时参数覆盖。白名单由 kbconfig.effective 把关
    # （不在 KB_PARAM_MAP 里的键直接丢弃），前端传再多也改不动别的配置。
    overrides: dict[str, Any] | None = None
    # 不挂载知识库时：True=完全不检索、直接问模型；False=跨全部库检索。
    direct: bool = False

    def filters(self) -> dict:
        return {
            "source_types": self.source_types,
            "doc_ids": self.doc_ids,
            "imported_after": self.imported_after,
            "imported_before": self.imported_before,
        }


class DebugRequest(AskRequest):
    top_k: int | None = Field(default=None, ge=1, le=50)


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


class PathImport(BaseModel):
    kb_id: int
    path: str
    plan: dict[str, object] | None = None


class TextImport(BaseModel):
    kb_id: int
    title: str = ""
    text: str = Field(min_length=1)
    plan: dict[str, object] | None = None


class UrlImport(BaseModel):
    kb_id: int
    urls: list[str] = Field(min_length=1, max_length=50)
    plan: dict[str, object] | None = None


class ReindexReq(BaseModel):
    # 不传 = 沿用上次导入时用的那套清洗方案；传空 dict = 这次不清洗
    plan: dict[str, object] | None = None


class FeedbackReq(BaseModel):
    message_id: int
    verdict: str = Field(pattern="^(up|down)$")
    comment: str = ""


class AppState:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.db = Database(self.cfg)
        self.embedder = Embedder(self.cfg.embedding)
        self.vectors = VectorStore(self.cfg)
        self.sparse = SparseIndex(self.cfg.retrieval.sparse)
        self.reranker = Reranker(self.cfg)
        self.engine = RetrievalEngine(
            self.cfg, self.embedder, self.vectors, self.sparse, self.reranker
        )
        self.pipeline = IngestPipeline(
            self.cfg, self.db, self.embedder, self.sparse, self.vectors
        )
        self.llm = LLMClient(self.cfg)
        self.db.ensure_schema()
        self.started_at = time.time()
        # 知识库级配置的副本缓存：key=kb_id，value=(覆盖值签名, Config 副本)。
        # 每次请求都 deepcopy 一份 Config 也能用，但那是每次提问多花几十微秒
        # 去复制一堆根本不会变的嵌套模型，而这里只按"覆盖值变了没"失效。
        self._kbcfg: dict[int, tuple[str, Config]] = {}
        self._kbcfg_lock = threading.Lock()
        # 预热进度。冷启动 40 秒里，前端必须能说清"为什么还不能问"，
        # 否则用户看到的就是"页面是空的 + 点了发送一直转圈"。
        self.warm: dict = {"state": "pending", "started_at": time.time(),
                           "took_ms": None, "error": None}
        # 前台问答站点（独立端口）。同一进程内复用上面的检索器与 LLM，
        # 所以不用再加载一遍嵌入模型。
        self.sites = InstanceManager(self)

    @property
    def conn(self):
        return self.db.conn()

    # ------------------------------------------------------------ 知识库配置

    def overrides(self, kb_id: int | None) -> dict:
        if not kb_id:
            return {}
        return kbconfig.load(self.conn, int(kb_id))

    def tuned(self, kb_id: int | None) -> dict:
        """与系统默认值不同的那几项 —— 调试面板要显示的是"这个库调过什么"，
        而现在 config_json 里每一项都有值，直接报长度永远是 25。"""
        values = self.overrides(kb_id)
        if not values:
            return {}
        baseline = kbconfig.baseline(self.cfg)
        return {k: v for k, v in values.items() if baseline.get(k) != v}

    def effective_cfg(self, kb_id: int | None) -> Config:
        """该知识库实际生效的配置。没有覆盖值时直接返回全局对象本身。"""
        if not kb_id:
            return self.cfg
        overrides = self.overrides(kb_id)
        if not overrides:
            return self.cfg
        signature = json.dumps(overrides, sort_keys=True, ensure_ascii=False)
        with self._kbcfg_lock:
            hit = self._kbcfg.get(int(kb_id))
            if hit and hit[0] == signature:
                return hit[1]
        clone = kbconfig.effective(self.cfg, overrides)
        with self._kbcfg_lock:
            self._kbcfg[int(kb_id)] = (signature, clone)
        return clone

    def invalidate_kb_cfg(self, kb_id: int | None = None) -> None:
        with self._kbcfg_lock:
            if kb_id is None:
                self._kbcfg.clear()
            else:
                self._kbcfg.pop(int(kb_id), None)


state = AppState()
app = FastAPI(title="ragkb", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# 静态资源绝不允许"启发式缓存"。FileResponse / StaticFiles 默认只带 ETag 和
# Last-Modified、不带 Cache-Control，WebView 会按"距今时长的 10%"自作主张缓存
# index.html —— 结果就是页面停在旧版：按钮在、逻辑是旧的（比如删库还在用被
# 吞掉的原生 confirm），表现成"点了没反应"，怎么查代码都对。
# 本地管理端正确性优先：一律 no-cache（每次协商，命中就 304，几乎零开销）。
@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        # 首页用 no-store：no-cache 只是"每次问问服务器"，某些内嵌 WebView
        # 连问都不问就直接给旧的，用户看到的是改版前的界面。
        resp.headers["Cache-Control"] = "no-store" if path == "/" else "no-cache"
    return resp


# --------------------------------------------------------------------- 基础


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
def status() -> dict:
    conn = state.conn
    cfg = state.cfg
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
        "rerank": {
            "enabled": cfg.retrieval.rerank.enabled,
            "default_model": cfg.retrieval.rerank.model,
            "last_error": state.reranker.last_error,
        },
        "refusal_calibrated": bool(cfg.refusal.calibrated_at),
        "refusal": {
            "min_score": cfg.refusal.min_score,
            "min_margin": cfg.refusal.min_margin,
            "calibrated_at": cfg.refusal.calibrated_at,
            "samples_ok": cfg.refusal.calibrated_samples_ok,
            # 有标定文件但样本不足时，界面上要说清"为什么现在用的是出厂阈值"，
            # 否则用户只会看到阈值和他上次标定的数字不一样，以为程序没保存。
            "note": (
                None if cfg.refusal.calibrated_at
                else ("标定样本不足，已回退出厂阈值" if calibration_of(cfg) else "尚未标定，使用出厂阈值")
            ),
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


# ------------------------------------------------------- 系统总览 / 模型服务


@app.get("/api/dashboard")
def dashboard(request: Request) -> dict:
    """首页一次拿全。服务状态只读缓存，不在这里发网络请求。"""
    return build_dashboard(
        state.cfg, state.conn, started_at=state.started_at, port=request.url.port
    )


@app.post("/api/services/probe")
def probe_services(name: str | None = None) -> dict:
    """真正去连。前端在首屏渲染完之后才调它，不让网络等待堵住首屏。"""
    if name:
        if name not in state.cfg.generation.providers:
            raise HTTPException(404, "后端不存在")
        item = services_mod.probe_one(name, state.cfg.generation.providers[name])
        services_mod.record(item)  # 写缓存并落盘，重启后仍显示这次结论
        return {"providers": {name: item}}
    result = services_mod.probe_all(state.cfg, force=True)
    ok = sum(1 for v in result.values() if v.get("ok"))
    logbus.note(f"探测 {len(result)} 个后端，{ok} 个可用", source="services")
    return {"providers": result}


@app.get("/api/models")
def list_models() -> dict:
    data = modelcfg.listing(state.cfg)
    for p in data["providers"]:
        probe = services_mod.cached(
            p["name"], provider=state.cfg.generation.providers.get(p["name"])) or {}
        p["status"] = probe.get("status", "unknown")
        p["latency_ms"] = probe.get("latency_ms")
        p["message"] = probe.get("message", "还没探测")
        p["checked_at"] = probe.get("checked_at")
        # 探测回来的模型 id 列表必须带出去。之前这里只取了 status，前端于是只
        # 能显示"4 个模型可用"却点不到任何一个——用户得自己去 LM Studio 抄名字。
        p["models"] = probe.get("models") or []
        p["model_listed"] = probe.get("model_listed")

    # 探测到的模型并进扁平目录：那些是"服务上真实存在、只是我还没加进清单"的，
    # 恰恰是用户最想选的一批。缺了它们，下拉看着就像"少了一大截"。
    meta = {p["name"]: p for p in data["providers"]}
    seen = {(c["provider"], c["model"]) for c in data["catalog"]}
    for p in data["providers"]:
        for mid in p["models"]:
            if (p["name"], mid) in seen:
                continue
            seen.add((p["name"], mid))
            data["catalog"].append({
                "provider": p["name"], "model": mid,
                "current": False, "provider_active": bool(p["active"]),
                "ready": bool(p["ready"]),
            })
    for c in data["catalog"]:
        m = meta.get(c["provider"]) or {}
        c["provider_label"] = m.get("kind_label") or c["provider"]
        c["local"] = bool(m.get("local"))
        # "当前"= 后端是当前后端 + 模型是这个后端的当前模型，缺一不可
        c["active"] = bool(c.get("provider_active") and c.get("current"))
    data["catalog"].sort(key=lambda c: (
        not c["active"], not c["local"], c["provider"], c["model"]))
    return data


class ProbeUrlRequest(BaseModel):
    base_url: str = ""
    # 探测用的密钥：填了就用这份，留空则退到该后端已存在内存里的密钥
    api_key: str = ""
    kind: str = "custom"
    model: str = ""
    # 正在编辑哪个后端：带上名字才能复用已存的密钥
    name: str = ""


@app.post("/api/services/probe-url")
def probe_url(req: ProbeUrlRequest) -> dict:
    """按用户刚敲进去的地址探测，保存之前就能确认对不对。

    与 `/api/services/probe` 的区别是它不读注册表、不写缓存：这是"试一下"，
    结果不该污染卡片上显示的状态。
    """
    try:
        clean = modelcfg.validate_url(req.base_url, allow_empty=req.kind == "mock") or None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tmp = ProviderCfg(
        base_url=clean,
        model=(req.model or "").strip() or "placeholder",
        kind=req.kind if req.kind in modelcfg.KIND_MAP else "custom",
    )
    # 弹窗里刚敲的密钥只挂在这个临时对象上，不进注册表、更不进配置文件。
    # 留空时带上后端名，探测会自动复用内存里已存的那份密钥。
    tmp.set_inline_key(req.api_key)
    hit = services_mod.probe_one(req.name.strip() or "__probe__", tmp)
    return {
        "ok": hit.get("ok"),
        "status": hit.get("status"),
        "message": hit.get("message"),
        "latency_ms": hit.get("latency_ms"),
        "models": hit.get("models") or [],
    }


class ModelUpsert(BaseModel):
    base_url: str = ""
    model: str = ""
    kind: str = "custom"
    # None = 不改动现有密钥；"" = 清空；非空 = 设为新密钥。
    # 密钥只进内存（ragkb.secrets），永远不写 models.yaml。
    api_key: str | None = None
    # 模型清单（人维护的"我打算用哪些模型"），None = 不动
    model_list: list[dict] | None = None
    # 是否把密钥记到本机。None = 不动现有设置；True = 写盘；False = 删掉盘上的
    remember: bool | None = None


@app.put("/api/models/{name}")
def upsert_model(name: str, req: ModelUpsert) -> dict:
    # 只改「用哪个模型」时不能把探测缓存清掉：缓存里存的是"这台服务上有哪些模型"，
    # 那是服务端的属性，和你选了哪一个无关。以前一律 invalidate，于是用户刚在卡片
    # 下拉里切一个模型，列表立刻消失、卡片退回"还没探测"——看起来就是"一切换就没了"。
    before = state.cfg.generation.providers.get(name)
    sig_before = (before.base_url, before.kind) if before else None
    try:
        result = modelcfg.upsert(
            state.cfg, name, base_url=req.base_url, model=req.model, kind=req.kind,
            model_list=req.model_list,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    after = state.cfg.generation.providers.get(name)
    sig_after = (after.base_url, after.kind) if after else None
    if sig_before != sig_after:
        services_mod.invalidate(name)

    # 密钥只在内存里：写进注册表随即丢掉引用，任何落盘路径都碰不到它。
    if req.api_key is not None:
        from .. import secrets as secrets_mod

        key = req.api_key.strip()
        if key:
            secrets_mod.put(name, key)
            services_mod.invalidate(name)     # 换了密钥，之前的探测结论作废
            logbus.note(f"后端 {name} 的密钥已更新（仅内存）", source="models")
        else:
            secrets_mod.drop(name)
            services_mod.invalidate(name)
            logbus.note(f"后端 {name} 的密钥已清空", source="models")
    logbus.note(
        f"后端 {name} 已保存：{req.model} @ {req.base_url or '-'}", source="models"
    )
    from .. import secrets as secrets_mod

    if req.remember is not None:
        if req.remember:
            ok = secrets_mod.save()
            logbus.note(
                (f"密钥已加密记到本机（{secrets_mod.backend_label()}）："
                 f"{state.cfg.storage.data_dir / 'secrets.json'}")
                if ok else f"记住密钥失败：{secrets_mod.last_error() or '无法写入 data 目录'}",
                level="WARNING", source="models")
        else:
            secrets_mod.forget()
    return {"ok": True, **result, "listing": modelcfg.listing(state.cfg)}


@app.delete("/api/models/{name}")
def delete_model(name: str) -> dict:
    providers = state.cfg.generation.providers
    if name not in providers:
        raise HTTPException(404, "后端不存在")
    if len(providers) <= 1:
        raise HTTPException(400, "至少要留一个后端：先新增一个，再删这个")
    # 删的正好是当前在用的那个时，以前是报错让用户自己先去切——那是把系统的
    # 内部约束变成一道手工题。这里直接切到另一个（优先本地）再删，用户点一次
    # 就完成；切换的结果会落盘，不会重启后又指回一个已删掉的后端。
    if state.cfg.generation.provider == name:
        others = [n for n in providers if n != name]
        picked = min(
            others,
            key=lambda n: not modelcfg.KIND_MAP.get(providers[n].kind or n, {}).get("local", False),
        )
        try:
            settings_mod.apply(state.cfg, {"generation.provider": picked})
        except ValueError:
            picked = others[0]
            settings_mod.apply(state.cfg, {"generation.provider": picked})
        settings_mod.save_local(state.cfg)
        state.invalidate_kb_cfg()
        logbus.note(f"删除当前后端 {name}，自动切换到 {picked}", level="WARNING", source="models")
    try:
        result = modelcfg.remove(state.cfg, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    services_mod.invalidate(name)
    from .. import secrets as secrets_mod

    secrets_mod.drop(name)      # 后端没了，内存里的密钥也不该留着
    logbus.note(f"后端 {name} 已删除", level="WARNING", source="models")
    return {"ok": True, **result}


@app.post("/api/models/keys/clear")
def clear_all_keys() -> dict:
    """清空所有内存密钥。存在的理由：密钥的唯一归宿是内存，那就得有个
    "现在就把它们全抹掉"的动作，而不是只能靠重启进程。"""
    from .. import secrets as secrets_mod

    gone = secrets_mod.names()
    secrets_mod.wipe_file()      # 连盘上的那份一起删，别留半拉子
    for n in state.cfg.generation.providers:
        services_mod.invalidate(n)
    logbus.note(f"已清空 {len(gone)} 个内存密钥：{', '.join(gone) or '-'}",
                level="WARNING", source="models")
    return {"ok": True, "cleared": gone}


class ActiveModel(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 可选：同时指定这个后端用哪个模型。"当前用什么"其实有两段——哪个后端、
    # 这个后端的哪个模型。以前只切后端，模型停在旧值上，用户改完发现"还是
    # 原来那个模型"，因为模型藏在后端的配置字段里，不像"当前"的一部分。
    model: str | None = None


@app.post("/api/models/active")
def set_active_model(req: ActiveModel) -> dict:
    """切换当前后端（可同时切模型），并立刻落盘。

    "设为当前"是一个明确的决定，不该像滑杆那样停在内存里等用户去点保存——
    重启一次又变回旧后端，人会以为是 bug。
    """
    try:
        settings_mod.apply(state.cfg, {"generation.provider": req.name})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    picked = ""
    if req.model is not None:
        prov = state.cfg.generation.providers.get(req.name)
        want = (req.model or "").strip()
        if prov is not None and want and prov.model != want:
            prov.model = want
            picked = want
            modelcfg.persist(state.cfg)   # 模型名写进 models.yaml，重启后仍是它
    saved = settings_mod.save_local(state.cfg)
    state.invalidate_kb_cfg()
    # 换了后端/模型，上一次的探测结论不再代表"现在这套能不能用"
    services_mod.invalidate(req.name)
    logbus.note(
        f"当前生成后端切换为 {req.name}" + (f" · 模型 {picked}" if picked else ""),
        source="models")
    return {"ok": True, "active": req.name, "model": picked, "saved": saved}


# ------------------------------------------------------------------- 参数面板
#
# 检索参数原本只能改 YAML 再重启，等于把"调参"这件事锁在编辑器里。
# 这里把参数注册表直接暴露成接口：GET 拿定义+当前值，PATCH 改运行时，
# POST /save 落 local.yaml，POST /reset 回出厂值，POST /reload 丢弃改动。


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_mod.snapshot(state.cfg)


class SettingsPatch(BaseModel):
    values: dict[str, object] = Field(min_length=1)

    model_config = {"extra": "forbid"}


@app.patch("/api/settings")
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


@app.post("/api/settings/save")
def save_settings() -> dict:
    result = settings_mod.save_local(state.cfg)
    if result["removed"]:
        result["message"] = "所有参数都等于出厂值，已删除 local.yaml"
    else:
        result["message"] = f"已写入 {len(result['written'])} 项到 config/local.yaml"
    return result


@app.post("/api/settings/reset")
def reset_settings() -> dict:
    result = settings_mod.reset(state.cfg)
    state.invalidate_kb_cfg()
    result["message"] = (
        f"已恢复 {len(result['restored'])} 项默认值（尚未保存）"
        if result["restored"] else "当前已经是默认值"
    )
    return result


@app.post("/api/settings/reload")
def reload_settings() -> dict:
    result = settings_mod.reload_from_disk(state.cfg)
    state.invalidate_kb_cfg()
    services_mod.invalidate()
    result["message"] = (
        f"已从磁盘重读，{len(result['reloaded'])} 项变化"
        if result["reloaded"] else "磁盘配置与内存一致"
    )
    return result


# ------------------------------------------------------------------- 知识库


@app.get("/api/kb")
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


@app.get("/api/kb/presets")
def kb_presets() -> list[dict]:
    """建库向导用的预设。放在 /api/kb/{kb_id} 之前声明，避免被路径参数吃掉。"""
    return [
        {"id": p["id"], "label": p["label"], "desc": p["desc"], "values": p["values"]}
        for p in kbconfig.PRESETS
    ]


@app.post("/api/kb")
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


@app.delete("/api/kb/{kb_id}")
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


@app.patch("/api/kb/{kb_id}")
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


@app.get("/api/kb/{kb_id}/detail")
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


@app.get("/api/kb/{kb_id}/config")
def kb_config(kb_id: int) -> dict:
    row = state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")
    return kbconfig.snapshot(
        state.cfg, kbconfig.load_full(state.conn, kb_id, state.cfg)
    )


@app.patch("/api/kb/{kb_id}/config")
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


@app.delete("/api/kb/{kb_id}/config")
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


@app.get("/api/kb/{kb_id}/docs")
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


@app.post("/api/kb/{kb_id}/ensure")
def ensure_kb(kb_id: int) -> dict:
    row = state.conn.execute("SELECT id, name FROM kb WHERE id=?", (kb_id,)).fetchone()
    if not row:
        raise HTTPException(404, "知识库不存在")
    return dict(row)


# --------------------------------------------------------------------- 导入
#
# 导入为什么变成"后台任务 + 轮询"：
# 上传进度条走到 100% 只说明字节传完了，真正的耗时在解析 / 切分 / 向量化
# （本机嵌入约 21 块/秒）。原来这段时间只有一个转圈的斜纹，用户没法判断
# 是"在跑"还是"死了"。改成任务制之后，前端能拿到 stage + pct + "128/300 块"
# 这种具体数字；顺带把重索引、补回源文件也统一到同一条链路。

_IMPORT_TASKS: dict[str, dict] = {}
# 用**可重入**锁：`import_tasks` 在持锁的情况下逐个调 `_task_public`，而后者
# 也要看一眼任务表来算排队位置。普通 Lock 会在那里把自己锁死（实测：请求
# 直接挂住不返回）。同一模块内的组合调用，重入是这里真正想要的语义。
_TASK_LOCK = threading.RLock()
_TASK_KEEP = 40
# 任务**串行**执行：解析/切分/向量化会同时写 SQLite、同时占用嵌入模型，
# 并行跑两个不只是进度条互相打架（两个任务往同一张卡上写百分比），还容易
# 把嵌入模型挤爆。排队执行总耗时几乎一样，但每个任务的进度是真的。
_TASK_Q: _queue.Queue[str] = _queue.Queue()
_TASK_SEQ = 0


def _task_worker() -> None:
    """单线程消费任务队列。任务里抛异常由 work() 自己兜住，这里只管状态切换。"""
    while True:
        tid = _TASK_Q.get()
        with _TASK_LOCK:
            task = _IMPORT_TASKS.get(tid)
            if task is not None:
                task["state"] = "running"
                task["stage"] = "启动"
        if task is None:
            continue
        try:
            task["_work"]()
        finally:
            _TASK_Q.task_done()


threading.Thread(target=_task_worker, daemon=True, name="import-worker").start()


def _task_public(task: dict) -> dict:
    """去掉下划线开头的内部字段（_work 是个函数，不能进 JSON），并补排队位置。"""
    with _TASK_LOCK:
        seq = task.get("_seq", 0)
        ahead = 0
        if task["state"] == "queued":
            ahead = sum(
                1
                for t in _IMPORT_TASKS.values()
                if t.get("_seq", 0) < seq and t["state"] in ("queued", "running")
            )
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    out["queue_pos"] = ahead
    return out


def _start_import_task(label: str, fn) -> dict:
    """fn(progress) -> dict（IngestResult.as_dict() 的形状）。

    返回里带 `queue_pos`：前面还有几个任务。不给这个数的话，用户连点三个
    重索引只会看到两个纹丝不动的 0%，像是卡死了。
    """
    global _TASK_SEQ
    tid = uuid.uuid4().hex[:10]
    with _TASK_LOCK:
        _TASK_SEQ += 1
        seq = _TASK_SEQ
    task: dict = {
        "task_id": tid,
        "label": label,
        "state": "queued",
        "stage": "排队",
        "pct": 0.0,
        "note": "",
        "message": None,
        "result": None,
        "started_at": _utcnow(),
        "took_ms": None,
        "_seq": seq,
    }
    with _TASK_LOCK:
        _IMPORT_TASKS[tid] = task
        while len(_IMPORT_TASKS) > _TASK_KEEP:
            _IMPORT_TASKS.pop(next(iter(_IMPORT_TASKS)))

    def progress(stage: str, pct: float, note: str = "") -> None:
        task["stage"] = stage
        task["pct"] = round(float(pct), 4)
        task["note"] = note or ""

    def work() -> None:
        started = time.perf_counter()
        try:
            result = fn(progress)
            # 索引刚建完，源文件还在手边 —— 这时候顺手做一次完整性自检最便宜：
            # 之后文件可能被挪走/改掉，就再也比不了了。结论直接挂在任务结果里，
            # 用户不用去预览弹窗里翻「完整性自检」那个按钮才知道导全了没有。
            if isinstance(result, dict) and result.get("status") == "ready" and result.get("doc_id"):
                digest = _coverage_digest(int(result["doc_id"]))
                if digest:
                    result["coverage"] = digest
                    if digest["big_gap_count"]:
                        logbus.note(
                            f"导入自检：doc#{result['doc_id']} 成段缺失 {digest['big_gap_count']} 处"
                            f"（最大 {digest['max_gap']} 字）",
                            level="WARN", source="ingest",
                        )
            task["result"] = result
            task["state"] = "ready" if result.get("status") == "ready" else "failed"
            task["message"] = result.get("message")
            task["stage"] = "完成" if task["state"] == "ready" else "失败"
            task["pct"] = 1.0 if task["state"] == "ready" else task["pct"]
        except Exception as exc:  # 任务线程里抛什么都得落到任务上，否则前端永远等
            task["state"] = "failed"
            task["message"] = f"{type(exc).__name__}: {exc}"
            logbus.note(f"导入任务失败：{task['message']}", level="ERROR", source="ingest")
        finally:
            task["took_ms"] = int((time.perf_counter() - started) * 1000)

    task["_work"] = work
    _TASK_Q.put(tid)
    return _task_public(task)


@app.get("/api/import/task/{task_id}")
def import_task(task_id: str) -> dict:
    task = _IMPORT_TASKS.get(task_id)
    if task is None:
        # 进程重启后旧 task_id 一律失效（任务表只在内存里），说清楚而不是 404 装死
        raise HTTPException(404, "任务不存在或服务已重启，请重新上传")
    return _task_public(task)


@app.get("/api/import/tasks")
def import_tasks() -> dict:
    """当前全部后台任务（前端切回页面时用它补齐：轮询只在任务发起的那一刻
    开始，切走再回来，先前那些任务的卡片已经没人喂数据了）。"""
    with _TASK_LOCK:
        items = [_task_public(t) for t in _IMPORT_TASKS.values()]
    items.sort(key=lambda t: t.get("started_at") or "")
    return {
        "items": items,
        "running": sum(1 for t in items if t["state"] == "running"),
        "queued": sum(1 for t in items if t["state"] == "queued"),
    }


# ---------------------------------------------------------------- 清洗
#
# 清洗 = "导入前把不该进库的内容剔掉"。两条硬约束：
#   ① 源文件与上传副本都只当原料，一个字节都不写回（写回会让重索引拿到
#      "洗过一遍"的输入，二次清洗结果不可预期）；
#   ② 每条规则都要报账单 —— 删错就是永久丢失，用户得在导入前看到删了什么。

@app.get("/api/clean/rules")
def clean_rules_api() -> dict:
    """规则定义、默认值、参数规格全部由后端下发，前端不写死任何一条。"""
    return rules_payload()


def _parse_plan(raw: str | None) -> "object":
    if raw is None or not str(raw).strip():
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        raise HTTPException(400, f"清洗方案不是合法 JSON：{exc}") from exc


@app.post("/api/clean/preview")
async def clean_preview(
    file: UploadFile | None = None,
    plan: str | None = Form(None),
    text: str | None = Form(None),
    path: str | None = Form(None),
    staged: str | None = Form(None),
    doc_id: int | None = Form(None),
    url: str | None = Form(None),
) -> dict:
    """不入库，只报告"这套规则会删掉什么"。

    上传的文件会落盘并回一个 staged 路径：预览完直接导入就不用再传一次字节，
    而且改规则重新预览走的还是同一份文件（否则每次勾选都要重传，本地文件
    倒还好，稍大一点就是几十秒）。
    """
    parsed = plan_from_dict(_parse_plan(plan))
    try:
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    target: Path | None = None
    title = ""
    if file is not None and (file.filename or ""):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(file.filename or "upload.bin").name
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
        await _save_upload(file, target, state.cfg.ingest.max_file_mb)
        title = name
    elif staged:
        target = Path(staged)
        # staged 只认 uploads 里的路径：这是个落盘路径参数，不校验等于把
        # 任意文件读取能力开放出去。
        if not _inside(target, UPLOAD_DIR) or not target.exists():
            raise HTTPException(400, "预览用的文件已失效，请重新选择文件")
        title = display_title(str(target))
    elif path:
        target = Path(path)
        if not target.exists():
            raise HTTPException(400, f"路径不存在：{path}")
        title = target.name
    elif doc_id:
        row = state.conn.execute(
            "SELECT source_type, source_uri, title FROM document WHERE id=?", (doc_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "文档不存在")
        if row["source_type"] != "file" or _source_missing("file", row["source_uri"] or ""):
            raise HTTPException(400, "这份文档的原始文件已不在，无法预览清洗效果")
        target = Path(row["source_uri"])
        title = row["title"] or target.name
    elif url:
        ucfg = state.cfg.ingest.url
        try:
            loaded = loader_mod.load_url(
                url.strip(), allowed_domains=ucfg.allowed_domains or None
            )
        except loader_mod.ExtractError as exc:
            raise HTTPException(400, str(exc)) from exc
        title = loaded.title or url
        cleaned, report = clean_loaded(loaded, parsed)
        return {
            "ok": True, "title": title, "staged": None,
            "report": report.as_dict(), "cleaned_text": cleaned.text[:6000],
            "cleaned_chars": len(cleaned.text),
        }

    try:
        if target is not None:
            loaded = loader_mod.load_path(
                target, min_pdf_text_chars=state.cfg.ingest.pdf.min_text_chars_per_page
            )
        else:
            if not (text or "").strip():
                raise HTTPException(400, "没有可预览的内容：给文件或文本")
            loaded = loader_mod.load_inline_text(text or "", title or "粘贴文本")
            title = title or "粘贴文本"
    except loader_mod.ExtractError as exc:
        raise HTTPException(400, str(exc)) from exc

    cleaned, report = clean_loaded(loaded, parsed)
    return {
        "ok": True,
        "title": title,
        "staged": str(target) if target is not None and _inside(target, UPLOAD_DIR) else None,
        "report": report.as_dict(),
        # 前 6000 字够判断"洗得对不对"了，全量文本回来只会让弹窗卡一下
        "cleaned_text": cleaned.text[:6000],
        "cleaned_chars": len(cleaned.text),
    }


@app.post("/api/import/path")
def import_path(req: PathImport) -> dict:
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(400, f"路径不存在：{req.path}")
    cfg = state.effective_cfg(req.kb_id)  # 分块参数按库生效
    if p.is_dir():
        results = []
        files = [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in loader_mod.SUPPORTED_EXTS]
        for f in files[:500]:
            results.append(
                state.pipeline.import_path(req.kb_id, f, cfg, clean=req.plan).as_dict())
        ok = sum(1 for r in results if r["status"] == "ready")
        logbus.note(f"目录导入 {p}：{ok}/{len(results)} 成功", source="ingest")
        return {"mode": "dir", "total": len(results), "ready": ok, "results": results}
    result = state.pipeline.import_path(req.kb_id, p, cfg, clean=req.plan).as_dict()
    logbus.note(
        f"导入 {p.name}：{result['message']}",
        level="INFO" if result["status"] == "ready" else "ERROR",
        source="ingest",
    )
    return result


@app.post("/api/import/text")
def import_text(req: TextImport) -> dict:
    result = state.pipeline.import_text(
        req.kb_id, req.text, req.title, state.effective_cfg(req.kb_id), clean=req.plan
    ).as_dict()
    logbus.note(
        f"粘贴文本《{req.title or '未命名'}》：{result['message']}",
        level="INFO" if result["status"] == "ready" else "ERROR",
        source="ingest",
    )
    return result


@app.post("/api/import/url")
def import_url(req: UrlImport) -> dict:
    """单条或批量。逐条独立失败——一条 URL 挂掉不该让整批回滚。"""
    cfg = state.effective_cfg(req.kb_id)
    results = []
    for raw in req.urls[:50]:
        url = raw.strip()
        if not url:
            continue
        item = state.pipeline.import_url(req.kb_id, url, cfg, clean=req.plan).as_dict()
        item["url"] = url
        results.append(item)
    if not results:
        raise HTTPException(400, "没有有效的 URL")
    ready = sum(1 for r in results if r["status"] == "ready")
    logbus.note(f"URL 导入：{ready}/{len(results)} 成功", source="ingest")
    return {"total": len(results), "ready": ready, "results": results}


# ---------------------------------------------------------------- 分片上传 API
#
# 四个动作对应传输的一生：init 拿分片方案 → put 逐片推进 → finish 合并成
# 一份 staged 文件 → （断了就）abort 清掉。
#
# finish 产出的路径直接喂给 /api/import/upload 的 `staged` 参数，导入链路
# 一个字都不用改：分片只是"另一种把字节放上磁盘的方式"，放完就退场。


class UploadInitReq(BaseModel):
    name: str
    size: int
    # 带上 kb_id 才能用这个库自己的覆盖值：库级把上限调小了，分片就得在这里
    # 拦住，否则传完 500MB 才在导入那一步被拒，白等一场。
    kb_id: int | None = None
    # 前端算的指纹（名字+大小+修改时间）。同一文件重传能接上已收的分片；
    # 不给就每次新开一个 id，退化成普通分片上传。
    fingerprint: str | None = None


@app.post("/api/upload/init")
def upload_init(req: UploadInitReq) -> dict:
    name = Path(req.name or "upload.bin").name
    size = int(req.size)
    cfg = state.effective_cfg(req.kb_id) if req.kb_id else state.cfg
    limit_mb = int(cfg.ingest.max_file_mb)
    limit = max(1, limit_mb) * 1024 * 1024
    if size <= 0:
        raise HTTPException(400, "文件是空的")
    if size > limit:
        raise HTTPException(
            413, f"文件 {size / 1048576:.0f}MB，超过上限 {limit_mb}MB"
        )

    fp = (req.fingerprint or "").lower()
    upload_id = fp if _UID_RE.match(fp) else hashlib.sha1(
        f"{name}|{size}|{time.time()}".encode()
    ).hexdigest()[:32]
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)

    # 同名同大小但内容不同（编辑过又存回去）会撞同一个 id。size 变了直接重来；
    # 大小没变就没法分辨，照旧续传——总比每次从零开始好，finish 还有总字节校验。
    if meta and int(meta.get("size", -1)) != size:
        _discard_parts(folder)
        meta = None

    total = max(1, (size + _CHUNK_SIZE - 1) // _CHUNK_SIZE)
    if not meta:
        meta = {"name": name, "size": size, "chunk_size": _CHUNK_SIZE,
                "chunks": total, "created_at": time.time()}
        _write_meta(folder, meta)

    received = _received_parts(folder)
    return {
        "upload_id": upload_id,
        "chunk_size": int(meta["chunk_size"]),
        "total_chunks": int(meta["chunks"]),
        "received": received,
        "name": meta.get("name") or name,
    }


@app.put("/api/upload/{upload_id}/part/{index}")
async def upload_part(upload_id: str, index: int, request: Request) -> dict:
    """收一片。body 就是裸字节，不走 multipart。

    multipart 会在每片外面套一层边界+头，服务端还得先解析再落盘；裸字节直接
    边收边写，省一次内存拷贝。代价是不能在这个请求里带别的字段——但分片本来
    就不需要别的字段，元信息在 init 时已经定了。
    """
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)
    if not meta:
        raise HTTPException(404, "上传已过期或不存在，请重新开始")
    if not (0 <= index < int(meta["chunks"])):
        raise HTTPException(400, f"分片序号越界：{index}")

    # 单片允许比标准片稍大一点（前端按字节切，末片永远更小），但不给"一片
    # 装下整个文件"留口子——那样分片就白做了。
    cap = int(meta["chunk_size"]) + (1 << 20)
    tmp = folder / f"{index}.part.tmp"
    written = 0
    try:
        with tmp.open("wb") as fh:
            async for block in request.stream():
                written += len(block)
                if written > cap:
                    raise HTTPException(413, "单片超过大小上限")
                fh.write(block)
        if written == 0:
            raise HTTPException(400, "分片是空的")
        os.replace(tmp, folder / f"{index}.part")
    except BaseException:
        _quiet_unlink(tmp)
        raise

    meta["updated_at"] = time.time()
    _write_meta(folder, meta)
    return {"received": len(_received_parts(folder)), "chunks": int(meta["chunks"])}


@app.post("/api/upload/{upload_id}/finish")
def upload_finish(upload_id: str) -> dict:
    """按序合并，落地成一份 staged 文件。

    总字节数必须等于 init 时声明的 size：不等就说明少片或有多余数据，拼出来
    是个**看起来正常、实际被截断**的文件——这种文件进了库比导入失败糟得多，
    检索永远少一截，还没有任何迹象。
    """
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)
    if not meta:
        raise HTTPException(404, "上传已过期或不存在，请重新开始")

    chunks = int(meta["chunks"])
    got = _received_parts(folder)
    missing = [i for i in range(chunks) if i not in set(got)]
    if missing:
        raise HTTPException(
            400, f"还缺 {len(missing)} 片（第一个是第 {missing[0] + 1} 片）"
        )

    name = Path(str(meta.get("name") or "upload.bin")).name
    # 目录名带 uuid 前缀：同名文件重复上传不会互相覆盖，而 display_title 会
    # 把这个前缀去掉，界面上看到的还是原名
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
    tmp = target.with_name(target.name + ".part")
    total = 0
    try:
        with tmp.open("wb") as out:
            for i in range(chunks):
                with (folder / f"{i}.part").open("rb") as fh:
                    shutil.copyfileobj(fh, out, _UPLOAD_CHUNK)
                total += (folder / f"{i}.part").stat().st_size
        if total != int(meta["size"]):
            raise HTTPException(
                400,
                f"合并后 {total} 字节，与声明的 {meta['size']} 不符，请重传",
            )
        os.replace(tmp, target)
    except BaseException:
        _quiet_unlink(tmp)
        raise
    _discard_parts(folder)
    return {"staged": str(target), "name": name, "size": total}


@app.delete("/api/upload/{upload_id}")
def upload_abort(upload_id: str) -> dict:
    folder = _upload_dir(upload_id)
    _discard_parts(folder)
    return {"ok": True}


@app.post("/api/import/upload")
async def import_upload(
    kb_id: int,
    file: UploadFile | None = None,
    plan: str | None = Form(None),
    staged: str | None = Form(None),
) -> dict:
    parsed = plan_from_dict(_parse_plan(plan))
    try:
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if staged:
        # 预览已经把字节收过了，别再传一次
        dest = Path(staged)
        if not _inside(dest, UPLOAD_DIR) or not dest.exists():
            raise HTTPException(400, "预览用的文件已失效，请重新选择文件")
        title = display_title(str(dest))
    else:
        if file is None:
            raise HTTPException(400, "没有收到文件")
        name = Path(file.filename or "upload.bin").name
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
        await _save_upload(file, dest, state.cfg.ingest.max_file_mb)
        title = name
    # 这里**故意不删**落盘副本：document.source_uri 指向它，删了就等于
    # 让这个文档永远"重索引失败"（之前正是这么做的，用户点重索引必然报
    # 文件不存在）。上传文件本来也不大，留着当代价换可重索引是划算的。
    unlink_on_fail = not staged  # 预览阶段的落盘副本已确认要导入，失败也留着
    cfg = state.effective_cfg(kb_id)

    def run(progress):
        try:
            result = state.pipeline.import_path(
                kb_id, dest, cfg, progress=progress, clean=parsed
            ).as_dict()
        except Exception:
            if unlink_on_fail:
                _quiet_unlink(dest)  # 连文档行都没建起来，留个孤儿文件没意义
            raise
        # 标题由流水线按落盘名去前缀生成（见 pipeline.display_title），
        # 这样重索引时也不会退回带 uuid 的版本。
        result["source_uri"] = str(dest)
        return result

    # 立刻返回 task_id：文件字节已经收完了，剩下的解析/向量化放到后台线程，
    # 前端逐帧轮询进度。同步跑完再返回的话，这段时间只能给一个假的转圈。
    return _start_import_task(title, run)


@app.post("/api/doc/{doc_id}/source")
async def restore_source(doc_id: int, file: UploadFile) -> dict:
    """给丢失了原始文件的文档补回文件，并立即重索引。

    起因：上传副本曾被删掉，`source_uri` 成了死路径，重索引 100% 失败。
    对这批历史文档，除了重新导入（等于换一个新 doc_id、丢掉已有的引用编号），
    更合理的是把文件放回原位。
    """
    row = state.conn.execute(
        "SELECT kb_id, source_type, source_uri, title, clean_json FROM document WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    if row["source_type"] != "file":
        raise HTTPException(400, "只有文件来源需要补原始文件；粘贴/URL 内容请重新导入")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(row["source_uri"] or "")
    if not target.name or not _inside(target, UPLOAD_DIR):
        # 原路径在 uploads 之外（手工导入的外部文件被挪走了）→ 收进 uploads，
        # 保证以后重索引有个稳定副本，再把 source_uri 改过来。
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{Path(file.filename or 'upload.bin').name}"
        state.conn.execute(
            "UPDATE document SET source_uri=? WHERE id=?", (str(target), doc_id)
        )
    n_bytes = await _save_upload(file, target, state.cfg.ingest.max_file_mb)
    if n_bytes == 0:
        # 空文件没有任何索引价值，但刚落下的空副本要清掉，否则 source_uri
        # 指向一个 0 字节文件，重索引会报"未检测到文本层"这种误导性原因。
        _quiet_unlink(target)
        raise HTTPException(400, "文件是空的")
    state.conn.execute("UPDATE document SET content_hash='' WHERE id=?", (doc_id,))
    kb_id = int(row["kb_id"])
    cfg = state.effective_cfg(kb_id)

    def run(progress):
        result = state.pipeline.import_path(
            kb_id, target, cfg, progress=progress, clean=_stored_plan(row["clean_json"])
        ).as_dict()
        result["source_uri"] = str(target)
        logbus.note(
            f"补回原始文件 doc#{doc_id} {target.name}：{result['message']}",
            level="INFO" if result["status"] == "ready" else "ERROR",
            source="ingest",
        )
        return result

    return _start_import_task(f"补回 {row['title'] or target.name}", run)


@app.post("/api/doc/{doc_id}/reindex")
def reindex(doc_id: int, req: ReindexReq | None = None) -> dict:
    row = state.conn.execute(
        "SELECT source_type, source_uri, kb_id, title, clean_json FROM document WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    if row["source_type"] != "file":
        raise HTTPException(400, "只有文件来源支持重新索引；粘贴内容请删除后重新导入")
    if _source_missing(row["source_type"], row["source_uri"] or ""):
        # 409 而不是 500：这不是系统故障，是"源文件没了"，前端据此改成重新上传入口
        return JSONResponse(
            status_code=409,
            content={
                "error": f"原始文件已不在：{row['source_uri']}",
                "missing_source": True,
                "doc_id": doc_id,
                "source_uri": row["source_uri"],
                "hint": "点「重新上传」选同一个文件，会放回原位并重新索引",
            },
        )
    # 不传 plan = 沿用上次那套清洗规则：重索引悄悄把清洗撤掉，等于库里的内容
    # 跟用户当初确认过的那份不是一回事，而且没有任何提示。
    plan = req.plan if (req and req.plan is not None) else _stored_plan(row["clean_json"])
    try:
        parsed = plan_from_dict(plan)
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    kb_id = int(row["kb_id"])
    cfg = state.effective_cfg(kb_id)
    state.conn.execute("UPDATE document SET content_hash='' WHERE id=?", (doc_id,))

    def run(progress):
        result = state.pipeline.import_path(
            kb_id, row["source_uri"], cfg, progress=progress, clean=parsed
        ).as_dict()
        logbus.note(f"重索引 {doc_id}：{result['message']}", source="ingest")
        return result

    return _start_import_task(f"重索引 {row['title'] or doc_id}", run)


def _stored_plan(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _reindex_kb(kb_id: int) -> dict | None:
    """重建整个库的索引（分块参数改了之后自动调用）。

    走的是同一个串行任务队列：不管库里有 3 篇还是 30 篇，都只占一个队列位置，
    前端也只有一张进度卡。逐个文档起任务会让任务坞瞬间堆满卡片，反而看不出
    整体进度。
    """
    rows = state.conn.execute(
        "SELECT id, title, source_type, source_uri, clean_json FROM document"
        " WHERE kb_id=? ORDER BY id", (kb_id,)
    ).fetchall()
    targets = [
        r for r in rows
        if r["source_type"] == "file"
        and not _source_missing(r["source_type"], r["source_uri"] or "")
    ]
    if not targets:
        return None

    skipped = len(rows) - len(targets)

    def run(progress):
        conn = state.conn      # worker 线程：必须重新取（state.conn 是 thread-local）
        cfg = state.effective_cfg(kb_id)
        n = len(targets)
        done = failed = 0
        chunks = 0
        notes: list[str] = []
        for i, r in enumerate(targets):
            title = r["title"] or f"doc#{r['id']}"
            progress("重建索引", i / n, f"{i + 1}/{n} · {title}")

            def sub(stage, pct, note=""):
                progress(stage, (i + max(0.0, min(1.0, float(pct)))) / n,
                         f"{i + 1}/{n} · {note or title}")

            try:
                conn.execute("UPDATE document SET content_hash='' WHERE id=?", (r["id"],))
                result = state.pipeline.import_path(
                    kb_id, r["source_uri"], cfg, progress=sub,
                    # 沿用这篇自己的清洗方案：改分块参数触发的重建不该顺手
                    # 把清洗撤掉
                    clean=_stored_plan(r["clean_json"]),
                ).as_dict()
                if result.get("status") == "ready":
                    done += 1
                    chunks += int(result.get("n_chunks") or 0)
                else:
                    failed += 1
                    notes.append(f"{title}：{result.get('message') or '失败'}")
            except Exception as exc:                      # 一篇炸了不能带走整批
                failed += 1
                notes.append(f"{title}：{type(exc).__name__}: {exc}")
        msg = f"重建完成：{done} 篇 / {chunks} 块" + (f"，{failed} 篇失败" if failed else "")
        if skipped:
            msg += f"（{skipped} 篇非文件或源文件缺失，已跳过）"
        logbus.note(f"知识库 #{kb_id} 重建索引：{msg}",
                    level="INFO" if not failed else "WARNING", source="ingest")
        return {
            "status": "ready" if done else "failed",
            "message": msg,
            "n_chunks": chunks,
            "notes": notes[:10],
        }

    started = _start_import_task(f"重建索引 · 库 #{kb_id}", run)
    started["docs"] = len(targets)
    started["skipped"] = skipped
    return started


@app.get("/api/doc/{doc_id}/preview")
def preview_doc(doc_id: int, offset: int = 0, limit: int = 200) -> dict:
    """文档预览：元信息 + 当前代次的 chunk 列表（分页）。

    只读 `active_gen` 那一代：重索引过程中新旧两代同时存在，混着展示会把
    同一段文字显示两遍，用户会以为切分出了重复块。

    为什么必须分页而不是"最多 500 块"：一本 40 万字的书能切出 1280 块，
    硬截断之后预览只显示前 39%，用户看到的是"我的文件没全进去"——而库里
    其实一块不少。截断本身不是问题，**静默**截断才是。
    """
    conn = state.conn
    doc = conn.execute(
        "SELECT d.id, d.kb_id, d.title, d.source_type, d.source_uri, d.extractor,"
        " d.status, d.fail_reason, d.n_pages, d.n_chunks, d.active_gen,"
        " d.imported_at, d.updated_at, k.name AS kb_name"
        " FROM document d LEFT JOIN kb k ON k.id = d.kb_id WHERE d.id=?",
        (doc_id,),
    ).fetchone()
    if doc is None:
        raise HTTPException(404, "文档不存在")

    cap = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    rows = conn.execute(
        "SELECT id, seq, text, token_count, page_no, heading_path, char_start, is_hidden"
        " FROM chunk WHERE doc_id=? AND generation=?"
        " ORDER BY seq LIMIT ? OFFSET ?",
        (doc_id, int(doc["active_gen"]), cap, off),
    ).fetchall()

    items = [
        {
            "id": r["id"],
            "seq": r["seq"],
            "text": r["text"],
            "chars": len(r["text"] or ""),
            "tokens": r["token_count"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
            "char_start": r["char_start"],
            "hidden": bool(r["is_hidden"]),
        }
        for r in rows
    ]
    total_chars = sum(it["chars"] for it in items)
    total = int(doc["n_chunks"] or 0)
    return {
        "doc": dict(doc),
        "chunks": items,
        "total": total,
        "shown": len(items),
        "offset": off,
        "loaded": off + len(items),
        "has_more": off + len(items) < total,
        "truncated": total > off + len(items),
        "chars": total_chars,
        "tokens": sum(int(it["tokens"] or 0) for it in items),
    }


@app.get("/api/doc/{doc_id}/coverage")
def doc_coverage(doc_id: int) -> dict:
    """完整性自检：源文件里的文字，有多少真的进了库。

    "文件是不是全进去了"这个问题不能靠感觉回答。做法是把源文件重新抽一遍
    拿到原文，再用滑动窗口检查每个窗口是否出现在当前代次的 chunk 拼接里，
    未命中的连续区间就是要给用户看的具体证据（而不只是一个百分比）。

    只比对空白归一化后的文本：换行/缩进差异不是内容丢失。
    """
    conn = state.conn
    if conn.execute("SELECT id FROM document WHERE id=?", (doc_id,)).fetchone() is None:
        raise HTTPException(404, "文档不存在")
    return _coverage_report(doc_id)


def _coverage_report(doc_id: int) -> dict:
    """算一份覆盖率结论（约 100ms，源文件还在就能算）。

    抽成函数是因为它有两处调用：手动点「完整性自检」，以及**导入/重索引任务
    结束时自动跑一次**。后者才是关键 —— 让人手动去点一个藏得很深的按钮，
    等于把"到底导全没有"继续留给感觉回答。
    """
    conn = state.conn
    doc = conn.execute(
        "SELECT id, title, source_type, source_uri, active_gen, status, extractor,"
        " content_hash, clean_json FROM document WHERE id=?", (doc_id,),
    ).fetchone()
    if doc is None:
        return {"ok": False, "reason": "文档不存在"}
    if doc["status"] != "ready":
        return {"ok": False, "reason": f"文档状态是 {doc['status']}，先成功索引再校验"}
    if _source_missing(doc["source_type"], doc["source_uri"] or ""):
        return {"ok": False, "reason": "源文件已不在原路径，无法比对（重新上传后可校验）"}

    try:
        loaded = loader_mod.load_path(
            doc["source_uri"],
            min_pdf_text_chars=state.cfg.ingest.pdf.min_text_chars_per_page,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"重新抽取源文件失败：{type(exc).__name__}: {exc}"}

    # 清洗过的文档，库里存的是"洗完之后"的文本。不先跑同一套规则就比对，
    # 每篇都会报成"源文件改过"——那句提示会彻底失去意义。
    plan = plan_from_dict(_stored_plan(doc["clean_json"]))
    cleaned_note = None
    if plan.any_enabled():
        loaded, report = clean_loaded(loaded, plan)
        cleaned_note = report.as_dict()

    rows = conn.execute(
        "SELECT text FROM chunk WHERE doc_id=? AND generation=? ORDER BY seq",
        (doc_id, int(doc["active_gen"])),
    ).fetchall()
    cov = _coverage_of(loaded.text, [r["text"] or "" for r in rows])
    # 源文件在导入之后被改过，是"覆盖不全"最常见的解释——不是切分丢了，
    # 而是库里存的是上一版。不区分这两者，用户会以为系统把内容吃掉了。
    now_hash = hashlib.sha256(loaded.text.encode("utf-8")).hexdigest()
    stale_source = bool(doc["content_hash"]) and now_hash != doc["content_hash"]
    cov["stale_source"] = stale_source
    cov["cleaned"] = cleaned_note
    return {
        **{"ok": True}, **cov,
        # 报"源文件字数"还是"清洗后字数"：后者才是库里应有的量，前者会让人
        # 以为清洗删掉的部分是"没导进去"
        "source_chars": len(loaded.text),
        "extractor": loaded.extractor,
        "doc_extractor": doc["extractor"],
    }


def _coverage_digest(doc_id: int) -> dict | None:
    """任务结束时附在结果里的那一小段结论：能算就算，算不了也不许拖累任务。"""
    try:
        r = _coverage_report(doc_id)
    except Exception:
        return None
    if not r.get("ok"):
        return None
    return {
        "ratio": r.get("ratio"),
        "big_gap_count": r.get("big_gap_count") or 0,
        "max_gap": r.get("max_gap") or 0,
        "stale_source": bool(r.get("stale_source")),
        "source_chars": r.get("source_chars") or 0,
        "chunks_chars": r.get("checked") or 0,
    }


def _cmp_norm(text: str) -> str:
    """比对用的归一化：清洗 + 去空白 + 去 Markdown 标记。

    三件事都在消除"看起来不一样、其实一个字没少"的假警报：
      · repair —— 切分本就会清掉控制字符与超长空白，那是清洗不是丢失；
      · 空白 —— 换行缩进差异不是内容差异；
      · `#|*~>` —— 抽取时标题的 `##` 会被摘掉、表格会重排成 `列 | 列`，
        若不忽略这些标记，每个标题行和表格行都会被报成"缺了一段"，
        用户看到的是满屏假缺口，反而不知道到底缺没缺。
    """
    text = repair_chunk_primitives(text or "")
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[#|*~>`]", "", text)


def _coverage_of(source: str, chunks: list[str], k: int = 24) -> dict:
    """返回原文被 chunk 覆盖的比例与未覆盖区间样本。"""
    src = _cmp_norm(source)
    joined = _cmp_norm("".join(chunks))
    if not src:
        return {"ratio": 1.0, "gaps": [], "gap_chars": 0, "checked": 0}
    seen = {joined[i : i + k] for i in range(0, max(0, len(joined) - k + 1))}

    gaps: list[dict] = []
    gap_chars = 0
    i = 0
    n = len(src)
    while i < n:
        w = src[i : i + k]
        if len(w) < k:  # 尾巴不足一个窗口，用能比的部分兜底
            w = src[i:]
            if not w or w in joined:
                break
            gaps.append({"start": i, "end": n, "text": source[i : i + 60]})
            gap_chars += len(w)
            break
        if w in seen:
            i += k
            continue
        j = i + 1
        while j < n and src[j : j + k] not in seen:
            j += 1
        end = min(j + k, n)
        gap_chars += end - i
        gaps.append({"start": i, "end": end, "text": src[max(0, i - 10) : end + 10][:120]})
        i = end + k
    covered = max(0, n - gap_chars)
    # 短缺口基本都是页眉页码、标记符号这类"顺序/清洗"差异，不是真丢了内容。
    # 只有成段（>= 60 字）的缺口才值得当成证据摆给用户，其余只进百分比。
    big = [g for g in gaps if g["end"] - g["start"] >= 60]
    return {
        "ratio": round(covered / n, 4) if n else 1.0,
        "gap_chars": gap_chars,
        "checked": n,
        "gaps": big[:5],
        "gap_count": len(gaps),
        "big_gap_count": len(big),
        "max_gap": max((g["end"] - g["start"] for g in gaps), default=0),
    }


@app.delete("/api/doc/{doc_id}")
def delete_doc(doc_id: int) -> dict:
    """删除走**后台任务**：几千块的文档要逐条清 FTS + 级联删 chunk，前端
    不能干等一个不返回的请求。任务进度跟导入/重索引同一条链路。"""
    conn = state.conn  # 请求线程自己的连接，只用来查这一下行元信息
    row = conn.execute(
        "SELECT title, source_type, source_uri FROM document WHERE id=?", (doc_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "文档不存在")
    label = row["title"] or f"doc#{doc_id}"

    def run(progress) -> dict:
        # state.conn 是 thread-local 的：任务跑在 worker 线程，必须在这里重新取，
        # 直接复用上面请求线程的连接会报 "created in a thread can only be used
        # in that same thread"。
        conn = state.conn
        ids = [r["id"] for r in conn.execute("SELECT id FROM chunk WHERE doc_id=?", (doc_id,))]
        total = len(ids)
        if total:
            # 分批清 FTS：一次 executemany 三千条没问题，但分批才能报出真进度
            B = 800
            for i in range(0, total, B):
                state.sparse.delete(conn, ids[i : i + B])
                done = min(i + B, total)
                progress("清理索引", 0.05 + 0.65 * done / total, f"{done}/{total} 块")
        progress("删除文档", 0.8)
        conn.execute("DELETE FROM document WHERE id=?", (doc_id,))  # chunk 级联删除
        state.vectors.invalidate()
        # 上传副本要跟着走：留着就是纯垃圾（没有 document 行指向它，重索引也用不上）。
        # 只删 uploads 里的，外部路径（手工导入的源文件）绝对不能碰。
        removed_copy = False
        if row["source_type"] == "file" and row["source_uri"]:
            p = Path(row["source_uri"])
            if _inside(p, UPLOAD_DIR):
                removed_copy = _quiet_unlink(p)
        progress("完成", 1.0)
        return {
            "status": "ready",
            "message": f"已删除{f' {total} 块' if total else ''}" + ("，源文件副本已清理" if removed_copy else ""),
            "deleted": doc_id,
        }

    return _start_import_task(f"删除 {label}", run)


@app.get("/api/chunks")
def get_chunks(ids: str) -> list[dict]:
    """批量取原文。检索面板要同时展示稠密/稀疏/融合三组结果，逐条请求是 N 次往返，
    而这里一次就够（面板一屏最多几十条）。"""
    parsed: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if part.isdigit():
            parsed.append(int(part))
    if not parsed:
        return []
    parsed = parsed[:80]
    placeholders = ",".join("?" for _ in parsed)
    rows = state.conn.execute(
        f"SELECT c.id, c.doc_id, c.seq, c.text, c.page_no, c.heading_path, c.token_count,"
        f" d.title AS doc_title, d.source_uri"
        f" FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id IN ({placeholders})",
        parsed,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "doc_id": r["doc_id"],
            "doc_title": r["doc_title"],
            "source_uri": r["source_uri"],
            "seq": r["seq"],
            "text": r["text"],
            "chars": len(r["text"] or ""),
            "tokens": r["token_count"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
        }
        for r in rows
    ]


@app.get("/api/chunk/{chunk_id}")
def get_chunk(chunk_id: int) -> dict:
    row = state.conn.execute(
        """
        SELECT c.id, c.doc_id, c.seq, c.text, c.page_no, c.heading_path, c.token_count,
               c.is_hidden, d.title, d.source_uri
        FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id=?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "chunk 不存在（可能已因重新索引被替换）")
    return dict(row)


# ---------------------------------------------------------------- chunk 编辑
#
# 手动编辑 chunk 是"检索质量最后一道人工阀门"：自动分块一定会切坏一些东西
# （表格被切断、标题与正文分家、代码块被拦腰截断）。与其逼用户去调参数再整体
# 重建，不如允许就地改一条——改完只重算这一条的 FTS 与向量，代价是毫秒级。


def _reindex_chunk(conn, chunk_id: int, text: str) -> None:
    """改一条 chunk 的文本，并同步 text_seg / content_hash / 嵌入 / FTS。"""
    import hashlib as _hashlib

    from ..ingest.chunker import estimate_tokens

    text = text.strip()
    if not text:
        raise HTTPException(400, "chunk 内容不能为空")

    digest = _hashlib.sha256(text.encode("utf-8")).hexdigest()
    seg = state.sparse.segment(text)
    conn.execute(
        "UPDATE chunk SET text=?, text_seg=?, token_count=?, content_hash=?, is_hidden=0"
        " WHERE id=?",
        (text, seg, estimate_tokens(text), digest, chunk_id),
    )
    state.sparse.delete(conn, [chunk_id])
    state.sparse.index(conn, [(chunk_id, text)])

    model_key = f"{state.cfg.embedding.model}@{state.cfg.embedding.dim}"
    has = conn.execute(
        "SELECT 1 FROM embedding WHERE content_hash=? AND model=?", (digest, model_key)
    ).fetchone()
    if not has and state.embedder.available:
        import numpy as np

        vec = state.embedder.encode([text])[0]
        conn.execute(
            "INSERT OR REPLACE INTO embedding (content_hash, model, dim, vec) VALUES (?,?,?,?)",
            (digest, model_key, int(state.cfg.embedding.dim),
             np.asarray(vec, dtype=np.float32).tobytes()),
        )
    state.vectors.invalidate()


def _active_chunk(conn, chunk_id: int):
    row = conn.execute(
        "SELECT c.*, d.active_gen FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "chunk 不存在")
    if row["generation"] != row["active_gen"]:
        raise HTTPException(409, "该 chunk 属于旧代次，已被重新索引替换，不能编辑")
    return row


def _renumber(conn, doc_id: int, generation: int, order: list[int] | None = None) -> None:
    """按给定顺序重排 seq。

    UNIQUE(doc_id, generation, seq) 意味着边改边写一定会撞，所以先整体写负数
    占位、再写正数落位——两步都不会出现重复值。
    """
    if order is None:
        order = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM chunk WHERE doc_id=? AND generation=? ORDER BY seq, id",
                (doc_id, generation),
            ).fetchall()
        ]
    # 占位值必须远离任何可能存在的 seq（新插入的块用 -1 占位），否则第一轮写入
    # 就会自己跟自己撞唯一约束
    base = -1_000_000
    conn.executemany(
        "UPDATE chunk SET seq=? WHERE id=?",
        [(base - i, cid) for i, cid in enumerate(order)],
    )
    conn.executemany("UPDATE chunk SET seq=? WHERE id=?", [(i, cid) for i, cid in enumerate(order)])
    visible = conn.execute(
        "SELECT count(*) c FROM chunk WHERE doc_id=? AND generation=? AND is_hidden=0",
        (doc_id, generation),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE document SET n_chunks=?, updated_at=? WHERE id=?",
        (int(visible), _utcnow(), doc_id),
    )


class ChunkPatch(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.patch("/api/chunk/{chunk_id}")
def patch_chunk(chunk_id: int, req: ChunkPatch) -> dict:
    conn = state.conn
    _active_chunk(conn, chunk_id)
    _reindex_chunk(conn, chunk_id, req.text)
    return {"ok": True, "chunk_id": chunk_id, "chars": len(req.text.strip())}


class ChunkSplit(BaseModel):
    offset: int | None = Field(default=None, ge=1)


@app.post("/api/chunk/{chunk_id}/split")
def split_chunk(chunk_id: int, req: ChunkSplit) -> dict:
    conn = state.conn
    row = _active_chunk(conn, chunk_id)
    text = row["text"]
    offset = req.offset or len(text) // 2
    if not (1 <= offset < len(text)):
        raise HTTPException(400, f"切分位置超出范围（1..{len(text)}）")

    left, right = text[:offset].rstrip(), text[offset:].lstrip()
    if not left or not right:
        raise HTTPException(400, "切分后有一半为空，换个位置")

    _reindex_chunk(conn, chunk_id, left)
    # 先给新块一个临时的负 seq（避开唯一约束），落库后再按顺序整体重排
    cur = conn.execute(
        "INSERT INTO chunk (doc_id, generation, seq, text, text_seg, token_count,"
        " page_no, heading_path, char_start, char_end, content_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["doc_id"], row["generation"], -1, right, "", 0,
         row["page_no"], row["heading_path"], None, None, ""),
    )
    new_id = int(cur.lastrowid)
    _reindex_chunk(conn, new_id, right)

    order = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM chunk WHERE doc_id=? AND generation=? AND id<>? ORDER BY seq, id",
            (row["doc_id"], row["generation"], new_id),
        ).fetchall()
    ]
    order.insert(order.index(chunk_id) + 1, new_id)
    _renumber(conn, row["doc_id"], row["generation"], order)
    return {"ok": True, "left_id": chunk_id, "right_id": new_id}


class ChunkMerge(BaseModel):
    ids: list[int] = Field(min_length=2, max_length=20)


@app.post("/api/chunk/merge")
def merge_chunks(req: ChunkMerge) -> dict:
    conn = state.conn
    rows = [_active_chunk(conn, cid) for cid in req.ids]
    doc_ids = {int(r["doc_id"]) for r in rows}
    gens = {int(r["generation"]) for r in rows}
    if len(doc_ids) > 1 or len(gens) > 1:
        raise HTTPException(400, "只能合并同一文档、同一代次的 chunk")

    rows.sort(key=lambda r: (r["seq"], r["id"]))
    merged = "\n\n".join(r["text"].strip() for r in rows)
    keep = int(rows[0]["id"])
    _reindex_chunk(conn, keep, merged)

    drop = [int(r["id"]) for r in rows[1:]]
    state.sparse.delete(conn, drop)
    conn.execute(
        f"UPDATE chunk SET is_hidden=1 WHERE id IN ({','.join('?' for _ in drop)})", drop
    )
    _renumber(conn, rows[0]["doc_id"], rows[0]["generation"])
    return {"ok": True, "chunk_id": keep, "merged": len(rows)}


@app.delete("/api/chunk/{chunk_id}")
def delete_chunk(chunk_id: int) -> dict:
    """软删除：置 is_hidden=1 并从 FTS 摘掉。

    硬删会让同一代次里的 seq 出现空洞，而 seq 是"引用位置"的锚点；
    软删除同时保留回滚可能——用户手滑删错了，改数据库就能救回来。
    """
    conn = state.conn
    row = _active_chunk(conn, chunk_id)
    conn.execute("UPDATE chunk SET is_hidden=1 WHERE id=?", (chunk_id,))
    state.sparse.delete(conn, [chunk_id])
    _renumber(conn, row["doc_id"], row["generation"])
    state.vectors.invalidate()
    return {"ok": True, "chunk_id": chunk_id, "hidden": True}


# ------------------------------------------------------------------- 检索调试


@app.post("/api/search/debug")
def search_debug(req: DebugRequest) -> dict:
    started = time.perf_counter()
    cfg = state.effective_cfg(req.kb_id)
    result = state.engine.search(
        state.conn, req.question, kb_id=req.kb_id, top_k=req.top_k, rerank=req.rerank,
        filters=req.filters(), cfg=cfg,
    )
    payload = _debug_payload(result, cfg, state.tuned(req.kb_id))
    payload["total_ms"] = int((time.perf_counter() - started) * 1000)
    return payload


def _debug_chunk_info(conn, chunk_ids: list[int]) -> dict[int, dict]:
    """批量取 chunk 的可读信息（文档名 / 章节路径 / 原文摘要）。

    调试面板如果只给 `chunk 2773 · 0.424`，用户只能看到一排编号和条形图——
    什么结论都得自己在脑内补。一次 JOIN 把 dense+sparse+fused 涉及的
    全部 id（最多 ~100 个）取回来，约几毫秒，不该省。
    """
    ids = sorted({int(c) for c in chunk_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.id, d.title AS doc_title, c.page_no, c.heading_path, c.text
        FROM chunk c JOIN document d ON d.id = c.doc_id
        WHERE c.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {
        r["id"]: {
            "doc_title": r["doc_title"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
            "preview": (r["text"] or "").strip().replace("\n", " ")[:120],
        }
        for r in rows
    }


def _debug_payload(result, cfg: Config | None = None, overrides: dict | None = None) -> dict:
    """一次检索的结果同时服务"回答"和"调试"两个消费者，绝不为了调试再跑一遍。"""
    cfg = cfg or state.cfg
    info = _debug_chunk_info(
        state.conn,
        [h.chunk_id for h in result.dense_hits] + [h.chunk_id for h in result.sparse_hits],
    )

    def readable(chunk_id: int) -> dict:
        # 查不到（文档刚删、跨库脏 id）时别让前端渲染出 undefined
        return info.get(
            int(chunk_id),
            {"doc_title": "?", "page_no": None, "heading_path": "", "preview": ""},
        )

    return {
        "query": result.query,
        "params": {
            "dense_top_k": cfg.retrieval.dense.top_k,
            "sparse_top_k": cfg.retrieval.sparse.top_k,
            "rrf_k": cfg.retrieval.fusion.k,
            "fusion_method": cfg.retrieval.fusion.method,
            "w_dense": cfg.retrieval.fusion.w_dense,
            "w_sparse": cfg.retrieval.fusion.w_sparse,
            "fusion_top_k": cfg.retrieval.fusion.top_k,
            "rerank_model": result.rerank_model,
            "rerank_used": result.rerank_used,
            "refusal_min_score": cfg.refusal.min_score,
            "refusal_min_margin": cfg.refusal.min_margin,
            "kb_overrides": len(overrides or {}),
        },
        "stages": {
            "dense": [
                {**readable(h.chunk_id), "chunk_id": h.chunk_id,
                 "score": round(h.score, 4), "rank": h.rank}
                for h in result.dense_hits
            ],
            "sparse": [
                {**readable(h.chunk_id), "chunk_id": h.chunk_id,
                 "score": round(h.score, 4), "rank": h.rank}
                for h in result.sparse_hits
            ],
            "fused": [
                {**readable(f["chunk_id"]), "chunk_id": f["chunk_id"],
                 "rrf": round(f["rrf"], 6),
                 "dense_rank": f["dense_rank"],
                 "sparse_rank": f["sparse_rank"]}
                for f in result.fused
            ],
        },
        "final": [
            {
                "n": c.final_rank,
                "chunk_id": c.chunk_id,
                "doc_title": c.doc_title,
                "page_no": c.page_no,
                "heading_path": c.heading_path,
                "dense_rank": c.dense_rank,
                "sparse_rank": c.sparse_rank,
                "dense_score": round(c.dense_score, 4) if c.dense_score is not None else None,
                "sparse_score": round(c.sparse_score, 4) if c.sparse_score is not None else None,
                "rrf": round(c.rrf_score, 6),
                "rerank_score": round(c.rerank_score, 4) if c.rerank_score is not None else None,
                "preview": c.text[:220],
            }
            for c in result.candidates
        ],
        "timings_ms": result.timings,
        "degraded": result.degraded,
        "refusal": {
            "refused": result.refusal.refused if result.refusal else False,
            "reason": result.refusal.reason if result.refusal else None,
            "top_score": result.refusal.top_score if result.refusal else None,
            "margin": result.refusal.margin if result.refusal else None,
            "calibrated": result.refusal.calibrated if result.refusal else False,
            "low_confidence": result.refusal.low_confidence if result.refusal else False,
            "confidence_reason": result.refusal.confidence_reason if result.refusal else None,
        },
    }


# --------------------------------------------------------------------- 问答


def _sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _history(conn, session_id: str, turns: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM message WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, turns * 2),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _context_usage(conn, session_id: str | None, history: list[dict], cfg) -> dict:
    """这一轮到底装了多少历史、还剩多少预算。

    规则本身在 chatctx（不依赖 torch，能单测）；这里只负责把"会话里一共多少条
    消息"查出来喂进去。
    """
    total_msgs = 0
    if session_id:
        row = conn.execute(
            "SELECT count(*) FROM message WHERE session_id=?", (session_id,)
        ).fetchone()
        total_msgs = int(row[0]) if row else 0
    return chatctx.usage(
        history,
        total_messages=total_msgs,
        budget=int(cfg.generation.context_token_budget),
        turns_limit=int(cfg.chat.history_turns),
        min_chars=int(cfg.generation.min_context_chars),
    )


@app.post("/api/ask")
def ask(req: AskRequest) -> Response:
    trace_id = str(uuid.uuid4())
    conn = state.conn
    # 这个库自己的一套参数（没有覆盖时就是全局对象本身，零开销）
    kb_cfg = state.effective_cfg(req.kb_id)
    # 对话页「高级参数」里的临时覆盖：只在本次请求生效，不写盘。
    # 借 kbconfig 那条链路是为了白名单和类型校验都只有一份实现。
    if req.overrides:
        kb_cfg = kbconfig.effective(kb_cfg, req.overrides)
    kb_overrides = state.tuned(req.kb_id)

    session_id = req.session_id
    if session_id:
        exists = conn.execute("SELECT id FROM session WHERE id=?", (session_id,)).fetchone()
        if not exists:
            now = _utcnow()
            conn.execute(
                "INSERT INTO session (id, kb_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
                (session_id, req.kb_id, req.question[:30], now, now),
            )
    history = _history(conn, session_id, kb_cfg.chat.history_turns) if session_id else []
    # 轮数只是第一道闸：5 轮长回答照样能把资料预算吃光。再按字符预算裁一次，
    # 从最早一轮开始整轮丢——半截对话比没有对话更容易引出错误指代。
    history = chatctx.trim_history(history, budget=int(kb_cfg.chat.history_token_budget))
    context_usage = _context_usage(conn, session_id, history, kb_cfg)

    # 本次提问真正要用的后端：对话页下拉选的那个优先，没传才落到库/系统当前后端。
    # 这个值算一次、全程复用——下面提示词、报错指路、探测缓存作废、query_log
    # 全都得指同一个后端。散着写 kb_cfg.generation.provider 的后果是：用户选了
    # deepseek 提问，401 报错却说"后端 siliconflow 密钥无效"，日志里也记成
    # siliconflow，等于"选哪个都是测当前那个"。
    gen_provider = req.provider or kb_cfg.generation.provider
    try:
        _gen_name, _gen_prov = state.llm.resolve(gen_provider)
        gen_model = req.model or _gen_prov.model
    except llm_mod.LLMError:
        # 后端名不存在：不在这里拦（下面调用会给出同一份报错），只把名字留下来
        _gen_name, gen_model = gen_provider, req.model or ""

    def stream() -> Iterator[str]:
        """StreamingResponse 会把同步生成器丢到线程池里迭代，而上面那个 conn 是在
        路由线程里创建的——sqlite3 连接不能跨线程用。所以这里必须重新取连接。

        外面这层 try 是最后一道防线：生成器里抛异常时 HTTP 200 已经发出去了，
        不兜的话前端只会看到"永远转圈"，连错误信息都拿不到。
        """
        try:
            yield from _impl()
        except Exception as exc:
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}", "trace_id": trace_id})

    def _impl() -> Iterator[str]:
        # 嵌入模型还没装进内存时，检索一定会卡在第一次 encode 上（40 秒）。
        # 与其让前端转圈到超时，不如立刻说清"在加载、还差多久"——用户能判断
        # 是等一等还是去查配置，而"一直转圈"什么信息都不给。
        if not state.embedder.is_loaded:
            w = state.warm
            if w["state"] in ("pending", "loading"):
                waited = int((time.time() - w["started_at"]))
                msg = (f"嵌入模型正在后台加载（已 {waited}s，通常 30-60 秒）。"
                       f"状态栏显示「索引就绪」后再问即可。")
            else:
                msg = f"嵌入模型加载失败：{w.get('error') or state.embedder.error or '未知原因'}"
            logbus.note(f"提问被拒：{msg}", level="WARNING", source="ask")
            yield _sse("error", {"message": msg, "trace_id": trace_id})
            return

        conn = state.db.conn()
        started = time.perf_counter()
        timings: dict[str, int] = {}
        if req.direct:
            # 不挂载知识库 + 用户选了「直接问模型」：检索整段跳过。
            # 这里必须是"一条空结果"而不是 None——下面拒答判定、引用校验、
            # 计时统计全都按 result 走，空对象能让它们自然退化成"没资料"。
            result = RetrievalResult(query=req.question, candidates=[], timings=timings)
        else:
            result = state.engine.search(
                conn,
                req.question,
                kb_id=req.kb_id,
                top_k=req.top_k,
                rerank=req.rerank,
                rerank_model=req.rerank_model,
                mmr=req.mmr,
                filters=req.filters(),
                cfg=kb_cfg,
            )
        timings.update(result.timings)

        if result.refusal and result.refusal.refused:
            payload_meta = {
                "trace_id": trace_id,
                "mode": "refuse",
                "reason": result.refusal.reason,
                "calibrated": result.refusal.calibrated,
                "candidates": len(result.candidates),
                "degraded": result.degraded,
                "timings_ms": timings,
                "context": context_usage,
                "debug": _debug_payload(result, kb_cfg, kb_overrides),
            }
            yield _sse("meta", payload_meta)
            for i in range(0, len(REFUSE_TEMPLATE), 12):
                yield _sse("delta", {"text": REFUSE_TEMPLATE[i : i + 12]})
            msg_id = (
                _save(conn, session_id, req.question, REFUSE_TEMPLATE, trace_id, [])
                if session_id else None
            )
            yield _sse("done", {
                "trace_id": trace_id, "refused": True,
                "provider": gen_provider, "model": gen_model,
                "citations": [],
                "cite_coverage": 0.0, "invalid_ids": [], "degraded": result.degraded,
                "answer": REFUSE_TEMPLATE,
                "message_id": msg_id,
            })
            _log(conn, trace_id, req, result, timings, "refuse", 0, 0, 0.0, history_len=len(history), provider=gen_provider)
            return

        bundle = (
            build_direct(req.question, history) if req.direct
            else build_context(
                result.candidates,
                question=req.question,
                token_budget=kb_cfg.generation.context_token_budget,
                min_context_chars=kb_cfg.generation.min_context_chars,
                prompt_version=state.cfg.prompt_version,
                history_text_tokens=sum(len(h["content"]) for h in history),
                # 这个后端待会儿真的要拿去生成，提示词就得取它那份覆盖。
                # 取成"系统当前后端"的话，界面切到别的后端后提示词会张冠李戴。
                provider=gen_provider,
            )
        )
        yield _sse("meta", {
            "trace_id": trace_id,
            "mode": "direct" if req.direct else "answer",
            "citations": [asdict(c) for c in bundle.citations],
            "context_used": bundle.used,
            "context_dropped": bundle.dropped,
            "context": context_usage,
            "degraded": result.degraded,
            "timings_ms": timings,
            "confidence": {
                "low": bool(result.refusal.low_confidence) if result.refusal else False,
                "reason": result.refusal.confidence_reason if result.refusal else None,
                "top_score": result.refusal.top_score if result.refusal else None,
            },
            "debug": _debug_payload(result, kb_cfg, kb_overrides),
            # 回显本次真正用的后端与模型：对话页下拉选了 A 却答的是 B，这件事
            # 只有服务端知道，前端不显示就没人能发现"选哪个都没区别"。
            "provider": gen_provider,
            "model": gen_model,
        })

        messages = bundle.messages(req.question, history)
        acc: list[str] = []
        error: str | None = None
        gen_started = time.perf_counter()
        try:
            for piece in state.llm.stream(
                messages,
                provider=gen_provider,
                model=req.model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                acc.append(piece)
                yield _sse("delta", {"text": piece})
        except GeneratorExit:
            # 客户端断开（关标签页／切走／网络掉）。这时回答只生成了一半，token
            # 已经花出去了，却不会再走到下面那个 _log —— query_log 里就没有这次
            # 请求。而它恰好属于"花了钱但没人看完"那一类，统计里漏掉它，等于把
            # 最该被看见的一批藏起来（总量看着正常，成本却对不上账）。
            timings["gen_ms"] = int((time.perf_counter() - gen_started) * 1000)
            try:
                _log(conn, trace_id, req, result, timings, "disconnect", 0, 0, 0.0,
                     len(history), provider=gen_provider)
            except Exception:
                pass  # 记日志失败不能盖住断开信号本身
            raise  # GeneratorExit 必须继续往上抛，否则生成器收不干净
        except LLMError as exc:
            # 原始报错是一串英文（"You didn't provide an API key…"），不说人话
            # 也不指路。补一句"下一步该干什么"，否则用户只知道"它又坏了"。
            error = str(exc)
            hint = llm_mod.human_hint(exc, gen_provider)
            if hint:
                error = f"{error}\n\n{hint}"
            # 调用被拒 = 探测时那把钥匙已经不作数了（换过、或重启后内存里没了）。
            # 缓存不作废的话，模型页会继续显示上一把钥匙探出来的绿灯。
            if getattr(exc, "status", None) in (401, 403):
                services_mod.invalidate(gen_provider)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        gen_ms = int((time.perf_counter() - gen_started) * 1000)
        timings["gen_ms"] = gen_ms

        answer = "".join(acc)
        if error and not answer:
            yield _sse("error", {"message": error, "trace_id": trace_id})
            _log(conn, trace_id, req, result, timings, "error", 0, 0, 0.0, len(history), provider=gen_provider)
            return

        report = cite_mod.verify(answer, len(bundle.citations))
        # 照抄检测：拿本次真正装进提示词的那几块原文比对（bundle.used 才是
        # 模型看到的资料，用它而不是全库，避免"抄了别的块"也算进比例）。
        vr = cite_mod.verbatim_ratio(answer, bundle.used_texts)
        if vr >= 0.6 and not report.blanket:
            report.blanket = f"疑似照抄原文：{vr:.0%} 的句子与资料逐字相同，未归纳作答"
        citations = [asdict(c) for c in bundle.citations]
        msg_id = (
            _save(conn, session_id, req.question, answer, trace_id, citations)
            if session_id else None
        )
        yield _sse("done", {
            "trace_id": trace_id,
            "refused": False,
            "provider": gen_provider,
            "model": gen_model,
            "citations": citations,
            "cite_coverage": report.coverage,
            "cite_blanket": report.blanket,
            "invalid_ids": report.invalid_ids,
            "uncited": report.uncited_sentences,
            "degraded": result.degraded + ([f"llm_error:{error}"] if error else []),
            "answer": answer,
            "message_id": msg_id,
        })
        _log(conn, trace_id, req, result, timings, "ok", 0, 0, report.coverage, len(history), provider=gen_provider)
        _ = started  # 保留给 trace 扩展，避免被 lint 判定未使用

    if not (kb_cfg.generation.stream if req.stream is None else req.stream):
        # stream=false：把同一条链路整条跑完，只取 done 事件的载荷当 JSON 返回。
        #
        # 不给非流式单写一遍"检索 + 生成 + 引用校验"——两份实现必然发散，而
        # 分叉处的差异（拒答判定、引用覆盖率、message_id 落库）恰恰是最难被
        # 发现的地方。这里只是把 SSE 当内部传输格式消费掉，对外是一份完整 JSON。
        #
        # 顺带的好处：走不支持 SSE、或者会在网关处被缓冲截断的客户端时，一次
        # 性响应比流式更可靠。代价是首字延迟变成整段延迟，所以默认仍是流式。
        done_payload: dict | None = None
        meta_payload: dict | None = None
        failure: str | None = None
        for line in stream():
            if line.startswith("event: done"):
                done_payload = json.loads(line.partition("\n")[2][len("data: "):].strip())
            elif line.startswith("event: meta"):
                # meta 里是引用列表、上下文占用、调试信息——非流式客户端同样
                # 需要它们，否则只能拿到一段没有出处的文字
                meta_payload = json.loads(line.partition("\n")[2][len("data: "):].strip())
            elif line.startswith("event: error"):
                failure = json.loads(
                    line.partition("\n")[2][len("data: "):].strip()
                ).get("message", "")
        if done_payload is None:
            raise HTTPException(502, failure or "生成失败，未返回结果")
        done_payload["stream"] = False
        done_payload["meta"] = meta_payload
        return JSONResponse(done_payload, headers={"X-Trace-Id": trace_id})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Trace-Id": trace_id,
                 "X-Accel-Buffering": "no"},
    )


def _save(conn, session_id: str, question: str, answer: str, trace_id: str,
          citations: list[dict]) -> int | None:
    """落库一轮问答，返回 assistant 消息 id —— 前端拿它做"有用/无用"反馈。"""
    now = _utcnow()
    conn.execute(
        "INSERT INTO message (session_id, role, content, trace_id, created_at)"
        " VALUES (?,?,?,?,?)", (session_id, "user", question, trace_id, now),
    )
    cur = conn.execute(
        "INSERT INTO message (session_id, role, content, trace_id, citations_json, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (session_id, "assistant", answer, trace_id,
         json.dumps(citations, ensure_ascii=False), now),
    )
    conn.execute("UPDATE session SET updated_at=? WHERE id=?", (now, session_id))
    return int(cur.lastrowid)


def _log(conn, trace_id: str, req: AskRequest, result, timings: dict, status: str,
         prompt_tokens: int, completion_tokens: int, coverage: float,
         history_len: int, provider: str | None = None) -> None:
    digest = hashlib.sha256(req.question.encode("utf-8")).hexdigest()[:16]
    provider_name = provider or req.provider or state.cfg.generation.provider
    provider_cfg = state.cfg.generation.providers.get(provider_name)
    model_name = provider_cfg.model if provider_cfg else provider_name
    candidates = [
        {
            "chunk_id": c.chunk_id,
            "dense_rank": c.dense_rank,
            "sparse_rank": c.sparse_rank,
            "rrf": round(c.rrf_score, 6),
            "rerank": round(c.rerank_score, 4) if c.rerank_score is not None else None,
        }
        for c in result.candidates[:10]
    ]
    conn.execute(
        """INSERT INTO query_log (trace_id, kb_id, question_digest, question_chars,
             retrieve_ms, rerank_ms, gen_ms, total_ms, prompt_tokens, completion_tokens,
             model, rerank_used, rerank_model, refused, refuse_reason, top_score,
             score_margin, candidates_json, degraded, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            trace_id, req.kb_id, digest, len(req.question),
            timings.get("dense_ms", 0) + timings.get("sparse_ms", 0) + timings.get("fusion_ms", 0),
            timings.get("rerank_ms", 0), timings.get("gen_ms", 0),
            sum(v for k, v in timings.items() if k.endswith("_ms")),
            prompt_tokens, completion_tokens,
            model_name,
            1 if result.rerank_used else 0, result.rerank_model,
            1 if (result.refusal and result.refusal.refused) else 0,
            result.refusal.reason if result.refusal else None,
            result.refusal.top_score if result.refusal else None,
            result.refusal.margin if result.refusal else None,
            json.dumps(candidates, ensure_ascii=False),
            ",".join(result.degraded) if result.degraded else None,
            _utcnow(),
        ),
    )
    _ = status, coverage, history_len


@app.get("/api/logs")
def logs(limit: int = 50, kb_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM query_log"
    params: list = []
    if kb_id is not None:
        sql += " WHERE kb_id = ?"
        params.append(kb_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(limit, 500))
    return [dict(r) for r in state.conn.execute(sql, params).fetchall()]


@app.get("/api/logs/system")
def system_logs(limit: int = 120, level: str | None = None) -> list[dict]:
    """进程内的环形日志。重启即清空——要长期证据看 /api/logs 里的 query_log。"""
    return logbus.entries(limit=limit, level=level)


# --------------------------------------------------------------------- 会话
#
# 界面上只保留"一个固定会话"（每个知识库各一个），所以这里的主入口是
# /api/session/current —— 取或建，前端不需要自己生成 id、也不需要记住
# localStorage 里的那份（那份在换浏览器/清缓存后就丢了，用户看到的是
# "历史全部消失"，而数据其实还在库里）。
# 多会话的接口保留着，命令行和脚本还能用，只是 UI 不再暴露。


def _session_row(session_id: str):
    return state.conn.execute(
        "SELECT id, kb_id, title, created_at, updated_at,"
        " (SELECT count(*) FROM message m WHERE m.session_id = s.id) AS messages"
        " FROM session s WHERE s.id=?",
        (session_id,),
    ).fetchone()


def _session_item(row) -> dict:
    item = dict(row)
    item["turns"] = int(item.get("messages") or 0) // 2
    return item


@app.get("/api/session/current")
def current_session(kb_id: int | None = None) -> dict:
    """该知识库的那个固定会话：有就取最近的，没有就建一个。

    "最近"而不是"最早"：老版本可能留下多条会话，取最近的那条不会让用户
    突然看到一份很旧的对话，也不会删掉任何数据。
    """
    conn = state.conn
    sql = (
        "SELECT s.id, s.kb_id, s.title, s.created_at, s.updated_at,"
        " (SELECT count(*) FROM message m WHERE m.session_id = s.id) AS messages"
        " FROM session s"
    )
    params: list = []
    if kb_id is not None:
        sql += " WHERE s.kb_id = ?"
        params.append(kb_id)
    sql += " ORDER BY s.updated_at DESC, s.rowid DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()

    if row is None:
        now = _utcnow()
        sid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session (id, kb_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (sid, kb_id, "对话", now, now),
        )
        row = conn.execute(
            "SELECT id, kb_id, title, created_at, updated_at, 0 AS messages"
            " FROM session WHERE id=?",
            (sid,),
        ).fetchone()
        logbus.note(f"建立固定会话（知识库 {kb_id if kb_id else '-'}）", source="session")
    return _session_item(row)


@app.post("/api/sessions/{session_id}/clear")
def clear_session(session_id: str) -> dict:
    """刷新会话：清空消息，保留会话本身。

    保留而不是删了重建，是因为前端、命令行都拿着这个 id；换 id 等于要求所有
    持有方同时察觉。标题也恢复成"对话"，否则侧栏还挂着上一轮的第一个问题。
    """
    row = _session_row(session_id)
    if row is None:
        raise HTTPException(404, "会话不存在")
    cur = state.conn.execute("DELETE FROM message WHERE session_id=?", (session_id,))
    now = _utcnow()
    state.conn.execute(
        "UPDATE session SET title=?, updated_at=? WHERE id=?", ("对话", now, session_id)
    )
    logbus.note(f"会话已刷新，清空 {cur.rowcount} 条消息", source="session")
    return {"ok": True, "id": session_id, "cleared": cur.rowcount}


@app.get("/api/sessions")
def list_sessions(kb_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = """
        SELECT s.id, s.kb_id, s.title, s.created_at, s.updated_at,
               (SELECT count(*) FROM message m WHERE m.session_id = s.id) AS messages,
               (SELECT content FROM message m WHERE m.session_id = s.id
                 ORDER BY m.id DESC LIMIT 1) AS last_message
        FROM session s
    """
    params: list = []
    if kb_id is not None:
        sql += " WHERE s.kb_id = ?"
        params.append(kb_id)
    sql += " ORDER BY s.updated_at DESC LIMIT ?"
    params.append(min(limit, 200))
    rows = state.conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["last_message"] = (item.get("last_message") or "")[:80]
        out.append(item)
    return out


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str) -> list[dict]:
    rows = state.conn.execute(
        "SELECT id, role, content, trace_id, citations_json, created_at"
        " FROM message WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["citations"] = json.loads(item.pop("citations_json") or "[]")
        out.append(item)
    return out


class SessionPatch(BaseModel):
    title: str = Field(min_length=1, max_length=120)


@app.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, req: SessionPatch) -> dict:
    cur = state.conn.execute(
        "UPDATE session SET title=?, updated_at=? WHERE id=?",
        (req.title.strip(), _utcnow(), session_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "id": session_id, "title": req.title.strip()}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    """删会话连同它的消息（message.session_id 是 ON DELETE CASCADE）。

    返回删掉的消息数：界面上要说清"删了多少"，只说"已删除"的话，用户无法
    判断那条空会话是不是真的没了。会话本来就不存在时给 404 —— 静默成功会让
    前端以为重绘后的列表是干净的新状态，实际是它自己刷新失败了。
    """
    res = _delete_one_session(session_id)
    logbus.note(
        f"会话 {session_id[:12]}… 已删除（{res['deleted']} 条消息）", source="session")
    return res


def _delete_one_session(session_id: str) -> dict:
    """删一条会话，返回 {ok, id, deleted(消息数)}；不存在时抛 404。

    和上面的 delete_session 分开，是为了让批量接口能复用同一段语义：批量删除
    如果自己再写一遍 SQL，两处的"删了多少"口径迟早会分叉。
    """
    row = state.conn.execute(
        "SELECT COUNT(*) AS c FROM message WHERE session_id=?", (session_id,),
    ).fetchone()
    msgs = int(row["c"]) if row else 0
    cur = state.conn.execute("DELETE FROM session WHERE id=?", (session_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "id": session_id, "deleted": msgs}


class SessionBulkDelete(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


@app.post("/api/sessions/bulk-delete")
def bulk_delete_sessions(req: SessionBulkDelete) -> dict:
    """批量删会话。

    逐条处理，不是拼一条 `DELETE ... WHERE id IN (...)`：批量操作里最需要回答的
    问题是"哪几条没删掉"，一条 SQL 只给得出一个总数，前端就只能说"删了 8 条"，
    而用户勾的是 10 条。

    整批不包在一个事务里。删到一半失败时如果全部回滚，用户看到的是"点了删除，
    什么都没发生"，再点一次还是同样的结果——那是个死循环。宁可删掉一部分、
    把剩下失败的 id 如实带回去。

    重复 id 只算一次，也不计入失败：同一次勾选里出现两遍不是错误，报出来只会
    让人以为有数据没删掉。
    """
    ordered: dict[str, None] = {}
    for raw in req.ids:
        sid = (raw or "").strip()
        if sid:
            ordered.setdefault(sid, None)

    deleted: list[str] = []
    msgs = 0
    failed: list[dict] = []
    for sid in ordered:
        try:
            msgs += int(_delete_one_session(sid)["deleted"])
        except HTTPException as exc:
            failed.append({"id": sid, "reason": exc.detail})
            continue
        deleted.append(sid)

    if deleted:
        logbus.note(
            f"批量删除 {len(deleted)} 条会话（{msgs} 条消息）"
            + (f"，{len(failed)} 条失败" if failed else ""),
            source="session",
        )
    return {
        "ok": not failed,
        "deleted": len(deleted),
        "messages": msgs,
        "failed": failed,
    }


# --------------------------------------------------------------------- 评估


class EvalReq(BaseModel):
    k: int = Field(default=5, ge=1, le=20)
    rerank: bool | None = None


@app.post("/api/eval/run")
def run_eval_api(req: EvalReq) -> dict:
    from ..evaluate import load_baseline, run_eval

    try:
        report = run_eval(state.cfg, state.conn, state.engine, k=req.k, rerank=req.rerank)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    baseline = load_baseline(state.cfg.eval.baseline)
    payload = report.as_dict()
    payload["baseline"] = {
        "exists": baseline is not None,
        "metrics": (baseline or {}).get("metrics"),
        "fingerprint": (baseline or {}).get("fingerprint"),
        "created_at": (baseline or {}).get("created_at"),
    }
    return payload


@app.post("/api/feedback")
def feedback(req: FeedbackReq) -> dict:
    state.conn.execute(
        "INSERT OR REPLACE INTO feedback (message_id, verdict, comment, created_at)"
        " VALUES (?,?,?,?)",
        (req.message_id, req.verdict, req.comment, _utcnow()),
    )
    return {"ok": True}


# ------------------------------------------------------------ 前台站点实例


def _kb_exists(kb_id: int) -> bool:
    return state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone() is not None


def _instance_status(name: str) -> str | None:
    return next((i["status"] for i in state.sites.status_list() if i["name"] == name), None)


class InstanceIn(BaseModel):
    """站点配置的增量补丁。

    `None` 和空值必须分开：以前 title="" 会被 PUT 当成"没传"，回退到旧值，
    于是界面上怎么清都清不掉一个标题。现在 None = 不动这一项，
    ""/[] = 明确清空。前端内联编辑也靠这个语义——改一个字段只发那一个。
    """
    name: str | None = None
    kb_id: int | None = None
    port: int | None = None
    lang: str | None = None
    kb_lang: str | None = None
    answer_mode: str | None = None
    provider: str | None = None
    roles: dict[str, str] | None = None
    alt: dict[str, Any] | None = None
    title: str | None = None
    subtitle: str | None = None
    welcome: str | None = None
    samples: list[str] | None = None
    disclaimer: str | None = None
    kb_name: str | None = None
    autostart: bool | None = None
    public: bool | None = None


@app.get("/api/instances")
def list_instances() -> dict:
    return {
        "items": state.sites.status_list(),
        "langs": list(instcfg.LANGS),
        "port_range": [instcfg.PORT_MIN, instcfg.PORT_MAX],
        "kbs": [dict(r) for r in state.conn.execute(
            "SELECT id, name FROM kb ORDER BY id").fetchall()],
        "providers": list(state.cfg.generation.providers.keys()),
    }


@app.get("/api/instances/defaults")
def instance_defaults(lang: str = "ug") -> dict:
    """新建实例时按语言预填一套文案。维吾尔语那套是默认场景，不是占位符。"""
    return instcfg.default_copy(lang if lang in instcfg.LANGS else "ug")


class RolesIn(BaseModel):
    roles: dict[str, str] = Field(default_factory=dict)


@app.get("/api/model-roles")
def get_model_roles() -> dict:
    """角色分工的总览：系统默认 + 每个站点的覆盖 + 实际会用到谁。

    界面上必须能看见"实际用了谁"：配了 hunyuan 却因为名字拼错回落到本地后端，
    用户只会觉得"翻译质量没变"，而看不出配置根本没生效。
    """
    from .. import roles as roles_mod

    providers = state.cfg.generation.providers
    default = state.cfg.generation.provider
    items = []
    for spec in instcfg.load_all():
        resolved = roles_mod.plan(
            instance_roles=spec.roles or {},
            global_roles=state.cfg.generation.roles or {},
            instance_provider=spec.provider or "",
            default_provider=default,
            known=providers,
        )
        items.append({"name": spec.name, "roles": spec.roles or {}, "resolved": resolved})
    return {
        "roles": list(roles_mod.ROLES),
        "labels": roles_mod.ROLE_LABELS,
        "providers": sorted(providers.keys()),
        "current": state.cfg.generation.roles or {},
        "default_provider": default,
        "sites": items,
    }


@app.put("/api/model-roles")
def put_model_roles(req: RolesIn) -> dict:
    """保存系统级角色分工。

    只接受"还活着"的后端名：指向一个没注册的后端，界面上下拉里根本没有它，
    留着只会让排查时多绕一圈。
    """
    known = state.cfg.generation.providers
    picked = {}
    rejected = []
    for role, name in (req.roles or {}).items():
        name = (name or "").strip()
        if not name:
            continue
        if name not in known:
            rejected.append(f"{role}={name}")
            continue
        picked[role] = name
    state.cfg.generation.roles = picked
    result = modelcfg.persist(state.cfg)
    logbus.note("模型角色已更新：" + (", ".join(f"{k}→{v}" for k, v in picked.items()) or "（全部清空）"),
                source="models")
    return {"ok": True, "roles": picked, "rejected": rejected, "path": result["path"]}


class TranslateTestIn(BaseModel):
    # 空串 = 用系统级 translate 角色当前指向的后端
    provider: str = ""
    text: str = ""
    # 方向默认对齐前台主链路（中文语料 ← 维语提问；答案再译回维语）
    src: str = "zh"
    dst: str = "ug"


@app.post("/api/translate-test")
def translate_test(req: TranslateTestIn) -> dict:
    """单独测翻译模型：不碰知识库、不碰生成，就发一句真实翻译。

    前台问答 = 翻译 + 检索 + 生成 + 再翻译，四步里翻译挂了两步。对话测试页
    只验生成，翻译模型坏了用户只看到"答案不相关"，根本想不到是它。这里把
    翻译单独拎出来测，一次调用、直接看译文。
    """
    from .. import lang as lang_mod

    providers = state.cfg.generation.providers
    name = req.provider.strip() or (state.cfg.generation.roles or {}).get("translate", "")
    if not name:
        raise HTTPException(400, "还没指定翻译模型：先在「前台站点 → 模型角色」里选一个")
    if name not in providers:
        raise HTTPException(400, f"后端 {name} 不存在或已删除")
    text = req.text.strip() or (
        "维吾尔医学认为人体有血液、黏液、黄胆汁、黑胆汁四种体液，"
        "体液失衡是疾病的主要原因。"
    )
    t0 = time.perf_counter()
    out, err = lang_mod.translate_query(
        state.llm, text, src=req.src, dst=req.dst, provider=name
    )
    ms = int((time.perf_counter() - t0) * 1000)
    # "译了个寂寞"也算失败：原文抄回来是 translate_query 的降级返回，不是成功
    ok = not err and bool(out.strip()) and out.strip() != text.strip()
    return {
        "ok": ok, "output": out, "error": err, "latency_ms": ms,
        "provider": name, "src": req.src, "dst": req.dst,
    }


# ------------------------------------------------------------------ 提示词配置
#
# 为什么挂在「模型」页而不是单开一页：提示词的有效性和模型强相关——同一份约束
# 给小模型要拆成编号条款、给云端强模型一句话就够。放一起，用户调提示词时眼前
# 就摆着"这份是给谁的"。


class PromptSaveIn(BaseModel):
    # 空串 = 角色级（所有没单独配的后端都用它）；非空 = 只管这一个后端
    provider: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class PromptRollbackIn(BaseModel):
    name: str = ""


def _prompt_role(role: str) -> str:
    if role not in promptcfg.ROLE_FIELDS:
        raise HTTPException(404, f"未知提示词角色：{role}")
    return role


@app.get("/api/prompts")
def list_prompts(provider: str = "") -> dict:
    """提示词总览：每个角色有哪些字段、哪些后端单独配过、当前生效的是哪一层。"""
    providers = list(state.cfg.generation.providers.keys())
    return promptcfg.overview(
        version=state.cfg.prompt_version,
        providers=providers,
        active=provider or state.cfg.generation.provider,
    )


@app.get("/api/prompts/{role}")
def get_prompt(role: str, provider: str = "") -> dict:
    """取某个角色在某个后端上的实际情况：每字段给出「生效文本 + 来源 + 内置原文」。

    内置原文必须一起给：用户改坏了要能一眼看出跟出厂差在哪，而不是靠记忆比对。
    """
    _prompt_role(role)
    fields = promptcfg.effective_fields(
        role, provider, version=state.cfg.prompt_version,
    )
    return {
        "role": role,
        "provider": provider,
        "fields": fields,
        "path": str(promptcfg.OVERLAY_PATH),
        "history_count": len(promptcfg.history()),
    }


@app.put("/api/prompts/{role}")
def put_prompt(role: str, req: PromptSaveIn) -> dict:
    """保存覆盖。字段传空串 = 该字段回到上一层（通常是内置默认）。"""
    _prompt_role(role)
    try:
        result = promptcfg.save(role, req.provider, req.fields,
                                version=state.cfg.prompt_version)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        # 体检报错不写盘：写到一半的提示词比不写更危险，它会立刻被问答链路读到
        return result
    where = f"（后端 {req.provider}）" if req.provider else "（角色默认）"
    logbus.note(
        f"提示词已{'更新' if result.get('saved') else '恢复默认'}"
        f"：{role}{where}",
        source="models",
    )
    return result


@app.post("/api/prompts/{role}/reset")
def reset_prompt(role: str, req: PromptSaveIn | None = None) -> dict:
    _prompt_role(role)
    provider = (req.provider if req else "") or ""
    try:
        result = promptcfg.reset(role, provider, version=state.cfg.prompt_version)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    logbus.note(f"提示词恢复默认：{role}" + (f"（后端 {provider}）" if provider else "（角色默认）"),
                source="models")
    return result


@app.get("/api/prompts/{role}/history")
def prompt_history(role: str) -> dict:
    _prompt_role(role)
    return {"role": role, "items": promptcfg.history()}


@app.post("/api/prompts/{role}/rollback")
def rollback_prompt(role: str, req: PromptRollbackIn) -> dict:
    """整体回滚。快照是每次保存前的完整副本，所以这里不需要重新体检——
    它本来就是某个曾经生效过的状态。"""
    _prompt_role(role)
    try:
        result = promptcfg.rollback(req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    logbus.note(f"提示词回滚到 {req.name}", source="models")
    return result


# 预览用的样例。不接真实检索：预览要回答的是"这段提示词读起来对不对"，
# 而不是"这个库能召回什么"，混进真实内容反而看不清提示词本身。
_PREVIEW_DOC = (
    "[1] 《晶珠本草》 · 第 12 页 · 药物·性味\n"
    "藏木香，味辛苦，性温。功能理气止痛，用于脘腹胀痛、呕吐泄泻。"
)
_PREVIEW_Q = "藏木香的性味和功效是什么？"


@app.post("/api/prompts/{role}/preview")
def preview_prompt(role: str, req: PromptSaveIn) -> dict:
    """把还没保存的草稿渲染成模型真正收到的东西。

    这一步存在的理由：提示词里写的是规则，模型读到的是"规则 + 系统追加的语言
    指令 + 证据档位例外 + 资料块"。只看规则本身，用户无法判断最终效果——
    尤其是那两段追加，它们会盖住用户刚写的某些约束。
    """
    _prompt_role(role)
    fields = req.fields or {}
    version = state.cfg.prompt_version

    if role == "generate":
        system = fields.get("system") or promptcfg.system_prompt(
            version, None, req.provider)
        # 语言指令与证据例外是运行期决定的行为约束，不随提示词可编辑。
        # 它们不拼进 system 一起显示——那样用户会以为自己刚写的东西就是最终全部；
        # 单独给一段"实际还会追加什么"，才看得出边界在哪。
        append_sample = ""
        try:
            from ..lang import answer_instruction

            append_sample = answer_instruction("ug")
        except Exception:
            append_sample = ""
        return {
            "role": role,
            "provider": req.provider,
            "system": system,
            "append_sample": append_sample,
            "user": f"【资料】\n{_PREVIEW_DOC}\n\n【问题】\n{_PREVIEW_Q}\n\n"
                    f"请依据上述资料作答，并按要求标注来源编号。",
            "note": "预览里的资料是固定样例，不接真实检索。"
                    "实际请求时 system 末尾还会追加两段，且都由运行期决定、不可在此编辑："
                    "① 非中文作答时追加语言指令（下面「实际还会追加」是维吾尔语的例子）；"
                    "② 检索证据不足时追加例外条款，允许用自身知识作答但必须标明哪些不是资料。",
        }

    builtin = promptcfg.builtin_fields("translate")
    user_tpl = fields.get("user") or builtin["user"]
    system = fields.get("system") or builtin["system"]
    return {
        "role": role,
        "provider": req.provider,
        "system": system,
        "append_sample": "",
        "user": promptcfg.fill(user_tpl, src="维吾尔语", dst="中文",
                               question="مىزاج دېگەن نېمە؟"),
        "note": "预览按「维吾尔语 → 中文」填了占位符；实际方向由知识库语言决定。"
                "重试路径（第二次翻译尝试）用的是另一套更硬的约束，不在这里显示。",
    }


@app.get("/api/instances/next-port")
def instance_next_port() -> dict:
    return {"port": instcfg.next_free_port()}


@app.put("/api/instances/{name}")
def put_instance(name: str, req: InstanceIn) -> dict:
    """保存站点配置。只有端口和监听地址变了才重启。

    以前的规则是"只要站点在跑就 restart"，因为 `build_site` 把 spec 闭包捕获
    成了快照，不重启新配置根本不生效。现在 site.py 每次请求现读配置，文案、
    角色、知识库、语言、答案模式全都是热的，只有 uvicorn 启动时钉死的
    socket 绑定（端口 / 0.0.0.0）非重启不可。

    这么做不是省那 3 秒：内联编辑意味着用户改一个标点就触发一次保存，
    如果每次都断服，"直接改"这个交互本身就是不可用的。
    """
    name = (name or req.name or "").strip().lower()
    old = instcfg.get(name)
    port = int(req.port or (old.port if old else instcfg.next_free_port()))

    def keep(attr: str, default):
        """没传的字段沿用旧值。None = 没传；""/[]/=明确清空。"""
        return getattr(old, attr) if old is not None else default

    # 备选语言文案：只下发主语言时保留原来那份
    alt_copy = (instcfg.coerce_alt(req.alt) if req.alt is not None
                else keep("alt", {}))
    spec = instcfg.InstanceSpec(
        name=name,
        kb_id=int(req.kb_id if req.kb_id is not None else keep("kb_id", 0)),
        port=port,
        lang=(req.lang or keep("lang", "ug")),
        kb_lang=(req.kb_lang or keep("kb_lang", "zh")),
        answer_mode=(req.answer_mode or keep("answer_mode", "translate")),
        provider=req.provider if req.provider is not None else keep("provider", ""),
        roles=(instcfg.coerce_roles(req.roles) if req.roles is not None
               else keep("roles", {})),
        alt=alt_copy,
        title=req.title if req.title is not None else keep("title", ""),
        subtitle=req.subtitle if req.subtitle is not None else keep("subtitle", ""),
        welcome=req.welcome if req.welcome is not None else keep("welcome", ""),
        samples=req.samples if req.samples is not None else keep("samples", []),
        disclaimer=req.disclaimer if req.disclaimer is not None else keep("disclaimer", ""),
        kb_name=req.kb_name if req.kb_name is not None else keep("kb_name", ""),
        autostart=bool(req.autostart) if req.autostart is not None else keep("autostart", False),
        public=bool(req.public) if req.public is not None else keep("public", False),
    )
    try:
        instcfg.validate(spec, kb_exists=_kb_exists)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for other in instcfg.load_all():
        if other.name != name and other.port == port:
            raise HTTPException(400, f"端口 {port} 已被实例「{other.name}」占用")

    # 冷字段：只有这两个动到 socket。其余字段下一次请求就生效。
    cold = old is None or old.port != spec.port or old.public != spec.public
    was_running = _instance_status(name) == "running"
    instcfg.upsert(spec)
    status = state.sites.get(name)
    if was_running and cold:
        status = state.sites.restart(name)
    logbus.note(f"站点实例 {name} 已保存（端口 {port}，知识库 #{spec.kb_id}，{spec.lang}"
                + ("，已重启生效）" if was_running and cold else "，热生效）"),
                source="sites")
    return {"ok": True, "spec": spec.as_dict(), "status": status,
            "restarted": bool(was_running and cold)}


@app.post("/api/instances/{name}/start")
def start_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    status = state.sites.start(name)
    if status["status"] == "error":
        # 已发出去的不是异常：启动失败是"环境问题"，但要让用户看见原因
        logbus.note(f"站点 {name} 启动失败：{status['error']}", level="ERROR", source="sites")
    else:
        logbus.note(f"站点 {name} 已启动：{status['url']}", source="sites")
    return status


@app.post("/api/instances/{name}/stop")
def stop_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    status = state.sites.stop(name)
    logbus.note(f"站点 {name} 已停止", source="sites")
    return status


@app.post("/api/instances/{name}/restart")
def restart_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    return state.sites.restart(name)


@app.delete("/api/instances/{name}")
def delete_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    state.sites.stop(name)
    instcfg.remove(name)
    logbus.note(f"站点实例 {name} 已删除", level="WARNING", source="sites")
    return {"ok": True, "deleted": name}


@app.exception_handler(Exception)
def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logbus.note(
        f"{type(exc).__name__}: {exc} @ {request.url.path}", level="ERROR", source="api"
    )
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}", "path": str(request.url.path)},
    )
