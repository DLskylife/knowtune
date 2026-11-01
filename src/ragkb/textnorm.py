"""码位归一化：把"看着是字、机器不认"的字修回来。

问题不是排版难看，是**检索直接废掉**。

真实来源：某些排版软件 / 字体子集导出的 PDF，汉字被写成 CJK 部首区
（U+2E80–U+2FDF，康熙部首）的码位——肉眼和汉字一模一样，机器眼里是
两个完全不同的字符。一份 195 页古籍里实测 134 种、49027 处：

  · jieba 切不出来，进了 BM25 索引也是怪字符，用户查"气"永远命中不了"⽓"；
  · bge 词表里没有这些码位，一个 512 维向量被几十个 UNK 摊薄，
    语义相似度整体往下掉。

两条路一起坏，表现出来就是"召回率低"，而且看日志一点异常都没有——
每步都成功，只是每一步都在拿错的字去比。

修法用 NFKC 逐字符映射：这些码位在 Unicode 里本就是同一个字的兼容形式，
NFKC 会把"⽓"折叠成"气"（实测 134 种全部可折叠，折完剩 0）。

刻意**只折叠该折的**：全角中文标点（，。！？）保持原样。NFKC 会把它们
变成 ASCII 逗号句号，正文显示会变丑，而它们对检索没有任何害处——
两侧（文档/查询）都是同一个全角标点，匹配天然一致。

同一个函数必须用在**入库和查询两端**。只统一一端，等于自己制造新的不一致。
"""

from __future__ import annotations

import unicodedata

# 需要折叠的码位区间。NFKC 对区间内字符做兼容分解，区间外一个字都不碰。
_FOLD_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2FDF),   # CJK 部首补充 + 康熙部首 ← 主要凶手
    (0xF900, 0xFAFF),   # CJK 兼容汉字（NFKC 映射回标准汉字）
    (0xFF10, 0xFF19),   # 全角数字 ０-９
    (0xFF21, 0xFF3A),   # 全角大写 Ａ-Ｚ
    (0xFF41, 0xFF5A),   # 全角小写 ａ-ｚ
)

# 零宽与格式控制字符：不可见，但会切断分词、让"完全相同"的两段文本不相等
_DELETE_CODEPOINTS: tuple[int, ...] = (
    0x00AD,             # 软连字符
    0x200B, 0x200C, 0x200D, 0x200E, 0x200F,   # 零宽/方向标记
    0x2060, 0xFEFF,     # 词连接符、BOM
)


def _build_table() -> dict[int, str | None]:
    """预计算映射表。translate 是 C 实现，比逐字符判断快一个量级。"""
    table: dict[int, str | None] = {}
    for lo, hi in _FOLD_RANGES:
        for cp in range(lo, hi + 1):
            src = chr(cp)
            dst = unicodedata.normalize("NFKC", src)
            if dst != src:
                table[cp] = dst
    for cp in _DELETE_CODEPOINTS:
        table[cp] = None          # None = 删除
    return table


_TABLE = _build_table()


def fold(text: str) -> str:
    """折叠兼容码位并删除零宽字符。幂等：折叠过的文本再折一次不变。"""
    if not text:
        return text
    return text.translate(_TABLE)


def fold_query(text: str) -> str:
    """查询侧。名字单独取一个，是为了在代码里一眼看出"两端都折过"。"""
    return fold(text)


def needs_fold(text: str) -> bool:
    """有没有该折的字。给"要不要提示用户重索引"这类判断用。"""
    return any(ord(ch) in _TABLE for ch in text)
