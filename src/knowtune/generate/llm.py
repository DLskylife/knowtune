"""LLM 客户端：一个协议（OpenAI 兼容），三档后端（vLLM / LM Studio / 云端）。

关键约束全在这里，因为 LLM 调用是整条链路唯一不可控的一段：
1. 超时必须是显式数值，httpx 默认 5s 但只是 total，read 必须单独给；
2. 重试只允许发生在**首 token 之前**——已经推出去的 token 再重试会导致
   回答重复拼接，这种 bug 用户一眼就能看出来；
3. 云端 profile 出站前做 PII 扫描，本地 profile 跳过（数据没出机器）。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Iterator

import httpx
from tenacity import (
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import Config, ProviderCfg
from . import think

PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "phone_cn": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card_cn": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "api_key": re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,})\b"),
}

MOCK_PREFIX = "【MOCK】未调用任何 LLM，以下是链路连通性回显，不是真实答案。\n\n"


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def human_hint(exc: "LLMError", provider: str = "") -> str:
    """把模型调用失败翻译成"下一步该干什么"。

    实测（2026-11-01）：三个云后端密钥全废，界面上只显示一串英文 401
    （"You didn't provide an API key…"），用户看不出这是"要填密钥"还是
    "系统坏了"，只会觉得"这个对话没有任何意义"。状态码能指向明确动作。
    """
    code = getattr(exc, "status", None)
    who = f"后端「{provider}」" if provider else "模型后端"
    if code in (401, 403):
        return f"{who}的密钥无效或未填写——去「模型」页重新填密钥，或换一个可用的后端。"
    if code == 404:
        return f"{who}的地址或模型名不存在（404）：多半是模型名写错、或地址少了 /v1。"
    if code == 429:
        return f"{who}被限流或额度用尽，稍后重试或换一个后端。"
    if code is not None and code >= 500:
        return f"{who}服务端出错，稍后重试。"
    return ""


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    degraded: list[str] | None = None
    ttft_ms: int | None = None


def scan_pii(text: str) -> list[str]:
    return [name for name, pat in PII_PATTERNS.items() if pat.search(text)]


def mask_pii(text: str) -> str:
    text = PII_PATTERNS["phone_cn"].sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    text = PII_PATTERNS["id_card_cn"].sub(lambda m: m.group(0)[:4] + "**********" + m.group(0)[-4:], text)
    text = PII_PATTERNS["email"].sub(lambda m: m.group(0)[:2] + "***@" + m.group(0).split("@")[-1], text)
    text = PII_PATTERNS["api_key"].sub("sk-***", text)
    return text


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, LLMError):
        return exc.retryable
    return False


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._before_sleep = self._log_retry

    # ------------------------------------------------------------------ 工具

    def _log_retry(self, state: RetryCallState) -> None:  # pragma: no cover - 仅日志
        exc = state.outcome.exception() if state.outcome else None
        print(f"[llm] retry attempt={state.attempt_number} reason={exc!r}")

    def resolve(self, provider: str | None) -> tuple[str, ProviderCfg]:
        name = provider or self.cfg.generation.provider
        if name not in self.cfg.generation.providers:
            raise LLMError(f"未知 provider: {name}")
        return name, self.cfg.generation.providers[name]

    def _timeout(self) -> httpx.Timeout:
        t = self.cfg.generation.timeout
        return httpx.Timeout(connect=t.connect, read=t.read, write=t.write, pool=t.pool)

    def _client(self) -> httpx.Client:
        """所有 LLM 请求统一从这里拿客户端。

        默认 trust_env=False：本项目的后端是本地 vLLM 与国内 API（百炼 /
        DeepSeek / 混元），都不需要代理。而机器上一旦存在 http_proxy 环境
        变量（开发机、公司网络、CI 镜像里很常见），httpx 会默认把所有请求
        塞进那个代理——代理连不上就是 "502 upstream connect failed"，看着
        像模型服务挂了，实际是本机代理劫持。这个坑排查成本极高，所以默认
        关掉环境代理。

        确实需要走代理访问境外模型的，用 KNOWTUNE_LLM_PROXY 显式指定，不要
        依赖环境变量生效（那会把本地 vLLM 一起带进代理）。
        """
        proxy = os.environ.get("KNOWTUNE_LLM_PROXY", "").strip()
        if proxy:
            return httpx.Client(timeout=self._timeout(), proxy=proxy, trust_env=False)
        return httpx.Client(timeout=self._timeout(), trust_env=False)

    @staticmethod
    def _drop_unsupported(payload: dict, body: str) -> bool:
        """部分模型明确拒绝 temperature（百炼的 kimi-k3 就是），返回 400：
        "Parameter 'temperature'=0.1 is not supported"。

        这不是故障，是我们多给了一个它不认的参数。去掉重试一次，而不是把
        400 甩给用户——用户看到"出错了"只会以为系统坏了，实际上服务一路
        正常，只是参数不兼容。只退让这两个字段，别的 400（模型名写错、
        密钥无效）必须如实抛出去，不能被这里吞掉。
        """
        low = body.lower()
        if "temperature" in payload and "temperature" in low and (
                "not support" in low or "unsupported" in low or "invalid" in low):
            payload.pop("temperature", None)
            return True
        # reasoning_effort 只有部分服务端认（DeepSeek 认，OpenAI 官方对
        # "none" 可能报非法值）。不认就去掉——少省一点思考，不能因此 500。
        if "reasoning_effort" in payload and "reasoning" in low:
            payload.pop("reasoning_effort", None)
            return True
        return False

    def _payload(
        self,
        messages: list[dict],
        model: str,
        *,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None,
        reasoning: str | None = None,
    ) -> dict:
        gen = self.cfg.generation
        payload = {
            "model": model,
            "messages": messages,
            "temperature": gen.temperature if temperature is None else temperature,
            "max_tokens": gen.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if stream:
            # 只有 OpenAI 官方保证这个字段；vLLM/LM Studio 不认就退化为本地估算 token
            payload["stream_options"] = {"include_usage": True}
        if getattr(gen, "disable_thinking", True):
            # 推理内容不进答案，干脆别让模型想——省 token，也不会漏 <think> 出来。
            # vLLM / LM Studio 认这个字段；不认的服务端当多余字段忽略。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if reasoning:
            # 真正能让推理模型闭嘴的是这个。2026-10-29 实测 deepseek-flash：
            # enable_thinking=false 被无视（照样烧 1020 个 reasoning token），
            # reasoning_effort=none 才真的归零。翻译这种确定性任务不需要思考，
            # 关掉后同一次翻译从 1116 token 降到 222，还少等十几秒。
            payload["reasoning_effort"] = reasoning
        return payload

    def _prepare(self, messages: list[dict], name: str, prov: ProviderCfg) -> list[dict]:
        """云端出站前做 PII 处理；本地后端不动。"""
        if name in {"vllm", "lmstudio", "mock"}:
            return messages
        policy = self.cfg.generation.pii_policy
        joined = "\n".join(m.get("content", "") for m in messages)
        hits = scan_pii(joined)
        if not hits:
            return messages
        if policy == "block":
            raise LLMError(f"出站内容包含疑似敏感信息（{', '.join(hits)}），已按 pii_policy=block 拦截")
        if policy == "mask":
            return [{**m, "content": mask_pii(m.get("content", ""))} for m in messages]
        return messages

    # ------------------------------------------------------------------ 主流程

    def stream(
        self,
        messages: list[dict],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning: str | None = None,
    ) -> Iterator[str]:
        name, prov = self.resolve(provider)

        if name == "mock":
            yield from self._mock_stream(messages)
            return

        think_f = think.ThinkFilter()

        if not prov.base_url:
            raise LLMError(f"provider {name} 未配置 base_url")

        prepared = self._prepare(messages, name, prov)
        payload = self._payload(
            prepared, model or prov.model, stream=True, temperature=temperature,
            max_tokens=max_tokens, reasoning=reasoning,
        )
        headers = {"Content-Type": "application/json"}
        key = prov.api_key(name)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = prov.base_url.rstrip("/") + "/chat/completions"

        attempt = 0
        max_attempts = max(1, self.cfg.generation.retry.max_attempts)
        while True:
            attempt += 1
            client = self._client()
            cm = client.stream("POST", url, json=payload, headers=headers)
            try:
                resp = cm.__enter__()
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", errors="replace")[:300]
                    if resp.status_code == 400 and self._drop_unsupported(payload, body):
                        cm.__exit__(None, None, None)
                        client.close()
                        attempt -= 1        # 参数兼容重试，不占用重试额度
                        continue
                    raise LLMError(
                        f"LLM 返回 {resp.status_code}: {body}",
                        retryable=resp.status_code >= 500 or resp.status_code == 429,
                        status=resp.status_code,
                    )
            except Exception as exc:
                cm.__exit__(type(exc), exc, exc.__traceback__)
                client.close()
                if attempt < max_attempts and _is_retryable(exc):
                    time.sleep(min(2 ** (attempt - 1), self.cfg.generation.retry.max_backoff_s))
                    continue
                if isinstance(exc, LLMError):
                    raise
                raise LLMError(f"LLM 连接失败：{type(exc).__name__}: {exc}",
                               retryable=True) from exc

            emitted = False
            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                emitted = True
                                # 思考块在这里就滤掉：漏进前端会变成一堆自言自语，
                                # 混进 RTL 容器里还会显示成倒着的乱码
                                out = think_f.feed(piece)
                                if out:
                                    yield out
                tail = think_f.flush()
                if tail:
                    yield tail
                return
            except Exception as exc:
                # 已经吐过 token 就绝不重试：重试会让前端看到重复的半句话
                if not emitted and attempt < max_attempts:
                    time.sleep(min(2 ** (attempt - 1), self.cfg.generation.retry.max_backoff_s))
                    continue
                raise LLMError(
                    f"{'输出中断' if emitted else 'LLM 请求失败'}：{type(exc).__name__}: {exc}"
                ) from exc
            finally:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass
                client.close()

    def complete(
        self,
        messages: list[dict],
        *,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        reasoning: str | None = None,
    ) -> LLMResult:
        name, prov = self.resolve(provider)
        if name == "mock":
            text = "".join(self._mock_stream(messages))
            return LLMResult(text=text, model="mock", provider=name, degraded=["mock"])

        if not prov.base_url:
            raise LLMError(f"provider {name} 未配置 base_url")

        prepared = self._prepare(messages, name, prov)
        payload = self._payload(
            prepared, model or prov.model, stream=False, temperature=temperature,
            max_tokens=max_tokens, reasoning=reasoning,
        )
        headers = {"Content-Type": "application/json"}
        key = prov.api_key(name)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        url = prov.base_url.rstrip("/") + "/chat/completions"

        started = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            with self._client() as client:
                try:
                    resp = client.post(url, json=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise LLMError(
                        f"LLM 请求失败：{type(exc).__name__}: {exc}", retryable=True) from exc
            if resp.status_code == 400 and attempts <= 2 and self._drop_unsupported(
                    payload, resp.text):
                continue
            break

        if resp.status_code >= 400:
            raise LLMError(
                f"LLM 返回 {resp.status_code}: {resp.text[:300]}",
                retryable=resp.status_code >= 500 or resp.status_code == 429,
                status=resp.status_code,
            )

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"LLM 返回空 choices：{str(data)[:200]}")
        text = think.strip((choices[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return LLMResult(
            text=text,
            model=prov.model,
            provider=name,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            ttft_ms=int((time.perf_counter() - started) * 1000),
        )

    # ------------------------------------------------------------------ mock

    def _mock_stream(self, messages: list[dict]) -> Iterator[str]:
        """把装填好的上下文原样回吐，专门用来在没有 LLM 的机器上验证链路。

        它的输出**不是答案**，UI 必须打 MOCK 角标——否则会变成"看起来能用
        实际在骗自己"，这是个人项目最常见的技术债来源。
        """
        body = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        nums = sorted({int(n) for n in re.findall(r"\[(\d+)\]", body)})
        lines = [MOCK_PREFIX.rstrip("\n"), ""]
        if nums:
            lines.append(f"共装填 {len(nums)} 段上下文，编号如下：")
            for n in nums:
                snippet = self._snippet_for(body, n)
                lines.append(f"[{n}] {snippet}")
        else:
            lines.append("未装填任何上下文（检索为空或问题被拒答）。")
        text = "\n".join(lines)
        for i in range(0, len(text), 6):
            yield text[i : i + 6]

    @staticmethod
    def _snippet_for(body: str, n: int) -> str:
        marker = f"[{n}]"
        pos = body.find(marker)
        if pos < 0:
            return "（无内容）"
        chunk = body[pos : pos + 120].replace("\n", " ")
        return chunk[len(marker) :].strip()[:80] + "…"
