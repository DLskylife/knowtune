"""看外面正在跑的那个链接：只访问、截图、报配色，不动进程。"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "_shot"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860/"
NAME = sys.argv[2] if len(sys.argv) > 2 else "now"

JS = """() => {
  const c = document.querySelector('.gradio-container');
  if (!c) return 'NO .gradio-container';
  const s = getComputedStyle(c);
  const acc = [...document.querySelectorAll('.block.gr-accordion > .label-wrap')]
    .map(e => (e.textContent || '').trim().slice(0, 14)).slice(0, 5);
  return '--body-background-fill=' + s.getPropertyValue('--body-background-fill') +
         ' | bodyBg=' + getComputedStyle(document.body).backgroundColor +
         ' | accordions=' + JSON.stringify(acc);
}"""

with sync_playwright() as pw:
    b = pw.chromium.launch(channel="msedge", headless=True)
    p = b.new_page(viewport={"width": 1440, "height": 900})
    p.goto(URL, wait_until="load")
    p.wait_for_timeout(3000)
    print(p.evaluate(JS))
    p.screenshot(path=str(OUT / f"{NAME}-top.png"))
    p.get_by_text("题目来源", exact=False).filter(visible=True).first.scroll_into_view_if_needed()
    p.wait_for_timeout(600)
    p.screenshot(path=str(OUT / f"{NAME}-source.png"))
    b.close()
print(f"截图：_shot/{NAME}-top.png / {NAME}-source.png")
