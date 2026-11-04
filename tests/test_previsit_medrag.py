"""MedRAG 数据源：英文原文 + 校验译文 → 知识库文档。

这套测试锁的是**上游数据的正确性**，不是代码风格。背景：最初灌库用的是整份
xlsx 的机器翻译，逐句和英文原文对拍才发现三类问题——12 个病名在 OpenCMKG
里查不到、8 处句子错译（"没有基础心脏病"译成"也可能发生心脏病"）、行序错乱
且丢 8 行。断言只有一条原则：**这个库里的每个病名、每句话，都必须能追到英文
原文，且中文是校验过的**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from medrag_source import (CATEGORIES, DATA, REL_SECTIONS, build_docs,  # noqa: E402
                           clean, load_en)

ZH = json.loads((DATA / "medrag_zh.json").read_text(encoding="utf-8"))

# 机翻病名里那些查不到、或干脆是英文缩写的。它们一旦回到库里，医生看到的是
# "urti""psvt"，而 OpenCMKG 那边一条记录都没有。
MACHINE_NAMES = ("urti", "psvt", "sle", "可能的nstemi", "集群头痛",
                 "过敏反应", "肺肿瘤", "胰腺肿瘤", "艾滋病毒初始感染",
                 "布尔哈夫综合征")


@pytest.fixture(scope="module")
def docs() -> dict[str, str]:
    return dict(build_docs())


class TestDocs:
    def test_six_system_docs(self, docs):
        assert len(docs) == 6
        assert all(t.startswith("MedRAG · ") for t in docs)

    def test_disease_count(self, docs):
        """63 个 subject − 9 个上级类目 − 5 个下划线重写残留 − 1 个只有问答残渣
        的无症状病（局限性水肿）= 48。"""
        n = sum(t.count("\n## ") for t in docs.values())
        assert n == 48, f"病种数不对：{n}"

    def test_no_machine_translated_name(self, docs):
        """机翻病名一个都不能留。"""
        for text in docs.values():
            for line in text.splitlines():
                if not line.startswith("## "):
                    continue
                name = line[3:].strip()
                for bad in MACHINE_NAMES:
                    assert bad not in name, f"病名仍是机翻：{name}"

    def test_no_latin_disease_name(self, docs):
        """病名不能是英文缩写（HIV/COPD 这类国际通用缩写除外）。"""
        for text in docs.values():
            for line in text.splitlines():
                if not line.startswith("## "):
                    continue
                name = line[3:].strip()
                latin = "".join(ch for ch in name if ch.isascii() and ch.isalpha())
                assert latin in ("", "HIV"), f"病名带英文：{name}"

    def test_each_disease_has_symptom_line(self, docs):
        """一块 = 一个病，每块必须带"表现："，否则命中后没有可追问的内容。"""
        for title, text in docs.items():
            cur, body = "", []
            for line in text.splitlines() + ["## <end>"]:
                if line.startswith("## "):
                    if cur:
                        assert any(x.startswith("表现：") for x in body), \
                            f"{title} / {cur} 没有症状描述"
                    cur, body = line[3:].strip(), []
                else:
                    body.append(line.strip())

    def test_sections_are_not_indexed(self, docs):
        """病史线索/危险因素不进库：它们不含症状，只会在召回里抢位置。"""
        for text in docs.values():
            assert "病史线索" not in text
            assert "危险因素" not in text

    def test_heading_path_maps_back_to_disease(self, docs):
        """灌库格式必须和 retriever 的解析对齐：heading 末段就是病名。"""
        from previsit.retriever import KIND_DISEASE, _split_parts

        for title, text in docs.items():
            for line in text.splitlines():
                if not line.startswith("## "):
                    continue
                name = line[3:].strip()
                kind, parsed, _ = _split_parts(title, f"{title} > {name}")
                assert (kind, parsed) == (KIND_DISEASE, name)


class TestTranslation:
    def test_every_sentence_translated(self):
        """英文原文里每条描述都必须有译文，缺一条就该灌库失败。"""
        en = load_en()
        missing = []
        for subj, rels in en.items():
            if subj in CATEGORIES:
                continue
            for rel, items in rels.items():
                for it in items:
                    c = clean(it)
                    if c and c not in ZH["text"]:
                        missing.append(c)
        assert not missing, f"{len(missing)} 句没有译文，首条：{missing[:1]}"

    def test_no_residue_in_translation(self):
        """译文里不能混进证据编码问答的残渣或英文原文。"""
        for src, zh in ZH["text"].items():
            assert len(zh) >= 4
            assert "？：" not in zh and "?:" not in zh and "::" not in zh
            assert not zh.startswith("描述"), zh
            assert not zh.strip().endswith("？"), zh

    def test_known_mistranslations_are_fixed(self):
        """踩过的具体错译：改回去就红。"""
        t = ZH["text"]
        # 原文是"往往没有基础心脏病，健康人也可能发生"，机翻多出一个"发生心脏病"
        assert t["Often no underlying heart disease, may occur in healthy individuals"] == \
            "往往没有基础心脏病，健康人也可能发生"
        assert "呼哧" not in t['Severe, hacking cough followed by a "whoop" sound during inspiration']
        assert "鸡鸣样吼声" in t['Severe, hacking cough followed by a "whoop" sound during inspiration']
        # 咽痛/锐痛/乏力 这类标准词，知识库必须统一，否则归一后落到别的症状
        assert t["Sore throat, nasal congestion, and cough"].startswith("咽痛")
        assert "锐性胸痛" in t["Sharp chest pain, often worsened by lying down, and relieved by sitting up"]

    def test_names_all_mapped(self):
        """每个非类目 subject 都要有中文名。"""
        en = load_en()
        for subj in en:
            if subj in CATEGORIES:
                continue
            assert subj in ZH["names"], f"{subj} 没有中文名"

    def test_relations_are_the_four_used(self):
        assert [k for k, _, _ in REL_SECTIONS] == [
            "has_symptomatology", "has_exposure", "has_anamnesis", "has_lifestyle"]


class TestKgNameCoverage:
    """病名必须能在 OpenCMKG 里查到科室——查不到就是译文又飘了。"""

    def test_most_names_have_department(self, docs):
        import sqlite3

        db = DATA / "kg.db"
        if not db.exists():
            pytest.skip("kg.db 不在")
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        known = {r[0] for r in con.execute("SELECT disease FROM disease_dept")}
        con.close()
        names = [ln[3:].strip() for t in docs.values()
                 for ln in t.splitlines() if ln.startswith("## ")]
        hit = [n for n in names if n in known]
        assert len(hit) >= 35, f"只有 {len(hit)}/{len(names)} 个病名能查到科室"

    def test_alias_veto_blocks_upstream_noise(self):
        """OpenCMKG 把"喉鸣"当成"耳鸣"的别名，热路径上必须否决。"""
        from previsit.kgdb import KG

        db = DATA / "kg.db"
        if not db.exists():
            pytest.skip("kg.db 不在")
        kg = KG(db)
        assert kg.ok
        std = [n["std"] for n in kg.normalize("突然出现呼吸困难和喉鸣")]
        assert "耳鸣" not in std, f"喉鸣被归一成耳鸣：{std}"
