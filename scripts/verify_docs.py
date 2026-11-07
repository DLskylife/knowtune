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
    ("1.7.18", "前台构建号"),
    ("0.9.54", "后台构建号"),
    ("4096", "精排缓存容量"),
    ("8013", "后台端口"),
    ("8800", "前台端口"),
    ("24/4/4", "统计口径一"),
    ("25/4/3", "统计口径二"),
    ("22/4/6", "统计口径三"),
]:
    if fact not in corpus:
        errors.append(f"[missing-fact] {label} ({fact}) 未出现在任何文档")

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
