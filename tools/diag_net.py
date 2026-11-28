"""监听切语言时所有 /api/answer/localize 请求的时序。"""
import time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800"
Q = "膝盖疼，蹲下去站不起来，走路的时候关节里响"

REQS = []
def on_request(req):
    if "/api/answer/localize" in req.url:
        body = req.post_data or ""
        try:
            import json
            n = len(json.loads(body).get("texts", []))
        except Exception:
            n = -1
        REQS.append({"t": time.time(), "ev": "req", "n": n})
def on_resp(resp):
    if "/api/answer/localize" in resp.url:
        REQS.append({"t": time.time(), "ev": "resp", "status": resp.status})

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True, args=["--disable-gpu"])
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("request", on_request)
    pg.on("response", on_resp)
    pg.goto(BASE, wait_until="networkidle")
    pg.click('#langSwitch button[data-lang="zh"]')
    pg.wait_for_timeout(400)
    t0 = time.time()
    pg.fill("#q", Q)
    pg.click("#btnSend")
    for _ in range(600):
        if pg.evaluate("() => !!document.querySelector('#msgs .msg.bot .foot')"):
            break
        pg.wait_for_timeout(200)
    print(f"答案完成 @ {time.time()-t0:.1f}s")
    pg.wait_for_timeout(1500)
    print(f"--- 切维语 @ {time.time()-t0:.1f}s ---")
    pg.click('#langSwitch button[data-lang="ug"]')
    for _ in range(180):  # 最多等 180s
        pg.wait_for_timeout(1000)
        chip = pg.evaluate("() => (document.querySelector('#toolChip')||{}).textContent||''")
        if "ئالماشتۇرۇلۇۋاتىدۇ" not in chip:
            print(f"切换完成 @ {time.time()-t0:.1f}s chip={chip}")
            break
    else:
        print(f"180s 后仍未完成 chip={chip}")
    print("\n=== 翻译请求时序 ===")
    base = REQS[0]["t"] if REQS else t0
    for r in REQS:
        print(f"  +{r['t']-base:6.1f}s {r['ev']:4} n={r.get('n','')} status={r.get('status','')}")
    b.close()
