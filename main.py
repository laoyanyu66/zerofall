#!/usr/bin/env python3
"""
ZeroFall - DeepSeek前置守护系统
主程序入口
"""
import sys
import os
import time
import ctypes
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPalette, QColor

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))


def _app_id_for_this_exe() -> str:
    """按 exe 绝对路径生成 AppUserModelID

    为什么要按路径区分：master 和 agent 是同一个 exe 的两个副本，
    如果共用同一个 AppUserModelID，Windows 会把它们当成同一个应用 ——
    任务栏按钮合并、共享图标缓存，用户根本分不清哪个是哪个。
    按路径哈希后：不同目录 = 不同应用；同一位置重装 = ID 不变（缓存正常）。
    """
    import hashlib
    try:
        key = str(Path(sys.executable).resolve()).lower()
    except Exception:
        key = "zerofall"
    return "ZeroFall.Guardian." + hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def _set_app_user_model_id(app_id: str = None):
    """告诉 Windows"我是一个独立应用"，而不是 python.exe 的一个实例

    不做这件事会有三个连带问题（都是这个项目真实踩过的）：
      1. 任务栏按 exe 路径缓存图标 —— 换了 exe 新图标，任务栏却一直显示旧的，
         因为 Shell 认为"这个程序没变"
      2. 任务栏按钮和 python.exe 归成一组，点一个全跳出来
      3. 任务栏右键菜单漏掉应用的项（AppUserModelID 决定了 JumpList）

    必须在创建 QApplication 之前调用，否则窗口已经按旧 ID 注册过了。
    """
    if sys.platform != 'win32':
        return
    if not app_id:
        app_id = _app_id_for_this_exe()
    try:
        import ctypes as _ct
        _ct.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as e:
        # 失败不影响功能，只是任务栏图标/归组退化
        print("[警告] 设置 AppUserModelID 失败: {}".format(e))


_set_app_user_model_id()

from config import ConfigManager
from logger import ZeroFallLogger
from modules.crash_reporter import CrashReporter
from ui.main_window import ZeroFallMainWindow


def is_admin():
    """检查是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def run_as_admin():
    """以管理员权限重新运行"""
    if is_admin():
        return True
    
    try:
        # PyInstaller打包后，sys.executable就是exe本身
        exe_path = os.path.abspath(sys.argv[0])
        
        # 添加标记参数，防止无限循环
        params = "--already-tried-admin"
        
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe_path, params, None, 1
        )
        
        return ret > 32
    except Exception as e:
        ZeroFallLogger.error(f"请求管理员权限失败: {e}")
        return False


def set_auto_start(enable: bool):
    """设置/取消开机自启"""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE)
        exe_path = os.path.abspath(sys.argv[0])
        if enable:
            winreg.SetValueEx(key, "ZeroFall", 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, "ZeroFall")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"设置开机自启失败: {e}")


def is_auto_start_enabled() -> bool:
    """检查是否已设置开机自启"""
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, "ZeroFall")
        winreg.CloseKey(key)
        return True
    except:
        return False


# ════════════════════════════════════════════════════════════════════
#  单实例保护
#  模块级全局：必须保住 mutex handle，否则函数返回后被 GC，锁对象销毁 → 失效
# ════════════════════════════════════════════════════════════════════
_instance_mutex = None
ZEROFALL_WINDOW_TITLE = "ZeroFall - DeepSeek前置守护系统"


def _instance_scope() -> str:
    """单实例作用域：按 exe 绝对路径区分

    为什么必须区分：master 和 agent 是同一个 exe 的两个副本，
    放在不同目录。如果 Mutex 名固定，第二个实例会被判定为"已在运行"
    直接退出 —— 同一台电脑上就永远没法同时测主控和子控。

    按 exe 路径哈希后：
      · 同一份程序重复启动 → 同名 Mutex → 被挡住（单实例保护的本意）
      · master 与 agent 在不同目录 → 不同名 Mutex → 可以并存
    """
    import hashlib
    try:
        key = str(Path(sys.executable).resolve()).lower()
    except Exception:
        key = "zerofall"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:10]


ZEROFALL_MUTEX_NAME = "ZeroFall_Single_Instance_Mutex_v3_" + _instance_scope()


def _find_existing_window() -> int:
    """查找已存在的 ZeroFall 主窗口，返回 hwnd（0 表示没有）"""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = user32.FindWindowW(None, ZEROFALL_WINDOW_TITLE)
        return int(hwnd) if hwnd else 0
    except Exception:
        return 0


def _activate_window(hwnd: int):
    """把已存在的窗口恢复到前台"""
    if not hwnd:
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def check_single_instance() -> bool:
    """检查是否已有实例在运行。
    
    返回 True  = 本进程是唯一实例，继续启动；
    返回 False = 已有实例，调用方必须立即退出。
    
    判据是命名 Mutex（按 exe 路径作用域）：
      · 同名 Mutex 已被持有 → 已有同作用域实例 → 激活它的窗口并退出
      · 拿到锁 → 本进程是唯一实例

    窗口查找只在"判定重复"之后用来激活已有窗口，不作为判据 ——
    因为 master 和 agent 的窗口标题完全相同。
    """
    global _instance_mutex
    
    # ── 判据：命名 Mutex 优先（按 exe 路径作用域）──
    # 窗口查找不能当判据：master 和 agent 的窗口标题一样，
    # 先查窗口会把另一个模式的实例误认成"自己已在跑"。
    
    # ── 判据：命名 Mutex（跨进程、跨会话都有效）──
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, True, ZEROFALL_MUTEX_NAME)
        err = ctypes.get_last_error()
        if err == 183:      # ERROR_ALREADY_EXISTS：同作用域的另一个实例持有这个锁
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
            _activate_window(_find_existing_window())
            return False
        # 锁到手：把 handle 存进模块级全局，整个进程生命周期都不释放
        _instance_mutex = handle
        return True
    except Exception:
        # 极端情况下取不到锁（权限异常等），放行以免完全无法启动
        return True


def cleanup_old_logs():
    """清理旧日志：常规日志保留 7 天，崩溃报告保留 30 天"""
    try:
        import time as _time
        from pathlib import Path as _P
        
        if getattr(sys, 'frozen', False):
            base = _P(sys.executable).parent / "logs"
        else:
            base = _P(__file__).parent / "logs"
        if not base.exists():
            return
        
        now = _time.time()
        removed = 0
        
        for f in base.glob("zerofall_*.log*"):
            try:
                if now - f.stat().st_mtime > 7 * 86400:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        
        crash_dir = base / "crashes"
        if crash_dir.exists():
            for f in crash_dir.glob("*.log"):
                try:
                    if now - f.stat().st_mtime > 30 * 86400:
                        f.unlink()
                        removed += 1
                except Exception:
                    pass
        
        if removed:
            ZeroFallLogger.info("已清理 {} 个过期日志文件".format(removed))
    except Exception:
        pass


def _install_error_reporter():
    """安装全局异常钩子：子控角色下把未捕获异常上报给主控

    主控收到后会拉起 DSH 会话自动修复并下发新版本。
    """
    import traceback as _tb

    def _hook(exc_type, exc_value, exc_tb):
        try:
            ZeroFallLogger.error(
                "未捕获异常: {}".format(''.join(_tb.format_exception(exc_type, exc_value, exc_tb))[-2000:]))
        except Exception:
            pass
        # 上报（只读配置，不启动完整 GUI）
        try:
            from config import ConfigManager
            cfg = ConfigManager().config
            if getattr(cfg, 'role', 'master') == 'agent' and getattr(cfg, 'report_to_master', True) \
                    and getattr(cfg, 'report_zf_error', True):
                from modules.control import AgentNode
                node = AgentNode(cfg)
                node.report_zf_error(
                    '{}: {}'.format(exc_type.__name__, exc_value),
                    context={'traceback': ''.join(_tb.format_exception(exc_type, exc_value, exc_tb))[-4000:]})
        except Exception:
            pass
        # 保留默认输出
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main():
    """主函数"""
    # 检查是否已有实例运行：有则激活已有窗口并退出
    if not check_single_instance():
        # 此时日志引擎可能尚未初始化，用 stderr 记录（无窗口环境下也能留痕）
        try:
            sys.stderr.write("[ZeroFall] 已有实例在运行，本进程退出\n")
        except Exception:
            pass
        sys.exit(0)
    
    # 检查是否已经尝试过提权（通过命令行参数）
    already_tried = "--already-tried-admin" in sys.argv
    
    # 初始化日志
    ZeroFallLogger()
    ZeroFallLogger.info("=" * 60)
    ZeroFallLogger.info("ZeroFall 启动")
    ZeroFallLogger.info("=" * 60)
    
    # 清理过期日志
    cleanup_old_logs()
    
    # 安装崩溃捕获钩子
    CrashReporter.install()
    
    # 设置开机自启
    set_auto_start(True)
    
    # 加载配置
    config_manager = ConfigManager()
    config = config_manager.load()
    
    # 检查管理员权限（只在未尝试过时才请求）
    if config.run_as_admin and not is_admin() and not already_tried:
        ZeroFallLogger.info("请求管理员权限...")
        if run_as_admin():
            sys.exit(0)
        else:
            ZeroFallLogger.warning("无法获取管理员权限，将以普通权限运行")
    
    # 创建Qt应用
    app = QApplication(sys.argv)

    # 应用级图标：任务栏、Alt+Tab、所有对话框都从它取
    try:
        from PyQt6.QtGui import QIcon as _QIcon
        _ico = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ZeroFall.ico"
        if _ico.exists():
            app.setWindowIcon(_QIcon(str(_ico)))
            print("[启动] 应用图标: {}".format(_ico))
        else:
            print("[警告] 找不到图标文件: {}".format(_ico))
    except Exception as _e:
        print("[警告] 设置应用图标失败: {}".format(_e))

    app.setApplicationName("ZeroFall")
    app.setApplicationVersion("1.5.1")
    
    # 全局异常钩子：子控角色下自动上报给主控
    _install_error_reporter()
    
    # 关键：有托盘图标常驻，关闭所有窗口不应退出程序
    # （否则自检对话框关闭瞬间主窗口未显示，Qt 会直接结束进程）
    app.setQuitOnLastWindowClosed(False)
    
    # 设置应用样式
    app.setStyle("Fusion")
    
    # 设置深色主题
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    app.setPalette(palette)

    # 统一设计系统：一次注入，全窗口生效（取代各处零散的内联颜色）
    try:
        from ui import theme as _theme
        app.setStyleSheet(_theme.qss())
    except Exception as _e:
        print('[主题] 加载失败，回退默认样式: {}'.format(_e))
    
    # 导入必要的模块
    from modules.snapshot_manager import SnapshotManager
    from modules.environment_checker import EnvironmentChecker
    from modules.guardian import Guardian
    from modules.baidu_pan import BaiduPanClient
    from ui.main_window import ZeroFallMainWindow
    from ui.loading_overlay import SelfCheckDialog
    
    # 初始化核心模块
    snapshot_manager = SnapshotManager(config)
    env_checker = EnvironmentChecker(config, snapshot_manager)
    guardian = Guardian(config, env_checker, snapshot_manager)
    baidu_pan = BaiduPanClient(config) if config.baidu_pan_enabled else None
    
    # 显示自检对话框
    check_dialog = SelfCheckDialog(config, snapshot_manager, env_checker, guardian, baidu_pan)
    check_dialog.show()
    check_dialog.raise_()
    
    # 执行自检
    QTimer.singleShot(100, check_dialog.run_checks)
    
    # 主窗口延迟到自检结束后再创建：避免构造期间（0.5~1s）阻塞事件循环，
    # 导致自检进度条长时间停在 0% 不刷新
    _holder = {"window": None, "shown": False}
    
    def show_main_window():
        if _holder["shown"]:
            return
        _holder["shown"] = True
        try:
            all_ok = check_dialog.all_passed
            has_failed = check_dialog.has_failed
            ZeroFallLogger.info(
                "[启动] 自检结论: 全绿={} 有失败={} | 进度={}/{}".format(
                    all_ok, has_failed,
                    check_dialog.done_count, check_dialog.total_count
                )
            )
            
            ZeroFallLogger.info("[启动] 准备显示主窗口")
            check_dialog.close()
            ZeroFallLogger.info("[启动] 自检对话框已关闭")
            
            if _holder["window"] is None:
                _t0 = time.time()
                ZeroFallLogger.info("[启动] 正在创建主窗口...")
                _holder["window"] = ZeroFallMainWindow(config_manager)
                ZeroFallLogger.info(
                    "[启动] 主窗口创建完成 {:.2f}s".format(time.time() - _t0)
                )
            _holder["window"].show()
            ZeroFallLogger.info("[启动] 主窗口已显示")
            
            # 自检全绿 → 自动启动 DeepSeek
            if all_ok and config.auto_launch_deepseek:
                ZeroFallLogger.info("[自动] 自检全部通过，2 秒后自动启动 DeepSeek")
                QTimer.singleShot(2000, _holder["window"].auto_launch_deepseek)
            elif not all_ok:
                ZeroFallLogger.info("[自动] 自检未全绿，不自动启动 DeepSeek")
        except Exception as _e:
            import traceback
            ZeroFallLogger.error("[启动] 显示主窗口失败: {}".format(_e))
            ZeroFallLogger.error(traceback.format_exc())
    
    # 主路径：自检真正完成时才显示主窗口（避免"面板先出、自检卡住"）
    check_dialog.all_done.connect(lambda _ok: show_main_window())
    # 兜底：万一自检异常卡死，15 秒后强制显示，不让用户干等
    QTimer.singleShot(15000, show_main_window)
    
    # 运行应用
    exit_code = app.exec()
    
    ZeroFallLogger.info("ZeroFall 退出")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
