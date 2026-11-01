"""前台交互冒烟测试（开发期自查，不参与线上运行）。

为什么不写单元测试：前台的问题几乎都出在"DOM 与双向文本"这一层——引用编号
在 RTL 里被重排、点引用没展开来源、切语言后正文不跟着换。这些只有真浏览器
能验证，jsdom 的排版和 bidi 是假的。

做法：把三个静态文件内联成一份 HTML，/api/info 与 /api/ask 换成 mock，
追加一段断言脚本，用 Edge headless 跑完把结果写进 document.title，
再 --dump-dom 抓回来。

用法:
    .venv/Scripts/python.exe scripts/uismoke.py
    .venv/Scripts/python.exe scripts/uismoke.py --zh
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shot as S  # noqa: E402  同目录的开发期工具，复用它的 mock

PROBE = r"""
<script>
(function () {
  // 受控闸门：/api/ask 的流推完第一块就停在这里，直到测试放行。
  // 这样"生成中"的界面状态可以被稳定断言，而不是赌时间差。
  window.__newGate = function () {
    window.__askGate = new Promise(function (r) { window.__releaseAsk = r; });
  };
  window.__newGate();

  var log = [];
  var fails = 0;
  function flush() {
    document.title = "RESULT" + JSON.stringify({fails: fails, log: log});
  }
  function ok(name, cond, extra) {
    if (!cond) fails++;
    log.push((cond ? "PASS " : "FAIL ") + name + (extra ? " [" + extra + "]" : ""));
    flush();
  }
  addEventListener("error", function (e) { log.push("JSERR " + e.message); fails++; });

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // 骨架位置快照。这里只量"结构锚点"的 left/right，不量宽度和高度：
  // 维语和中文的示例文案长短不同，胶囊按内容收缩是正常的；而侧栏、按钮、
  // 输入框的位置不该因为换了个语言就跑到另一边去。
  function rectOf(sel, edge) {
    var el = document.querySelector(sel);
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return Math.round(edge === "right" ? r.right : r.left);
  }
  function snap() {
    return {
      "侧栏": rectOf(".side"),
      "语言开关": rectOf("#langSwitch"),
      "发送键": rectOf("#btnSend"),
      "输入框": rectOf(".box"),
      "标题": rectOf(".top h1"),
      "示例区": rectOf(".samples"),
      "AI气泡": rectOf(".msg.bot"),
      "用户气泡": rectOf(".msg.user", "right"),
    };
  }
  function diffRects(a, b) {
    var bad = [];
    Object.keys(a).forEach(function (k) {
      if (a[k] === null || b[k] === null) {
        if (a[k] !== b[k]) bad.push(k + " 缺失");
      } else if (Math.abs(a[k] - b[k]) > 1) {
        bad.push(k + " " + a[k] + "→" + b[k]);
      }
    });
    return bad;
  }
  async function until(fn, ms) {
    var t0 = Date.now();
    while (Date.now() - t0 < (ms || 5000)) {
      try { if (fn()) return true; } catch (e) {}
      await wait(60);
    }
    return false;
  }

  addEventListener("load", async function () {
   try {
    // 空态：欢迎区在、占位类在、没有横向溢出
    await until(function () { return document.querySelector(".chip"); });
    ok("空态-示例卡", document.querySelectorAll(".chip").length === 3);
    ok("空态-垂直居中", document.getElementById("chat").classList.contains("empty"));
    ok("无横向溢出", document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
       document.documentElement.scrollWidth + ">" + document.documentElement.clientWidth);

    // 提问：点第一张示例卡（流被闸门卡住，停在"生成中"）
    document.querySelector(".chip").click();
    await wait(80);
    ok("生成中-停止按钮可点", !document.getElementById("btnSend").disabled);
    ok("生成中-按钮改文案", document.getElementById("btnSend").classList.contains("stopping"),
       document.getElementById("sendLabel").textContent);
    ok("生成中-流式光标", !!document.querySelector(".answer.streaming"));
    ok("生成中-状态行", !document.getElementById("status").hidden);

    window.__releaseAsk();

    var got = await until(function () {
      return document.querySelector(".msg.bot .answer .md");
    });
    ok("答案已渲染", got);
    await wait(400);

    // 富文本：列表被解析成真正的 ol（不是把 "1." 当正文）
    ok("列表解析", document.querySelectorAll(".md ol > li").length === 4,
       String(document.querySelectorAll(".md ol > li").length));
    // 引用编号做成可点按钮
    var cites = document.querySelectorAll(".cite");
    ok("引用可点", cites.length > 0, cites.length + " 个");

    // 1.7.1：引用牌必须和它注释的那个字绑成一体。中文行内处处可断，牌是
    // 原子 inline 盒，行尾差几像素就会被单独推到下一行行首——用户看到的是
    // 一行以"①。"开头，会当成新的序号。这里量两件事：结构上被 .nb 包住，
    // 且把正文列压到 520px 制造行尾吃紧的情形后，牌仍与前一个字同一行。
    var boundCnt = 0;
    cites.forEach(function (c) {
      var p = c.parentNode;
      if (p && p.classList && p.classList.contains("nb") && p.textContent.length > 1) boundCnt++;
    });
    ok("引用-绑定前字", boundCnt === cites.length, boundCnt + "/" + cites.length);

    var msgsEl = document.getElementById("msgs");
    var oldMax = msgsEl.style.maxWidth;
    msgsEl.style.maxWidth = "520px";
    await wait(200);
    var orphans = [];
    document.querySelectorAll(".msg.bot .answer .cite").forEach(function (c) {
      var prev = c.previousSibling;
      if (!prev || prev.nodeType !== 3 || !prev.textContent.length) return;
      var rg = document.createRange();
      var n = prev.textContent.length;
      rg.setStart(prev, n - 1);
      rg.setEnd(prev, n);
      var cr = c.getBoundingClientRect();
      // 引用可能落在列表项里（mock 答案就是四条 ol），不能只找 p
      var host = c.closest("p, li") || c.parentNode;
      var pl = host.getBoundingClientRect().left;
      if (Math.abs(rg.getBoundingClientRect().top - cr.top) >= 6 || (cr.left - pl) < 8) {
        orphans.push(c.textContent.trim());
      }
    });
    ok("引用-不掉行首", orphans.length === 0, orphans.join(","));
    msgsEl.style.maxWidth = oldMax;
    await wait(100);

    // 证据档位 + 来源胶囊 + 复制按钮
    ok("证据徽标", !!document.querySelector(".ev-badge.high"));
    var srcs = document.querySelector(".srcs");
    ok("来源胶囊", !!srcs && !srcs.open, srcs ? srcs.querySelector("summary").textContent.trim() : "无");
    ok("复制按钮", !!document.querySelector(".copy"));

    // 点引用 → 展开来源并高亮那一条
    if (cites.length && srcs) {
      cites[0].click();
      await wait(120);
      ok("引用跳转-展开", srcs.open);
      ok("引用跳转-高亮", !!srcs.querySelector(".src-item.flash"),
         (srcs.querySelector(".src-item.flash") || {}).dataset
           ? "" : "");
    }

    // 切语言：标题、正文、徽标三者一起换，但**骨架一像素都不许动**
    // （这是用户明确反馈过的问题：切到维语，整页元素换边，找不着按钮）
    document.getElementById("chat").scrollTop = 0;
    await wait(60);
    var before = snap();
    var sideLeftUg = document.querySelector(".side").getBoundingClientRect().left;
    var zhBtn = document.querySelector('#langSwitch [data-lang="zh"]');
    if (zhBtn) {
      zhBtn.click();
      await wait(200);
      var moved = diffRects(before, snap());
      ok("切换-骨架不位移", moved.length === 0, moved.join(" ; "));
      ok("切换-标题", /[\u4e00-\u9fa5]/.test(document.getElementById("siteTitle").textContent),
         document.getElementById("siteTitle").textContent);
      // 侧栏顶上那行知识库名以前是库里的中文名，切到维语界面还挂四个汉字——
      // 比按钮没翻更显眼，因为它是全页最大的字之一。现在可按语言配。
      var sideName = document.getElementById("sideName").textContent;
      ok("切换-侧栏名", /[\u4e00-\u9fa5]/.test(sideName) && !/[\u0600-\u06FF]/.test(sideName),
         sideName);
      // 资料/作答胶囊不能写死"维语作答"：高亮中文时它还这么说，就是撒谎。
      var chip = document.getElementById("toolChip").textContent;
      ok("切换-资料胶囊", /中文/.test(chip) && !/维语/.test(chip), chip);
      var body = document.querySelector(".msg.bot .answer").textContent;
      ok("切换-正文", /[\u4e00-\u9fa5]/.test(body) && body.length > 20);
      ok("切换-徽标", /[\u4e00-\u9fa5]/.test(document.querySelector(".ev-badge").textContent));
      // 必须重新查：renderLangSwitch 会整体重建按钮，zhBtn 已脱离文档
      ok("切换-按钮高亮",
         document.querySelector('#langSwitch [data-lang="zh"]').classList.contains("on"));
      ok("切换-占位符", /[\u4e00-\u9fa5]/.test(document.getElementById("q").placeholder));
      ok("切换-侧栏仍在左侧", document.querySelector(".side").getBoundingClientRect().left === sideLeftUg,
         sideLeftUg + "→" + document.querySelector(".side").getBoundingClientRect().left);
      // 切回维语，正文要回到主文，骨架同样不许动
      var backBefore = snap();
      document.querySelector('#langSwitch [data-lang="ug"]').click();
      await wait(200);
      ok("切回-正文", /[\u0600-\u06FF]/.test(document.querySelector(".msg.bot .answer").textContent));
      var movedBack = diffRects(backBefore, snap());
      ok("切回-骨架不位移", movedBack.length === 0, movedBack.join(" ; "));
    }

    // 回到底部按钮：把滚动条拉到顶部模拟用户往上翻
    var chat = document.getElementById("chat");
    chat.scrollTop = 0;
    // headless + virtual-time 下浏览器不推进渲染帧，原生 scroll 事件不会派发，
    // 所以这里手动派发一次（走的是同一套事件处理路径，不是绕开逻辑）。
    chat.dispatchEvent(new Event("scroll"));
    await wait(80);
    var tb = document.getElementById("toBottom");
    ok("回到底部按钮出现", !tb.hidden,
       "scrollTop=" + chat.scrollTop + " scrollHeight=" + chat.scrollHeight
       + " clientH=" + chat.clientHeight);
    tb.click();
    await wait(300);
    ok("回到底部后隐藏", tb.hidden);

    // 停止：再问一次，闸门不放行，直接点"停止"
    window.__newGate();
    document.getElementById("q").value = "قان ئازلىقى";
    document.getElementById("q").dispatchEvent(new Event("input"));
    document.getElementById("btnSend").click();
    await wait(120);
    document.getElementById("btnSend").click();
    await wait(150);
    ok("停止-按钮恢复", !document.getElementById("btnSend").classList.contains("stopping"));
    ok("停止-状态行收起", document.getElementById("status").hidden);
    ok("停止-光标移除", !document.querySelector(".answer.streaming"));

    // 新对话：消息清空、欢迎区回来
    document.getElementById("btnNew").click();
    await wait(120);
    ok("新对话-清空", document.querySelectorAll(".msg").length === 0);
    ok("新对话-欢迎区", !document.getElementById("welcome").hidden
        && document.getElementById("chat").classList.contains("empty"));

    // 侧栏：刚才那轮对话要留在列表里，点回去能恢复（刷新不丢历史）
    var sess = document.querySelectorAll(".sess");
    ok("侧栏-会话已记录", sess.length === 1, String(sess.length));
    if (sess.length) {
      sess[0].click();
      await wait(180);
      ok("侧栏-恢复历史", document.querySelectorAll(".msg").length === 2,
         String(document.querySelectorAll(".msg").length));
      ok("侧栏-恢复内容", /[\u0600-\u06FF]{10,}/.test(
         document.querySelector(".msg.bot .answer").textContent));
    }

    // 1.7.3：会话历史可删。这里要验三件事——删除必须两步（历史只在本机，
    // 没有回收站）；删掉"不是当前这条"时，正在看的内容不许被顺手清掉；
    // 删掉当前这条时，必须落回一个干净的新对话，不能停在一条不存在的会话里。
    document.getElementById("btnNew").click();
    await wait(100);
    document.getElementById("q").value = "مىزاج تۈرلىرى";
    document.getElementById("q").dispatchEvent(new Event("input"));
    document.getElementById("btnSend").click();
    await until(function () {
      return document.querySelectorAll(".msg").length >= 2
        && !!document.querySelector(".msg.bot .answer .md");
    });
    await wait(200);

    var rows = document.querySelectorAll(".sess-row");
    ok("删除-两条历史", rows.length === 2, String(rows.length));
    ok("删除-每条均有按钮", document.querySelectorAll(".sess-del").length === rows.length);

    var other = document.querySelector(".sess-row:not(.on)");
    var otherDel = other && other.querySelector(".sess-del");
    var msgsBefore = document.querySelectorAll(".msg").length;
    ok("删除-有非当前行", !!otherDel);
    if (otherDel) {
      otherDel.click();
      await wait(100);
      ok("删除-一次点只待确认", other.classList.contains("ask")
         && document.querySelectorAll(".sess-row").length === 2);
      ok("删除-确认态换文案", !otherDel.querySelector("svg")
         && otherDel.textContent.trim().length > 0);

      otherDel.click();
      await wait(150);
      ok("删除-非当前只删一条", document.querySelectorAll(".sess-row").length === 1);
      ok("删除-当前内容不动", document.querySelectorAll(".msg").length === msgsBefore,
         document.querySelectorAll(".msg").length + "/" + msgsBefore);
    }

    var curDel = document.querySelector(".sess-row.on .sess-del");
    if (curDel) {
      curDel.click();
      await wait(80);
      curDel.click();
      await wait(150);
    }
    ok("删除-删完回到空态", document.querySelectorAll(".sess-row").length === 0
       && !!document.querySelector(".side-empty"));
    ok("删除-落回新对话", !document.getElementById("welcome").hidden
       && document.querySelectorAll(".msg").length === 0);
    var saved = [];
    try { saved = JSON.parse(localStorage.getItem("knowtune.site.chats") || "[]"); } catch (_) {}
    ok("删除-本地存档同步", saved.length === 0, String(saved.length));

    flush();
   } catch (e) {
     log.push("THROW " + (e && e.message ? e.message : String(e)));
     fails++;
     flush();
   }
  });
})();
</script>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zh", action="store_true", help="以中文界面作为初始语言")
    ap.add_argument("--h", type=int, default=620, help="窗口高度，调小才能测滚动")
    args = ap.parse_args()

    html = S.build_html("ug", False, args.zh, gate="await window.__askGate;")
    html = html.replace("</body>", PROBE + "</body>")

    tmp = Path(tempfile.mkdtemp(prefix="knowtune-smoke-"))
    page = tmp / "smoke.html"
    page.write_text(html, encoding="utf-8")

    cmd = [
        str(S.EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check",
        f"--window-size=1000,{args.h}",
        "--virtual-time-budget=15000",
        "--dump-dom", page.as_uri(),
    ]
    dom = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"RESULT(\{.*?\})</title>", dom, re.S)
    if not m:
        print("没有取到结果，页面可能提前挂了。DOM 片段：", file=sys.stderr)
        print(dom[-800:], file=sys.stderr)
        return 1

    res = json.loads(m.group(1))
    for line in res["log"]:
        print(line)
    print(f"\n{'通过' if res['fails'] == 0 else '失败'}：{len(res['log'])} 项，{res['fails']} 项未过")
    return 0 if res["fails"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
