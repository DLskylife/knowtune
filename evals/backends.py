"""把主项目已经配好的后端读进来，打分台不用再手填地址和模型。

打分台此前要人手敲 `base_url` / `model` / `api_key`，而这三样在「模型服务」页
早就配过一遍了。同一件事填两遍的后果不是麻烦，而是**评错东西**：
`models.yaml` 里的 lmstudio 是 1235 端口、默认模型 qwen2.5-3b-doctor-merged，
而打分台的占位值是 1234 + doctor-7b——看着像，跑起来打的是另一个模型，
最后那份"评测报告"评的不是你以为的那个模型。

读什么、按什么顺序：

0. **主服务固定入口 `/v1`**（2026-11-04 起）：问 `/api/gateway` 拿地址、
   `/v1/models` 拿模型清单。打分台是「别处」之一，理应和 dify / 脚本一样
   只写死那一个地址——模型页换端点、换模型，这边立刻跟着变。
1. `config/default.yaml` 的 `generation.providers`：出厂后端（地址 / 模型 / 模型清单）
2. `config/models.yaml` 的 `providers`：界面里改过的覆盖项（整体替换语义，与
   `knowtune/config.py::_apply_models_file` 一致）
3. `deleted` 列表里的后端真的消失
4. 密钥：`data/secrets.json`（DPAPI 密文，只在同一账户同一台机器能解）→
   `api_key_env` 指向的环境变量 → 空

第 0 步拿不到（主服务没起）才退回 1–4，独立跑打分台时行为和以前一样。

**为什么不直接 import knowtune**：打分台跑在 `evals/.venv`（Gradio 6 与主项目
依赖打架），那边没有 pydantic、没有 knowtune 包。为了二十行配置解析把整套依赖
装进去，换来的是每次主项目升依赖就得重新对一次版本。这里只用标准库 + PyYAML。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"

DEFAULT_GATEWAY = "http://127.0.0.1:8000/v1"
_GATEWAY_ENVS = ("KNOWTUNE_GATEWAY", "PREVISIT_LLM_GATEWAY")
# 主服务没起时不必每次渲染下拉都等一次超时；主服务起来后最多 8 秒就能看见。
_GATEWAY_TTL = 8.0
_GW: dict[str, Any] = {"ts": 0.0, "entries": None}
# 本地地址绝不能走系统代理，否则 127.0.0.1 会被发到公司代理上去。
_URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 内置但不该出现在打分台里的后端：mock 不产出真实文本，评它没有意义。
_SKIP = {"mock"}


# ------------------------------------------------------------------ 读配置


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge() -> dict[str, dict[str, Any]]:
    """default.yaml 打底，models.yaml 覆盖。"""
    gen = _read_yaml(CONFIG / "default.yaml").get("generation") or {}
    merged: dict[str, dict[str, Any]] = {
        str(name): dict(spec)
        for name, spec in (gen.get("providers") or {}).items()
        if isinstance(spec, dict)
    }
    reg = _read_yaml(CONFIG / "models.yaml")
    for name, spec in (reg.get("providers") or {}).items():
        if isinstance(spec, dict):
            merged[str(name)] = {**merged.get(str(name), {}), **spec}
    for name in reg.get("deleted") or []:
        merged.pop(str(name), None)
    return merged


# ------------------------------------------------------------------ 密钥


def _dpapi_unseal(blob: str) -> str:
    """解一条 DPAPI 密文。非 Windows / 换了账户都解不开，一律返回空串。"""
    if os.name != "nt":
        return ""
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    raw = base64.b64decode(blob)
    buf = ctypes.create_string_buffer(raw, len(raw))
    src = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst))
    if not ok:
        return ""
    try:
        return ctypes.string_at(dst.pbData, dst.cbData).decode("utf-8", "replace")
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


_SEEN: dict[str, str] = {}


def _saved_keys() -> dict[str, str]:
    """读一次缓存一次：每换一个下拉项就解一次密文没必要。"""
    if _SEEN:
        return _SEEN
    path = DATA / "secrets.json"
    if not path.exists():
        return _SEEN
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _SEEN
    stored = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        return _SEEN
    enc = str(data.get("enc") or "plain")
    for name, val in stored.items():
        if enc == "plain":                      # 旧版明文文件，读进来就是了
            _SEEN[str(name)] = str(val)
            continue
        if enc != "dpapi":
            continue
        try:
            got = _dpapi_unseal(str(val))
        except Exception:
            got = ""
        if got:
            _SEEN[str(name)] = got
    return _SEEN


def api_key(name: str, spec: dict[str, Any]) -> str:
    saved = _saved_keys().get(name)
    if saved:
        return saved
    env_name = (spec.get("api_key_env") or "").strip()
    if env_name:
        return (os.environ.get(env_name) or "").strip()
    return ""


# ------------------------------------------------------------- 固定入口 /v1


def gateway_base() -> str:
    """主服务固定入口。`knowtune serve` 会把实际端口写进 KNOWTUNE_GATEWAY。"""
    for key in _GATEWAY_ENVS:
        got = (os.environ.get(key) or "").strip()
        if got:
            return got.rstrip("/")
    return DEFAULT_GATEWAY


def _gateway_root(base: str) -> str:
    return base[:-3] if base.endswith("/v1") else base


def _get_json(url: str, timeout: float = 1.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _URL_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gateway_status() -> dict[str, Any]:
    """给界面用：网关通不通、地址是什么、有多少模型可选。"""
    eps = _gateway_entries()
    if eps is None:
        return {"ok": False, "base_url": gateway_base(), "models": 0,
                "note": "主服务未启动，已退回本地配置清单"}
    return {"ok": True, "base_url": eps[0]["base_url"],
            "models": len(eps[0]["models"]), "model": eps[0]["model"],
            "note": "经主服务 /v1 调用"}


def _gateway_entries() -> list[dict[str, Any]] | None:
    """把网关包装成一个可评端点。拿不到就返回 None，由 entries() 走旧路径。"""
    now = time.monotonic()
    if now - _GW["ts"] < _GATEWAY_TTL:
        return _GW["entries"]

    base = gateway_base()
    root = _gateway_root(base)
    try:
        info = _get_json(root + "/api/gateway", 0.8)
    except Exception:                         # 主服务没起 / 端口不对
        info = None
    try:
        listed = _get_json(base + "/models", 0.8)
    except Exception:
        listed = None
    if not info and not listed:
        _GW.update(ts=now, entries=None)
        return None

    url = str((info or {}).get("base_url") or base)
    ids: list[str] = []
    for m in (listed or {}).get("data") or []:
        mid = str(m.get("id") or "").strip() if isinstance(m, dict) else str(m).strip()
        if mid and mid not in ids:
            ids.append(mid)
    # 其他端点也可以用「端点::模型」从同一个地址打到，不必再记各自的端口。
    try:
        reg = _get_json(root + "/api/models", 0.8)
        for p in reg.get("providers") or []:
            if not str(p.get("base_url") or "").strip():
                continue
            for m in p.get("models") or []:
                mid = str(m.get("id") if isinstance(m, dict) else m).strip()
                if mid:
                    ref = f"{p.get('name')}::{mid}"
                    if ref not in ids:
                        ids.append(ref)
    except Exception:                         # 注册表接口出错不影响主清单
        pass
    if not ids:
        _GW.update(ts=now, entries=None)
        return None

    want = str((info or {}).get("model") or "").strip()
    if want in ids:
        ids.remove(want)
        ids.insert(0, want)
    eps = [{
        "name": "gateway",
        "kind": "local",
        "base_url": url,
        "model": want or ids[0],
        "models": ids,
        "api_key": "",              # 主服务 /v1 无鉴权，别去 secrets 里翻
        "alive": True,              # 刚从它拿到数据，必然在监听
        "source": "gateway",
    }]
    _GW.update(ts=now, entries=eps)
    return eps


# ------------------------------------------------------------------ 对外


def _models_of(spec: dict[str, Any]) -> list[str]:
    """取这个端点的模型 id 列表。

    2026-11-04 起配置里的字段叫 `models`（每条是 {id, path, port, context}）；
    更早的版本叫 `model_list`，里面只有 id。两种都认——打分台读的是**别人**
    写的配置文件，不能假设对方已经升级。
    """
    raw = spec.get("models")
    if raw is None:
        raw = spec.get("model_list")
    out: list[str] = []
    for m in raw or []:
        mid = m.get("id") if isinstance(m, dict) else m
        if mid:
            out.append(str(mid))
    return out


def entries() -> list[dict[str, Any]]:
    """所有可评的端点。

    主服务在跑时只返回「固定入口 /v1」这一个端点：所有模型（含按端点限定的
    `端点::模型`）都从同一个地址打过去。主服务没起才退回读 YAML 的老清单——
    那条路是降级备份，不是另一套需要人维护的配置。
    """
    gw = _gateway_entries()
    if gw is not None:
        return gw
    out: list[dict[str, Any]] = []
    for name, spec in _merge().items():
        if name in _SKIP:
            continue
        base = (spec.get("base_url") or "").strip()
        if not base:
            continue                    # 没地址的端点评不了，列出来只会让人选错
        models = _models_of(spec)
        cur = (spec.get("model") or "").strip()
        if cur in models:
            # 在用的那个排第一：下拉里一眼看到"现在跑的是什么"，
            # 不用在一串模型名里找哪一个等于 config 里的 model。
            models.remove(cur)
            models.insert(0, cur)
        elif cur:
            models.insert(0, cur)
        if not models:
            continue
        out.append({
            "name": name,
            "kind": str(spec.get("kind") or "openai"),
            "base_url": base,
            "model": cur or models[0],
            "models": models,
            "api_key": api_key(name, spec),
            "alive": _alive(base),
        })
    return out


def choices() -> list[tuple[str, str]]:
    """(显示名, 值)。值是 `后端::模型`，两段一起选，省掉一个联动下拉。

    名字后面带状态：本机没起 / 没存密钥。这两件事决定了点"开始"之后是
    出答案、连不上、还是一排 401——不写在这里，用户只能靠试。
    """
    out: list[tuple[str, str]] = []
    for ep in entries():
        if _is_local(ep["base_url"]):
            note = "" if ep["alive"] else " · 未启动"
        else:
            note = "" if ep["api_key"] else " · 无密钥"
        for model in ep["models"]:
            out.append((f"{ep['name']} · {model}{note}", f"{ep['name']}::{model}"))
    return out


def fill(value: str) -> dict[str, Any] | None:
    """把下拉选中值展开成填写用的字段。"""
    name, _, model = (value or "").partition("::")
    if not name:
        return None
    for ep in entries():
        if ep["name"] != name:
            continue
        model = model or ep["model"]
        return {
            "label": model,
            "base_url": ep["base_url"],
            "model": model,
            "api_key": ep["api_key"],
            "backend": name,
            "key_present": bool(ep["api_key"]),
            "local": _is_local(ep["base_url"]),
            "alive": ep["alive"],
        }
    return None


def _is_local(base_url: str) -> bool:
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")


_ALIVE: dict[str, bool] = {}


def refresh() -> None:
    """「重读配置」按钮：清掉密钥与连通性缓存。

    连通性也要清——刚在 LM Studio 里点了 Start，缓存里还写着"未启动"，
    下拉上那条提示就成了误导。
    """
    _SEEN.clear()
    _ALIVE.clear()
    _GW.update(ts=0.0, entries=None)   # 模型页刚换了端点/模型，别拿 8 秒前的清单


def _alive(base_url: str) -> bool:
    """本地后端到底起没起。只探本机地址，远端一律返回 False——

    探一个公网地址要等 DNS 和 TCP 握手，界面打开会卡；而且云端"连得上"
    本来也说明不了什么（没密钥照样 401）。
    """
    if not _is_local(base_url):
        return False
    if base_url in _ALIVE:
        return _ALIVE[base_url]
    ok = False
    try:
        parts = urlsplit(base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=0.25):
            ok = True
    except OSError:
        ok = False
    _ALIVE[base_url] = ok
    return ok


def current_provider() -> str:
    """配置里声明的默认后端。local.yaml 是用户在参数面板改过的值，优先。"""
    for path in (CONFIG / "local.yaml", CONFIG / "default.yaml"):
        gen = _read_yaml(path).get("generation") or {}
        name = str(gen.get("provider") or "").strip()
        if name:
            return name
    return ""


def default_value() -> str:
    """默认选中的那一项。

    优先级：配置声明的默认后端（能用时）→ 起着的本地后端 → 有密钥的后端 →
    声明的那一个 → 第一个。

    "能用"= 有密钥，或者是**真的在监听**的本机地址。只看"是不是 localhost"
    不够：出厂配置里的 vllm 指向 8100，多数机器上根本没起，选中它点开始就是
    connection refused；而同一个 config 里 lmstudio 的 1235 往往是活的。
    """
    eps = entries()
    if not eps:
        return ""

    def usable(ep: dict[str, Any]) -> bool:
        return bool(ep["api_key"]) or ep["alive"]

    want = current_provider()
    pick = next((ep for ep in eps if ep["name"] == want and usable(ep)), None)
    if pick is None:
        pick = next((ep for ep in eps if ep["alive"]), None)
    if pick is None:
        pick = next((ep for ep in eps if ep["api_key"]), None)
    if pick is None:
        pick = next((ep for ep in eps if ep["name"] == want), None)
    if pick is None:
        pick = eps[0]
    return f"{pick['name']}::{pick['model']}"


def default_pair() -> tuple[str, str]:
    """A/B 两个默认值：A 当对照，B 当待评的那个。

    A/B 的意义是"同一件事、两个模型"，所以优先在**同一个后端里挑两个不同
    模型**（base 与微调版通常就住在同一个 LM Studio 里）：让 A、B 分别落在
    两个不同后端上，比的其实是两个服务商，那是最难解释的一种差异。

    B 填配置里**当前在用**的那个模型——刚训完的版本通常就是它；A 填清单里
    另一个（排最后的那个，一般是上一版或基线）。挑不到就退回两个不同的
    可用后端，再不行两边填同一个，总比留空强。
    """
    eps = entries()
    if not eps:
        return "", ""
    usable = [ep for ep in eps if ep["alive"] or ep["api_key"]] or eps
    for ep in usable:
        others = [m for m in ep["models"] if m != ep["model"]]
        if others:
            return f"{ep['name']}::{others[-1]}", f"{ep['name']}::{ep['model']}"
    a = usable[0]
    b = usable[1] if len(usable) > 1 else a
    return f"{a['name']}::{a['model']}", f"{b['name']}::{b['model']}"


def summary() -> str:
    """一行说明：读到了几个后端、几个带密钥、没密钥的怎么办。"""
    eps = entries()
    if not eps:
        return ("没读到后端配置（`config/models.yaml`）：下面只能手填，"
                "或者先去「模型服务」页配一个。")
    keys = sum(1 for ep in eps if ep["api_key"])
    if keys:
        return f"已读到 {len(eps)} 个后端，其中 {keys} 个有可用密钥"
    return (f"已读到 {len(eps)} 个后端，密钥一个都没读到——"
            "去「模型服务」页填一次就会存到本机，这里自动带上（本地后端本来就不需要）")
