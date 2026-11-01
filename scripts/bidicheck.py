"""维吾尔语排版方向实测（开发期自查）。

用户问"维语是倒着写的吗"。这个问题不能靠眼睛判断：阿拉伯字母本来就从右往左
写，"看起来是反的"和"真的渲染反了"在截图上长得一样。所以这里做两件事：

1. 把库里一条真实答案塞进前端的 localStorage 存档，让**线上页面自己的**
   renderChat/renderAnswer 去渲染它——不重写渲染逻辑，不造测试专用路径。
2. 用 Range 量每一段第一个字符的屏幕 x，和段落右边界比。首字符贴在右边
   = RTL 正确；贴在左边 = 整段被当成 LTR 排，词序对语者是反的。

顺便量一个对照组：同一段文字分别强制 dir=rtl / dir=ltr，看一眼浏览器给出的
首字符落点差多少，确认"量 x"这个判据本身是灵敏的。

用法:
    .venv/Scripts/python.exe scripts/bidicheck.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
ROOT = Path(__file__).resolve().parents[1]
PORT = 9224

# 把真实答案预置进存档，让页面按正常启动流程恢复并渲染它
SEED = r"""
(() => {
  const A = __ANSWER__;
  const Q = __QUESTION__;
  const rec = {
    id: "bidi-1", title: Q.slice(0, 24), at: Date.now(),
    msgs: [{ q: Q, a: A, alt: "", ev: "high", ms: 2400,
             cit: [{ doc: "维医典籍.pdf", sec: "第一章", preview: "四种体液" },
                   { doc: "维医典籍.pdf", sec: "第二章", preview: "三种因素" }] }]
  };
  localStorage.setItem("knowtune.site.chats", JSON.stringify([rec]));
  localStorage.setItem("knowtune.site.session", "bidi-1");
  return "seeded:" + A.length;
})()
"""

MEASURE = r"""
(() => {
  const out = {};
  const p = document.querySelector(".msg.bot .md p") || document.querySelector(".msg.bot .answer p");
  if (!p) return { err: "没有答案段落", cls: (document.querySelector(".msg.bot") || {}).className };
  const cs = getComputedStyle(p);
  const pr = p.getBoundingClientRect();

  // 找到承载正文的文本节点
  let tn = null;
  for (const n of p.childNodes) {
    if (n.nodeType === 3 && n.textContent.trim()) { tn = n; break; }
  }
  if (!tn) return { err: "段落里没有文本节点", html: p.innerHTML.slice(0, 200) };

  const xAt = (i) => {
    const r = document.createRange();
    r.setStart(tn, i); r.setEnd(tn, i + 1);
    const b = r.getBoundingClientRect();
    return Math.round(b.left);
  };

  // 对照组：同一段开头，强制两个方向
  const s = tn.textContent.slice(0, 26);
  const box = document.createElement("div");
  box.style.cssText = "position:absolute;left:0;top:0;width:700px;visibility:hidden";
  box.innerHTML = '<p id="cR" dir="rtl" style="width:700px;font-size:15px">' + s + '</p>'
                + '<p id="cL" dir="ltr" style="width:700px;font-size:15px">' + s + '</p>';
  document.body.appendChild(box);
  const firstOff = (id) => {
    const e = document.getElementById(id), t = e.firstChild;
    const rg = document.createRange(); rg.setStart(t, 0); rg.setEnd(t, 1);
    return Math.round(rg.getBoundingClientRect().left - e.getBoundingClientRect().left);
  };

  out.direction = cs.direction;
  out.align = cs.textAlign;
  out.pLeft = Math.round(pr.left);
  out.pRight = Math.round(pr.right);
  out.pWidth = Math.round(pr.width);
  out.firstCharX = xAt(0);
  out.thirdCharX = xAt(2);
  out.lastCharX = xAt(tn.textContent.length - 1);
  out.textLen = tn.textContent.length;
  out.head = tn.textContent.slice(0, 26);
  out.ctrlRtl = firstOff("cR");
  out.ctrlLtr = firstOff("cL");

  // 用户气泡也量一下：问题那一句是不是也从右边起
  const u = document.querySelector(".msg.user");
  if (u && u.firstChild) {
    const ur = u.getBoundingClientRect();
    const ut = document.createRange(); ut.setStart(u.firstChild, 0); ut.setEnd(u.firstChild, 1);
    out.userDir = getComputedStyle(u).direction;
    out.userFirstX = Math.round(ut.getBoundingClientRect().left);
    out.userRight = Math.round(ur.right);
  }
  box.remove();
  return out;
})()
"""


async def run(url: str, out: Path) -> int:
    import sqlite3

    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT content FROM message WHERE id=40").fetchone()
    con.close()
    if not row:
        print("库里没有 id=40 这条消息", file=sys.stderr)
        return 2
    answer = row["content"]
    question = "ئۇيغۇر تېبابىتىدىكى تۆت خىل خىلىت قايسىلار؟"

    prof = tempfile.mkdtemp(prefix="edge-bidi-")
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-proxy-server", "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
         "--blink-settings=preferredColorScheme=1",
         "--window-size=1100,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(80):
        try:
            for t in httpx.get(f"http://127.0.0.1:{PORT}/json/list", timeout=1).json():
                if (t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                        and url.split("//")[1].split("/")[0] in (t.get("url") or "")):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
        except Exception:
            pass
        if ws_url:
            break
        time.sleep(0.5)
    if not ws_url:
        proc.terminate()
        print("连不上调试端口", file=sys.stderr)
        return 1

    try:
        async with websockets.connect(ws_url, max_size=40 * 1024 * 1024) as ws:
            seq = 0

            async def call(method, params=None):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        return msg

            async def evaluate(expr, timeout_ms=60000):
                r = await call("Runtime.evaluate", {
                    "expression": expr, "awaitPromise": True,
                    "returnByValue": True, "timeout": timeout_ms})
                res = r.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"].get("text")
                                       or json.dumps(res["exceptionDetails"], ensure_ascii=False)[:300])
                return res.get("result", {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            # 等页面 boot 完（示例卡出现即 applyChrome 跑过）
            for _ in range(120):
                if await evaluate("!!document.querySelector('.chip')"):
                    break
                await asyncio.sleep(0.25)

            seed = (SEED.replace("__ANSWER__", json.dumps(answer, ensure_ascii=False))
                        .replace("__QUESTION__", json.dumps(question, ensure_ascii=False)))
            print("写入存档:", await evaluate(seed))

            await call("Page.reload", {"ignoreCache": True})
            # 等恢复后的答案渲染出来
            for _ in range(120):
                done = await evaluate(
                    "!!(document.querySelector('.msg.bot .md p, .msg.bot .answer p'))")
                if done:
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.6)

            data = await evaluate(MEASURE)
            print("=== 线上页面实测 ===")
            print(json.dumps(data, ensure_ascii=False, indent=2))

            if data and "firstCharX" in data:
                pr = data["pRight"]
                d_right = abs(data["firstCharX"] - pr)
                d_left = abs(data["firstCharX"] - data["pLeft"])
                verdict = ("RTL 正确（首字符贴右边）" if d_right < d_left
                           else "方向错了（首字符贴左边，读起来就是倒的）")
                print(f"\n首字符到右边界 {d_right}px、到左边界 {d_left}px → {verdict}")

            shot = await call("Page.captureScreenshot", {"format": "png"})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["result"]["data"]))
            print("png:", out)
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800/"
    o = Path(sys.argv[2] if len(sys.argv) > 2 else "_shot/bidi_live.png")
    if not EDGE.exists():
        print(f"找不到 Edge: {EDGE}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run(u, (ROOT / o).resolve() if not o.is_absolute() else o)))
