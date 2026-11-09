# -*- coding: utf-8 -*-
"""开机自启（Windows 注册表 Run 项）

为什么单独成模块：
  原来 main.py 里有 set_auto_start() / is_auto_start_enabled()，
  但两者的写入路径不一致（一个删值、一个只看键存不存在），
  而且没有任何地方调用 —— 配置项 auto_start_enabled 形同虚设。
  这里统一成一对读写函数，并让它们在失败时如实返回 False，
  不静默吞掉错误（用户改开关却不起作用最难受）。

注册表位置：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
用 HKCU 而不是 HKLM：不需要管理员权限，只影响当前用户。
"""
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ZeroFall"


def _launch_command() -> str:
    """返回开机时要执行的命令行

    打包后直接用 exe 本身；源码运行时用 python + main.py，
    这样开发期也能测出效果，不必先打包。
    """
    if getattr(sys, 'frozen', False):
        return '"{}"'.format(sys.executable)
    main_py = Path(__file__).resolve().parent.parent / 'main.py'
    exe = Path(sys.executable)
    pythonw = exe.parent / 'pythonw.exe'      # 不要弹黑框
    interp = pythonw if pythonw.exists() else exe
    return '"{}" "{}"'.format(interp, main_py)


def is_enabled() -> bool:
    """当前是否已登记开机自启（且指向可用的可执行文件）"""
    if sys.platform != 'win32':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def current_value() -> str:
    """读取已登记的启动命令（便于排查指向错误的情况）"""
    if sys.platform != 'win32':
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return str(value or '')
    except Exception:
        return ''


def set_enabled(enabled: bool) -> bool:
    """开启或关闭开机自启，返回是否真的成功

    幂等：重复开启只是覆盖同一个值，重复关闭时值不存在也算成功。
    """
    if sys.platform != 'win32':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_ALL_ACCESS) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ,
                                  _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def apply_setting(enabled: bool) -> bool:
    """按目标状态同步注册表；已经是目标状态就不动它

    避免每次保存设置都无谓改一遍注册表 —— 频繁写会触发安全软件关注。
    """
    if is_enabled() == bool(enabled):
        return True
    return set_enabled(bool(enabled))
