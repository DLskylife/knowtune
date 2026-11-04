"""走一遍「单模型多答」页的真实交互：配置 → 生成 → 打分 → 提交 → 导出。

    evals/.venv/Scripts/python.exe evals/shot_sampling.py

依赖 mock 端点（mock_endpoint.py --port 18080）与运行中的 scoreboard.py（:7860）。
mock 对同一句话只回一句，K 条样本会完全一样——这正好用来验证「样本雷同」的
提示；导出 DPO 前脚本会把后两条样本各改一句话，模拟人工修订后的差异性。
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
    # 两个页签的控件同名（base_url / model / …），必须只取当前可见的那个，
    # 否则会去填另一个页签里 display:none 的输入框，等 30 秒后超时。
    page.get_by_label(label, exact=False).filter(visible=True).first.fill(value)


def click(page, name: str) -> None:
    page.get_by_role("button", name=name).filter(visible=True).first.click()


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1400})
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "score-0-tabs.png"), full_page=False)

        page.get_by_role("tab", name="单模型多答 + DPO").click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "score-1-empty.png"), full_page=True)

        fill(page, "模型名", "mock-doctor")
        fill(page, "base_url", MOCK)
        fill(page, "model", "doctor")
        click(page, "开始 / 新建一轮")
        page.wait_for_timeout(2000)

        # 只生成本题：演示只需要一条题，全量 20×3 条要跑一分钟，
        # 而且加载遮罩会盖住整页，截出来什么也看不见。
        click(page, "🔄 重生成本题")
        for _ in range(60):
            txt = page.locator("#samphead").inner_text()
            if "耗时" in txt:
                break
            page.wait_for_timeout(1000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "score-2-generated.png"), full_page=True)
        print("casehead:", page.locator("#samphead").inner_text()[:90])

        boxes = page.locator(".cand textarea:visible")
        print("可见样本数：", boxes.count())
        # 后两条各改一句：模拟人工修订（也让 DPO 对子不是一字不差）
        if boxes.count() >= 3:
            boxes.nth(1).fill(boxes.nth(1).input_value() + "\n医生：另外请先量一下体温。")
            boxes.nth(2).fill(boxes.nth(2).input_value() + "\n医生：建议先休息观察。")

        for label, value in [
            ("1 · 准确", 5), ("1 · 切题", 5), ("1 · 安全", 4), ("1 · 表达", 4),
            ("2 · 准确", 3), ("2 · 切题", 3), ("2 · 安全", 3), ("2 · 表达", 4),
            ("3 · 准确", 2), ("3 · 切题", 2), ("3 · 安全", 2), ("3 · 表达", 3),
        ]:
            try:
                page.get_by_label(label, exact=False).filter(visible=True).first.fill(str(value))
            except Exception as exc:
                print(f"  滑块 {label} 失败：{type(exc).__name__}: {exc}")
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT / "score-3-scored.png"), full_page=True)

        click(page, "✅ 提交并下一条")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "score-4-stats.png"), full_page=True)
        print("stats:", page.locator("text=DPO 素材").first.inner_text()[:120])

        click(page, "⬇ 导出 DPO 对子")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "score-5-export.png"), full_page=True)

        # 回到 A/B 页，确认两个页签共存没把原页面弄坏
        page.get_by_role("tab", name="A/B 双模型盲测").click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "score-6-ab-tab.png"), full_page=False)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
