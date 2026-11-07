"""预问诊的主项目装配层。

引擎在顶层包 `previsit`（src/previsit/），不 import 主项目；这里负责把主项目
这一侧的东西接上去：

- 用主 Config 的数据目录放会话库（data/previsit.db）；
- 挂回主项目的密钥文件（主界面填过的 key 重启后仍能读回）；
- 用 KnowtuneLLMAdapter 把主项目 LLMClient（含 provider/model 选择、mock
  降级）适配成引擎的 complete() 接口；
- 给后台的参数面板提供 `config_describe / config_save / config_reset`——
  预问诊没有独立站点，后台不该为了读几个参数就把整个 app 挂起来。

路由一行不写——都从 previsit.app.create_app(engine) 来。
"""

from __future__ import annotations

from .. import modelcfg, secrets as secrets_mod
from ..config import load_config
from ..generate.llm import LLMClient
from . import KnowtuneLLMAdapter, PrevisitEngine
from .config import load_previsit
from .store import Store
from previsit.llm import GatewayLLM


def model_catalog() -> dict:
    """把主项目的后端注册表交给预问诊的参数面板。

    只搬"选模型需要的字段"：名字、类型、地址、默认模型、模型清单、是否
    本地、是否就绪。密钥一个字节都不出去（面板只需要知道配没配）。
    """
    data = modelcfg.listing(load_config())
    providers = []
    for p in data.get("providers") or []:
        models = [str(m.get("id")) for m in (p.get("models") or [])
                  if isinstance(m, dict) and m.get("id")]
        for mid in p.get("probed") or []:
            if str(mid) not in models:
                models.append(str(mid))
        if p.get("model") and p["model"] not in models:
            models.insert(0, str(p["model"]))
        providers.append({
            "name": p.get("name"),
            "label": p.get("kind_label") or p.get("kind") or p.get("name"),
            "base_url": p.get("base_url") or "",
            "model": p.get("model") or "",
            "local": bool(p.get("local")),
            "ready": bool(p.get("ready")),
            "models": models,
        })
    return {"source": "project", "providers": providers}


def _main_llm_factory(pv: dict) -> object:
    """主项目挂载时的模型客户端：网关优先，降级走主项目的 LLMClient。

    挂在主服务里时，网关就是自己。多这一跳不是为了快，是为了让「预问诊」
    和「别处调用」走同一条链路：模型页换端点、换模型、加密钥，预问诊立刻
    跟着变，不用再单独配一遍；将来把预问诊拆成独立进程，地址一个字不用改。
    """
    cfg = load_config()
    # 老配置里可能钉着已经删掉的后端（lmstudio / siliconflow …）。留着它，
    # 网关每次回「端点不存在」，备用链路再报「未知 provider」，最后整场
    # 问诊悄悄退化成规则兜底。认不出来的名字一律当「跟随当前端点」。
    raw = str(pv.get("provider") or "").strip()
    provider = raw if raw in cfg.generation.providers else ""
    clean = dict(pv)
    clean["provider"] = provider or None
    direct = KnowtuneLLMAdapter(LLMClient(cfg),
                                provider=provider or None,
                                model=pv.get("model") or None)
    return GatewayLLM.wrap(clean, direct)


def build_engine() -> PrevisitEngine:
    """主项目侧的引擎装配。api/app.py、frontsite/site.py 共用这一个入口。

    translator_factory 必须一起给：任何一个"只给了 pv/store/llm 的引擎"都是一颗
    定时炸弹——它照样能开诊、能问问题，只有在维语会话里才会表现成"回中文"，
    而且只在某些装配顺序下发生。工厂挂在引擎上，缺了会在下一个 ug 请求前自愈。
    """
    from previsit.app import build_translator

    cfg = load_config()
    pv = load_previsit()
    secrets_mod.attach(cfg.storage.data_dir)
    store = Store(cfg.storage.data_dir / "previsit.db")
    llm = _main_llm_factory(pv)
    return PrevisitEngine(pv, store, llm, translator_factory=build_translator)


def _bind_main() -> None:
    """把主项目的模型目录与模型工厂交给预问诊那一层（只做一次也无妨）。"""
    from previsit.app import set_llm_factory, set_model_catalog

    set_model_catalog(model_catalog)
    set_llm_factory(_main_llm_factory)


def ensure_engine():
    """后台读/写参数时用的引擎。

    必须复用 previsit 侧那个进程级单例：参数面板报的"现在实际在用什么"要是
    真在跑的那一份。另建一个引擎，面板会显示一套、站点跑另一套——最典型的是
    保存完提示成功、问诊里却还是老参数。没有就按主项目这一侧装配一个。
    """
    from previsit import app as pv_app

    if getattr(pv_app, "_engine", None) is None:
        _bind_main()
        pv_app.create_app(build_engine())
    return pv_app._engine


def config_describe() -> dict:
    from previsit import app as pv_app

    ensure_engine()
    return pv_app.describe_config()


def config_save(values: dict) -> dict:
    from previsit import app as pv_app

    ensure_engine()
    return pv_app.save_config(values)


def config_reset() -> dict:
    from previsit import app as pv_app

    ensure_engine()
    return pv_app.reset_config()


def create_app():
    from previsit.app import create_app as _create

    _bind_main()
    return _create(build_engine())


def app():                            # 兼容老用法：uvicorn knowtune.previsit.app:app
    return create_app()
