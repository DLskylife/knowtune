"""本地模型：扫 .gguf，用 llama.cpp 起一个 OpenAI 兼容端点。

这个模块只回答三个问题，每个都对应界面上一个按钮：

1. **我机器上有哪些模型？** —— `scan()` 扫目录找 `.gguf`。
2. **llama-server 在哪？** —— `find_server()`，找不到就说清去哪装。
3. **把某个模型跑起来 / 停掉 / 活着没？** —— `start/stop/status`。

设计上的几个硬约束：

- **一个模型一个进程一个端口**。llama.cpp 不支持在同一个进程里热切换模型，
  所以「选模型」在本地语境下就等于「换一个监听端口」。这决定了配置里模型必须
  带 `port`，否则两个模型会抢同一个口。
- **不猜杀进程**。`stop()` 只杀自己写进 pid 文件的那个 pid，并且在杀之前核对
  命令行里确实有 `llama-server`——pid 会被系统复用，照着一个旧 pid 杀下去可能
  砍掉用户别的程序。
- **启动必须有上限**。加载 7B 的 gguf 在机械盘上能花两分钟，但也可能因为显存
  不足直接崩。`start()` 轮询 `/v1/models` 直到 `ready_timeout_s`，超时就杀掉
  子进程并报错——留下一个半死不活的进程比直接失败更难排查。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / "data" / "llamacpp"

# 只有 .gguf 能被 llama.cpp 直接吃。safetensors 需要先转换，不在扫描范围内——
# 扫出来也起不了，列在界面上只会制造"为什么点了没反应"。
SUFFIXES = (".gguf",)

# 找 llama-server 的顺序。环境变量优先：用户自己编译或解压到奇怪路径时，
# 让他用 KNOWTUNE_LLAMA_SERVER 指过去，比让程序去猜十个目录靠谱。
ENV_SERVER = "KNOWTUNE_LLAMA_SERVER"
SERVER_NAMES = ("llama-server.exe", "llama-server") if os.name == "nt" else ("llama-server",)

# 端口区间：8080 是 llama.cpp 自己的默认，8000 是本系统 Web 服务的，
# 8100 留给以前配过 vLLM 的人。8090 起一段专用，冲突概率最低。
PORT_MIN = 8090
PORT_MAX = 8189

# 默认上下文。太小会让长答案被截断，太大会直接吃光显存起不来。
# 4096 是 7B Q4 在 8G 显存上能稳跑的常见值。
DEFAULT_CTX = 4096

# 启动等待上限。7B 从机械盘加载 + 预热，两分钟内没起来基本就是起不来了。
READY_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 1.0


class LocalError(Exception):
    """本地实例相关的所有失败。message 直接给用户看，必须是能行动的话。"""


@dataclass
class ScannedModel:
    path: str
    name: str
    size_mb: int
    quant: str = ""        # 从文件名里认出来的量化档，认不出就空
    root: str = ""         # 属于哪个扫描目录，界面上用来分组

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "name": self.name,
            "size_mb": self.size_mb, "quant": self.quant, "root": self.root,
        }


@dataclass
class Instance:
    name: str
    path: str
    port: int
    pid: int
    started_at: float
    ctx: int
    log: str = ""
    # 监听地址。127.0.0.1 = 只本机；0.0.0.0 = 局域网也能调。
    # base_url 一律给回环地址：它表示"从这台机器怎么调"，即使绑了 0.0.0.0
    # 回环也仍然可用；局域网地址单独放在 lan_base_url 里。
    host: str = "127.0.0.1"
    # 发给 /v1/chat/completions 的 model 值。llama-server 用 --alias 固定它，
    # 配置清单里的 id 与之一致，外部调用才不用猜。
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def lan_base_url(self) -> str:
        if self.host not in ("0.0.0.0", "::"):
            return ""
        ip = _lan_ip()
        return f"http://{ip}:{self.port}/v1" if ip else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "path": self.path, "port": self.port,
            "pid": self.pid, "started_at": self.started_at, "ctx": self.ctx,
            "host": self.host, "model": self.model,
            "base_url": self.base_url, "lan_base_url": self.lan_base_url,
            "chat_url": self.base_url + "/chat/completions",
            "models_url": self.base_url + "/models",
            "log": self.log,
        }


def _lan_ip() -> str:
    """本机在局域网里的 IP。UDP connect 不真的发包，拿的是出口网卡地址。"""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return str(s.getsockname()[0])
    except OSError:
        return ""
    finally:
        s.close()


def api_info(inst: Instance) -> dict[str, Any]:
    """给外部调用方的一揽子信息：地址、model、能直接粘的 curl。

    单独一个函数而不是让界面自己拼：地址在三个地方出现（启动返回、状态接口、
    界面卡片），各自拼一次必然漂移。
    """
    model = inst.model or Path(inst.path).stem
    d = inst.as_dict()
    d["model"] = model
    d["curl"] = (
        f'curl {d["chat_url"]} -H "Content-Type: application/json" '
        f'-d \'{{"model": "{model}", "messages": [{{"role": "user", '
        f'"content": "你好"}}]}}\''
    )
    return d


# ------------------------------------------------------------------ 扫描


def default_dirs() -> list[str]:
    """没配扫描目录时去哪找。

    只放**真的常见**的几个位置，不递归整个磁盘——那会把一次点击变成几分钟的
    磁盘 IO。LM Studio / Ollama / llama.cpp 的默认下载目录各算一个，再加上
    项目自己的 data/models。
    """
    home = Path.home()
    cands = [
        ROOT / "data" / "models",
        home / ".cache" / "lm-studio" / "models",
        home / ".lmstudio" / "models",
        home / ".ollama" / "models",
        home / "models",
        Path("D:/models") if os.name == "nt" else Path("/models"),
    ]
    # LM Studio 的模型根目录是可改的（装到 D 盘的人几乎都改过），改完只写进
    # 它自己的 settings.json（downloadsFolder）。不去读它，默认列表就永远猜
    # .lmstudio/models，扫出来一个都没有——实测这台机器模型全在
    # D:/llamafactory/models，而那个"默认位置"是空目录。
    lm_root = _lmstudio_root(home)
    if lm_root:
        cands.insert(0, lm_root)
    return [str(p) for p in cands if p.exists()]


def _lmstudio_root(home: Path) -> Path | None:
    """读 LM Studio settings.json 里的 downloadsFolder。读不了/没有就返回 None。"""
    import json

    try:
        raw = (home / ".lmstudio" / "settings.json").read_text(encoding="utf-8")
        folder = (json.loads(raw) or {}).get("downloadsFolder") or ""
    except (OSError, ValueError):
        return None
    folder = folder.strip()
    return Path(folder) if folder else None


def _quant_of(name: str) -> str:
    """从文件名里认量化档：Q4_K_M / IQ3_XS / Q8_0 这些。

    不认也没关系，纯展示用；但认出来很有用——同一个模型的三份量化在列表里
    长得几乎一样，只有这几位能区分。
    """
    up = name.upper()
    for tag in ("IQ1_S", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ2_M", "IQ3_XXS", "IQ3_XS",
                "IQ3_S", "IQ3_M", "IQ4_XS", "IQ4_NL", "Q2_K", "Q3_K_S", "Q3_K_M",
                "Q3_K_L", "Q4_0", "Q4_K_S", "Q4_K_M", "Q5_0", "Q5_K_S", "Q5_K_M",
                "Q6_K", "Q8_0", "F16", "BF16"):
        # 用 - / . / _ 包起来的才算，避免 "Q4_0_test" 这种误判成两档
        if tag in up:
            return tag
    return ""


def scan(dirs: Iterable[str] | None = None, limit: int = 500) -> list[ScannedModel]:
    """递归找 .gguf。

    深度上限 4：模型目录通常是 `<厂商>/<模型名>/<文件>.gguf`，四层够用；
    再深就是用户在往里塞乱七八糟的东西，扫出来的也不是模型。
    """
    roots = [str(d) for d in (dirs or default_dirs())]
    out: list[ScannedModel] = []
    seen: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.exists() or not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel = Path(dirpath).relative_to(base)
            if len(rel.parts) >= 4:
                dirnames[:] = []
                continue
            for fn in filenames:
                if not fn.lower().endswith(SUFFIXES):
                    continue
                full = os.path.join(dirpath, fn)
                if full in seen:
                    continue
                seen.add(full)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                out.append(ScannedModel(
                    path=full,
                    name=fn[:-len(".gguf")],
                    size_mb=int(size / 1024 / 1024),
                    quant=_quant_of(fn),
                    root=str(base),
                ))
                if len(out) >= limit:
                    return out
    out.sort(key=lambda m: (-m.size_mb, m.name))
    return out


# ------------------------------------------------------------------ 可执行


def find_server(explicit: str = "") -> str:
    """定位 llama-server。找到返回绝对路径，找不到抛 LocalError（带安装指引）。"""
    if explicit and Path(explicit).exists():
        return str(Path(explicit))
    env = (os.environ.get(ENV_SERVER) or "").strip()
    if env and Path(env).exists():
        return str(Path(env))
    for name in SERVER_NAMES:
        hit = shutil.which(name)
        if hit:
            return hit
    # 常见解压位置兜底。列出来是因为 Windows 用户多半是下 zip 解压，
    # 很少有人会把它加进 PATH。
    guesses = [
        ROOT / "tools" / "llama" / SERVER_NAMES[0],
        Path("C:/llama.cpp") / SERVER_NAMES[0],
        Path("C:/llama") / SERVER_NAMES[0],
        Path.home() / "llama.cpp" / SERVER_NAMES[0],
    ]
    for g in guesses:
        if g.exists():
            return str(g)
    raise LocalError(
        "没找到 llama-server。最省事的一条命令：\n"
        f"  python scripts/get_llama.py\n"
        "它会挑对系统和后端（Windows 默认 Vulkan，走显卡驱动、不用另装 CUDA），"
        f"下载后解压到 {ROOT / 'tools' / 'llama'}。\n"
        "也可以手工三选一：\n"
        "1) 去 github.com/ggml-org/llama.cpp/releases 下对应系统的压缩包，"
        "解压后把 llama-server 所在目录加进 PATH；\n"
        f"2) 设环境变量 {ENV_SERVER}=<llama-server 的完整路径>；\n"
        f"3) 直接放进 {ROOT / 'tools' / 'llama'} 目录。"
    )


# ------------------------------------------------------------------ 状态


def _state_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]
    return STATE_DIR / f"{safe}.json"


def _write_state(inst: Instance) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(inst.name).write_text(
        json.dumps(inst.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _read_state(name: str) -> dict[str, Any] | None:
    p = _state_path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # tasklist 比 os.kill(pid, 0) 可靠：Windows 上后者对已退出进程也可能
        # 返回成功，导致"明明死了却显示运行中"。
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=8,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return str(pid) in (r.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cmdline_of(pid: int) -> str:
    """拿进程命令行，用来确认这个 pid 真的是我们的 llama-server。"""
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
                capture_output=True, text=True, timeout=8,
                creationflags=0x08000000,
            )
            return r.stdout or ""
        return Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _alive(port: int, timeout: float = 0.6) -> bool:
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.get(f"http://127.0.0.1:{port}/v1/models")
            return r.status_code < 500
    except httpx.HTTPError:
        return False


def status(name: str) -> dict[str, Any]:
    """这个模型现在什么状态。四个值：

    - `running`  进程在、端口通
    - `starting` 进程在、端口还没通（刚点启动那一小会儿）
    - `stopped`  没进程，但有过启动记录
    - `unknown`  从没启动过

    `api` 是外部调用方要的那一段：地址、chat 端点、model、curl 示例。
    """
    st = _read_state(name)
    if not st:
        return {"name": name, "state": "unknown", "port": None, "pid": None,
                "base_url": "", "api": {}, "message": "还没启动过"}
    pid = int(st.get("pid") or 0)
    port = int(st.get("port") or 0)
    api = _api_from_state(st)
    if not _pid_alive(pid):
        return {"name": name, "state": "stopped", "port": port, "pid": pid,
                "base_url": api.get("base_url", ""), "api": api,
                "message": "进程已退出（显存不足或手动关闭）"}
    ok = _alive(port)
    return {
        "name": name, "state": "running" if ok else "starting",
        "port": port, "pid": pid, "base_url": api.get("base_url", ""),
        "api": api, "log": st.get("log", ""),
        "message": "" if ok else "进程在，端口还没就绪",
    }


def _api_from_state(st: dict[str, Any]) -> dict[str, Any]:
    """从落盘的状态重建 api 信息。老记录里没有 host/model 也要能给出地址。"""
    port = int(st.get("port") or 0)
    if not port:
        return {}
    inst = Instance(
        name=str(st.get("name") or ""), path=str(st.get("path") or ""),
        port=port, pid=int(st.get("pid") or 0),
        started_at=float(st.get("started_at") or 0),
        ctx=int(st.get("ctx") or 0), log=str(st.get("log") or ""),
        host=str(st.get("host") or "127.0.0.1"),
        model=str(st.get("model") or Path(st.get("path") or "model").stem),
    )
    return api_info(inst)


def running() -> dict[str, dict[str, Any]]:
    """所有有记录的实例状态，按名字索引。list 页面一次拿全，不必逐个问。"""
    out: dict[str, dict[str, Any]] = {}
    if not STATE_DIR.exists():
        return out
    for p in STATE_DIR.glob("*.json"):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = str(st.get("name") or p.stem)
        out[name] = status(name)
    return out


def _pick_port(preferred: int | None = None, *, strict: bool = False) -> int:
    """挑一个没被占的端口。preferred 可用就用它，否则在区间里扫。

    strict=True 时 preferred 被占就报错，不另挑一个。原因：外部程序写死的
    API 地址里带着端口，悄悄换一个等于让那个地址失效——宁可启动失败并说清
    "谁占了、怎么办"，也不要给一个看起来成功、其实别人调不通的结果。
    """
    import socket

    def free(p: int) -> bool:
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", p)) != 0

    if preferred:
        if free(preferred):
            return preferred
        if strict:
            raise LocalError(
                f"配置的端口 {preferred} 已被占用，不能悄悄换端口——"
                f"外部程序写死的地址会失效。先停掉占用它的进程，"
                f"或在模型清单里把 port 改成一个空端口再启动。")
    for p in range(PORT_MIN, PORT_MAX + 1):
        if free(p):
            return p
    raise LocalError(f"{PORT_MIN}-{PORT_MAX} 全被占了，先关掉几个再试")


# ------------------------------------------------------------------ 启停


def start(
    *,
    name: str,
    path: str,
    port: int | None = None,
    ctx: int = DEFAULT_CTX,
    ngl: int | None = None,
    alias: str = "",
    host: str = "127.0.0.1",
    server: str = "",
    ready_timeout_s: float = READY_TIMEOUT_S,
    extra_args: list[str] | None = None,
) -> Instance:
    """起一个 llama-server，等它真的能应答再返回。

    失败时一定把子进程杀掉：留一个占了端口但永远不就绪的进程，下次点启动会
    报"端口被占用"，而占用者正是自己上一次失败的那次尝试——这种自我阻塞最难查。
    """
    p = Path(path)
    if not p.exists():
        raise LocalError(f"模型文件不存在：{path}")
    if p.suffix.lower() not in SUFFIXES:
        raise LocalError("只支持 .gguf")

    cur = status(name)
    if cur["state"] in ("running", "starting"):
        raise LocalError(f"「{name}」已经在跑（端口 {cur['port']}），先停掉再启动")

    exe = find_server(server)
    chosen = _pick_port(port, strict=bool(port))
    host = (host or "127.0.0.1").strip()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / f"{_state_path(name).stem}.log"

    cmd = [
        exe, "-m", str(p),
        "--host", host,
        "--port", str(chosen),
        "-c", str(max(512, int(ctx))),
        # 单槽位。llama-server 默认开 4 个并发槽，每个槽各留一份 ctx 大小的
        # KV 缓存：7B + 4×4096 在 6G 卡上光 KV 就占 2G。本地实例本来就是给
        # 一个人用的，多出来的槽位只是把显存从权重手里拿走。
        # 实测（RTX 3060 6G，Qwen2.5-7B Q4）：出词速度 27.8 → 27.0 tok/s，
        # 看不出差别——省下的显存被自动卸载策略放回了权重，而瓶颈不在这。
        # 保留它的理由是语义正确 + 给更大的 ctx 留空间，不是"提速"。
        "--parallel", "1",
        "--no-webui",          # 我们只要 /v1，自带网页会多占一屏显存和一份静态资源
        "--jinja",             # 用 gguf 自带的 chat template，否则多轮格式会错
    ]
    if ngl is not None and ngl >= 0:
        cmd += ["-ngl", str(ngl)]
    if alias:
        # 固定 /v1/models 返回的名字。不传的话 llama.cpp 会拿文件名当 id，
        # 于是"配置里写 a、服务回报 b"，模型名对不上又得多一层映射。
        cmd += ["--alias", alias]
    if extra_args:
        cmd += [str(a) for a in extra_args]

    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    kwargs: dict[str, Any] = {
        "stdout": log_f, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
        "cwd": str(p.parent),
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：别弹一个黑框出来
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        log_f.close()
        raise LocalError(f"启动 {exe} 失败：{exc}") from exc

    inst = Instance(name=name, path=str(p), port=chosen, pid=proc.pid,
                    started_at=time.time(), ctx=int(ctx), log=str(log_path),
                    host=host, model=alias or p.stem)
    _write_state(inst)

    deadline = time.time() + ready_timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _tail(log_path, 1200)
            _write_state(replace(inst, pid=0))
            raise LocalError(f"llama-server 启动后立刻退出（退出码 {proc.returncode}）。\n{tail}")
        if _alive(chosen, timeout=1.0):
            return inst
        time.sleep(POLL_INTERVAL_S)

    _kill(proc.pid)
    _write_state(replace(inst, pid=0))
    raise LocalError(
        f"等待 {int(ready_timeout_s)}s 仍未就绪，已停掉进程。\n{_tail(log_path, 1200)}")


def _tail(path: Path, chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-chars:].strip()


def _kill(pid: int) -> None:
    """只杀确认过是 llama-server 的 pid。"""
    if not _pid_alive(pid):
        return
    if "llama" not in _cmdline_of(pid).lower():
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=0x08000000)
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError):
        pass


def stop(name: str) -> dict[str, Any]:
    st = _read_state(name)
    if not st:
        return {"name": name, "stopped": False, "message": "没有启动记录"}
    pid = int(st.get("pid") or 0)
    had = _pid_alive(pid)
    _kill(pid)
    _write_state(Instance(
        name=name, path=str(st.get("path", "")), port=int(st.get("port") or 0),
        pid=0, started_at=float(st.get("started_at") or 0), ctx=int(st.get("ctx") or 0),
        log=str(st.get("log", ""))))
    return {"name": name, "stopped": had,
            "message": "已停止" if had else "进程本来就没在跑"}


def forget(name: str) -> None:
    """删掉启动记录（模型从配置里移除时调）。不杀进程——那是 stop 的事。"""
    try:
        _state_path(name).unlink()
    except OSError:
        pass


def base_url_for(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/v1"


def port_of(url: str | None, default: int = 0) -> int:
    try:
        return urlsplit(url or "").port or default
    except ValueError:
        return default


def describe_env() -> dict[str, Any]:
    """界面顶部那行环境说明：能不能起、去哪找模型。"""
    try:
        exe = find_server()
        ok, msg = True, exe
    except LocalError as exc:
        exe, ok, msg = "", False, str(exc)
    return {
        "server": exe,
        "available": ok,
        "message": msg,
        "dirs": default_dirs(),
        "port_range": [PORT_MIN, PORT_MAX],
        "platform": sys.platform,
        "devices": devices(exe) if ok else [],
    }


_DEVS: tuple[float, list[dict[str, Any]]] | None = None
_DEV_CACHE_S = 60.0


def devices(server: str = "") -> list[dict[str, Any]]:
    """llama-server 能用的计算设备（Vulkan0 / CUDA0 …）。

    为什么值得单独查：同样一个 7B，跑在 3060 上是 28 tok/s，跑在 CPU 上是 5。
    用户点启动前就该看到"这次会用哪块卡"，而不是等答案一个字一个字蹦出来才
    怀疑哪里不对。缓存 60 秒——这个函数要起一次子进程。
    """
    global _DEVS
    now = time.time()
    if _DEVS and now - _DEVS[0] < _DEV_CACHE_S:
        return _DEVS[1]
    out: list[dict[str, Any]] = []
    try:
        exe = find_server(server)
        r = subprocess.run(
            [exe, "--list-devices"], capture_output=True, text=True, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        for line in (r.stdout or "").splitlines():
            m = re.match(r"\s*([A-Za-z]+\d+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)", line)
            if m:
                out.append({
                    "id": m.group(1), "name": m.group(2),
                    "total_mb": int(m.group(3)), "free_mb": int(m.group(4)),
                })
    except (LocalError, OSError, subprocess.SubprocessError):
        pass
    _DEVS = (now, out)
    return out
