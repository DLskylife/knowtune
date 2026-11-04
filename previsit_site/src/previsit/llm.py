"""预问诊自带的最小 LLM 客户端（OpenAI 兼容 chat/completions）。

为什么不用主项目的 LLMClient：那是隔离的一部分。这个包一旦 import
knowtune.generate.llm，就会连带拖进主项目的配置加载、密钥体系、重试策略——
"预问诊能独立部署"就变成了空话。这里只做一件事：往一个 OpenAI 兼容端点
发 chat 请求，超时/报错抛 LLMError，由引擎熔断降级。

配置全部来自环境变量（部署时最不需要解释的东西）：

    PREVISIT_LLM_BASE_URL   例：https://api.deepseek.com/v1（显式直连，优先级最高）
    PREVISIT_LLM_GATEWAY    主服务固定入口，默认 http://127.0.0.1:8000/v1
    KNOWTUNE_GATEWAY        同上；主服务启动时自动写进环境，子进程也能读到
    PREVISIT_LLM_API_KEY    密钥（端点不需要鉴权时可省）
    PREVISIT_LLM_MODEL      模型名（默认取 base_url 同厂常见名，最好显式给）
    PREVISIT_LLM_TIMEOUT    单次请求超时秒数（默认 20，与引擎 timeout_s 独立）

2026-11-04 起默认链路是 **主服务的 /v1 网关**：模型页换端点、换模型，预问诊
不用再改一次自己的配置。网关没起 / 上游模型没加载时，自动退回原来的直连
客户端（挂在主服务里就是主项目的 LLMClient），不会把问诊卡死。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_GATEWAY = "http://127.0.0.1:8000/v1"
_GATEWAY_ENVS = ("PREVISIT_LLM_GATEWAY", "KNOWTUNE_GATEWAY")


class LLMError(Exception):
    """模型调用失败。引擎捕获后熔断降级，不会把问诊卡死。"""


class OpenAICompatLLM:
    """引擎只依赖一个方法：`complete(msgs, *, temperature, max_tokens) -> str`。

    返回纯文本；失败抛 LLMError。没有配 base_url 时 `from_env` 返回 None，
    引擎拿到 llm=None 就直接走规则——这比"配一个必然失败的客户端"诚实。
    """

    def __init__(self, base_url: str, api_key: str = "",
                 model: str = "", timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or "default"
        self.timeout_s = float(timeout_s)

    @classmethod
    def from_env(cls) -> "OpenAICompatLLM | None":
        base = os.environ.get("PREVISIT_LLM_BASE_URL", "").strip()
        if not base:
            return None
        return cls(
            base,
            api_key=os.environ.get("PREVISIT_LLM_API_KEY", "").strip(),
            model=os.environ.get("PREVISIT_LLM_MODEL", "").strip(),
            timeout_s=float(os.environ.get("PREVISIT_LLM_TIMEOUT") or DEFAULT_TIMEOUT_S),
        )

    @classmethod
    def from_config(cls, pv: dict[str, Any] | None = None) -> "OpenAICompatLLM | None":
        """按预问诊配置建客户端（独立部署用）。

        优先顺序：环境变量里的 base_url > 配置里的 `llm_base_url` >
        按 `provider` 名字去 models.yaml 里找同名后端。最后这条是给独立部署
        留的路：没有主项目的模型注册表，但可以手写一份 models.yaml 放在旁边。

        密钥只从环境变量取：models.yaml 是可以进 git 的文件，往里写密钥等于
        把密钥提交上去。主项目那边有密钥体系（knowtune.secrets），走的是
        KnowtuneLLMAdapter，不经过这里。
        """
        pv = pv or {}
        base = os.environ.get("PREVISIT_LLM_BASE_URL", "").strip()
        model = os.environ.get("PREVISIT_LLM_MODEL", "").strip()
        provider = str(pv.get("provider") or "").strip()
        if not base:
            base = str(pv.get("llm_base_url") or "").strip()
        if not model:
            model = str(pv.get("model") or "").strip()
        if not base and provider:
            try:
                from .settings import catalog_from_file

                for p in catalog_from_file().get("providers") or []:
                    if p.get("name") == provider:
                        base = str(p.get("base_url") or "")
                        model = model or str(p.get("model") or "")
                        break
            except Exception:            # 目录读不出来就退回"没有模型"
                base = ""
        if not base:
            return None
        return cls(
            base,
            api_key=os.environ.get("PREVISIT_LLM_API_KEY", "").strip(),
            model=model,
            timeout_s=float(os.environ.get("PREVISIT_LLM_TIMEOUT") or DEFAULT_TIMEOUT_S),
        )

    def retune(self, provider: str | None = None, model: str | None = None) -> None:
        """换模型/后端。独立部署的客户端只认模型名，后端在构造时就定死了，
        所以这里只改 model；地址变了要重新 from_config 换对象。"""
        self.model = (model or "").strip() or self.model

    @property
    def provider(self) -> str:
        return f"openai-compatible:{self.base_url}"

    def complete(self, msgs: list[dict[str, str]], *, temperature: float = 0.2,
                 max_tokens: int = 320, **_: Any) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            resp = httpx.post(f"{self.base_url}/chat/completions",
                              json=payload, headers=headers,
                              timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise LLMError(f"请求失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"端点返回 {resp.status_code}：{resp.text[:200]}")
        try:
            data = resp.json()
            return str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"响应格式不对：{exc}") from exc


# ------------------------------------------------------------- 固定入口 /v1


def gateway_url(pv: dict[str, Any] | None = None) -> str:
    """主服务的固定 OpenAI 兼容入口。

    环境变量优先于配置文件：`knowtune serve` 启动时把实际端口写进
    KNOWTUNE_GATEWAY，子进程（预问诊独立部署、打分台）不用猜端口。
    """
    pv = pv or {}
    for key in _GATEWAY_ENVS:
        got = (os.environ.get(key) or "").strip()
        if got:
            return got.rstrip("/")
    got = str(pv.get("llm_gateway") or "").strip()
    if got and got.lower() not in _OFF_WORDS:
        return got.rstrip("/")
    return DEFAULT_GATEWAY


_OFF_WORDS = {"off", "false", "0", "no", "direct", "禁用", "关闭"}


class GatewayLLM:
    """优先把请求打到主服务 /v1，不通时退回备用客户端。

    为什么要包一层而不是直接把 base_url 改成网关：独立部署时主服务可能压根
    没起，挂在主服务里时又是启动早期（app 在 import 阶段装配，那时候端口
    还没监听）。探活放在第一次 complete() 里做，失败后 `retry_after` 秒内
    不再试，避免每一轮问诊都白等一次超时。

    `source` 给界面上报「现在到底走哪条路」，别让配置写着网关、实际在跑
    直连这种事藏着。
    """

    def __init__(self, base_url: str, fallback: Any = None, *,
                 provider: str = "", model: str = "", api_key: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 retry_after: float = 20.0) -> None:
        self.base_url = (base_url or DEFAULT_GATEWAY).rstrip("/")
        self.fallback = fallback
        self.api_key = (api_key or "").strip()
        self._provider = (provider or "").strip()
        self._model = (model or "").strip()
        self.timeout_s = float(timeout_s)
        self.retry_after = float(retry_after)
        self.source = "gateway"
        self.last_error = ""
        self._down_until = 0.0
        self._model_cache: tuple[str, float] = ("", 0.0)

    @classmethod
    def wrap(cls, pv: dict[str, Any] | None, fallback: Any = None) -> Any:
        """按配置构造。`llm_gateway: false/direct` 时完全不接网关。"""
        pv = pv or {}
        raw = pv.get("llm_gateway")
        # `False or ""` 会变成空串，显式关不掉网关——布尔值得单独认。
        pref = ("false" if raw is False else str(raw or "")).strip().lower()
        if pref in _OFF_WORDS:
            return fallback
        return cls(
            gateway_url(pv), fallback,
            provider=str(pv.get("provider") or "").strip(),
            model=str(pv.get("model") or "").strip(),
            api_key=(os.environ.get("PREVISIT_LLM_API_KEY") or "").strip(),
            timeout_s=float(pv.get("timeout_s") or DEFAULT_TIMEOUT_S),
        )

    @property
    def provider(self) -> str:
        if self.source == "gateway":
            return f"gateway:{self.base_url}"
        return str(getattr(self.fallback, "provider", "") or "direct")

    def retune(self, provider: str | None = None, model: str | None = None) -> None:
        """参数面板保存后调。换模型就立刻重试网关，别让用户等 20 秒。"""
        self._provider = (provider or "").strip()
        self._model = (model or "").strip()
        self._model_cache = ("", 0.0)
        self._down_until = 0.0
        if self.fallback is not None and callable(getattr(self.fallback, "retune", None)):
            self.fallback.retune(provider, model)

    def reconfigure(self, pv: dict[str, Any]) -> None:
        """覆盖层变了：地址/超时/密钥都要跟着变，对象本身留着（熔断状态在上面）。"""
        self.timeout_s = float(pv.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self._down_until = 0.0
        self._model_cache = ("", 0.0)
        self.retune(pv.get("provider") or None, pv.get("model") or None)

    def _default_model(self) -> str:
        """没显式指定模型时，问网关要当前在用的那个。

        /v1/models 在上游没起时会兜底返回配置清单，所以这里拿到的仍然是
        「模型页上选中的那个」，不是随便一个名字。缓存 30 秒，避免每轮
        问诊多一次往返。
        """
        cached, ts = self._model_cache
        if cached and time.monotonic() - ts < 30:
            return cached
        try:
            r = httpx.get(self.base_url + "/models",
                          timeout=min(2.0, self.timeout_s))
            ids = [str(m.get("id")) for m in (r.json().get("data") or [])
                   if isinstance(m, dict) and m.get("id")]
            if ids:
                self._model_cache = (ids[0], time.monotonic())
                return ids[0]
        except Exception:                     # 探不通就让 complete 去走降级
            pass
        return ""

    def complete(self, msgs: list[dict[str, str]], *, temperature: float = 0.2,
                 max_tokens: int = 320, **_: Any) -> str:
        now = time.monotonic()
        if now >= self._down_until:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            # 参数面板里钉了某个端点时，用网关的 provider 路由头带过去；
            # 不把「端点::模型」拼进 model 里，那个语法是网关内部的事。
            if self._provider and self._provider != "gateway":
                headers["X-KnowTune-Provider"] = self._provider
            payload = {
                "model": self._model or self._default_model() or "default",
                "messages": msgs,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            try:
                resp = httpx.post(self.base_url + "/chat/completions",
                                  json=payload, headers=headers,
                                  timeout=self.timeout_s)
                if (resp.status_code == 400 and self._provider
                        and "X-KnowTune-Provider" in headers):
                    # 参数面板里还钉着一个已经删掉的端点名。别让整场问诊为
                    # 这一行旧配置一直走规则：去掉路由头按当前默认端点再试一次，
                    # 并记住这个名不能用，后续不再发。
                    headers.pop("X-KnowTune-Provider")
                    self._provider = ""
                    resp = httpx.post(self.base_url + "/chat/completions",
                                      json=payload, headers=headers,
                                      timeout=self.timeout_s)
                if resp.status_code >= 400:
                    raise LLMError(f"网关返回 {resp.status_code}：{resp.text[:180]}")
                text = str(resp.json()["choices"][0]["message"]["content"] or "").strip()
                self.source = "gateway"
                self.last_error = ""
                return text
            except (httpx.HTTPError, LLMError, KeyError, IndexError, ValueError) as exc:
                self.last_error = str(exc)
                self._down_until = now + self.retry_after
        self.source = "fallback"
        if self.fallback is not None:
            return self.fallback.complete(msgs, temperature=temperature,
                                          max_tokens=max_tokens)
        raise LLMError(f"网关不可用：{self.last_error or '没有可用的备用模型'}")


def build_llm(pv: dict[str, Any] | None, fallback: Any = None) -> Any:
    """统一构造入口：网关优先，退回 fallback；显式关闭网关时直接用 fallback。"""
    return GatewayLLM.wrap(pv, fallback)
