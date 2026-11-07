"""验「站点 → 模型与提示词」里预问诊那一行的两个模型下拉。

为什么要跑浏览器：这一行的值来自 /api/previsit/config（全局覆盖层），和表格
其它行（实例 roles）是两个数据源。接口对不代表界面接上了——拼错字段名、
provSelect 收到不认识的形状，都会安静地显示成「跟随系统」，看起来像"没配"。

用法：python scripts/shot_pv_row.py [输出图片路径]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

OUT = sys.argv[1] if len(sys.argv) > 1 else "_shot/pv_row.png"
BASE = "http://127.0.0.1:8013"

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
    pg = b.new_page(viewport={"width": 1500, "height": 1000})
    errs: list[str] = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.goto(BASE, wait_until="networkidle")

    pg.click('[data-view="site"]')
    pg.wait_for_timeout(900)

    cfg = pg.locator("[data-scfg]").first
    cfg.wait_for(state="visible", timeout=8000)
    cfg.click()
    pg.wait_for_timeout(1200)

    # 预问诊那两个下拉：按 data-f 定位，顺带把当前选中的文本读出来。
    out = {}
    for f in ("pv_role_translate", "pv_role_generate"):
        el = pg.locator(f'[data-f="{f}"]')
        n = el.count()
        if not n:
            out[f] = "MISSING"
            continue
        out[f] = el.first.evaluate(
            "e => ({value: e.value, label: e.selectedOptions[0]?.textContent || '', "
            "options: e.options.length})"
        )

    # 顺便确认这一行的标题还在（别把行渲染丢了）
    row = pg.locator("tr", has=pg.locator("b", has_text="预问诊"))
    out["row_visible"] = row.count() > 0

    pg.screenshot(path=OUT, full_page=False)
    print("截图:", OUT)
    for k, v in out.items():
        print(f"  {k}: {v}")
    print("console errors:", errs if errs else "无")

    # 真按一次「保存并生效」：这一步验证的是"把 pv_ 两列从站点 payload 里摘出来、
    # 再单独走预问诊配置接口"那段拆分逻辑。只点开看一眼是验不到的。
    if len(sys.argv) > 2 and sys.argv[2] == "save":
        pg.get_by_role("button", name="保存并生效").click()
        pg.wait_for_timeout(2500)
        tb = pg.locator("#toastBox")
        print("保存提示:", tb.inner_text() if tb.count() else "（没有 toast）")
        print("隐藏的 pv 字段是否泄漏进站点 payload：由后端忽略，这里只看有无报错")
        print("console errors after save:", errs if errs else "无")
    b.close()
