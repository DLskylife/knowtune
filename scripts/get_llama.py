#!/usr/bin/env python
"""下载 llama-server 可执行文件到 tools/llama。

本地模型那块「没找到 llama-server」的唯一出路原本是让用户自己去 GitHub 下
压缩包、解压、加 PATH。国内网络下 github.com 的 release 直连经常 502（实测
本机就是），而 release 的域名和 api.github.com 是两个网络路径，所以「能查
版本号」不等于「能下文件」。这个脚本把三件事做掉：查最新可用 tag、挑对
平台/后端的资产、镜像兜底下载并就地解压。

    python scripts/get_llama.py                 # 自动选：Windows 优先 Vulkan
    python scripts/get_llama.py --backend cpu   # 没有独显 / 想最小体积
    python scripts/get_llama.py --tag b11744    # 钉一个版本
    python scripts/get_llama.py --list          # 只看这个 tag 有哪些资产

只用标准库：这个脚本要在任何一台机器上跑得起来，不该先装依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tools" / "llama"
REPO = "ggml-org/llama.cpp"

# 直连优先，被挡再走镜像。gh-proxy / ghproxy 实测能下（206 断点续传可用），
# ghfast 会 403——留在列表里是因为它偶尔恢复，失败就换下一个，代价只有几秒。
MIRRORS = (
    "https://github.com/{repo}/releases/download/{tag}/{asset}",
    "https://gh-proxy.com/https://github.com/{repo}/releases/download/{tag}/{asset}",
    "https://ghproxy.net/https://github.com/{repo}/releases/download/{tag}/{asset}",
)


def latest_tag() -> str:
    """最近一个「带二进制」的 tag。

    `releases/latest` 现在指向一个只有 nightly-tag.txt 的占位发布，直接用它
    会挑到一个没有资产的 tag，然后报「找不到对应压缩包」——查的是发布，装的
    是二进制，这两个不是一回事。
    """
    url = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
    with urllib.request.urlopen(url, timeout=30) as r:
        rels = json.load(r)
    for rel in rels:
        if any(_is_pkg(a["name"]) for a in rel.get("assets", [])):
            return rel["tag_name"]
    raise SystemExit("最近 20 个发布里都没有压缩包，去 GitHub 页面手动确认版本")


# Windows 发 .zip，macOS/Linux 发 .tar.gz。只认 zip 会让非 Windows 直接报
# 「这个发布里没有包」——而包里其实有，只是后缀不同。
PKG_SUFFIXES = (".zip", ".tar.gz")


def _is_pkg(name: str) -> bool:
    return name.endswith(PKG_SUFFIXES)


def pick_asset(assets: list[dict], backend: str) -> dict:
    sysname = platform.system().lower()
    # Linux 的资产名用的是 ubuntu，不是 linux；写死 linux 会一个都匹配不上。
    os_tags = {"windows": ["win"], "linux": ["ubuntu", "linux"],
               "darwin": ["macos"]}.get(sysname)
    if not os_tags:
        raise SystemExit(f"不认识的系统：{platform.system()}")
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"

    want = [f"bin-{t}-{backend}" for t in os_tags]
    if backend != "cpu":
        want += [f"bin-{t}-cpu" for t in os_tags]   # 指定后端没有就退到 CPU

    for frag in want:
        for a in assets:
            n = a["name"]
            if _is_pkg(n) and frag in n and arch in n and n.startswith("llama-"):
                return a
    names = "\n  ".join(a["name"] for a in assets if _is_pkg(a["name"]))
    raise SystemExit(f"这个发布里没有 {sysname}/{arch}/{backend} 的包。可用：\n  {names}")


def download(url: str, dest: Path, expect: int = 0, tries: int = 4) -> None:
    """下载，断点续传。

    镜像的常见失败不是「连不上」，而是「下到一半断」：本次实测 ghproxy.net
    在 11.1/23.6 MB 处断开，urllib 一声不响地正常返回。它支持 Range，所以
    正确做法不是换个镜子重头来，而是接着下——否则大包（CUDA 版 275MB）
    永远下不完。
    """
    got = dest.stat().st_size if dest.exists() else 0
    last = ""
    for attempt in range(1, tries + 1):
        headers = {"User-Agent": "knowtune-get-llama"}
        if got:
            headers["Range"] = f"bytes={got}-"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=180
            ) as r:
                if got and r.status != 206:      # 不支持续传 → 从头写
                    got = 0
                with dest.open("ab" if got else "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        print(f"\r  {got / 1048576:5.1f} MB", end="")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        print()
        if not expect:
            return
        if got >= expect * 0.98:
            return
        if attempt < tries:
            print(f"  第 {attempt} 次只到 {got / 1048576:.1f}/{expect / 1048576:.1f} MB，续传…")
    raise OSError(f"重试 {tries} 次仍未下完（{got / 1048576:.1f}MB）：{last}")


def looks_like_pkg(path: Path) -> bool:
    """按文件头认：zip 是 PK\\x03\\x04，gzip 是 \\x1f\\x8b。"""
    try:
        head = path.open("rb").read(4)
    except OSError:
        return False
    return head.startswith(b"PK") or head.startswith(b"\x1f\x8b")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="", help="发布 tag；默认取最新的带二进制版本")
    ap.add_argument("--backend", default="vulkan" if os.name == "nt" else "cpu",
                    choices=["vulkan", "cuda", "cpu", "sycl", "rocm", "opencl"],
                    help="Windows 默认 vulkan：单包 35MB 走显卡驱动，不用另下 CUDA 运行时")
    ap.add_argument("--list", action="store_true", help="只列出该版本有哪些包")
    ap.add_argument("--dir", default=str(TARGET), help="解压到哪")
    args = ap.parse_args()

    tag = args.tag or latest_tag()
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"
    with urllib.request.urlopen(url, timeout=30) as r:
        assets = json.load(r).get("assets", [])
    if args.list:
        for a in assets:
            print(f"{a['size'] // 1048576:5d} MB  {a['name']}")
        return 0

    asset = pick_asset(assets, args.backend)
    print(f"{tag} → {asset['name']}（{asset['size'] // 1048576} MB）")

    tmp = Path(os.environ.get("TEMP", "/tmp")) / asset["name"]
    last_err = ""
    for tpl in MIRRORS:
        link = tpl.format(repo=REPO, tag=tag, asset=asset["name"])
        host = link.split("/")[2]
        print(f"  下载（{host}）…")
        try:
            download(link, tmp, asset["size"])
            if not looks_like_pkg(tmp):
                raise OSError("下到的不是压缩包（多半是镜像的错误页）")
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"{host}: {exc}"
            print(f"  失败：{last_err}")
            tmp.unlink(missing_ok=True)
    else:
        raise SystemExit(f"所有下载地址都失败，最后一条：{last_err}")

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    if tmp.name.endswith(".zip"):
        with zipfile.ZipFile(tmp) as z:
            z.extractall(out)
    else:
        import tarfile

        with tarfile.open(tmp) as t:
            # filter="data" 会拒绝绝对路径和 .. —— 从网上下的包不该有写盘自由
            t.extractall(out, filter="data")
    tmp.unlink(missing_ok=True)

    exe = out / ("llama-server.exe" if os.name == "nt" else "llama-server")
    if not exe.exists():
        # 压缩包里有的版本多套一层同名目录
        hit = next(out.rglob(exe.name), None)
        exe = hit or exe
    if not exe.exists():
        raise SystemExit(f"解压完了但没有 {exe.name}，检查 {out}")
    if os.name != "nt":
        exe.chmod(0o755)
    print(f"\n装好了：{exe}")
    print("程序会自动找到它（tools/llama 是 find_server 的兜底路径之一）。")
    print("换个位置也行：设环境变量 KNOWTUNE_LLAMA_SERVER=<完整路径>。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
