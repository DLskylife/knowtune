"""译文缓存：同一句话只翻一次，跨会话 / 刷新 / 设备复用。

切语言的定义是「同一句话换文本」。既然是同一句话，就没必要每来一个会话、
每刷一次页面都重新叫一次模型——实测同一段答案走云端翻译要 12～20 秒，
偶发失败还要重试；而命中缓存是毫秒级。

这里用独立的 SQLite 文件（data/translation_cache.db），不碰主库：
它是纯派生产物，删掉只会导致下次重新翻译，不会丢任何用户数据。

key 用 sha256(src + dst + 正文)，文本本身就是查键——问题、标题、答案
哪怕只有标点不同也算两句话，各自独立翻译、独立缓存。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config

_LOCAL = threading.local()


def _path() -> Path:
    return load_config().storage.data_dir / "translation_cache.db"


def _key(text: str, src: str, dst: str) -> str:
    return hashlib.sha256(f"{src}\x00{dst}\x00{text}".encode("utf-8")).hexdigest()


def _conn() -> sqlite3.Connection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS translation ("
            " k TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL,"
            " text TEXT NOT NULL, trans TEXT NOT NULL, at TEXT NOT NULL)"
        )
        _LOCAL.conn = conn
    return conn


def get(texts: list[str], *, src: str = "zh", dst: str = "ug") -> dict[str, str]:
    """返回 {原文: 译文}，只含命中且译文非空的项。"""
    want = [t for t in texts if t and t.strip()]
    if not want:
        return {}
    conn = _conn()
    out: dict[str, str] = {}
    # 分批，避开 SQLite 变量数上限（老版本默认 999）
    for i in range(0, len(want), 400):
        chunk = want[i:i + 400]
        keys = [_key(t, src, dst) for t in chunk]
        marks = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT k, trans FROM translation WHERE k IN ({marks})", keys
        ).fetchall()
        found = dict(rows)
        for text, key in zip(chunk, keys):
            hit = found.get(key)
            if hit:
                out[text] = hit
    return out


def put(pairs: dict[str, str], *, src: str = "zh", dst: str = "ug") -> int:
    """写入译文，返回写入条数。空译文不写——失败留空让下次还能重试。"""
    clean = {k: v for k, v in pairs.items() if k and k.strip() and v and v.strip()}
    if not clean:
        return 0
    conn = _conn()
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.executemany(
        "INSERT OR REPLACE INTO translation(k,src,dst,text,trans,at)"
        " VALUES(?,?,?,?,?,?)",
        [(_key(t, src, dst), src, dst, t, v, at) for t, v in clean.items()],
    )
    return len(clean)


def stats() -> dict[str, Any]:
    """缓存规模，给排查与自检用。"""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n, MAX(at) AS latest FROM translation"
        ).fetchone()
        return {"count": row["n"], "latest": row["latest"] or ""}
    except Exception as exc:
        return {"error": str(exc)}
