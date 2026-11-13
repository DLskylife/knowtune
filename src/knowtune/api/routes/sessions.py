"""会话路由：固定会话、列出/改名/删除/清空/批量删除，以及问答反馈。"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import logbus
from ...db import utcnow as _utcnow
from ..state import state

router = APIRouter()


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


@router.get("/api/session/current")
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


@router.post("/api/sessions/{session_id}/clear")
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


@router.get("/api/sessions")
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


@router.get("/api/sessions/{session_id}/messages")
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


@router.patch("/api/sessions/{session_id}")
def rename_session(session_id: str, req: SessionPatch) -> dict:
    cur = state.conn.execute(
        "UPDATE session SET title=?, updated_at=? WHERE id=?",
        (req.title.strip(), _utcnow(), session_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "id": session_id, "title": req.title.strip()}


@router.delete("/api/sessions/{session_id}")
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


@router.post("/api/sessions/bulk-delete")
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


class FeedbackReq(BaseModel):
    message_id: int
    verdict: str = Field(pattern="^(up|down)$")
    comment: str = ""


@router.post("/api/feedback")
def feedback(req: FeedbackReq) -> dict:
    state.conn.execute(
        "INSERT OR REPLACE INTO feedback (message_id, verdict, comment, created_at)"
        " VALUES (?,?,?,?)",
        (req.message_id, req.verdict, req.comment, _utcnow()),
    )
    return {"ok": True}