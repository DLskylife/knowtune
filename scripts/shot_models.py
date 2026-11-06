"""截「模型服务」页的本地模型扫描面板，验证 llama-server 已被认到。

用系统装的 Edge（channel=msedge），不额外下 Chromium：这台机器上浏览器本来就有，
再下一份 120MB 的只为截张图不值。
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "_shot/models.png"
BASE = "http://127.0.0.1:8765"

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
    pg = b.new_page(viewport={"width": 1500, "height": 1100})
    errs: list[str] = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE, wait_until="networkidle")
    # 侧栏「模型服务」
    for label in ("模型服务", "模型"):
        link = pg.get_by_text(label, exact=True)
        if link.count():
            link.first.click()
            break
    pg.wait_for_timeout(2500)
    # 展开扫描面板
    btn = pg.get_by_role("button", name="扫描本地模型")
    if btn.count():
        btn.first.click()
    pg.wait_for_timeout(4000)
    pg.screenshot(path=OUT, full_page=True)
    print("saved", OUT)
    for e in errs[:5]:
        print("console:", e[:160])
    b.close()
