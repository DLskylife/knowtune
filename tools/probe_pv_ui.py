"""预问诊界面探针：切到第三个板块、问一句、截图（桌面 + 窄屏）。"""
from __future__ import annotations

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800"


def run(pg, tag: str) -> None:
    pg.goto(BASE, wait_until="networkidle")
    narrow = pg.viewport_size["width"] < 860
    if narrow:
        # 窄屏的侧栏是抽屉：语言开关和板块都在里面
        pg.click("#btnSide")
        pg.wait_for_timeout(450)
    pg.evaluate("""() => {
      const b = document.querySelector('#langSwitch button[data-lang="zh"]');
      if (b) b.click();
    }""")
    pg.wait_for_timeout(400)
    pg.click('.nav-item[data-view="pv"]')
    pg.wait_for_timeout(600)
    pg.fill("#q", "肚子疼了三天，一阵一阵的，还有点恶心")
    pg.click("#btnSend")
    for _ in range(600):
        if pg.evaluate("() => !document.getElementById('status').hidden === false"):
            pass
        if pg.evaluate("() => !!document.querySelector('#pvMsgs .msg.bot:not(.pv-greet)')"):
            break
        pg.wait_for_timeout(150)
    pg.wait_for_timeout(800)
    print(tag, "side=", repr(pg.evaluate("() => document.getElementById('sideScroll').innerText")))
    pg.screenshot(path=f"_shot/{tag}.png")
    print("shot", tag)


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
    d = b.new_page(viewport={"width": 1440, "height": 900})
    run(d, "ui_pv_zh")
    m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    run(m, "ui_pv_mobile")
    b.close()
