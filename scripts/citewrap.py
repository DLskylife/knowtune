"""引用牌掉行回归探针（开发期自查，不参与线上运行）。

背景见 docs/ARCHITECTURE.md 12.34：中文正文里汉字之间处处可断行，而引用牌是
原子 inline 盒，行尾差几像素就会被整体推到下一行，出现"①。"独占行首。

这个脚本把正文列宽从 420 扫到 736（每 2px），对每个宽度量一次"牌是否和它前面
那个字在同一行/是否贴在行首"。对照组塞未绑定的裸 HTML，实验组走真实渲染路径
（paintAnswer 里的 bindCites）。修复前后应该是 14 → 0。

为什么不放进 uismoke：uismoke 是"通过/不通过"的门禁，压一个固定宽度就够；
这里要看的是**沿宽度分布**有没有漏网的窗口，属于排查工具。

用法:
    .venv/Scripts/python.exe scripts/citewrap.py
    .venv/Scripts/python.exe scripts/citewrap.py http://127.0.0.1:8800/
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
PORT = 9231
ROOT = Path(__file__).resolve().parents[1]

# 和用户截图里那段的形状一致：中文正文 + 维吾尔语括注 + 句中/句末各一枚牌，
# 外加一处连引（"[2][4]"）——连着两枚时只有第一枚能靠文本节点绑定。
TEXT = ("资料中未找到“维吾尔医（تبابت يۇنۇنيدا）”四种体液/四种赫里提（خىلىت）的直接对应内容；"
        "资料只涉及藏医体系，且明确列出的是“三大因素”：龙、赤巴、培根[4]，以及"
        "“五源（水、土、火、气、空）”等概念[1]。因此不能用这些资料把藏医“三因”"
        "等同于维吾尔医“四种赫里提”[1][2]。")

SWEEP = r"""
(() => {
  const msgs = document.getElementById("msgs");
  msgs.innerHTML = "";
  document.getElementById("welcome").hidden = true;
  const bot = document.createElement("div"); bot.className = "msg bot";
  const ans = document.createElement("div"); ans.className = "answer alt"; ans.dir = "auto";
  bot.appendChild(ans); msgs.appendChild(bot);
  const text = %s;
  const raw = renderRich(text);
  bot.dataset.main = text;
  bot.dataset.draft = text;
  paintAnswer(bot, ans);
  const bound = ans.innerHTML;

  const stat = {}, hit = {};
  for (const mode of ["before", "fixed"]) {
    stat[mode] = 0; hit[mode] = [];
    for (let w = 420; w <= 736; w += 2) {
      msgs.style.maxWidth = w + "px";
      ans.innerHTML = mode === "before" ? raw : bound;
      const p = ans.querySelector("p");
      const pLeft = p.getBoundingClientRect().left;
      [...ans.querySelectorAll(".cite")].forEach((c) => {
        const prev = c.previousSibling;
        // 绑好的牌前兄弟是 .nb 里的文本节点；裸 HTML 里就是正文文本节点
        const tn = prev && prev.nodeType === 3 ? prev
                 : (prev && prev.nodeType === 1 && prev.firstChild
                    && prev.firstChild.nodeType === 3 ? prev.firstChild : null);
        if (!tn || !tn.textContent.length) return;
        const r = c.getBoundingClientRect();
        const rg = document.createRange();
        const n = tn.textContent.length;
        rg.setStart(tn, n - 1); rg.setEnd(tn, n);
        const sameLine = Math.abs(rg.getBoundingClientRect().top - r.top) < 6;
        const atHead = (r.left - pLeft) < 8;
        if (!sameLine || atHead) {
          stat[mode]++;
          if (hit[mode].length < 4) hit[mode].push({w: w, n: c.textContent,
            same: sameLine, head: atHead, x: Math.round(r.left - pLeft)});
        }
      });
    }
  }
  msgs.style.maxWidth = "";
  return {stat: stat, hit: hit, sample: bound.slice(0, 300)};
})()
"""


async def run(url: str) -> int:
    prof = tempfile.mkdtemp(prefix="edge-cdp-")
    proc = subprocess.Popen([str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-proxy-server", "--no-first-run", "--no-default-browser-check",
        f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
        "--blink-settings=preferredColorScheme=1", "--window-size=1440,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    host = url.split("//")[1].split("/")[0]
    ws_url = None
    for _ in range(60):
        try:
            for t in httpx.get(f"http://127.0.0.1:{PORT}/json/list", timeout=1).json():
                if (t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                        and host in (t.get("url") or "")):
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

            async def call(m, p=None):
                nonlocal seq
                seq += 1
                await ws.send(json.dumps({"id": seq, "method": m, "params": p or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == seq:
                        return msg

            async def ev(e, t=120000):
                r = await call("Runtime.evaluate", {"expression": e, "awaitPromise": True,
                                                    "returnByValue": True, "timeout": t})
                res = r.get("result", {})
                if "exceptionDetails" in res:
                    return {"err": json.dumps(res["exceptionDetails"], ensure_ascii=False)[:600]}
                return res.get("result", {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for _ in range(120):
                if await ev('!!document.querySelector(".chip")'):
                    break
                await asyncio.sleep(0.3)
            await ev("""(async()=>{const b=document.querySelector('#langSwitch [data-lang="zh"]');
                       if(b)b.click(); await new Promise(r=>setTimeout(r,300));return 1})()""")
            got = await ev(SWEEP % json.dumps(TEXT, ensure_ascii=False))
            if isinstance(got, dict) and got.get("err"):
                print(got["err"])
                return 2
            print(json.dumps(got, ensure_ascii=False, indent=1))
            ok = got["stat"]["fixed"] == 0
            print("\n结论：" + ("通过（绑定后 0 次掉行）" if ok
                              else f"仍有 {got['stat']['fixed']} 次掉行"))
            return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8800/"
    if not EDGE.exists():
        print(f"找不到 Edge: {EDGE}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(run(u)))
