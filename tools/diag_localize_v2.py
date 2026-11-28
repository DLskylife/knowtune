"""新架构验收：生成时译文落盘 -> 切语言纯渲染 -> 往返稳定 -> 老会话兜底。

旧 diag_final.py 针对已删除的 "切语言现翻 + busy 状态机" 写的，等的是
"工具条切换中"，对现在的架构没有意义。这一版验的是用户真正要的那件事：
答案落盘时译文已经在 localStorage 里；之后切语言一毫秒都不等、不发请求。
"""
import json
import time

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8800"
Q = "膝盖疼，蹲下去站不起来，走路的时候关节里响"

SCAN_ZH = """() => {
  const out = [];
  document.querySelectorAll('#msgs .msg').forEach(msg => {
    const w = document.createTreeWalker(msg, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = (n.nodeValue || '').trim();
      if (!t || !/[\\u3400-\\u9fff]/.test(t)) continue;
      const e = n.parentElement;
      if (!e || ['SCRIPT','STYLE'].includes(e.tagName)) continue;
      const cs = getComputedStyle(e);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      out.push(e.className + ':' + t.slice(0, 40));
    }
  });
  return out;
}"""

LAST_REC = """() => {
  const cs = JSON.parse(localStorage.getItem('knowtune.site.chats') || '[]');
  if (!cs.length || !cs[0].msgs.length) return null;
  const m = cs[0].msgs[cs[0].msgs.length - 1];
  return {
    ug: !!m.ug, dug: !!m.dug, qu: !!m.qu, tu: !!cs[0].tu,
    cit: (m.cit || []).map(c => ({
      d: !!c._u_doc_title, h: !!c._u_heading_path, p: !!c._u_preview
    })),
  };
}"""


def waits_localized(pg, limit=120):
    for _ in range(limit):
        r = pg.evaluate(LAST_REC)
        if r and (r["ug"] or r["dug"]):
            return r
        pg.wait_for_timeout(1000)
    return None


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True,
                              args=["--disable-gpu"])
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        requests = []
        pg.on("request", lambda r: requests.append(r.url))

        pg.goto(BASE, wait_until="networkidle")
        pg.evaluate("() => localStorage.clear()")
        pg.reload(wait_until="networkidle")
        pg.click('#langSwitch button[data-lang="zh"]')
        pg.wait_for_timeout(300)

        t0 = time.time()
        pg.fill("#q", Q)
        pg.click("#btnSend")
        for _ in range(900):
            state = pg.evaluate("""() => ({
              err: !!document.querySelector('#msgs .answer.err'),
              saved: (() => {
                const cs = JSON.parse(localStorage.getItem('knowtune.site.chats') || '[]');
                return !!(cs[0] && cs[0].msgs && cs[0].msgs.length);
              })(),
            })""")
            if state["err"]:
                print("!! 请求报错:", pg.evaluate(
                    "()=>document.querySelector('#msgs .answer.err')?.textContent"))
                break
            if state["saved"]:
                break
            pg.wait_for_timeout(200)
        done_at = time.time() - t0
        rec0 = pg.evaluate(LAST_REC)
        print(f"[1] 答案落盘 {done_at:.1f}s，落盘瞬间译文={json.dumps(rec0, ensure_ascii=False)}")

        rec = waits_localized(pg)
        print(f"[2] 译文落盘 {time.time()-t0:.1f}s，{json.dumps(rec, ensure_ascii=False)}")
        if not rec:
            print("!! localized 从未落盘，后面不用测了")
            print("pageerror:", errs or "无")
            b.close()
            return

        # 切维语：计数请求，确认主路径不发网络请求
        before = len(requests)
        pg.click('#langSwitch button[data-lang="ug"]')
        pg.wait_for_timeout(120)
        zh = pg.evaluate(SCAN_ZH)
        print(f"[3] 切维语后残留中文 {len(zh)} 处，新增请求 {len(requests)-before} 个")
        for z in zh[:8]:
            print("    ", z)

        # 展开来源，确认预览三字段都有译文
        pg.evaluate("()=>document.querySelectorAll('#msgs details.srcs').forEach(d=>d.open=true)")
        pg.wait_for_timeout(300)
        zh2 = pg.evaluate(SCAN_ZH)
        print(f"[4] 展开来源后残留中文 {len(zh2)} 处")
        for z in zh2[:8]:
            print("    ", z)

        # 往返：中文 -> 维语，维语侧必须在极短时间内恢复
        pg.click('#langSwitch button[data-lang="zh"]')
        pg.wait_for_timeout(120)
        pg.click('#langSwitch button[data-lang="ug"]')
        pg.wait_for_timeout(120)
        zh3 = pg.evaluate(SCAN_ZH)
        print(f"[5] 往返切换后残留中文 {len(zh3)} 处")

        # 老会话：注入一份升级前生成的记录（无任何译文），验证按会话兜底
        pg.evaluate("""() => {
          const cs = JSON.parse(localStorage.getItem('knowtune.site.chats') || '[]');
          const old = {
            id: 'legacy-probe', title: ' legacy-title ', at: Date.now(), c: 'med', msgs: [{
              q: ' legacy question ', a: ' legacy answer ', d: '', cit: []
            }]
          };
          localStorage.setItem('knowtune.site.chats', JSON.stringify([old].concat(cs)));
          localStorage.setItem('knowtune.site.sessions', JSON.stringify({med: 'legacy-probe'}));
        }""")
        pg.reload(wait_until="networkidle")
        pg.click('#langSwitch button[data-lang="ug"]')
        for _ in range(60):
            got = pg.evaluate("""() => {
              const cs = JSON.parse(localStorage.getItem('knowtune.site.chats')||'[]');
              const c = cs.find(x => x.id === 'legacy-probe');
              return !!(c && c.tu && c.msgs[0].ug && c.msgs[0].qu);
            }""")
            if got:
                break
            pg.wait_for_timeout(1000)
        print(f"[6] 老会话兜底 {'已补齐' if got else '未补齐'}")
        print("pageerror:", errs or "无")
        pg.screenshot(path="_shot/localize_v2.png", full_page=True)
        b.close()


if __name__ == "__main__":
    main()
