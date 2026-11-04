"""同义归一、疾病锁定、人话截断、线索不越界。

这四件事各自都有"看起来对、用起来错"的坑，逐条锁住：

1. 归一落点必须落在症状词表里（`发烧`→`发热` 但词表只有`发烧`，照搬会让
   下游一条疾病都查不到）；
2. 别名区间不能重叠（`肚子疼` 里含 `肚子`，两个都归一会凭空多出一个`腹胀`）；
3. 危险症状禁止被改写（`吐血` 在 sameas 里同时挂 `呕血` 和 `便血`，改错
   方向等于把上消化道出血指到下消化道）；
4. 线索（候选病、科室倾向）**绝不能**出现在给患者看的病历正文里。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from previsit import kgdb
from previsit.config import load_previsit
from previsit.engine import PrevisitEngine
from previsit.store import Store

DB = Path(kgdb.DEFAULT_DB)
HAVE_KG = DB.exists()
needs_kg = pytest.mark.skipif(not HAVE_KG, reason="需要 data/previsit/kg.db（跑 scripts/build_previsit_kg.py）")


@pytest.fixture(scope="module")
def kg():
    k = kgdb.KG(DB, enabled=True)
    if not k.ok:
        pytest.skip(k.error or "图谱库打不开")
    return k


@needs_kg
class TestNormalize:
    def test_colloquial_to_standard(self, kg):
        """患者的口语要落到词表里的标准词上。"""
        got = {n["std"] for n in kg.normalize("我肚子疼，还拉稀")}
        assert "腹痛" in got
        assert "腹泻" in got

    def test_landing_must_be_in_vocab(self, kg):
        """归一落点必须在症状词表里，否则下游查不到任何疾病。"""
        vocab = kg.symptom_vocab()
        for text in ("孩子发烧39度", "胸口闷得慌", "喘不上气"):
            for n in kg.normalize(text):
                assert n["std"] in vocab, f"{text} → {n['std']} 不在词表"

    def test_no_overlapping_spans(self, kg):
        """`肚子疼` 占掉的区间不能再被 `肚子` 归一一次。"""
        got = kg.normalize("我肚子疼")
        spans = [(n["start"], n["end"]) for n in got]
        for i, (s1, e1) in enumerate(spans):
            for s2, e2 in spans[i + 1:]:
                assert e1 <= s2 or s1 >= e2, f"区间重叠：{got}"

    def test_dangerous_symptom_not_rewritten(self, kg):
        """`吐血` 不许被改写成 `便血`——方向错了就是安全问题。"""
        stds = {n["std"] for n in kg.normalize("我突然吐血了")}
        assert "便血" not in stds

    def test_empty_and_noise_safe(self, kg):
        assert kg.normalize("") == []
        assert kg.normalize("嗯嗯啊啊") == []


@needs_kg
class TestDiseaseLock:
    def test_requires_confirmed_symptoms(self, kg):
        """归一后的症状能锁定候选病，且带科室与命中数。"""
        syms = [n["std"] for n in kg.normalize("肚子疼拉稀")]
        cands = kg.candidate_diseases(syms, limit=5)
        assert cands, "腹痛+腹泻应该能锁出候选病"
        for c in cands:
            assert c["hits"] >= 2
            assert "dept" in c and "coverage" in c

    def test_rare_symptom_does_not_dominate(self, kg):
        """IDF 必须封顶：否则只有 25 个病携带的罕见症状会把罕见病顶到第一。"""
        syms = [n["std"] for n in kg.normalize("肚子疼拉稀")]
        cands = kg.candidate_diseases(syms, limit=3)
        assert cands
        # 命中的症状越常见，越应该是消化系统方向，而不是肿瘤外科那种
        assert any("消化" in str(c["dept"]) or "普外" in str(c["dept"]) for c in cands)

    def test_empty_input_safe(self, kg):
        assert kg.candidate_diseases([]) == []
        assert kg.candidate_diseases(["不存在的症状xyz"]) == []


class TestFirstQuestion:
    @pytest.mark.parametrize("raw,want", [
        ("好的。那您肚子疼是偏上腹还是偏下腹？", "好的。那您肚子疼是偏上腹还是偏下腹？"),
        ("疼了几天？有没有发烧？有没有吐？", "疼了几天？"),
        ("先说一句没有问号的", "先说一句没有问号的"),
        ("", ""),
    ])
    def test_truncate_at_first_question_mark(self, raw, want):
        """模型爱连珠炮，代码强切第一个问号。"""
        assert PrevisitEngine._first_question(raw) == want


def _engine(tmp_path: Path, *, kg: bool = True) -> PrevisitEngine:
    pv = load_previsit()
    pv["timeout_s"] = 0
    pv["retrieval"] = {"enabled": False}       # 不碰网络，测试要确定性
    pv["kg"] = dict(pv.get("kg") or {})
    pv["kg"]["enabled"] = kg
    return PrevisitEngine(pv, Store(tmp_path / "previsit.db"), None)


@needs_kg
class TestCluesStayInternal:
    def test_clues_not_in_patient_record(self, tmp_path):
        """候选病、科室倾向只出现在 `clues` 字段，病本文本里不能有。"""
        eng = _engine(tmp_path)
        v = eng.start("肚子疼拉稀两天了", {"age": "30", "gender": "男"})
        sid = v["session_id"]
        for _ in range(14):
            if v["state"] in ("done", "emergency"):
                break
            v = eng.reply(sid, "没有")
        assert v["state"] == "done"
        rec = v["record"]
        assert "可能的" not in rec["text"]
        assert "不构成诊断" not in rec["text"] or "仅用于就诊前信息汇总" in rec["text"]
        # 线索本身该有内容（症状归一出来了）
        assert rec.get("clues")

    def test_conditions_split_from_symptoms(self, tmp_path):
        """`高血压` 既是病名又被挂在症状位上，必须分到既往病那栏。"""
        eng = _engine(tmp_path)
        v = eng.start("咳嗽三天了，有高血压", {"age": "60", "gender": "男"})
        dx = v.get("dx") or {}
        assert "高血压" in (dx.get("conditions") or []), dx
        assert "高血压" not in (dx.get("symptoms") or []), dx


@needs_kg
class TestRedFlagNotSwallowed:
    def test_chief_companion_survives(self, tmp_path):
        """主诉里的伴随症状必须留在 companions 槽位。

        曾经按"这个词在不在整段主诉里"排除伴随症状，患者说"胸口压着疼，
        还出汗"时"出汗"被吃掉，ACS 红旗征规则匹配不到，分诊从"尽快就诊"
        掉成"门诊就诊"。这是安全问题，锁死。
        """
        eng = _engine(tmp_path)
        v = eng.start("胸口压着疼，还出汗", {"age": "52", "gender": "男"})
        assert "出汗" in (v["slots"].get("companions") or []), v["slots"]

    def test_acs_triage_urgency(self, tmp_path):
        """胸痛 + 出汗 + 压榨样 → 必须给"尽快就诊"，不能是门诊。"""
        eng = _engine(tmp_path)
        v = eng.start("胸口压着疼，还出汗", {"age": "52", "gender": "男"})
        sid = v["session_id"]
        v = eng.reply(sid, "像石头压着")
        for _ in range(14):
            if v["state"] in ("done", "emergency"):
                break
            v = eng.reply(sid, "没有")
        assert v["state"] == "done"
        assert "尽快" in str(v.get("urgency") or ""), v.get("urgency")


@needs_kg
class TestRedFlagNotSwallowed:
    def test_chief_companion_survives(self, tmp_path):
        """主诉里的伴随症状必须留在 companions 槽位。

        曾经按"这个词在不在整段主诉里"排除伴随症状，患者说"胸口压着疼，
        还出汗"时"出汗"被吃掉，ACS 红旗征规则匹配不到，分诊从"尽快就诊"
        掉成"门诊就诊"。这是安全问题，锁死。
        """
        eng = _engine(tmp_path)
        v = eng.start("胸口压着疼，还出汗", {"age": "52", "gender": "男"})
        assert "出汗" in (v["slots"].get("companions") or []), v["slots"]

    def test_acs_triage_urgency(self, tmp_path):
        """胸痛 + 出汗 + 压榨样 → 必须给"尽快就诊"，不能是门诊。"""
        eng = _engine(tmp_path)
        v = eng.start("胸口压着疼，还出汗", {"age": "52", "gender": "男"})
        sid = v["session_id"]
        v = eng.reply(sid, "像石头压着")
        for _ in range(14):
            if v["state"] in ("done", "emergency"):
                break
            v = eng.reply(sid, "没有")
        assert v["state"] == "done"
        assert "尽快" in str(v.get("urgency") or ""), v.get("urgency")
