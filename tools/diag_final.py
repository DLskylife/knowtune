"""最终验收：中文提问->切维语，长等待直到工具条恢复+预览补齐。"""
import time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8800"; Q="膝盖疼，蹲下去站不起来，走路的时候关节里响"
with sync_playwright() as p:
    b=p.chromium.launch(channel="msedge",headless=True,args=["--disable-gpu"])
    pg=b.new_page(viewport={"width":1440,"height":900})
    errs=[];pg.on("pageerror",lambda e:errs.append(str(e)))
    pg.goto(BASE,wait_until="networkidle")
    pg.click('#langSwitch button[data-lang="zh"]');pg.wait_for_timeout(400)
    t0=time.time()
    pg.fill("#q",Q);pg.click("#btnSend")
    for _ in range(900):
        if pg.evaluate("()=>!!document.querySelector('#msgs .msg.bot .foot')"):break
        pg.wait_for_timeout(200)
    print("答案完成 %.1fs"%(time.time()-t0))
    pg.wait_for_timeout(1500)
    pg.click('#langSwitch button[data-lang="ug"]')
    print("切维语 %.1fs"%(time.time()-t0))
    # 等工具条恢复（最多180s）
    for i in range(180):
        pg.wait_for_timeout(1000)
        chip=pg.evaluate("()=>(document.querySelector('#toolChip')||{}).textContent||''")
        if 'ئالماشتۇرۇلۇۋاتىدۇ' not in chip:
            print("工具条恢复 %.1fs"%(time.time()-t0));break
    else: print("工具条180s未恢复")
    # 展开来源，等预览补齐（最多120s）
    pg.evaluate("()=>document.querySelectorAll('#msgs details.srcs').forEach(d=>d.open=true)")
    for i in range(24):
        pg.wait_for_timeout(5000)
        left=pg.evaluate("""()=>{let n=0;document.querySelectorAll('#msgs .src-prev').forEach(e=>{
          if(/[\\u3400-\\u9fff]/.test(e.textContent))n++;});return n;}""")
        if left==0:
            print("预览补齐 %.1fs"%(time.time()-t0));break
    else: print("预览仍剩%d条中文"%left)
    # 全屏中文扫描
    zh=pg.evaluate("""()=>{let out=[];const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;
      while((n=w.nextNode())){const t=(n.nodeValue||'').trim();if(!t||!/[\\u3400-\\u9fff]/.test(t))continue;
      const e=n.parentElement;if(!e||['SCRIPT','STYLE'].includes(e.tagName))continue;
      const cs=getComputedStyle(e);if(cs.display==='none'||cs.visibility==='hidden')continue;
      if(e.tagName==='BUTTON'&&t.trim()==='中文')continue;
      out.push(e.className+':'+t.slice(0,30));}return out;}""")
    print("残留中文节点 %d:"%len(zh))
    for z in zh[:10]:print("  ",z)
    print("pageerror:",errs or "无")
    pg.screenshot(path="_shot/final_verify.png",full_page=True)
    b.close()
