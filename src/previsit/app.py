"""预问诊的 HTTP 入口（只有 API，没有页面）。

预问诊的界面画在维医站点里（左侧第三个板块），这里只提供接口：

    /api/previsit/meta      界面要的文案
    /api/previsit/start     开一场问诊
    /api/previsit/reply     答一句
    /api/previsit/config*   参数面板（后台 /api/previsit/* 也调同一套函数）

数据与依赖边界：

- 配置：config/previsit.yaml（文案 + 模型参数 + 一段提示词）；
- 会话数据：`$PREVISIT_DATA_DIR/previsit.db`；
- 模型：由主项目注入（跟随后台当前后端）。这是**硬依赖**——壳子没有规则
  兜底，模型接不上就如实报错，不编一句话糊过去。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import settings as settings_mod
from .chat import ChatEngine, ChatError
from .config import ROOT, load_previsit
from .llm import (GatewayLLM, OpenAICompatLLM, TranslatorChain, build_llm,
                  gateway_url)
from .store import Store


def data_dir() -> Path:
    env = os.environ.get("PREVISIT_DATA_DIR", "").strip()
    if env:
        p = Path(env)
    else:
        p = ROOT / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


_engine: ChatEngine | None = None
_store: Store | None = None

# 模型后端清单的来源。主项目挂载时会把自己的注册表交进来（那时才有
# providers/model_list 可列）；独立部署退回读旁边的 config/models.yaml。
_MODEL_CATALOG: Any = None
# 模型客户端工厂。主项目挂载时注入自己的 LLMClient 适配器，独立部署时退回
# OpenAICompatLLM；两边都统一走「网关优先 + 降级」那一层。
_LLM_FACTORY: Any = None


def set_model_catalog(fn: Any) -> None:
    """主项目把"有哪些后端和模型"这件事交给预问诊的参数面板。"""
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


def get_engine() -> ChatEngine:
    """进程级单例：两份配置、两个 Store 句柄会出现"改了不生效"的排查陷阱。"""
    global _engine, _store
    if _engine is None:
        pv = load_previsit()
        _store = Store(data_dir() / "previsit.db")
        main_llm = build_llm_for(pv)
        # 维语翻译模型：复用主链路的网关地址（保证可达），但强制用强模型翻译
        # —— 本地 7B 扛不了维语，必须走网关上的强模型（实测 DeepSeek-V4 中英↔维语
        # 干净）。装配失败不影响起站：工厂留在引擎上，第一次 ug 请求前会再试一次。
        _engine = ChatEngine(pv, _store, main_llm,
                             translator=build_translator(pv, main_llm),
                             translator_factory=build_translator)
    return _engine


def build_translator(pv: dict[str, Any], main_llm: Any) -> Any:
    """维语翻译客户端链：和主问答共用网关地址（可达性有保证），但模型显式指定
    为强模型——本地 7B 做不了维语翻译，若用网关默认模型（很可能就是那台 7B）会
    退回中文。

    模型来源优先级：参数面板/覆盖层 > 环境变量 > 内置默认。
    链的顺序：配置的模型 → 网关当前默认模型。第二条不是摆设：配置里那个模型
    被下线/限流时，网关默认模型往往还能翻，比直接报错强。
    `retry_after=0` 是有意的：默认那 20 秒熔断窗口是为主问诊设计的（别每轮都
    白等一次超时），翻译链不行——它失败一次的代价是患者这一轮直接看不到回答，
    下一句必须重新试。"""
    base = getattr(main_llm, "base_url", "") or gateway_url(pv)
    if not base:
        return None
    provider = str(pv.get("translator_provider") or "").strip()
    model = (str(pv.get("translator_model") or "").strip()
             or os.environ.get("PREVISIT_TRANSLATOR_MODEL")
             or "deepseek-ai/DeepSeek-V4.1-Flash")
    try:
        clients = [GatewayLLM(base, provider=provider, model=model,
                              timeout_s=20.0, retry_after=0.0)]
        if model:                          # 兜底：让网关挑它当前的默认模型
            clients.append(GatewayLLM(base, provider=provider, model="",
                                      timeout_s=20.0, retry_after=0.0))
        return TranslatorChain(clients)
    except Exception:                          # 网关不在就别让站点起不来
        return None


def reload_engine() -> dict:
    """把磁盘上的配置重新灌进引擎（参数面板保存后调用）。

    不重建引擎：会话流水在 Store 里，换对象等于把正在等模型的那一轮掐掉。
    这里只换配置和模型客户端（端点 / 模型 / 密钥都可能被面板改掉）。
    """
    eng = get_engine()
    pv = load_previsit()
    eng.apply_config(pv)
    eng.llm = build_llm_for(pv)
    # 翻译客户端也必须跟着重建：它粘的是主模型的网关地址，面板一改端点，
    # 旧 translator 会继续往老地址发请求，维语会话静默退回中文。
    eng.set_translator(build_translator(pv, eng.llm))
    return pv


def get_store() -> Store:
    get_engine()
    assert _store is not None
    return _store


class StartReq(BaseModel):
    # 主诉可以空着：哪里不舒服由模型第一句来问；患者直接开口的第一句话
    # 就是主诉（当作他说的第一句）。
    complaint: str = Field(default="", max_length=2000)
    # 作答语言（界面语言）。预问诊不检索资料，答案直接按界面语言给；
    # 不传就按中文默认。后端据此选整段提示词的语言。
    ui_lang: str = Field(default="", max_length=8)


class ReplyReq(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    ui_lang: str = Field(default="", max_length=8)


class LocalizeReq(BaseModel):
    """把已有流水补成某种语言（目前只补维吾尔语）。"""
    session_id: str = Field(min_length=1, max_length=64)
    ui_lang: str = Field(default="", max_length=8)


class ConfigReq(BaseModel):
    # {字段路径: 值}，路径形如 "max_tokens" / "prompts.chat"。
    # 只认 settings.FIELDS 里声明过的键，其余静默丢弃——面板是白名单。
    values: dict[str, Any] = Field(default_factory=dict)


def _runtime(eng: ChatEngine) -> dict:
    """参数面板顶部那行"现在实际在用什么"：配置写的是想要什么，这里报真的。"""
    llm = eng.llm
    gw = isinstance(llm, GatewayLLM)
    tr = eng.translator
    chain = tr.models() if isinstance(tr, TranslatorChain) else []
    return {
        "provider": eng.provider or "",
        "model": eng.model or "",
        # 界面上要能看出「到底走没走固定入口」。配了网关、实际在跑直连，
        # 是最难自己发现的一类问题。
        "llm_source": (getattr(llm, "source", "") if gw else ("direct" if llm else "none")),
        "llm_gateway": llm.base_url if gw else "",
        "llm_gateway_error": llm.last_error if gw else "",
        # 维语翻译模型（外部）。base/model 都对得上才说明翻译链路通了；
        # error 不为空就是"维语会话会报错"的现场证据（以前静默退回中文，
        # 面板上看不出任何异常）。
        "translator": bool(tr),
        "translator_base": getattr(tr, "base_url", "") if tr else "",
        "translator_model": (getattr(tr, "_model", "")
                             or getattr(tr, "model", "") or "") if tr else "",
        "translator_chain": chain,
        "translator_error": eng.translator_error,
        "temperature": eng.pv.get("temperature"),
        "max_tokens": eng.pv.get("max_tokens"),
        "timeout_s": eng.timeout_s,
        "llm": bool(eng.llm),
        "llm_error": eng.last_error,
    }


# ---------------------------------------------------------------- 参数面板（函数层）
# 这三个函数是"参数面板"的唯一实现，HTTP 路由和后台 /api/previsit/* 都调它们。
def describe_config() -> dict:
    data = settings_mod.describe(model_catalog())
    data["runtime"] = _runtime(get_engine())
    return data


def save_config(values: dict[str, Any]) -> dict:
    """保存面板上的改动，并让新参数立刻生效（不重启、不打断正在进行的问诊）。"""
    res = settings_mod.apply(values)      # 越界 / 类型不对 → ValueError
    reload_engine()
    data = settings_mod.describe(model_catalog())
    data["saved"] = res["saved"]
    data["runtime"] = _runtime(get_engine())
    return data


def reset_config() -> dict:
    settings_mod.reset()
    reload_engine()
    data = settings_mod.describe(model_catalog())
    data["runtime"] = _runtime(get_engine())
    return data


def create_app(engine: ChatEngine | None = None) -> FastAPI:
    """engine 可注入：挂进主项目时传入（用主项目的 LLM 后端和密钥体系）。

    宿主注入引擎会跳过 get_engine() 里的翻译装配，所以这里补一刀：注入的引擎
    若没挂维语翻译模型，就地给它装上（否则 ug 会话拿不到翻译，退回中文）。"""
    if engine is not None:
        global _engine, _store
        _engine = engine
        _store = engine.store
        # 宿主注入的引擎是裸的（build_engine 只给了 pv/store/llm）。这里补两刀：
        # 先挂工厂（缺了能自愈），再当场装一个（面板的「运行时」要能立刻看到）。
        engine.translator_factory = build_translator
        if not getattr(engine, "translator", None):
            engine.set_translator(build_translator(engine.pv, engine.llm))
    eng = get_engine()
    store = get_store()
    pv = eng.pv

    app = FastAPI(title="预问诊", version="0.4.0")

    @app.get("/api/previsit/meta")
    def meta() -> dict:
        return {
            "enabled": bool(pv.get("enabled", True)),
            "title": pv.get("title"),
            "subtitle": pv.get("subtitle"),
            "welcome": pv.get("welcome"),
            "disclaimer": pv.get("disclaimer"),
            "placeholder": pv.get("placeholder"),
            # 适配器（主项目）没有 provider 属性，退到配置里的名字，否则整页
            # meta 会 500——患者看到的是"连不上服务"。
            "provider": (getattr(eng.llm, "provider", None)
                         or eng.provider or None),
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
    @app.get("/api/previsit/config")
    def get_config() -> dict:
        return describe_config()

    @app.put("/api/previsit/config")
    def put_config(req: ConfigReq) -> dict:
        try:
            return save_config(req.values)
        except ValueError as exc:      # 越界/类型不对：直接告诉用户哪一项不对
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/previsit/config/reset")
    def reset_config_route() -> dict:
        return reset_config()

    @app.post("/api/previsit/start")
    def start(req: StartReq) -> dict:
        # 参数面板上的总开关。关掉之后不让开新会话——入口还在，但点了会说
        # 明没开放，而不是让患者对着空白页猜。
        if not eng.pv.get("enabled", True):
            raise HTTPException(403, "预问诊当前未开放，请到人工分诊台就诊")
        try:
            return eng.start(req.complaint.strip(), lang=req.ui_lang or None)
        except ChatError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/previsit/reply")
    def reply(req: ReplyReq) -> dict:
        try:
            return eng.reply(req.session_id, req.message.strip(), lang=req.ui_lang or None)
        except KeyError:
            raise HTTPException(404, "会话不存在或已过期，请点「重新开始」") from None
        except ChatError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/previsit/localize")
    def localize(req: LocalizeReq) -> dict:
        """切到某种语言时，把还没有该语言版本的答复翻过去。

        和 `/api/ask` 同一件事的另一种时机：那边是"生成时就产出目标语言"，
        这边是"切过来时才补翻译"——因为预问诊一场会话会被反复来回切，
        提前翻全部是浪费，切到哪补到哪、补完写库，只花一次。"""
        try:
            return eng.localize(req.session_id, lang=req.ui_lang or None)
        except KeyError:
            raise HTTPException(404, "会话不存在或已过期，请点「重新开始」") from None

    @app.get("/api/previsit/session/{sid}")
    def session(sid: str) -> dict:
        sess = store.get(sid)
        if not sess:
            raise HTTPException(404, "会话不存在")
        return eng.view(sess)

    return app


def _count_sessions(store: Store) -> int:
    try:
        with store._conn() as c:      # noqa: SLF001 - 只读计数，不值得为它加方法
            return int(c.execute("SELECT COUNT(*) FROM session").fetchone()[0])
    except Exception:
        return 0

# 不再有模块级 `app = create_app()`：预问诊没有自己的进程，app 只由调用方
# 按需创建（站点 build_site 里 mount、后台侧只取参数面板那几个函数）。
