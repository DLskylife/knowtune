"""看站点页 01 段「翻译/推理模型」下拉里本地那组到底列了什么。

不猜 UI，直接从 DOM 读：每个 select 的 optgroup 名、选项 value/text、
当前选中值，再和 /api/models 的 local 清单对照。
"""
import json
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8013/"


def api(path):
    return json.load(urllib.request.urlopen("http://127.0.0.1:8013" + path, timeout=30))


def main():
    inst = api("/api/instances")
    spec = [i for i in inst["items"] if i["name"] == "uyghur-med"][0]
    print("instances.yaml roles:", json.dumps(spec.get("roles"), ensure_ascii=False))
    print("sections roles:", json.dumps(
        {k: (v or {}).get("roles") for k, v in (spec.get("sections") or {}).items()},
        ensure_ascii=False))

    models = api("/api/models")
    local = [p for p in models["providers"] if p.get("kind") == "local"]
    for p in local:
        print("\n本地 provider:", p["name"], "kind=", p.get("kind"),
              "base_url=", p.get("base_url"), "models=", len(p.get("models") or []))
        for m in (p.get("models") or []):
            print("   id=%-38s port=%-5s state=%-8s running_url=%s"
                  % (m.get("id"), m.get("port"), m.get("state"), m.get("running_url")))

    with sync_playwright() as pw:
        br = pw.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1500, "height": 1000})
        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(1200)
        # 进「前台问答站点」页：左侧 nav-item[data-view=site]
        pg.click('.nav-item[data-view="site"]')
        pg.wait_for_timeout(2000)
        print("\n当前 view:", pg.evaluate("() => document.body.dataset.view || ''"))
        print("有站点卡:", pg.evaluate("() => document.querySelectorAll('.site-block').length"))

        rows = pg.evaluate("""() => {
          const out = [];
          document.querySelectorAll('.site-block select[data-f]').forEach((s) => {
            const groups = [...s.querySelectorAll('optgroup')].map((g) => ({
              g: g.label,
              opts: [...g.querySelectorAll('option')].map((o) => ({
                v: o.value, t: o.textContent, sel: o.selected,
              })),
            }));
            out.push({ f: s.dataset.f, value: s.value, groups });
          });
          return out;
        }""")
        for r in rows:
            print("\n=== select data-f=%s  value=%r" % (r["f"], r["value"]))
            for g in r["groups"]:
                print("  [%s] %d 项" % (g["g"], len(g["opts"])))
                for o in g["opts"]:
                    mark = " ←当前" if o["sel"] else ""
                    print("      %-42s %s%s" % (o["v"], o["t"], mark))

        pg.screenshot(path="_shot/sites_model.png", full_page=True)
        br.close()


if __name__ == "__main__":
    main()
