"""Deterministic validation for the full docs rewrite. Read-only."""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md"] + sorted((ROOT / "docs").iterdir())
MD = [p for p in DOCS if p.suffix == ".md"]
HTML = [p for p in DOCS if p.suffix == ".html"]
errors: list[str] = []
notes: list[str] = []


def headings(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^#{2,4}\s+(\d+(?:\.\d+)*)[.、\s]", line)
        if m:
            out.add(m.group(1))
    return out


def links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = re.findall(r"\[[^\]]*\]\(([^)]+)\)", text)
    found += re.findall(r'(?:href|src)="([^"#?:]+?)(?:[?#][^"]*)?"', text)
    return [x for x in found if not x.startswith(("http", "mailto:", "#", "data:"))]


# --- 1. links -------------------------------------------------------------
for p in DOCS:
    for raw in links(p):
        target = (p.parent / raw).resolve()
        if not target.exists():
            errors.append(f"[link] {p.relative_to(ROOT)} -> {raw} (missing)")

# --- 2. section references ------------------------------------------------
# A reference must resolve somewhere in the doc set. References written as an
# explicit "[doc.md](doc.md) §N" pair must additionally resolve in that doc.
head_maps = {p.name: headings(p) for p in MD}
all_heads: set[str] = set().union(*head_maps.values())
for p in MD:
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if "§" not in line:
            continue
        paired: dict[str, str] = {}
        for m in re.finditer(r"\]\(([\w./-]+\.md)\)\s*§(\d+(?:\.\d+)*)", line):
            paired[m.group(2)] = Path(m.group(1)).name
        for ref in re.findall(r"§(\d+(?:\.\d+)*)", line):
            if ref not in all_heads:
                errors.append(f"[section] {p.relative_to(ROOT)}:{i} §{ref} 全文档无此章节")
            if ref in paired:
                target = head_maps.get(paired[ref], set())
                if ref not in target and ref.split(".")[0] not in target:
                    errors.append(
                        f"[section] {p.relative_to(ROOT)}:{i} §{ref} -> "
                        f"{paired[ref]} (no such heading)"
                    )

# --- 3. stale claims ------------------------------------------------------
STALE = [
    "精排模型未下载", "精排未开启", "精排没有缓存", "当前直接降级",
    "测试 172 条全绿", "172 条全绿", "每次问答都算支撑度",
    "rerank_unavailable 写进响应", "精排可选",
]
ALLOW = ("已失效", "不得", "不写", "不能写", "不能", "不是", "旧版", "历史",
         "清掉", "禁止", "纠正", "不生效", "已不生效", "误", "未", "不用来")
for p in DOCS:
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        for s in STALE:
            if s in line and not any(a in line for a in ALLOW):
                errors.append(f"[stale] {p.relative_to(ROOT)}:{i} {s}")

# obsolete numbers must only appear in an explicitly historical line
for p in DOCS:
    section = ""
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.match(r"^#{2,4}\s", line):
            section = line
        in_history = any(k in section for k in ("历史", "已失效", "归档"))
        for old in ("0.692", "0.959", "61 组", "漏拒 23", "0.54 为", "172"):
            if old in line and not in_history and not any(
                a in line for a in ("已失效", "历史", "旧版", "当时", "不得", "禁止", "纠正")
            ):
                errors.append(f"[stale-number] {p.relative_to(ROOT)}:{i} {old}")

# --- 4. current facts appear ---------------------------------------------
corpus = "\n".join(p.read_text(encoding="utf-8") for p in DOCS)
for fact, label in [
    ("0.581", "现行拒答阈值"),
    ("AUC 1.0", "现行标定 AUC"),
    ("4096", "精排缓存容量"),
    ("8013", "后台端口"),
    ("8800", "前台端口"),
    ("24/4/4", "统计口径一"),
    ("25/4/3", "统计口径二"),
    ("22/4/6", "统计口径三"),
]:
    if fact not in corpus:
        errors.append(f"[missing-fact] {label} ({fact}) 未出现在任何文档")

# Build numbers are read from source, not hardcoded. Hardcoding them made the
# checker go stale on every legitimate version bump; it also could not detect
# the actual failure mode (the four constants drifting apart).
STATIC = ROOT / "src/knowtune"
for label, pkg, const, req in [
    ("前台构建号", "frontsite", "window.SITE_BUILD", "window.SITE_REQ_BUILD"),
    ("后台构建号", "api", "window.ADMIN_BUILD", "window.ADMIN_REQ_BUILD"),
]:
    js = STATIC / pkg / "static/app.js"
    html = STATIC / pkg / "static/index.html"
    m = re.search(rf'{re.escape(const)}\s*=\s*"([^"]+)"', js.read_text(encoding="utf-8"))
    if not m:
        errors.append(f"[build] {label}: 无法从 {js.name} 读取 {const}")
        continue
    version = m.group(1)
    declared = {version}
    for pat in (rf'{re.escape(req)}\s*=\s*"([^"]+)"', r"\?v=([\d.]+)"):
        declared.update(re.findall(pat, html.read_text(encoding="utf-8")))
    if declared != {version}:
        errors.append(f"[build] {label}: HTML 与 JS 构建号不一致 {sorted(declared)}")
    if version not in corpus:
        errors.append(f"[missing-fact] {label} 源码={version}，文档未同步")
    else:
        notes.append(f"{label} 源码={version}，HTML/JS 四处一致且文档已同步")

def ordered(line: str, *tokens: str) -> bool:
    """Tokens must appear in order, searching forward from the previous hit.

    A plain ``find`` on each token is wrong here: prose often mentions one
    level early ("留空跟随站点级 roles") before the ordered chain starts.
    """
    pos = 0
    for token in tokens:
        found = line.find(token, pos)
        if found < 0:
            return False
        pos = found + len(token)
    return True


for label, tokens, minimum in [
    ("提示词优先级", ("by_provider", "by_scope", "default", "内置"), 6),
    ("角色优先级", ("板块级 roles", "站点级 roles", "系统级 roles"), 5),
]:
    hits = [
        p.name for p in DOCS
        if any(ordered(line, *tokens)
               for line in p.read_text(encoding="utf-8").splitlines())
    ]
    if not hits:
        errors.append(f"[missing-fact] {label} 统一写法缺失")
    elif len(hits) < minimum:
        errors.append(f"[missing-fact] {label} 仅 {len(hits)} 份文档写明：{hits}")
    else:
        notes.append(f"{label} 在 {len(hits)} 份文档中顺序一致：{', '.join(hits)}")

# --- 5. HTML balance ------------------------------------------------------
class Balance(HTMLParser):
    VOID = {"meta", "link", "br", "img", "input", "hr", "source"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.bad: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.bad.append(f"close </{tag}> while open {self.stack[-3:]}")
        else:
            self.stack.pop()


for p in HTML:
    b = Balance()
    b.feed(p.read_text(encoding="utf-8"))
    for x in b.bad:
        errors.append(f"[html] {p.relative_to(ROOT)} {x}")
    if b.stack:
        errors.append(f"[html] {p.relative_to(ROOT)} unclosed {b.stack[-5:]}")

# --- 6. code / config static checks --------------------------------------
py = subprocess.run(
    [sys.executable, "-c",
     "import ast,pathlib;"
     "bad=[str(p) for p in pathlib.Path('src').rglob('*.py') "
     "if not _try(p)]" if False else
     "import ast,pathlib\n"
     "bad=[]\n"
     "for p in pathlib.Path('src').rglob('*.py'):\n"
     "    try: ast.parse(p.read_text(encoding='utf-8'))\n"
     "    except Exception as e: bad.append(f'{p}: {e}')\n"
     "print(len(bad)); [print(x) for x in bad]"],
    cwd=ROOT, capture_output=True, text=True,
)
if py.stdout.strip().splitlines()[0] != "0":
    errors.append(f"[python] syntax errors: {py.stdout.strip()}")

node = subprocess.run(["node", "--check", "src/knowtune/frontsite/static/app.js"],
                      cwd=ROOT, capture_output=True, text=True)
if node.returncode:
    errors.append(f"[js] frontsite app.js: {node.stderr.strip()}")
node2 = subprocess.run(["node", "--check", "src/knowtune/api/static/app.js"],
                       cwd=ROOT, capture_output=True, text=True)
if node2.returncode:
    errors.append(f"[js] admin app.js: {node2.stderr.strip()}")

yaml_check = subprocess.run(
    [sys.executable, "-c",
     "import yaml,pathlib\n"
     "for p in pathlib.Path('config').rglob('*.yaml'): yaml.safe_load(p.read_text(encoding='utf-8'))\n"
     "print('ok')"],
    cwd=ROOT, capture_output=True, text=True,
)
if "ok" not in yaml_check.stdout:
    errors.append(f"[yaml] {yaml_check.stderr.strip()}")

sql = (ROOT / "src/knowtune/schema.sql").read_text(encoding="utf-8")
try:
    con = sqlite3.connect(":memory:")
    con.executescript(sql)
    con.close()
except Exception as e:
    errors.append(f"[sql] {e}")

# --- 6b. structure / config / install health -----------------------------
# 这一节的动机：b5efb48 把 app.py 从 4400 行拆成 274 行 + routes/ 8 模块后，
# 文档里「约 4400 行，唯一大文件」和一堆 :1271-1303 行号全部失效，而上面
# 1~6 节一条都没报；同一时期 editable 安装掉了、models.yaml 混进盘符绝对
# 路径，ALL CHECKS PASSED 照样照常打印。校验器必须能看见代码本身。
HISTORY_MARKERS = ("历史", "曾", "同日", "已失效", "当时", "旧版", "归档", "快照")

for p in MD:  # WBS.html 是交付时快照，明确豁免
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if any(k in line for k in HISTORY_MARKERS):
            continue
        if "4400" in line or "唯一大文件" in line:
            errors.append(f"[structure] {p.name}:{i} app.py 拆分后仍写 4400 行/唯一大文件")
        if re.search(r"api/app\.py:\d+", line):
            errors.append(
                f"[structure] {p.name}:{i} 已失效的 app.py 行号引用（应写模块/函数名）"
            )

# 构建号：只校验「现行陈述」，历史演进段（含 曾/同日/历史）整行豁免
build_versions: dict[str, str] = {}
for pkg, const in (("api", "window.ADMIN_BUILD"), ("frontsite", "window.SITE_BUILD")):
    js = (STATIC / pkg / "static/app.js").read_text(encoding="utf-8")
    m = re.search(rf'{re.escape(const)}\s*=\s*"([^"]+)"', js)
    if m:
        build_versions[m.group(1)] = pkg
for p in MD:
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if any(k in line for k in HISTORY_MARKERS):
            continue
        claimed = re.findall(r"(?:构建号|现行|统一)\s*`?((?:0\.9|1\.7)\.\d+)", line)
        claimed += re.findall(r"\?v=((?:0\.9|1\.7)\.\d+)", line)
        for v in claimed:
            if v not in build_versions:
                errors.append(
                    f"[build] {p.name}:{i} 构建号 {v} 不是现行源码值 {sorted(build_versions)}"
                )

# 端点装配：include_router 数量必须等于 routes/ 下的模块数，且文档写明拆分
app_src = (ROOT / "src/knowtune/api/app.py").read_text(encoding="utf-8")
route_mods = [p for p in (ROOT / "src/knowtune/api/routes").glob("*.py")
              if p.name != "__init__.py"]
n_include = len(re.findall(r"^app\.include_router\(", app_src, re.M))
if n_include != len(route_mods):
    errors.append(
        f"[structure] app.py include_router={n_include} 与 routes 模块数 "
        f"{len(route_mods)} 不等"
    )
if "api/routes/" not in corpus:
    errors.append("[structure] 文档未写明 api/routes/ 拆分")

# 干净解释器：不注入 sys.path，必须能 import knowtune 并跑 CLI
clean = subprocess.run(
    [sys.executable, "-c", "import knowtune, knowtune.cli; print(knowtune.__file__)"],
    cwd=ROOT, capture_output=True, text=True,
)
if clean.returncode or "knowtune" not in clean.stdout:
    errors.append(f"[install] 干净解释器 import knowtune 失败：{clean.stderr.strip()[-300:]}")
cli_smoke = subprocess.run(
    [sys.executable, "-m", "knowtune.cli", "--help"],
    cwd=ROOT, capture_output=True, text=True,
)
if cli_smoke.returncode or "serve" not in cli_smoke.stdout:
    errors.append(f"[install] CLI 入口不可用：{cli_smoke.stderr.strip()[-300:]}")

# 站点卡参数行入口：README 写「四行参数表整行可点」，代码得真接住；
# 同时守住左右分栏，防止有人又把参数区塞回四格横排。
admin_js = (STATIC / "api" / "static/app.js").read_text(encoding="utf-8")
for token, why in (
    ("const TAB_NAME", "页签映射"),
    ('data-jumptab="${tab}"', "参数行的跳转属性"),
    ("async function openSiteConfig(name, initialTab", "弹窗初始页签参数"),
    ("jump.dataset.jumptab", "卡片点击分支"),
    ('class="sbk-grid"', "左右分栏容器"),
    ('class="sbk-rows"', "纵向参数行表"),
):
    if token not in admin_js:
        errors.append(f"[ui] app.js 缺少站点卡参数入口的{why}：{token}")
admin_css = (STATIC / "api" / "static/style.css").read_text(encoding="utf-8")
for token, why in (
    (".sbk-row:hover", "参数行可点样式"),
    (".sbk-grid", "左右分栏样式"),
    (".sbk-side", "左栏样式"),
):
    if token not in admin_css:
        errors.append(f"[ui] style.css 缺少{why}：{token}")

# models.yaml 不允许盘符绝对路径（项目外模型用 ../ 相对路径）
for p in (ROOT / "config").glob("*.yaml"):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"^\s*path:\s*[A-Za-z]:[\\/]", line):
            errors.append(
                f"[paths] {p.name}:{i} 模型 path 写成盘符绝对路径：{line.strip()}"
            )

# --- 7. report ------------------------------------------------------------
print(f"docs={len(DOCS)} md={len(MD)} html={len(HTML)}")
print(f"headings: " + ", ".join(f"{k}={len(v)}" for k, v in head_maps.items()))
for n in notes:
    print("NOTE", n)
if errors:
    print(f"FAILED: {len(errors)} issue(s)")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("ALL CHECKS PASSED")
