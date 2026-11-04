"""预问诊站点的 HTTP 入口（独立部署版）。

一个进程、一份配置、一个 SQLite，不 import 主项目的任何东西：

1. 独立进程：`python main.py`（部署目录）或 `uvicorn previsit.app:app`；
2. 挂进主项目：knowtune 侧通过 knowtune/previsit 桥接把本 app mount 到
   `/previsit`，终端用户不用换端口。

数据与依赖边界（"只调用后端知识库"的落地）：

- 知识面：config/previsit.yaml + config/previsit_kb.yaml + data/previsit/kg.db，
  三者都在 PREVISIT_HOME（默认包路径向上两级）下；
- 会话数据：`$PREVISIT_DATA_DIR/previsit.db`（默认 PREVISIT_HOME/data）；
- 模型：PREVISIT_LLM_BASE_URL 没设就没有 LLM，纯规则 + 知识库跑完全程。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import settings as settings_mod
from .config import ROOT, load_previsit
from .engine import PrevisitEngine
from .llm import GatewayLLM, OpenAICompatLLM, build_llm
from .store import Store

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"


def data_dir() -> Path:
    env = os.environ.get("PREVISIT_DATA_DIR", "").strip()
    if env:
        p = Path(env)
    else:
        p = ROOT / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


_engine: PrevisitEngine | None = None
_store: Store | None = None

# 模型后端清单的来源。主项目挂载时会把自己的注册表交进来（那时才有
# providers/model_list 可列）；独立部署退回读旁边的 config/models.yaml。
_MODEL_CATALOG: Any = None
# 模型客户端工厂。主项目挂载时注入自己的 LLMClient 适配器，独立部署时退回
# OpenAICompatLLM；两边都统一走「网关优先 + 降级」那一层。
_LLM_FACTORY: Any = None


def set_model_catalog(fn: Any) -> None:
    """主项目把"有哪些后端和模型"这件事交给预问诊的参数面板。

    签名：`fn() -> {"source": ..., "providers": [{"name","label","model",
    "models":[...], "local", "ready"}]}`。不注册就只有文件里那点信息。
    """
    global _MODEL_CATALOG
    _MODEL_CATALOG = fn


def set_llm_factory(fn: Any) -> None:
    """主项目注入：`fn(pv) -> llm`。不注入就用独立部署那份客户端。"""
    global _LLM_FACTORY
    _LLM_FACTORY = fn


def build_llm_for(pv: dict[str, Any]) -> Any:
    if callable(_LLM_FACTORY):
        try:
            return _LLM_FACTORY(pv)
        except Exception:
            pass                                  # 注入失败不能让站点起不来
    return build_llm(pv, OpenAICompatLLM.from_config(pv))


def model_catalog() -> dict:
    if callable(_MODEL_CATALOG):
        try:
            data = _MODEL_CATALOG()
            if isinstance(data, dict):
                return data
        except Exception:                      # 面板取不到清单不该让整页打不开
            pass
    return settings_mod.catalog_from_file()


def get_engine() -> PrevisitEngine:
    """进程级单例。

    被主项目 mount 时，同一进程里只建一次：两份配置、两个 Store 句柄会出现
    "配置明明改了、一边生效一边不生效"的排查陷阱。
    """
    global _engine, _store
    if _engine is None:
        pv = load_previsit()
        _store = Store(data_dir() / "previsit.db")
        _engine = PrevisitEngine(pv, _store, build_llm_for(pv))
    return _engine


def reload_engine() -> dict:
    """把磁盘上的配置重新灌进引擎（参数面板保存后调用）。

    不重建引擎：会话池、线程池、熔断计时器都在引擎里，换掉对象等于把正在
    等模型的那一轮凭空掐掉。这里只换配置派生出来的部件——模型客户端按新
    的 provider/model 重定向，图谱连接和检索器在 `apply_config` 里重建。
    """
    eng = get_engine()
    pv = load_previsit()
    eng.apply_config(pv)
    # 端点 / 模型 / 网关地址都可能被面板改掉，统一按新配置重建模型客户端。
    # 引擎本身不换：会话池、线程池、熔断计时器都挂在它身上，换掉等于把
    # 正在等模型的那一轮凭空掐掉。
    eng.llm = build_llm_for(pv)
    return pv


def get_store() -> Store:
    get_engine()
    assert _store is not None
    return _store


class StartReq(BaseModel):
    # 主诉可以空着：表单上那个必填文本域已经去掉了，哪里不舒服由第一句追问
    # 来问，患者开口的第一句话会被落成主诉（engine.reply 里做）。
    complaint: str = Field(default="", max_length=2000)
    # 基本信息表单。字段由 config/previsit.yaml 的 profile_fields 定义，
    # 这里收成自由 dict 再白名单过滤——schema 是配置驱动的，写死成模型字段
    # 就等于"加一个表单项要改代码"。
    profile: dict[str, Any] | None = None


class ReplyReq(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class ConfigReq(BaseModel):
    # {字段路径: 值}，路径形如 "max_turns" / "retrieval.top_k"。
    # 只认 settings.FIELDS 里声明过的键，其余静默丢弃——面板是白名单。
    values: dict[str, Any] = Field(default_factory=dict)


def _runtime(eng: PrevisitEngine) -> dict:
    """参数面板顶部那行"现在实际在用什么"。

    配置写的是"想要什么"，这里报的是"真的接上了什么"：模型客户端建没建成、
    图谱库开没打开、检索走的是进程内还是 HTTP。两行对不上时，问题就在这一行。
    """
    retriever = getattr(eng, "retriever", None)
    llm = eng.llm
    gw = isinstance(llm, GatewayLLM)
    return {
        "provider": eng.provider or "",
        "model": eng.model or "",
        # 界面上要能看出「到底走没走固定入口」。配了网关、实际在跑直连，
        # 是最难自己发现的一类问题。
        "llm_source": (getattr(llm, "source", "") if gw else ("direct" if llm else "none")),
        "llm_gateway": llm.base_url if gw else "",
        "llm_gateway_error": llm.last_error if gw else "",
        "temperature": eng.pv.get("temperature"),
        "max_tokens": eng.pv.get("max_tokens"),
        "timeout_s": eng.timeout_s,
        "max_turns": eng.pv.get("max_turns"),
        "llm": bool(eng.llm),
        "llm_error": eng.last_error,
        "kg": bool(getattr(getattr(eng, "kg", None), "ok", False)),
        "kg_error": getattr(getattr(eng, "kg", None), "error", None),
        "retrieval": bool(getattr(retriever, "ok", False)) if retriever else False,
        "retrieval_kind": type(retriever).__name__ if retriever else "",
        "domains": len(eng.domains),
        "slots": len(eng.slots),
    }


def create_app(engine: PrevisitEngine | None = None) -> FastAPI:
    """engine 可注入：独立部署不传（自己按环境建），主项目挂载时传入
    （用主项目的 LLM 后端和密钥体系），两边共用同一套路由。"""
    if engine is not None:
        global _engine, _store
        _engine = engine
        _store = engine.store
    eng = get_engine()
    store = get_store()
    pv = eng.pv

    app = FastAPI(title="预问诊", version="0.3.0")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="previsit-static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC / "index.html"))

    @app.get("/api/previsit/meta")
    def meta() -> dict:
        return {
            "enabled": bool(pv.get("enabled", True)),
            "title": pv.get("title"),
            "subtitle": pv.get("subtitle"),
            "welcome": pv.get("welcome"),
            "disclaimer": pv.get("disclaimer"),
            "placeholder": pv.get("placeholder"),
            # 适配器（主项目）没有 provider 属性，这里要能退到配置里的名字，
            # 否则主项目挂载时整页 meta 会 500——患者看到的是"连不上服务"。
            "provider": (getattr(eng.llm, "provider", None)
                         or eng.provider or None),
            # 空态不列槽位：还没说主诉，系统并不知道要问什么——硬列一套
            # 腹痛的槽位只会让咳嗽的人以为被问错了。开诊后由 start 返回真槽位。
            "slots": [],
            "domains": [{"key": d["key"], "label": d["label"]} for d in eng.domains],
            "profile_fields": pv.get("profile_fields") or [],
            "record_title": (pv.get("record") or {}).get("title"),
            "record_note": (pv.get("record") or {}).get("note"),
        }

    @app.get("/api/previsit/health")
    def health() -> dict:
        return {
            "ok": True,
            "sessions": _count_sessions(store),
            "llm_error": eng.last_error,
            "llm": bool(eng.llm),
        }

    # ------------------------------------------------------------ 参数面板
    # 改的是 config/previsit.overrides.yaml（只记和 previsit.yaml 不一样的键），
    # 保存后调 reload_engine() 让新参数立即生效——不重启进程、不打断会话。
    @app.get("/api/previsit/config")
    def get_config() -> dict:
        data = settings_mod.describe(model_catalog())
        data["runtime"] = _runtime(get_engine())
        return data

    @app.put("/api/previsit/config")
    def put_config(req: ConfigReq) -> dict:
        try:
            res = settings_mod.apply(req.values)
        except ValueError as exc:      # 越界/类型不对：直接告诉用户哪一项不对
            raise HTTPException(400, str(exc)) from None
        reload_engine()
        data = settings_mod.describe(model_catalog())
        data["saved"] = res["saved"]
        data["runtime"] = _runtime(get_engine())
        return data

    @app.post("/api/previsit/config/reset")
    def reset_config() -> dict:
        settings_mod.reset()
        reload_engine()
        data = settings_mod.describe(model_catalog())
        data["runtime"] = _runtime(get_engine())
        return data

    @app.post("/api/previsit/start")
    def start(req: StartReq) -> dict:
        # 参数面板上的总开关。页面照常打开（患者能看到说明），但不让开新问诊——
        # 关掉一个入口却还能继续挂新会话，那不叫关闭。
        if not eng.pv.get("enabled", True):
            raise HTTPException(403, "预问诊当前未开放，请到人工分诊台就诊")
        complaint = req.complaint.strip()
        # 空主诉是正常入参（表单上不再收这一项），engine 会开一个"等患者开口"
        # 的空会话，而不是让入口直接 400。
        return eng.start(complaint, req.profile)

    @app.post("/api/previsit/reply")
    def reply(req: ReplyReq) -> dict:
        try:
            return eng.reply(req.session_id, req.message.strip())
        except KeyError:
            raise HTTPException(404, "会话不存在或已过期，请点「重新开始」") from None

    @app.get("/api/previsit/session/{sid}")
    def session(sid: str) -> dict:
        sess = store.get(sid)
        if not sess:
            raise HTTPException(404, "会话不存在")
        return eng.view(sess)

    @app.get("/api/previsit/record/{sid}")
    def record(sid: str) -> dict:
        """单独取病历：给"复制/下载"用，也方便别处（HIS、导诊台）按 id 拉。"""
        sess = store.get(sid)
        if not sess:
            raise HTTPException(404, "会话不存在")
        rec = sess.get("record")
        if not rec:
            raise HTTPException(409, "问诊尚未结束，病历还没生成")
        return rec

    return app


def _count_sessions(store: Store) -> int:
    try:
        with store._conn() as c:      # noqa: SLF001 - 只读计数，不值得为它加方法
            return int(c.execute("SELECT COUNT(*) FROM session").fetchone()[0])
    except Exception:
        return 0


app = create_app()
