"""SQLite 连接管理。

sqlite3 的连接默认绑定创建它的线程；FastAPI 的同步路由跑在线程池里，
所以这里用 threading.local 做每线程一个连接——比拼锁简单，也不会在
并发导入时把查询堵死（WAL 模式下读写可以并行）。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .config import Config

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _migrate(conn: sqlite3.Connection) -> None:
    """只加列，不删不改。个人项目里"删表重建"是数据丢失的最短路径。

    用 PRAGMA table_info 而不是 try/except ALTER：SQLite 的报错文本跨版本
    不稳定，靠字符串判断"列已存在"迟早会失效。
    """
    kb_cols = {row[1] for row in conn.execute("PRAGMA table_info(kb)").fetchall()}
    if "config_json" not in kb_cols:
        # 知识库级参数覆盖。只存与全局不同的项，NULL 表示完全跟随全局。
        conn.execute("ALTER TABLE kb ADD COLUMN config_json TEXT")

    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(document)").fetchall()}
    if "clean_json" not in doc_cols:
        # 导入时用的清洗方案。重索引要能沿用同一套，否则"重索引"等于悄悄
        # 把清洗规则撤掉，库里的内容跟用户上次确认过的那份对不上。
        conn.execute("ALTER TABLE document ADD COLUMN clean_json TEXT")


class Database:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.path = cfg.storage.db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.cfg.storage.busy_timeout_ms / 1000,
            isolation_level=None,  # 手动管理事务，避免隐式 BEGIN 造成的锁升级
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self.cfg.storage.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.ensure_schema()
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            bootstrap = self._new_conn()
            try:
                bootstrap.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                _migrate(bootstrap)
            finally:
                bootstrap.close()
            self._initialized = True

    @staticmethod
    def tx(conn: sqlite3.Connection):
        """显式事务上下文。嵌套调用不安全，只在最外层用。"""
        return _Tx(conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
