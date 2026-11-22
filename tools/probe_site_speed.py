"""前台问答计时探针：验证阶段秒表、耗时拆分与首字延迟。

只做观测，不改页面状态。输出阶段时间线 + 两张截图（等待中 / 完成）。
用法：python tools/probe_site_speed.py "问题" [zh|ug]
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800"
Q = sys.argv[1] if len(sys.argv) > 1 else "膝盖疼，蹲下去站不起来，走路的时候关节里响"
UI = sys.argv[2] if len(sys.argv) > 2 else "zh"


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        btn = pg.query_selector(f'#langSwitch button[data-lang="{UI}"]')
        if btn:
            btn.click()
        else:
            print("注意：没找到语言按钮", UI)
        pg.wait_for_timeout(700)

        t0 = time.time()
        pg.fill("#q", Q)
        pg.click("#btnSend")

        timeline: list[tuple[float, str]] = []
        shot = False
        for _ in range(600):
            txt = pg.evaluate("() => { const e = document.getElementById('status');"
                              " return e && !e.hidden ? e.textContent : ''; }")
            if txt and (not timeline or timeline[-1][1] != txt):
                timeline.append((time.time() - t0, txt))
            if not shot and txt and "s" in txt and time.time() - t0 > 1.0:
                pg.screenshot(path="_shot/site_wait.png")
                shot = True
            if pg.evaluate("() => !!document.querySelector('.msg.bot .foot')"):
                break
            pg.wait_for_timeout(100)

        total = time.time() - t0
        foot = pg.evaluate(
            "() => { const f = [...document.querySelectorAll('.msg.bot .foot')].pop();"
            " return f ? f.textContent : ''; }")
        ans = pg.evaluate(
            "() => { const a = [...document.querySelectorAll('.msg.bot .answer')].pop();"
            " return a ? a.textContent : ''; }")
        pg.screenshot(path="_shot/site_done.png", full_page=True)
        print("总耗时 %.1fs" % total)
        last = ""
        for t, s in timeline:
            base = s.rsplit(" ", 1)[0] if s.endswith("s") else s
            if base != last:
                print("  %5.1fs  %s" % (t, s))
                last = base
        print("脚注:", foot.strip()[:120])
        print("答案:", ans.strip()[:160].replace("\n", " "))
        print("pageerror:", errs or "无")
        b.close()


if __name__ == "__main__":
    main()
