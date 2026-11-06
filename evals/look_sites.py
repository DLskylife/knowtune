"""冒烟截图：主站五个视图 + 站点页两张卡的「模型入口」行。

用法：evals/.venv/Scripts/python.exe evals/look_sites.py [port]
只读，不点任何会写配置的按钮。
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8010"
BASE = f"http://127.0.0.1:{PORT}/"
OUT = "_shot"

VIEWS = [
    ("home", "首页"), ("chat", "对话"), ("kb", "知识库"),
    ("models", "模型"), ("site", "站点"),
]


def main() -> int:
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="msedge", headless=True)
        page = b.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="load")
        page.wait_for_timeout(1800)

        probes: dict[str, str] = {}
        for view, label in VIEWS:
            page.locator(f'.nav-item[data-view="{view}"]').click()
            page.wait_for_timeout(1300)
            page.screenshot(path=f"{OUT}/v3-{view}.png", full_page=False)
            got = page.evaluate("""() => {
              const sels = ['.card', '.svc-card', '.site-block', '.ev-card', '.kpi'];
              const out = {};
              for (const s of sels) {
                const el = [...document.querySelectorAll(s)].find(e => e.offsetParent);
                if (!el) continue;
                const c = getComputedStyle(el);
                out[s] = [c.backgroundColor, c.borderColor, c.borderRadius].join(' | ');
              }
              return out;
            }""")
            probes.update(got)
            print(f"{label}: ok")

        # 站点页往下滚，把预问诊卡和评测台卡都收进画面
        page.locator('.nav-item[data-view="site"]').click()
        page.wait_for_timeout(1300)
        rows = page.locator(".gw-inline")
        print("模型入口行数：", rows.count(), "（应为 2：预问诊 + 评测台）")
        for i in range(rows.count()):
            print("  -", " ".join(rows.nth(i).inner_text().split())[:160])
        page.locator(".site-block[data-pv]").scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        page.screenshot(path=f"{OUT}/v3-site-previsit.png", full_page=False)
        page.locator(".ev-card").scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        page.screenshot(path=f"{OUT}/v3-site-eval.png", full_page=False)

        # 卡片基准是否真的统一：五个视图里出现的各类卡片，背景/边框/圆角
        # 必须落在同一组值上。这是样式统一后的回归探针，别靠眼睛找。
        print("=== 卡片基准（五视图合并） ===")
        for k, v in probes.items():
            print(f"  {k:14s} {v}")
        vals = set(probes.values())
        print("  不同组合数：", len(vals), "（1 = 全部统一）")
        print("JS 错误：", errors or "无")
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
