"""预问诊语言切换回归：开场白固定 + 切语言走翻译，不重开、不重答。

跑法：.venv/Scripts/python.exe scripts/check_pv_hello.py

守的四件事：
1. 进入预问诊就有固定问候，且**不发任何请求**（不读模型、不翻译）；
2. 切换语言只重画，问候原地跟着换；
3. 中文问出来的回答，切到维语时被**翻译**成维语（同一句话，不是重新回答），
   语言标记随之消失；
4. 再切一轮不再请求——译文写进库了。
"""
from __future__ import annotations

import re
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800/"
UG = re.compile(r"[\u0600-\u06ff]")

errors: list[str] = []
reqs: list[str] = []


def enter_pv(pg) -> None:
    pg.wait_for_selector("#nav button", timeout=90000)
    for b in pg.query_selector_all("#nav button"):
        if "预问诊" in (b.inner_text() or ""):
            b.click()
            break
    pg.wait_for_function("() => document.body.dataset.view === 'pv'", timeout=30000)
    pg.wait_for_timeout(300)


def switch(pg, ug: bool) -> None:
    for b in pg.query_selector_all("#langSwitch button"):
        t = b.inner_text() or ""
        if (UG.search(t) if ug else t.strip() == "中文"):
            b.click()
            return


def answers(pg) -> list[str]:
    return pg.eval_on_selector_all(
        "#pvMsgs .msg.bot .answer", "els => els.map(e => e.innerText)")


def tags(pg) -> list[str]:
    return pg.eval_on_selector_all(
        "#pvMsgs .msg.bot",
        "els => els.map(e => e.querySelector('.alang') ? e.querySelector('.alang').innerText : '')")


def main() -> int:
    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge")
        pg = br.new_page(viewport={"width": 1200, "height": 700})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("request", lambda r: reqs.append(r.url.split("/api/previsit/")[-1])
              if "/api/previsit/" in r.url else None)

        pg.goto(BASE, wait_until="networkidle")
        enter_pv(pg)
        first = answers(pg)
        print("问候:", first)
        assert first and first[0].startswith("您好"), "进入预问诊没有固定问候"
        assert not reqs, f"进入预问诊就发请求了：{reqs}"

        switch(pg, True)
        pg.wait_for_timeout(300)
        ug_greet = answers(pg)[0]
        print("维语问候:", ug_greet)
        assert UG.search(ug_greet), "切维语后问候没跟着换"

        switch(pg, False)
        pg.wait_for_timeout(200)
        pg.fill("#q", "肚子疼，两天了")
        pg.click("#btnSend")
        pg.wait_for_function(
            "() => document.querySelectorAll('#pvMsgs .msg').length >= 3", timeout=240000)
        zh = answers(pg)
        print("中文回答:", zh)
        assert len(zh) == 2
        assert pg.eval_on_selector("#q", "e => e.value") == "", "首句发出后输入框没清空"

        switch(pg, True)
        pg.wait_for_function(
            """() => [...document.querySelectorAll('#pvMsgs .msg.bot')].slice(1).every(
                 e => /[\\u0600-\\u06ff]/.test(e.querySelector('.answer').innerText)
                      && !e.querySelector('.alang'))""",
            timeout=180000)
        ug = answers(pg)
        print("维语回答:", ug)
        assert not any(tags(pg)), f"还有语言标记没消：{tags(pg)}"

        switch(pg, False)
        pg.wait_for_timeout(400)
        assert answers(pg)[1] == zh[1], "切回中文不是同一句话"
        n = len(reqs)
        switch(pg, True)
        pg.wait_for_timeout(1200)
        assert len(reqs) == n, f"第二次切维语又翻译了一遍：{reqs[n:]}"
        assert answers(pg)[1] == ug[1], "第二次切维语内容不一致"

        print("请求序列:", reqs)
        print("pageerror:", errors or "none")
        br.close()
    assert not errors, errors
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
