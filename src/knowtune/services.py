"""后端服务探测 —— 首页与模型页要回答的那个问题："现在到底能不能用"。

两段式验证（2026-10-28 教训）：
1. 打 `{base_url}/models`——快，能区分"端口活着 / 进程没起 / 模型没加载"；
2. /models 通了**还不算联通**：one-api 这类网关对 /models 永远回 200 全列表，
   结果四个后端全都"就绪"，实际只有一个真加载了模型。所以再发一次
   `max_tokens=1` 的补全，模型真能吐字才算 ok，吐不出就是 warn。
   ping 的 prompt 只有 1 个 token，本地服务几百毫秒内返回。

探测结果带内存缓存（首页刷新不该同步探 N 个后端），并且**落盘**——
以前缓存在内存里，服务一重启模型页就全部显示"还没探测"，用户以为
之前测的结果丢了（数据没更新，其实是没人再探过）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Config, ProviderCfg

CACHE_TTL_S = 15.0
# 缓存格式版本。v1 没有版本号，且 key_fp 恒为 "no_key"（指纹函数没带后端名），
# 等于没绑钥匙。读到 v1 一律作废重测。
_CACHE_VERSION = 2
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_FILE: Path | None = None
_CACHE_LOADED = False

_ERROR_HINTS = {
    "connect": "端口上没有服务在监听（进程没起，或地址写错）",
    "timeout": "连接超时：端口没有响应（服务没起、被防火墙丢弃，或模型还在加载）",
    "404": "服务在跑，但这个路径没有 /models —— 大概率 base_url 少了 /v1",
    # 旧文案写"检查环境变量里的密钥"。密钥是用户在弹窗里填的、存在内存或
    # secrets.json 里，根本不经过环境变量——照着这句话去找，只会找到一片空地。
    "401": "密钥被拒绝（没复制全、已失效/被停用，或不属于这个站点）",
    "403": "密钥有效但没权限（模型未开通、账户欠费，或来源 IP 受限）",
}


def _srv_detail(resp: httpx.Response, limit: int = 180) -> str:
    """捞出服务端自己写的错误消息。

    以前这里把 body 整个丢掉，只留一句自编提示。硅基流动返回的是
    `{"code":30014,"message":"Token is invalid."}`——一句话就点明了是密钥，
    我们却翻译成"检查环境变量里的密钥"，把唯一准确的线索扔了。
    """
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001 - 读不到 body 不值得让探测失败
        return ""
    msg = ""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        for cand in (
            body.get("message"), body.get("msg"), body.get("detail"),
            err.get("message") if isinstance(err, dict) else err,
        ):
            if isinstance(cand, str) and cand.strip():
                msg = cand.strip()
                break
        if not msg and body.get("code") is not None:
            msg = f"code={body['code']}"
    if not msg:
        msg = " ".join(text.split())[:limit]
    # 有些网关会把密钥回显在错误里。原样展示等于把它写进界面和日志。
    msg = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-***", msg)
    return msg[:limit]


def _key_shape(key: str) -> str:
    """给用户一面镜子：只说形态，不泄露本体。

    401 的常见原因不是"密钥错"，而是"密钥不完整"——少了后半截、末尾带着
    换行、从网页或聊天记录里复制时混进全角字符。光看"鉴权失败"四个字，
    谁也查不出来；报出长度和异常字符，用户一眼就能看出"我填的怎么只有 20 位"。
    """
    raw = key or ""
    s = raw.strip()
    bits = [f"长度 {len(s)}"]
    if len(raw) != len(s):
        bits.append("首尾有空白")
    weird = [c for c in s if not (c.isascii() and not c.isspace())]
    if weird:
        shown = "".join(dict.fromkeys(weird))[:4]
        bits.append(f"含非 ASCII 或内部空白 {shown!r}")
    if s and not s.startswith("sk-"):
        bits.append("不是 sk- 开头")
    bits.append(f"指纹 {hashlib.sha256(s.encode('utf-8', 'ignore')).hexdigest()[:8]}")
    return "，".join(bits)


def _classify(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, httpx.ConnectError):
        return "down", _ERROR_HINTS["connect"]
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", _ERROR_HINTS["timeout"]
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        msg = f"HTTP {code}：{_ERROR_HINTS.get(str(code), '服务返回了错误')}"
        detail = _srv_detail(exc.response)
        if detail:
            msg += f"｜服务端原话：{detail}"
        return "error", msg
    return "error", f"{type(exc).__name__}: {exc}"


# 这些模型会出现在云厂商的 /models 里，但**不能做文本对话**：图像、语音、
# 实时流、向量、重排。把它们混进"可点选"的下拉里，用户点到就是一次 403/500，
# 而我们自己的报错还写着"检查密钥权限"——纯属把人往沟里带。
_NON_CHAT_HINTS = (
    "realtime", "audio", "image", "embedding", "rerank", "asr", "tts",
    "video", "ocr", "wanx", "cosyvoice", "paraformer", "livetranslate",
    "text-to-speech", "speech-", "-speech", "flux", "stable-diffusion",
    # ModelScope / 火山这类聚合市场实测漏网的三类：文生/图生视频（Wan2.2-T2V、
    # I2V）、图像生成（Kwai-Kolors/kolors）、向量（BAAI/bge-m3）。它们混在
    # 下拉里点一下就是一次 403/500，而且白白把候选列表撑长，真想用的模型
    # 反而"一直找不到"。
    "t2v", "i2v", "text-to-video", "image-to-video", "kolors", "bge",
)


def _key_fp(prov: ProviderCfg | None, name: str = "") -> str:
    """这把密钥的指纹（不存密钥本体，只存够区分"换没换"的一小段哈希）。

    探测结论是**对某把钥匙而言**的：密钥换了、或者重启后内存里的密钥没了
    （默认不落盘）而盘上缓存还在，旧绿灯就成了过期结论——用户看到"测试通过"，
    一提问却 401。缓存里带上指纹，换钥匙就自动作废。

    必须传后端名：密钥是按后端名存在内存里的，只调 `api_key()` 拿的是"本次
    探测临时填的那把"（通常为空），于是两边都算成 "no_key"、永远相等——
    指纹机制看着在跑，其实一直空转（2026-11-01 发现）。
    """
    if prov is None:
        return "unknown"
    try:
        key = prov.api_key(name) or ""
    except Exception:  # noqa: BLE001 - 取不到就当"没密钥"，不拖累探测
        key = ""
    if not key.strip():
        return "no_key"
    return hashlib.sha256(key.strip().encode("utf-8", "ignore")).hexdigest()[:12]


def split_chat_models(ids: list[str]) -> tuple[list[str], int]:
    """挑出能做文本对话的模型，返回 (对话类列表, 被过滤掉的数量)。

    注意别把 "omni" 一刀切掉：实测 `qwen3.8-omni-flash` 能对话，
    不能对话的是 `-realtime` 那一支，所以按更具体的词过滤。
    """
    chat, rest = [], 0
    for m in ids:
        low = m.lower()
        if any(h in low for h in _NON_CHAT_HINTS):
            rest += 1
            continue
        chat.append(m)
    return chat, rest


def _is_local(url: str) -> bool:
    """本机地址必须绕开 HTTP(S)_PROXY。

    踩过：用户开着代理时，httpx 默认读环境变量，连 127.0.0.1:8000 也发给代理，
    代理回一个 502。表现是"本地服务明明起来了，页面说不可用"，而且延迟 5 秒。
    """
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名走系统代理，那是用户自己要的行为
    return ip.is_loopback or ip.is_private or ip.is_unspecified


def _verify_chat(p: ProviderCfg, *, headers: dict, timeout_s: float = 20.0) -> tuple[bool, str]:
    """发一次 max_tokens=1 的补全。/models 通了只说明"有个 OpenAI 兼容服务
    在这个端口上"，不说明你配的那个模型真能出字。

    超时给到 20s、连接 3s：本地 7B 首次被请求时要先加载权重，十几秒都正常，
    沿用服务探测的 2.5s 会把"正在加载"误判成"不通"。
    """
    url = p.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": p.model or "default",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=3.0),
            trust_env=not _is_local(url),
        ) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        if not body.get("choices"):
            return False, "响应里没有 choices"
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 探测必须吞掉一切异常
        return False, _classify(exc)[1]


def probe_one(
    name: str, p: ProviderCfg, *, timeout_s: float = 2.5, chat_timeout_s: float = 10.0
) -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "name": name,
        "kind": p.kind,
        "base_url": p.base_url,
        "model": p.model,
        "checked_at": time.time(),
        "key_fp": _key_fp(p, name),
    }

    if p.kind == "mock" or not p.base_url:
        result.update(
            status="mock" if p.kind == "mock" else "unset",
            ok=p.kind == "mock",
            latency_ms=0,
            message="不发起网络请求" if p.kind == "mock" else "还没填 base_url",
            models=[],
        )
        return result

    headers = {}
    key = p.api_key(name)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # 没填密钥也照发：有些自建网关不需要鉴权，先斩后奏比直接拦死准。
    # 真被拒了再在错误信息里点明"是缺密钥"，而不是丢一个裸 401 让人自己猜。
    auth_miss = not key

    url = p.base_url.rstrip("/") + "/models"
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout_s, connect=min(1.5, timeout_s)),
            trust_env=not _is_local(url),
        ) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        ids = [
            str(m.get("id"))
            for m in (body.get("data") or [])
            if isinstance(m, dict) and m.get("id")
        ]
        latency = int((time.perf_counter() - started) * 1000)
        listed = p.model in ids
        # 图像/语音/实时/向量这几类也会挂在同一个 /models 下，不能让人去点它们
        chat_ids, hidden = split_chat_models(ids)
        # /models 200 ≠ 联通。追加对话验证：真能出 1 个 token 才算 ok。
        # 但弹窗里刚填完地址、还没选模型时就点验证，手里只有个 placeholder——
        # 拿它去对话必然失败，报出来就是"placeholder 对话失败"，把"地址和密钥
        # 都对"这个好消息盖掉了，用户以为配错了。这种情况 /models 通了就算通。
        blind = p.model in ("", "placeholder")
        chat_ok, chat_msg = (False, "") if blind else _verify_chat(
            p, headers=headers, timeout_s=chat_timeout_s)
        if blind and ids:
            status = "ok"
            msg = "地址和密钥都通 —— 从下面的候选里选一个模型（选完再验一次）"
        elif chat_ok:
            # 只报结论。以前这里写"就绪 · 对话验证通过 · 12 个可对话模型
            # （另有 3 个图像/语音/实时类已隐藏）"，用户每测一次就被灌一遍
            # 与"通不通"无关的统计。模型数量在下拉里看得见，状态行不需要复述。
            status = "ok"
            msg = "连通"
        elif not ids:
            status = "warn"
            msg = "服务在跑，但 /models 为空、对话也没通 —— 先在服务里加载一个模型"
        else:
            status = "warn"
            extra = "" if listed else f"（且模型列表里没有 {p.model}）"
            if "403" in chat_msg or "401" in chat_msg:
                # 列表能拉、对话被拒：最常见的两个原因是"这个模型不归这把密钥调"
                # 与"专属部署得用业务空间域名"。不点破的话，用户只会反复换密钥。
                hint = ("（模型出现在 /models 里 ≠ 这把密钥能调用它：先换个模型试；"
                        "专属部署要在业务空间专属域名下调用）")
            elif p.model not in chat_ids:
                hint = "（这个模型多半不是文本对话模型，换一个 qwen-plus / glm / deepseek 这类）"
            elif "超时" in chat_msg:
                hint = "（可能是模型正在加载，稍后重试）"
            else:
                hint = ""
            msg = f"服务在跑，但 {p.model} 对话失败 —— {chat_msg}{hint}{extra}"
        result.update(status=status, ok=True, latency_ms=latency, message=msg,
                      # 截断保险丝，不是展示上限：60 会把 ModelScope 这种几百条
                      # 的市场截在字母序前段，用户"一直找不到"目标模型。现在
                      # 非对话类已过滤 + 前端有筛选框，放宽到 300 足够。
                      models=chat_ids[:300], models_hidden=hidden,
                      models_total=len(ids), model_listed=listed)
    except Exception as exc:  # noqa: BLE001 - 探测必须吞掉一切异常
        status, message = _classify(exc)
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
        if auth_miss and (code in (401, 403) or "401" in message or "403" in message):
            status, message = "no_key", "这个服务要鉴权，但还没填密钥——在弹窗里填 API Key 再探测"
        elif code in (401, 403) and key:
            # 密钥填了却被拒：把"我们实际发出去的那把密钥长什么样"报出来。
            # 这是用户唯一能自查的东西——服务端只会说 invalid，不会告诉他
            # 自己只粘了一半。
            message += f"｜本次用的密钥：{_key_shape(key)}"
        result.update(
            status=status, ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            message=message, models=[],
        )
    return result


def _ensure_cache_file(cfg: Config) -> None:
    """绑定落盘路径并读回上次的结果。只做一次；探测结果跨重启保留，
    模型页才不会在服务重启后全部显示"还没探测"。"""
    global _CACHE_FILE, _CACHE_LOADED
    if _CACHE_LOADED:
        return
    with _CACHE_LOCK:
        if _CACHE_LOADED:
            return
        try:
            _CACHE_FILE = cfg.storage.data_dir / "probe_cache.json"
            if _CACHE_FILE.exists():
                data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    ver = data.pop("__v", 1)
                    if ver != _CACHE_VERSION:
                        # v1 的缓存里 key_fp 恒为 "no_key"（那时指纹函数没带
                        # 后端名，比对永远相等），等于没有指纹。继续用它，
                        # "重启后密钥清空、卡片还亮绿灯"就照旧发生。整批作废，
                        # 让用户重测一次，好过拿假绿灯骗人。
                        data = {}
                    _CACHE.update(data)
                    # 老版本把统计写进了结论（"就绪 · 对话验证通过 · 67 个可对话
                    # 模型（另有 31 个图像/语音类已隐藏）"）。缓存是落盘的，改了
                    # 代码却不清一次，界面会一直念旧文案——看起来像没生效。
                    stale = [k for k, v in _CACHE.items()
                             if isinstance(v, dict) and v.get("status") == "ok"
                             and v.get("message") != "连通"]
                    for k in stale:
                        _CACHE[k]["message"] = "连通"
                    if stale or ver != _CACHE_VERSION:
                        _persist()
        except Exception:  # noqa: BLE001 - 缓存坏了不该挡住探测
            pass
        _CACHE_LOADED = True


def warm_cache(cfg: Config) -> int:
    """启动时把上次的探测结论读回内存。

    不读的话，模型页在「第一次点探测」之前整页都是"还没探测"——盘上明明
    存着结论，界面上却像数据丢了。以前只有 probe_all 才会触发加载，而首屏
    走的是 cached()，永远等不到那一步。
    """
    _ensure_cache_file(cfg)
    with _CACHE_LOCK:
        return len(_CACHE)


def _persist() -> None:
    if _CACHE_FILE is None:
        return
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        # 版本号只在盘上带着，不进内存 _CACHE——否则遍历缓存的地方都要额外
        # 判断"这条不是后端"。
        body = {k: v for k, v in _CACHE.items() if isinstance(v, dict)}
        body["__v"] = _CACHE_VERSION
        tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception:  # noqa: BLE001 - 落盘失败只影响下次重启的展示
        pass


def record(item: dict[str, Any]) -> None:
    """外部（如 /api/services/probe?name=）探测完单颗后写缓存。"""
    with _CACHE_LOCK:
        _CACHE[item["name"]] = item
    _persist()


def probe_all(cfg: Config, *, force: bool = True) -> dict[str, dict[str, Any]]:
    _ensure_cache_file(cfg)
    now = time.time()
    names = list(cfg.generation.providers)

    def run(name: str) -> tuple[str, dict[str, Any]]:
        cached = _CACHE.get(name)
        if not force and cached and now - cached["checked_at"] < CACHE_TTL_S:
            return name, cached
        return name, probe_one(name, cfg.generation.providers[name])

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(names)))) as pool:
        for name, item in pool.map(run, names):
            with _CACHE_LOCK:
                _CACHE[name] = item
    _persist()
    return {name: _CACHE[name] for name in names if name in _CACHE}


def cached(name: str, *, max_age_s: float = 24 * 3600,
           provider: ProviderCfg | None = None) -> dict[str, Any] | None:
    """上次探测结果。窗口给到 24h：这是"最近一次的结论"而不是"实时状态"，
    前端会显示探测时间；真要新数据走强制探测。之前窗口只有 300s，服务重启
    后模型页整页"还没探测"，看起来像数据全丢了。

    `provider` 给进来时会比对密钥指纹：24h 的窗口是给"同一把钥匙"的宽容度，
    换了钥匙（或重启后内存密钥没了、只剩盘上的旧缓存）就必须重新探测——
    否则界面会拿着上一把钥匙的绿灯说"可用"，一调用却 401。
    """
    item = _CACHE.get(name)
    if not item:
        return None
    if time.time() - item.get("checked_at", 0) >= max_age_s:
        return None
    if provider is not None:
        fp = _key_fp(provider, name)
        if fp != "unknown" and item.get("key_fp") and item["key_fp"] != fp:
            return None
    return item


def invalidate(name: str | None = None) -> None:
    with _CACHE_LOCK:
        if name is None:
            _CACHE.clear()
        else:
            _CACHE.pop(name, None)
    _persist()
