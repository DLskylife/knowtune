"""HTTP 接口装配入口。

路由按资源域拆分到 `routes/` 目录下：meta（基础/设置）、docs（文档/chunk/导入）、
models（模型服务/网关）、kb（知识库）、search（检索/问答/日志）、sessions（会话/反馈）、
prompts（提示词/角色）、sites（前台站点实例）。

本文件只负责：创建 FastAPI 实例、启动/关闭生命周期、挂载中间件、注册各路由模块。
不做多用户、不做鉴权（明确不在第一阶段范围）。
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import logbus, modelcfg, secrets as secrets_mod, services as services_mod
from ..db import utcnow as _utcnow
from .common import STATIC_DIR, _PARTS_DIR, _PART_TTL_H, _discard_parts
from .routes import docs, kb, meta, models, prompts, search, sessions, sites
from .state import AppState, state

# 把 root / uvicorn 的日志分流一份进内存环形缓冲区，首页「系统日志」直接读它。
logbus.install()


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


def _autostart_local_models(state: "AppState") -> None:
    """把标了 autostart 的本地端点拉起来。在后台线程里跑。

    为什么需要这个：预问诊可以钉在某个端点上（`config/previsit.yaml` 的
    `provider`）。钉在 local 时，如果重启后没人去模型页点「启动」，预问诊
    不会报错——它会按设计降级成规则模板，问出来的话术变朴素、抽取退化成
    关键词，而界面上一切正常。配置写着"本地"，实际模型没在跑，是最难察觉的
    那种失败。

    放线程里而不是像前台站点那样同步起：加载 4.4G 权重实测 20 秒上下，
    同步做会让 `knowtune serve` 二十秒不监听端口。代价是启动后这二十秒内
    打进来的问诊仍会降级，所以成功/失败都往日志里写一条，别让它悄悄发生。
    """
    from .. import localmodels as local_mod

    for name, p in state.cfg.generation.providers.items():
        if not getattr(p, "autostart", False) or p.kind != "local":
            continue
        entry = p.entry()
        if entry is None or not entry.path:
            logbus.note(f"端点「{name}」标了开机自启，但默认模型没有本地路径（path），跳过",
                        level="WARNING", source="models")
            continue
        inst_name = f"{name}/{entry.id}"
        try:
            cur = local_mod.status(inst_name)
            if cur.get("state") in ("running", "starting"):
                logbus.note(f"本地实例已在运行：{entry.id} @ :{cur.get('port')}", source="models")
                continue
            inst = local_mod.start(
                name=inst_name, path=entry.path,
                port=entry.port or None,
                ctx=entry.context or local_mod.DEFAULT_CTX,
                alias=entry.id,
            )
        except Exception as exc:      # 显存不够、端口被占、文件被删……
            logbus.note(
                f"本地端点「{name}」自启失败：{type(exc).__name__}: {exc}。"
                f"钉在它上面的功能（预问诊 / 站点角色）会降级成模板，去模型页手动启动可恢复",
                level="ERROR", source="models")
            continue
        entry.port = inst.port
        p.base_url = inst.base_url
        try:
            modelcfg.persist(state.cfg)
        except Exception as exc:      # 落盘失败不该让这次启动白费
            logbus.note(f"自启后回写端点地址失败：{type(exc).__name__}: {exc}",
                        level="WARNING", source="models")
        services_mod.invalidate(name)
        logbus.note(f"本地实例已自启：{entry.id} @ {inst.base_url}（pid {inst.pid}）",
                    source="models")


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

    # 精排放在嵌入模型之后：嵌入是每次提问都要用的，精排只影响排序质量，
    # 两者抢 CPU 时不该让精排挡在前面。库级可能关了精排，但全局开着就先热着——
    # 用户随时会切到开着精排的库，省下的是那一次 7 秒。
    if state.cfg.retrieval.rerank.enabled:
        t1 = time.perf_counter()
        err = state.reranker.warmup()
        if err:
            print(f"[warmup] 精排预热失败：{err}")
            logbus.note(err, level="WARNING", source="warmup")
        else:
            ms = int((time.perf_counter() - t1) * 1000)
            print(f"[warmup] 精排模型就绪（{ms}ms）")


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
    # 标了 autostart 的本地端点先起（加载权重二十秒上下，必须异步）。
    # 放在站点自启之前：站点里的角色可能也指向同一个本地端点。
    threading.Thread(target=_autostart_local_models, args=(state,),
                     daemon=True, name="local-autostart").start()
    # 标了 autostart 的前台实例随后台一起起来。放在线程外面同步做：它们只是
    # 端口监听，不阻塞，而放到后台线程里会让"启动失败"变得没有反馈。
    for name in state.sites.start_autostart():
        print(f"[sites] 已自动启动站点 {name}")
    # 同一个端口上已经有同名站点在跑（多半是另开了一个后台）：这不是故障，
    # 但必须说出来。否则用户会在两个后台之间反复点"启动"，两边都给他一句
    # "端口被占用，换一个端口"——而那个链接其实一直是通的。
    for it in state.sites.status_list():
        if it.get("status") != "external":
            continue
        pid = (it.get("managed_by") or {}).get("pid")
        logbus.note(
            f"站点 {it['name']}（:{it['port']}）已由{'PID ' + str(pid) if pid else '另一个后台进程'}"
            "托管，本后台不重复启动；要启停请去那个后台", level="WARNING", source="sites")
    yield
    state.sites.stop_all()


app = FastAPI(title="KnowTune", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 静态资源绝不允许"启发式缓存"。FileResponse / StaticFiles 默认只带 ETag 和
# Last-Modified、不带 Cache-Control，WebView 会按"距今时长的 10%"自作主张缓存
# index.html —— 结果就是页面停在旧版：按钮在、逻辑是旧的（比如删库还在用被
# 吞掉的原生 confirm），表现成"点了没反应"，怎么查代码都对。
# 本地管理端正确性优先：一律 no-cache（每次协商，命中就 304，几乎零开销）。
@app.middleware("http")
async def no_stale_static(request: Request, call_next):
    path = request.url.path
    # /v1 是给外部程序调的 OpenAI 兼容入口。浏览器里的第三方工具会先发
    # OPTIONS 预检，这里直接放行；否则它们连 POST 都发不出去，还以为是
    # 模型服务挂了。服务默认只监听 127.0.0.1，不需要凭据/Cookie。
    if path.startswith("/v1/"):
        cors = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "600",
        }
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        resp = await call_next(request)
        for k, v in cors.items():
            resp.headers[k] = v
        return resp
    resp = await call_next(request)
    if path == "/" or path.startswith("/static/"):
        # 首页用 no-store：no-cache 只是"每次问问服务器"，某些内嵌 WebView
        # 连问都不问就直接给旧的，用户看到的是改版前的界面。
        resp.headers["Cache-Control"] = "no-store" if path == "/" else "no-cache"
    return resp


# 路由按资源域注册。
app.include_router(meta.router)
app.include_router(models.router)
app.include_router(kb.router)
app.include_router(docs.router)
app.include_router(search.router)
app.include_router(sessions.router)
app.include_router(prompts.router)
app.include_router(sites.router)


@app.exception_handler(Exception)
def unhandled(request: Request, exc: Exception) -> JSONResponse:
    logbus.note(
        f"{type(exc).__name__}: {exc} @ {request.url.path}", level="ERROR", source="api"
    )
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}", "path": str(request.url.path)},
    )