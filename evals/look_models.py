"""冒烟截图：模型服务页（新配置模型 + 本地模型扫描面板）。

用法：evals/.venv/Scripts/python.exe evals/look_models.py [port]
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "8010"
BASE = f"http://127.0.0.1:{PORT}/"
OUT = "_shot"


def main() -> int:
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="msedge", headless=True)
        page = b.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="load")
        page.wait_for_timeout(2000)

        # 模型服务页
        page.locator('[data-view="models"]').click()
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{OUT}/models-page.png", full_page=False)

        # 展开本地模型说明折叠区，再点扫描
        page.locator("details.launch-fold summary, details summary").filter(
            has_text="本地模型怎么跑起来").first.click()
        page.wait_for_timeout(500)
        page.get_by_role("button", name="扫描本地模型").click()
        page.wait_for_timeout(4000)
        page.screenshot(path=f"{OUT}/models-scan.png", full_page=True)

        # 把 1.8B 加入端点 → 看端点卡片
        page.get_by_role("button", name="＋ 加入").last.click()
        page.wait_for_timeout(2500)
        page.screenshot(path=f"{OUT}/models-endpoint.png", full_page=True)

        print("JS 错误：", errors or "无")
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
