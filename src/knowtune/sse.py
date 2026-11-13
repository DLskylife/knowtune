"""SSE（Server-Sent Events）事件编码。

前后台问答都走流式输出，事件格式必须一致：两套前端 app.js 用同一份解析器
读 ``event:`` / ``data:`` 行。这里放一份，避免两边各自定义后悄悄发散。
"""

from __future__ import annotations

import json


def sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"