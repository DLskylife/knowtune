"""预问诊档案存储（SQLite，单文件 data/previsit.db）。

一张 session 表存整份 MedicalState，一张 turn 表存问答流水。

为什么档案用 JSON 列而不是一堆字段：槽位是**配置驱动**的——今天配 5 个、
明天加一个"既往史"，如果每个槽位一列，加槽位就得写迁移。存成 JSON，
配置里加一行就生效，老库不用动。这正是"只改配置"能成立的前提。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  id          TEXT PRIMARY KEY,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  state       TEXT NOT NULL DEFAULT 'intake',
  chief       TEXT NOT NULL DEFAULT '',
  slots       TEXT NOT NULL DEFAULT '{}',
  profile     TEXT NOT NULL DEFAULT '{}',
  domain      TEXT NOT NULL DEFAULT '',
  attempts    TEXT NOT NULL DEFAULT '{}',
  danger      INTEGER NOT NULL DEFAULT 0,
  danger_msg  TEXT NOT NULL DEFAULT '',
  dept        TEXT NOT NULL DEFAULT '',
  urgency     TEXT NOT NULL DEFAULT '',
  report      TEXT NOT NULL DEFAULT '',
  record      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS turn (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id, seq);
"""

# 老库升级用：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 而"基本信息表单"是后加的，线上那个 previsit.db 里肯定没有 profile 列。
_ADDED_COLUMNS = {
    "profile": "TEXT NOT NULL DEFAULT '{}'",
    "record": "TEXT NOT NULL DEFAULT ''",
    # domain：会话判定的症状域，锁定后不再变（槽位集合由它派生）
    "domain": "TEXT NOT NULL DEFAULT ''",
    # attempts：每个槽位问过几次，用来"问两次还问不出就跳过"
    "attempts": "TEXT NOT NULL DEFAULT '{}'",
    # ros：知识图谱追问（系统回顾）的状态——问过哪些、患者答了什么、当前
    # 挂着哪一个问题在等回答。同样存 JSON，加字段不用迁移。
    "ros": "TEXT NOT NULL DEFAULT '{}'",
    # last_ask：上一句问的是哪个槽位。否定快答必须知道"患者这句话否的是谁"，
    # 靠"追问队列第一个"猜会猜错——重问中的槽位 attempts 已到顶被排除，
    # 答案就会被安到下一个无辜槽位头上。
    "last_ask": "TEXT NOT NULL DEFAULT ''",
    # dx：诊断线索（只给医生看/给引擎决定追问，不给患者下诊断）。
    # 存 JSON：归一后的标准症状、本地图谱锁定的候选病、科室投票、MedRAG
    # 命中的病种。都是"线索"不是"结论"，加字段不用迁移。
    "dx": "TEXT NOT NULL DEFAULT '{}'",
}


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        have = {r["name"] for r in c.execute("PRAGMA table_info(session)")}
        for col, decl in _ADDED_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE session ADD COLUMN {col} {decl}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------------- session
    def create(self, chief: str, empty_slots: dict[str, Any],
               profile: dict[str, Any] | None = None,
               domain: str = "") -> str:
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO session(id, created_at, updated_at, state, chief, slots, profile, domain)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (sid, now, now, "intake", chief,
                 json.dumps(empty_slots, ensure_ascii=False),
                 json.dumps(profile or {}, ensure_ascii=False),
                 domain or ""),
            )
        return sid

    def get(self, sid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
            if not row:
                return None
            turns = c.execute(
                "SELECT role, content, created_at FROM turn WHERE session_id=? ORDER BY seq",
                (sid,),
            ).fetchall()
        out = dict(row)
        for col in ("slots", "profile", "attempts", "ros", "dx"):
            try:
                out[col] = json.loads(out.get(col) or "{}")
            except json.JSONDecodeError:
                out[col] = {}
        try:                       # record 存的是结构化病历 JSON，空串 = 还没生成
            out["record"] = json.loads(out.get("record") or "null")
        except json.JSONDecodeError:
            out["record"] = None
        out["turns"] = [{"role": t["role"], "content": t["content"]} for t in turns]
        return out

    def update(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("slots", "profile", "record", "attempts", "ros", "dx"):
                v = json.dumps(v, ensure_ascii=False) if v is not None else ""
            if k == "danger":
                v = 1 if v else 0
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=?")
        vals.append(time.time())
        vals.append(sid)
        with self._conn() as c:
            c.execute(f"UPDATE session SET {', '.join(cols)} WHERE id=?", vals)

    # ---------------------------------------------------------------- turn
    def add_turn(self, sid: str, role: str, content: str) -> None:
        with self._conn() as c:
            n = c.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turn WHERE session_id=?", (sid,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO turn(session_id, seq, role, content, created_at) VALUES(?,?,?,?,?)",
                (sid, int(n) + 1, role, content, time.time()),
            )
