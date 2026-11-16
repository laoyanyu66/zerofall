# -*- coding: utf-8 -*-
"""豆包接力 —— dsh 崩溃、且选择「保留现场」时，把现场交给豆包分析

为什么要有它：崩溃后 ZeroFall 默认会回退快照把 DSH 拉起来，但那会把现场冲掉。
一旦选择「不回退、保留现场」，人就得自己去翻日志、自己想原因。这个模块把这一步
自动化：拉起豆包 → 新建对话 → 把崩溃现场（退出码、崩溃日志路径、最近的崩溃尾部）
拼成一句话发过去，让豆包从根因分析开始做。

设计约束（都是踩过坑的地方）：
  · 只做**本机 GUI 接力**：不碰豆包的任何私有接口/账号态，失败就退化成"窗口拉起来、
    提示词放剪贴板"，由人按 Ctrl+V；
  · 前台窗口有系统限制，激活前先 ShowWindow + AttachThreadInput，否则 SetForegroundWindow
    经常只是任务栏闪一下；
  · 新对话不做假设：先尝试 Ctrl+N，再粘贴 + 回车；两条都失败也有兜底提示；
  · 整个过程可 dry_run（不按键、不写剪贴板），便于测试与"只想看看会发什么"。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# 64 位下必须显式声明签名：不声明时 ctypes 默认 restype=c_int，
# HGLOBAL/HANDLE 会被截成 32 位，GlobalLock 立刻访问冲突（0xC0000005）。
_HAVE_WIN32 = os.name == "nt"
if _HAVE_WIN32:
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.GetForegroundWindow.restype = wt.HWND
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    user32.EmptyClipboard.restype = wt.BOOL
    user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = wt.BOOL
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = wt.BOOL
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

# ── 默认文案：现场信息会拼在后面 ────────────────────────────────────────────

DEFAULT_PROMPT = (
    "dsh又崩溃了，帮我分析一下是什么原因，并帮我修复优化一下。"
    "\n\n崩溃现场已经保留（ZeroFall 没有回退快照），下面是定位线索："
    "\n- 退出码：{exit_code}"
    "\n- 崩溃日志：{crash_log}"
    "\n- 崩溃日志目录：{crash_dir}"
    "\n- DSH profile：{profile}"
    "\n\n请先读这些日志和会话状态，给出根因判断和最小改动的修复步骤；"
    "需要我提供额外信息的地方直接问。"
)

#: 常见安装位置（豆包默认装在 D:\Doubao，但换机器不一定）
_EXE_CANDIDATES = (
    Path(r"D:\Doubao\app\Doubao.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Doubao" / "Doubao.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Doubao" / "Doubao.exe",
    Path(os.environ.get("ProgramFiles", "")) / "Doubao" / "Doubao.exe",
    Path(os.environ.get("ProgramFiles(x86)", "")) / "Doubao" / "Doubao.exe",
)

_WINDOW_TITLE_HINTS = ("豆包", "Doubao", "doubao")
_PROCESS_HINT = "doubao"


# ── 进程 / 窗口 ────────────────────────────────────────────────────────────


def find_exe(configured: str = "") -> Optional[Path]:
    """找豆包可执行文件：配置优先，其次常见安装位置，最后读开始菜单快捷方式。"""
    if configured:
        p = Path(configured)
        if p.is_file():
            return p
    for cand in _EXE_CANDIDATES:
        try:
            if cand and cand.is_file():
                return cand
        except OSError:
            continue
    lnk = _resolve_start_menu_shortcut()
    if lnk and lnk.is_file():
        return lnk
    return None


def _resolve_start_menu_shortcut() -> Optional[Path]:
    """从开始菜单/桌面的 豆包.lnk 里读真实目标。"""
    candidates: List[Path] = []
    for base in (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ):
        try:
            if base.is_dir():
                candidates.extend(base.rglob("豆包.lnk"))
                candidates.extend(base.rglob("Doubao.lnk"))
        except OSError:
            continue
    for lnk in candidates:
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}'); $s.TargetPath"],
                capture_output=True, text=True, encoding="mbcs", errors="replace",
                timeout=15,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            # 中文 Windows 上快捷方式路径是 ANSI 代码页：按 UTF-8 解码会让 stdout
            # 变成 None，下一行 .strip() 直接 AttributeError（2026-11-12 自检发现）。
            target = (out.stdout or "").strip().strip('"')
            if target and Path(target).is_file():
                return Path(target)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _window_pid(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_image(pid: int) -> str:
    """拿进程镜像路径（用来把窗口和 Doubao.exe 对上，别抓错窗口）。"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def find_main_window() -> Optional[int]:
    """找豆包的可见主窗口（进程镜像含 doubao，标题非空）。"""
    if not _HAVE_WIN32:
        return None
    found: List[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _enum(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if not title:
                return True
            image = _process_image(_window_pid(hwnd)).lower()
            if _PROCESS_HINT in image or any(h in title for h in _WINDOW_TITLE_HINTS):
                found.append(int(hwnd))
        except Exception:  # noqa: BLE001 - 枚举回调不能抛
            pass
        return True

    callback = _enum  # 必须持有引用，否则枚举途中被 GC → 访问冲突
    user32.EnumWindows(callback, 0)
    if not found:
        return None
    # 优先带"豆包/Doubao"标题的
    for hwnd in found:
        title = _window_title(hwnd)
        if any(h.lower() in title.lower() for h in _WINDOW_TITLE_HINTS):
            return hwnd
    return found[0]


def is_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Doubao.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000,  # CREATE_NO_WINDOW：tasklist 也会弹黑框
        )
        return "doubao.exe" in out.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return find_main_window() is not None


def activate(hwnd: int) -> bool:
    """把窗口提到前台。前台锁存在时用 AttachThreadInput 硬切一次。"""
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        target_tid = _window_pid(hwnd)
        cur_tid = _window_pid(fg) if fg else 0
        if cur_tid and target_tid and cur_tid != target_tid:
            user32.AttachThreadInput(cur_tid, target_tid, True)
            try:
                user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(cur_tid, target_tid, False)
        else:
            user32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return user32.GetForegroundWindow() == hwnd
    except OSError:
        return False


# ── 剪贴板 / 按键 ───────────────────────────────────────────────────────────


def set_clipboard(text: str) -> bool:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    try:
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
        finally:
            user32.CloseClipboard()
        return True
    except OSError:
        return False


def get_clipboard_text() -> str:
    """读回剪贴板文本（调试/测试用；也用于接力前保存原内容）。"""
    CF_UNICODETEXT = 13
    try:
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.c_wchar_p(ptr).value or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except OSError:
        return ""


def _key(vk: int, down: bool):
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0)


def _chord(modifier_vk: int, key_vk: int):
    _key(modifier_vk, True)
    time.sleep(0.03)
    _key(key_vk, True)
    time.sleep(0.03)
    _key(key_vk, False)
    _key(modifier_vk, False)
    time.sleep(0.1)


def send_new_chat():
    """Ctrl+N（豆包/多数 Electron 客户端的"新对话"）：拿不到也无所谓。"""
    _chord(0x11, 0x4E)  # Ctrl + N


def send_paste():
    _chord(0x11, 0x56)  # Ctrl + V


def send_enter():
    _key(0x0D, True)
    time.sleep(0.03)
    _key(0x0D, False)


# ── 主流程 ─────────────────────────────────────────────────────────────────


def build_prompt(exit_code: Optional[int] = None, crash_log: str = "",
                 crash_dir: str = "", profile: str = "",
                 template: str = "", extra: str = "") -> str:
    tpl = template or DEFAULT_PROMPT
    try:
        text = tpl.format(
            exit_code=exit_code if exit_code is not None else "未知",
            crash_log=crash_log or "（未找到最新的崩溃日志）",
            crash_dir=crash_dir or "（未知）",
            profile=profile or "desktop",
        )
    except (KeyError, IndexError, ValueError):
        # 自定义模板占位符写错时，原样返回，别把接力卡死
        text = tpl
    return text + (extra or "")


def relay(exit_code: Optional[int] = None, crash_log: str = "", crash_dir: str = "",
          profile: str = "desktop", prompt_template: str = "", exe_hint: str = "",
          *, extra: str = "", dry_run: bool = False, launch_wait: float = 6.0,
          send_keys: bool = True, double_enter: bool = True) -> Dict[str, Any]:
    """拉起豆包并把崩溃现场发过去。

    返回 report：{"ok": bool, "steps": [...], "prompt": str, "error": str}
    任何一步失败都不会抛异常 —— 崩溃处理路径上不能因为接力把 ZeroFall 拖崩。
    """
    report: Dict[str, Any] = {"ok": False, "steps": [], "prompt": "", "error": ""}
    prompt = build_prompt(exit_code, crash_log, crash_dir, profile, prompt_template, extra)
    report["prompt"] = prompt

    exe = find_exe(exe_hint)
    running = is_running()

    if dry_run:
        report["steps"].append("dry-run: 跳过启动/激活/按键")
        report["exe"] = str(exe) if exe else ""
        report["running"] = running
        report["ok"] = True
        return report

    if not running:
        if not exe:
            report["error"] = "找不到豆包可执行文件"
            return report
        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            report["steps"].append(f"已启动豆包：{exe}")
            time.sleep(min(max(launch_wait, 1.0), 30.0))
        except OSError as exc:
            report["error"] = f"启动豆包失败：{exc}"
            return report
    else:
        report["steps"].append("豆包已在运行")

    hwnd = find_main_window()
    if not hwnd:
        report["error"] = "没找到豆包主窗口"
        report["prompt_copied"] = set_clipboard(prompt)
        return report
    report["hwnd"] = hwnd

    activated = activate(hwnd)
    report["steps"].append("窗口已激活" if activated else "窗口激活失败（继续尝试按键）")

    if not send_keys:
        report["prompt_copied"] = set_clipboard(prompt)
        report["steps"].append("已把提示词放进剪贴板（send_keys=False）")
        report["ok"] = report["prompt_copied"]
        return report

    if not set_clipboard(prompt):
        report["error"] = "写剪贴板失败"
        return report
    report["steps"].append("提示词已进剪贴板")

    send_new_chat()
    time.sleep(0.6)
    send_paste()
    time.sleep(0.4)
    send_enter()
    if double_enter:
        # 有些客户端第一次回车只是"把焦点挪进输入框/换行"，再补一次更稳；
        # 真发出去之后再回车只会是空输入，不会多发一条消息。
        time.sleep(0.9)
        send_enter()
    report["steps"].append("已发送（Ctrl+N → Ctrl+V → Enter{}）".format(" ×2" if double_enter else ""))
    report["ok"] = True
    return report
