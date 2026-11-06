"""题库三来源的交互回归：档案自带 → 导入文件 → 自己输入。

    evals/.venv/Scripts/python.exe evals/shot_casebook.py

服务由脚本自己起、自己收（用后台任务起的话，工具调用一结束进程就被回收，
下一次截图必然 ERR_CONNECTION_REFUSED）。不真的调模型——只看「开始」之后
载入了几条题，因为这一步错了，后面打的分全是另一批题的。
"""

from __future__ import annotations

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
CSV = OUT / "_cases_demo.csv"
CUSTOM = Path(__file__).resolve().parent / "cases_custom.jsonl"


def boot() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "evals" / "scoreboard.py"),
         "--port", str(PORT), "--no-browser"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return proc
        time.sleep(0.4)
    proc.terminate()
    raise RuntimeError("打分台没起来")


def visible(page, label: str):
    return page.get_by_label(label, exact=False).filter(visible=True).first


def click(page, name: str) -> None:
    """点按钮。Gradio 在来源切换时会重排整块，等一下再点；
    实在被过渡层挡住就直接调 DOM 的 click()，别让一次动画卡死整个回归。"""
    btn = page.get_by_role("button", name=name).filter(visible=True).first
    btn.wait_for(state="visible", timeout=15000)
    try:
        btn.click(timeout=8000)
    except Exception:
        btn.evaluate("el => el.click()")
    page.wait_for_timeout(300)


def _diag(page) -> None:
    radio = page.evaluate("""() => [...document.querySelectorAll('input[type=radio]')]
        .filter(r => r.offsetParent).map(r => r.value + ':' + r.checked).join(', ')""")
    btns = page.evaluate("""() => [...document.querySelectorAll('.gradio-container button')]
        .filter(b => b.offsetParent).map(b => b.innerText.trim()).filter(Boolean).slice(0, 24)""")
    print("  [diag] radio:", radio)
    print("  [diag] 可见按钮:", btns)
    page.screenshot(path=str(OUT / "_diag-typed.png"))


def pick_radio(page, name: str) -> None:
    """点来源单选，并确认真的选中了——点空等于在评上一份题库，界面看不出来。"""
    # Gradio 的 input 被 CSS 藏起来了，点的是外面那层 label
    page.locator("label").filter(has_text=name).filter(visible=True).first.click()
    page.wait_for_timeout(900)
    # 两个页签各有一组同名单选，只认"有一个被选中"，不要拿第一个 input 判断
    checked = page.evaluate(
        """(name) => [...document.querySelectorAll(`input[type=radio][value="${name}"]`)]
                     .some(r => r.checked)""", name)
    if not checked:
        page.locator("label").filter(has_text=name).filter(visible=True).first.click()
        page.wait_for_timeout(700)
    print(f"  [radio] {name} 选中: {checked}")


def head(page) -> str:
    return page.locator("#samphead").inner_text().replace("\n", " ")[:100]


def main() -> int:
    # 「自己输入」会把题库落到 cases_custom.jsonl。回归脚本不该顺手改掉
    # 用户攒的那份，跑完原样放回去。
    backup = CUSTOM.read_bytes() if CUSTOM.exists() else None
    proc = boot()
    try:
        return _run()
    finally:
        if backup is None:
            CUSTOM.unlink(missing_ok=True)
        else:
            CUSTOM.write_bytes(backup)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run() -> int:
    CSV.write_text(
        "题目,标签,必须,备注\n"
        "用一句话解释什么是复利,事实,利滚利,超过 30 字算不合格\n"
        "17 的 23% 是多少？保留两位小数,计算,3.91,\n"
        "我在改一个 Flask 项目 || 那它怎么做限流？,多轮,限流,\n",
        encoding="utf-8-sig")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1500})
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(2000)
        page.get_by_role("tab", name="单模型多答 + DPO").click()
        page.wait_for_timeout(1000)
        # 上一次的会话会自动恢复，先显式载入一次，别让残留状态冒充结果
        click(page, "开始 / 新建一轮")
        page.wait_for_timeout(2000)
        got = head(page)
        assert "/ 20 题" in got, f"档案自带应载入 20 题：{got}"
        print("① 档案自带：", got)
        page.screenshot(path=str(OUT / "case-1-archive.png"), full_page=True)

        click(page, "导入文件 · jsonl / json / csv / tsv / txt")
        page.wait_for_timeout(900)  # 展开动画还没落定就点单选，click 会落空
        pick_radio(page, "导入文件")
        page.screenshot(path=str(OUT / "case-2-upload.png"), full_page=True)

        page.get_by_role("tabpanel").first.locator("input[type=file]").first.set_input_files(str(CSV))
        page.wait_for_timeout(1500)
        click(page, "开始 / 新建一轮")
        page.wait_for_timeout(2500)
        got = head(page)
        print("② 导入 CSV：", got)
        print("  [toast]", page.evaluate(
            """() => [...document.querySelectorAll('[class*=toast]')]
                     .map(e => e.innerText.replace(/\\n/g, ' '))
                     .filter(t => t.length > 4).slice(-2)"""))
        assert "/ 3 题" in got, f"CSV 应载入 3 题：{got}"
        print("  [toast]", page.evaluate(
            """() => [...document.querySelectorAll('[class*=toast]')]
                     .map(e => e.innerText.replace(/\\n/g, ' '))
                     .filter(t => t.length > 4).slice(-2)"""))
        page.screenshot(path=str(OUT / "case-3-imported.png"), full_page=True)

        click(page, "自己输入 · 一行一题")
        page.wait_for_timeout(900)
        pick_radio(page, "自己输入")
        _diag(page)
        click(page, "填入示例")
        # 「填入示例」是服务端回填，响应晚到会把刚敲的字盖回去，等它落定
        page.wait_for_timeout(1800)
        # 示例 3 条 + 脚本追加 1 条：条数必须和 CSV 的 3 条区分开，
        # 否则"换了来源"和"state 没变"在截图上一模一样。
        box = visible(page, "题库（一行一题）")
        box.fill(box.input_value().rstrip() + "\n脚本临时加的一题 @必须:脚本")
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "case-4-typed.png"), full_page=True)
        click(page, "开始 / 新建一轮")
        page.wait_for_timeout(2500)
        got = head(page)
        assert "/ 4 题" in got, f"手输应载入 4 题：{got}"
        print("③ 自己输入：", got)
        page.screenshot(path=str(OUT / "case-5-started.png"), full_page=True)

        # 切回档案自带，把 jsonl 抄进输入框，确认往返不丢题
        page.get_by_text("档案自带", exact=True).filter(visible=True).first.click()
        page.wait_for_timeout(500)
        page.get_by_text("自己输入", exact=True).filter(visible=True).first.click()
        page.wait_for_timeout(500)
        click(page, "← 抄档案题库")
        page.wait_for_timeout(1200)
        print("④ 抄进来的行数：",
              len(visible(page, "题库（一行一题）").input_value().splitlines()))
        page.screenshot(path=str(OUT / "case-6-copy.png"), full_page=True)

        # A/B 页同一个来源组件，确认没被 TabbedInterface 弄丢
        page.get_by_role("tab", name="A/B 双模型盲测").click()
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "case-7-arena.png"), full_page=True)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
