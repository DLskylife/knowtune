"""打分台深色皮肤的多视图截图 + DOM 探针。

Gradio 进程由脚本自己起、自己收——用后台任务起服务时，工具调用一结束进程就被
回收，下一次截图必然 ERR_CONNECTION_REFUSED。

    evals/.venv/Scripts/python.exe evals/shot_dark.py
    evals/.venv/Scripts/python.exe evals/shot_dark.py --probe   # 只打印控件样式
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "_shot"
OUT.mkdir(exist_ok=True)
PORT = 7861
URL = f"http://127.0.0.1:{PORT}/"


def wait_port(timeout: float = 60.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return
        time.sleep(0.4)
    raise RuntimeError("打分台没起来")


WHITE_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('.gradio-container *')) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 6 || rect.height < 6) continue;
    const cs = getComputedStyle(el);
    const m = (cs.backgroundColor || '').match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (!m) continue;
    const r = +m[1], g = +m[2], b = +m[3];
    if (r >= 180 && g >= 180 && b >= 180) {
      out.push(el.tagName.toLowerCase() + '.' + String(el.className).slice(0, 40) +
               ' ' + cs.backgroundColor + ' ' + (el.textContent || '').trim().slice(0, 12));
    }
  }
  return out.slice(0, 12);
}"""


def whites(page) -> None:
    """深色页面上任何一块近白都是漏改：状态徽标、toast、代码块都栽过。"""
    rows = page.evaluate(WHITE_JS)
    print("== 近白块 ==")
    print("none" if not rows else "\n".join(rows))


def probe(page) -> None:
    print("== range ==")
    print(page.evaluate("""() => {
      const r = document.querySelector('.gradio-container input[type=range]');
      if (!r) return 'no range';
      const cs = getComputedStyle(r);
      return r.className + ' | bg=' + cs.backgroundColor + ' | accent=' + cs.accentColor;
    }"""))
    print("== 滑块包裹层 ==")
    print(page.evaluate("""() => {
      const r = document.querySelector('.gradio-container input[type=range]');
      let el = r, out = [];
      for (let i = 0; i < 4 && el; i++) { out.push(el.tagName.toLowerCase() + '.' + (el.className||'')); el = el.parentElement; }
      return out.join(' <- ');
    }"""))
    print("== 滑块标签 ==")
    print(page.evaluate("""() => {
      const l = [...document.querySelectorAll('.gradio-container span')].find(e => (e.textContent||'').includes('准确'));
      return l ? l.className + ' | color=' + getComputedStyle(l).color : 'none';
    }"""))
    print("== radio ==")
    print(page.evaluate("""() => {
      const r = document.querySelector('.gradio-container input[type=radio]');
      if (!r) return 'no radio';
      const lab = r.closest('label') || r.parentElement;
      return r.outerHTML.slice(0, 120) + '\\nWRAP ' + lab.className + ' bg=' + getComputedStyle(lab).backgroundColor;
    }"""))
    print("== accordion 内容区 ==")
    print(page.evaluate("""() => {
      const a = document.querySelector('.gradio-container .block.gr-accordion');
      return [...a.children].map(c => c.tagName.toLowerCase() + '.' + (c.className||'')).join(' | ');
    }"""))
    print("== 变量 ==")
    print(page.evaluate("""() => {
      const c = document.querySelector('.gradio-container');
      const s = getComputedStyle(c);
      return ['--layout-max-width', '--block-background-fill', '--input-background-fill',
              '--body-background-fill', '--block-border-color', '--slider-color']
        .map(k => k + '=' + s.getPropertyValue(k)).join(' | ');
    }"""))


def shots(page) -> None:
    whites(page)
    page.screenshot(path=str(OUT / "dark-1.png"))
    page.get_by_text("② 两个端点", exact=False).filter(visible=True).first.scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "dark-2.png"))
    page.get_by_text("候选 ①", exact=False).filter(visible=True).first.scroll_into_view_if_needed()
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "dark-3.png"))
    page.get_by_role("tab", name="单模型多答 + DPO").click()
    page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "dark-4-sampling.png"))
    whites(page)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="只打印控件样式，不截图")
    ap.add_argument("--port", type=int, default=7861)
    args = ap.parse_args()

    global PORT, URL
    PORT = args.port
    URL = f"http://127.0.0.1:{PORT}/"

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "evals" / "scoreboard.py"), "--port", str(PORT), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(URL, wait_until="load")
            page.wait_for_timeout(2500)
            if args.probe:
                probe(page)
            else:
                shots(page)
                print("截图：_shot/dark-1..4")
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
