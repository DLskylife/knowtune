"""反馈路由：回答的「有用 / 无用」。

会话历史不归后台——后台问答的上下文由请求带 history，前台会话在浏览器
localStorage，两边都不在后台建会话。这里只剩 feedback：它按 message_id
落一条评价，与会话管理无关。前台还有自己那份 /api/feedback（site.py），
两处字段一致，共用 feedback 表。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ...db import utcnow as _utcnow
from ..state import state

router = APIRouter()


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
