"""命令行入口。Web UI 是主形态，CLI 负责批量导入与脚本化。

    knowtune init                     初始化数据库与默认知识库
    knowtune status                   环境与索引状态
    knowtune kb list|create <name>
    knowtune ingest <path> [--kb 1]   导入文件或目录
    knowtune ask "问题" [--kb 1]      单次问答（走当前 provider）
    knowtune debug "问题"             打印两路召回与融合排序（调检索必用）
    knowtune serve [--port 8000]      启动 Web UI
    knowtune previsit [--port 8900]   启动预问诊站点（独立端口）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .db import Database
from .generate import cite as cite_mod
from .generate.llm import LLMClient, LLMError
from .generate.prompt import REFUSE_TEMPLATE, build_context
from .index.embedder import Embedder
from .index.sparse import SparseIndex
from .index.vectors import VectorStore
from .ingest import loader as loader_mod
from .ingest.pipeline import IngestPipeline
from .retrieve.engine import RetrievalEngine
from .retrieve.rerank import Reranker


def _bootstrap():
    cfg = load_config()
    db = Database(cfg)
    db.ensure_schema()
    embedder = Embedder(cfg.embedding)
    vectors = VectorStore(cfg)
    sparse = SparseIndex(cfg.retrieval.sparse)
    reranker = Reranker(cfg)
    engine = RetrievalEngine(cfg, embedder, vectors, sparse, reranker)
    pipeline = IngestPipeline(cfg, db, embedder, sparse, vectors)
    return cfg, db, engine, pipeline


def _default_kb(conn) -> int:
    row = conn.execute("SELECT id FROM kb ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO kb (name, description) VALUES ('default', '默认知识库')")
    return int(cur.lastrowid)


def cmd_init(args) -> int:
    cfg, db, _, _ = _bootstrap()
    conn = db.conn()
    kb_id = _default_kb(conn)
    print(f"数据库已就绪：{cfg.storage.db_path}")
    print(f"默认知识库 id={kb_id}")
    return 0


def cmd_status(args) -> int:
    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    rows = conn.execute(
        "SELECT id, name, (SELECT count(*) FROM document d WHERE d.kb_id=kb.id) docs FROM kb ORDER BY id"
    ).fetchall()
    print(f"配置: {cfg.storage.db_path}")
    print(f"嵌入模型: {cfg.embedding.model} (dim={cfg.embedding.dim}) "
          f"可用={engine.embedder.available}")
    if engine.embedder.error:
        print(f"  错误: {engine.embedder.error}")
    print(f"精排: enabled={cfg.retrieval.rerank.enabled} model={cfg.retrieval.rerank.model}")
    print(f"LLM: provider={cfg.generation.provider}")
    print("知识库:")
    for r in rows:
        n = conn.execute(
            "SELECT count(*) c FROM chunk ch JOIN document d ON d.id=ch.doc_id"
            " WHERE d.kb_id=? AND ch.generation=d.active_gen", (r["id"],)
        ).fetchone()["c"]
        print(f"  [{r['id']}] {r['name']}  文档 {r['docs']}  活跃 chunk {n}")
    return 0


def cmd_kb(args) -> int:
    _, db, _, _ = _bootstrap()
    conn = db.conn()
    if args.action == "list":
        for r in conn.execute("SELECT id, name FROM kb ORDER BY id").fetchall():
            print(f"[{r['id']}] {r['name']}")
    else:
        cur = conn.execute("INSERT INTO kb (name) VALUES (?)", (args.name,))
        print(f"已创建 [{cur.lastrowid}] {args.name}")
    return 0


def cmd_ingest(args) -> int:
    _, db, _, pipeline = _bootstrap()
    conn = db.conn()
    kb_id = args.kb or _default_kb(conn)
    target = Path(args.path)
    if not target.exists():
        print(f"路径不存在: {target}", file=sys.stderr)
        return 2

    files = (
        [f for f in target.rglob("*") if f.is_file() and f.suffix.lower() in loader_mod.SUPPORTED_EXTS]
        if target.is_dir()
        else [target]
    )
    ok = 0
    for f in files:
        res = pipeline.import_path(kb_id, f)
        mark = "OK  " if res.status == "ready" else "FAIL"
        print(f"[{mark}] {f.name} — {res.message}")
        if res.status == "ready":
            ok += 1
    print(f"\n完成 {ok}/{len(files)}")
    return 0 if ok == len(files) else 1


def cmd_ask(args) -> int:
    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    kb_id = args.kb or _default_kb(conn)
    result = engine.search(conn, args.question, kb_id=kb_id)

    if result.refusal and result.refusal.refused:
        print(REFUSE_TEMPLATE)
        print(f"\n[拒答] reason={result.refusal.reason} "
              f"top={result.refusal.top_score} margin={result.refusal.margin} "
              f"calibrated={result.refusal.calibrated}")
        return 0

    bundle = build_context(
        result.candidates,
        question=args.question,
        token_budget=cfg.generation.context_token_budget,
        min_context_chars=cfg.generation.min_context_chars,
        prompt_version=cfg.prompt_version,
        provider=args.provider or cfg.generation.provider,
    )
    client = LLMClient(cfg)
    try:
        answer = "".join(client.stream(bundle.messages(args.question), provider=args.provider))
    except LLMError as exc:
        print(f"LLM 调用失败：{exc}", file=sys.stderr)
        print("检索结果仍然可用，可加 --debug 查看。", file=sys.stderr)
        return 1

    report = cite_mod.verify(answer, len(bundle.citations))
    print(report.cleaned)
    print("\n" + "─" * 60)
    for c in bundle.citations:
        loc = f" p.{c.page_no}" if c.page_no else ""
        print(f"[{c.n}] {c.doc_title}{loc}  chunk={c.chunk_id}")
    print(f"引用覆盖率={report.coverage:.0%}  "
          f"越界编号={report.invalid_ids or '无'}  "
          f"降级={result.degraded or '无'}")
    return 0


def cmd_debug(args) -> int:
    _, db, engine, _ = _bootstrap()
    conn = db.conn()
    kb_id = args.kb or _default_kb(conn)
    result = engine.search(conn, args.question, kb_id=kb_id, top_k=args.top_k)
    print(f"查询: {args.question}\n")
    for name, hits in (("dense", result.dense_hits), ("sparse", result.sparse_hits)):
        print(f"── {name} top{len(hits)}")
        for h in hits[:10]:
            print(f"  #{h.rank:<2} chunk={h.chunk_id:<6} score={h.score:.4f}")
    print("── fused (RRF)")
    for i, f in enumerate(result.fused[:10], 1):
        print(f"  #{i:<2} chunk={f['chunk_id']:<6} rrf={f['rrf']:.6f} "
              f"dense_rank={f['dense_rank']} sparse_rank={f['sparse_rank']}")
    print("── final")
    for c in result.candidates:
        head = (c.heading_path or "")[:40]
        print(f"  [{c.final_rank}] {c.doc_title} {head}")
        print(f"      {c.text[:100].replace(chr(10), ' ')}…")
    print(f"\n耗时 {result.timings}  降级 {result.degraded or '无'}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    host = "127.0.0.1" if args.host in ("0.0.0.0", "127.0.0.1", "localhost") else args.host
    # 固定入口的实际端口写进环境：挂在本进程里的预问诊、以及后来起来的
    # 独立子进程，都按这个地址找网关，不用各自猜端口。
    import os

    os.environ["KNOWTUNE_GATEWAY"] = f"http://{host}:{args.port}/v1"
    print(f"Web UI: http://{host}:{args.port}")
    # 本地模型实例的端口会变，这个不会——外部程序一律写死它。
    print(f"API:    http://{host}:{args.port}/v1  (OpenAI 兼容 /v1/chat/completions)")
    if args.host == "0.0.0.0":
        from knowtune import localmodels

        ip = localmodels._lan_ip()
        if ip:
            print(f"LAN:    http://{ip}:{args.port}/v1  (同一局域网可调用，无鉴权)")
    uvicorn.run("knowtune.api.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_previsit(args) -> int:
    """预问诊站点：独立进程、独立端口，复用主服务的模型后端与已存密钥。"""
    import uvicorn

    print(f"预问诊: http://127.0.0.1:{args.port}")
    uvicorn.run("knowtune.previsit.app:create_app", host=args.host, port=args.port,
                log_level="info", factory=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knowtune", description="KnowTune — 本地知识库问答系统（带引用的 RAG）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("status", help="查看状态").set_defaults(func=cmd_status)

    p_kb = sub.add_parser("kb", help="知识库管理")
    p_kb.add_argument("action", choices=["list", "create"])
    p_kb.add_argument("name", nargs="?", default="")
    p_kb.set_defaults(func=cmd_kb)

    p_in = sub.add_parser("ingest", help="导入文件或目录")
    p_in.add_argument("path")
    p_in.add_argument("--kb", type=int, default=None)
    p_in.set_defaults(func=cmd_ingest)

    p_ask = sub.add_parser("ask", help="问答")
    p_ask.add_argument("question")
    p_ask.add_argument("--kb", type=int, default=None)
    p_ask.add_argument("--provider", default=None)
    p_ask.set_defaults(func=cmd_ask)

    p_dbg = sub.add_parser("debug", help="检索调试")
    p_dbg.add_argument("question")
    p_dbg.add_argument("--kb", type=int, default=None)
    p_dbg.add_argument("--top-k", type=int, default=None)
    p_dbg.set_defaults(func=cmd_debug)

    p_srv = sub.add_parser("serve", help="启动 Web UI")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.set_defaults(func=cmd_serve)

    p_pv = sub.add_parser("previsit", help="启动预问诊站点（独立端口）")
    p_pv.add_argument("--port", type=int, default=8900)
    p_pv.add_argument("--host", default="127.0.0.1")
    p_pv.set_defaults(func=cmd_previsit)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
