"""后台页面截图（开发期自查）。

为什么不用 headless --screenshot：那个只能截"页面 load 后 3 秒"，而这里要看的
是「点模型页 → 点某张卡片的提示词 → 抽屉渲染出来」之后的状态。没有交互就没有
可看的东西。所以走 CDP：真点、真等、真截。

用法（服务要先起来）：
    .venv/Scripts/python.exe scripts/admin_shot.py --url http://127.0.0.1:8011 \
        --out _shot/admin_prompt.png
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDP:
    def __init__(self, port: int) -> None:
        self.port = port
        self.ws = None
        self.mid = 0

    def connect(self, url: str, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        target = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/list", timeout=1) as r:
                    tabs = json.loads(r.read().decode())
                target = next((t for t in tabs if t.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not target:
            raise RuntimeError("连不上浏览器的调试端口")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                             timeout=30, max_size=64 * 1024 * 1024)
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method: str, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        return r.get("result", {}).get("value")

    def shot(self, path: Path) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8011")
    ap.add_argument("--out", default="_shot/admin_prompt.png")
    ap.add_argument("--click", default="", help="进抽屉后再点的选择器，逗号分隔")
    ap.add_argument("--wait", type=float, default=1.6, help="每次点击后等多久")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=960)
    args = ap.parse_args()

    port = free_port()
    profile = tempfile.mkdtemp(prefix="knowtune-shot-")
    proc = subprocess.Popen([
        EDGE, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", f"--window-size={args.width},{args.height}",
        "--no-first-run", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=2", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        cdp = CDP(port)
        cdp.connect(args.url)
        cdp.call("Emulation.setDeviceMetricsOverride", width=args.width,
                 height=args.height, deviceScaleFactor=2, mobile=False)
        cdp.call("Page.navigate", url=args.url + "/static/index.html")
        time.sleep(3.0)

        # 进模型页，等卡片渲染出来
        cdp.js("document.querySelector('[data-view=models]').click()")
        time.sleep(1.5)
        cards = cdp.js("document.querySelectorAll('.svc-card').length")
        print("模型卡片:", cards)

        # 点第一张卡片上的「提示词」
        cdp.js("document.querySelector('[data-mact=prompt]').click()")
        time.sleep(2.0)
        drawer = cdp.js("!document.getElementById('promptDrawer').hidden")
        print("抽屉打开:", drawer)
        counts = cdp.js(
            "JSON.stringify({roles:document.querySelectorAll('.pr-role').length,"
            "areas:document.querySelectorAll('.pr-field textarea').length,"
            "chars:(document.querySelector('.pr-field textarea')||{}).value?.length})")
        print("抽屉内容:", counts)

        for sel in [s for s in args.click.split(",") if s.strip()]:
            hit = cdp.js(f"(() => {{ const el = document.querySelector({json.dumps(sel)});"
                         f" if (!el) return false; el.click(); return true; }})()")
            print("点击", sel, "->", hit)
            time.sleep(args.wait)

        panel = cdp.js(
            "JSON.stringify({panel: !!document.querySelector('.pr-panel'),"
            "note: (document.getElementById('prNote')||{}).textContent})")
        print("面板:", panel)

        cdp.shot(Path(args.out))
        print("已保存", args.out)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
