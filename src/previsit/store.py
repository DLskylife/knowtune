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
  content_int TEXT NOT NULL DEFAULT '',
  lang        TEXT NOT NULL DEFAULT '',
  translated  INTEGER NOT NULL DEFAULT 1,
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
    # content_int：每条 turn 额外存一份"模型内部语言"文本（目前只给维语会话用）。
    # 维语走"本地 7B 用中文推理 + 外部模型翻译"的链路：7B 全程看中文，
    # 所以内部上下文是中文；界面给患者看的 content 才是维吾尔语。加字段不用迁移。
    # lang：会话创建时的界面语言（zh / ug / en）。reply 请求没带语言时以它为准——
    # 前端被缓存住旧 JS、漏发 ui_lang 时，患者不该突然收到另一种语言。
    "lang": "TEXT NOT NULL DEFAULT ''",
    "content_int": "TEXT NOT NULL DEFAULT ''",
}

# turn 表后加的列（同上面的迁移逻辑，但作用在 turn 上）。
_ADDED_TURN_COLUMNS = {
    "content_int": "TEXT NOT NULL DEFAULT ''",
    # lang：这一条正文是哪种语言（ug / zh / en）。问答板块每条回答都带
    # `answer_lang`（前端 `rec.al`），渲染时据此打"某某语作答"的标记；
    # 预问诊以前只有会话级的语言，切一次语言整场都错，这里改成逐条记录。
    "lang": "TEXT NOT NULL DEFAULT ''",
    # translated：目标语言是否真的译成了。0 = 正文是原文（中文），界面要挂
    # 一条"翻译未完成"的提示——问答板块同一含义的字段叫 answer_translated。
    "translated": "INTEGER NOT NULL DEFAULT 1",
    # content_ug：这条 turn 的维吾尔语版本。谁生成的不重要——中文界面问出来的
    # 回答，患者切到维语时要看得到维语；缺的由 `ChatEngine.localize()` 调一次
    # 翻译补上并写回这里，只补一次。content_int 始终是中文那份，两者一起
    # 构成"同一句话的两个语言版本"，切语言只换文本、不重开、不重新生成。
    "content_ug": "TEXT NOT NULL DEFAULT ''",
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
        have_s = {r["name"] for r in c.execute("PRAGMA table_info(session)")}
        for col, decl in _ADDED_COLUMNS.items():
            if col not in have_s:
                c.execute(f"ALTER TABLE session ADD COLUMN {col} {decl}")
        # turn 表也要迁移：content_int 是后加的（维语会话存内部语言）。
        have_t = {r["name"] for r in c.execute("PRAGMA table_info(turn)")}
        for col, decl in _ADDED_TURN_COLUMNS.items():
            if col not in have_t:
                c.execute(f"ALTER TABLE turn ADD COLUMN {col} {decl}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    # ---------------------------------------------------------------- session
    def create(self, chief: str, empty_slots: dict[str, Any],
               profile: dict[str, Any] | None = None,
               domain: str = "", lang: str = "") -> str:
        sid = uuid.uuid4().hex[:16]
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO session(id, created_at, updated_at, state, chief, slots, profile, domain, lang)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, now, now, "intake", chief,
                 json.dumps(empty_slots, ensure_ascii=False),
                 json.dumps(profile or {}, ensure_ascii=False),
                 domain or "", (lang or "").strip()),
            )
        return sid

    def get(self, sid: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
            if not row:
                return None
            turns = c.execute(
                "SELECT role, content, content_int, content_ug, lang, translated, created_at"
                " FROM turn WHERE session_id=? ORDER BY seq",
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
        out["turns"] = [{"role": t["role"], "content": t["content"],
                         "content_int": t["content_int"] or t["content"],
                         "content_ug": t["content_ug"] or "",
                         "lang": t["lang"] or "",
                         "translated": bool(t["translated"])}
                        for t in turns]
        return out

    def turns_raw(self, sid: str) -> list[dict[str, Any]]:
        """带 id 的原始流水，只给"补译文"这条路用（要按 id 回填）。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, role, content, content_int, content_ug FROM turn"
                " WHERE session_id=? ORDER BY seq",
                (sid,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_turn_ug(self, turn_id: int, text: str) -> None:
        """把某条的维吾尔语版本写回去。翻译只做一次，下次切语言直接读。"""
        with self._conn() as c:
            c.execute("UPDATE turn SET content_ug=? WHERE id=?", (text or "", turn_id))

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
    def add_turn(self, sid: str, role: str, content: str,
                 content_int: str = "", lang: str = "",
                 translated: bool = True, content_ug: str = "") -> None:
        # content：界面展示语言（维语会话里是维吾尔语）；content_int：喂给模型
        # 的内部语言（维语链路里是中文，让本地 7B 在自己最强的语言上推理）；
        # content_ug：维语版本（中文界面下先留空，切到维语时由 localize 补）。
        with self._conn() as c:
            n = c.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM turn WHERE session_id=?", (sid,)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO turn(session_id, seq, role, content, content_int,"
                " content_ug, lang, translated, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, int(n) + 1, role, content, content_int or "",
                 content_ug or "", lang or "", 1 if translated else 0, time.time()),
            )
