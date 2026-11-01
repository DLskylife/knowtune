"""前台实例的启停管理（2026-10-28）。

每个实例 = 一条线程里跑的一个 uvicorn，占一个端口。选"进程内多线程"而不是
每个实例起一个子进程，理由很实在：

1. **嵌入模型只有一份**。子进程方案要么每个站点各加载一次（多占几百 MB，还可能
   加载失败），要么搞一套 IPC 让子进程回主进程检索——那才是真正的冗杂。
2. 日志、配置、任务队列都在一起，出问题只有一个地方可查。
3. Windows 上没有 fork，子进程要处理进程树、孤儿进程和端口回收，麻烦得多。

代价是一个实例崩了会带走整个后台进程。用 uvicorn 自己的异常隔离（它不会让
请求里的异常逃到 loop 外）加上启动前的端口探测，把这个风险压到可接受。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from . import instcfg


def _copy_lang(spec) -> str:
    """这套界面文案实际是什么语言。判定逻辑与前台 /api/info 同一份。"""
    from .. import lang as lang_mod

    blob = " ".join(x for x in (spec.title, spec.welcome, spec.subtitle) if x)
    got = lang_mod.detect(blob)
    return got if got in ("ug", "zh", "en") else spec.lang


class _Server(uvicorn.Server):
    """uvicorn 0.54 的 `capture_signals()` 已经自己判断了"非主线程就不装
    signal handler"，所以这里什么都不用覆盖——不要自作聪明地覆盖它：它是个
    @contextlib.contextmanager，覆盖成普通方法会让 `with` 直接抛
    'NoneType' object does not support the context manager protocol。"""


@dataclass
class Runtime:
    spec: Any
    status: str = "stopped"       # stopped | starting | running | error | stopping
    error: str | None = None
    started_at: float | None = None
    # 维吾尔语能力自检的结果（只有 lang=ug 的实例才有值）。
    # 见 _probe_lang_capability：不做这一步，"答案为什么是中文"只能靠猜。
    ug_capable: bool | None = None
    ug_note: str | None = None
    ug_sample: str | None = None
    requests: int = 0
    last_ask_at: float | None = None
    _server: Any = field(default=None, repr=False)
    _thread: Any = field(default=None, repr=False)

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at) if self.started_at else 0

    def url(self) -> str:
        host = "127.0.0.1" if self.spec.host() == "0.0.0.0" else self.spec.host()
        return f"http://{host}:{self.spec.port}/"

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.spec.as_dict(),
            "status": self.status,
            "error": self.error,
            "url": self.url(),
            "host": self.spec.host(),
            "uptime_s": self.uptime_s(),
            "started_at": self.started_at,
            "ug_capable": self.ug_capable,
            "ug_note": self.ug_note,
            "ug_sample": self.ug_sample,
        }


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


class InstanceManager:
    def __init__(self, state: Any) -> None:
        self.state = state
        self._runtimes: dict[str, Runtime] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- 查询

    def _rt(self, name: str) -> Runtime:
        spec = instcfg.get(name)
        if spec is None:
            raise KeyError(name)
        with self._lock:
            rt = self._runtimes.get(name)
            if rt is None:
                rt = Runtime(spec=spec)
                self._runtimes[name] = rt
            else:
                # 配置文件可能刚被后台改过（换端口/换知识库），以文件为准
                rt.spec = spec
            return rt

    def status_list(self) -> list[dict[str, Any]]:
        out = []
        for spec in instcfg.load_all():
            rt = self._runtimes.get(spec.name)
            if rt is None:
                rt = Runtime(spec=spec)
                self._runtimes[spec.name] = rt
            else:
                rt.spec = spec
            # 线程还活着但端口没了 = 进程还在、服务已经死了，别报"运行中"
            if rt.status == "running" and rt._thread and not rt._thread.is_alive():
                rt.status = "error"
                rt.error = "服务线程已退出"
            # 界面上要能看见"这套文案实际是什么语言"：配置里的 lang 是手填的，
            # 文案也是手填的，两者对不上时页面按内容走，用户却只看到下拉没反应。
            d = rt.as_dict()
            d["content_lang"] = _copy_lang(spec)
            out.append(d)
        return out

    def get(self, name: str) -> dict[str, Any]:
        return self._rt(name).as_dict()

    # ------------------------------------------------------------- 启停

    def start(self, name: str) -> dict[str, Any]:
        from .site import build_site

        rt = self._rt(name)
        if rt.status == "running":
            return rt.as_dict()

        if port_in_use(rt.spec.port) and rt.status != "running":
            rt.status = "error"
            rt.error = (f"端口 {rt.spec.port} 已被占用（可能是别的应用，"
                        f"或另一个实例还在跑）。换一个端口再启动。")
            return rt.as_dict()

        app = build_site(rt.spec, self.state)
        config = uvicorn.Config(
            app, host=rt.spec.host(), port=rt.spec.port,
            log_level="warning", access_log=False, log_config=None,
        )
        server = _Server(config)
        rt._server = server
        rt.status = "starting"
        rt.error = None

        def run() -> None:
            try:
                server.run()
            except Exception as exc:  # 端口绑定失败、loop 炸了都在这里
                rt.error = f"{type(exc).__name__}: {exc}"
                if rt.status != "stopping":
                    rt.status = "error"
            finally:
                if rt.status != "stopping":
                    rt.status = "stopped"

        rt._thread = threading.Thread(target=run, name=f"site-{name}", daemon=True)
        rt._thread.start()

        # 等它真的听上端口再报"运行中"：立刻返回 running 而实际启动失败，
        # 用户点开链接只会看到"无法访问"，还以为是浏览器的问题。
        for _ in range(60):
            time.sleep(0.1)
            if port_in_use(rt.spec.port):
                rt.status = "running"
                rt.started_at = time.time()
                break
            if rt.status == "error":
                break
        else:
            rt.status = "error"
            rt.error = rt.error or "启动超时（6 秒内没有监听端口）"
        if rt.status == "running":
            threading.Thread(
                target=self._probe_lang_capability, args=(rt,),
                name=f"probe-{name}", daemon=True).start()
        return rt.as_dict()

    # --------------------------------------------------------- 语言能力自检

    def _probe_lang_capability(self, rt: Runtime) -> None:
        """维吾尔语站点启动时，先确认这个模型到底能不能吐维吾尔语。

        qwen2.5-7b 这一档对维吾尔语的支持很弱：提示写得再硬，它也可能回中文、
        复读、或者把系统提示一起吐出来。不先测一次，用户只会看到"答案不对"，
        却分不清是检索错了、还是模型根本说不了维吾尔语。
        """
        from ..lang import detect, repeating

        if rt.spec.lang != "ug":
            return
        try:
            out = self.state.llm.complete(
                [{"role": "user", "content":
                  "把下面这句中文翻译成维吾尔语（用阿拉伯字母），只输出译文：\n"
                  "你好，今天天气很好。"}],
                provider=rt.spec.provider or None,
                temperature=0.0, max_tokens=60,
            )
        except Exception as exc:
            rt.ug_note = f"自检没跑通：{type(exc).__name__}: {exc}"
            return
        text = (getattr(out, "text", "") or "").strip()
        rt.ug_sample = text[:80]
        if detect(text) != "ug":
            rt.ug_capable = False
            rt.ug_note = ("这个模型没能把一句中文译成维吾尔语——维吾尔语站点大概率"
                          "会回中文。换一个维吾尔语能力更强的模型再试。")
        elif repeating(text):
            rt.ug_capable = False
            rt.ug_note = ("模型一开口就复读（维吾尔语输出最常见的失败模式）。"
                          "答案会被截断成半句，建议换更大的模型。")
        else:
            rt.ug_capable = True
            rt.ug_note = ("能输出维吾尔文字形。语义是否通顺要看实际答案——"
                          "7B 级模型在这一步仍可能跑偏，答案下方保留了中文原稿可对。")

    def stop(self, name: str) -> dict[str, Any]:
        rt = self._rt(name)
        if rt.status != "running" or not rt._server:
            rt.status = "stopped"
            rt.error = None
            return rt.as_dict()

        rt.status = "stopping"
        rt._server.should_exit = True
        rt._thread.join(timeout=8)
        if rt._thread.is_alive():
            rt.status = "error"
            rt.error = "8 秒内没能正常退出（可能还有请求在处理）"
            return rt.as_dict()
        rt.status = "stopped"
        rt.error = None
        rt.started_at = None
        rt._server = None
        rt._thread = None
        return rt.as_dict()

    def restart(self, name: str) -> dict[str, Any]:
        self.stop(name)
        return self.start(name)

    # ------------------------------------------------------------- 批量

    def start_autostart(self) -> list[str]:
        started = []
        for spec in instcfg.load_all():
            if spec.autostart:
                st = self.start(spec.name)
                if st["status"] == "running":
                    started.append(spec.name)
        return started

    def stop_all(self) -> None:
        for name in list(self._runtimes):
            try:
                self.stop(name)
            except Exception:
                pass
