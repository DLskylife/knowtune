"""前台实例的启停管理（2026-10-28）。

每个实例 = 一条线程里跑的一个 uvicorn，占一个端口。选"进程内多线程"而不是
每个实例起一个子进程，理由很实在：

1. **嵌入模型只有一份**。子进程方案要么每个站点各加载一次（多占几百 MB，还可能
   加载失败），要么搞一套 IPC 让子进程回主进程检索——那才是真正的冗杂。
2. 日志、配置、任务队列都在一起，出问题只有一个地方可查。
3. Windows 上没有 fork，子进程要处理进程树、孤儿进程和端口回收，麻烦得多。

代价是一个实例崩了会带走整个后台进程。用 uvicorn 自己的异常隔离（它不会让
请求里的异常逃到 loop 外）加上启动前的端口探测，把这个风险压到可接受。

**端口上到底是谁**（2026-11-06）。因为实例是进程内线程，一个后台进程就能
把 8800 占死；再开第二个后台（`knowtune serve --port 8013` 之外又起一个
8765），第二个后台的 autostart 必然撞墙，而"端口已被占用，换一个端口"这句
话在那个场景下是纯误导——8800 上跑的就是同一个站点，链接好好的，只是不归
它管。所以启动前先问一句 `/api/info`：

- 答话的是本进程（PID 相同）→ 直接认领，别再绑一次；
- 答话的是同名站点的另一个进程 → 标成"外部托管"，卡片照样给链接，但不谎报启停；
- 答话的是**另一个**站点 → 说清楚是哪个站点占了端口；
- 根本不是前台站点 → 用 psutil 查出名号，把"别的应用"四个字换成具体是谁。
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
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
    # 一句人话的补充说明。和 error 分开：error 是"没起来"，note 是"起来了但
    # 不在我手里"或者"我替你把状态纠正过来了"。
    note: str | None = None
    # 外部托管时：那个进程的 PID 和端口。界面上要能指名道姓。
    managed_by: dict[str, Any] | None = None
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
            "note": self.note,
            "managed_by": self.managed_by,
        }


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


# 本进程里真正在监听的那些 uvicorn：端口 -> 记录。
# Runtime 只是"账面上的状态"，这里才是"实际跑着的东西"。两者一旦脱钩
# （保存配置触发重启、线程异常退出、状态被重置），用户看到的就是
# "卡片说未运行、端口却连得上"——这种状态没有任何排查入口，所以账和实
# 必须存放在同一处，stop 也要能从端口反查回 server。
_LIVE: dict[int, dict[str, Any]] = {}
_LIVE_LOCK = threading.Lock()


def _live_record(name: str, port: int) -> dict[str, Any] | None:
    """先按端口找，再按名字找。

    为什么要兜一下名字：配置里改端口会立刻写盘，而正在跑的那个线程还绑在旧
    端口上。只按 rt.spec.port 查，就会查空——明明是自己，却被当成"别人"。
    """
    with _LIVE_LOCK:
        rec = _LIVE.get(port)
        if rec and rec.get("name") == name:
            return rec
        for r in _LIVE.values():
            if r.get("name") == name:
                return r
    return None


def probe_site(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> dict[str, Any] | None:
    """问一句这个端口上跑的是谁。不是前台站点（或没应答）就返回 None。

    必须 `trust_env=False`：本机回环流量走系统代理会直接超时，而这里等不起，
    也完全没必要——代理是按域名给的，127.0.0.1 永远该直连。
    """
    try:
        with httpx.Client(trust_env=False, timeout=timeout) as c:
            r = c.get(f"http://{host}:{port}/api/info")
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("name"):
        return None
    return {
        "name": str(d.get("name")),
        "pid": d.get("pid"),
        # 老版本没有 app 字段，靠 /api/info 的形状也能认；这里只做加分项。
        "app": str(d.get("app") or ""),
        "kb_name": str(d.get("kb_name") or ""),
    }


def port_owner(port: int) -> dict[str, Any] | None:
    """端口对应的进程（PID / 可执行名）。查不到就 None，别为了报错去装依赖。

    psutil 在依赖里，但它是可选路径：拿不到时调用方会退回到"查不出是谁"，
    而不是整页崩掉。
    """
    try:
        import psutil
    except Exception:
        return None
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status != psutil.CONN_LISTEN or not c.laddr:
                continue
            if c.laddr.port != port or not c.pid:
                continue
            try:
                exe = psutil.Process(c.pid).name()
            except Exception:
                exe = ""
            return {"pid": c.pid, "exe": exe}
    except Exception:
        return None
    return None


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
            elif rt.status == "running" and not port_in_use(rt.spec.port):
                rt.status = "error"
                rt.error = (f"线程还在，但 {rt.spec.port} 端口没在监听"
                            "（多半是绑定失败，看看后台日志）")
            # 账面说没跑、端口上却明明是自己或同名站点：按事实改口。
            # 少了这一步，错误提示会一直挂着，哪怕问题早就没了。
            if rt.status in ("stopped", "error", "external"):
                self._reconcile(rt)
            # 界面上要能看见"这套文案实际是什么语言"：配置里的 lang 是手填的，
            # 文案也是手填的，两者对不上时页面按内容走，用户却只看到下拉没反应。
            d = rt.as_dict()
            d["content_lang"] = _copy_lang(spec)
            out.append(d)
        return out

    def get(self, name: str) -> dict[str, Any]:
        return self._rt(name).as_dict()

    # ------------------------------------------------------- 端口上的身份辨认

    def _who(self, port: int, name: str) -> tuple[str, dict[str, Any] | None]:
        """端口上到底是谁。

        返回的判定决定下一步动作，而不是让调用方拿着一个 bool 自己猜：
        self（本进程的同一个站点）| twin（另一个进程的同名站点）|
        other_site（另一个前台站点）| foreign（别的程序）| unknown（问不出来）
        """
        with _LIVE_LOCK:
            rec = _LIVE.get(port)
        if rec and rec.get("name") == name and rec.get("server") is not None:
            return "self", rec

        info = probe_site(port)
        if info:
            return ("twin" if info["name"] == name else "other_site"), info
        owner = port_owner(port)
        if owner:
            return "foreign", owner
        return "unknown", None

    def _adopt(self, rt: Runtime, rec: dict[str, Any]) -> None:
        """本进程里已经在跑的实例，把账认回来，别再绑一次端口。"""
        rt._server = rec.get("server")
        rt._thread = rec.get("thread")
        rt.status = "running"
        rt.error = None
        rt.managed_by = None
        rt.started_at = rt.started_at or rec.get("started_at") or time.time()
        rt.note = "本进程里已经在跑，直接接管（之前的运行状态记录丢了）"

    def _mark_external(self, rt: Runtime, info: dict[str, Any]) -> None:
        """同名站点跑在**别的后台进程**里：链接可用，但不归这个后台管。"""
        pid = info.get("pid")
        who = f"PID {pid}" if pid else "另一个后台进程"
        rt.status = "external"
        rt.error = None
        rt.managed_by = {"pid": pid, "port": rt.spec.port}
        rt.note = (f"这个站点正跑在{who}里，链接现在就能用。本页只能查看——"
                   "要启停，去那个后台；想让这里接管，先关掉它再点启动。")

    def _reconcile(self, rt: Runtime) -> None:
        """账面上的状态和端口上的事实对一次账，只修"用户能看见的谎"。"""
        port = rt.spec.port
        if not port_in_use(port):
            if rt.status == "external":   # 那边已经关了
                rt.status = "stopped"
                rt.managed_by = None
                rt.note = None
            return
        kind, who = self._who(port, rt.spec.name or "")
        if kind == "self" and who is not None:
            self._adopt(rt, who)
        elif kind == "twin" and who is not None:
            self._mark_external(rt, who)
        # other_site / foreign / unknown：不动已有的报错，start() 写的那句更具体。

    # ------------------------------------------------------------- 启停

    def start(self, name: str) -> dict[str, Any]:
        from .site import build_site

        rt = self._rt(name)
        if rt.status == "running":
            return rt.as_dict()

        if port_in_use(rt.spec.port):
            rt.managed_by = None
            kind, who = self._who(rt.spec.port, rt.spec.name)
            if kind == "self" and who is not None:
                self._adopt(rt, who)
                return rt.as_dict()
            rt.status = "error"
            rt.note = None
            if kind == "twin" and who is not None:
                self._mark_external(rt, who)
                return rt.as_dict()
            if kind == "other_site" and who is not None:
                rt.error = (f"端口 {rt.spec.port} 上是另一个前台站点"
                            f"「{who['name']}」，不是本实例。把那个站点的端口改掉，"
                            "或者给本实例换一个端口。")
            elif kind == "foreign" and who is not None:
                rt.error = (f"端口 {rt.spec.port} 被 {who.get('exe') or '另一个程序'}"
                            f"（PID {who.get('pid')}）占着，不是本系统的前台站点。"
                            "先结束它，或给本实例换一个端口。")
            else:
                rt.error = (f"端口 {rt.spec.port} 被占着，但问不出是谁"
                            "（可能是别的应用，也可能是还没起完的进程）。"
                            "等几秒再试，或换一个端口。")
            return rt.as_dict()

        app = build_site(rt.spec, self.state)
        # 端口在**这一刻**就钉死。之后再改配置（保存会写盘、rt.spec 也会跟着换），
        # 这一条记录仍然只属于 old port——线程还绑在这儿呢。
        bound_port = rt.spec.port
        config = uvicorn.Config(
            app, host=rt.spec.host(), port=bound_port,
            log_level="warning", access_log=False, log_config=None,
        )
        server = _Server(config)
        rt._server = server
        rt.status = "starting"
        rt.error = None
        rt.note = None
        rt.managed_by = None

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
                # 线程走了，账上的运行体也要撤：留着会让下一次 start() 以为
                # 自己还活着，转而去"接管"一个已经不复存在的 server。
                with _LIVE_LOCK:
                    rec = _LIVE.get(bound_port)
                    if rec is not None and rec.get("server") is server:
                        _LIVE.pop(bound_port, None)

        rt._thread = threading.Thread(target=run, name=f"site-{name}", daemon=True)
        with _LIVE_LOCK:
            _LIVE[bound_port] = {
                "server": server, "thread": rt._thread,
                "name": name, "started_at": time.time(),
            }
        rt._thread.start()

        # 等它真的听上端口再报"运行中"：立刻返回 running 而实际启动失败，
        # 用户点开链接只会看到"无法访问"，还以为是浏览器的问题。
        for _ in range(60):
            time.sleep(0.1)
            if port_in_use(bound_port):
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
        if rt.status == "external":
            # 归另一个后台管，这里没有它的 server 句柄。硬报"已停止"就是撒谎：
            # 链接还会照样打开，用户会以为按钮坏了。
            who = (rt.managed_by or {}).get("pid")
            rt.note = (f"这个站点在{'PID ' + str(who) if who else '另一个后台进程'}"
                       "里跑着，本页停不了它。要停，去那个后台；或者关掉它再回这里。")
            return rt.as_dict()

        # 句柄优先从端口反查：rt._server 丢了也要能停掉真实在跑的那个。
        rec = _live_record(name, rt.spec.port)
        server = rt._server or (rec or {}).get("server")
        thread = rt._thread or (rec or {}).get("thread")

        if not server:
            rt.status = "stopped"
            rt.error = None
            rt.note = None
            rt.managed_by = None
            rt.started_at = None
            return rt.as_dict()

        rt.status = "stopping"
        server.should_exit = True
        if thread is not None:
            thread.join(timeout=8)
        if thread is not None and thread.is_alive():
            rt.status = "error"
            rt.error = "8 秒内没能正常退出（可能还有请求在处理）"
            return rt.as_dict()
        rt.status = "stopped"
        rt.error = None
        rt.note = None
        rt.managed_by = None
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
