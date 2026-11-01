"""线上页面实测（开发期自查，不参与线上运行）。

为什么不用 curl：前台的问题大多只在真浏览器里出现——流式渲染、localStorage
存档、切语言时元素位移。curl 只能证明接口返回 200。

做法：起一个带 remote-debugging 的 headless Edge，用 CDP 在页面里直接跑 JS，
点示例问题 → 等真实答案 → 量骨架位置 → 切语言 → 再量一遍 → 截图。

用法:
    .venv/Scripts/python.exe scripts/livecheck.py
    .venv/Scripts/python.exe scripts/livecheck.py http://127.0.0.1:8800/ _shot/live.png
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
PORT = 9223

ASK = r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const chip = document.querySelector(".chip");
  if (!chip) return { err: "没有示例卡，页面可能没起来" };
  const t0 = Date.now();
  chip.click();
  while (Date.now() - t0 < 180000) {
    if (document.querySelector(".msg.bot .foot")) break;
    if (document.querySelector(".msg.bot .answer.err")) break;
    await sleep(400);
  }
  const ans = document.querySelector(".msg.bot .answer");
  const badge = document.querySelector(".ev-badge");
  return {
    ms: Date.now() - t0,
    chars: ans ? ans.textContent.length : 0,
    bad: !!document.querySelector(".msg.bot .answer.err"),
    ev: badge ? badge.textContent.trim() : "",
    cites: document.querySelectorAll(".cite").length,
    sess: document.querySelectorAll(".sess").length,
    stored: (localStorage.getItem("knowtune.site.chats") || "").length,
    answer: ans ? ans.textContent.slice(0, 90) : "",
  };
})()
"""

MEASURE = r"""
(() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.right)];
  };
  return {
    side: pick(".side"),
    lang: pick("#langSwitch"),
    send: pick("#btnSend"),
    box: pick(".box"),
    bot: pick(".msg.bot"),
    user: pick(".msg.user"),
  };
})()
"""


async def run(url: str, out: Path) -> int:
    prof = tempfile.mkdtemp(prefix="edge-cdp-")
    proc = subprocess.Popen(
        [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-proxy-server", "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
         "--blink-settings=preferredColorScheme=1",
         "--window-size=1100,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = None
    for _ in range(60):
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

            async def call(method: str, params: dict | None = None):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        return msg

            async def evaluate(expr: str, timeout_ms: int = 200000):
                r = await call("Runtime.evaluate", {
                    "expression": expr, "awaitPromise": True,
                    "returnByValue": True, "timeout": timeout_ms,
                })
                res = r.get("result", {})
                if "exceptionDetails" in res:
                    raise RuntimeError(res["exceptionDetails"].get("text")
                                       or str(res["exceptionDetails"]))
                return res.get("result", {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            # 等页面真的跑完 boot（示例卡是 applyChrome 之后才有的）：
            # 直接 sleep 固定秒数会偶发拿到"还在加载"的空 DOM。
            ready = await evaluate(r"""
              (async () => {
                const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                for (let i = 0; i < 120; i++) {
                  if (document.querySelector(".chip")) return "ok";
                  await sleep(250);
                }
                return document.title + " / " + document.readyState;
              })()
            """, 40000)
            print("页面就绪:", ready)

            print("提问…（真实调用模型，可能要一分钟以上）")
            got = await evaluate(ASK)
            print("答案: ", json.dumps(got, ensure_ascii=False)[:400])
            if not got or got.get("bad"):
                print("真实问答失败，截图留证据")
            before = await evaluate(MEASURE)
            shot = await call("Page.captureScreenshot", {"format": "png"})
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(base64.b64decode(shot["result"]["data"]))

            zh = await evaluate(r"""
              (async () => {
                const b = document.querySelector('#langSwitch [data-lang="zh"]');
                if (!b) return "no-btn";
                b.click();
                await new Promise((r) => setTimeout(r, 400));
                return "ok";
              })()
            """)
            after = await evaluate(MEASURE)
            moved = {k: [before.get(k), after.get(k)] for k in (before or {})
                     if before.get(k) != after.get(k)}
            print("切到中文:", zh, "| 骨架位移:", json.dumps(moved, ensure_ascii=False))
            out2 = out.with_name(out.stem + "_zh.png")
            shot = await call("Page.captureScreenshot", {"format": "png"})
            out2.write_bytes(base64.b64decode(shot["result"]["data"]))
            print(f"png:  {out}\npng:  {out2}")
            return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800/"
    o = Path(sys.argv[2] if len(sys.argv) > 2 else "_shot/live_v17.png")
    if not EDGE.exists():
        print(f"找不到 Edge: {EDGE}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run(u, (ROOT / o).resolve() if not o.is_absolute() else o)))
