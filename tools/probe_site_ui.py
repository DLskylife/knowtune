"""前台界面探针：空态 / 对话态 / 窄屏 / 深色，四张截图 + 关键几何与配色。

用法：python tools/probe_site_ui.py [zh|ug] [--ask]
默认只截空态（快）；带 --ask 会真提一个问题再把对话态截下来。
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800"
UI = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "zh"
ASK = "--ask" in sys.argv
Q = os.environ.get("Q", "膝盖疼，蹲下去站不起来，走路的时候关节里响")

PROBE = """() => {
  const g = (sel, props) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const out = { w: Math.round(r.width), h: Math.round(r.height),
                  x: Math.round(r.x), y: Math.round(r.y) };
    for (const p of props) out[p] = cs.getPropertyValue(p).trim();
    return out;
  };
  return {
    bodyBg: getComputedStyle(document.body).backgroundColor,
    side: g('.side', ['background-color', 'border-right-width']),
    brand: g('.brand-mark', ['background-image', 'border-radius']),
    newChat: g('#btnNewChat', ['height', 'border-radius', 'background-color', 'border-color']),
    nav: g('.nav-item', ['height', 'color', 'font-size']),
    welcome: g('.wel-text', ['font-size', 'font-weight', 'color']),
    box: g('.box', ['border-radius', 'background-color', 'border-color', 'box-shadow']),
    send: g('.send', ['width', 'height', 'border-radius', 'background-color', 'color']),
    user: g('.msg.user', ['background-color', 'color', 'border-radius']),
    answer: g('.answer', ['font-size', 'line-height', 'color']),
    hiddenTop: (() => { const t = document.querySelector('.top');
      return t ? getComputedStyle(t).display : 'none'; })(),
  };
}"""


def measure(pg, tag: str) -> None:
    print(f"--- {tag} ---")
    for k, v in pg.evaluate(PROBE).items():
        print(f"  {k}: {v}")


def shot(pg, name: str, full: bool = False) -> None:
    pg.screenshot(path=f"_shot/{name}.png", full_page=full)
    print("shot:", name)


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])

        # 1) 空态 · 桌面
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE, wait_until="networkidle")
        btn = pg.query_selector(f'#langSwitch button[data-lang="{UI}"]')
        if btn:
            btn.click()
        pg.wait_for_timeout(700)
        measure(pg, "empty")
        shot(pg, f"ui_empty_{UI}")

        if ASK:
            pg.fill("#q", Q)
            pg.click("#btnSend")
            for _ in range(900):
                if pg.evaluate("() => !!document.querySelector('.msg.bot .foot')"):
                    break
                pg.wait_for_timeout(100)
            pg.wait_for_timeout(400)
            measure(pg, "chat")
            shot(pg, f"ui_chat_{UI}", full=True)
            pg.close()

            # 2) 深色
            dk = b.new_page(viewport={"width": 1440, "height": 900},
                            color_scheme="dark")
            dk.goto(BASE, wait_until="networkidle")
            db = dk.query_selector(f'#langSwitch button[data-lang="{UI}"]')
            if db:
                db.click()
            dk.wait_for_timeout(500)
            dk.fill("#q", Q)
            dk.click("#btnSend")
            for _ in range(900):
                if dk.evaluate("() => !!document.querySelector('.msg.bot .foot')"):
                    break
                dk.wait_for_timeout(100)
            shot(dk, f"ui_dark_{UI}")
            dk.close()

            # 3) 窄屏
            mb = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
            mb.goto(BASE, wait_until="networkidle")
            # 窄屏的侧栏是抽屉，先点菜单键把语言开关露出来，再关掉抽屉
            mb.click("#btnSide")
            mb.wait_for_timeout(400)
            xb = mb.query_selector(f'#langSwitch button[data-lang="{UI}"]')
            if xb:
                xb.click()
            mb.wait_for_timeout(400)
            # 抽屉盖住大半个屏幕，点 scrim 的中心会落在侧栏上；
            # 直接派发 click 事件关掉它，避免和遮罩的层级打架。
            mb.evaluate("() => document.getElementById('scrim').click()")
            mb.wait_for_timeout(400)
            mb.fill("#q", Q)
            mb.click("#btnSend")
            for _ in range(900):
                if mb.evaluate("() => !!document.querySelector('.msg.bot .foot')"):
                    break
                mb.wait_for_timeout(100)
            shot(mb, f"ui_mobile_{UI}")
            mb.close()

        print("pageerror:", errs or "无")
        b.close()


if __name__ == "__main__":
    main()
