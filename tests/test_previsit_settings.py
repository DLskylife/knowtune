"""运行时可改参数：schema / 覆盖层 / 热生效。

锁四件事：
1. 保存只落「和 previsit.yaml 不一样」的键，改回默认值那一行自动消失；
2. 覆盖层是逐层合并的，改 `kg.ros_max` 不能把整个 `kg` 段抹掉；
3. 越界值在后端被拒（面板可绕过，校验不能只在前端）；
4. 保存后引擎**不重启**就换上新参数，注入的进程内检索器不被替换掉。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from previsit import config as config_mod
from previsit import settings as settings_mod
from previsit.config import load_previsit
from previsit.engine import PrevisitEngine
from previsit.retriever import InProcessRetriever
from previsit.store import Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cfgdir(tmp_path: Path) -> Path:
    """一份真实配置的临时副本：测试绝不写仓库里的 config/。"""
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(ROOT / "config" / "previsit.yaml", d / "previsit.yaml")
    shutil.copy(ROOT / "config" / "previsit_kb.yaml", d / "previsit_kb.yaml")
    return d / "previsit.yaml"


def _overrides(cfg: Path) -> dict:
    p = settings_mod.overrides_path(cfg)
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_描述带分组和默认值(cfgdir: Path) -> None:
    d = settings_mod.describe(cfg_path=cfgdir)
    assert {g["key"] for g in d["groups"]} >= {"model", "flow", "kg", "retrieval"}
    paths = {f["path"] for f in d["fields"]}
    assert {"provider", "model", "temperature", "max_turns", "kg.ros_max",
            "retrieval.top_k", "prompts.ask"} <= paths
    # 没有任何覆盖时，值 == 默认值，overridden 为空
    assert d["overridden"] == []
    assert d["values"]["temperature"] == d["defaults"]["temperature"]
    # 每个字段都得有 label 和 value 键，前端按这个渲染
    for f in d["fields"]:
        assert f["label"] and "value" in f and "default" in f


def test_保存只落改过的键(cfgdir: Path) -> None:
    settings_mod.apply({"temperature": 0.35, "kg.ros_max": 4}, cfg_path=cfgdir)
    over = _overrides(cfgdir)
    assert over == {"temperature": 0.35, "kg": {"ros_max": 4}}

    pv = load_previsit(cfgdir)
    assert pv["temperature"] == 0.35
    assert pv["kg"]["ros_max"] == 4
    # 同级没改的键必须还在
    assert pv["kg"]["min_ratio"] == 0.12
    assert pv["kg"]["enabled"] is True
    # 顶层没改的键也还在
    assert pv["max_turns"] == 14


def test_改回默认值会删掉那一行(cfgdir: Path) -> None:
    settings_mod.apply({"temperature": 0.9, "max_turns": 20}, cfg_path=cfgdir)
    assert _overrides(cfgdir) == {"temperature": 0.9, "max_turns": 20}

    base = load_previsit(cfgdir, use_overrides=False)
    settings_mod.apply({"temperature": base["temperature"]}, cfg_path=cfgdir)
    over = _overrides(cfgdir)
    assert "temperature" not in over
    assert over == {"max_turns": 20}


def test_原样保存不会写出一堆覆盖项(cfgdir: Path) -> None:
    """前端会把 27 个字段整包发回来，没改的那些一个都不许落盘。

    这是最容易翻车的一处：空串 vs null、textarea 削掉的结尾换行，只要有一处
    判成"不等"，点一次保存就会把整份默认值冻进覆盖层，之后改 previsit.yaml
    再也不生效——而且界面上完全看不出来。
    """
    d = settings_mod.describe(cfg_path=cfgdir)
    settings_mod.apply(d["values"], cfg_path=cfgdir)
    assert not settings_mod.overrides_path(cfgdir).exists()

    # 改一项后，其余字段原样回传也不能被带进文件
    settings_mod.apply({**d["values"], "temperature": 0.4}, cfg_path=cfgdir)
    assert _overrides(cfgdir) == {"temperature": 0.4}


def test_全部恢复默认会删文件(cfgdir: Path) -> None:
    settings_mod.apply({"max_turns": 18}, cfg_path=cfgdir)
    p = settings_mod.overrides_path(cfgdir)
    assert p.exists()
    settings_mod.reset(cfg_path=cfgdir)
    assert not p.exists()
    assert load_previsit(cfgdir)["max_turns"] == 14


@pytest.mark.parametrize("values,needle", [
    ({"max_turns": 999}, "不能大于"),
    ({"max_turns": 2}, "不能小于"),
    ({"temperature": "abc"}, "必须填一个数字"),
    ({"max_tokens": 12.5}, "必须是整数"),
    ({"enabled": "也许"}, "只能是开或关"),
    ({"title": "x" * 61}, "不能超过"),
])
def test_越界与类型错误被拒(cfgdir: Path, values: dict, needle: str) -> None:
    with pytest.raises(ValueError) as exc:
        settings_mod.apply(values, cfg_path=cfgdir)
    assert needle in str(exc.value)


def test_白名单之外的键被丢掉(cfgdir: Path) -> None:
    settings_mod.apply({"danger_rules": [], "api_key": "sk-xxx"}, cfg_path=cfgdir)
    # 一条都不认：不能因为面板能发任意 JSON，就让它改分诊规则或写密钥
    assert _overrides(cfgdir) == {}
    assert load_previsit(cfgdir)["danger_rules"], "危险词表不该被清空"


def test_覆盖层写坏时退回默认(cfgdir: Path) -> None:
    settings_mod.overrides_path(cfgdir).write_text(
        "temperature: [坏掉的 yaml\n", encoding="utf-8")
    pv = load_previsit(cfgdir)          # 不抛异常
    assert pv["temperature"] == 0.2


def test_引擎热更新不动会话与检索器(tmp_path: Path) -> None:
    pv = load_previsit(ROOT / "config" / "previsit.yaml", use_overrides=False)
    store = Store(tmp_path / "pv.db")
    eng = PrevisitEngine(pv, store)
    eng.retriever = InProcessRetriever(lambda q, k: [], top_k=8, max_per_doc=2)

    new = dict(pv)
    new.update({"temperature": 0.55, "max_tokens": 256, "timeout_s": 7,
                "max_turns": 9, "retrieval": {**pv["retrieval"], "top_k": 4,
                                              "max_per_doc": 3},
                "kg": {**pv["kg"], "ros_max": 1}})
    eng.apply_config(new)

    assert eng.pv is new
    assert eng.timeout_s == 7
    assert eng.ros_max == 1
    # 注入的进程内检索器必须被保留（换掉就等于把 12ms 变成一次 HTTP）
    assert isinstance(eng.retriever, InProcessRetriever)
    assert eng.retriever.top_k == 4
    assert eng.retriever.max_per_doc == 3
    # 线程池与会话缓存是进程级资源，不该被重建
    assert eng._pool is not None and eng._slot_order == {}


def test_关掉召回后进程内检索器被禁用(tmp_path: Path) -> None:
    pv = load_previsit(ROOT / "config" / "previsit.yaml", use_overrides=False)
    eng = PrevisitEngine(pv, Store(tmp_path / "pv.db"))
    eng.retriever = InProcessRetriever(lambda q, k: [], top_k=8)
    eng.apply_config({**pv, "retrieval": {**pv["retrieval"], "enabled": False}})
    assert eng.retriever is not None
    assert eng.retriever.ok is False


def test_接口能读能写且立即生效(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config" / "previsit.yaml"
    cfg.parent.mkdir()
    shutil.copy(ROOT / "config" / "previsit.yaml", cfg)
    shutil.copy(ROOT / "config" / "previsit_kb.yaml", cfg.parent / "previsit_kb.yaml")
    monkeypatch.setattr(config_mod, "DEFAULT_PATH", cfg)
    monkeypatch.setattr(config_mod, "DEFAULT_KB_PATH", cfg.parent / "previsit_kb.yaml")
    monkeypatch.setattr(settings_mod, "DEFAULT_PATH", cfg)
    monkeypatch.setenv("PREVISIT_DATA_DIR", str(tmp_path / "data"))

    from previsit import app as app_mod

    monkeypatch.setattr(app_mod, "_engine", None)
    monkeypatch.setattr(app_mod, "_store", None)
    client = TestClient(app_mod.create_app())

    got = client.get("/api/previsit/config").json()
    assert got["values"]["max_turns"] == 14
    assert got["runtime"]["slots"] > 0

    bad = client.put("/api/previsit/config", json={"values": {"max_turns": 999}})
    assert bad.status_code == 400 and "不能大于" in bad.json()["detail"]

    ok = client.put("/api/previsit/config",
                    json={"values": {"max_turns": 9, "retrieval.top_k": 5}})
    assert ok.status_code == 200
    body = ok.json()
    assert body["values"]["max_turns"] == 9
    assert body["values"]["retrieval.top_k"] == 5
    assert body["runtime"]["max_turns"] == 9
    assert set(body["overridden"]) == {"max_turns", "retrieval.top_k"}
    # 内存里的引擎也换了，不用重启
    assert app_mod.get_engine().pv["max_turns"] == 9

    reset = client.post("/api/previsit/config/reset").json()
    assert reset["values"]["max_turns"] == 14
    assert reset["overridden"] == []

    # 表单不再收主诉：不带 complaint 也必须能开场（返回空 turns，等患者开口）
    opened = client.post("/api/previsit/start", json={"profile": {"gender": "男"}})
    assert opened.status_code == 200
    assert opened.json()["turns"] == []

    # 总开关：关掉之后不许再开新问诊，但页面自己的接口还活着
    off = client.put("/api/previsit/config", json={"values": {"enabled": False}})
    assert off.status_code == 200
    blocked = client.post("/api/previsit/start", json={"complaint": "肚子疼"})
    assert blocked.status_code == 403
    assert "未开放" in blocked.json()["detail"]
