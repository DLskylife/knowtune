"""系统原生的「选择文件夹」窗口。

**为什么必须由服务端弹，不能让浏览器弹**：浏览器出于安全永远不把真实路径交给
页面。`<input webkitdirectory>` 只给相对路径，`showDirectoryPicker()` 的
FileSystemDirectoryHandle 也只有目录名——是 `D:/models` 还是
`C:/Users/sky/models`，页面永远不知道。可后端要照着路径去扫盘，非绝对路径
不可。于是只能让本机进程去弹一个系统对话框：这是唯一能拿到 `D:\\models` 的
合法途径，也正好是用户心里"Windows 该有的那个窗口"。

三个平台各走各的原生实现，不引入任何依赖：

- **Windows**：`IFileOpenDialog` + `FOS_PICKFOLDERS`，就是资源管理器里右键
  「选择文件夹」弹出的那个窗口（Win10/11 的新式样式，左侧导航、地址栏、
  可新建文件夹）。接口定义得靠 `Add-Type` 现场编译 C#：Python 标准库没有
  COM 绑定，装 pywin32 只为弹个窗不值。编译不出来（ConstrainedLanguage、
  被安全软件拦）就退回 `Shell.Application.BrowseForFolder`——样式老一点，
  但一定弹得出来。
- **macOS**：`osascript` 的 `choose folder`。
- **Linux**：`zenity` / `kdialog`，桌面环境通常自带；都没有就报错，让前端
  退回"手动粘贴路径"，不至于把人卡死在死路上。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys

# 用户在窗口里翻目录能翻很久，但也不能无限挂着：一次请求占一个线程，
# 挂到明天早上就是泄漏。5 分钟还没选完，基本等于人已经走了。
TIMEOUT_S = 300

# Windows 的窗口句柄是 IntPtr，python 侧不用管，全部在 C# 里处理。


class PickError(RuntimeError):
    """弹不出窗口：没装依赖、环境不允许弹（服务跑在无桌面的会话里）等。"""


def pick(initial: str = "", title: str = "选择文件夹",
         timeout: float = TIMEOUT_S) -> str:
    """弹窗让用户选一个目录。

    返回值：选中的绝对路径；用户取消返回空串。
    弹不出来抛 `PickError`（调用方据此提示"手动粘贴路径"）。
    """
    start = (initial or "").strip()
    if start and not os.path.isdir(start):
        start = ""
    if sys.platform.startswith("win"):
        return _pick_windows(title, timeout)
    if sys.platform == "darwin":
        return _pick_macos(start, title, timeout)
    return _pick_linux(start, title, timeout)


def _decode(raw: bytes) -> str:
    # 路径可能带中文，PowerShell/zenity 的 stdout 按 UTF-8 出来；
    # 万一某个环境吐的是本地代码页，replace 也不至于把整行吞掉。
    return raw.decode("utf-8", "replace").strip()


def _run(cmd: list[str], timeout: float, what: str) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise PickError(f"{what}没找到，无法弹出选择窗口") from exc
    except subprocess.TimeoutExpired as exc:
        raise PickError("选择窗口等了太久，已关掉") from exc
    if proc.returncode != 0:
        msg = _decode(proc.stderr) or _decode(proc.stdout)
        raise PickError(msg.splitlines()[-1] if msg else f"{what}退出码 {proc.returncode}")
    return _decode(proc.stdout)


# --------------------------------------------------------------- Windows

# C# 现场编译的接口定义。`IFileDialog` 的 vtable 顺序不能少一个方法——COM 是
# 按序号调用的，漏一个 `SetFileName` 就会把后面的参数全错位，症状是点了
# 「打开」直接崩，极难查。这里把 IFileDialog 全量列出，IFileOpenDialog 追加
# 自己那两个。
_WIN_CS = r"""
using System;
using System.Runtime.InteropServices;

public static class KtPick
{
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    private class FileOpenDialogRCW { }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }

    [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileOpenDialog
    {
        [PreserveSig] int Show(IntPtr hwndParent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName(out IntPtr pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
        void GetResults(out IntPtr ppenum);
        void GetSelectedItems(out IntPtr ppsai);
    }

    // Returns the absolute path, or null when the user cancels.
    public static string Pick(string title)
    {
        IFileOpenDialog dlg = (IFileOpenDialog)new FileOpenDialogRCW();
        // FOS_PICKFOLDERS: 选目录而不是选文件；FOS_FORCEFILESYSTEM: 只认真实
        // 磁盘路径（否则能选中「此电脑」，拿回来的是空串）；PATHMUSTEXIST:
        // 不让手敲一个不存在的路径。
        dlg.SetOptions(0x20u | 0x40u | 0x800u);
        if (!string.IsNullOrEmpty(title)) dlg.SetTitle(title);
        // 初始目录不在这里设：SetFolder 要一个 IShellItem，而
        // SHCreateItemFromParsingName 在这套 P/Invoke 声明下稳定抛
        // ArgumentException（试过 out 参数、返回值、LPStruct、ref Guid 四种
        // marshaling，全在同一个点上炸）。好在不必设——不调用 SetClientGuid
        // 时，这个对话框的位置由系统按 CLSID 记住，用户第二次打开就停在上次
        // 确认过的目录，效果一样，还少一层能出错的东西。
        // 把当前前台窗口（多半是浏览器）当 owner：对话框才会浮在它上面，
        // 而不是弹到断掉的后台去、让人以为按钮没反应。
        int hr = dlg.Show(GetForegroundWindow());
        if (hr < 0) return null;    // 0x800704C7 = 用户取消
        IShellItem item;
        dlg.GetResult(out item);
        if (item == null) return null;
        IntPtr ptr;
        item.GetDisplayName(0x80058000, out ptr);   // SIGDN_FILESYSPATH
        try { return Marshal.PtrToStringUni(ptr); }
        finally { Marshal.FreeCoTaskMem(ptr); }
    }
}
"""


def _b64(text: str) -> str:
    return base64.b64encode((text or "").encode("utf-8")).decode("ascii")


def _windows_script(title: str) -> str:
    # 标题以 base64 传进去：里面有中文、空格、引号、`$`，直接拼进脚本迟早被
    # PowerShell 解释成变量或转义符。
    return f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$title = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(title)}'))
$code = @'
{_WIN_CS}
'@
$picked = $null
try {{
    Add-Type -TypeDefinition $code -Language CSharp
    $picked = [KtPick]::Pick($title)
}} catch {{
    # 编译不出来（受限语言模式 / 安全软件拦截）就走老接口，别让人卡在这儿。
    try {{
        $shell = New-Object -ComObject Shell.Application
        $folder = $shell.BrowseForFolder(0, $title, 0x51, 0)
        if ($folder -ne $null) {{ $picked = $folder.Self.Path }}
    }} catch {{
        Write-Output ('KT_ERR:' + $_.Exception.Message)
        exit 1
    }}
}}
if ($picked) {{ Write-Output ('KT_DIR:' + $picked) }}
else {{ Write-Output 'KT_CANCEL' }}
"""


def _pick_windows(title: str, timeout: float) -> str:
    # initial 故意不接收：Windows 的对话框自己记住上次位置，见 _WIN_CS 里的说明。
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise PickError("这台机器上没有 PowerShell，无法弹出选择窗口")
    script = _windows_script(title)
    # -EncodedCommand 走 UTF-16 base64：中文标题不会在命令行被代码页搞坏。
    # -STA 是 COM 对话框的硬要求（PowerShell 5.1 默认就是 STA，但 pwsh 7 装了
    # 几遍之后不保证）。
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    out = _run([exe, "-NoProfile", "-STA", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text",
                "-EncodedCommand", encoded], timeout, "PowerShell")
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("KT_DIR:"):
            path = line[len("KT_DIR:"):].strip().strip('"')
            # 有些环境会把 \\?\ 前缀带出来，扫盘时它是能用的，但显示很丑。
            if path.startswith("\\\\?\\"):
                path = path[4:]
            return path
        if line.startswith("KT_ERR:"):
            raise PickError(line[len("KT_ERR:"):].strip() or "选择窗口打不开")
        if line == "KT_CANCEL":
            return ""
    raise PickError("选择窗口没有返回结果")


# ----------------------------------------------------------------- macOS


def _pick_macos(initial: str, title: str, timeout: float) -> str:
    script = f'POSIX path of (choose folder with prompt "{title}"'
    if initial:
        script += f' default location POSIX file "{initial}"'
    script += ")"
    try:
        out = _run(["osascript", "-e", script], timeout, "osascript")
    except PickError as exc:
        # 用户按取消时 osascript 退出码非 0，报的是 -128，不该当成故障。
        if "-128" in str(exc) or "User canceled" in str(exc):
            return ""
        raise
    return out.rstrip("/") or "/"


# ----------------------------------------------------------------- Linux


def _pick_linux(initial: str, title: str, timeout: float) -> str:
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if initial:
            cmd.append(f"--filename={initial.rstrip('/')}/")
        try:
            return _run(cmd, timeout, "zenity")
        except PickError as exc:
            if "cancel" in str(exc).lower() or "取消" in str(exc):
                return ""
            raise
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", initial or os.path.expanduser("~"),
               "--title", title]
        try:
            return _run(cmd, timeout, "kdialog")
        except PickError as exc:
            if "cancel" in str(exc).lower():
                return ""
            raise
    raise PickError("这台机器没有 zenity 或 kdialog，弹不出选择窗口：手动填路径吧")
