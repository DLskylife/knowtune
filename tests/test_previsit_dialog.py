"""预问诊对话行为的回归测试：否定快答、不知道跳过、过敏史/既往史必问。

背景（2026-11-03 用户实测反馈）：患者对"还有没有其他不舒服"回答"没有"，
状态机只会再问一遍"能再具体说说吗"，多耗一轮、病历还留不下"无"；过敏史、
既往史根本不在任何域的槽位里，病历不充分。这组测试锁住修复后的行为。
"""

from __future__ import annotations

from pathlib import Path

from knowtune.config import load_config
from knowtune.generate.llm import LLMClient
from knowtune.previsit import KnowtuneLLMAdapter
from knowtune.previsit.config import load_previsit
from previsit.engine import (
    PrevisitEngine,
    _explicit_no,
    _explicit_unknown,
    _rule_allergy,
    _rule_past,
)
from previsit.store import Store


def _engine(tmp_path: Path, *, kg: bool = False) -> PrevisitEngine:
    cfg = load_config()
    pv = load_previsit()
    pv["kg"] = dict(pv.get("kg") or {})
    pv["kg"]["enabled"] = kg
    pv["timeout_s"] = 0          # 一律走规则兜底，测试不等大模型
    # 语义召回关掉：这组测试锁的是"槽位按配置顺序问"，召回会改顺序（那是
    # test_previsit_retrieval.py 的事）。
    pv["retrieval"] = {"enabled": False}
    store = Store(tmp_path / "previsit.db")
    return PrevisitEngine(pv, store, KnowtuneLLMAdapter(LLMClient(cfg)))


class TestFastAnswerUnits:
    def test_explicit_no_variants(self):
        for t in ("没有", "没了", "无", "没有过", "没有过敏", "不过敏",
                  "没有其他不舒服", "没什么", "都没有了"):
            assert _explicit_no(t), t
        for t in ("不过有点恶心", "没有，就是有点发烧", "四天了", "上腹疼",
                  "不知道有没有咯血"):
            assert not _explicit_no(t), t

    def test_explicit_unknown_variants(self):
        for t in ("不清楚", "不知道", "记不清", "没注意"):
            assert _explicit_unknown(t), t
        assert not _explicit_unknown("没有")
        assert not _explicit_unknown("三天了")

    def test_allergy_extractor(self):
        assert _rule_allergy("我对青霉素过敏") == "对青霉素过敏"
        assert _rule_allergy("海鲜过敏，吃了起疹子") == "海鲜过敏"
        assert _rule_allergy("没有过敏") is None
        assert _rule_allergy("不过敏") is None

    def test_past_extractor(self):
        assert _rule_past("有高血压和糖尿病") == "高血压、糖尿病"
        assert _rule_past("做过阑尾炎手术") == "手术"
        assert _rule_past("没有高血压") is None
        assert _rule_past("身体一直很好") is None


class TestDialogBehavior:
    def test_denial_records_none_and_stops_asking(self, tmp_path):
        """答"没有"= 明确阴性：记"无"、同一槽位不再问第二遍。"""
        eng = _engine(tmp_path)
        v = eng.start("咳嗽三天了，有痰", {"age": "35", "gender": "男"})
        sid = v["session_id"]
        # 咳嗽域槽位序：duration→nature→trigger→companions→history→allergy→past
        # 主诉已填 duration/nature，第一问是 trigger，第二问才是伴随症状。
        v = eng.reply(sid, "晚上咳得厉害")            # trigger
        v = eng.reply(sid, "没有")                    # companions：明确否定
        assert v["slots"]["companions"] == ["无"]
        q_after = v.get("question") or ""
        # 接下来的问题不该再围绕"其他不舒服"打转
        for _ in range(2):
            v = eng.reply(sid, "没有")
        questions = [t["content"] for t in eng.store.get(sid)["turns"]
                     if t["role"] == "assistant"]
        comp_qs = [q for q in questions
                   if "有没有发烧" in q or "其他不舒服" in q]
        assert len(comp_qs) == 1, f"伴随症状被问了多遍：{comp_qs}"

    def test_unknown_skips_without_none(self, tmp_path):
        """答"不清楚"：不是阴性，不记"无"，但也不再重复追问。"""
        eng = _engine(tmp_path)
        v = eng.start("咳嗽三天了，有痰", {"age": "35", "gender": "男"})
        sid = v["session_id"]
        v = eng.reply(sid, "晚上咳得厉害")
        v = eng.reply(sid, "不清楚")
        # "不清楚"不是阴性：伴随症状槽位保持未采集，不记"无"
        assert not v["slots"].get("companions")
        # 关键断言：这个槽位不会再被追问
        v = eng.reply(sid, "没有")
        v = eng.reply(sid, "没有")
        questions = [t["content"] for t in eng.store.get(sid)["turns"]
                     if t["role"] == "assistant"]
        comp_qs = [q for q in questions
                   if "有没有发烧" in q or "其他不舒服" in q]
        assert len(comp_qs) == 1

    def test_denial_with_content_uses_extractor(self, tmp_path):
        """"没有，就是有点恶心"：伴随症状要抽到"恶心"，绝不能记成"无"。"""
        eng = _engine(tmp_path)
        v = eng.start("咳嗽三天了，有痰", {"age": "35", "gender": "男"})
        sid = v["session_id"]
        v = eng.reply(sid, "晚上咳得厉害")
        v = eng.reply(sid, "没有，就是有点恶心")
        assert v["slots"]["companions"] == ["恶心"]

    def test_allergy_and_past_asked_in_every_domain(self, tmp_path):
        """过敏史、既往史不受域管辖：咳嗽的人也必须被问到。"""
        eng = _engine(tmp_path)
        v = eng.start("咳嗽三天了，有痰", {"age": "35", "gender": "男"})
        sid = v["session_id"]
        seen = {"allergy": False, "past": False}
        for _ in range(12):
            if v["state"] in ("done", "emergency"):
                break
            q = v.get("question") or ""
            if "过敏" in q and not seen["allergy"]:
                seen["allergy"] = True
                v = eng.reply(sid, "对青霉素过敏")
                continue
            if ("慢性病" in q or "高血压" in q) and not seen["past"]:
                seen["past"] = True
                v = eng.reply(sid, "有高血压和糖尿病")
                continue
            v = eng.reply(sid, "没有")
        assert seen["allergy"], "过敏史没被问到"
        assert seen["past"], "既往病史没被问到"
        record = eng.store.get(sid)["record"]
        text = record["text"] if isinstance(record, dict) else str(record)
        assert "青霉素" in text
        assert "高血压" in text and "糖尿病" in text

    def test_denial_session_ends_within_budget(self, tmp_path):
        """全答"没有"：每个槽位一轮收掉，不再两轮两轮地耗。"""
        eng = _engine(tmp_path)
        v = eng.start("肚子疼", {"age": "35", "gender": "男"})
        sid = v["session_id"]
        n = 0
        while v["state"] not in ("done", "emergency") and n < 20:
            v = eng.reply(sid, "没有")
            n += 1
        assert v["state"] == "done"
        slots = len(v["slot_status"])
        assert n <= slots, f"全盘否定却问了 {n} 轮（槽位共 {slots} 个）"
        record = eng.store.get(sid)["record"]
        text = record["text"] if isinstance(record, dict) else str(record)
        assert "无" in text


class TestBlankChief:
    """表单去掉「这次哪里不舒服」之后的空主诉开场（2026-11-04）。

    那个必填文本域是整个入口最难的一格：要患者在还不知道系统会问什么的时候
    先概括自己的病。现在空着也能开始，主诉由第一句用户消息落成——测试锁住
    "空主诉 → 首句即主诉 → 病历抬头不是未填写"这条链。
    """

    def test_start_without_chief(self, tmp_path):
        eng = _engine(tmp_path)
        v = eng.start("", {"gender": "男", "age": "35"})
        assert v["session_id"]
        assert v["state"] == "intake"
        assert v["turns"] == [], "开场不该自己编一句话，第一句留给患者"
        assert v["chief"] == ""
        # 起始域给的是兜底域，槽位集合先摆出来，说第一句就会换掉
        assert v["domain"] == "general"
        assert v["slot_status"], "右侧档案区不能空着"

    def test_first_reply_becomes_chief(self, tmp_path):
        eng = _engine(tmp_path)
        v = eng.start("", {"gender": "男", "age": "35"})
        sid = v["session_id"]
        v = eng.reply(sid, "肚子疼，一阵一阵的，两天了")
        assert v["chief"] == "肚子疼，一阵一阵的，两天了"
        assert v["domain"] == "abd", "第一句就得判到腹痛域"
        assert v["question"], "判完域要接着问下一句"

    def test_blank_start_record_has_chief(self, tmp_path):
        eng = _engine(tmp_path)
        v = eng.start("", {"gender": "男", "age": "35"})
        sid = v["session_id"]
        v = eng.reply(sid, "肚子疼，一阵一阵的，两天了")
        for _ in range(20):
            if v["state"] in ("done", "emergency"):
                break
            v = eng.reply(sid, "没有")
        assert v["state"] == "done"
        record = eng.store.get(sid)["record"]
        text = record["text"] if isinstance(record, dict) else str(record)
        assert "肚子疼" in text
        assert "未填写" not in text, "首句已经是主诉了，抬头不能还写未填写"
