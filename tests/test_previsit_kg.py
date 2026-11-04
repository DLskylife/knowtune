"""知识图谱追问（系统回顾）的确定性与降级测试。

锁三件事：
1. 图谱能给出"该问什么"，且排序有依据（覆盖率），不是词表顺序；
2. 图谱不在/关掉时，问诊流程**完全不变**——它是增强，不是依赖；
3. 患者回答"有/没有"会被记进病历，且问多少条有上限。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowtune.config import load_config
from knowtune.generate.llm import LLMClient
from knowtune.previsit import KnowtuneLLMAdapter
from knowtune.previsit.config import load_previsit
from previsit import kgdb
from previsit.engine import PrevisitEngine
from previsit.store import Store

KG_DB = Path("data/previsit/kg.db")
HAS_KG = KG_DB.exists()

needs_kg = pytest.mark.skipif(not HAS_KG, reason="先跑 scripts/build_previsit_kg.py 建库")


class TestKgdb:
    @needs_kg
    def test_cough_asks_the_right_followups(self):
        kg = kgdb.KG(KG_DB)
        assert kg.ok, kg.error
        cands = kg.ros(["咳嗽"], limit=10)
        names = [c["symptom"] for c in cands]
        assert "呼吸困难" in names[:5]
        assert "咯血" in names[:10]
        # 覆盖率必须有依据：排第一的至少覆盖 10% 的含咳嗽疾病
        assert cands[0]["ratio"] >= 0.10
        assert cands[0]["base"] > 20

    @needs_kg
    def test_exclude_known_symptoms(self):
        kg = kgdb.KG(KG_DB)
        seeds = ["腹痛"]
        first = kg.ros(seeds, limit=3)[0]["symptom"]
        after = kg.ros(seeds, exclude=[first], limit=3)
        assert first not in [c["symptom"] for c in after]

    @needs_kg
    def test_probes_normalise_colloquial(self):
        kg = kgdb.KG(KG_DB)
        assert "咳嗽" in kg.probes("这两天咳嗽得很厉害", limit=5)

    def test_missing_db_degrades_silently(self, tmp_path):
        """库不在 = 功能不存在，绝不能抛异常把问诊带崩。"""
        kg = kgdb.KG(tmp_path / "nope.db")
        assert kg.ok is False
        assert kg.ros(["咳嗽"], limit=3) == []
        assert kg.dept_votes(["咳嗽"]) == []
        assert kg.probes("咳嗽") == []

    @needs_kg
    def test_dept_votes(self):
        kg = kgdb.KG(KG_DB)
        votes = kg.dept_votes(["咯血", "咳嗽"], limit=3)
        assert votes and votes[0]["dept"]
        assert votes[0]["ratio"] >= votes[-1]["ratio"]


def _engine(tmp_path: Path, *, kg: bool = True) -> PrevisitEngine:
    cfg = load_config()
    pv = load_previsit()
    pv["kg"] = dict(pv.get("kg") or {})
    pv["kg"]["enabled"] = kg
    # 追问/抽取一律走规则兜底：测试不该等一个本地大模型，也更接近线上
    # "模型挂了"的真实降级路径。
    pv["timeout_s"] = 0
    # 关掉语义召回：这里测的是图谱追问，槽位顺序要稳定
    pv["retrieval"] = {"enabled": False}
    store = Store(tmp_path / "previsit.db")
    return PrevisitEngine(pv, store, KnowtuneLLMAdapter(LLMClient(cfg)))


ANSWERS = ["三天了", "有痰，白色的", "没有", "有一点", "不清楚", "没有过", "有"]


def _drive(eng: PrevisitEngine, complaint: str, max_steps: int = 14):
    """一路问答走到底，返回 (问过的所有句子, 最终 session)。"""
    v = eng.start(complaint, {"age": "35", "gender": "男"})
    sid = v["session_id"]
    questions = [v.get("question") or ""]
    for i in range(max_steps):
        if v["state"] in ("done", "emergency"):
            break
        v = eng.reply(sid, ANSWERS[i % len(ANSWERS)])
        questions.append(v.get("question") or "")
    sess = eng.store.get(sid)
    return questions, sess


class TestRosFlow:
    @needs_kg
    def test_ros_asked_after_slots_and_recorded(self, tmp_path):
        eng = _engine(tmp_path)
        questions, sess = _drive(eng, "咳嗽，已经三天了")
        ros_q = [q for q in questions if q.startswith("再确认一下")]
        assert ros_q, "槽位问齐后应该从图谱补系统回顾问题"
        assert len(ros_q) <= int(eng.ros_max or 0) + 1
        ros = json.loads(sess["ros"] or "{}") if isinstance(sess["ros"], str) \
            else (sess["ros"] or {})
        assert ros.get("asked"), "问过的问题要留痕，否则同一句会重复问"
        assert len(ros["asked"]) <= eng.ros_max
        if ros.get("answers"):
            record = sess["record"]
            text = record["text"] if isinstance(record, dict) else str(record)
            assert "系统回顾" in text

    @needs_kg
    def test_no_duplicate_ros_question(self, tmp_path):
        eng = _engine(tmp_path)
        questions, sess = _drive(eng, "肚子疼两天了")
        ros_q = [q for q in questions if q.startswith("再确认一下")]
        assert len(ros_q) == len(set(ros_q)), "同一个伴随症状不该问两遍"

    def test_flow_unchanged_when_kg_off(self, tmp_path):
        """关掉图谱：问诊照常收网，且一句图谱追问都不出现。"""
        eng = _engine(tmp_path, kg=False)
        assert eng.kg.ok is False
        questions, sess = _drive(eng, "咳嗽，已经三天了")
        assert sess["state"] == "done"
        assert not [q for q in questions if q.startswith("再确认一下")]

    def test_flow_unchanged_when_db_missing(self, tmp_path):
        eng = _engine(tmp_path)
        eng.kg = kgdb.KG(tmp_path / "nope.db")
        eng.kg_cfg = {"enabled": True, "db": str(tmp_path / "nope.db"), "ros_max": 3}
        questions, sess = _drive(eng, "头痛两天")
        assert sess["state"] in ("done", "emergency")
        assert not [q for q in questions if q.startswith("再确认一下")]
