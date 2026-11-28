"""把前台服务和诊断绑在同一进程生命周期里跑，避免后台服务被工具回收。

用法：python tools/run_diag.py tools/diag_probe.py
"""
import runpy
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8800/"

target = sys.argv[1] if len(sys.argv) > 1 else "tools/diag_probe.py"
log = (ROOT / "_serve.log").open("w", encoding="utf-8")
proc = subprocess.Popen(
    [str(ROOT / ".venv/Scripts/knowtune.exe"), "serve", "--port", "8013"],
    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
)
try:
    for _ in range(180):
        try:
            with urllib.request.urlopen(BASE, timeout=2) as r:
                if r.status == 200:
                    print("端口就绪，等待知识库预热", flush=True)
                    break
        except Exception:
            time.sleep(1)
    else:
        print("服务 180 秒未就绪", flush=True)
        sys.exit(1)
    # 端口 200 只说明 HTTP 起来了。嵌入模型 + 向量矩阵 + 重排模型还要 40~70 秒，
    # 这期间 /api/ask 会回 "知识库还没准备好"。不等就直接测，测到的全是假失败。
    time.sleep(75)
    print("知识库预热完成", flush=True)
    sys.argv = [target]
    runpy.run_path(str(ROOT / target), run_name="__main__")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
