# -*- coding: utf-8 -*-
"""图标加载：统一处理"图标在哪、用哪个尺寸"

为什么单独抽出来：ZeroFall 有 4 个地方要显示图标，各自的失败方式不一样，
散着写就会出现"任务栏新了、托盘还是老的"这种半修状态。

  1. 窗口图标     Qt 的 setWindowIcon   → 影响标题栏
  2. 任务栏按钮   Windows 取窗口图标 / exe 资源图标 → 由 AppUserModelID 决定缓存
  3. 托盘图标     Win32 LoadImageW + HICON → 路径找不到就退回系统默认图标
  4. exe 本身     --icon 打进 PE 资源  → 资源管理器、开始菜单

**图标的三层回退**（这是"换台电脑也能显示"的关键）：
  A. `sys._MEIPASS/ZeroFall.ico`  —— PyInstaller onefile 解压出来的外部文件
     依赖 `--add-data`，参数漏了就没有
  B. `ExtractIconExW(sys.executable)` —— 直接从 exe 的 PE 资源里取
     只要打包时带了 `--icon`，这个一定有
  C. 系统默认应用图标 —— 兜底，保证不崩

只写 A 的话，打包命令改一下静默变白图标；加上 B 才真正可靠。
"""
import ctypes
import sys
from ctypes import wintypes as wt
from pathlib import Path

ICON_NAME = "ZeroFall.ico"

# Win32 常量
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040
IDI_APPLICATION = 32512
LOGPIXELSX = 88


def icon_file_path() -> Path:
    """ico 文件在哪（不保证存在）

    frozen（PyInstaller onefile）：文件被解压到 sys._MEIPASS
    源码运行：项目根目录
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / ICON_NAME
    # 源码运行：本文件在 <项目>/modules/ 下
    return Path(__file__).resolve().parent.parent / ICON_NAME


def icon_file_exists() -> bool:
    try:
        return icon_file_path().is_file()
    except Exception:
        return False


def tray_icon_size() -> int:
    """托盘图标该用多少像素

    Windows 托盘基础尺寸是 16px，但系统 DPI 放大后实际需要
    16 * (dpi/96) 像素。写死 16 在 150% 缩放下会明显糊，
    写死 32 在 100% 缩放下会被缩小（还行但不算最优）。

    返回 16 / 20 / 24 / 32 / 40 / 48 之一。
    """
    if sys.platform != "win32":
        return 16
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        if not hdc:
            return 16
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX) or 96
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        scaled = int(round(16 * dpi / 96.0))
        # 归到 ico 里实际有的尺寸，避免 LoadImageW 缩放失真
        for candidate in (16, 20, 24, 32, 40, 48, 64):
            if scaled <= candidate:
                return candidate
        return 64
    except Exception:
        return 16


def load_tray_hicon():
    """加载托盘用的 HICON，三层回退

    Returns:
        (hicon, source) —— source 是 'file' / 'exe' / 'default' / 'none'
        调用方应该把 source 写进日志，出问题时一眼能看出走了哪条路。
    """
    if sys.platform != "win32":
        return 0, "none"

    user32 = ctypes.windll.user32
    size = tray_icon_size()

    # 关键：x64 下句柄是 64 位，不设 restype 会被截成 32 位
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                  ctypes.c_int, ctypes.c_int, wt.UINT]

    # A. 独立 ico 文件
    try:
        p = icon_file_path()
        if p.is_file():
            h = user32.LoadImageW(None, str(p), IMAGE_ICON,
                                  size, size, LR_LOADFROMFILE)
            if h:
                return h, "file"
    except Exception:
        pass

    # B. 从 exe 自身资源里抠（--icon 打的图标，任何打包方式都有）
    try:
        shell32 = ctypes.windll.shell32
        hicon_large = wt.HICON()
        hicon_small = wt.HICON()
        shell32.ExtractIconExW.restype = wt.UINT
        shell32.ExtractIconExW.argtypes = [wt.LPCWSTR, ctypes.c_int,
                                           ctypes.POINTER(wt.HICON),
                                           ctypes.POINTER(wt.HICON), wt.UINT]
        n = shell32.ExtractIconExW(str(sys.executable), -1,
                                   ctypes.byref(hicon_large),
                                   ctypes.byref(hicon_small), 1)
        if n > 0:
            # 托盘用小尺寸那个更贴切
            h = hicon_small.value or hicon_large.value
            if h:
                return h, "exe"
    except Exception:
        pass

    # C. 系统默认
    try:
        user32.LoadIconW.restype = wt.HANDLE
        user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
        h = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        if h:
            return h, "default"
    except Exception:
        pass

    return 0, "none"


def describe() -> str:
    """一行诊断信息，便于在别的电脑上排查"图标没出来"

    用法：拿到用户反馈时让他看这行，就能判断是 add-data 漏了、
    还是 exe 资源没打进去。
    """
    p = icon_file_path()
    parts = [
        "frozen={}".format(bool(getattr(sys, "_MEIPASS", None))),
        "ico={} ({})".format(p, "存在" if p.is_file() else "缺失"),
        "tray_size={}px".format(tray_icon_size()),
    ]
    try:
        parts.append("exe={}".format(sys.executable))
    except Exception:
        pass
    return " | ".join(parts)
