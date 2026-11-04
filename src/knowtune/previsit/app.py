"""预问诊站点入口（主项目桥接版）。

真身：顶层独立包 `previsit`（src/previsit/app.py），那里能脱离主项目独立
部署。这里只做主项目一侧的装配：

- 用主 Config 的数据目录放会话库（data/previsit.db）；
- 挂回主项目的密钥文件（主界面填过的 key 重启后仍能读回）；
- 用 KnowtuneLLMAdapter 把主项目 LLMClient（含 provider/model 选择、mock
  降级）适配成独立引擎的 complete() 接口。

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


_KB_SEARCH = None


def set_kb_search(fn) -> None:
    """主服务把"进程内查库"这个函数交给预问诊（省掉一次 HTTP 往返）。

    签名：`fn(question: str, top_k: int) -> list[dict]`，dict 里带
    doc_title / heading_path / text / score。不注册就走 HTTP 那条路。
    """
    global _KB_SEARCH
    _KB_SEARCH = fn


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
    """主项目侧的引擎装配。api/app.py、frontsite/site.py 共用这一个入口。"""
    cfg = load_config()
    pv = load_previsit()
    secrets_mod.attach(cfg.storage.data_dir)
    store = Store(cfg.storage.data_dir / "previsit.db")
    llm = _main_llm_factory(pv)
    eng = PrevisitEngine(pv, store, llm)
    ret = pv.get("retrieval") or {}
    if _KB_SEARCH is not None and isinstance(ret, dict) and ret.get("enabled"):
        from previsit.retriever import InProcessRetriever

        eng.retriever = InProcessRetriever(
            _KB_SEARCH,
            top_k=int(ret.get("top_k") or 8),
            max_per_doc=int(ret.get("max_per_doc") or 2),
        )
    return eng


def create_app():
    from previsit.app import create_app as _create
    from previsit.app import set_model_catalog, set_llm_factory

    set_model_catalog(model_catalog)
    set_llm_factory(_main_llm_factory)
    return _create(build_engine())


def app():                            # 兼容老用法：uvicorn knowtune.previsit.app:app
    return create_app()
