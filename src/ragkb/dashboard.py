"""首页数据聚合：系统资产、检索健康、服务状态、日志。

这个模块只读不写，也不做任何"探测"（那是 services.py 的事）——首页首屏必须
在 50ms 内出来，任何可能等网络的东西都不能出现在这里。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import logbus, services as services_mod
from .config import Config

_pkg_version = "0.4.0"


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dir_bytes(path: Path, cap: int = 6000) -> tuple[int, int]:
    """返回（字节数，文件数）。文件数多到离谱时提前退出——首页不能因为
    某个用户把整个 C 盘软链进 data/ 就卡死。"""
    total = 0
    files = 0
    if not path.exists():
        return 0, 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                continue
            if files >= cap:
                return total, files
    return total, files


def _db_bytes(cfg: Config) -> int:
    base = Path(cfg.storage.db_path)
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(base) + suffix)
        if p.exists():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _mb(n: int) -> float:
    return round(n / 1024 / 1024, 1)


def _kb_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT k.id, k.name, k.description, k.created_at, k.config_json,
               (SELECT count(*) FROM document d WHERE d.kb_id=k.id) AS docs,
               (SELECT count(*) FROM document d WHERE d.kb_id=k.id AND d.status='ready') AS ready,
               (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
                 WHERE d.kb_id=k.id AND c.generation=d.active_gen) AS chunks,
               (SELECT max(d.updated_at) FROM document d WHERE d.kb_id=k.id) AS updated_at
        FROM kb k ORDER BY k.id
        """
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        try:
            cfg_json = json.loads(item.pop("config_json") or "{}")
        except ValueError:
            cfg_json = {}
        item["overrides"] = len(cfg_json) if isinstance(cfg_json, dict) else 0
        out.append(item)
    return out


def _health(conn) -> dict[str, Any]:
    since = _iso_hours_ago(24)
    row = conn.execute(
        """
        SELECT count(*) AS n,
               COALESCE(sum(refused), 0) AS refused,
               COALESCE(sum(CASE WHEN degraded IS NOT NULL AND degraded <> '' THEN 1 ELSE 0 END), 0) AS degraded,
               COALESCE(avg(total_ms), 0) AS avg_ms,
               max(created_at) AS last_at
        FROM query_log WHERE created_at >= ?
        """,
        (since,),
    ).fetchone()
    n = int(row["n"] or 0)
    p50 = None
    if n:
        p50 = conn.execute(
            "SELECT total_ms FROM query_log WHERE created_at >= ? ORDER BY total_ms LIMIT 1 OFFSET ?",
            (since, n // 2),
        ).fetchone()
        p50 = int(p50["total_ms"]) if p50 and p50["total_ms"] is not None else None
    return {
        "window_h": 24,
        "queries": n,
        "refused": int(row["refused"] or 0),
        "refusal_rate": round(int(row["refused"] or 0) / n, 3) if n else None,
        "degraded": int(row["degraded"] or 0),
        "avg_ms": int(row["avg_ms"] or 0),
        "p50_ms": p50,
        "last_query_at": row["last_at"],
    }


def _eval_baseline(cfg: Config) -> dict[str, Any]:
    path = Path(cfg.eval.baseline)
    if not path.exists():
        return {"exists": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exists": False}
    metrics = data.get("metrics") or {}
    return {
        "exists": True,
        "created_at": data.get("created_at"),
        "metrics": metrics,
        "fingerprint": data.get("fingerprint"),
    }


def _recent_queries(conn, limit: int = 12) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT q.trace_id, q.kb_id, q.question_chars, q.total_ms, q.retrieve_ms,
               q.rerank_ms, q.gen_ms, q.refused, q.refuse_reason, q.model,
               q.degraded, q.created_at, k.name AS kb_name
        FROM query_log q LEFT JOIN kb k ON k.id = q.kb_id
        WHERE q.question_digest <> 'system'
        ORDER BY q.created_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def build(
    cfg: Config,
    conn,
    *,
    started_at: float,
    port: int | None = None,
) -> dict[str, Any]:
    assets_row = conn.execute(
        """
        SELECT (SELECT count(*) FROM kb) AS kbs,
               (SELECT count(*) FROM document) AS docs,
               (SELECT count(*) FROM document WHERE status='ready') AS docs_ready,
               (SELECT count(*) FROM document WHERE status='failed') AS docs_failed,
               (SELECT count(*) FROM chunk c JOIN document d ON d.id=c.doc_id
                 WHERE c.generation=d.active_gen) AS chunks,
               (SELECT count(*) FROM chunk WHERE is_hidden=1) AS hidden_chunks,
               (SELECT count(*) FROM embedding) AS embeddings,
               (SELECT count(*) FROM session) AS sessions,
               (SELECT count(*) FROM message) AS messages,
               (SELECT count(*) FROM query_log WHERE question_digest <> 'system') AS queries,
               (SELECT count(*) FROM feedback WHERE verdict='up') AS fb_up,
               (SELECT count(*) FROM feedback WHERE verdict='down') AS fb_down
        """
    ).fetchone()

    data_bytes, data_files = _dir_bytes(Path(cfg.storage.data_dir))
    db_bytes = _db_bytes(cfg)

    # 服务状态只给"上次探到什么"，不在这里发请求。前端渲染完首屏再强制探一次。
    svc = []
    for name, p in cfg.generation.providers.items():
        cached = services_mod.cached(name, provider=p) or {}
        svc.append({
            "name": name,
            "kind": p.kind,
            "model": p.model,
            "base_url": p.base_url,
            "active": name == cfg.generation.provider,
            "status": cached.get("status", "unknown"),
            "latency_ms": cached.get("latency_ms"),
            "message": cached.get("message", "还没探测"),
            "checked_at": cached.get("checked_at"),
        })

    return {
        "server": {
            "version": _pkg_version,
            "started_at": datetime.fromtimestamp(started_at).strftime("%Y-%m-%dT%H:%M:%S"),
            "uptime_s": int(time.time() - started_at),
            "port": port,
            "pid": os.getpid(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "db_path": str(cfg.storage.db_path),
        },
        "assets": {
            "kbs": assets_row["kbs"],
            "docs": assets_row["docs"],
            "docs_ready": assets_row["docs_ready"],
            "docs_failed": assets_row["docs_failed"],
            "chunks": assets_row["chunks"],
            "hidden_chunks": assets_row["hidden_chunks"],
            "embeddings": assets_row["embeddings"],
            "embedding_mb": _mb(int(assets_row["embeddings"] or 0) * cfg.embedding.dim * 4),
            "sessions": assets_row["sessions"],
            "messages": assets_row["messages"],
            "queries": assets_row["queries"],
            "feedback_up": assets_row["fb_up"],
            "feedback_down": assets_row["fb_down"],
            "db_mb": _mb(db_bytes),
            "data_mb": _mb(data_bytes),
            "data_files": data_files,
        },
        "kbs": _kb_rows(conn),
        "health": _health(conn),
        "eval": _eval_baseline(cfg),
        "embedder": {
            "model": cfg.embedding.model,
            "dim": cfg.embedding.dim,
            "device": cfg.embedding.device,
        },
        "rerank": {
            "enabled": cfg.retrieval.rerank.enabled,
            "model": cfg.retrieval.rerank.model,
        },
        "services": svc,
        "logs": {
            "queries": _recent_queries(conn),
            "system": logbus.entries(limit=60),
        },
    }
