"""导入前的数据清洗：把"不该进库的内容"在切分之前剔掉。

**第一条原则：源文件一个字节都不改。** 清洗发生在「抽取之后、切分之前」的
内存里，作用于 LoadedDoc 的块。上传副本也只当原料——把清洗结果写回文件会让
重索引拿到"洗过一遍"的输入（二次清洗的结果不可预期），而且从此分不清库里
的内容对应源文件的哪一部分。

**第二条原则：清洗必须可核对。** 规则删错就是永久丢失：chunk 里没有的东西
永远检索不到，且没有任何迹象提示"这里原本有内容"。所以每条规则都报"删了
几个块 / 多少字 / 哪些行"，并附样例 —— 预览存在的全部理由就是让人在导入前
逐条看一眼，而不是事后对着检索结果怀疑"是不是没导全"。

规则的默认值偏向保守：只默认开那些删错了也几乎不心疼的（空白、页码、目录
点线行、重复块），会删掉实质内容的（过短片段、代码块、关键词、正则）默认关，
要用的人自己打开，并在预览里确认代价。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..models import Block, LoadedDoc

# ---------------------------------------------------------------- 正则

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TRAIL_WS = re.compile(r"[ \t\u3000]+$", re.M)
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_FULL_SPACE = re.compile("\u3000")

_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)
_HTML_TAG = re.compile(r"<[^>]{0,300}>")
_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w-]+")
_MD_HEAD = re.compile(r"^#{1,6}[ \t]*", re.M)
_MD_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.M)
_MD_EMPH = re.compile(r"(\*\*|__|~~)")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")

_IMAGE_ONLY = re.compile(
    r"^(?:!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>|data:image/[^\s]+|<图片[:：]?[^>]*>)$",
    re.I | re.S,
)
_PAGE_NO = re.compile(
    r"^(?:第\s*\d+\s*页(?:\s*[/共]\s*\d+\s*页?)?|[—–\-~～]\s*\d+\s*[—–\-~～]"
    r"|\[?\s*page\s*\d+(?:\s*(?:of|/)\s*\d+)?\s*\]?|\d{1,4})$",
    re.I,
)
_TOC_DOTS = re.compile(r"\.{3,}|\u00b7{3,}|\u2022{3,}|…{2,}")
_TAIL_NUM = re.compile(r"\d{1,4}\s*$")
_CODE_FENCE = re.compile(r"^\s*(?:```|~~~)")
_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+")

MAX_SAMPLES = 3
MAX_SNIPPET = 120


# ---------------------------------------------------------------- 规则表

RULES: tuple[dict[str, Any], ...] = (
    {"id": "norm_ws", "label": "空白与不可见字符", "level": "text", "default": True,
     "hint": "去零宽字符/控制字符、全角空格转半角、行尾空白、3 行以上空行压成 1 行"},
    {"id": "strip_html", "label": "HTML 标签", "level": "text", "default": True,
     "hint": "去掉 <script>/<style> 整段与其余标签，只留文字"},
    {"id": "strip_md", "label": "Markdown 标记", "level": "text", "default": False,
     "hint": "去掉 # 标题符、** 强调、> 引用符，链接只留文字。字数统计会变，慎用于要保留排版的文档"},
    {"id": "strip_url", "label": "网址与邮箱", "level": "text", "default": False,
     "hint": "删掉 http(s)://…、www.…、name@host"},
    {"id": "drop_empty", "label": "空白块", "level": "block", "default": True,
     "hint": "清洗后什么都不剩的块"},
    {"id": "drop_page_no", "label": "页码行", "level": "block", "default": True,
     "hint": "整块只有页码：「第 12 页」「- 12 -」「Page 3 / 20」「12」"},
    {"id": "drop_toc", "label": "目录点线行", "level": "block", "default": True,
     "hint": "「第一章 …… 12」这类点线引导 + 行尾页码的目录行"},
    {"id": "drop_image", "label": "图片与附件", "level": "block", "default": True,
     "hint": "整块只有图片：![]()、<img>、base64 数据串"},
    {"id": "drop_dup", "label": "完全重复的块", "level": "block", "default": True,
     "hint": "忽略空白后逐字相同的块只留第一处（PDF 转出来的重复页眉页脚多半落在这里）"},
    {"id": "drop_repeated_lines", "label": "多次出现的短行（页眉页脚）", "level": "line",
     "default": True,
     "hint": "页眉页脚会横跨多页重复出现。按页码取每页首末行，数字抹掉后相同即判定，"
             "所以「第 40 页 / 共 195 页」和「第 41 页」算同一行。表格不在页面边缘，不会误删",
     "param": {"key": "repeat_min", "label": "最少跨几页出现", "kind": "int",
               "default": 3, "min": 2, "max": 99}},
    {"id": "drop_short", "label": "过短片段", "level": "block", "default": False,
     "hint": "正文不足 N 字的片段（孤立编号、图注碎片）。代码块不受影响",
     "param": {"key": "short_min", "label": "最少字数", "kind": "int",
               "default": 8, "min": 1, "max": 500}},
    {"id": "drop_code", "label": "代码块", "level": "block", "default": False,
     "hint": "整块是代码的删掉。源码类知识库别开这个"},
    {"id": "drop_keywords", "label": "含指定词的整段", "level": "block", "default": False,
     "hint": "块里出现任一个词就整段丢弃。适合清「内部资料 禁止外传」这类模板段落",
     "param": {"key": "keywords", "label": "关键词", "kind": "text", "default": "",
               "placeholder": "内部资料,禁止外传,免责声明"}},
    {"id": "drop_regex", "label": "命中正则的整段", "level": "block", "default": False,
     "hint": "整块匹配命中就丢弃。正则写错会在导入前明确报错，不会静默什么都不删",
     "param": {"key": "regex", "label": "正则表达式", "kind": "text", "default": "",
               "placeholder": "^(免责声明|参考文献)"}},
)

RULE_MAP = {r["id"]: r for r in RULES}


# ---------------------------------------------------------------- 方案

@dataclass
class CleanPlan:
    """一次导入要跑哪些规则。enabled 缺项按规则表默认值补齐。"""

    enabled: dict[str, bool] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    def on(self, rule_id: str) -> bool:
        return bool(self.enabled.get(rule_id, RULE_MAP[rule_id]["default"]))

    def opt(self, key: str, fallback: Any = None) -> Any:
        value = self.options.get(key, fallback)
        return fallback if value is None else value

    def any_enabled(self) -> bool:
        return any(self.on(r["id"]) for r in RULES)

    def active_ids(self) -> list[str]:
        return [r["id"] for r in RULES if self.on(r["id"])]

    def as_dict(self) -> dict:
        return {"enabled": dict(self.enabled), "options": dict(self.options)}

    def validate(self) -> None:
        """正则写错要在这里炸，而不是等到删了半篇才发现规则根本没生效。"""
        if not self.on("drop_regex"):
            return
        pattern = (self.opt("regex", "") or "").strip()
        if not pattern:
            return
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"清洗正则写错了：{exc}") from exc


def default_plan() -> CleanPlan:
    return CleanPlan(enabled={}, options={})


def plan_from_dict(raw: Any) -> CleanPlan:
    """宽容解析：前端只把"改过的"发回来，缺的一律按默认走。"""
    if not isinstance(raw, dict):
        return default_plan()
    enabled = {k: bool(v) for k, v in (raw.get("enabled") or {}).items()
               if isinstance(k, str) and k in RULE_MAP}
    # 参数一律按规则表声明的类型收敛：表单里所有 input.value 都是字符串，
    # "12" 流到 < 比较上就是 TypeError，而且只在大文档才炸。
    kinds = {(r.get("param") or {}).get("key"): (r.get("param") or {}).get("kind")
             for r in RULES if r.get("param")}
    options: dict[str, Any] = {}
    for k, v in (raw.get("options") or {}).items():
        if not isinstance(k, str):
            continue
        if kinds.get(k) == "int":
            try:
                options[k] = int(float(v))
            except (TypeError, ValueError):
                continue
        elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
            options[k] = str(v)
    return CleanPlan(enabled=enabled, options=options)


def rules_payload() -> dict:
    """给前端画规则面板：定义、默认值、参数规格全在后端，前端不写死任何一条。"""
    return {
        "rules": [dict(r) for r in RULES],
        "defaults": {r["id"]: r["default"] for r in RULES},
        "options": {(r.get("param") or {}).get("key"): r["param"]["default"]
                    for r in RULES if r.get("param")},
    }


# ---------------------------------------------------------------- 执行

@dataclass
class CleanReport:
    before_chars: int = 0
    before_blocks: int = 0
    after_chars: int = 0
    after_blocks: int = 0
    rules: list[dict] = field(default_factory=list)

    @property
    def removed_chars(self) -> int:
        return max(0, self.before_chars - self.after_chars)

    def as_dict(self) -> dict:
        return {
            "before": {"chars": self.before_chars, "blocks": self.before_blocks},
            "after": {"chars": self.after_chars, "blocks": self.after_blocks},
            "removed_chars": self.removed_chars,
            "removed_pct": round(self.removed_chars / self.before_chars * 100, 1)
            if self.before_chars else 0.0,
            "rules": self.rules,
        }


def _snippet(text: str) -> str:
    flat = _WS.sub(" ", (text or "").strip())
    return flat[:MAX_SNIPPET] + ("…" if len(flat) > MAX_SNIPPET else "")


def _line_key(line: str) -> str | None:
    """行指纹：数字抹成 #，空白抹掉。太长的行不参与（正文不该被当页眉）。

    「第 40 页 / 共 195 页」和「第 41 页 / 共 195 页」逐字不同，抹掉数字后
    是同一个东西。不抹，页码一变就谁也认不出谁——这正是页眉页脚删不掉的
    原因：它们每页都长一样，唯独数字不一样。
    """
    s = (line or "").strip()
    if not s:
        return None
    key = _WS.sub("", _NUM.sub("#", s))
    return key if 0 < len(key) <= 80 else None


def clean_text(text: str, plan: CleanPlan, kind: str = "para", hit=None) -> str:
    """文本级规则，按顺序逐条跑。给 hit 就顺便把每条规则的字数差记下来。

    逐条单独跑（而不是一把梭）只是为了记账：用户要看到"URL 删了 1200 字"，
    而不是一句"文本级规则共删了 3000 字"。几千个块跑几遍正则很便宜。
    """
    out = text or ""

    def step(rule_id: str, new: str) -> None:
        nonlocal out
        if new != out:
            if hit is not None:
                hit(rule_id, "", chars=max(0, len(out) - len(new)))
            out = new

    if plan.on("norm_ws"):
        step("norm_ws", _norm_ws(out, kind))
    if plan.on("strip_html"):
        step("strip_html", _HTML_TAG.sub("", _SCRIPT.sub("", out)))
    if plan.on("strip_md"):
        md = _MD_LINK.sub(r"\1", out)
        md = _MD_HEAD.sub("", md)
        md = _MD_QUOTE.sub("", md)
        md = _MD_EMPH.sub("", md)
        step("strip_md", md)
    if plan.on("strip_url"):
        step("strip_url", _EMAIL.sub("", _URL.sub("", out)))
    return out


def _norm_ws(text: str, kind: str) -> str:
    out = _ZERO_WIDTH.sub("", text)
    out = _CTRL.sub("", out)
    out = _FULL_SPACE.sub(" ", out)
    out = _TRAIL_WS.sub("", out)
    out = _MULTI_BLANK.sub("\n\n", out)
    # 代码块里的缩进是内容，不能压；其他地方的连续空格基本都是排版产物
    if kind != "code":
        out = _MULTI_SPACE.sub(" ", out)
    return out.strip()


def _detect_running_heads(
    blocks: list[tuple[Block, str]], repeat_min: int
) -> tuple[set[str], set[str]]:
    """找出页眉页脚，返回 (要删的行指纹集合, 证据原文集合)。

    判据是**页覆盖率**，不是重复次数。页眉页脚的定义就是"几乎每页都有"，
    所以要求同一个指纹出现在 ≥repeat_min 页、且 ≥30% 的页上。

    为什么不能只数重复次数：一张三十行的表，每行数字都不同、抹掉数字后
    又长得一样，纯按次数统计一定会把整张表当页眉删掉。而表格只活在它所在
    的那一两页，覆盖率个位数；页眉是 100%。这一条把两者彻底分开，不靠
    长度、不靠标点猜测。

    没有页码信息时（Markdown / txt）退回逐字精确匹配，不做数字模板化——
    缺位置和覆盖率证据就不敢用模板。删错了不会报错，只会让某些段落凭空
    消失，这种损失没法靠"再删一次"挽回。
    """
    total_pages = len({b.page_no for b, _ in blocks if b.page_no is not None})
    pages_of: dict[str, set[int]] = {}
    display: dict[str, str] = {}
    plain: Counter[str] = Counter()

    for b, text in blocks:
        for ln in (ln.strip() for ln in text.splitlines()):
            if not ln:
                continue
            if b.page_no is None:
                if len(ln) <= 40:
                    plain[ln] += 1
                continue
            key = _line_key(ln)
            if key:
                pages_of.setdefault(key, set()).add(b.page_no)
                display.setdefault(key, ln)

    doomed: set[str] = set()
    evidence: set[str] = set()
    if total_pages:
        floor = max(repeat_min, int(total_pages * 0.3))
        for key, pgs in pages_of.items():
            if len(pgs) >= floor:
                doomed.add(key)
                evidence.add(display[key])
    # 纯符号行（《》、——— 这种）：没有字符可检索，留着只会稀释向量
    for key, ln in display.items():
        if len(key) <= 10 and not any(ch.isalnum() for ch in key):
            doomed.add(key)
            evidence.add(ln)
    for ln, n in plain.items():
        if n >= repeat_min:
            doomed.add(ln)
            evidence.add(ln)
    return doomed, evidence


def clean_loaded(loaded: LoadedDoc, plan: CleanPlan) -> tuple[LoadedDoc, CleanReport]:
    """清洗一份已抽取的文档，返回 (新 LoadedDoc, 逐条规则的账单)。

    顺序是有讲究的：先做文本级替换（把页面噪声压成可判定的形态），再做行级
    （跨块统计出来的页眉页脚），最后才判整块去留——否则"第 12 页"这种块会
    因为前面的规则没跑而躲过判定。
    """
    stats = {r["id"]: {"id": r["id"], "label": r["label"], "blocks": 0, "lines": 0,
                       "chars": 0, "samples": []} for r in RULES}

    def hit(rule_id: str, text: str, *, chars: int = 0, lines: int = 0, blocks: int = 0) -> None:
        s = stats[rule_id]
        s["chars"] += chars
        s["lines"] += lines
        s["blocks"] += blocks
        if text and len(s["samples"]) < MAX_SAMPLES:
            s["samples"].append(_snippet(text))

    before_chars = sum(len(b.text or "") for b in loaded.blocks)
    before_blocks = len(loaded.blocks)

    # ① 文本级
    stage1: list[tuple[Block, str]] = []
    for b in loaded.blocks:
        stage1.append((b, clean_text(b.text or "", plan, b.kind, hit)))

    # ② 行级：跨块统计出来的页眉页脚
    stage2 = stage1
    if plan.on("drop_repeated_lines"):
        repeat_min = max(2, int(plan.opt("repeat_min", 3) or 3))
        doomed, evidence = _detect_running_heads(stage1, repeat_min)
        if doomed:
            stage2 = []
            for b, t in stage1:
                kept_lines = [ln for ln in t.splitlines()
                              if _line_key(ln) not in doomed and ln.strip() not in doomed]
                new = "\n".join(kept_lines)
                if new != t:
                    hit("drop_repeated_lines", "",
                        chars=max(0, len(t) - len(new)),
                        lines=len(t.splitlines()) - len(kept_lines))
                stage2.append((b, new))
            # 样例给"被判定为页眉页脚的那些行"，比给出被改后的块更能说明删了什么
            stats["drop_repeated_lines"]["samples"] = [_snippet(s) for s in sorted(evidence)[:3]]

    # ③ 块级
    kept: list[Block] = []
    seen: set[str] = set()
    for b, text in stage2:
        stripped = text.strip()
        if not stripped:
            if plan.on("drop_empty"):
                hit("drop_empty", text, blocks=1, chars=len(text))
                continue
        elif _block_dropped(stripped, text, b, plan, hit):
            continue
        key = _WS.sub("", stripped)
        if key and plan.on("drop_dup"):
            if key in seen:
                hit("drop_dup", text, blocks=1, chars=len(text))
                continue
            seen.add(key)
        kept.append(Block(text=text, page_no=b.page_no,
                          heading_path=b.heading_path, kind=b.kind))

    text_all = "\n\n".join(b.text for b in kept)
    report = CleanReport(
        before_chars=before_chars,
        before_blocks=before_blocks,
        after_chars=sum(len(b.text) for b in kept),
        after_blocks=len(kept),
        rules=[s for s in stats.values()
               if s["blocks"] or s["lines"] or s["chars"]],
    )
    new_doc = LoadedDoc(
        text=text_all,
        blocks=kept,
        title=loaded.title,
        n_pages=loaded.n_pages,
        # 抽取器**不随清洗改名**：文档行上"抽取器已升级"的提示是按抽取器名
        # 判断的，改了会让每篇清洗过的文档都谎报"内容可补全"。
        extractor=loaded.extractor,
        meta={**loaded.meta, "clean": plan.as_dict()},
    )
    return new_doc, report


def _block_dropped(stripped: str, raw: str, block: Block, plan: CleanPlan, hit) -> bool:
    def drop(rule_id: str) -> bool:
        hit(rule_id, raw, blocks=1, chars=len(raw))
        return True

    if plan.on("drop_page_no") and _PAGE_NO.match(stripped):
        return drop("drop_page_no")
    if plan.on("drop_toc") and _TOC_DOTS.search(stripped) and _TAIL_NUM.search(stripped):
        return drop("drop_toc")
    if plan.on("drop_image") and _IMAGE_ONLY.match(stripped):
        return drop("drop_image")
    if plan.on("drop_code") and (block.kind == "code" or _CODE_FENCE.match(raw)):
        return drop("drop_code")
    if plan.on("drop_short") and block.kind != "code":
        if len(_WS.sub("", stripped)) < max(1, int(plan.opt("short_min", 8) or 8)):
            return drop("drop_short")
    if plan.on("drop_keywords"):
        words = [w.strip() for w in (plan.opt("keywords", "") or "").replace("，", ",").split(",")
                 if w.strip()]
        if any(w in stripped for w in words):
            return drop("drop_keywords")
    if plan.on("drop_regex"):
        pattern = (plan.opt("regex", "") or "").strip()
        if pattern:
            try:
                if re.search(pattern, stripped):
                    return drop("drop_regex")
            except re.error:
                pass  # 校验阶段已经挡过一次，这里不重复报错
    return False