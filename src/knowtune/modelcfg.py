"""端点注册表 —— `config/models.yaml` 的读写。

一个端点 = 一个 OpenAI 兼容地址 + 一个密钥 + 它下面的一组模型。只有两类：

  local   本机能起。模型带 `.gguf` 路径，界面直接给「启动 / 停止」。
  openai  别人的地址。云端、自建网关、容器里的服务，一律算这一类。

**为什么把八种 kind 砍成两种**：vllm / lmstudio / ollama / deepseek / bailian /
openai / custom / mock 说的是同一个协议，区别只在"谁在跑"和"怎么起"。云端的
"怎么起"根本没答案，本地的正确答案又早就不止一种（llama.cpp / LM Studio /
Ollama 各自的命令都不一样，写死在代码里必然过期）。类型越多，过期的提示越多。

**落盘只写差异**：与 `default.yaml` 相同的条目不再抄一遍。整份拷贝等于永久
冻结当时的出厂值，以后默认值改了，用户机器上还在跑旧的，而且没人看得出为什么。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from . import paths as paths_mod
from .config import ROOT, Config, ModelEntry, ProviderCfg, _read_yaml, defaults_config

MODELS_CONFIG = ROOT / "config" / "models.yaml"

# 两种端点。label 只用于界面分组，没有启动命令、没有提示文案——那些要么在
# localmodels.py 里按真实环境算，要么根本不该有。
KINDS: tuple[dict[str, Any], ...] = (
    {
        "id": "local",
        "label": "本地（llama.cpp）",
        "local": True,
        "default_base": "http://127.0.0.1:8090/v1",
        "hint": "选一个本地 .gguf，点「启动」就会起一个 OpenAI 兼容端点；模型和端口都跟模型走。",
    },
    {
        "id": "openai",
        "label": "自定义 OpenAI 兼容",
        "local": False,
        "default_base": "https://api.openai.com/v1",
        "hint": "填地址 + 密钥 + 模型名即可。任何实现 /v1/chat/completions 的服务都算。",
    },
)
KIND_MAP = {k["id"]: k for k in KINDS}
DEFAULT_KIND = "openai"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")


def _norm_kind(kind: str | None, base_url: str | None = None) -> str:
    """老配置里可能写着 vllm / lmstudio / deepseek 这些已删的类型。

    归类规则：
      - vllm / lmstudio / ollama 一定在跑在本机，算 local；
      - custom / 空 / 不认识 —— 只能看地址。老配置里的 custom 既可能是本机的
        llama.cpp，也可能是别人的网关，按名字猜必错；
      - 其余（deepseek / bailian / openai …）都是云端。
    不能抛异常：配置文件是旧版本留下的，抛了服务就起不来。
    """
    k = (kind or "").strip().lower()
    if k in KIND_MAP:
        return k
    if k in {"vllm", "lmstudio", "ollama"}:
        return "local"
    host = (urlparse(base_url or "").hostname or "").lower()
    return "local" if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"} else DEFAULT_KIND


def kind_of(name: str, base_url: str | None = None) -> dict[str, Any]:
    return KIND_MAP.get(_norm_kind(name, base_url), KIND_MAP[DEFAULT_KIND])


# --------------------------------------------------------------------- 文件


def read() -> dict[str, Any]:
    return _read_yaml(MODELS_CONFIG)


def persist(cfg: Config) -> dict[str, Any]:
    """把端点清单 + 角色分工写回 models.yaml。对外只暴露这一个入口。"""
    return _persist(cfg)


def _spec_of(p: ProviderCfg) -> dict[str, Any]:
    spec: dict[str, Any] = {"base_url": p.base_url, "model": p.model, "kind": p.kind}
    # 只在打开时写，别把每个端点都灌一个 autostart: false 进去——那会让
    # 「和 default.yaml 是否有差异」的判断全变成"有差异"，每次启动都重写文件。
    if p.autostart:
        spec["autostart"] = True
    if p.model_list:
        # 界面「扫描模型」给出的是绝对路径。写回配置前统一归一成项目根相对路径，
        # 否则用户在界面里点一次保存，盘符又悄悄回到 models.yaml 里。
        items: list[dict[str, Any]] = []
        for m in p.model_list:
            d = m.model_dump(exclude_none=True)
            if d.get("path"):
                d["path"] = paths_mod.stored_path(d["path"])
            items.append(d)
        spec["models"] = items
    if p.api_key_env:
        spec["api_key_env"] = p.api_key_env
    return spec


def _persist(cfg: Config) -> dict[str, Any]:
    base = defaults_config().generation.providers
    out: dict[str, Any] = {}
    deleted: list[str] = []
    for name, p in cfg.generation.providers.items():
        spec = _spec_of(p)
        b = base.get(name)
        if b is not None and spec == _spec_of(b):
            continue
        out[name] = spec
    for name in base:
        if name not in cfg.generation.providers:
            deleted.append(name)

    from .roles import normalize

    roles = normalize(getattr(cfg.generation, "roles", {}),
                      known=cfg.generation.providers)

    if not out and not deleted and not roles:
        if MODELS_CONFIG.exists():
            MODELS_CONFIG.unlink()
        return {"path": str(MODELS_CONFIG), "removed": True, "providers": {}, "deleted": []}

    header = (
        "# 端点注册表。只记录与 default.yaml 不同的条目。\n"
        "#\n"
        "# 路径口径：本地 `path` 写**项目根相对路径**（用 / 分隔，基准是仓库根目录），\n"
        "# 不写盘符。目录改名/换盘/拷到别的机器时绝对路径会立刻失效。项目外的\n"
        "# 模型可以用 `../` 往上走，也可以在界面里改。\n"
        "#\n"
        "# providers:\n"
        "#   <名字>:                     小写字母数字，2-24 字符\n"
        "#     kind: local | openai      local 表示模型带 .gguf、本机能起\n"
        "#     autostart: true           可选；后台启动时自动拉起默认模型\n"
        "#     base_url: http://...      OpenAI 兼容地址，末尾到 /v1\n"
        "#     model: <默认模型 id>\n"
        "#     api_key_env: <变量名>      可选；密钥也可以只放界面里\n"
        "#     models:\n"
        "#       - id: <发给接口的 model 字段>\n"
        "#         path: data/models/x.gguf  本地 gguf（相对项目根）；有这个才能点「启动」\n"
        "#         port: 8090              本机监听端口；不写就自动挑\n"
        "#         context: 4096           上下文长度\n"
        "#         max_output: 1024        单次输出上限\n"
        "#\n"
        "# roles: 哪个端点+模型干哪件事，值写成 `端点::模型`\n"
        "#   translate: local::qwen2.5-3b -- 翻译用\n"
        "#   generate:  local::qwen2.5-3b -- 生成答案用\n\n"
    )
    body: dict[str, Any] = {"deleted": deleted, "providers": out}
    if roles:
        body["roles"] = roles
    MODELS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    MODELS_CONFIG.write_text(
        header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"path": str(MODELS_CONFIG), "removed": False, "providers": out, "deleted": deleted}


# --------------------------------------------------------------------- 校验


def validate_url(url: str, *, allow_empty: bool = False) -> str:
    url = (url or "").strip()
    if not url:
        if allow_empty:
            return ""
        raise ValueError("base_url 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url 必须以 http:// 或 https:// 开头")
    if not parsed.netloc:
        raise ValueError("base_url 缺少主机名")
    if " " in url:
        raise ValueError("base_url 里不能有空格")
    return url.rstrip("/")


def _num(val: Any, cap: int = 100_000_000) -> int | None:
    if val in (None, "", 0, "0"):
        return None
    try:
        n = int(str(val).strip().lower().replace(",", ""))
    except (TypeError, ValueError):
        return None
    return n if 0 < n <= cap else None


def clean_models(raw: list[dict[str, Any]] | None) -> list[ModelEntry]:
    """清洗模型清单：去空行、去重、长度做上限，坏值不抛异常只忽略。

    清单多半来自手填或扫描，脏数据是常态（空行、重复 id、把上下文填成 "128k"）。
    原则是"能救就救，救不了就丢"，绝不因为一行写错就让整份配置存不下去。
    """
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            # 不接受 `- qwen2.5` 这种简写：一个模型现在带 path / port / context，
            # 光有名字说明不了它本机能不能起、该占哪个端口。留个裸字符串在这里
            # 只会让"点了启动没反应"变成常态。
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen or len(out) >= 50:
            continue
        seen.add(mid)
        path = str(item.get("path") or "").strip() or None
        port = _num(item.get("port"), 65535)
        out.append(ModelEntry(
            id=mid[:120],
            alias=str(item.get("alias") or "").strip()[:80],
            context=_num(item.get("context")),
            max_output=_num(item.get("max_output")),
            path=path[:400] if path else None,
            port=port,
        ))
    return out


def upsert(
    cfg: Config,
    name: str,
    *,
    base_url: str,
    model: str,
    kind: str = DEFAULT_KIND,
    models: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """新增或修改一个端点。改完立刻生效——LLMClient 持有的是同一个 cfg 对象。"""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("名字只能用小写字母、数字、- 和 _，2-24 个字符（它会被写进配置文件）")
    kind = _norm_kind(kind, base_url)

    model = (model or "").strip()
    if not model:
        raise ValueError("模型名不能为空（本地模型填文件名，云端填接口返回的 id）")
    if len(model) > 120:
        raise ValueError("模型名过长")

    clean_url = validate_url(base_url) or None
    # 密钥不在这里存：它由调用方放进内存注册表（knowtune.secrets），
    # 这里只管地址/模型/类型这些"可以见人"的字段。
    #
    # models 传 None 表示"这次不改清单"——卡片上切个模型只改 model 一个字段，
    # 要是把 None 当成"清空"，切换一次模型就把人维护的清单全删了。
    prev = cfg.generation.providers.get(name)
    if models is None:
        entries = list(prev.model_list) if prev else []
    else:
        entries = clean_models(models)
    if entries and model not in {m.id for m in entries}:
        entries.insert(0, ModelEntry(id=model))
    cfg.generation.providers[name] = ProviderCfg(
        base_url=clean_url, model=model, kind=kind, model_list=entries,
        api_key_env=prev.api_key_env if prev else None,
        # 界面上改个地址/模型不能把「开机自启」顺手关掉：那不是表单字段，
        # 而是这份端点"怎么跑"的约定，只有手改 models.yaml 才能改。
        autostart=prev.autostart if prev else False,
    )
    if cfg.generation.provider not in cfg.generation.providers:
        cfg.generation.provider = name
    return {"name": name, **_persist(cfg), "active": cfg.generation.provider}


def remove(cfg: Config, name: str) -> dict[str, Any]:
    if name not in cfg.generation.providers:
        raise ValueError("端点不存在")
    if len(cfg.generation.providers) <= 1:
        raise ValueError("至少要留一个端点")
    if cfg.generation.provider == name:
        raise ValueError(f"「{name}」是当前使用的端点，先切换到别的再删")
    del cfg.generation.providers[name]
    return {"deleted": name, **_persist(cfg)}


def set_active(cfg: Config, name: str) -> dict[str, Any]:
    if name not in cfg.generation.providers:
        raise ValueError("端点不存在")
    cfg.generation.provider = name
    return {"active": name}


# --------------------------------------------------------------------- 展示


def readiness(cfg: Config, name: str | None = None) -> dict[str, Any]:
    """这个端点"配好了没有"——注意与"连不连得通"是两件事。

    界面上必须把这两者分开：地址填错、端口没起是运行时故障，重试或改地址就能救；
    而没填模型名、没配密钥是配置缺失，重试一万次也没用，只能去补字段。
    """
    target = name or cfg.generation.provider
    p = cfg.generation.providers.get(target)
    if p is None:
        return {"provider": target, "ready": False, "code": "no_provider",
                "reason": f"端点「{target}」不在注册表里，重新选一个"}
    # mock 是"没有任何端点时验证链路"的替身，不需要地址。
    if target == "mock" or ((p.model or "").strip() == "mock" and not (p.base_url or "").strip()):
        return {"provider": target, "ready": True, "code": "mock", "reason": ""}
    if not (p.base_url or "").strip():
        return {"provider": target, "ready": False, "code": "no_url", "reason": "还没填地址"}
    if not (p.model or "").strip():
        return {"provider": target, "ready": False, "code": "no_model", "reason": "还没填模型名"}
    # 本地端点不强求密钥（自建服务常常不鉴权），云端必须有。
    if _norm_kind(p.kind, p.base_url) == "openai" and not p.api_key(target):
        return {"provider": target, "ready": False, "code": "no_key",
                "reason": "缺密钥：点编辑填 API Key"}
    return {"provider": target, "ready": True, "code": "ok", "reason": ""}


def _model_view(m: ModelEntry, state: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": m.id,
        "label": m.label,
        "context": m.context,
        "max_output": m.max_output,
        "path": m.path,
        "port": m.port,
        "local": m.local,
        "state": (state or {}).get("state", "unknown") if m.local else "",
        "pid": (state or {}).get("pid"),
        "running_url": (state or {}).get("base_url", ""),
    }


def listing(cfg: Config) -> dict[str, Any]:
    from . import localmodels as local_mod
    from . import secrets as secrets_mod

    base = defaults_config().generation.providers
    live = local_mod.running()
    items = []
    for name, p in cfg.generation.providers.items():
        kind = _norm_kind(p.kind, p.base_url)
        meta = kind_of(kind)
        ready = readiness(cfg, name)
        items.append({
            "name": name,
            "kind": kind,
            "kind_label": meta["label"],
            "local": bool(meta["local"]),
            "base_url": p.base_url,
            "model": p.model,
            "key_present": bool(p.api_key(name)),
            "models": [_model_view(m, live.get(_live_key(name, m))) for m in p.model_list],
            "active": name == cfg.generation.provider,
            "builtin": name in base,
            "ready": ready["ready"],
            "ready_reason": ready["reason"],
            "hint": meta["hint"],
        })
    items.sort(key=lambda x: (not x["active"], not x["local"], x["name"]))

    # 扁平目录：所有端点 × 所有模型，一条一个可选项。
    #
    # 为什么要有它：对话页的"生成模型"以前只能列当前端点的候选，想用另一个
    # 端点上的某个模型，得先切端点再切模型——两层嵌套，第二层还常常是空的。
    # 看起来就是"我加过的模型选不到"。这里压成一层。
    catalog = []
    for name, p in cfg.generation.providers.items():
        kind = _norm_kind(p.kind, p.base_url)
        ready = readiness(cfg, name)
        ids = [m.id for m in p.model_list]
        if p.model and p.model not in ids:
            ids.insert(0, p.model)
        for mid in ids:
            if not mid:
                continue
            entry = p.entry(mid)
            catalog.append({
                "provider": name,
                "model": mid,
                "label": entry.label if entry else mid,
                "current": bool(p.model) and mid == p.model,
                "provider_active": name == cfg.generation.provider,
                "ready": ready["ready"],
                "local": kind == "local",
            })

    active_p = cfg.generation.providers.get(cfg.generation.provider)
    return {
        "active": cfg.generation.provider,
        "active_ready": readiness(cfg)["ready"],
        "active_reason": readiness(cfg)["reason"],
        # 全局"当前"是「端点 + 模型」两段，分开给，界面才能一次说清"现在在用哪套"
        "active_model": active_p.model if active_p else "",
        "providers": items,
        "catalog": catalog,
        "kinds": [dict(k) for k in KINDS],
        "keys_persisted": secrets_mod.persisted(),
        "key_autosave": secrets_mod.autosave(),
        "key_cipher": secrets_mod.backend_label(),
        "env": local_mod.describe_env(),
        "file": {"path": str(MODELS_CONFIG), "exists": MODELS_CONFIG.exists()},
    }


def _live_key(endpoint: str, m: ModelEntry) -> str:
    """本地实例的状态按什么名字存。

    用 `端点/模型` 而不是模型 id：两个端点下可能有同名模型（一个本机一个云端），
    只按模型名存会让它们互相覆盖状态。
    """
    return f"{endpoint}/{m.id}"
