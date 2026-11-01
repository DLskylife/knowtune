"""核心逻辑的确定性测试：不依赖任何模型下载，CI 里能秒过。

凡是能靠断言锁死的，就不要靠"跑一次看着还行"。
"""

from __future__ import annotations

import pytest

from ragkb.config import ChunkingCfg, defaults_config, load_config
from ragkb.generate.cite import verify, verbatim_ratio
from ragkb.ingest.chunker import chunk_blocks, estimate_tokens
from ragkb.ingest.loader import ExtractError, decode_bytes, load_inline_text
from ragkb.models import Block, Hit
from ragkb.retrieve.engine import fuse_rrf


class _StubChat:
    """site 测试桩：真实 Config 一定有这些字段，轻量桩只补被读到的部分。"""

    history_turns = 5
    history_token_budget = 1500


class _StubEvidence:
    mid_ratio = 0.6


class TestRRF:
    def test_reciprocal_rank_math(self):
        a = [Hit(1, 0.9, 1, "dense"), Hit(2, 0.8, 2, "dense")]
        b = [Hit(2, -1.0, 1, "sparse"), Hit(3, -2.0, 2, "sparse")]
        fused = fuse_rrf([a, b], [0.5, 0.35], k=60, top_k=3)
        by_id = {f["chunk_id"]: f["rrf"] for f in fused}
        assert by_id[2] == pytest.approx(0.5 / 62 + 0.35 / 61)
        assert by_id[1] == pytest.approx(0.5 / 61)
        assert fused[0]["chunk_id"] == 2  # 两路都命中的必须排第一

    def test_weight_sum_not_one_is_fine(self):
        """0.50 + 0.35 = 0.85 不是 bug：整体缩放不改排序。"""
        a = [Hit(1, 0.9, 1, "dense"), Hit(2, 0.8, 2, "dense")]
        b = [Hit(2, -1.0, 1, "sparse")]
        scaled = fuse_rrf([a, b], [0.5, 0.35], k=60, top_k=3)
        normalised = fuse_rrf([a, b], [0.5 / 0.85, 0.35 / 0.85], k=60, top_k=3)
        assert [f["chunk_id"] for f in scaled] == [f["chunk_id"] for f in normalised]

    def test_single_route_still_ranks(self):
        fused = fuse_rrf([[Hit(5, 0.1, 1, "dense")]], [0.5], k=60, top_k=5)
        assert fused[0]["chunk_id"] == 5
        assert fused[0]["sparse_rank"] is None

    def test_rank_starts_from_one(self):
        """rank 从 1 起算；如果实现里从 0 起，k 的实际作用就变了。"""
        fused = fuse_rrf([[Hit(7, 1.0, 1, "dense")]], [1.0], k=60, top_k=1)
        assert fused[0]["rrf"] == pytest.approx(1 / 61)


class TestChunker:
    def test_no_empty_chunks(self):
        blocks = [Block(text="第一段内容" * 20), Block(text=""), Block(text="   "),
                  Block(text="第二段内容" * 20)]
        drafts = chunk_blocks(blocks, ChunkingCfg(size=200, overlap=20, min_chunk=10))
        assert drafts
        assert all(d.text.strip() for d in drafts)

    def test_seq_contiguous(self):
        """seq 必须连续，否则 UNIQUE(doc_id, generation, seq) 会在重新索引时炸。"""
        blocks = [Block(text=f"第{i}段" * 30) for i in range(20)]
        drafts = chunk_blocks(blocks, ChunkingCfg(size=200, overlap=20, min_chunk=10))
        assert [d.seq for d in drafts] == list(range(len(drafts)))

    def test_overlap_starts_at_sentence_boundary(self):
        long_text = "。".join(["这是一句测试内容用于验证重叠边界" for _ in range(40)]) + "。"
        drafts = chunk_blocks([Block(text=long_text)], ChunkingCfg(size=240, overlap=80, min_chunk=10))
        for d in drafts[1:]:
            assert not d.text.startswith("。")
            assert len(d.text) > 0

    def test_oversized_block_is_split(self):
        single = Block(text="无标点长文本" * 200)
        drafts = chunk_blocks([single], ChunkingCfg(size=200, overlap=0, min_chunk=10))
        assert len(drafts) > 1
        assert all(len(d.text) <= 400 for d in drafts)

    def test_heading_propagates_to_all_chunks(self):
        blocks = [Block(text="标题", kind="heading", heading_path="A > B")]
        blocks += [Block(text=f"正文{i}" * 40, heading_path="A > B") for i in range(6)]
        drafts = chunk_blocks(blocks, ChunkingCfg(size=200, overlap=20, min_chunk=10))
        assert all(d.heading_path == "A > B" for d in drafts)


class TestCite:
    def test_invalid_id_stripped(self):
        report = verify("答案在这 [1]，还有这里 [9]。", n_context=3)
        assert report.invalid_ids == [9]
        assert "[9]" not in report.cleaned
        assert "[1]" in report.cleaned

    def test_coverage_all_cited(self):
        text = "混合检索通过 RRF 融合两路结果，权重只表达相对比例 [1]。分块大小对召回的影响比换模型更大 [2]。"
        report = verify(text, n_context=2)
        assert report.coverage == 1.0
        assert report.cited_ids == [1, 2]

    def test_coverage_zero_when_uncited(self):
        text = "混合检索通过 RRF 融合两路结果，权重只表达相对比例。分块大小对召回的影响比换模型更大。"
        report = verify(text, n_context=2)
        # 只有 >=25 字的句子进分母，第二句 18 字不计入——这是有意的门槛，
        # 否则覆盖率会被"是的。""好的。"这类短句稀释成没有意义的数字
        assert report.coverage == 0.0
        assert len(report.uncited_sentences) == 1

    def test_short_sentences_excluded_from_denominator(self):
        text = "是的 [1]。\n\n1. 第一条原因\n2. 第二条原因"
        report = verify(text, n_context=1)
        assert report.coverage >= 0.9

    def test_hedging_with_citations_forces_zero(self):
        """词库外问题的实测翻车：模型答"未找到依据"却挂满编号，覆盖率 100%
        和低置信提醒同时出现。对冲式引用不构成证据，覆盖率必须归零。"""
        text = ("根据现有资料，未找到与“甲信”相关的术语或上下文，"
                "无法提供具体查询内容 [1][2][3][4][5]。")
        report = verify(text, n_context=5)
        assert report.coverage == 0.0
        assert report.blanket and "未找到" in report.blanket

    def test_blanket_run_flagged(self):
        """单句连挂 3+ 个编号 = 笼统引用，标记但不改覆盖率（指向性差，
        不等于没引用，归零会冤枉正常的"综合多来源"）。"""
        text = ("本病在古籍中多有记载，历代医家论述各有侧重 [1][2][3]，"
                "治疗上以清热为主，辅以调和体液 [1]。")
        report = verify(text, n_context=3)
        assert report.blanket and "笼统" in report.blanket
        assert report.coverage > 0

    def test_normal_answer_no_blanket(self):
        text = "用黄连二两，醋浸一宿，加入蜡五合，煎熬成膏，涂敷患处 [1]。"
        report = verify(text, n_context=1)
        assert report.blanket is None
        assert report.coverage == 1.0


class TestVerbatim:
    """照抄检测：小模型把检索块整段搬出来，界面必须能认出这不是答案。"""

    SRC = "甲信突出骨，用黄连二两，醋浸一宿，加入蜡清五合，熬煎成膏，去渣取胶涂上。"

    def test_copy_is_detected(self):
        ratio = verbatim_ratio(self.SRC, [self.SRC])
        assert ratio >= 0.9

    def test_paraphrase_is_not(self):
        ratio = verbatim_ratio("黄连醋浸后与蜡同煎成膏，外敷患处 [1]。", [self.SRC])
        assert ratio < 0.5

    def test_no_sources_means_zero(self):
        assert verbatim_ratio("随便一句回答内容足够长以便进入统计。", []) == 0.0


class TestLoader:
    def test_decode_gbk(self):
        raw = "中文编码测试".encode("gbk")
        text, enc = decode_bytes(raw)
        assert text == "中文编码测试"
        assert enc in {"gb18030", "gbk"}

    def test_unsupported_extension(self, tmp_path):
        from ragkb.ingest.loader import load_path

        p = tmp_path / "x.exe"
        p.write_bytes(b"MZ")
        with pytest.raises(ExtractError, match="不支持的格式"):
            load_path(p)

    def test_empty_file_rejected(self, tmp_path):
        from ragkb.ingest.loader import load_path

        p = tmp_path / "empty.md"
        p.write_text("   \n\n  ", encoding="utf-8")
        with pytest.raises(ExtractError):
            load_path(p)

    def test_markdown_heading_path(self):
        doc = load_inline_text("# 一级\n\n正文一\n\n## 二级\n\n正文二", "t")
        paths = [b.heading_path for b in doc.blocks]
        assert any(p and "一级" in p and "二级" in p for p in paths)


class TestConfig:
    def test_paths_are_absolute(self):
        cfg = load_config()
        assert cfg.storage.db_path.is_absolute()
        assert cfg.eval.dataset.is_absolute()

    def test_unknown_provider_raises(self):
        cfg = load_config()
        cfg.generation.provider = "not-exist"
        with pytest.raises(KeyError):
            cfg.generation.active()

    def test_token_estimate(self):
        assert estimate_tokens("中文" * 10) == 20
        assert estimate_tokens("a" * 40) == 10


class TestEvalMatcher:
    def test_match_requires_doc_and_all_keys(self):
        from ragkb.evaluate import _match
        from ragkb.models import Candidate

        cand = Candidate(
            chunk_id=1, doc_id=1, doc_title="ARCHITECTURE", source_uri="x",
            page_no=None, heading_path="4. 检索层 > 4.2 权重", text="权重只表达相对比例",
            dense_rank=1, sparse_rank=None, dense_score=0.7, sparse_score=None,
            rrf_score=0.01, final_rank=1,
        )
        assert _match(cand, {"doc": "ARCHITECTURE", "keys": ["相对比例"]})
        # 关键词全命中才算——少一个就不是这条来源
        assert not _match(cand, {"doc": "ARCHITECTURE", "keys": ["相对比例", "量纲"]})
        # 文档不匹配直接 False
        assert not _match(cand, {"doc": "OTHER", "keys": []})


class TestUrlGuard:
    def test_rejects_private_hosts(self):
        from ragkb.ingest.loader import _assert_public_host

        for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "localhost"):
            with pytest.raises(ExtractError):
                _assert_public_host(host)

    def test_allowed_domains_is_checked_before_network(self):
        from ragkb.ingest.loader import load_url

        with pytest.raises(ExtractError, match="白名单"):
            load_url("https://evil.example.org/x", allowed_domains=["python.org"])

    def test_rejects_non_http_scheme(self):
        from ragkb.ingest.loader import load_url

        with pytest.raises(ExtractError, match="http/https"):
            load_url("file:///etc/passwd")


class TestRefusalCalibration:
    def test_calibration_ignored_when_model_differs(self, tmp_path):
        """阈值是嵌入模型余弦分布的属性，换模型后必须作废。"""
        import json

        from ragkb import config as cfg_mod

        path = tmp_path / "cal.json"
        path.write_text(json.dumps({
            "min_score": 0.71, "min_margin": 0.03,
            "calibrated_at": "2026-10-27T00:00:00Z", "model": "other@384",
            "reliable": True,
        }), encoding="utf-8")

        cfg = load_config(reload=True)
        cfg.refusal.calibration_file = str(path)
        cfg.refusal.min_score = 0.35
        cfg.refusal.calibrated_at = None
        cfg_mod._apply_calibration(cfg)
        assert cfg.refusal.min_score == 0.35
        assert cfg.refusal.calibrated_at is None

    def test_calibration_applied_when_reliable(self, tmp_path):
        import json

        from ragkb import config as cfg_mod

        cfg = load_config(reload=True)
        path = tmp_path / "cal.json"
        path.write_text(json.dumps({
            "min_score": 0.54, "min_margin": 0.0,
            "calibrated_at": "2026-10-27T00:00:00Z",
            "model": f"{cfg.embedding.model}@{cfg.embedding.dim}",
            "reliable": True,
        }), encoding="utf-8")

        cfg.refusal.calibration_file = str(path)
        cfg_mod._apply_calibration(cfg)
        assert cfg.refusal.min_score == 0.54
        assert cfg.refusal.calibrated_at is not None
        assert cfg.refusal.calibrated_samples_ok is True

    def test_unreliable_calibration_is_not_applied(self, tmp_path):
        """样本不足的标定不能改阈值。

        真实事故：2 条负样本扫出 min_score=0.54 被直接写进运行配置，
        而本语料可答问题的 top 低到 0.560、用户换个问法就掉到 0.53——
        于是"问什么都拒"。宁可退回出厂值，也不能让一个统计上站不住的
        数字替用户做决定。
        """
        import json

        from ragkb import config as cfg_mod

        cfg = load_config(reload=True)
        baseline = cfg.refusal.min_score
        path = tmp_path / "cal.json"
        path.write_text(json.dumps({
            "min_score": 0.54, "min_margin": 0.0,
            "calibrated_at": "2026-10-27T00:00:00Z",
            "model": f"{cfg.embedding.model}@{cfg.embedding.dim}",
            "reliable": False,
        }), encoding="utf-8")

        cfg.refusal.calibration_file = str(path)
        cfg_mod._apply_calibration(cfg)
        assert cfg.refusal.min_score == baseline
        assert cfg.refusal.calibrated_at is None
        assert cfg.refusal.calibrated_samples_ok is False


class TestSettingsRegistry:
    """参数面板的注册表：越界要拒、未知要拒、组合要拒、落盘只写差异。"""

    def _cfg(self):
        # 必须用**出厂配置**，不能用 load_config()。后者会叠上用户真实的
        # local.yaml —— 用户把 provider 改成 lmstudio 之后，下面那条
        # "全是默认值时应该删掉 local.yaml" 就会莫名其妙失败。测试不能
        # 依赖用户机器上改过什么。
        return defaults_config().model_copy(deep=True)

    def test_every_param_is_readable_and_in_a_known_group(self):
        from ragkb import settings

        groups = {g[0] for g in settings.GROUPS}
        cfg = self._cfg()
        for p in settings.PARAMS:
            assert p.group in groups, p.path
            settings._get(cfg, p.path)  # 路径写错会在这里炸

    def test_rejects_out_of_range(self):
        from ragkb import settings

        with pytest.raises(ValueError):
            settings.apply(self._cfg(), {"retrieval.dense.top_k": 5})
        with pytest.raises(ValueError):
            settings.apply(self._cfg(), {"refusal.min_score": 1.4})

    def test_rejects_unknown_path(self):
        from ragkb import settings

        with pytest.raises(ValueError, match="未知参数"):
            settings.apply(self._cfg(), {"retrieval.magic": 1})

    def test_rejects_overlap_not_less_than_size(self):
        from ragkb import settings

        with pytest.raises(ValueError, match="重叠"):
            settings.apply(self._cfg(), {"chunking.overlap": 480})

    def test_apply_is_all_or_nothing(self):
        """一条非法就整批不落地——改一半的配置比不改更难查。"""
        from ragkb import settings

        cfg = self._cfg()
        before = cfg.retrieval.fusion.w_dense
        with pytest.raises(ValueError):
            settings.apply(cfg, {"retrieval.fusion.w_dense": 0.9, "retrieval.rerank.top_k": 99})
        assert cfg.retrieval.fusion.w_dense == before

    def test_snapshot_marks_unsaved(self):
        from ragkb import settings

        cfg = self._cfg()
        settings.apply(cfg, {"retrieval.fusion.w_dense": 0.7})
        snap = settings.snapshot(cfg)
        assert "retrieval.fusion.w_dense" in snap["unsaved"]
        assert "retrieval.fusion.w_dense" in snap["modified"]

    def test_save_writes_only_diffs(self, tmp_path, monkeypatch):
        from ragkb import settings

        monkeypatch.setattr(settings, "LOCAL_CONFIG", tmp_path / "local.yaml")
        cfg = self._cfg()
        settings.apply(cfg, {"retrieval.fusion.w_dense": 0.7, "retrieval.mmr.lambda_": 0.8})
        result = settings.save_local(cfg)

        raw = (tmp_path / "local.yaml").read_text(encoding="utf-8")
        assert "w_dense: 0.7" in raw
        # 别名要写回 YAML 里的 lambda，而不是 pydantic 的 lambda_
        assert "lambda: 0.8" in raw and "lambda_" not in raw
        # 没改过的参数一个都不该出现
        assert "top_k" not in raw
        assert set(result["written"]) >= {"retrieval.fusion.w_dense", "retrieval.mmr.lambda_"}

    def test_save_removes_file_when_everything_is_default(self, tmp_path, monkeypatch):
        from ragkb import settings

        path = tmp_path / "local.yaml"
        path.write_text("retrieval:/n  fusion:/n    w_dense: 0.7\n", encoding="utf-8")
        monkeypatch.setattr(settings, "LOCAL_CONFIG", path)

        result = settings.save_local(self._cfg())  # 当前值全是出厂值
        assert result["removed"] is True
        assert not path.exists()

    def test_reset_goes_back_to_factory(self):
        from ragkb import settings

        cfg = self._cfg()
        settings.apply(cfg, {"retrieval.fusion.w_dense": 0.7, "retrieval.rerank.top_k": 9})
        settings.reset(cfg)
        base = settings.defaults_config()
        assert cfg.retrieval.fusion.w_dense == base.retrieval.fusion.w_dense
        assert cfg.retrieval.rerank.top_k == base.retrieval.rerank.top_k

    def test_deep_merge_keeps_siblings(self):
        from ragkb.config import _deep_merge

        merged = _deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
        assert merged == {"a": {"b": 1, "c": 3}}


def _temp_db(tmp_path):
    """独立库：不能碰用户真实的 data/library.db —— 测试把主库改坏是最难查的事故。"""
    from ragkb.config import load_config
    from ragkb.db import Database

    cfg = load_config().model_copy(deep=True)
    cfg.storage.db_path = tmp_path / "t.db"
    cfg.storage.data_dir = tmp_path
    cfg.storage.vector_dir = tmp_path / "vec"
    db = Database(cfg)
    db.ensure_schema()
    return cfg, db.conn()


class TestKbConfig:
    """知识库级覆盖：只存差异、不污染全局、跨字段按库自己的块长判。"""

    def _cfg(self):
        return load_config().model_copy(deep=True)

    def test_every_preset_is_valid(self):
        from ragkb import kbconfig

        cfg = self._cfg()
        for preset in kbconfig.PRESETS:
            kbconfig.normalize(cfg, preset["values"])  # 越界会在这里炸

    def test_effective_does_not_mutate_global(self):
        from ragkb import kbconfig

        cfg = self._cfg()
        before = cfg.chunking.size
        clone = kbconfig.effective(cfg, {"chunking.size": 300})
        assert clone.chunking.size == 300
        assert cfg.chunking.size == before
        assert clone is not cfg

    def test_cross_check_uses_kb_size_not_global(self):
        from ragkb import kbconfig

        cfg = self._cfg()
        # 块长 360 时，重叠 400 必须被拒
        with pytest.raises(ValueError, match="重叠"):
            kbconfig.normalize(cfg, {"chunking.size": 360, "chunking.overlap": 400})
        # 而 300 必须通过：拿全局 480 去判会把它误拒
        kbconfig.normalize(cfg, {"chunking.size": 360, "chunking.overlap": 300})

    def test_rejects_params_that_are_system_level(self):
        from ragkb import kbconfig

        with pytest.raises(ValueError, match="不支持按库覆盖"):
            kbconfig.normalize(self._cfg(), {"embedding.model": "other/model"})

    def test_save_load_roundtrip_and_snapshot_flags(self, tmp_path):
        from ragkb import kbconfig

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('t')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]

        overrides = kbconfig.normalize(cfg, {"chunking.size": 320, "refusal.min_score": 0.6})
        kbconfig.save(conn, kb_id, overrides)
        assert kbconfig.load(conn, kb_id) == overrides

        snap = kbconfig.snapshot(cfg, overrides)
        rows = {i["key"]: i for g in snap["groups"] for i in g["items"]}
        # 全量语义之后不再有 global / override 两列，每个值就是本库的值
        assert rows["chunking.size"]["value"] == 320
        assert "global" not in rows["chunking.size"]
        assert "override" not in rows["chunking.size"]
        # 没给值的项沿用传入的那套（UI 传的是完整一套）
        assert rows["chunking.overlap"]["value"] == cfg.chunking.overlap

    def test_migration_adds_config_json_column(self, tmp_path):
        _, conn = _temp_db(tmp_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kb)").fetchall()}
        assert "config_json" in cols

    def test_unknown_kb_rows_do_not_crash(self, tmp_path):
        from ragkb import kbconfig

        _, conn = _temp_db(tmp_path)
        assert kbconfig.load(conn, 9999) == {}


class TestModelRegistry:
    """后端注册表：只落差异、内置可删、当前后端不能删。"""

    def _cfg(self):
        # 同上：出厂配置，避免用户 local.yaml / models.yaml 里的条目被算成"差异"。
        return defaults_config().model_copy(deep=True)

    def test_write_only_diff(self, tmp_path, monkeypatch):
        import yaml

        from ragkb import modelcfg

        path = tmp_path / "models.yaml"
        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", path)
        cfg = self._cfg()

        modelcfg.upsert(cfg, "myserver", base_url="http://127.0.0.1:9000/v1",
                        model="qwen3-8b", kind="custom")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(raw["providers"]) == {"myserver"}
        assert raw["deleted"] == []
        assert cfg.generation.providers["myserver"].base_url == "http://127.0.0.1:9000/v1"

    def test_editing_builtin_is_recorded(self, tmp_path, monkeypatch):
        import yaml

        from ragkb import modelcfg

        path = tmp_path / "models.yaml"
        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", path)
        cfg = self._cfg()
        modelcfg.upsert(cfg, "vllm", base_url="http://127.0.0.1:9000/v1", model="x", kind="vllm")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["providers"]["vllm"]["model"] == "x"

    def test_deleting_builtin_leaves_a_marker(self, tmp_path, monkeypatch):
        import yaml

        from ragkb import modelcfg

        path = tmp_path / "models.yaml"
        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", path)
        cfg = self._cfg()
        modelcfg.remove(cfg, "openai")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "openai" not in cfg.generation.providers
        assert "openai" in raw["deleted"]

    def test_cannot_delete_active_provider(self, tmp_path, monkeypatch):
        from ragkb import modelcfg

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        cfg = self._cfg()
        with pytest.raises(ValueError, match="当前使用"):
            modelcfg.remove(cfg, cfg.generation.provider)

    def test_rejects_bad_name_and_url(self, tmp_path, monkeypatch):
        from ragkb import modelcfg

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        cfg = self._cfg()
        with pytest.raises(ValueError):
            modelcfg.upsert(cfg, "有中文", base_url="http://127.0.0.1:1/v1", model="m")
        with pytest.raises(ValueError):
            modelcfg.upsert(cfg, "ok", base_url="127.0.0.1:1/v1", model="m")
        with pytest.raises(ValueError):
            modelcfg.upsert(cfg, "ok", base_url="http://127.0.0.1:1/v1", model="")

    def test_listing_marks_local_and_active(self):
        from ragkb import modelcfg

        data = modelcfg.listing(load_config())
        active = [p for p in data["providers"] if p["active"]]
        assert len(active) == 1
        assert active[0]["name"] == load_config().generation.provider
        assert all(p["kind_label"] for p in data["providers"])


class TestReadiness:
    """「配好了没有」和「连不连得通」必须分开判。

    混成一句"不可用"的后果：密钥没设的云端后端会被当成"服务没起"，
    用户去重启服务、去改端口，而真正缺的是一个环境变量。
    """

    def _cfg(self, **provider):
        from ragkb.config import ProviderCfg

        cfg = defaults_config().model_copy(deep=True)
        cfg.generation.providers = {"t": ProviderCfg(**provider)}
        cfg.generation.provider = "t"
        return cfg

    def test_complete_local_provider_is_ready(self):
        from ragkb import modelcfg

        cfg = self._cfg(base_url="http://127.0.0.1:8000/v1", model="qwen", kind="vllm")
        assert modelcfg.readiness(cfg) == {
            "provider": "t", "ready": True, "code": "ok", "reason": ""}

    def test_missing_url_model_and_key_have_distinct_codes(self):
        from ragkb import modelcfg

        assert modelcfg.readiness(self._cfg(base_url=None, model="q", kind="custom"))["code"] == "no_url"
        assert modelcfg.readiness(self._cfg(base_url="http://x/v1", model="", kind="custom"))["code"] == "no_model"
        # 云端没配密钥：字段齐但内存里没有密钥
        cfg = self._cfg(base_url="https://api.x/v1", model="gpt", kind="openai")
        r = modelcfg.readiness(cfg)
        assert r["code"] == "no_key" and "密钥" in r["reason"]

    def test_cloud_ready_once_key_is_in_memory(self):
        """密钥进内存后，云端后端就该是就绪的。"""
        from ragkb import modelcfg, secrets

        cfg = self._cfg(base_url="https://api.x/v1", model="gpt", kind="openai")
        secrets.put("t", "sk-test")
        try:
            assert modelcfg.readiness(cfg)["ready"] is True
        finally:
            secrets.drop("t")

    def test_mock_is_ready_without_url(self):
        from ragkb import modelcfg

        assert modelcfg.readiness(self._cfg(model="mock", kind="mock"))["code"] == "mock"

    def test_dangling_provider_is_reported(self):
        from ragkb import modelcfg

        cfg = defaults_config().model_copy(deep=True)
        cfg.generation.provider = "ghost"
        r = modelcfg.readiness(cfg)
        assert r["ready"] is False and r["code"] == "no_provider"

    def test_listing_carries_ready_flags(self):
        from ragkb import modelcfg

        data = modelcfg.listing(load_config())
        assert isinstance(data["active_ready"], bool)
        assert all("ready" in p and "ready_reason" in p for p in data["providers"])


class TestServiceProbe:
    def test_mock_needs_no_network(self):
        from ragkb import services
        from ragkb.config import ProviderCfg

        r = services.probe_one("mock", ProviderCfg(model="mock", kind="mock"))
        assert r["status"] == "mock" and r["ok"] is True

    def test_unset_base_url_reported(self):
        from ragkb import services
        from ragkb.config import ProviderCfg

        r = services.probe_one("x", ProviderCfg(base_url=None, model="m", kind="custom"))
        assert r["status"] == "unset" and r["ok"] is False

    def test_localhost_bypasses_proxy(self):
        """本地地址必须不被 HTTP_PROXY 劫持，否则"服务起来了"也会报 502。"""
        from ragkb.services import _is_local

        assert _is_local("http://127.0.0.1:8000/v1/models")
        assert _is_local("http://localhost:1234/v1")
        assert _is_local("http://192.168.1.9:8080/v1")
        assert not _is_local("https://api.openai.com/v1/models")


class TestDashboard:
    def test_builds_with_empty_database(self, tmp_path):
        import time as _t

        from ragkb.dashboard import build

        cfg, conn = _temp_db(tmp_path)
        data = build(cfg, conn, started_at=_t.time(), port=8765)
        assert data["assets"]["kbs"] == 0
        assert data["server"]["port"] == 8765
        assert data["health"]["queries"] == 0
        assert isinstance(data["services"], list)
        assert "queries" in data["logs"] and "system" in data["logs"]

    def test_counts_after_insert(self, tmp_path):
        import time as _t

        from ragkb.dashboard import build

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('a')")
        data = build(cfg, conn, started_at=_t.time())
        assert data["assets"]["kbs"] == 1
        assert data["kbs"][0]["name"] == "a"


class TestChatContext:
    """上下文占用核算：满了要能说清"满在哪"，不能只报一句"已满"。"""

    @staticmethod
    def _hist(n: int, chars: int = 100) -> list[dict]:
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * chars}
                for i in range(n)]

    def test_plenty_of_room_says_nothing(self):
        from ragkb.chatctx import notice, usage

        ctx = usage(self._hist(2), total_messages=2, budget=6000, turns_limit=5)
        assert ctx["truncated"] is False
        assert ctx["exhausted"] is False
        assert notice(ctx) is None

    def test_turns_truncated_is_reported_separately(self):
        """轮次被 history_turns 截断 ≠ 预算被吃满，两者提示语必须不同。"""
        from ragkb.chatctx import notice, usage

        ctx = usage(self._hist(10, chars=10), total_messages=40, budget=6000, turns_limit=5)
        assert ctx["truncated"] is True
        assert ctx["exhausted"] is False
        text = notice(ctx)
        assert "早期对话已不再带入" in text
        assert "保留最近 5 轮" in text

    def test_budget_eaten_is_reported_as_full(self):
        from ragkb.chatctx import notice, usage

        ctx = usage(self._hist(4, chars=900), total_messages=4, budget=1000, turns_limit=5)
        assert ctx["exhausted"] is True
        assert "上下文已满" in notice(ctx)

    def test_budget_left_never_below_fallback(self):
        """build_context 兜底至少留 200 字符，提示里的"剩余"不能算出 0 甚至负数。"""
        from ragkb.chatctx import MIN_CONTEXT_CHARS, usage

        ctx = usage(self._hist(2, chars=5000), total_messages=2, budget=1000, turns_limit=5)
        assert ctx["budget_left"] == MIN_CONTEXT_CHARS

    def test_no_history_is_never_exhausted(self):
        from ragkb.chatctx import notice, usage

        ctx = usage([], total_messages=0, budget=800, turns_limit=5)
        assert ctx["exhausted"] is False
        assert notice(ctx) is None


class TestChunkStrategy:
    """分块策略：三种策略必须真的切出不同的块数，否则它就是另一个"调了没反应"。"""

    @staticmethod
    def _blocks() -> list:
        from ragkb.models import Block

        # 三个章节，每章两小段，一段约 125 字；正文里不写标题文字，
        # 章节归属只通过 heading_path 表达（真实 loader 也是这么给的）
        out = []
        for h in ("A > 一", "A > 二", "A > 三"):
            for i in range(2):
                out.append(Block(text=f"第{i + 1}段。" + "内容" * 60, heading_path=h))
        return out

    def test_heading_strategy_merges_within_heading_only(self):
        drafts = chunk_blocks(self._blocks(), ChunkingCfg(size=400, overlap=0,
                                                         min_chunk=10, strategy="heading"))
        # 三个章节 → 三块；同章节内两段合并，跨章节绝不合并
        assert len(drafts) == 3
        assert {d.heading_path for d in drafts} == {"A > 一", "A > 二", "A > 三"}

    def test_size_strategy_packs_by_length(self):
        # 关掉"按标题对齐"才是纯按长度：124*3 ≈ 376 < 400 → 两块
        drafts = chunk_blocks(self._blocks(), ChunkingCfg(
            size=400, overlap=0, min_chunk=10, strategy="size", respect_headings=False))
        assert len(drafts) == 2

    def test_size_strategy_can_align_to_headings(self):
        """respect_headings 真的参与切分——以前它是个调了没反应的死开关。"""
        drafts = chunk_blocks(self._blocks(), ChunkingCfg(
            size=400, overlap=0, min_chunk=10, strategy="size", respect_headings=True))
        # 装到六成满（240 字）遇新章节就换块，于是变成一章一块
        assert len(drafts) == 3

    def test_paragraph_strategy_one_block_per_paragraph(self):
        blocks = self._blocks()
        drafts = chunk_blocks(blocks, ChunkingCfg(size=400, overlap=40,
                                                  min_chunk=10, strategy="paragraph"))
        # 每段一块，且不重叠（重叠会把上一段的尾巴缝到下一段开头）
        assert len(drafts) == len(blocks)
        for d in drafts:
            assert len(d.text) <= max(len(b.text) for b in blocks)

    def test_unknown_strategy_falls_back_to_size(self):
        drafts = chunk_blocks(self._blocks(), ChunkingCfg(
            size=400, overlap=0, min_chunk=10, strategy="whatever", respect_headings=False))
        assert len(drafts) == 2


class TestRefusalSoftSignal:
    """拒答拆两档：硬拒只拦"明显不沾边"，中间地带照答但标低置信。"""

    @staticmethod
    def _engine(cfg=None):
        from ragkb.config import Config
        from ragkb.retrieve.engine import _soft

        return _soft

    def test_high_score_is_confident(self):
        from ragkb.config import RefusalCfg

        soft = self._engine()
        cfg = RefusalCfg(min_score=0.35)
        assert soft(0.72, 0.09, cfg) == (False, None)

    def test_middle_band_is_low_confidence_not_refusal(self):
        from ragkb.config import RefusalCfg

        soft = self._engine()
        cfg = RefusalCfg(min_score=0.35)
        low, reason = soft(0.40, 0.05, cfg)
        assert low is True
        assert "0.40" in reason

    def test_flat_candidates_are_low_confidence(self):
        from ragkb.config import RefusalCfg

        soft = self._engine()
        cfg = RefusalCfg(min_score=0.35)
        low, reason = soft(0.70, 0.004, cfg)
        assert low is True
        assert "持平" in reason

    def test_no_score_never_claims_confidence(self):
        from ragkb.config import RefusalCfg

        soft = self._engine()
        assert soft(None, None, RefusalCfg()) == (False, None)


class TestSourcePaths:
    """上传副本被删 → source_uri 变死路径 → 重索引 100% 失败。

    修法有两条腿：上传不再删副本；判定"源文件在不在"抽成可测函数，
    让接口能提前 409 并把入口换成"重新上传"，而不是让用户点一次错一次。
    """

    def test_missing_file_detected(self, tmp_path):
        from ragkb.paths import source_missing

        assert source_missing("file", str(tmp_path / "nope.docx")) is True

    def test_existing_file_not_flagged(self, tmp_path):
        from ragkb.paths import source_missing

        p = tmp_path / "a.docx"
        p.write_bytes(b"x")
        assert source_missing("file", str(p)) is False

    def test_url_and_text_never_missing(self):
        from ragkb.paths import source_missing

        # URL / 粘贴文本没有本地副本，"缺失"这个概念不适用，否则界面会误报
        assert source_missing("url", "https://example.com/a") is False
        assert source_missing("text", "text://abc/def") is False

    def test_inside_root(self, tmp_path):
        from ragkb.paths import inside

        root = tmp_path / "uploads"
        root.mkdir()
        assert inside(root / "a.docx", root) is True
        assert inside(tmp_path / "a.docx", root) is False
        assert inside(root / ".." / "a.docx", root) is False


class TestDisplayTitle:
    """落盘名带 8 位十六进制前缀防撞名，标题里不能带。

    这个 bug 出现过两次：上传时修好了，重索引走 `_commit` 又退回带前缀的版本
    （`_commit` 用的是 `loaded.title`，而 loader 给的是 `path.stem`）。所以
    标题统一由 `display_title()` 生成，两处共用同一个函数。
    """

    def test_strips_upload_prefix(self):
        from ragkb.ingest.pipeline import display_title

        assert display_title("D:/kb/data/uploads/23970fa2-zyyw_update.docx") == "zyyw_update.docx"

    def test_plain_path_untouched(self):
        from ragkb.ingest.pipeline import display_title

        assert display_title("D:/docs/ARCHITECTURE.md") == "ARCHITECTURE.md"

    def test_short_hex_is_not_a_prefix(self):
        from ragkb.ingest.pipeline import display_title

        # 只有 8 位才算前缀：3f2a- 这种不该被吃掉，否则会误伤正常文件名
        assert display_title("3f2a-note.md") == "3f2a-note.md"

    def test_url_keeps_last_segment(self):
        from ragkb.ingest.pipeline import display_title

        assert display_title("https://example.com/a/b.html") == "b.html"


class TestWeightedFusion:
    """融合方式从 RRF 换成归一化加权，实测 R@5 0.75 → 0.96。

    这里锁住三件事：bm25 的符号方向、单路退化、以及"两路都命中该赢"。
    方向那一条最容易写错——第一版就是漏了它，加权融合的 R@5 掉到 0.32，
    比原来的 RRF 还差一半。
    """

    @staticmethod
    def _hit(cid, score, rank, label):
        from ragkb.models import Hit

        return Hit(chunk_id=cid, score=score, rank=rank, label=label)

    def test_bm25_scores_are_flipped(self):
        from ragkb.retrieve.engine import fuse_weighted

        # bm25 返回负值、越小越相关：-9 是最相关的那条
        sparse = [self._hit(1, -9.0, 1, "sparse"), self._hit(2, -1.0, 2, "sparse")]
        out = fuse_weighted([sparse], [1.0], top_k=2)
        assert [o["chunk_id"] for o in out] == [1, 2]

    def test_better_score_wins_within_one_road(self):
        from ragkb.retrieve.engine import fuse_weighted

        dense = [self._hit(7, 0.9, 1, "dense"), self._hit(8, 0.4, 2, "dense")]
        out = fuse_weighted([dense], [1.0], top_k=2)
        assert [o["chunk_id"] for o in out] == [7, 8]

    def test_hit_in_both_roads_beats_single_road(self):
        from ragkb.retrieve.engine import fuse_weighted

        dense = [self._hit(1, 0.80, 1, "dense"), self._hit(2, 0.79, 2, "dense")]
        sparse = [self._hit(1, -5.0, 1, "sparse")]
        out = fuse_weighted([dense, sparse], [0.35, 0.5], top_k=2)
        assert out[0]["chunk_id"] == 1

    def test_zero_weight_road_is_ignored(self):
        from ragkb.retrieve.engine import fuse_weighted

        dense = [self._hit(1, 0.9, 1, "dense")]
        sparse = [self._hit(2, -9.0, 1, "sparse")]
        out = fuse_weighted([dense, sparse], [1.0, 0.0], top_k=2)
        assert [o["chunk_id"] for o in out] == [1]

    def test_flat_scores_do_not_divide_by_zero(self):
        from ragkb.retrieve.engine import fuse_weighted

        # 整路分数相同 → 归一化分母为 0，必须退化成 0.5 而不是崩掉
        dense = [self._hit(1, 0.5, 1, "dense"), self._hit(2, 0.5, 2, "dense")]
        out = fuse_weighted([dense], [1.0], top_k=2)
        assert len(out) == 2


class TestQueryInstruction:
    """查询侧指令前缀：只加在查询上，语料侧绝不能加。"""

    def test_prefix_applied_once(self):
        from ragkb.retrieve.engine import RetrievalEngine

        cfg = type("C", (), {"embedding": type("E", (), {"query_instruction": "指令："})()})()
        assert RetrievalEngine._query_text("什么是维医", cfg) == "指令：什么是维医"
        # 重复调用不该叠加
        assert RetrievalEngine._query_text("指令：什么是维医", cfg) == "指令：什么是维医"

    def test_empty_instruction_passes_through(self):
        from ragkb.retrieve.engine import RetrievalEngine

        cfg = type("C", (), {"embedding": type("E", (), {"query_instruction": ""})()})()
        assert RetrievalEngine._query_text("什么是维医", cfg) == "什么是维医"


class TestDocxFlowExtraction:
    """Word 抽取必须拿到表格与文本框。

    旧实现只遍历 document.paragraphs，于是表格里的字 100% 丢失、文本框里的
    字也丢失——用户看到的就是"文件没全进去"，而导入还报成功。
    """

    def test_table_and_textbox_are_extracted(self, tmp_path):
        docx = pytest.importorskip("docx")
        from ragkb.ingest.loader import load_path

        d = docx.Document()
        d.add_paragraph("正文第一段")
        tbl = d.add_table(rows=2, cols=2)
        tbl.cell(0, 0).text = "药名"
        tbl.cell(0, 1).text = "用量"
        tbl.cell(1, 0).text = "二十五味珍珠丸"
        tbl.cell(1, 1).text = "一次 1g"
        d.add_paragraph("正文第二段")

        # 文本框：python-docx 没有公开 API，直接塞 XML
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
        p = d.add_paragraph()
        run = p.add_run()
        tb = OxmlElement("w:txbxContent")
        inner = OxmlElement("w:p")
        ir = OxmlElement("w:r")
        it = OxmlElement("w:t")
        it.text = "文本框里的药方"
        ir.append(it)
        inner.append(ir)
        tb.append(inner)
        run._r.append(tb)

        path = tmp_path / "t.docx"
        d.save(str(path))
        loaded = load_path(path, min_pdf_text_chars=0)

        text = loaded.text
        assert "正文第一段" in text and "正文第二段" in text
        assert "二十五味珍珠丸" in text, "表格内容必须入库"
        assert "文本框里的药方" in text, "文本框内容必须入库"
        assert any(b.kind == "table" for b in loaded.blocks)
        assert loaded.extractor == "python-docx/flow"

    def test_deleted_revision_is_not_extracted(self, tmp_path):
        """被删除的修订（w:del）不能当作正文：文档里根本看不到这些字。"""
        docx = pytest.importorskip("docx")
        from docx.oxml import OxmlElement
        from ragkb.ingest.loader import load_path

        d = docx.Document()
        p = d.add_paragraph()
        p.add_run("保留的文字")
        run = p.add_run()
        r = run._r
        r.getparent().remove(r)
        p._p.append(r)
        del_el = OxmlElement("w:del")
        rr = OxmlElement("w:r")
        tt = OxmlElement("w:t")
        tt.text = "被删掉的文字"
        rr.append(tt)
        del_el.append(rr)
        p._p.append(del_el)

        path = tmp_path / "d.docx"
        d.save(str(path))
        loaded = load_path(path, min_pdf_text_chars=0)
        assert "保留的文字" in loaded.text
        assert "被删掉的文字" not in loaded.text


class TestCoverage:
    """完整性自检：覆盖率算法必须能真检出"少了一段"。"""

    def test_full_coverage(self):
        from ragkb.api.app import _coverage_of

        src = "藏医三因学说：隆、赤巴、培根。"
        r = _coverage_of(src, [src])
        assert r["ratio"] == 1.0
        assert r["gaps"] == []

    def test_missing_middle_is_reported(self):
        from ragkb.api.app import _coverage_of

        head = "这一句是文档开头的内容，足够长以形成一个窗口，后面还要再补一些字让它更长。"
        tail = "这一句是文档结尾的内容，同样足够长以形成窗口，后面也要再补一些字让它更长。"
        middle = ("这一段被切分弄丢了，自检必须把它找出来并给出原文片段；"
                  "之所以要求成段才报，是因为页眉页码那类几十字的差异不是真的内容丢失，"
                  "而当证据摆给用户看只会让人以为系统把文件吃掉了。")
        r = _coverage_of(head + middle + tail, [head, tail])
        assert r["ratio"] < 1.0
        assert r["big_gap_count"] >= 1
        assert "被切分弄丢了" in r["gaps"][0]["text"]

    def test_short_gaps_are_not_reported_as_evidence(self):
        """几十字的差异（页码、标记符号）只进百分比，不摆出来当"丢了内容"。"""
        from ragkb.api.app import _coverage_of

        src = "甲" * 100 + "乙" * 30 + "丙" * 100
        r = _coverage_of(src, ["甲" * 100, "丙" * 100])
        assert r["ratio"] < 1.0
        assert r["big_gap_count"] == 0
        assert r["gaps"] == []

    def test_whitespace_is_ignored(self):
        from ragkb.api.app import _coverage_of

        src = "维医 four 体液 学说"
        assert _coverage_of(src, [src.replace(" ", "\n\n")])["ratio"] == 1.0

    def test_stale_extractor_flag(self):
        from ragkb.api.app import _stale_extractor

        assert _stale_extractor("python-docx", "/data/uploads/a.docx") is True
        assert _stale_extractor("python-docx/flow", "/data/uploads/a.docx") is False
        assert _stale_extractor("python-docx", "/data/uploads/a.pdf") is False
        assert _stale_extractor(None, "/data/uploads/a.docx") is False


class TestImportTaskQueue:
    """多个重索引必须排队，而且每个任务要能说出自己排在第几位。

    以前每个任务各起一条线程：两个重索引同时写 SQLite、同时占嵌入模型，
    前端还只有一张进度卡，两个轮询往同一处写百分比——用户看到的就是数字
    来回跳、不知道哪个是哪个。
    """

    def test_two_tasks_do_not_overlap(self):
        import time

        from ragkb.api.app import _start_import_task, import_task

        spans: list[tuple[str, float, float]] = []

        def make(name):
            def fn(progress):
                t0 = time.perf_counter()
                time.sleep(0.12)
                spans.append((name, t0, time.perf_counter()))
                return {"status": "ready", "message": "ok"}
            return fn

        a = _start_import_task("a", make("a"))
        # 等 a 真正开跑再提交 b，这样 b 的排队位置是确定的 1，
        # 否则两个都还躺在队列里，"排在第几位"取决于调度时序，测不稳
        for _ in range(200):
            if import_task(a["task_id"])["state"] == "running":
                break
            time.sleep(0.01)
        b = _start_import_task("b", make("b"))
        assert b["state"] == "queued"
        assert b["queue_pos"] == 1, "第二个任务必须知道自己前面还有一个"

        for started in (a, b):
            snap = None
            for _ in range(200):
                snap = import_task(started["task_id"])
                if snap["state"] in ("ready", "failed"):
                    break
                time.sleep(0.02)
            assert snap["state"] == "ready", snap

        assert [s[0] for s in spans] == ["a", "b"], "任务必须按提交顺序执行"
        (_, a0, a1), (_, b0, _) = spans
        assert a1 <= b0 + 1e-6, "两个任务的执行区间重叠了，说明没有串行"

    def test_internal_fields_are_not_exposed(self):
        """任务里挂着 _work（一个函数），不能进 JSON 响应。"""
        import time

        from ragkb.api.app import _start_import_task, import_task

        t = _start_import_task("x", lambda progress: {"status": "ready", "message": "ok"})
        for _ in range(200):
            snap = import_task(t["task_id"])
            if snap["state"] in ("ready", "failed"):
                break
            time.sleep(0.02)
        assert not [k for k in snap if k.startswith("_")]
        assert snap["state"] == "ready"


class TestProbeChatVerifyAndPersistence:
    """探测的两处升级（2026-10-28）：

    1. /models 通了不算联通——one-api 这类网关对 /models 永远回 200 全列表，
       结果四个后端全"就绪"，实际只有一个能对话。必须再发一次 1 token 补全。
    2. 探测结果落盘——以前只存内存，服务一重启模型页整页"还没探测"，
       用户以为数据丢了。
    """

    def test_chat_failure_is_warn_not_ok(self, monkeypatch):
        """服务可达、/models 有列表，但对话失败 → 必须是 warn 而不是 ok。"""
        import httpx

        from ragkb import services
        from ragkb.config import ProviderCfg

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"data": [{"id": "qwen"}]}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return FakeResp()
            def post(self, *a, **k): raise httpx.ConnectError("refused")

        monkeypatch.setattr(services.httpx, "Client", FakeClient)
        r = services.probe_one("fake", ProviderCfg(
            base_url="http://127.0.0.1:9999/v1", model="qwen", kind="openai"))
        assert r["status"] == "warn", "对话失败就不能标 ok"
        assert "对话失败" in r["message"]

    def test_chat_pass_is_ok(self, monkeypatch):
        from ragkb import services
        from ragkb.config import ProviderCfg

        class FakeResp:
            def __init__(self, body): self._b = body
            def raise_for_status(self): pass
            def json(self): return self._b

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, *a, **k): return FakeResp({"data": [{"id": "qwen"}]})
            def post(self, *a, **k): return FakeResp({"choices": [{"message": {"content": "ok"}}]})

        monkeypatch.setattr(services.httpx, "Client", FakeClient)
        r = services.probe_one("fake", ProviderCfg(
            base_url="http://127.0.0.1:9999/v1", model="qwen", kind="openai"))
        assert r["status"] == "ok"
        assert "对话验证通过" in r["message"]

    def test_probe_cache_survives_restart(self, tmp_path, monkeypatch):
        import json
        import time

        from ragkb import services

        f = tmp_path / "probe_cache.json"
        monkeypatch.setattr(services, "_CACHE_FILE", f)
        monkeypatch.setattr(services, "_CACHE", {})
        services.record({"name": "vllm", "checked_at": time.time(), "status": "ok",
                         "message": "就绪", "latency_ms": 5, "models": ["qwen"],
                         "model_listed": True, "kind": "openai", "base_url": "x", "model": "qwen"})
        assert f.exists(), "record 必须落盘"
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["vllm"]["status"] == "ok"
        # 模拟重启：内存清空后 load 回来
        monkeypatch.setattr(services, "_CACHE", {})
        services._CACHE.update(json.loads(f.read_text(encoding="utf-8")))
        assert services.cached("vllm") is not None, "重启后 cached 必须能读到上次结论"


class TestKbFullConfig:
    """库级参数从"差异覆盖"改成"每个库一套完整值"（2026-10-28）。

    界面上因此取消了「覆盖 / 跟随全局」这个维度：24 项参数每行一个开关 +
    每个布尔项一个三态下拉，页面长到没法扫。代价是全局改了本库不跟着变——
    这本来就更符合"给这个库调一套参数"的心理模型。
    """

    def test_materialize_is_a_complete_set(self):
        from ragkb import kbconfig
        from ragkb.config import load_config

        cfg = load_config()
        full = kbconfig.materialize(cfg, {"chunking.size": 320})
        missing = [k for k in kbconfig.KB_PARAM_KEYS if k not in full]
        assert not missing, f"缺了这些参数：{missing}"
        assert full["chunking.size"] == 320

    def test_old_diff_only_kb_is_migrated(self, tmp_path):
        """老库存的是差异：读的时候用当前全局值补齐，并且**写回**。"""
        import json

        from ragkb import kbconfig
        from ragkb.config import load_config

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name, config_json) VALUES ('old', ?)",
                     (json.dumps({"chunking.size": 333}),))
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]

        values = kbconfig.load_full(conn, kb_id, cfg)
        assert values["chunking.size"] == 333
        assert "retrieval.rerank.top_k" in values, "缺口必须用全局值补齐"

        raw = json.loads(conn.execute(
            "SELECT config_json FROM kb WHERE id=?", (kb_id,)).fetchone()["config_json"])
        assert raw.get(kbconfig.FULL_MARK) == 1, "补齐之后必须写回成全量"
        # 再读一次不该再触发迁移，且结果一致
        assert kbconfig.load_full(conn, kb_id, cfg) == values

    def test_needs_reindex_only_for_chunking(self):
        from ragkb import kbconfig

        before = {"chunking.size": 480, "chunking.overlap": 80,
                  "retrieval.rerank.top_k": 5}
        after1 = {**before, "retrieval.rerank.top_k": 8}
        after2 = {**before, "chunking.size": 400}
        assert kbconfig.needs_reindex(before, after1) == []
        assert kbconfig.needs_reindex(before, after2) == ["chunking.size"]

    def test_load_strips_the_marker(self, tmp_path):
        """__full__ 只是存储格式标记，不能混进"参数值"里。"""
        from ragkb import kbconfig

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('t')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        full = kbconfig.materialize(cfg, {"chunking.size": 400})
        kbconfig.save_full(conn, kb_id, full)
        values = kbconfig.load(conn, kb_id)
        assert kbconfig.FULL_MARK not in values
        assert values["chunking.size"] == 400


class TestAutoCoverageOnTask:
    """导入/重索引结束时自动做一次完整性自检（2026-10-28）。

    起因是"我怎么感觉数据没完全导入好"——这个问题不该靠翻预览弹窗里一个
    藏得很深的按钮来回答。索引刚建完源文件还在手边，这时比对最便宜，结论
    直接挂在任务结果上，用户导入完就看到"全文已入库 99.89%"。
    """

    def test_result_carries_coverage(self, monkeypatch):
        import time

        import ragkb.api.app as A

        monkeypatch.setattr(
            A, "_coverage_digest",
            lambda did: {"ratio": 0.9989, "big_gap_count": 0, "max_gap": 39,
                         "stale_source": False, "source_chars": 224996,
                         "chunks_chars": 213695},
        )
        t = A._start_import_task("x", lambda p: {"status": "ready", "message": "ok", "doc_id": 7})
        snap = None
        for _ in range(200):
            snap = A.import_task(t["task_id"])
            if snap["state"] in ("ready", "failed"):
                break
            time.sleep(0.02)
        assert snap["state"] == "ready"
        assert snap["result"]["coverage"]["ratio"] == 0.9989

    def test_failed_task_has_no_coverage(self):
        import time

        from ragkb.api.app import _start_import_task, import_task

        t = _start_import_task("y", lambda p: {"status": "failed", "message": "bad", "doc_id": 7})
        for _ in range(200):
            snap = import_task(t["task_id"])
            if snap["state"] in ("ready", "failed"):
                break
            time.sleep(0.02)
        assert snap["state"] == "failed"
        assert "coverage" not in (snap["result"] or {})

    def test_digest_never_breaks_the_task(self, monkeypatch):
        """自检本身炸了也不能把导入任务拖成失败。"""
        import ragkb.api.app as A

        def boom(doc_id):
            raise RuntimeError("源文件突然读不到了")

        monkeypatch.setattr(A, "_coverage_report", boom)
        assert A._coverage_digest(1) is None


class TestLaunchCommands:
    """启动命令（2026-10-28）：按系统给、端口和卡片一致、不留占位符。

    之前那几条命令是照着 Linux 写的，还把 vLLM 的端口定在 8000——而 8000
    正是本系统 Web 服务的端口，照抄的人只会看到"vLLM 起不来"。
    """

    def test_port_placeholder_is_filled(self):
        from ragkb import modelcfg

        r = modelcfg._render_launch(modelcfg.KIND_MAP["lmstudio"], "1235")
        assert "{port}" not in r["launch"]
        assert "1235" in r["launch"], "命令里的端口必须和卡片上的地址一致"

    def test_vllm_port_does_not_collide_with_web_ui(self):
        from ragkb import modelcfg

        assert ":8000" not in modelcfg.KIND_MAP["vllm"]["default_base"], \
            "8000 是 ragkb 自己的 Web 服务端口，撞上就只剩'起不来'三个字"

    def test_every_local_kind_has_a_copyable_command(self):
        """以前 custom 那一栏只有一行注释，复制过去什么都不会发生。"""
        from ragkb import modelcfg

        for k in modelcfg.kinds():
            if k["local"] and k["id"] != "mock":
                assert k["launch"].strip(), f"{k['id']} 没给能照抄的命令"
            assert "{port}" not in (k["launch"] or "")


class TestDeleteKb:
    """删库（2026-10-28）。

    起因是"删除知识库没有实现"：后端其实是通的，卡住的是前端那句原生
    confirm() —— 在内嵌 WebView 里它被吞掉，点击后毫无反应，看上去就是没做。
    顺带把后端补实：FTS 是独立虚表，外键级联管不到它，不手动删会留下搜得到、
    却指向已消失 chunk 的幽灵行；另外删库要能报出"删掉了多少"。
    """

    def _state(self, cfg, conn):
        from ragkb.index.sparse import SparseIndex

        class _V:
            def __init__(self):
                self.invalidated = 0

            def invalidate(self):
                self.invalidated += 1

        class _S:
            def __init__(self, conn):
                self.conn = conn
                self.sparse = SparseIndex(cfg.retrieval.sparse)
                self.vectors = _V()
                self.cleared = []

            def invalidate_kb_cfg(self, kb_id=None):
                self.cleared.append(kb_id)

        return _S(conn)

    def test_deletes_docs_chunks_and_fts(self, tmp_path):
        from ragkb.api import app as A

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('gone')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.execute(
            "INSERT INTO document (kb_id, source_type, source_uri, content_hash,"
            " extractor, status, imported_at, updated_at, active_gen)"
            " VALUES (?,'text','t','h','x','ready','now','now',1)", (kb_id,))
        doc_id = conn.execute("SELECT id FROM document").fetchone()["id"]
        conn.executemany(
            "INSERT INTO chunk (doc_id, generation, seq, text, text_seg, token_count,"
            " content_hash) VALUES (?,1,?,?,?,1,?)",
            [(doc_id, i, f"内容{i}", f"内容 {i}", f"h{i}") for i in range(5)])
        ids = [r["id"] for r in conn.execute("SELECT id FROM chunk")]
        conn.executemany("INSERT INTO chunk_fts(rowid, text_seg) VALUES (?,?)",
                         [(i, "内容") for i in ids])

        st = self._state(cfg, conn)
        monkeypatched = A.state
        A.state = st
        try:
            r = A.delete_kb(kb_id)
        finally:
            A.state = monkeypatched

        assert r["docs"] == 1 and r["chunks"] == 5, "返回值要报实数，不是一句已删除"
        assert conn.execute("SELECT COUNT(*) c FROM document").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM chunk").fetchone()["c"] == 0
        # 幽灵行：搜得到但 chunk 已经没了
        assert conn.execute("SELECT COUNT(*) c FROM chunk_fts").fetchone()["c"] == 0
        assert st.vectors.invalidated == 1
        assert kb_id in st.cleared

    def test_sessions_survive_unbound(self, tmp_path):
        """会话按设计保留：库没了，历史问答还有价值，只是解除绑定。
        确认框里就是这么写的，实现不能偷偷把它们一起删了。"""
        from ragkb.api import app as A

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('s')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.execute(
            "INSERT INTO session (id, kb_id, title, created_at, updated_at)"
            " VALUES ('s1',?,'问答','now','now')", (kb_id,))

        st = self._state(cfg, conn)
        prev = A.state
        A.state = st
        try:
            r = A.delete_kb(kb_id)
        finally:
            A.state = prev

        assert r["sessions"] == 1
        row = conn.execute("SELECT kb_id FROM session WHERE id='s1'").fetchone()
        assert row is not None, "会话不该被连带删除"
        assert row["kb_id"] is None

    def test_missing_kb_is_404_not_silent_ok(self, tmp_path):
        """以前删一个不存在的库也返回 {"deleted": id}，前端只会刷掉列表，
        看不出到底是删成功了还是压根没这个库。"""
        from fastapi import HTTPException

        from ragkb.api import app as A

        cfg, conn = _temp_db(tmp_path)
        st = self._state(cfg, conn)
        prev = A.state
        A.state = st
        try:
            try:
                A.delete_kb(999)
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError("不存在的库必须报 404")
        finally:
            A.state = prev


class TestImportCleaning:
    """导入前清洗（2026-10-28）。

    用户要的是"导入前把不必要的内容洗掉"，附加条件是**不动源文件**。
    所以清洗只作用于抽取结果在内存里的那份，连上传副本也不写回——写回会让
    重索引拿到洗过一遍的输入，二次清洗结果不可预期，而且从此分不清库里的内容
    对应源文件的哪一部分。

    另一条底线：删了什么必须能列出来。规则删错 = 永久丢失，chunk 里没有的
    内容永远检索不到，也不会有任何迹象。
    """

    @staticmethod
    def _doc(blocks):
        from ragkb.models import Block, LoadedDoc

        return LoadedDoc(
            text="\n\n".join(blocks),
            blocks=[Block(text=t) for t in blocks],
            title="t", n_pages=None, extractor="x",
        )

    def test_source_file_is_never_touched(self, tmp_path):
        """最重要的一条：清洗只读源文件，不写。"""
        from ragkb.ingest import loader as L
        from ragkb.ingest.cleaner import clean_loaded, plan_from_dict

        src = tmp_path / "a.md"
        src.write_text("正文一段。\n\n第 12 页\n", encoding="utf-8")
        before = src.read_bytes()

        loaded = L.load_path(src)
        plan = plan_from_dict({"enabled": {"drop_page_no": True}})
        cleaned, _ = clean_loaded(loaded, plan)

        assert "第 12 页" not in cleaned.text
        assert src.read_bytes() == before, "源文件必须一个字节都不变"

    def test_default_rules_drop_only_junk(self):
        """默认规则只该清掉噪声，不能吃掉正文。"""
        from ragkb.ingest.cleaner import clean_loaded, default_plan

        doc = self._doc([
            "第一章 概述........... 12",
            "第 12 页",
            "![配图](a.png)",
            "维医的四种体液学说，是诊断与用药的基础。",
            "维医的四种体液学说，是诊断与用药的基础。",
            "   \n\n\n\n   ",
        ])
        cleaned, rep = clean_loaded(doc, default_plan())
        texts = [b.text for b in cleaned.blocks]
        assert any("四种体液" in t for t in texts), "正文不能被默认规则删掉"
        assert not any("第 12 页" in t for t in texts)
        assert not any("a.png" in t for t in texts)
        assert sum(1 for t in texts if "四种体液" in t) == 1, "重复块只留一处"
        assert rep.removed_chars > 0

    def test_every_rule_reports_what_it_removed(self):
        """账单要能指到具体规则：一句"共删了 300 字"没法让人判断删得对不对。"""
        from ragkb.ingest.cleaner import clean_loaded, plan_from_dict

        doc = self._doc(["第 7 页", "http://example.com/a/b", "正常正文内容足够长。"])
        plan = plan_from_dict({"enabled": {"drop_page_no": True, "strip_url": True}})
        _, rep = clean_loaded(doc, plan)
        payload = rep.as_dict()
        by_id = {r["id"]: r for r in payload["rules"]}
        assert by_id["drop_page_no"]["blocks"] == 1
        assert by_id["drop_page_no"]["samples"], "删了什么要给样例"
        assert by_id["strip_url"]["chars"] > 0

    def test_keyword_and_regex_rules(self):
        from ragkb.ingest.cleaner import clean_loaded, plan_from_dict

        doc = self._doc([
            "内部资料，禁止外传。",
            "参考文献列表",
            "这一段是正文，讲脉诊的基本方法。",
        ])
        plan = plan_from_dict({
            "enabled": {"drop_keywords": True, "drop_regex": True},
            "options": {"keywords": "内部资料", "regex": "^参考文献"},
        })
        cleaned, rep = clean_loaded(doc, plan)
        assert len(cleaned.blocks) == 1
        assert "脉诊" in cleaned.blocks[0].text
        assert {r["id"] for r in rep.rules} == {"drop_keywords", "drop_regex"}

    def test_bad_regex_is_rejected_before_anything_is_deleted(self):
        """正则写错必须明确报错，而不是静默"什么都没删"。"""
        import pytest

        from ragkb.ingest.cleaner import plan_from_dict

        plan = plan_from_dict({"enabled": {"drop_regex": True}, "options": {"regex": "([a-"}})
        with pytest.raises(ValueError):
            plan.validate()

    def test_repeated_lines_catches_header_footer(self):
        """页眉页脚靠"同一行出现多次"识别，不是靠猜格式。"""
        from ragkb.ingest.cleaner import clean_loaded, plan_from_dict

        doc = self._doc([
            "某某医院内部讲义\n第一段正文。",
            "某某医院内部讲义\n第二段正文。",
            "某某医院内部讲义\n第三段正文。",
        ])
        plan = plan_from_dict({"enabled": {"drop_repeated_lines": True}})
        cleaned, rep = clean_loaded(doc, plan)
        assert all("某某医院内部讲义" not in b.text for b in cleaned.blocks)
        assert all("正文" in b.text for b in cleaned.blocks), "块本身要保留"
        assert rep.as_dict()["rules"][0]["lines"] == 3

    def test_plan_roundtrip_and_unknown_rules_ignored(self):
        """前端可能把旧规则 id 发回来（规则改版过），不能因此炸掉。"""
        from ragkb.ingest.cleaner import plan_from_dict

        plan = plan_from_dict({"enabled": {"drop_page_no": True, "no_such_rule": True},
                              "options": {"short_min": "12"}})
        assert plan.on("drop_page_no")
        assert "no_such_rule" not in plan.enabled
        assert plan.opt("short_min") == 12, "数字参数要从字符串收敛成 int"


class TestModelRoles:
    """模型角色分工（2026-10-28）。

    维吾尔语问答要把"翻译"和"输出"分开：翻译要忠实（混元这类云端模型又快又稳），
    输出要会组织（本地模型读中文资料）。这里锁住三件事：
    1. 解析优先级：站点覆盖 > 系统默认 > 站点后端 > 当前默认。
    2. 配了不存在的后端不能炸，要静默退到下一级（拼错名字是最常见的错）。
    3. 站点真的按角色用了不同的后端 —— 这是整个模块的唯一目的。
    """

    def test_resolution_order(self):
        from ragkb.roles import resolve

        known = {"hunyuan": 1, "local": 1, "def": 1}
        got = resolve("translate", instance_roles={"translate": "local"},
                      global_roles={"translate": "hunyuan"},
                      instance_provider="def", default_provider="def", known=known)
        assert got == "local", "站点级覆盖优先级最高"

        got = resolve("translate", global_roles={"translate": "hunyuan"},
                      instance_provider="def", default_provider="def", known=known)
        assert got == "hunyuan", "没写站点级就用系统默认"

        got = resolve("translate", instance_provider="def", default_provider="def", known=known)
        assert got == "def"

    def test_unknown_provider_falls_through(self):
        """配了拼错/已删掉的后端名：退到下一级，不能让站点 500。"""
        from ragkb.roles import normalize, resolve

        known = {"def": 1}
        assert resolve("translate", instance_roles={"translate": "typo"},
                       global_roles={"translate": "gone"},
                       default_provider="def", known=known) == "def"
        assert normalize({"translate": "nope", "generate": "def", "bogus": "x"},
                         known=known) == {"generate": "def"}

    def test_site_uses_different_providers_per_role(self, tmp_path):
        """翻译走翻译模型、输出走输出模型 —— 分工的唯一验收标准。"""
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "gen-model"
            providers = {"tr-model": {}, "gen-model": {}}
            roles: dict = {}
            context_token_budget = 6000
            min_context_chars = 200

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _Out:
            def __init__(self, text="译文"):
                self.text = text

        calls: list[tuple[str, str]] = []

        class _LLM:
            def complete(self, msgs, provider=None, **kw):
                calls.append(("complete", provider or ""))
                return _Out("中文草稿")

            def stream(self, msgs, provider=None, **kw):
                calls.append(("stream", provider or ""))
                return iter(["جاۋاب"])

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"
            llm = _LLM()
            warm = {"state": "ready", "error": ""}

            def effective_cfg(self, kb_id):
                return self.cfg

            class embedder:
                is_loaded = True
                error = ""

            class engine:
                @staticmethod
                def search(conn, query, kb_id=None, cfg=None):
                    class R:
                        candidates = []
                        refusal = None
                        timings = {}
                        degraded = []
                    return R()

        spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8805, lang="ug",
                            answer_mode="translate",
                            roles={"translate": "tr-model", "generate": "gen-model"})
        client = TestClient(build_site(spec, _State()))
        info = client.get("/api/info").json()
        assert info["roles"]["translate"] == "tr-model"
        assert info["roles"]["generate"] == "gen-model"

        # 跑一次真实提问（检索为空会走拒答，但翻译那一步已经发生）
        with client.stream("POST", "/api/ask", json={"question": "مىزاج دېگەن نېمە؟"}) as r:
            for _ in r.iter_bytes():
                pass
        assert ("complete", "tr-model") in calls, f"翻译必须走翻译模型，实际：{calls}"



class _Refusal:
    """替身：分档与拒答逻辑只读这两个阈值。"""
    enabled = True
    min_score = 0.5
    min_margin = 0.02


class TestEvidenceGrade:
    """检索证据分档（2026-10-28）。

    模型必须知道"这批资料有多可信"：命中硬就照资料答，命中差就允许用自身知识
    但必须标明。这里锁住分档边界、指令内容，以及"低证据不再硬拒答"这件事。
    """

    def test_override_lands_in_system_prompt(self):
        """例外条款必须进系统提示——写进用户消息压不过"禁止外部知识"那条硬约束。"""
        from ragkb.generate.prompt import build_context

        def _bundle(evidence):
            return build_context(
                [], question="量子计算机的纠错码原理", token_budget=1000,
                prompt_version="qa_v1", answer_lang="zh", evidence=evidence,
            )

        mid = _bundle("mid")
        assert "本轮例外" in mid.system_prompt, "证据不足时系统提示要点名作废"
        assert "第 1、3 条本轮不适用" in mid.system_prompt
        assert "请依据上述资料作答" not in mid.user_prompt, "不能再催它照资料答"

        high = _bundle("high")
        assert "本轮例外" not in high.system_prompt, "证据充足时不该放开"
        assert "请依据上述资料作答" in high.user_prompt

    def test_no_grade_lets_model_surrender(self):
        """三档都必须禁止"资料里没有/无法回答"式收尾。

        实测漏洞：召回一般时模型照着"以资料为主"推演一圈，最后憋出一句
        "无法给出答案"。用户要的是答案，不是投降声明。
        """
        from ragkb.generate.evidence import guidance

        for g in ("high", "mid", "low"):
            text = guidance(g)
            assert "无法回答" in text, f"{g} 档要明令禁止投降：{text}"
            assert "资料里没有" in text or "资料中未找到" in text, f"{g} 档要指明退路"
        assert "资料整体与问题无关" in guidance("mid"), "中间档要覆盖'资料基本无关'的情况"

    def test_grade_boundaries(self):
        from ragkb.generate.evidence import grade_of

        assert grade_of(0.62, min_score=0.5) == "high"
        assert grade_of(0.50, min_score=0.5) == "high", "等于阈值算充足"
        assert grade_of(0.35, min_score=0.5) == "mid"
        assert grade_of(0.10, min_score=0.5) == "low"
        assert grade_of(None, min_score=0.5) == "low", "一条没召回 = 最低档"

    def test_low_guidance_tells_model_to_mark_general_knowledge(self):
        """低档指令的核心是"让用户分清哪句是资料、哪句是通用知识"。"""
        from ragkb.generate.evidence import guidance, label

        low = guidance("low")
        assert "通用知识" in low and "不要编造" in low
        assert "严格依据" in guidance("high")
        assert "资料中未找到" in guidance("mid")
        assert label("low", "ug"), "维语界面也要有标签"

    @staticmethod
    def _run(tmp_path, top_score, refused=True):
        """跑一次证据不足的提问，返回 (SSE 事件行, 送到模型的最后一条消息列表)。"""
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site
        from ragkb.models import RefusalDecision

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "gen-model"
            providers = {"gen-model": {}}
            roles: dict = {}
            context_token_budget = 6000
            min_context_chars = 200
            prompt_version = "qa_v1"

        class _Refusal:
            enabled = True
            min_score = 0.5
            min_margin = 0.02

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        seen: list[str] = []

        class _LLM:
            def complete(self, msgs, provider=None, **kw):
                seen.append(msgs[-1]["content"])
                class O:
                    text = "这是通用知识作答的示例。"
                return O()

            def stream(self, msgs, provider=None, **kw):
                seen.append(msgs[-1]["content"])
                yield "جاۋاب"

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"
            warm = {"state": "ready", "error": ""}

            def effective_cfg(self, kb_id):
                return self.cfg

            class embedder:
                is_loaded = True
                error = ""

            class engine:
                @staticmethod
                def search(conn, query, kb_id=None, cfg=None):
                    class R:
                        candidates = []
                        timings = {}
                        degraded = []
                    R.refusal = RefusalDecision(refused=refused, reason="low_score",
                                                top_score=top_score, margin=0.0,
                                                calibrated=False)
                    return R()

        _State.llm = _LLM()
        spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8808, lang="ug",
                            answer_mode="translate")
        client = TestClient(build_site(spec, _State()))
        events = []
        with client.stream("POST", "/api/ask",
                           json={"question": "مىزاج دېگەن نېمە؟"}) as r:
            for line in r.iter_lines():
                events.append(line)
        return events, seen

    def test_low_evidence_answers_instead_of_refusing(self, tmp_path):
        """证据不足时不再回一句"资料里没有"，而是如实作答并标档位。"""
        events, seen = self._run(tmp_path, top_score=0.05)

        meta = next((e for e in events if '"answer_lang"' in e), "")
        assert '"evidence": "low"' in meta, f"低证据要标出来：{meta}"
        assert '"soft_refused": true' in meta
        assert '【检索证据：不足】' in "".join(seen), "低证据指令必须下发到模型"
        assert any('"refused": false' in e for e in events), "低证据不该再硬拒答"

    def test_mid_evidence_also_answers(self, tmp_path):
        """中间档同样要答——召回更差的有答案、稍好的反而拒答，是倒挂。"""
        events, seen = self._run(tmp_path, top_score=0.32)

        meta = next((e for e in events if '"answer_lang"' in e), "")
        assert '"evidence": "mid"' in meta, f"中间档要标出来：{meta}"
        assert '【检索证据：一般】' in "".join(seen), "中间档指令必须下发到模型"
        assert not any('"refused": true' in e for e in events), "前台不该有硬拒答"
        assert any('"refused": false' in e for e in events)

    def test_high_evidence_is_strict(self, tmp_path):
        """命中够硬时反过来：严格照资料答，不补资料外的东西。"""
        events, seen = self._run(tmp_path, top_score=0.62, refused=False)

        meta = next((e for e in events if '"answer_lang"' in e), "")
        assert '"evidence": "high"' in meta, f"高档要标出来：{meta}"
        assert '"soft_refused": false' in meta
        assert '【检索证据：充足】' in "".join(seen), "高档指令要求严格照资料"
        assert '【检索证据：不足】' not in "".join(seen), "够硬时不该放开自身知识"


class TestLanguageFollow:
    """回答跟随提问的语言（2026-10-28）。

    用户拿中文问维医，就该得到中文答案——把他的问题硬翻成维吾尔语再让他读
    维吾尔语，是把"站点语言"错当成了"用户语言"。
    """

    @staticmethod
    def _client(tmp_path, llm):
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "gen-model"
            providers = {"gen-model": {}}
            roles: dict = {}
            context_token_budget = 6000
            min_context_chars = 200

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"
            warm = {"state": "ready", "error": ""}

            def effective_cfg(self, kb_id):
                return self.cfg

            class embedder:
                is_loaded = True
                error = ""

            class engine:
                @staticmethod
                def search(conn, query, kb_id=None, cfg=None):
                    class R:
                        candidates = []
                        refusal = None
                        timings = {}
                        degraded = []
                    return R()

        # 类体里写 llm = llm 会 NameError：class 体用 LOAD_NAME，读不到外层局部变量
        _State.llm = llm

        spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8806, lang="ug",
                            answer_mode="translate")
        return TestClient(build_site(spec, _State()))

    def _collect(self, client, question):
        """跑一次 SSE，返回 (meta, done)。"""
        import json as _json

        meta = done = None
        with client.stream("POST", "/api/ask", json={"question": question}) as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    try:
                        payload = _json.loads(line[6:])
                    except ValueError:
                        continue
                    if "answer_lang" in payload and "mode" in payload:
                        meta = payload
                    elif "trace_id" in payload and "citations" in payload:
                        done = payload
        return meta, done

    def test_chinese_question_gets_chinese_answer(self, tmp_path):
        """中文提问 → 直接中文答，不进两段式（连翻译那步都省掉）。"""

        class _Out:
            text = "维医的四种体液是血液质、黏液质、胆液质、黑胆质。"

        calls: list[str] = []

        class _LLM:
            def complete(self, msgs, provider=None, **kw):
                calls.append("complete:" + (provider or ""))
                return _Out()

            def stream(self, msgs, provider=None, **kw):
                calls.append("stream:" + (provider or ""))
                yield "中文答案"

        meta, done = self._collect(self._client(tmp_path, _LLM()), "维医中的四种体液是什么？")
        assert meta["answer_lang"] == "zh", f"中文问题必须用中文答：{meta}"
        assert meta["two_stage"] is False, "中文问 + 中文语料 = 不需要翻译"
        assert done["draft"] is None
        assert not any(c.startswith("complete") for c in calls), "没有翻译环节"

    def test_uyghur_question_still_two_stage_and_ships_draft(self, tmp_path):
        """维语提问照旧走两段式，且 translating 事件把中文草稿带给前端
        （中文界面下前端要立刻显示草稿，不能让人盯着维语译文干等）。"""

        class _Out:
            text = "بۇ بىر جاۋاب"

        class _LLM:
            def complete(self, msgs, provider=None, **kw):
                return _Out()

            def stream(self, msgs, provider=None, **kw):
                yield "جاۋاب"

        raw: list[str] = []
        client = self._client(tmp_path, _LLM())
        with client.stream("POST", "/api/ask", json={"question": "مىزاج دېگەن نېمە؟"}) as r:
            for line in r.iter_lines():
                raw.append(line)
        assert any('"stage": "translating"' in x and '"draft"' in x for x in raw), \
            "translating 事件必须携带中文草稿"
        meta = next(_l for _l in raw if '"answer_lang"' in _l)
        assert '"answer_lang": "ug"' in meta
    """维吾尔语前台（2026-10-28）。

    用户要的是"维吾尔语问、维吾尔语答"的前台，且不能和后台混在一起。
    这里锁住三件最容易悄悄退化的事：
    1. 语言别判错——判成中文的话，维语问题既不会被翻译、也不会用维语提示。
    2. 翻译回显要被拒——本地小模型常把提示词复读回来，那玩意儿当检索词
       只会召回噪声，而且界面上完全看不出来。
    3. 实例端口不能撞后台（8000）或 vLLM（8100）。
    """

    def test_detect_uyghur_vs_chinese(self):
        from ragkb.lang import detect

        assert detect("مىزاج دېگەن نېمە؟") == "ug"
        assert detect("维吾尔医学的四种体液是什么？") == "zh"
        assert detect("What is mizaj?") == "en"
        # 中文句子里夹一两个阿拉伯字母不该被判成维语
        assert detect("维医中的 ې 符号说明") == "zh"

    def test_translation_echo_is_rejected(self):
        """模型把整个提示吐回来时要当没翻译，而不是拿去检索。"""
        from ragkb.lang import translate_query

        class _Out:
            text = "源语言代码：ug\n问题：مىزاج دېگەن نېمە？\n\n中文译文：气质是什么"

        class _LLM:
            def complete(self, *a, **k):
                return _Out()

        got, err = translate_query(_LLM(), "مىزاج دېگەن نېمە؟", src="ug")
        assert got == "مىزاج دېگەن نېمە؟", "回显必须判为失败并退回原文"
        # 丢弃原因必须带出来。以前这里是 err is None，等于把"翻译被丢了"
        # 伪装成"本来就不用翻"，排查时完全看不出区别。
        assert err and "translation_dropped" in err and "回显" in err

    def test_translation_retry_after_echo(self):
        """第一次抄回原文，第二次必须重试并拿到译文（不复用那次的坏输出）。"""
        from ragkb.lang import translate_query

        calls = []

        class _LLM:
            def complete(self, *a, **k):
                calls.append(k)

                class _Out:
                    text = "مىزاج دېگەن نېمە؟" if len(calls) == 1 else "气质是什么？"

                return _Out()

        got, err = translate_query(_LLM(), "مىزاج دېگەن نېمە؟", src="ug")
        assert got == "气质是什么？", "第二次拿到译文就该采用"
        assert err is None
        assert len(calls) == 2, "抄回之后必须重试一次，不能直接放弃"

    def test_translation_gives_up_after_two(self):
        """两次都抄回：退回原文，并说清原因，且不超过两次。"""
        from ragkb.lang import translate_query

        calls = []

        class _LLM:
            def complete(self, *a, **k):
                calls.append(1)

                class _Out:
                    text = "مىزاج دېگەن نېمە؟"

                return _Out()

        got, err = translate_query(_LLM(), "مىزاج دېگەن نېمە؟", src="ug")
        assert got == "مىزاج دېگەن نېمە؟"
        assert len(calls) == 2, "最多重试一次，别在翻译上无限消耗"
        assert err and "translation_dropped" in err and "抄回" in err

    def test_clean_translation_is_kept(self):
        from ragkb.lang import translate_query

        class _Out:
            text = "译文：维医中的气质是什么？"

        class _LLM:
            def complete(self, *a, **k):
                return _Out()

        got, _ = translate_query(_LLM(), "مىزاج دېگەن نېمە؟", src="ug")
        assert got == "维医中的气质是什么？", "正常译文要保留，并去掉「译文：」前缀"

    def test_translation_failure_never_raises(self):
        """翻译是加分项：翻译服务挂了也要能继续回答。"""
        from ragkb.lang import translate_query

        class _LLM:
            def complete(self, *a, **k):
                raise RuntimeError("连接失败")

        got, err = translate_query(_LLM(), "سوئال", src="ug")
        assert got == "سوئال"
        assert "RuntimeError" in (err or "")

    def test_ug_system_prompt_exists_and_is_used(self):
        """维语站点的系统提示必须是维语版，否则模型照着中文资料吐中文。"""
        from ragkb.generate.prompt import load_system_prompt

        zh = load_system_prompt("qa_v1")
        ug = load_system_prompt("qa_v1", answer_lang="ug")
        assert ug != zh
        assert "ئۇيغۇر تىلىدا" in ug

    def test_repeating_detection(self):
        from ragkb.lang import repeating

        assert repeating("正气正气正气" * 10)
        assert not repeating("正气、血液质、胆液质是维医的三种基本气质类型。")
        # 正常排比：同一个词在一段话里出现三次，但**不相邻**。
        # 旧实现按"出现次数"数，把这种句子判成复读从中间砍掉，
        # 用户看到的就是一句没说完的话（维语译文里非常常见）。
        para = ("مەنبەلەردە بۇ ھەقتە ئۇچۇر يوق. يەنە مەنبەلەردە باشقا "
                "ئۇچۇر يوق. شۇڭا مەنبەلەردە دېيىلگەن نەرسە ئاز.")
        assert not repeating(para)
        # 真的复读：同一段紧挨着重复
        assert repeating(para[-40:] * 4)

    # ---------------------------------------------------------- 实例配置

    def test_port_range_excludes_backend_and_vllm(self, monkeypatch, tmp_path):
        from ragkb.frontsite import instcfg

        monkeypatch.setattr(instcfg, "INSTANCES_CONFIG", tmp_path / "instances.yaml")
        assert instcfg.PORT_MIN > 8100, "不能撞后台 8000，也不能撞 vLLM 8100"

        spec = instcfg.InstanceSpec(name="xx", kb_id=1, port=8000)
        try:
            instcfg.validate(spec, kb_exists=lambda i: True)
        except ValueError as exc:
            assert "8800" in str(exc), "报错要写出允许的端口区间，而不只是说不行"
        else:
            raise AssertionError("端口 8000 必须被拒")

    def test_bad_config_entry_is_skipped_not_fatal(self, monkeypatch, tmp_path):
        """手写坏了一行配置，不能让整个后台起不来。"""
        from ragkb.frontsite import instcfg

        p = tmp_path / "instances.yaml"
        p.write_text(
            "instances:\n"
            "  - name: 'Bad Name'\n    kb_id: 1\n    port: 99999\n"
            "  - name: ok\n    kb_id: 1\n    port: 8801\n",
            encoding="utf-8")
        monkeypatch.setattr(instcfg, "INSTANCES_CONFIG", p)
        items = instcfg.load_all()
        assert [i.name for i in items] == ["ok"]

    def test_next_free_port_avoids_taken_ones(self, monkeypatch, tmp_path):
        import socket

        from ragkb.frontsite import instcfg

        monkeypatch.setattr(instcfg, "INSTANCES_CONFIG", tmp_path / "instances.yaml")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        taken = listener.getsockname()[1]
        try:
            port = instcfg.next_free_port()
            assert port != taken
            assert instcfg.PORT_MIN <= port <= instcfg.PORT_MAX
        finally:
            listener.close()

    def test_site_info_reports_rtl_and_binding(self, tmp_path):
        """前台 /api/info 是页面渲染的第一依赖：dir/lang 错了整页就是反的。"""
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name, description) VALUES ('维医','x')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        # TestClient 在另一个线程里跑应用，而 sqlite 连接不能跨线程用。
        # 真实的 Database.conn() 本来就是每线程一条，这里照它的样子给新的。
        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "mock"

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"

            # /api/info 现在按"这个知识库"的有效配置算 roles（和 /api/ask 一致），
            # 桩里没有这个方法就会吃 AttributeError
            def effective_cfg(self, kb_id):
                return self.cfg

        spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8801, lang="ug")
        client = TestClient(build_site(spec, _State()))
        info = client.get("/api/info").json()
        assert info["dir"] == "rtl" and info["lang"] == "ug"
        assert info["kb_name"] == "维医"
        assert info["cross_lingual"] is True, "维语问 + 中文语料 = 必须走翻译检索"
        assert client.get("/").status_code == 200

    def test_site_info_language_follows_content(self, tmp_path):
        """语言标签必须跟着**文案内容**走（2026-10-29）。

        配置里的 lang 是手填的，文案也是手填的，没有任何机制保证两者一致。
        线上撞到的那份就是 `lang: zh` + 维语标题/欢迎语/示例，而 alt 里反
        过来是中文。前端拿 lang 决定高亮哪颗按钮、拿文案渲染页面，于是
        "高亮中文、正文维语"；点一下切换，内容换了、高亮没换。用户没有
        任何办法把它纠正回来——这类错配只能由代码按字符集判定。
        """
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "mock"

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"

            def effective_cfg(self, kb_id):
                return self.cfg

        st = _State()
        ug_title = "ئۇيغۇر تېبابىتى بىلىم سوئال-جاۋاب سىستېمىسى"
        zh_title = "维医知识问答"

        def info_of(**kw):
            spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8801, **kw)
            return TestClient(build_site(spec, st)).get("/api/info").json()

        # ① 声明中文、文案是维语 → 标签必须判成维语（不是反过来）
        i1 = info_of(lang="zh", title=ug_title)
        assert i1["lang"] == "ug", "语言标签跟着内容走，不跟着手填的 lang"
        assert i1["site_lang"] == "zh", "配置里那个值仍然要能看到，排查时要用"
        assert i1["dir"] == "rtl"
        # 另一套这时没填，应补成中文（不是补成和主语言一样的维语）
        assert i1["alt"]["lang"] == "zh"
        assert "维医" in i1["alt"]["title"] or "ئۇيغۇر" not in i1["alt"]["title"]

        # ② 两套文案各填各的：标签必须分别对应各自的内容
        i2 = info_of(lang="ug", title=ug_title,
                     alt={"lang": "zh", "title": zh_title})
        assert i2["lang"] == "ug" and i2["alt"]["lang"] == "zh"
        assert i2["title"] == ug_title and i2["alt"]["title"] == zh_title

        # ③ 两套填成同一语言 = 切换按钮点下去什么都不会变。
        #    判重后自动把另一套换成另一种语言，用内置文案补齐。
        i3 = info_of(lang="ug", title=ug_title,
                     alt={"lang": "ug", "title": ug_title})
        assert i3["lang"] != i3["alt"]["lang"], "两颗按钮不能是同一门语言"
        assert i3["alt"]["title"] and i3["alt"]["title"] != ug_title

        # ④ 知识库显示名也要能按语言分开配：维语界面挂四个汉字最扎眼
        i4 = info_of(lang="ug", title=ug_title, kb_name="ئۇيغۇر تېبابىتى ئامبىرى",
                     alt={"lang": "zh", "kb_name": "维医知识库"})
        assert i4["kb_name"] == "ئۇيغۇر تېبابىتى ئامبىرى"
        assert i4["alt"]["kb_name"] == "维医知识库"
        assert i4["kb_name_raw"] == "维医", "库里那个名字不能被改写"

    def test_site_info_uses_kb_level_provider(self, tmp_path):
        """/api/info 也必须按知识库级后端算 roles（2026-10-29）。

        知识库可以单独钉一个后端（kb.config_json 的 generation.provider）。
        如果 info 用全局 provider、ask 用 kb 级 provider，页面就会显示"百炼"
        而实际去连本地的 1235 端口——页面一切正常、问答全失败，排查的人会
        先去怀疑密钥写错了。两个接口必须走同一条取值路径。
        """
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _Global:
            provider = "global-provider"
            providers = {"global-provider": {}, "kb-provider": {}}
            roles: dict = {}

        class _Kb:
            provider = "kb-provider"
            providers = {"global-provider": {}, "kb-provider": {}}
            roles: dict = {}

        class _Cfg:
            generation = _Global()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _KbCfg:
            generation = _Kb()
            refusal = _Refusal()
            prompt_version = "qa_v1"

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"

            def effective_cfg(self, kb_id):
                return _KbCfg()

        spec = InstanceSpec(name="uytest", kb_id=kb_id, port=8801, lang="ug")
        info = TestClient(build_site(spec, _State())).get("/api/info").json()
        assert info["roles"]["generate"] == "kb-provider", "info 用了全局后端，会和 ask 不一致"
        assert info["roles"]["translate"] == "kb-provider"

    def test_site_info_carries_alt_copy_for_language_switch(self, tmp_path):
        """语言按钮是**整站切换**：/api/info 必须带另一套语言的文案。

        只换答案语言的话，在没有回答的空页面上点它等于什么都没发生——
        用户看到的就是"点了不切换"。
        """
        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()

        import sqlite3

        db_path = cfg.db_path if hasattr(cfg, "db_path") else cfg.storage.db_path

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "mock"

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"

            # /api/info 现在按"这个知识库"的有效配置算 roles（和 /api/ask 一致），
            # 桩里没有这个方法就会吃 AttributeError
            def effective_cfg(self, kb_id):
                return self.cfg

        spec = InstanceSpec(
            name="uytest", kb_id=kb_id, port=8802, lang="ug", answer_mode="translate",
            alt={"lang": "zh", "title": "维医知识问答", "welcome": "你好！"},
        )
        info = TestClient(build_site(spec, _State())).get("/api/info").json()
        assert info["lang"] == "ug", "主语言不变"
        assert info["alt"]["lang"] == "zh"
        assert info["alt"]["dir"] == "ltr"
        assert info["alt"]["title"] == "维医知识问答", "自定义文案要原样下发"
        # 没写的字段按语言默认值补齐，前端不用自己做兜底
        assert info["alt"]["welcome"] == "你好！"
        assert info["alt"]["samples"], "示例问题不能是空的，否则切过去没东西可点"

    def test_alt_copy_ignores_junk(self, monkeypatch, tmp_path):
        """alt 写了乱七八糟的东西不能让实例起不来。"""
        from ragkb.frontsite import instcfg

        p = tmp_path / "instances.yaml"
        p.write_text(
            "instances:\n"
            "  - name: ok\n    kb_id: 1\n    port: 8803\n    lang: ug\n"
            "    alt: '这不是字典'\n"
            "  - name: ok2\n    kb_id: 1\n    port: 8804\n    lang: ug\n"
            "    alt:\n      lang: badlang\n      title: 标题\n"
            "      samples: ['一', '二']\n",
            encoding="utf-8")
        monkeypatch.setattr(instcfg, "INSTANCES_CONFIG", p)
        items = {i.name: i for i in instcfg.load_all()}
        assert items["ok"].alt == {}, "非字典的 alt 一律当没配"
        assert items["ok2"].alt["samples"] == ["一", "二"]
        assert "lang" not in items["ok2"].alt, "语言代码不合法就丢掉，交给站点默认"

    def test_instance_start_and_stop_releases_the_port(self, tmp_path):
        """启停是真的：启动后端口可达，停止后必须释放，否则下次起不来。"""
        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.runner import InstanceManager, port_in_use

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('维医')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]

        class _DB:
            def conn(self):
                return conn

        class _G:
            provider = "mock"

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"

        import ragkb.frontsite.instcfg as instcfg

        orig = instcfg.INSTANCES_CONFIG
        instcfg.INSTANCES_CONFIG = tmp_path / "instances.yaml"
        try:
            port = instcfg.next_free_port()
            instcfg.upsert(InstanceSpec(name="life", kb_id=kb_id, port=port, lang="ug"))
            mgr = InstanceManager(_State())
            st = mgr.start("life")
            assert st["status"] == "running", st.get("error")
            assert port_in_use(port)
            assert mgr.stop("life")["status"] == "stopped"
            assert not port_in_use(port)
        finally:
            instcfg.INSTANCES_CONFIG = orig


class TestThinkStrip:
    """思考块剥离（2026-10-28）。

    思考型模型会把 <think>…</think> 连同答案一起返回。漏进前端不只是"多一堆
    自言自语"——草稿是中文、容器是 RTL 维吾尔语时，这段中文被双向算法一搅
    就是一坨看着像倒着的乱字。所以必须在 LLM 层滤掉。
    """

    def test_strip_balanced_block(self):
        from ragkb.generate.think import strip

        raw = "<think>让我想想这个问题。</think>真正的答案在这里。"
        assert strip(raw) == "真正的答案在这里。", "思考块要整段去掉"
        assert strip("没有思考块的正常回答。") == "没有思考块的正常回答。"

    def test_strip_unclosed_block(self):
        """模型没写闭合标签 = 整段都在推理，全丢。"""
        from ragkb.generate.think import strip

        assert strip("<think>一直想到天荒地老") == ""

    def test_stream_tag_split_across_chunks(self):
        """标签被切在两个 chunk 之间是最容易漏的情况。"""
        from ragkb.generate.think import ThinkFilter

        f = ThinkFilter()
        out = ""
        for piece in ["前面正常。", "<thi", "nk>推理中", "</th", "ink>后面答案"]:
            out += f.feed(piece)
        out += f.flush()
        assert out == "前面正常。后面答案", f"跨 chunk 的标签也要滤掉：{out!r}"

    def test_stream_no_think_passes_through(self):
        from ragkb.generate.think import ThinkFilter

        f = ThinkFilter()
        out = "".join(f.feed(p) for p in ["維医", "的四种", "体液"]) + f.flush()
        assert out == "維医的四种体液", f"没有思考块时不能吞字：{out!r}"

    def test_stream_unclosed_think_discards_all(self):
        from ragkb.generate.think import ThinkFilter

        f = ThinkFilter()
        out = "".join(f.feed(p) for p in ["<think>", "想啊想"]) + f.flush()
        assert out == "", f"未闭合的思考块不该漏出去：{out!r}"

    def test_case_insensitive(self):
        from ragkb.generate.think import strip

        assert strip("<THINK>推理</THINK>答案") == "答案"


class TestLLMThinkWiring:
    """思考块剥离必须真的接在 LLM 的流上。

    ThinkFilter 本身测过了，但"接没接上"是另一回事——方法名写错这种低级错误
    只有跑通真实代码路径才暴露得出来（线上就因此抛过一次 AttributeError）。
    """

    @staticmethod
    def _client(monkeypatch, pieces):
        import json

        from ragkb.config import Config, ProviderCfg
        from ragkb.generate.llm import LLMClient
        import ragkb.generate.llm as llm_mod

        cfg = Config()
        cfg.generation.providers["fake"] = ProviderCfg(
            base_url="http://127.0.0.1:9999/v1", model="qwen", kind="openai")
        cfg.generation.provider = "fake"

        lines = ["data: " + json.dumps({"choices": [{"delta": {"content": p}}]})
                 for p in pieces] + ["data: [DONE]"]

        class FakeResp:
            status_code = 200
            def read(self): return b""
            def iter_lines(self): return iter(lines)

        class FakeStream:
            def __enter__(self): return FakeResp()
            def __exit__(self, *a): return False

        class FakeClient:
            def __init__(self, *a, **k): pass
            def stream(self, *a, **k): return FakeStream()
            def close(self): pass

        monkeypatch.setattr(llm_mod.httpx, "Client", FakeClient)
        return LLMClient(cfg)

    def test_stream_drops_think_block(self, monkeypatch):
        c = self._client(monkeypatch, ["<think>推理中", "</think>", "真正的答案"])
        out = "".join(c.stream([{"role": "user", "content": "hi"}]))
        assert out == "真正的答案", f"真实流路径上也要滤掉思考块：{out!r}"

    def test_stream_without_think_is_intact(self, monkeypatch):
        c = self._client(monkeypatch, ["维医", "四种体液"])
        out = "".join(c.stream([{"role": "user", "content": "hi"}]))
        assert out == "维医四种体液", f"没有思考块时不能吞字：{out!r}"


class TestOrphanThinkTag:
    """孤立闭合标签（2026-10-28）。

    模板可能在提示里偷偷开一个 <think>，于是补全流里只有 </think> 没有开标签。
    这种标签必须吃掉——它混进 RTL 维吾尔语容器里就是一堆看着像倒着的乱字。
    """

    def test_orphan_close_tag_is_dropped(self):
        from ragkb.generate.think import strip

        assert strip("正文</think>") == "正文"
        assert "think" not in strip("答案A\n</think>\n\n答案A").lower()

    def test_orphan_close_in_stream(self):
        from ragkb.generate.think import ThinkFilter

        f = ThinkFilter()
        out = "".join(f.feed(p) for p in ["答案。</", "think>", "收尾"]) + f.flush()
        assert "<" not in out and "think" not in out.lower(), f"标签不该漏出去：{out!r}"
        assert out == "答案。收尾"

class TestSessionKeys:
    """密钥只活在内存里（2026-10-28 改）。

    改版原因：把密钥写进配置文件 / .env 都会被"保存设置"回写、被导出、被截图。
    现在密钥只存在进程内存（ragkb.secrets），服务一停就没了，重启需重填。
    这类"故意不持久化"的设计必须被测试钉住，否则某次重构顺手加个落盘就白做了。
    """

    def setup_method(self):
        from ragkb import secrets

        secrets.clear()

    def teardown_method(self):
        from ragkb import secrets

        secrets.clear()

    def test_put_get_drop(self):
        from ragkb import secrets

        secrets.put("deepseek", "sk-abc")
        assert secrets.get("deepseek") == "sk-abc"
        assert secrets.has("deepseek")
        secrets.drop("deepseek")
        assert secrets.get("deepseek") is None
        secrets.put("a", "1")
        secrets.put("b", "2")
        secrets.clear()
        assert secrets.names() == [], "清空后不留任何名字"

    def test_empty_key_is_ignored(self):
        """空串当"没填"，不能把已有密钥冲成一个空字符串。"""
        from ragkb import secrets

        secrets.put("x", "sk-real")
        secrets.put("x", "   ")
        assert secrets.get("x") == "sk-real"

    def test_api_key_resolution_order(self):
        """临时密钥 > 内存密钥 > 环境变量；后端名决定查哪一条。"""
        from ragkb import secrets
        from ragkb.config import ProviderCfg

        p = ProviderCfg(base_url="https://api.x/v1", model="m", kind="openai")
        assert p.api_key("x") is None

        secrets.put("x", "sk-mem")
        assert p.api_key("x") == "sk-mem"
        assert p.api_key("other") is None, "别的后端名不能拿到这份密钥"

        p.set_inline_key("sk-inline")
        assert p.api_key("x") == "sk-inline", "探测时临时填的优先"

    def test_key_never_lands_in_config_dump(self):
        """密钥不能出现在任何会写进 YAML 的序列化结果里。"""
        import json

        from ragkb import secrets
        from ragkb.config import Config, ProviderCfg

        secrets.put("x", "sk-MUST-NOT-LEAK")
        cfg = Config()
        cfg.generation.providers["x"] = ProviderCfg(
            base_url="https://api.x/v1", model="m", kind="openai")
        blob = json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False)
        assert "sk-MUST-NOT-LEAK" not in blob, "密钥泄漏到了配置序列化结果里"

    def test_upsert_stores_key_in_memory_only(self, tmp_path, monkeypatch):
        """走一遍真实的保存接口：密钥进内存、文件里没有。"""
        from fastapi.testclient import TestClient

        from ragkb import modelcfg, secrets
        from ragkb.api import app as app_mod

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        client = TestClient(app_mod.app)
        r = client.put("/api/models/deepseek", json={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "kind": "deepseek", "api_key": "sk-upsert-secret",
        })
        assert r.status_code == 200, r.text
        assert secrets.get("deepseek") == "sk-upsert-secret"
        text = (tmp_path / "models.yaml").read_text(encoding="utf-8")
        assert "sk-upsert-secret" not in text, "密钥被写进了 models.yaml"

    def test_clear_all_keys_endpoint(self):
        from fastapi.testclient import TestClient

        from ragkb import secrets
        from ragkb.api import app as app_mod

        secrets.put("a", "sk-1")
        secrets.put("b", "sk-2")
        r = TestClient(app_mod.app).post("/api/models/keys/clear", json={})
        assert r.status_code == 200
        assert sorted(r.json()["cleared"]) == ["a", "b"]
        assert secrets.names() == []


class TestChatModelFilter:
    """列表里能对话的模型 ≠ 能点的模型（2026-10-28）。

    实测：百炼的 /models 同时返回图像、语音、实时、向量类模型。用户从下拉里
    点到 `qwen3.8-omni-flash-realtime`，对话直接 500，而报错还写着"检查密钥
    权限"——把人往密钥上带。所以列出来之前必须先把非对话类筛掉。
    """

    def test_filters_non_chat_models(self):
        from ragkb.services import split_chat_models

        ids = [
            "qwen-plus", "qwen3.8-omni-flash", "glm-5.3-prime",
            "qwen3.8-omni-flash-realtime", "qwen-image-2.1-pro",
            "text-embedding-v4", "paraformer-v2", "qwen-audio-3.1-realtime-flash",
        ]
        chat, hidden = split_chat_models(ids)
        assert chat == ["qwen-plus", "qwen3.8-omni-flash", "glm-5.3-prime"]
        assert hidden == 5

    def test_omni_itself_is_kept(self):
        """`omni` 不能一刀切：实测 qwen3.8-omni-flash 能对话，坏的是 -realtime 那支。"""
        from ragkb.services import split_chat_models

        chat, _ = split_chat_models(["qwen3.8-omni-flash"])
        assert chat == ["qwen3.8-omni-flash"]


class TestModelListAndRemember:
    """模型清单 + 可选"记住密钥"（2026-10-28，参考 ccswitch 的配法）。

    要点：清单是**人维护的候选表**，不是探测返回的原始列表；密钥默认不落盘，
    只有显式勾了才写 data/secrets.json。
    """

    def setup_method(self):
        from ragkb import secrets

        secrets.clear()
        secrets.forget()

    def teardown_method(self):
        from ragkb import secrets

        secrets.wipe_file()

    def test_clean_model_list_dedupes_and_tolerates_junk(self):
        from ragkb.modelcfg import _clean_model_list

        got = _clean_model_list([
            {"id": "qwen-plus", "context": "128000", "max_output": 8192},
            {"id": "qwen-plus"},              # 重复
            {"id": "   "},                    # 空
            "not-a-dict",                     # 脏数据
            {"id": "kimi-k3", "context": "128k"},   # 带单位的写法救不回来，留空
        ])
        assert [m.id for m in got] == ["qwen-plus", "kimi-k3"]
        assert got[0].context == 128000 and got[0].max_output == 8192
        assert got[1].context is None

    def test_model_list_round_trip_through_yaml(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from ragkb import modelcfg
        from ragkb.api import app as app_mod

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        c = TestClient(app_mod.app)
        r = c.put("/api/models/bailian", json={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus", "kind": "bailian",
            "model_list": [{"id": "qwen-plus", "context": 128000, "max_output": 8192},
                           {"id": "glm-5.3-prime"}],
        })
        assert r.status_code == 200, r.text
        me = [p for p in r.json()["listing"]["providers"] if p["name"] == "bailian"][0]
        assert [m["id"] for m in me["model_list"]] == ["qwen-plus", "glm-5.3-prime"]
        text = (tmp_path / "models.yaml").read_text(encoding="utf-8")
        assert "qwen-plus" in text and "glm-5.3-prime" in text

    def test_remember_true_writes_file_false_deletes_it(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from ragkb import modelcfg, secrets
        from ragkb.api import app as app_mod

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        secrets.attach(tmp_path)
        c = TestClient(app_mod.app)

        r = c.put("/api/models/deepseek", json={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "kind": "deepseek", "api_key": "sk-remember-me", "remember": True,
        })
        assert r.status_code == 200, r.text
        assert secrets.persisted(), "勾了『记住』就该有盘上的文件"
        assert "sk-remember-me" in (tmp_path / "secrets.json").read_text(encoding="utf-8")

        r = c.put("/api/models/deepseek", json={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "kind": "deepseek", "remember": False,
        })
        assert r.status_code == 200
        assert not secrets.persisted(), "取消勾选要删掉盘上的文件"
        assert secrets.get("deepseek") == "sk-remember-me", "内存里那份不该被顺手清掉"

    def test_default_does_not_touch_disk(self, tmp_path, monkeypatch):
        """不传 remember 时绝不能偷偷落盘——默认必须是纯内存。"""
        from fastapi.testclient import TestClient

        from ragkb import modelcfg, secrets
        from ragkb.api import app as app_mod

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        secrets.attach(tmp_path)
        c = TestClient(app_mod.app)
        c.put("/api/models/deepseek", json={
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
            "kind": "deepseek", "api_key": "sk-memory-only",
        })
        assert secrets.get("deepseek") == "sk-memory-only"
        assert not secrets.persisted(), "没勾就落盘 = 违背『停服即清空』"


class TestSwitchModelKeepsList:
    """卡片上切模型不能把清单清空（2026-10-28）。

    回归点：切模型只 PUT 了 model 一个字段，后端把"没传 model_list"当成清空，
    切一次模型人维护的清单就没了。现在 None = 不动，[] = 显式清空。
    """

    def test_none_keeps_existing_list(self, tmp_path, monkeypatch):
        from ragkb import modelcfg
        from ragkb.config import Config, ProviderCfg

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        cfg = Config()
        cfg.generation.providers["tt"] = ProviderCfg(
            base_url="https://api.x/v1", model="a", kind="openai",
            model_list=[{"id": "a", "context": 1000}, {"id": "b"}],
        )
        modelcfg.upsert(cfg, "tt", base_url="https://api.x/v1", model="b", kind="openai")
        got = [m.id for m in cfg.generation.providers["tt"].model_list]
        assert got == ["a", "b"], f"切模型把清单弄丢了：{got}"

    def test_empty_list_clears(self, tmp_path, monkeypatch):
        from ragkb import modelcfg
        from ragkb.config import Config, ProviderCfg

        monkeypatch.setattr(modelcfg, "MODELS_CONFIG", tmp_path / "models.yaml")
        cfg = Config()
        cfg.generation.providers["tt"] = ProviderCfg(
            base_url="https://api.x/v1", model="a", kind="openai",
            model_list=[{"id": "a"}],
        )
        modelcfg.upsert(cfg, "tt", base_url="https://api.x/v1", model="a",
                        kind="openai", model_list=[])
        assert cfg.generation.providers["tt"].model_list == []


class TestFormerlyHardcodedParams:
    """2026-10-30：把散落在代码里的魔法数字接进参数注册表。

    这类改动的失败模式是「面板上有、后端不读」——调了没反应，比没有这个选项
    更糟。所以每条断言都要求**改配置真的改变行为**，而不是只检查 key 存在。
    """

    def _cfg(self):
        return defaults_config().model_copy(deep=True)

    def test_all_new_paths_registered(self):
        from ragkb import settings

        keys = {p.path for p in settings.PARAMS}
        for k in (
            "refusal.margin_window",
            "refusal.soft_margin",
            "refusal.soft_floor",
            "evidence.mid_ratio",
            "generation.min_context_chars",
            "generation.disable_thinking",
            "chat.history_token_budget",
            "retrieval.expand.neighbors",
            "retrieval.expand.discount",
            "retrieval.expand.seed_top",
            "retrieval.expand.max_candidates",
            "retrieval.rerank.max_length",
            "ingest.max_file_mb",
            "ingest.pdf.min_text_chars_per_page",
            "eval.k",
        ):
            assert k in keys, f"{k} 没进注册表"

    def test_soft_confidence_reads_config(self):
        """低置信提醒的两个阈值原先是模块常量，现在必须跟着配置走。"""
        from ragkb.retrieve.engine import _soft

        cfg = self._cfg()
        cfg.refusal.min_score = 0.35

        cfg.refusal.soft_margin = 0.10
        assert _soft(0.42, 0.5, cfg.refusal)[0] is True, "0.42 < 0.35+0.10 应提示"
        cfg.refusal.soft_margin = 0.01
        assert _soft(0.42, 0.5, cfg.refusal)[0] is False, "余量收窄后不该再提示"

        # soft_floor 独立于 min_margin：分数够高但前几条持平，同样要提示
        cfg.refusal.soft_floor = 0.2
        low, reason = _soft(0.90, 0.05, cfg.refusal)
        assert low is True and "几乎持平" in (reason or "")

    def test_margin_window_controls_how_many_scores_count(self):
        """margin = top1 - mean(前 N 条)，N 由 refusal.margin_window 决定。"""
        from ragkb.retrieve.engine import RetrievalEngine
        from ragkb.models import Hit

        eng = object.__new__(RetrievalEngine)      # 不走 __init__，不加载模型
        hits = [Hit(1, 0.80, 1, "dense"), Hit(2, 0.79, 2, "dense"), Hit(3, 0.20, 3, "dense")]

        cfg = self._cfg()
        cfg.refusal.min_score = 0.9                # 保证走"算分数"这条路径
        cfg.refusal.margin_window = 2
        dec = eng._decide_refusal([object()], [], hits, cfg)
        assert dec.margin == pytest.approx(0.01), "窗口=2 时只该看前两条"

        cfg.refusal.margin_window = 5
        dec = eng._decide_refusal([object()], [], hits, cfg)
        assert dec.margin == pytest.approx(0.305), "窗口放大后 0.20 要计入均值"

    def test_grade_mid_ratio_changes_band(self):
        from ragkb.generate.evidence import grade_of

        # min_score=0.6：mid 门槛 = 0.6 * ratio
        assert grade_of(0.56, min_score=0.6, mid_ratio=0.9) == "mid"
        assert grade_of(0.56, min_score=0.6, mid_ratio=0.5) == "mid"
        assert grade_of(0.40, min_score=0.6, mid_ratio=0.9) == "low"
        assert grade_of(0.40, min_score=0.6, mid_ratio=0.5) == "mid", "放宽比例应升档"

    def test_trim_history_drops_whole_earliest_turns(self):
        from ragkb.chatctx import trim_history

        hist = [
            {"role": "user", "content": "一" * 100},
            {"role": "assistant", "content": "二" * 100},
            {"role": "user", "content": "三" * 100},
            {"role": "assistant", "content": "四" * 100},
        ]
        assert trim_history(hist, budget=0) == [], "预算 0 = 不带历史"
        kept = trim_history(hist, budget=200)
        assert [m["content"][0] for m in kept] == ["三", "四"], "只该留下最后一轮"
        # 预算小到连一条都装不下时，仍从最新的开始留，且不能以 assistant 开头
        one = trim_history(hist, budget=50)
        assert one and one[0]["role"] == "user"

    def test_build_context_respects_floor(self):
        """预算被历史吃光时，资料段至少留 min_context_chars 字符。"""
        from ragkb.generate.prompt import build_context
        from ragkb.models import Candidate

        text = "资料正文" * 50          # 200 字
        cand = Candidate(
            chunk_id=1, doc_id=1, doc_title="t", source_uri="u", page_no=None,
            heading_path=None, text=text,
        )
        cfg = self._cfg()
        bundle = build_context(
            [cand], question="问", token_budget=100, history_text_tokens=90,
            prompt_version=cfg.prompt_version, min_context_chars=300,
        )
        assert bundle.citations[0].preview == text[:160]
        assert text in bundle.user_prompt, "地板 300 应容得下整条 212 字的片段"

    def test_cross_checks_for_new_params(self):
        from ragkb import settings

        cfg = self._cfg()
        cfg.retrieval.fusion.top_k = 20
        with pytest.raises(ValueError, match="融合候选数"):
            settings.apply(cfg, {"retrieval.expand.seed_top": 30})

        # 先收窄总预算，再设一个高于它的地板 —— 出厂值下 6000 的预算容得下
        # 任何合法地板，这条组合只能在用户把预算调小之后被触发。
        settings.apply(cfg, {"generation.context_token_budget": 512})
        with pytest.raises(ValueError, match="上下文预算"):
            settings.apply(cfg, {"generation.min_context_chars": 600})

    def test_evidence_section_is_persisted(self, tmp_path, monkeypatch):
        """新开的 evidence 段能否写进 local.yaml —— 嵌套写错会静默丢键。"""
        from ragkb import settings

        monkeypatch.setattr(settings, "LOCAL_CONFIG", tmp_path / "local.yaml")
        cfg = self._cfg()
        settings.apply(cfg, {"evidence.mid_ratio": 0.7})
        settings.save_local(cfg)
        raw = (tmp_path / "local.yaml").read_text(encoding="utf-8")
        assert "mid_ratio: 0.7" in raw


class TestHardening:
    """2026-10-30：补三个"平时看不出、出事就难查"的口子。

    这类改动的验收标准是**边界行为**——超限要删半截、失败要保住旧副本、
    重启要收尾孤儿。正常路径本来就没坏，只测正常路径等于没测。
    """

    @staticmethod
    def _upload(data: bytes, size: int | None = None, name: str = "a.txt"):
        import io

        from starlette.datastructures import UploadFile

        return UploadFile(file=io.BytesIO(data), filename=name, size=size)

    def test_upload_streams_and_reports_size(self, tmp_path):
        import asyncio

        from ragkb.api.app import _save_upload

        n = asyncio.run(_save_upload(self._upload(b"x" * 5000), tmp_path / "a.txt", 1))
        assert n == 5000
        assert (tmp_path / "a.txt").read_bytes() == b"x" * 5000
        assert not (tmp_path / "a.txt.part").exists(), "临时文件必须收干净"

    def test_upload_rejects_declared_size_before_reading(self, tmp_path):
        """Content-Length 就足以判死时，一个字节都不该落盘。

        这条盯的是"读进内存再判大小"的老写法：它能把 5GB 请求全吃下来。
        """
        import asyncio

        from fastapi import HTTPException

        from ragkb.api.app import _save_upload

        target = tmp_path / "a.txt"
        with pytest.raises(HTTPException) as e:
            asyncio.run(_save_upload(self._upload(b"", size=99 * 1024 * 1024), target, 1))
        assert e.value.status_code == 413
        assert not target.exists()

    def test_upload_aborts_midway_keeps_old_copy(self, tmp_path):
        """分块传输没有 Content-Length，只能边读边数。

        超限时必须删临时文件，并且**不能碰**目标路径上原有的那份——"补原始
        文件"正是往已有副本上覆盖，写一半失败顺手把好文件毁了是最难查的损坏。
        """
        import asyncio

        from fastapi import HTTPException

        from ragkb.api.app import _save_upload

        target = tmp_path / "a.txt"
        target.write_bytes(b"old")
        big = b"y" * (2 * 1024 * 1024 + 7)
        with pytest.raises(HTTPException) as e:
            asyncio.run(_save_upload(self._upload(big, size=None), target, 1))
        assert e.value.status_code == 413
        assert target.read_bytes() == b"old", "失败的上传不能毁掉旧副本"
        assert not (tmp_path / "a.txt.part").exists()

    # ------------------------------------------------------------ 启动收尾
    @staticmethod
    def _doc(conn, kb_id: int, status: str, uri: str) -> None:
        conn.execute(
            "INSERT INTO document (kb_id, source_type, source_uri, title, content_hash,"
            " extractor, status, imported_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (kb_id, "file", uri, uri, "", "x", status, "t", "t"),
        )

    @staticmethod
    def _patch_state(monkeypatch, conn):
        import ragkb.api.app as A

        class _S:
            class db:
                @staticmethod
                def conn():
                    return conn

        monkeypatch.setattr(A, "state", _S())
        return A

    def test_startup_closes_orphan_indexing(self, tmp_path, monkeypatch):
        """进程被 kill 后文档会永久卡在 indexing；启动要收尾，且不碰终态。"""
        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('k')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        self._doc(conn, kb_id, "indexing", "a")
        self._doc(conn, kb_id, "pending", "b")
        self._doc(conn, kb_id, "ready", "c")
        self._doc(conn, kb_id, "failed", "d")
        conn.execute("UPDATE document SET fail_reason='抽取失败：坏文件' WHERE source_uri='d'")

        A = self._patch_state(monkeypatch, conn)
        A._sweep_orphan_imports()

        got = {
            r["source_uri"]: (r["status"], r["fail_reason"])
            for r in conn.execute(
                "SELECT source_uri, status, fail_reason FROM document ORDER BY id"
            ).fetchall()
        }
        assert got["a"][0] == "failed" and "重启" in (got["a"][1] or "")
        assert got["b"][0] == "failed", "pending 也可能是被杀在半路"
        assert got["c"][0] == "ready", "ready 是终态，不该被动"
        assert got["d"][1] == "抽取失败：坏文件", "已有的失败原因比「重启」具体，不能覆盖"

    def test_startup_sweep_noop_when_clean(self, tmp_path, monkeypatch):
        """没有孤儿时不许产生任何写入（否则每次启动都刷 updated_at）。"""
        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('k')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        self._doc(conn, kb_id, "ready", "a")

        A = self._patch_state(monkeypatch, conn)
        A._sweep_orphan_imports()
        row = conn.execute("SELECT status, updated_at FROM document").fetchone()
        assert row["status"] == "ready" and row["updated_at"] == "t"

    # -------------------------------------------------------- 前台反馈入口
    def test_frontsite_has_feedback_endpoint(self, tmp_path):
        """前台以前根本没有这个路由：按钮画出来也是 404。

        前台是**独立进程**，后台那个 /api/feedback 它够不着——两边跑在不同
        端口上，正确做法是各自挂一个。
        """
        import sqlite3

        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('k')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.execute(
            "INSERT INTO session (id, kb_id, title, created_at, updated_at)"
            " VALUES ('s1', ?, 't', 't', 't')", (kb_id,)
        )
        cur = conn.execute(
            "INSERT INTO message (session_id, role, content, trace_id, created_at)"
            " VALUES ('s1', 'assistant', 'a', 'tr', 't')"
        )
        mid = int(cur.lastrowid)
        conn.commit()
        db_path = str(cfg.storage.db_path)

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                # 与 Database._new_conn 一致：生产连接是 isolation_level=None，
                # 桩用默认值会让 DML 隐式开事务、关连接时回滚，表现为
                # "接口返回 200 但库里什么都没有"——和真 bug 长得一模一样。
                c.isolation_level = None
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "def"
            providers: dict = {}
            roles: dict = {}
            context_token_budget = 6000
            min_context_chars = 200

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"
            warm = {"state": "ready", "error": ""}

            def effective_cfg(self, kb_id):
                return self.cfg

            class embedder:
                is_loaded = True
                error = ""

        spec = InstanceSpec(name="fb", kb_id=kb_id, port=8807, lang="ug")
        client = TestClient(build_site(spec, _State()))

        r = client.post(
            "/api/feedback",
            json={"message_id": mid, "verdict": "down", "comment": "答非所问"},
        )
        assert r.status_code == 200 and r.json()["ok"] is True
        row = sqlite3.connect(db_path).execute(
            "SELECT verdict, comment FROM feedback WHERE message_id=?", (mid,)
        ).fetchone()
        assert row == ("down", "答非所问")

        # 改主意：同一条再点一次应该覆盖，不堆两条（否则统计里一条答案算两次）
        client.post("/api/feedback", json={"message_id": mid, "verdict": "up", "comment": ""})
        n = sqlite3.connect(db_path).execute("SELECT count(*) FROM feedback").fetchone()[0]
        assert n == 1

    def test_frontsite_ask_reports_message_id(self, tmp_path):
        """前台 done 事件要带 message_id。

        没有它前端连按钮都不会画（见 decorateBot：rec.mid 为空就不生成），
        所以"前台补了反馈入口"这件事真正落地的地方是这一个字段。
        """
        import json as _json
        import sqlite3

        from fastapi.testclient import TestClient

        from ragkb.frontsite.instcfg import InstanceSpec
        from ragkb.frontsite.site import build_site

        cfg, conn = _temp_db(tmp_path)
        conn.execute("INSERT INTO kb (name) VALUES ('k')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        conn.commit()
        db_path = str(cfg.storage.db_path)

        class _DB:
            def conn(self):
                c = sqlite3.connect(db_path)
                c.isolation_level = None
                c.row_factory = sqlite3.Row
                return c

        class _G:
            provider = "gen"
            providers = {"gen": {}}
            roles: dict = {}
            context_token_budget = 6000
            min_context_chars = 200

        class _Cfg:
            generation = _G()
            refusal = _Refusal()
            prompt_version = "qa_v1"
            chat = _StubChat()
            evidence = _StubEvidence()

        class _Out:
            text = "answer"

        class _LLM:
            def complete(self, msgs, provider=None, **kw):
                return _Out()

            def stream(self, msgs, provider=None, **kw):
                return iter(["answer"])

        class _State:
            db = _DB()
            cfg = _Cfg()
            prompt_version = "qa_v1"
            llm = _LLM()
            warm = {"state": "ready", "error": ""}

            def effective_cfg(self, kb_id):
                return self.cfg

            class embedder:
                is_loaded = True
                error = ""

            class engine:
                @staticmethod
                def search(conn, query, kb_id=None, cfg=None):
                    class R:
                        candidates = []
                        refusal = None
                        timings = {}
                        degraded = []
                    return R()

        spec = InstanceSpec(name="mid", kb_id=kb_id, port=8808, lang="ug",
                            answer_mode="direct")
        client = TestClient(build_site(spec, _State()))
        r = client.post("/api/ask", json={"question": "سوئال", "session_id": "sx"})
        assert r.status_code == 200
        assert "event: done" in r.text, "没有 done 事件，说明链路断在前面了"
        tail = r.text[r.text.index("event: done"):]
        data_line = next(l for l in tail.splitlines() if l.startswith("data: "))
        done = _json.loads(data_line[len("data: "):])
        assert done.get("message_id"), "前台 done 必须带 message_id"

        n = sqlite3.connect(db_path).execute(
            "SELECT count(*) FROM message WHERE session_id='sx' AND role='assistant'"
        ).fetchone()[0]
        assert n == 1, "落库与下发必须是同一条消息"


class TestChunkedUpload:
    """2026-10-30：大文件上传。

    「上传太大没解决方案」的根因不在服务端判大小，而在链路：几十 MB 就开始
    撞反向代理的 body 上限和浏览器对久无进展请求的超时，传不到判大小那一步。
    分片把一次大传输拆成多次小传输，才谈得上支持大文件。

    这里盯的全是**失败路径**——正常拼装本来就该过，不测也知道；真正会出事的是
    少一片、少几十字节、断在中途这几种，它们拼出来的文件"看着正常"。
    """

    @pytest.fixture()
    def cli(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import ragkb.api.app as A

        monkeypatch.setattr(A, "UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(A, "_PARTS_DIR", tmp_path / "uploads" / ".parts")
        monkeypatch.setattr(A, "_CHUNK_SIZE", 1024)   # 1KB 一片，测试才跑得快
        return TestClient(A.app)

    @staticmethod
    def _init(cli, size: int, fp: str = "a" * 16, name: str = "a.bin"):
        return cli.post("/api/upload/init", json={
            "name": name, "size": size, "fingerprint": fp,
        })

    def test_roundtrip_reassembles_exact_bytes(self, cli):
        data = bytes(range(256)) * 10       # 2560 字节 → 3 片
        info = self._init(cli, len(data)).json()
        assert info["total_chunks"] == 3
        cs, uid = info["chunk_size"], info["upload_id"]
        for i in range(info["total_chunks"]):
            r = cli.put(f"/api/upload/{uid}/part/{i}", content=data[i * cs:(i + 1) * cs])
            assert r.status_code == 200, r.text
        fin = cli.post(f"/api/upload/{uid}/finish")
        assert fin.status_code == 200
        from pathlib import Path

        assert Path(fin.json()["staged"]).read_bytes() == data

    def test_resume_reuses_received_parts(self, cli):
        """断点续传：同一个文件再 init 一次，已收的片要还在。

        中断在大文件上传里是常态。不能续传的分片上传只是把一次失败拆成十次。
        """
        data = b"y" * 3000
        fp = "b" * 16
        uid = self._init(cli, len(data), fp).json()["upload_id"]
        cli.put(f"/api/upload/{uid}/part/0", content=data[:1024])

        again = self._init(cli, len(data), fp).json()
        assert again["upload_id"] == uid, "同一指纹必须复用同一个会话"
        assert again["received"] == [0]

    def test_size_change_discards_old_parts(self, cli):
        """同名但大小变了（编辑后又存回去），旧分片必须丢掉。

        留着就会拼出一个前 4MB 是新文件、后面是旧文件的东西——比直接失败更糟。
        """
        fp = "c" * 16
        uid = self._init(cli, 3000, fp, "a.bin").json()["upload_id"]
        cli.put(f"/api/upload/{uid}/part/0", content=b"x" * 1024)
        again = self._init(cli, 5000, fp, "a.bin").json()
        assert again["upload_id"] == uid and again["received"] == []

    def test_finish_rejects_missing_parts(self, cli):
        data = b"z" * 3000
        info = self._init(cli, len(data)).json()
        uid = info["upload_id"]
        cli.put(f"/api/upload/{uid}/part/0", content=data[:1024])
        r = cli.post(f"/api/upload/{uid}/finish")
        assert r.status_code == 400 and "还缺" in r.json()["detail"]

    def test_finish_rejects_short_last_part(self, cli):
        """片数齐了但字节数不对——最危险的一种。

        拼出来是个**看起来正常、实际被截断**的文件，入库后检索永远少一截，
        而且没有任何迹象。宁可让它在这里失败。
        """
        data = b"q" * 2048
        info = self._init(cli, len(data)).json()
        uid, cs = info["upload_id"], info["chunk_size"]
        cli.put(f"/api/upload/{uid}/part/0", content=data[:cs])
        cli.put(f"/api/upload/{uid}/part/1", content=data[cs:cs + 900])   # 少 148 字节
        r = cli.post(f"/api/upload/{uid}/finish")
        assert r.status_code == 400 and "不符" in r.json()["detail"]

    def test_oversize_is_refused_at_init(self, cli, monkeypatch):
        """超上限要在 init 就拒，不能等用户把 500MB 传完再告诉他。

        （以前单请求上传就是这个毛病：字节全传上来了，才回 413。）
        """
        import ragkb.api.app as A

        monkeypatch.setattr(A.state.cfg.ingest, "max_file_mb", 1)
        r = self._init(cli, 2 * 1024 * 1024)
        assert r.status_code == 413

    def test_bad_id_and_out_of_range_index(self, cli):
        assert cli.put("/api/upload/NOTHEX/part/0", content=b"x").status_code == 400
        info = self._init(cli, 100).json()
        uid = info["upload_id"]
        assert cli.put(f"/api/upload/{uid}/part/9", content=b"x").status_code == 400

    def test_abort_removes_parts(self, cli, tmp_path):
        info = self._init(cli, 3000).json()
        uid = info["upload_id"]
        cli.put(f"/api/upload/{uid}/part/0", content=b"x" * 1024)
        folder = tmp_path / "uploads" / ".parts" / uid
        assert folder.exists()
        assert cli.delete(f"/api/upload/{uid}").json()["ok"] is True
        assert not folder.exists()

    def test_sweep_removes_only_stale(self, cli, tmp_path):
        """启动清扫只清过期的那批，正在传的不许动。"""
        import os as _os

        import ragkb.api.app as A

        old = self._init(cli, 3000, "d" * 16).json()["upload_id"]
        new = self._init(cli, 3000, "e" * 16).json()["upload_id"]
        base = tmp_path / "uploads" / ".parts"
        for uid in (old, new):
            cli.put(f"/api/upload/{uid}/part/0", content=b"x" * 1024)
        _os.utime(base / old, (0, 0))
        assert A._sweep_stale_parts() == 1
        assert not (base / old).exists()
        assert (base / new).exists()


class TestCleanupNeverFailsRequest:
    """2026-10-30：清理动作把整个服务带崩过一次，必须钉住。

    现场：一个 37MB 文件传完、合并落地成功，收尾删分片目录时，宿主的批量删除
    守卫（沙箱、审计钩子这类）直接抛 SystemExit(1) —— 而 `ignore_errors=True`
    只盖 OSError，SystemExit 是 BaseException，一路穿过 request 处理、穿过
    uvicorn，进程没了。用户看到的是"上传成功但服务挂了"。

    所以规则是：清理是收尾动作，失败只准留垃圾，不准改变已经成功的结果。
    """

    def test_quiet_unlink_swallows_blocked_delete(self, tmp_path, monkeypatch):
        import pathlib

        import ragkb.api.app as A

        f = tmp_path / "0.part"
        f.write_bytes(b"x")

        def blocked(self, missing_ok=False):
            raise SystemExit(1)          # 宿主删除守卫的原样行为

        monkeypatch.setattr(pathlib.Path, "unlink", blocked)
        assert A._quiet_unlink(f) is False
        assert f.exists(), "删不掉就留着，不能假装删了"

    def test_discard_parts_reports_failure_without_raising(self, tmp_path, monkeypatch):
        import pathlib

        import ragkb.api.app as A

        folder = tmp_path / "p"
        folder.mkdir()
        (folder / "0.part").write_bytes(b"x")
        (folder / "meta.json").write_text("{}", encoding="utf-8")

        def blocked(self, missing_ok=False):
            raise SystemExit(1)

        monkeypatch.setattr(pathlib.Path, "unlink", blocked)
        assert A._discard_parts(folder) is False
        assert folder.exists()

    def test_finish_still_returns_staged_when_cleanup_fails(self, tmp_path, monkeypatch):
        """合并成功 = 请求成功。删不掉分片只是磁盘上多几 MB 垃圾。"""
        import pathlib

        from fastapi.testclient import TestClient

        import ragkb.api.app as A

        monkeypatch.setattr(A, "UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(A, "_PARTS_DIR", tmp_path / "uploads" / ".parts")
        monkeypatch.setattr(A, "_CHUNK_SIZE", 1024)
        cli = TestClient(A.app)

        data = b"w" * 2500
        uid = cli.post("/api/upload/init", json={
            "name": "w.bin", "size": len(data), "fingerprint": "f" * 16,
        }).json()["upload_id"]
        for i in range(3):
            assert cli.put(
                f"/api/upload/{uid}/part/{i}", content=data[i * 1024:(i + 1) * 1024]
            ).status_code == 200

        def blocked(self, missing_ok=False):
            raise SystemExit(1)

        monkeypatch.setattr(pathlib.Path, "unlink", blocked)
        r = cli.post(f"/api/upload/{uid}/finish")
        assert r.status_code == 200, r.text
        assert pathlib.Path(r.json()["staged"]).read_bytes() == data

    def test_abort_returns_ok_when_cleanup_fails(self, tmp_path, monkeypatch):
        import pathlib

        from fastapi.testclient import TestClient

        import ragkb.api.app as A

        monkeypatch.setattr(A, "UPLOAD_DIR", tmp_path / "uploads")
        monkeypatch.setattr(A, "_PARTS_DIR", tmp_path / "uploads" / ".parts")
        cli = TestClient(A.app)
        uid = cli.post("/api/upload/init", json={
            "name": "a.bin", "size": 3000, "fingerprint": "9" * 16,
        }).json()["upload_id"]
        cli.put(f"/api/upload/{uid}/part/0", content=b"x" * 1024)

        def blocked(self, missing_ok=False):
            raise SystemExit(1)

        monkeypatch.setattr(pathlib.Path, "unlink", blocked)
        r = cli.delete(f"/api/upload/{uid}")
        assert r.status_code == 200 and r.json()["ok"] is True


class TestSessionBulkDelete:
    """会话批量删除（2026-10-30）。

    批量接口的验收点不在"能删掉"，而在**删不掉的时候怎么交代**：
    勾了 10 条只成功 8 条，返回里必须能指出是哪 2 条，否则用户只能再勾一遍
    碰运气。同理，整批失败不能因为其中一条不存在就把成功的那些回滚掉——
    "点了删除什么都没发生，再点还是一样"是个死循环。
    """

    @pytest.fixture()
    def cli(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import ragkb.api.app as A

        # state.conn 是个 property（每次回 db.conn()），直接 setattr 会被拒；
        # 而且 TestClient 在另一个线程跑应用，sqlite 连接不能跨线程用——所以换的
        # 是整个 db，让每个线程各自拿到自己的连接（顺带保住 foreign_keys=ON，
        # 否则 message 的 CASCADE 不生效，删完会话消息还留着）。
        from ragkb.config import load_config
        from ragkb.db import Database

        cfg = load_config().model_copy(deep=True)
        cfg.storage.db_path = tmp_path / "t.db"
        cfg.storage.data_dir = tmp_path
        cfg.storage.vector_dir = tmp_path / "vec"
        db = Database(cfg)
        db.ensure_schema()
        conn = db.conn()
        self._seed(conn, {"s1": 3, "s2": 2, "s3": 0})
        monkeypatch.setattr(A.state, "db", db)
        return TestClient(A.app)

    @staticmethod
    def _seed(conn, counts: dict):
        conn.execute("INSERT INTO kb (name) VALUES ('k')")
        kb_id = conn.execute("SELECT id FROM kb").fetchone()["id"]
        for sid, n in counts.items():
            conn.execute(
                "INSERT INTO session (id, kb_id, title, created_at, updated_at)"
                " VALUES (?,?,?, 't', 't')", (sid, kb_id, sid))
            for i in range(n):
                conn.execute(
                    "INSERT INTO message (session_id, role, content, trace_id, created_at)"
                    " VALUES (?, 'user', ?, 'tr', 't')", (sid, f"{sid}-{i}"))
        conn.commit()

    def test_reports_deleted_count_and_cascades(self, cli, tmp_path, monkeypatch):
        import ragkb.api.app as A

        r = cli.post("/api/sessions/bulk-delete", json={"ids": ["s1", "s3"]})
        assert r.status_code == 200, r.text
        body = r.json()
        # 数字必须对得上勾的东西：s1 三条消息、s3 空会话，共 3 条消息
        assert (body["deleted"], body["messages"], body["failed"]) == (2, 3, [])
        left = A.state.conn.execute(
            "SELECT id FROM session ORDER BY id").fetchall()
        assert [x["id"] for x in left] == ["s2"]
        assert A.state.conn.execute("SELECT count(*) FROM message").fetchone()[0] == 2

    def test_duplicate_id_counts_once_and_is_not_a_failure(self, cli):
        r = cli.post("/api/sessions/bulk-delete", json={"ids": ["s1", "s1", "s2"]})
        body = r.json()
        assert body["deleted"] == 2 and body["failed"] == [], \
            "同一条勾了两遍不是错误，报出来只会让人以为有数据没删掉"

    def test_partial_failure_keeps_the_successful_half(self, cli):
        """一条不存在，另外两条照样删。整批回滚才是真的坑。"""
        import ragkb.api.app as A

        r = cli.post("/api/sessions/bulk-delete", json={"ids": ["s1", "ghost", "s2"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] == 2
        assert body["ok"] is False
        assert body["failed"] == [{"id": "ghost", "reason": "会话不存在"}]
        left = [x["id"] for x in A.state.conn.execute("SELECT id FROM session").fetchall()]
        assert left == ["s3"], "成功的那两条不能被回滚掉"

    def test_empty_list_is_rejected(self, cli):
        assert cli.post("/api/sessions/bulk-delete", json={"ids": []}).status_code == 422

    def test_single_delete_still_reports_message_count(self, cli):
        """单条接口被抽成 _delete_one_session 之后，口径不能变。"""
        r = cli.delete("/api/sessions/s1")
        assert r.status_code == 200
        assert r.json()["deleted"] == 3
        assert cli.delete("/api/sessions/s1").status_code == 404


class TestChunkSizeVsEmbedWindow:
    """块长不能超过嵌入窗口（2026-10-30）。

    现场：知识库级覆盖把 chunking.size 设成 1000，而嵌入模型窗口是 512。
    于是 **67% 的块在向量化时被截断**，块的后半段压根没进索引。它不抛异常、
    不打日志，唯一的症状是"召回莫名偏低"——所以这条必须写进测试，而不是
    只靠注释提醒。
    """

    def test_global_setting_rejects_diluted_chunk(self):
        from ragkb import settings

        cfg = load_config().model_copy(deep=True)
        with pytest.raises(ValueError, match="两倍"):
            settings.apply(cfg, {"chunking.size": 2000})
        # 1000 是本语料上实测召回更高的值，不能被护栏挡在外面
        settings.apply(cfg, {"chunking.size": 1000})
        assert cfg.chunking.size == 1000

    def test_kb_override_rejects_oversized_chunk(self):
        """库级覆盖走的是另一条校验路径，必须同样拦得住。"""
        from ragkb import kbconfig

        cfg = load_config().model_copy(deep=True)
        with pytest.raises(ValueError, match="两倍"):
            kbconfig.normalize(cfg, {"chunking.size": 2000, "chunking.overlap": 80})
        ok = kbconfig.normalize(cfg, {"chunking.size": 1000, "chunking.overlap": 80})
        assert ok["chunking.size"] == 1000

    def test_real_chunks_fit_the_window(self):
        """护栏判的是 size，真正要证的是"切出来的块不超限"。"""
        from ragkb.config import load_config
        from ragkb.ingest.chunker import chunk_blocks
        from ragkb.models import Block

        cfg = load_config().model_copy(deep=True)
        cfg.chunking.size = 480
        # 一段没有句读的长文：硬切路径也要守住上限
        blocks = [Block(text="甲乙丙丁" * 900, page_no=1, heading_path=None)]
        drafts = chunk_blocks(blocks, cfg.chunking)
        assert drafts, "没切出块来，这条测试就没意义了"
        win = int(cfg.embedding.max_seq_length)
        over = [d for d in drafts if d.token_count > win]
        assert not over, f"{len(over)} 个块超过嵌入窗口 {win}"

