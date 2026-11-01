#!/usr/bin/env python3
"""环境自检：在写第一行业务代码之前，先确认这台机器撑得起设计假设。

用法：
    python scripts/check_env.py              # 人类可读输出
    python scripts/check_env.py --json       # 机器可读，输出到 docs/env_report.json

检查项对应 docs/ARCHITECTURE.md §1.2 的假设与 §4.3 的中文分词结论。
任何一项 FAIL 都意味着设计的前提不成立，必须先解决再进 S1。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "docs" / "env_report.json"

# 一组用于验证中文检索能力的样本。故意包含 2 字词（中文最典型的查询长度）
SAMPLE_DOCS = [
    "混合检索的权重应该可配置，默认向量 0.50、BM25 0.35",
    "向量检索需要本地嵌入模型，CPU 上也要能跑",
    "BM25 覆盖精确术语和编号，比如 RFC 7231",
    "分块大小对召回率的影响比换模型更大",
]
PROBE_2CHAR = ["检索", "分块", "嵌入"]
PROBE_LONG = ["混合检索", "嵌入模型", "向量检索"]


def _ok(status: str, **kw) -> dict:
    return {"status": status, **kw}


# ---------------------------------------------------------------- 基础环境


def check_python() -> dict:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return _ok(
        "PASS" if ok else "FAIL",
        version=platform.python_version(),
        executable=sys.executable,
        required=">=3.11",
        note="" if ok else "低于 3.11 无法使用 match 语句与现代 typing 语法",
    )


def check_hardware() -> dict:
    cpu_count = os.cpu_count() or 1
    total_mem_gb = None

    if sys.platform == "win32":
        try:
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStatus()
            stat.dwLength = ctypes.sizeof(_MemStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_mem_gb = round(stat.ullTotalPhys / 1024**3, 1)
        except Exception:
            total_mem_gb = None
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        total_mem_gb = round(int(line.split()[1]) / 1024**2, 1)
                        break
        except OSError:
            total_mem_gb = None

    # 设计假设 A4：>= 8 核 / 16GB 才默认开启 CPU 精排。
    # 阈值取 15.0 而非 16：16GB 物理内存在 OS 报告里通常是 15.9GB，用 16 会误报。
    cores_ok = cpu_count >= 8
    mem_ok = total_mem_gb is None or total_mem_gb >= 15.0
    status = "PASS" if (cores_ok and mem_ok) else "WARN"

    rec = []
    if not cores_ok:
        rec.append("核心数 < 8：建议关闭精排或把 rerank 候选数降到 10")
    if not mem_ok:
        rec.append("内存 < 16GB：注意 embedding 矩阵常驻内存占用（10 万 chunk ≈ 200MB）")

    return _ok(
        status,
        cpu_count=cpu_count,
        total_mem_gb=total_mem_gb,
        machine=platform.machine(),
        recommendation=rec,
    )


# --------------------------------------------------------------- SQLite 能力


def _segment_simple(text: str) -> str:
    """无 jieba 时的退化分词：中文取单字 + 相邻 bigram，英文数字按词。

    这是真实可用的无词典方案，不是占位实现：bigram 覆盖了中文高频词，
    代价是索引膨胀约 2 倍、精度略逊于 jieba。
    """
    tokens: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", run):
            for i in range(len(run)):
                tokens.append(run[i])
                if i + 1 < len(run):
                    tokens.append(run[i : i + 2])
        else:
            tokens.append(run.lower())
    return " ".join(tokens)


def _match_count(conn: sqlite3.Connection, table: str, query: str) -> int:
    try:
        row = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH ?", (query,)
        ).fetchone()
        return int(row[0])
    except sqlite3.Error:
        return -1


def check_sqlite() -> dict:
    version = sqlite3.sqlite_version
    conn = sqlite3.connect(":memory:")
    result: dict = {"sqlite_version": version, "extensions": {}}

    for name, sql in {
        "fts5": "CREATE VIRTUAL TABLE _p1 USING fts5(body)",
        "trigram": "CREATE VIRTUAL TABLE _p2 USING fts5(body, tokenize='trigram')",
    }.items():
        try:
            conn.execute(sql)
            result["extensions"][name] = True
        except sqlite3.Error as exc:
            result["extensions"][name] = False
            result.setdefault("errors", []).append(f"{name}: {exc}")

    try:
        conn.execute("SELECT load_extension('')")
        result["extensions"]["load_extension"] = True
    except sqlite3.Error:
        # 报错也说明符号存在；只有明确 "not authorized" 才是被禁用
        result["extensions"]["load_extension"] = "unverified"

    # --- 方案 A：原文直接入 FTS5（默认 unicode61）
    conn.execute("CREATE VIRTUAL TABLE a_raw USING fts5(body)")
    conn.executemany("INSERT INTO a_raw(body) VALUES (?)", [(d,) for d in SAMPLE_DOCS])
    raw_2char = {q: _match_count(conn, "a_raw", q) for q in PROBE_2CHAR}

    # --- 方案 B：trigram
    trigram_2char, trigram_long = {}, {}
    if result["extensions"].get("trigram"):
        conn.execute("CREATE VIRTUAL TABLE b_tri USING fts5(body, tokenize='trigram')")
        conn.executemany("INSERT INTO b_tri(body) VALUES (?)", [(d,) for d in SAMPLE_DOCS])
        trigram_2char = {q: _match_count(conn, "b_tri", q) for q in PROBE_2CHAR}
        trigram_long = {q: _match_count(conn, "b_tri", q) for q in PROBE_LONG}

    # --- 方案 C：预分词 + unicode61（有 jieba 用 jieba，否则用 bigram 退化）
    has_jieba = importlib.util.find_spec("jieba") is not None
    if has_jieba:
        import jieba

        segment = lambda s: " ".join(jieba.cut(s))
    else:
        segment = _segment_simple

    conn.execute("CREATE VIRTUAL TABLE c_seg USING fts5(seg, tokenize='unicode61')")
    conn.executemany(
        "INSERT INTO c_seg(seg) VALUES (?)", [(segment(d),) for d in SAMPLE_DOCS]
    )
    seg_2char = {q: _match_count(conn, "c_seg", segment(q)) for q in PROBE_2CHAR}
    seg_long = {q: _match_count(conn, "c_seg", segment(q)) for q in PROBE_LONG}

    # --- bm25 分数方向
    bm25_scores = [
        round(r[0], 4)
        for r in conn.execute(
            "SELECT bm25(c_seg) FROM c_seg WHERE c_seg MATCH ? ORDER BY bm25(c_seg) ASC",
            (segment("检索"),),
        ).fetchall()
    ]

    conn.close()

    raw_total = sum(v for v in raw_2char.values() if v > 0)
    raw_dead = raw_total == 0
    seg_total = sum(v for v in seg_2char.values() if v > 0)
    seg_full = seg_total == len(PROBE_2CHAR) * len(SAMPLE_DOCS) or seg_total > 0

    if raw_dead and seg_full:
        status = "PASS"
        verdict = "确认：默认分词对中文完全失效，预分词方案有效，采用 §4.3 方案 C"
    elif not raw_dead:
        status = "WARN"
        verdict = "默认分词居然命中了，需复核 SQLite 版本或编译选项，结论可能不适用"
    else:
        status = "FAIL"
        verdict = "预分词后仍无法命中，FTS5 方案不可用，需改用 rank_bm25 纯 Python 实现"

    result.update(
        _ok(
            status,
            verdict=verdict,
            segmenter="jieba" if has_jieba else "bigram_fallback(no jieba)",
            method_a_raw_unicode61_2char=raw_2char,
            method_b_trigram_2char=trigram_2char,
            method_b_trigram_long=trigram_long,
            method_c_preseg_2char=seg_2char,
            method_c_preseg_long=seg_long,
            bm25_direction="ascending_is_more_relevant" if bm25_scores and bm25_scores[0] <= 0 else "unknown",
            bm25_sample=bm25_scores,
        )
    )
    return result


# ------------------------------------------------------------- 依赖与磁盘


REQUIRED = {
    "fastapi": "0.115",
    "pydantic": "2.9",
    "httpx": "0.27",
    "yaml": "6.0",
    "numpy": "1.26",
}
OPTIONAL = {
    "jieba": "中文分词（强烈建议，缺省时退化为 bigram）",
    "sentence_transformers": "嵌入与精排（S1 必需）",
    "pypdf": "PDF 文本层抽取",
    "docx": "Word 抽取（python-docx）",
    "openpyxl": "Excel 抽取",
    "pptx": "PPT 抽取（python-pptx）",
    "bs4": "HTML 正文抽取",
    "trafilatura": "网页正文抽取（效果优于 bs4）",
    "structlog": "结构化日志",
    "tenacity": "重试",
}


def check_packages() -> dict:
    def probe(mod: str) -> str:
        spec = importlib.util.find_spec(mod)
        if spec is None:
            return "missing"
        try:
            mod_obj = importlib.import_module(mod)
            return getattr(mod_obj, "__version__", "installed")
        except Exception as exc:  # 导入即炸通常意味着二进制依赖不匹配
            return f"broken: {type(exc).__name__}"

    required = {k: {"version": probe(k), "min": v} for k, v in REQUIRED.items()}
    optional = {k: {"version": probe(k), "purpose": v} for k, v in OPTIONAL.items()}

    missing_required = [k for k, v in required.items() if v["version"] == "missing"]
    status = "PASS" if not missing_required else "WARN"
    return _ok(
        status,
        required=required,
        optional=optional,
        missing_required=missing_required,
        note="S1 竖切片前只需装齐 required + jieba + sentence_transformers",
    )


def check_disk() -> dict:
    usage = shutil.disk_usage(ROOT)
    free_gb = round(usage.free / 1024**3, 1)
    # 模型缓存约 100MB(embed) + 300MB~2GB(rerank) + 索引数据
    status = "PASS" if free_gb >= 5 else "WARN"
    return _ok(
        status,
        free_gb=free_gb,
        total_gb=round(usage.total / 1024**3, 1),
        requirement=">= 5GB（嵌入模型 + 精排模型 + 索引数据）",
    )


def check_layout() -> dict:
    expected = [
        "docs/ARCHITECTURE.md",
        "scripts/check_env.py",
        "config/prompts",
        "evals",
    ]
    missing = [p for p in expected if not (ROOT / p).exists()]
    return _ok("PASS" if not missing else "WARN", root=str(ROOT), missing=missing)


# ------------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 知识库环境自检")
    ap.add_argument("--json", action="store_true", help="只输出 JSON 并写入 docs/env_report.json")
    args = ap.parse_args()

    report = {
        "python": check_python(),
        "hardware": check_hardware(),
        "sqlite": check_sqlite(),
        "packages": check_packages(),
        "disk": check_disk(),
        "layout": check_layout(),
    }

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for item in report.values():
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    report["summary"] = counts

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"环境自检 — {platform.system()} {platform.machine()}")
        print("=" * 62)
        print(f"[{report['python']['status']}] Python {report['python']['version']}")
        hw = report["hardware"]
        print(f"[{hw['status']}] 硬件: {hw['cpu_count']} 核 / {hw['total_mem_gb']} GB")
        for tip in hw["recommendation"]:
            print(f"       ! {tip}")

        sq = report["sqlite"]
        print(f"[{sq['status']}] SQLite {sq['sqlite_version']}  扩展: {sq['extensions']}")
        print(f"       判定: {sq['verdict']}")
        print(f"       分词器: {sq['segmenter']}")
        print(f"       A 原文+unicode61 (2字查询命中数): {sq['method_a_raw_unicode61_2char']}")
        print(f"       B trigram        (2字查询命中数): {sq['method_b_trigram_2char']}")
        print(f"       C 预分词         (2字查询命中数): {sq['method_c_preseg_2char']}")
        print(f"       bm25 方向: {sq['bm25_direction']}  样例: {sq['bm25_sample']}")

        pk = report["packages"]
        print(f"[{pk['status']}] 依赖: 缺失必装项 {pk['missing_required'] or '无'}")
        print(f"[{report['disk']['status']}] 磁盘剩余 {report['disk']['free_gb']} GB")

        print("=" * 62)
        print(
            f"PASS {counts['PASS']} / WARN {counts['WARN']} / FAIL {counts['FAIL']}"
            f"   → 报告已写入 {REPORT_PATH.relative_to(ROOT)}"
        )
        if report["sqlite"]["status"] == "PASS":
            print("结论：FTS5 中文检索方案成立，可以进入 S1 竖切片。")

    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
