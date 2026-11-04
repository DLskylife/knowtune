"""通用档案的端到端演示：切档案 → 载入 → 生成 → 打分 → 提交 → 统计 → 导出。

两个页签的控件同名，所有定位都必须 `.filter(visible=True)`——否则会去操作
另一个页签里 display:none 的输入框，然后等 30 秒超时。

    evals/.venv/Scripts/python.exe evals/shot_general.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "_shot"
OUT.mkdir(exist_ok=True)

URL = "http://127.0.0.1:7860/"
DIMS = ("准确", "完整", "可用", "清晰")


def click(page, name: str) -> None:
    page.get_by_role("button", name=name).filter(visible=True).first.click()


def fill(page, label: str, value: str) -> None:
    page.get_by_label(label, exact=False).filter(visible=True).first.fill(value)


def score(page, sample: int, values: tuple[int, ...]) -> None:
    for dim, val in zip(DIMS, values):
        loc = page.get_by_label(f"{sample} · {dim}", exact=False)
        loc = loc.filter(visible=True).first
        # range input 用 fill 会被框架拦，直接设值再派发事件最稳
        loc.evaluate(
            "(el, v) => { const setter = Object.getOwnPropertyDescriptor("
            "window.HTMLInputElement.prototype, 'value').set;"
            "setter.call(el, String(v));"
            "el.dispatchEvent(new Event('input', {bubbles: true}));"
            "el.dispatchEvent(new Event('change', {bubbles: true})); }", val)
        page.wait_for_timeout(120)


def main() -> None:
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="msedge", headless=True)
        page = b.new_page(viewport={"width": 1500, "height": 1300})
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(2600)

        # ① A/B 页：切到通用档案，看滑块标签跟没跟着换
        page.get_by_label("评测档案", exact=False).filter(visible=True).first.click()
        page.wait_for_timeout(600)
        page.get_by_role("option", name="通用问答").click()
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "gen-2-ab-general.png"), full_page=True)

        # ② 采样页：同样切档案，载入题库
        page.get_by_role("tab", name="单模型多答 + DPO").click()
        page.wait_for_timeout(1800)
        page.get_by_label("评测档案", exact=False).filter(visible=True).first.click()
        page.wait_for_timeout(600)
        page.get_by_role("option", name="通用问答").click()
        page.wait_for_timeout(1800)

        fill(page, "模型名（写进结果文件）", "mock-general")
        fill(page, "base_url", "http://127.0.0.1:18080/v1")
        fill(page, "model", "doctor")
        click(page, "开始 / 新建一轮")
        page.wait_for_timeout(2500)
        print("载入：", page.locator("#samphead").inner_text())
        page.screenshot(path=str(OUT / "gen-3-sample-loaded.png"), full_page=True)

        # ③ 前两题：生成本题 → 三档打分 → 提交
        plan = [
            ((5, 5, 4, 5), (4, 4, 4, 4), (2, 2, 2, 2)),
            ((5, 4, 5, 4), (3, 3, 3, 3), (1, 2, 2, 1)),
        ]
        for round_no, scores in enumerate(plan, 1):
            click(page, "🔄 重生成本题")
            for _ in range(40):
                if "耗时" in page.locator("#samphead").inner_text():
                    break
                page.wait_for_timeout(600)
            if round_no == 1:
                page.screenshot(path=str(OUT / "gen-4-sample-generated.png"), full_page=True)
            for i, vals in enumerate(scores, 1):
                score(page, i, vals)
            click(page, "✅ 提交并下一条")
            page.wait_for_timeout(1800)

        stats = page.locator("text=模型评测").last
        stats.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "gen-5-sample-stats.png"), full_page=True)
        print("统计已渲染")

        # ④ 导出 DPO 与报告
        click(page, "⬇ 导出 DPO 对子")
        page.wait_for_timeout(2000)
        click(page, "⬇ 导出评测报告")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "gen-6-sample-export.png"), full_page=True)
        b.close()


if __name__ == "__main__":
    main()
