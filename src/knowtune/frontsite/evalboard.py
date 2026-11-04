"""模型评测台的托管（2026-11-04）。

`evals/scoreboard.py` 跑在**独立 venv** 里：它要 Gradio 6，主项目那套
fastapi/uvicorn/httpx 的版本组合跟它对不上。硬塞进同一个 venv，轻则启动报
版本冲突，重则把主服务依赖一起升级掉——所以它是一个真的子进程，而不是
InstanceManager 里的一条线程。

子进程方案的代价是"句柄会丢"：后台服务重启一次，`Popen` 对象就没了，7860
上那个进程还在跑。所以把 pid 落盘（`evals/runs/.scoreboard.pid`），并用
netstat 拿"此刻到底是谁占着端口"来对账：

    端口在听 + pid 与落盘的一致  → 我们自己起的，可以停
    端口在听 + pid 对不上        → 别人占着 7860，拒绝动手
    端口没人听 + 有 pid 文件     → 上次没退干净，删掉文件即可

判"在不在跑"只认端口，不认 Popen 对象：进程可能已经死了但对象还活着，
反过来也可能（重启后台之后）。端口才是唯一的事实。
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .. import logbus

ROOT = Path(__file__).resolve().parents[3]
EVALS = ROOT / "evals"
SCRIPT = EVALS / "scoreboard.py"
PROFILES_PY = EVALS / "profiles.py"
RUNS = EVALS / "runs"
PIDFILE = RUNS / ".scoreboard.pid"
LOG = RUNS / "scoreboard.log"

PORT = 7860

PY = EVALS / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

# Windows 上没有 CREATE_NO_WINDOW 的话，启一个 python 子进程会顺带弹一个黑框。
_NOCONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_proc: subprocess.Popen | None = None
_lock = threading.RLock()


def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _port_pid(port: int) -> int | None:
    """谁在监听这个端口。取不到就返回 None（不抛：调用方只是想知道状态）。"""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, timeout=8, creationflags=_NOCONSOLE,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.decode("mbcs", "replace").splitlines():
            parts = line.split()
            # proto  local  foreign  state  pid
            if len(parts) < 5 or parts[-2].upper() != "LISTENING":
                continue
            if not parts[1].endswith(f":{port}"):
                continue
            try:
                return int(parts[-1])
            except ValueError:
                return None
        return None

    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        first = (out.stdout or "").split()
        return int(first[0]) if first else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _read_pidfile() -> int | None:
    try:
        return int(PIDFILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    """这个 pid 到底是什么程序。只在"要不要结束它"这一步问，不进 status——
    查一次要几百毫秒（Windows 上还得起一个 PowerShell），而卡片每刷新一次
    都会问一遍状态，那点延迟会变成肉眼可见的卡顿。"""
    if os.name != "nt":
        try:
            return (Path(f"/proc/{pid}/cmdline").read_bytes()
                    .replace(b"\0", b" ").decode("utf-8", "replace"))
        except OSError:
            return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, timeout=12, creationflags=_NOCONSOLE,
        ).stdout
        return out.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _tail(path: Path, n: int = 6) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines[-n:] if ln.strip()]


def _count_cases(path: Path) -> int:
    try:
        return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


def _profiles() -> list[dict[str, Any]]:
    """档案清单直接从 evals/profiles.py 加载。

    profiles.py 只依赖 json/re/pathlib，没有 gradio，所以可以在主服务里安全
    执行——比起在后端再抄一份内置档案的名字，抄的那份迟早和代码说不一样的。
    """
    if not PROFILES_PY.exists():
        return []
    try:
        spec = importlib.util.spec_from_file_location("_kt_eval_profiles", PROFILES_PY)
        if spec is None or spec.loader is None:
            return []
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = []
        for key, prof in mod.all_profiles().items():
            cases = str(prof.get("cases") or "")
            out.append({
                "key": key,
                "label": prof.get("label") or key,
                "dims": [d.get("label") or d.get("key") for d in prof.get("dimensions") or []],
                "cases": _count_cases(EVALS / cases) if cases else 0,
            })
        return out
    except Exception as exc:      # 档案写坏了不该让整张卡消失
        return [{"key": "error", "label": f"档案读取失败：{exc}", "dims": [], "cases": 0}]


def _runs() -> dict[str, int]:
    out = {"dpo": 0, "reports": 0, "samples": 0}
    if not RUNS.exists():
        return out
    for f in RUNS.iterdir():
        n = f.name
        if n.startswith("dpo_") and n.endswith(".jsonl"):
            out["dpo"] += 1
        elif n.startswith("eval_") and n.endswith(".md"):
            out["reports"] += 1
        elif n.startswith("sample_") and n.endswith(".csv"):
            out["samples"] += 1
    return out


def status() -> dict[str, Any]:
    pid = _port_pid(PORT)
    own = _read_pidfile()
    running = pid is not None
    managed = bool(pid and (pid == own or (_proc is not None and _proc.pid == pid)))
    ready = PY.exists() and SCRIPT.exists()
    return {
        "key": "eval",
        "port": PORT,
        "url": f"http://127.0.0.1:{PORT}/",
        "running": running,
        "pid": pid,
        "managed": managed,
        "ready": ready,
        "python": _rel(PY),
        "script": _rel(SCRIPT),
        "log": _rel(LOG),
        "profiles": _profiles(),
        "runs": _runs(),
        "error": None if ready else (
            "找不到 evals/.venv/Scripts/python.exe" if not PY.exists()
            else "找不到 evals/scoreboard.py"),
    }


def start(wait_s: float = 45.0, gateway: str = "") -> dict[str, Any]:
    """启动独立进程。gateway 是主服务自己的 /v1 地址，显式传进子进程环境。

    打分台跑在另一个 venv 里，靠继承 KNOWTUNE_GATEWAY 找主服务；但主服务
    若不是经 `knowtune serve` 起的（直接 uvicorn），那个变量就不存在，子进程
    会去猜 8000。由调用方按真实 request.base_url 传进来，猜端口这回事就不存在了。
    """
    global _proc
    with _lock:
        st = status()
        if st["running"]:
            return st
        if not st["ready"]:
            raise RuntimeError(st["error"] or "评测台文件缺失")

        RUNS.mkdir(parents=True, exist_ok=True)
        child_env = os.environ.copy()
        if gateway:
            child_env["KNOWTUNE_GATEWAY"] = gateway.rstrip("/")
        # 日志追加而不是覆盖：上一次为什么起不来，往往就在上一段末尾。
        with open(LOG, "ab") as log:
            _proc = subprocess.Popen(
                [str(PY), str(SCRIPT), "--port", str(PORT), "--no-browser"],
                cwd=str(EVALS), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                env=child_env, creationflags=_NOCONSOLE,
            )
        PIDFILE.write_text(str(_proc.pid), encoding="utf-8")
        logbus.note(f"评测台启动中（pid {_proc.pid}，端口 {PORT}）", source="eval")

        deadline = time.time() + wait_s
        while time.time() < deadline:
            if _proc.poll() is not None:
                tail = "；".join(_tail(LOG, 4))
                PIDFILE.unlink(missing_ok=True)
                _proc = None
                raise RuntimeError(f"评测台启动失败（进程已退出）{('：' + tail) if tail else ''}")
            ready_pid = _port_pid(PORT)
            if ready_pid is not None:
                # pid 文件里记的必须是**监听端口那个进程**，而不是 Popen 拿到的那个。
                # venv 的 python.exe 在 Windows 上是个 launcher：它再拉起一个真解释器
                # 然后等它退出，两者 pid 不同。记错了 status 会一直报"非后台托管"
                # （端口上的 pid 和文件里对不上），停止按钮也只能退回去查命令行——
                # 那一步要起一个 PowerShell，慢得没道理。
                PIDFILE.write_text(str(ready_pid), encoding="utf-8")
                logbus.note(f"评测台已就绪：http://127.0.0.1:{PORT}/", source="eval")
                return status()
            time.sleep(0.3)

        # 超时：把刚才写进去的 pid 清掉，否则下次 status 会拿着一个
        # "看起来有人、实际没监听"的 pid 去判断托管关系。
        PIDFILE.unlink(missing_ok=True)
        raise RuntimeError(f"启动超时（{int(wait_s)} 秒内没有监听 {PORT}），"
                           f"日志见 {_rel(LOG)}")


def stop(wait_s: float = 12.0) -> dict[str, Any]:
    global _proc
    with _lock:
        pid = _port_pid(PORT)
        if pid is None:
            # 端口空着：可能只是残留的 pid 文件
            PIDFILE.unlink(missing_ok=True)
            _proc = None
            return status()

        own = _read_pidfile()
        if not (pid == own or (_proc is not None and _proc.pid == pid)):
            # 后台重启过一次，`Popen` 句柄就没了，pid 文件也可能被清掉——这时候
            # 端口上那个进程其实还是我们的。看一眼命令行再决定，而不是让用户
            # 每次重启后台之后都得去终端手动 taskkill。
            # 7860 也是 Gradio 的默认端口，别的程序完全可能占着它，所以必须核对。
            if "scoreboard.py" not in _cmdline(pid):
                raise RuntimeError(f"端口 {PORT} 被另一个进程（pid {pid}）占着，"
                                   f"不是评测台——不替你结束它")

        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10, creationflags=_NOCONSOLE)
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"停止失败：{exc}") from exc

        deadline = time.time() + wait_s
        while time.time() < deadline:
            if _port_pid(PORT) is None:
                break
            time.sleep(0.2)

        PIDFILE.unlink(missing_ok=True)
        _proc = None
        logbus.note("评测台已停止", source="eval")
        return status()


def restart(gateway: str = "") -> dict[str, Any]:
    # 停不掉（比如端口被外部进程占着）就别硬启：起第二个只会在端口上打架
    stop()
    return start(gateway=gateway)
