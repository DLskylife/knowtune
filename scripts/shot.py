"""前台静态页截图工具（开发期自查用，不参与线上运行）。

为什么需要它：前台是给终端用户看的，改完之后"能不能看"这件事没法靠读 CSS 判断。
这个脚本把 static/ 下的三个文件拼成一份自包含 HTML（内联 CSS/JS），再把
/api/info 与 /api/ask 换成假数据，最后交给 Edge headless 截图——不需要起服务、
不需要模型密钥、也不花钱。

用法:
    .venv/Scripts/python.exe scripts/shot.py --out _shot/empty.png
    .venv/Scripts/python.exe scripts/shot.py --filled --out _shot/filled.png
    .venv/Scripts/python.exe scripts/shot.py --filled --zh --out _shot/filled_zh.png
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "ragkb" / "frontsite" / "static"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

INFO_UG = {
    "name": "uyghur-med",
    "lang": "ug",
    "dir": "rtl",
    "kb_id": 2,
    "kb_name": "ئۇيغۇر تېبابىتى",
    "kb_desc": "",
    "title": "ئۇيغۇر تېبابىتى بىلىم سوئال-جاۋاب سىستېمىسى",
    "subtitle": "بىلىم ئامبىرىدىن ئىزدەپ، مەنبەسىنى كۆرسىتىپ جاۋاب بېرىدۇ",
    "welcome": "ئەسسالامۇ ئەلەيكۇم! ئۇيغۇر تېبابىتى بويىچە سوئالىڭىزنى يېزىڭ.",
    "samples": [
        "ئۇيغۇر تېبابىتىدىكى تۆت خىل خىلىت قايسىلار؟",
        "مىزاجنىڭ قانداق تۈرلىرى بار؟",
        "قان ئازلىقنى ئۇيغۇر تېبابىتىدە قانداق داۋالايدۇ؟",
    ],
    "disclaimer": "بۇ سىستېما بىلىم ئامبىرىدىكى ماتېرىيالغا ئاساسەن جاۋاب بېرىدۇ، داۋالاش ئۈچۈن دوختۇرغا كۆرۈنۈڭ.",
    "alt": {
        "lang": "zh",
        "dir": "ltr",
        "kb_name": "维医知识库",
        "title": "维医知识问答",
        "subtitle": "基于知识库检索，回答附来源",
        "welcome": "你好！请输入关于维医的问题。",
        "samples": ["维医中的四种体液是什么？", "气质有哪些分类？", "维医如何治疗贫血？"],
        "disclaimer": "本系统依据知识库内容作答，不构成医疗建议。",
    },
    "docs": 3,
    "chunks": 486,
    "answer_lang": "ug",
    "query_lang": "ug",
    "kb_lang": "zh",
    "answer_mode": "translate",
    "cross_lingual": True,
    "roles": {"translate": "bailian", "generate": "bailian"},
}

ANSWER_UG = (
    "ئۇيغۇر تېبابىتىدە تۆت خىل خىلىت بار، ئۇلار تۆۋەندىكىچە:\n\n"
    "1. قان خىلىتى — ئىسسىق ۋە نەم، تەبىئىتى قانغا ئوخشايدۇ [1].\n"
    "2. بەلغەم خىلىتى — سوغۇق ۋە نەم، سۇغا ئوخشايدۇ [1].\n"
    "3. سەپرا خىلىتى — ئىسسىق ۋە قۇرغاق، ئوتقا ئوخشايدۇ [2].\n"
    "4. سەۋدا خىلىتى — سوغۇق ۋە قۇرغاق، توپىغا ئوخشايدۇ [2].\n\n"
    "ھەر بىر ئادەمنىڭ مىزاجى بۇ تۆت خىلىتنىڭ بىرىكتۈرمىسىدىن تەركىب تاپىدۇ، "
    "شۇڭا داۋالاش ئۇسۇلىمۇ كىشىگە قاراپ پەرقلىنىدۇ."
)

ANSWER_ZH = (
    "维医认为人体由四种体液构成：\n\n"
    "1. **血液质（قان）**——湿热，性质如血 [1]\n"
    "2. **黏液质（بەلغەم）**——湿寒，性质如水 [1]\n"
    "3. **胆液质（سەپرا）**——干热，性质如火 [2]\n"
    "4. **黑胆质（سەۋدا）**——干寒，性质如土 [2]\n\n"
    "每个人的气质由此四者的配比决定，因此治疗需因人而异。"
)

CITES = [
    {"n": 1, "doc_title": "ئۇيغۇر تېبابىتى ئاساسلىرى.pdf", "page_no": 12,
     "heading_path": "ئىككىنچى باب / خىلىت نەزەرىيىسى",
     "preview": "ئۇيغۇر تېبابىتىدە ئادەم بەدىنى تۆت خىلىتتىن تەركىب تاپىدۇ: قان، بەلغەم، سەپرا، سەۋدا."},
    {"n": 2, "doc_title": "تىببىي مىزاج قوللانمىسى.docx", "page_no": 5,
     "heading_path": "مىزاج تۈرلىرى",
     "preview": "سەپرا خىلىتى ئىسسىق ۋە قۇرغاق، سەۋدا خىلىتى سوغۇق ۋە قۇرغاق بولىدۇ."},
]


def _sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def build_error_stream() -> str:
    """失败态：用来检查"模型没连上"时用户看到什么。"""
    return _sse("query", {"original": "سوئال", "used": "维医 四种体液",
                          "translated": True}) + _sse(
        "error", {"message": "LLMError: LLM 请求失败：ConnectError: [WinError 10061] "
                             "由于目标计算机积极拒绝，无法连接。"})


def build_stream(lang: str) -> str:
    draft = ANSWER_ZH if lang != "zh" else ANSWER_ZH
    answer = ANSWER_UG if lang != "zh" else ANSWER_ZH
    used = "维医 四种体液 气质"
    out = [_sse("query", {"original": "سوئال", "used": used, "translated": True})]
    out.append(_sse("meta", {"citations": CITES, "mode": "answer", "evidence": "high",
                             "two_stage": True, "context_used": 4}))
    out.append(_sse("stage", {"stage": "drafting"}))
    out.append(_sse("stage", {"stage": "translating", "draft": draft}))
    step = 12
    for i in range(0, len(answer), step):
        out.append(_sse("delta", {"text": answer[i:i + step]}))
    out.append(_sse("done", {"citations": CITES, "evidence": "high", "refused": False,
                             "draft": draft, "loop_truncated": False,
                             "timings_ms": {"search_ms": 240, "gen_ms": 6200}}))
    return "".join(out)


MOCK = """
<script>
(function () {
  // 每次跑都从零开始：会话存在 localStorage 里，不清掉的话上一轮的对话
  // 会出现在这一轮的截图/断言里（侧栏多一条、`.msg` 计数多两个）。
  try { localStorage.clear(); } catch (e) {}
  const INFO = __INFO__;
  const STREAM = __STREAM__;
  const realFetch = window.fetch;
  window.fetch = async function (url, opt) {
    const u = String(url);
    if (u.indexOf("/api/info") >= 0) {
      return new Response(JSON.stringify(INFO), {headers: {"Content-Type": "application/json"}});
    }
    if (u.indexOf("/api/ask") >= 0) {
      // 分块下发：真实接口是流式的，一次性给完整 body 测不出"生成中"的
      // 中间态（停止按钮、流式光标、滚动跟随都只在中间态里出现）。
      const enc = new TextEncoder();
      const chunks = STREAM.split("\\n\\n").map(function (x) { return x + "\\n\\n"; });
      const stream = new ReadableStream({
        async start(c) {
          c.enqueue(enc.encode(chunks[0]));
          __GATE__
          for (let i = 1; i < chunks.length; i++) c.enqueue(enc.encode(chunks[i]));
          c.close();
        }
      });
      return new Response(stream, {headers: {"Content-Type": "text/event-stream"}});
    }
    return realFetch.apply(window, arguments);
  };
  __DRIVE__
})();
</script>
"""

DRIVE_FILLED = """
addEventListener("load", function () {
  setTimeout(function () {
    const chips = document.querySelectorAll(".chip");
    if (chips.length) chips[0].click();
  }, 120);
});
"""

DRIVE_DRAWER = """
addEventListener("load", function () {
  setTimeout(function () {
    var b = document.getElementById("btnSide");
    if (b) b.click();
  }, 300);
});
"""

DRIVE_ZH = """
addEventListener("load", function () {
  setTimeout(function () {
    const b = document.querySelector('#langSwitch [data-lang="zh"]');
    if (b) b.click();
  }, 60);
});
"""


def build_html(lang: str, filled: bool, zh: bool = False, gate: str = "",
               error: bool = False, stream_file: str = "", drawer: bool = False) -> str:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    html = re.sub(r'<link rel="stylesheet"[^>]*>', f"<style>\n{css}\n</style>", html)
    mock = MOCK.replace("__INFO__", json.dumps(INFO_UG, ensure_ascii=False))
    if stream_file:
        # 用真实接口录下来的 SSE 喂给页面：截图里就是线上的答案，不是编的
        stream = Path(stream_file).read_text(encoding="utf-8")
    elif error:
        stream = build_error_stream()
    else:
        stream = build_stream(lang)
    mock = mock.replace("__STREAM__", json.dumps(stream, ensure_ascii=False))
    # 先切语言再提问：模拟真实用户点顶栏那颗"中文"按钮
    drive = ((DRIVE_ZH if zh else "") + (DRIVE_FILLED if filled else "")
             + (DRIVE_DRAWER if drawer else ""))
    mock = mock.replace("__DRIVE__", drive)
    # gate：让 /api/ask 的流在某个点停住，用来测"生成中"的界面状态
    mock = mock.replace("__GATE__", gate)
    html = re.sub(r'<script src="/app\.js[^"]*"></script>',
                  lambda m: mock + f"<script>\n{js}\n</script>", html)
    return html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_shot/shot.png")
    ap.add_argument("--filled", action="store_true", help="模拟一轮问答")
    ap.add_argument("--zh", action="store_true", help="中文界面")
    ap.add_argument("--error", action="store_true", help="模拟模型未连接的失败态")
    ap.add_argument("--stream-file", default="", help="用录好的 SSE 文本当答案")
    ap.add_argument("--w", type=int, default=1100)
    ap.add_argument("--h", type=int, default=900)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--keep", action="store_true", help="保留生成的 HTML 便于调试")
    # 系统的浅/深色是随日落自动切的，截图不能"今天白明天黑"：
    # 显式指定一次，两套配色才都能被验证。
    ap.add_argument("--theme", choices=["light", "dark", "auto"], default="light")
    ap.add_argument("--drawer", action="store_true", help="窄屏下展开侧栏抽屉")
    args = ap.parse_args()

    if not EDGE.exists():
        print(f"找不到 Edge: {EDGE}", file=sys.stderr)
        return 2

    out = (ROOT / args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="ragkb-shot-"))
    page = tmpdir / "index.html"
    page.write_text(build_html("zh" if args.zh else "ug", args.filled, args.zh,
                               error=args.error, stream_file=args.stream_file,
                               drawer=args.drawer),
                    encoding="utf-8")

    # Edge headless 在 Windows 上有 500px 的最小窗口宽度：直接按 390 截图，
    # 布局视口会被撑到 500，截出来的右侧是被裁掉的，看着像"页面溢出"。
    # 所以窄屏一律套一个 iframe，媒体查询按 iframe 的宽度算。
    target = page
    win_w, win_h = args.w, args.h
    if args.w < 500:
        wrap = tmpdir / "wrap.html"
        wrap.write_text(
            "<!doctype html><meta charset=utf-8>"
            "<body style=\'margin:0;background:#8a8f8c;display:flex;"
            "justify-content:center;align-items:flex-start\'>"
            f"<iframe src=\'index.html\' style=\'width:{args.w}px;height:{args.h}px;"
            "border:0;display:block;background:#fff\'></iframe></body>",
            encoding="utf-8")
        target = wrap
        win_w, win_h = max(args.w, 500), args.h

    cmd = [
        str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check",
        "--allow-file-access-from-files",
        "--force-device-scale-factor=" + str(args.scale),
        *(["--blink-settings=preferredColorScheme="
           # Edge 实测：preferredColorScheme 1=浅色、0=深色（2 会被忽略）
           + ("1" if args.theme == "light" else "0")] if args.theme != "auto" else []),
        f"--window-size={win_w},{win_h}",
        "--virtual-time-budget=6000",
        f"--screenshot={out}",
        target.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if not out.exists():
        print(proc.stderr[-1500:], file=sys.stderr)
        return 1
    if args.keep:
        print(f"html: {target}")
    print(f"png:  {out}  ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
