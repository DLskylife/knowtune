"""后端注册表 —— `config/models.yaml` 的读写。

模型管理页要能"加一个本地端口、删掉用不上的云端、改个模型名"，这些都不该
逼用户去编辑 YAML 再重启。但也不能把整份 providers 抄进一个文件了事：整份
拷贝等于永久冻结当时的默认值，以后 default.yaml 改了推荐端口，用户机器上
还在跑旧的，而且没人看得出为什么。

所以这里只落**差异**：与出厂值不同的条目 + 被显式删除的内置名字。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .config import ROOT, Config, ModelEntry, ProviderCfg, _read_yaml, defaults_config

MODELS_CONFIG = ROOT / "config" / "models.yaml"

# 内置的几类后端。kind 只影响 UI 提示（启动命令、本地/云端徽标），
# 协议永远只有一种：OpenAI 兼容 /v1/chat/completions。
#
# launch 按平台分开写，并且端口用 `{port}` 占位：
#   · 用户机器是 Windows，一条 `vllm serve ...` 复制过去根本执行不了，
#     而 vLLM 在 Windows 原生压根跑不起来（得走 WSL2）——这类坑必须写在命令里，
#     不是让人事后去搜报错；
#   · 端口写死会和卡片上的地址对不上：用户把 LM Studio 起在 1235，命令却让
#     他 --port 1234，照抄一遍服务起了、探测还是红的。
# 所以渲染时按「当前系统 + 该类型现有实例的端口」把占位符替换掉（见 kinds()）。
KINDS: tuple[dict[str, Any], ...] = (
    {
        "id": "vllm",
        "label": "vLLM",
        "local": True,
        # 8100，不是 8000：8000 是本系统自己的 Web 服务端口，两个撞在一起时
        # 用户只会看到"vLLM 起不来"，查半天不知道是被自己占了。
        "default_base": "http://127.0.0.1:8100/v1",
        "default_model": "Qwen2.5-7B-Instruct",
        "launch": {
            "posix": ["python -m vllm.entrypoints.openai.api_server"
                      " --model Qwen/Qwen2.5-7B-Instruct --port {port} --max-model-len 8192"],
            "win": ["wsl -- python -m vllm.entrypoints.openai.api_server"
                    " --model Qwen/Qwen2.5-7B-Instruct --port {port} --max-model-len 8192"],
        },
        "launch_note": "Windows 原生跑不了 vLLM，要在 WSL2 里起（上面这条已带 wsl 前缀）。",
        "hint": "先起服务，再回上面点「测试联通」。OpenAI 兼容接口在 /v1 下。",
    },
    {
        "id": "lmstudio",
        "label": "LM Studio",
        "local": True,
        "default_base": "http://127.0.0.1:1234/v1",
        "default_model": "qwen2.5-7b-instruct",
        "launch": {
            "posix": ["lms server start --port {port}"],
            "win": ["lms server start --port {port}",
                    "# 或者不用命令行：LM Studio → Local Server → 端口改成 {port} → Start"],
        },
        "launch_note": "lms 命令行要在 LM Studio 里先打开（Settings → Developer → Enable CLI）；"
                       "用图形界面起服务同样可以。",
        "hint": "模型要先在 LM Studio 里 Load 进内存，否则 /v1/models 返回空列表。",
    },
    {
        "id": "ollama",
        "label": "Ollama",
        "local": True,
        "default_base": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5:7b",
        "launch": {
            "posix": ["ollama pull qwen2.5:7b", "ollama serve"],
            "win": ["ollama pull qwen2.5:7b", "ollama serve"],
        },
        "launch_note": "装完 Ollama 后台服务一般已经在跑；只有 11434 连不上时才手动 serve。"
                       "pull 必须在 serve 之后（或后台已运行时）执行。",
        "hint": "Ollama 自带 OpenAI 兼容层，直接用 /v1 路径即可。",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "local": False,
        "default_base": "https://api.deepseek.com",
        # 不预设模型名：DeepSeek 的模型线换过好几轮（chat / reasoner / flash / v4…），
        # 预设一个过期的名字只会让新用户第一步就撞 404。填好密钥点"探测"，
        # 从返回的列表里点选真实存在的那个。
        "default_model": "",
        "launch": {},
        "launch_note": "",
        "hint": "密钥在弹窗里直接填（只存内存，服务重启后需重填）。"
                "填完点「探测」，从返回的模型列表里点选一个。",
    },
    {
        "id": "bailian",
        "label": "阿里云百炼（DashScope）",
        "local": False,
        # 官方文档：华北2(北京) 的 OpenAI 兼容入口。
        # 新加坡地域是 https://dashscope-intl.aliyuncs.com/compatible-mode/v1
        "default_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "launch": {},
        "launch_note": "",
        "hint": "百炼的 OpenAI 兼容入口；换成新加坡地域要把地址改成 dashscope-intl。"
                "密钥在弹窗里直接填，只存内存。",
    },
    {
        "id": "openai",
        "label": "OpenAI 兼容云 API",
        "local": False,
        "default_base": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "launch": {},
        "launch_note": "",
        "hint": "密钥在弹窗里直接填，只存内存、不落任何配置文件。",
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI 兼容服务",
        "local": True,
        "default_base": "http://127.0.0.1:8080/v1",
        "default_model": "",
        # 以前这里只有一行注释（"任何实现 /v1/chat/completions 的服务都行"），
        # 看着像给了命令，复制过去却什么都不会发生。给一条能照抄的真例子。
        "launch": {
            "posix": ["llama-server -m /path/to/model.gguf --port {port}"],
            "win": ["llama-server.exe -m C:\\path\\to\\model.gguf --port {port}"],
        },
        "launch_note": "例子是 llama.cpp；TGI / one-api / 任何实现 /v1/chat/completions"
                       " 的服务都行，把命令换成你自己的即可。",
        "hint": "填你自己的端口即可。启动顺序无所谓，随时可以回来点探测。",
    },
    {
        "id": "mock",
        "label": "Mock（不联网）",
        "local": True,
        "default_base": "",
        "default_model": "mock",
        "launch": {},
        "launch_note": "",
        "hint": "只用来验证「检索→装填→引用校验→SSE」链路，输出不是答案。",
    },
)
KIND_MAP = {k["id"]: k for k in KINDS}
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
WINDOWS = os.name == "nt"


def _port_of(url: str | None, default: str = "") -> str:
    try:
        p = urlparse(url or "").port
    except ValueError:
        return default
    return str(p) if p else default


def _render_launch(kind: dict[str, Any], port: str) -> dict[str, str]:
    """把 {port} 换成实际端口，并按当前系统挑一套命令出来。"""
    spec = kind.get("launch") or {}
    def fmt(lines: list[str] | None) -> str:
        return "\n".join((ln or "").replace("{port}", port) for ln in (lines or [])).strip()
    win, posix = fmt(spec.get("win")), fmt(spec.get("posix"))
    if WINDOWS:
        return {"launch": win or posix, "launch_alt": posix if win else "",
                "launch_os": "Windows"}
    return {"launch": posix or win, "launch_alt": win if posix else "",
            "launch_os": "Linux / macOS"}


def kinds(cfg: Config | None = None) -> list[dict[str, Any]]:
    """给 UI 的类型清单。`launch` 已经是能直接复制的、属于当前系统的命令。"""
    out = []
    for k in KINDS:
        item = dict(k)
        # 端口取自该类型现有实例的 base_url：用户把服务开在 1235，命令就该写 1235
        port = _port_of(k.get("default_base"))
        if cfg is not None:
            for name, p in cfg.generation.providers.items():
                if (p.kind or name) == k["id"] and (p.base_url or ""):
                    port = _port_of(p.base_url, port)
                    break
        item.update(_render_launch(k, port or "8080"))
        out.append(item)
    return out


def kind_of(name: str) -> dict[str, Any]:
    return KIND_MAP.get(name, KIND_MAP["custom"])


# --------------------------------------------------------------------- 文件


def persist(cfg: Config) -> dict[str, Any]:
    """把后端清单 + 角色分工写回 models.yaml。对外只暴露这一个入口：
    调用方不需要知道落盘的细节，也不该绕过清洗直接写文件。"""
    return _persist(cfg)


def read() -> dict[str, Any]:
    return _read_yaml(MODELS_CONFIG)


def _persist(cfg: Config) -> dict[str, Any]:
    base = defaults_config().generation.providers
    out: dict[str, Any] = {}
    deleted: list[str] = []
    for name, p in cfg.generation.providers.items():
        spec: dict[str, Any] = {
            "base_url": p.base_url,
            "model": p.model,
            "kind": p.kind,
        }
        if p.model_list:
            spec["model_list"] = [m.model_dump(exclude_none=True) for m in p.model_list]
        b = base.get(name)
        same = b is not None and spec == {
            "base_url": b.base_url,
            "model": b.model,
            "kind": b.kind,
        }
        if not same:
            out[name] = spec
    for name in base:
        if name not in cfg.generation.providers:
            deleted.append(name)

    from .roles import normalize

    # 角色分工跟后端清单同住一份文件：删掉某个后端时，指向它的角色也一起失效，
    # 不会留下一个"翻译成空气"的配置。
    roles = normalize(getattr(cfg.generation, "roles", {}),
                      known=cfg.generation.providers)

    if not out and not deleted and not roles:
        if MODELS_CONFIG.exists():
            MODELS_CONFIG.unlink()
        return {"path": str(MODELS_CONFIG), "removed": True, "providers": {}, "deleted": []}

    header = (
        "# 后端注册表：只记录与 default.yaml 不同的条目 + 被删除的内置后端。\n"
        "# roles 是角色分工（哪个后端干翻译、哪个干输出），没配就所有活儿都归\n"
        "# 当前后端。由 Web UI 的「模型服务 / 站点」页维护，改完重启生效（或点重读）。\n\n"
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


def _clean_model_list(raw: list[dict[str, Any]] | None) -> list[ModelEntry]:
    """清洗模型清单：去空行、去重、长度做上限，坏值不抛异常只忽略。

    清单是手填的，脏数据是常态（空行、重复、把上下文长度填成 "128k"）。
    这里的原则是"能救就救，救不了就丢"，绝不因为一行写错就让整份配置存不下去。
    """
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen or len(out) >= 50:
            continue
        seen.add(mid)

        def _num(key: str) -> int | None:
            val = item.get(key)
            if val in (None, "", 0, "0"):
                return None
            try:
                n = int(str(val).strip().lower().replace(",", ""))
            except (TypeError, ValueError):
                return None
            return n if 0 < n <= 100_000_000 else None

        out.append(ModelEntry(id=mid[:120], context=_num("context"),
                              max_output=_num("max_output")))
    return out


def upsert(
    cfg: Config,
    name: str,
    *,
    base_url: str,
    model: str,
    kind: str = "custom",
    model_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """新增或修改一个后端。改完立刻生效——LLMClient 持有的是同一个 cfg 对象。"""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError("名字只能用小写字母、数字、- 和 _，2-24 个字符（它会被写进配置文件）")
    if kind not in KIND_MAP:
        raise ValueError(f"未知类型：{kind}")

    model = (model or "").strip()
    if not model:
        raise ValueError("模型名不能为空（本地服务在 /v1/models 里能看到的名字）")
    if len(model) > 120:
        raise ValueError("模型名过长")

    is_mock = kind == "mock"
    clean_url = validate_url(base_url, allow_empty=is_mock) or None
    # 密钥不在这里存：它由调用方放进口内存注册表（ragkb.secrets），
    # 这里只管地址/模型/类型这些"可以见人"的字段。
    #
    # model_list 传 None 表示"这次不改清单"——卡片上切个模型只改 model 一个字段，
    # 要是把 None 当成"清空"，切换一次模型就把人维护的清单全删了。
    prev = cfg.generation.providers.get(name)
    if model_list is None:
        entries = list(prev.model_list) if prev else []
    else:
        entries = _clean_model_list(model_list)
    cfg.generation.providers[name] = ProviderCfg(
        base_url=clean_url, model=model, kind=kind, model_list=entries
    )
    if name not in cfg.generation.providers or cfg.generation.provider not in cfg.generation.providers:
        cfg.generation.provider = name
    return {"name": name, **_persist(cfg), "active": cfg.generation.provider}


def remove(cfg: Config, name: str) -> dict[str, Any]:
    if name not in cfg.generation.providers:
        raise ValueError("后端不存在")
    if len(cfg.generation.providers) <= 1:
        raise ValueError("至少要留一个后端")
    if cfg.generation.provider == name:
        raise ValueError(f"「{name}」是当前使用的后端，先切换到别的再删")
    del cfg.generation.providers[name]
    return {"deleted": name, **_persist(cfg)}


def set_active(cfg: Config, name: str) -> dict[str, Any]:
    if name not in cfg.generation.providers:
        raise ValueError("后端不存在")
    cfg.generation.provider = name
    return {"active": name}


# --------------------------------------------------------------------- 展示


def readiness(cfg: Config, name: str | None = None) -> dict[str, Any]:
    """这个后端"配好了没有"——注意与"连不连得通"是两件事。

    界面上必须把这两者分开：地址填错、端口没起是运行时故障，重试或改地址就能救；
    而没填模型名、没配密钥是配置缺失，重试一万次也没用，只能去补字段。用户看到的
    提示语因此必须不同，否则他会对着一个"连接超时"反复点重试，而真正缺的是模型名。
    """
    import os

    target = name or cfg.generation.provider
    p = cfg.generation.providers.get(target)
    if p is None:
        return {
            "provider": target, "ready": False, "code": "no_provider",
            "reason": f"当前指向的后端「{target}」不在注册表里，去模型页重新选一个",
        }
    kind = p.kind if p.kind in KIND_MAP else "custom"
    if kind == "mock":
        return {"provider": target, "ready": True, "code": "mock", "reason": ""}
    if not (p.base_url or "").strip():
        return {"provider": target, "ready": False, "code": "no_url",
                "reason": "还没填 base_url"}
    if not (p.model or "").strip():
        return {"provider": target, "ready": False, "code": "no_model",
                "reason": "还没填模型名"}
    # 云端后端要密钥；本地的不强求（自建网关常常不鉴权）。
    # 判据是"内存里有没有"，不是"环境变量有没有"——密钥现在只活在内存里。
    if not p.api_key(target) and KIND_MAP.get(
            p.kind if p.kind in KIND_MAP else "custom", {}).get("local") is False:
        return {"provider": target, "ready": False, "code": "no_key",
                "reason": "云端后端要密钥：在模型页填 API Key（只存内存，重启后需重填）"}
    return {"provider": target, "ready": True, "code": "ok", "reason": ""}


def listing(cfg: Config) -> dict[str, Any]:
    import os

    base = defaults_config().generation.providers
    items = []
    for name, p in cfg.generation.providers.items():
        # 老配置里没有 kind 字段，靠名字兜底：否则 vllm 会显示成"云 API"，
        # 而用户正是靠这个标签判断"这是不是本地的"。
        kind = p.kind if p.kind in KIND_MAP else ("openai" if p.kind else name)
        if kind not in KIND_MAP:
            kind = "custom"
        meta = kind_of(kind)
        ready = readiness(cfg, name)
        # 卡片上的启动命令按**这个后端自己的端口**渲染，而不是类型默认端口：
        # 同一个类型可能配了两个实例（1234 / 1235），命令指错等于白起一次。
        launch = _render_launch(meta, _port_of(p.base_url) or _port_of(meta["default_base"]) or "8080")
        items.append({
            "name": name,
            "kind": kind,
            "kind_label": meta["label"],
            "local": bool(meta["local"]),
            "base_url": p.base_url,
            "model": p.model,
            # 只报"内存里有没有"，绝不把密钥本身吐给前端
            "key_present": bool(p.api_key(name)),
            "model_list": [m.model_dump(exclude_none=True) for m in p.model_list],
            "active": name == cfg.generation.provider,
            "builtin": name in base,
            "ready": ready["ready"],
            "ready_reason": ready["reason"],
            "launch": launch["launch"],
            "launch_alt": launch["launch_alt"],
            "launch_os": launch["launch_os"],
            "launch_note": meta.get("launch_note", ""),
            "hint": meta["hint"],
        })
    items.sort(key=lambda x: (not x["active"], not x["local"], x["name"]))
    from . import secrets as secrets_mod

    return {
        "active": cfg.generation.provider,
        "active_ready": readiness(cfg)["ready"],
        "active_reason": readiness(cfg)["reason"],
        "providers": items,
        "kinds": kinds(cfg),
        # 密钥是否被"记住到本机"（默认 False = 纯内存，重启即清空）
        "keys_persisted": secrets_mod.persisted(),
        # 落盘用的是哪种保护（Windows 账户加密 / 不支持）。界面要如实写，
        # 别让人以为"记住"等于明文躺在一个文件里。
        "key_cipher": secrets_mod.backend_label(),
        "file": {"path": str(MODELS_CONFIG), "exists": MODELS_CONFIG.exists()},
    }
