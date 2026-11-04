"""用系统 Edge 走一遍真实交互：配置 → 生成 → 打分 → 提交 → 统计，逐步截图。

    evals/.venv/Scripts/python.exe evals/shot_arena.py

依赖 mock 端点（mock_endpoint.py --port 18080）与运行中的页面（:7860）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "_shot"
OUT.mkdir(exist_ok=True)
URL = "http://127.0.0.1:7860/"
MOCK = "http://127.0.0.1:18080/v1"


def fill(page, label: str, value: str) -> None:
    page.get_by_label(label, exact=False).first.fill(value)


def set_slider(page, label: str, value: int) -> None:
    page.get_by_label(label, exact=False).first.fill(str(value))


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1180})
        page.goto(URL, wait_until="load")   # Gradio 常驻长连接，等 networkidle 会永远等下去
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "arena-1-empty.png"), full_page=True)

        fill(page, "A base_url", MOCK)
        fill(page, "A model", "mock-base")
        fill(page, "B base_url", MOCK)
        fill(page, "B model", "mock-doctor")

        page.get_by_role("button", name="开始 / 新建一轮").click()
        page.wait_for_timeout(2500)

        page.get_by_role("button", name="⚡ 全部生成").click()
        for _ in range(90):
            try:
                txt = page.locator("#casehead").inner_text()
            except Exception:
                txt = ""
            if "耗时" in txt or "全部评完" in txt:
                break
            page.wait_for_timeout(1000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "arena-2-generated.png"), full_page=True)
        print("casehead:", page.locator("#casehead").inner_text()[:70])

        cands = page.locator(".cand textarea")
        for i in range(cands.count()):
            print(f"  候选[{i}]", cands.nth(i).input_value()[:70].replace("\n", " | "))

        # 左侧判优：准确/切题/安全/表达 = 5/4/3/2；右侧 2/2/3/3
        for label, value in [
            ("① 准确", 5), ("① 切题", 4), ("① 安全", 3), ("① 表达", 2),
            ("② 准确", 2), ("② 切题", 2), ("② 安全", 3), ("② 表达", 3),
        ]:
            try:
                set_slider(page, label, value)
            except Exception as exc:
                print(f"  滑块 {label} 失败：{type(exc).__name__}: {exc}")
        page.get_by_label("① 更好", exact=False).first.click()
        fill(page, "备注", "左侧先问红旗，右侧一次问五个并推荐用药")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "arena-3-scored.png"), full_page=True)

        page.get_by_role("button", name="✅ 提交并下一条").click()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "arena-4-next.png"), full_page=True)
        print("stats:", page.locator("text=胜负").first.inner_text()[:160])

        page.get_by_role("button", name="⬇ 导出 CSV").click()
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "arena-5-export.png"), full_page=True)

        # 多轮用例：从题目下拉跳到 p18（4 轮），确认整段对话能完整显示
        page.get_by_label("题目（✓ 已评）").click()
        page.wait_for_timeout(800)
        page.locator("li", has_text="p18").first.click()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "arena-6-multiturn.png"), full_page=True)
        print("多轮 casehead:", page.locator("#casehead").inner_text()[:70])
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
