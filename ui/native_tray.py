"""
ZeroFall 原生系统托盘图标（安全版）
使用纯ctypes创建消息窗口，避免PyQt6的nativeEvent崩溃问题
"""
import sys
import ctypes
import ctypes.wintypes as wt
import threading
from pathlib import Path

from PyQt6.QtWidgets import QMenu, QApplication
from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtGui import QCursor

sys.path.append(str(Path(__file__).parent.parent))
from logger import ZeroFallLogger

# Windows API 常量
WM_USER = 0x0400
WM_TRAYMESSAGE = WM_USER + 101

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_STATE = 0x00000008
NIF_SHOWTIP = 0x00000080

NIS_FORCEINLINE = 0x00000040

NOTIFYICON_VERSION_4 = 4

WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_DESTROY = 0x0002
WM_NULL = 0x0000

IDI_APPLICATION = 32512
LR_LOADFROMFILE = 0x00000010
IMAGE_ICON = 1

WS_OVERLAPPED = 0x00000000
WS_SYSMENU = 0x00080000
HWND_MESSAGE = -3

CS_HREDRAW = 0x0002
CS_VREDROW = 0x0001

# MSG structure
class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
    ]

# WNDCLASSW structure (not in ctypes.wintypes)
# 返回类型必须是 LRESULT（c_ssize_t），不能用 c_long（32位会截断/溢出）
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


# GUID structure (for NOTIFYICONDATAW.guidItem)
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


# 完整 NOTIFYICONDATAW（Windows 10+ x64 = 976 字节）
# 缺字段会导致 cbSize 不符，Shell_NotifyIconW 直接失败
class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


class NativeTrayIcon(QObject):
    """使用纯Win32 API创建的系统托盘图标（安全版）"""
    
    clicked = pyqtSignal()
    double_clicked = pyqtSignal()
    tray_recreated = pyqtSignal()   # explorer 重启后图标已重新添加
    
    _instance_count = 0
    
    def __init__(self, icon_path: str, tooltip: str, parent=None):
        super().__init__(parent)
        NativeTrayIcon._instance_count += 1
        self._tray_id = 1000 + NativeTrayIcon._instance_count
        self.icon_path = icon_path
        self.tooltip = tooltip
        self._hicon = 0
        self._hwnd = 0
        self._added = False
        self._context_menu = None
        self._running = False
        self._thread = None
        self._taskbar_created_msg = 0
        # 图标重加后由主线程重新写入 IsPromoted
        self.tray_recreated.connect(self._mark_tray_promoted)
        
        # 在单独线程中创建窗口和消息循环
        self._ready_event = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=3.0)  # 等待窗口创建完成
    
    def _thread_main(self):
        """后台线程：创建消息窗口并运行消息循环"""
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            
            hinstance = kernel32.GetModuleHandleW(None)
            
            # 关键：DefWindowProcW 必须声明签名，否则 64 位 lparam 按 32 位传递会溢出崩溃
            user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.SetForegroundWindow.argtypes = [wt.HWND]
            user32.SetForegroundWindow.restype = wt.BOOL
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            
            # x64 下句柄是 64 位，不声明 restype 会被截断成 32 位
            user32.CreateWindowExW.restype = wt.HWND
            user32.CreateWindowExW.argtypes = [
                wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID
            ]
            user32.RegisterClassW.restype = wt.ATOM
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
            user32.GetMessageW.restype = wt.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.restype = ctypes.c_ssize_t
            
            # 注册 TaskbarCreated 消息：explorer.exe 重启后必须重新 NIM_ADD，否则图标消失
            try:
                user32.RegisterWindowMessageW.restype = wt.UINT
                user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
                self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            except Exception:
                self._taskbar_created_msg = 0
            
            # 定义窗口过程回调（返回 LRESULT = c_ssize_t）
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
            
            def wnd_proc(hwnd, msg, wparam, lparam):
                try:
                    if msg == WM_TRAYMESSAGE:
                        # v4 format: lparam contains the actual message in LOWORD
                        actual_msg = lparam & 0xFFFF
                        
                        if actual_msg == WM_RBUTTONUP:
                            if self._context_menu:
                                try:
                                    user32.SetForegroundWindow(hwnd)
                                    QTimer.singleShot(0, self._show_context_menu)
                                except Exception:
                                    pass
                            return 0
                        
                        elif actual_msg == WM_LBUTTONUP:
                            try:
                                self.clicked.emit()
                            except Exception:
                                pass
                            return 0
                        
                        elif actual_msg == WM_LBUTTONDBLCLK:
                            try:
                                self.double_clicked.emit()
                            except Exception:
                                pass
                            return 0
                    
                    elif msg == WM_DESTROY:
                        user32.PostQuitMessage(0)
                        return 0
                    
                    elif self._taskbar_created_msg and msg == self._taskbar_created_msg:
                        # explorer.exe 重启（含主题切换导致的任务栏重建）：重新添加图标
                        try:
                            self._added = False
                            self._add_tray_icon()
                            ZeroFallLogger.info("[托盘] 收到 TaskbarCreated，图标已重新添加")
                            try:
                                self.tray_recreated.emit()
                            except Exception:
                                pass
                        except Exception as _e:
                            ZeroFallLogger.warning("[托盘] 重新添加图标失败: {}".format(_e))
                        return 0
                    
                    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
                except Exception:
                    # 回调里绝不能抛异常，否则 ctypes 会吞掉并可能导致崩溃
                    return 0
            
            # 保持回调函数引用（防止GC）
            self._wnd_proc = WNDPROC(wnd_proc)
            
            # 注册窗口类
            class_name = f"ZeroFallTrayWnd_{self._tray_id}"
            
            wnd_class = WNDCLASSW()
            wnd_class.style = 0
            wnd_class.lpfnWndProc = self._wnd_proc
            wnd_class.cbClsExtra = 0
            wnd_class.cbWndExtra = 0
            wnd_class.hInstance = hinstance
            wnd_class.hIcon = 0
            wnd_class.hCursor = 0
            wnd_class.hbrBackground = 0
            wnd_class.lpszMenuName = None
            wnd_class.lpszClassName = class_name
            
            atom = user32.RegisterClassW(ctypes.byref(wnd_class))
            if not atom:
                ZeroFallLogger.error(f"RegisterClassW失败: {ctypes.get_last_error()}")
                self._ready_event.set()
                return
            
            # 创建消息窗口
            self._hwnd = user32.CreateWindowExW(
                0,                      # dwExStyle
                class_name,             # lpClassName
                "ZeroFall Tray",        # lpWindowName
                0x80000000,             # dwStyle (WS_POPUP)
                -1000, -1000, 1, 1,    # x, y, width, height (off-screen)
                None,                   # hWndParent
                None,                   # hMenu
                hinstance,              # hInstance
                None                    # lpParam
            )
            
            create_error = ctypes.windll.kernel32.GetLastError()
            if not self._hwnd:
                ZeroFallLogger.error(f"CreateWindowExW失败: error={create_error}")
                self._ready_event.set()
                return
            
            # 加载图标
            self._hicon = self._load_hicon()
            
            # 创建托盘图标
            self._add_tray_icon()
            
            # 标记就绪
            self._ready_event.set()
            self._running = True
            
            ZeroFallLogger.info(f"原生托盘窗口已创建 (HWND={self._hwnd}, ID={self._tray_id})")
            
            # 消息循环
            msg = MSG()
            while self._running:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        
        except Exception as e:
            ZeroFallLogger.error(f"托盘线程异常: {e}")
            self._ready_event.set()
    
    def _load_hicon(self):
        """加载托盘图标句柄（三层回退，换台电脑也能显示）

        为什么要三层：只依赖 --add-data 带出来的 ico 文件，一旦打包参数
        漏了就会静默退回系统默认的白窗口图标。直接从 exe 资源里抠
        （ExtractIconEx）更可靠 —— 只要 --icon 打了就一定有。
        """
        try:
            from modules.icon_loader import load_tray_hicon, tray_icon_size
            hicon, source = load_tray_hicon()
            size = tray_icon_size()
            if source == "file":
                ZeroFallLogger.info(
                    "托盘图标已加载 ({}x{} 来自 ico 文件) 句柄=0x{:X}".format(
                        size, size, hicon))
            elif source == "exe":
                ZeroFallLogger.info(
                    "托盘图标已加载 ({}x{} 从 exe 资源提取) 句柄=0x{:X}".format(
                        size, size, hicon))
            elif source == "default":
                ZeroFallLogger.warning(
                    "托盘图标退化为系统默认图标 —— ico 与 exe 资源都没取到，"
                    "检查打包是否带了 --icon 和 --add-data")
            else:
                ZeroFallLogger.error("托盘图标加载失败：所有途径都返回空句柄")
            return hicon
        except Exception as e:
            ZeroFallLogger.error(f"加载图标失败: {e}")
            return 0

    def _add_tray_icon(self):
        """添加托盘图标"""
        if not self._hwnd or not self._hicon:
            return
        
        try:
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = self._tray_id
            nid.uCallbackMessage = WM_TRAYMESSAGE
            nid.hIcon = self._hicon
            nid.szTip = str(self.tooltip or "")[:127]
            
            # ── 第一步：NIM_ADD（只带基础标志，不含 NIF_STATE）──
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            result = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            add_err = kernel32.GetLastError()
            
            if not result:
                # 首次 NIM_ADD 偶发 E_FAIL（explorer 还没准备好接收新图标），
                # 稍等重试一次通常就成功；仍失败再试 NIM_MODIFY（图标可能
                # 已经存在只是 ADD 被拒）。全程不刷 ERROR —— 托盘图标最终
                # 是能出现的，刷 ERROR 只会让人误判。
                try:
                    import time as _t
                    _t.sleep(0.15)
                    result = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
                except Exception:
                    result = 0
                if not result:
                    try:
                        result = shell32.Shell_NotifyIconW(
                            NIM_MODIFY, ctypes.byref(nid))
                    except Exception:
                        result = 0
                if result:
                    # 重试成功也要记状态，否则下次调用又会被当成"首次添加"
                    self._added = True
                    ZeroFallLogger.info(
                        "托盘图标重试成功 (ID={})".format(self._tray_id))
                else:
                    ZeroFallLogger.warning(
                        "托盘图标注册未确认 (ID={}, error={})，"
                        "不影响主窗口使用；关窗时若托盘不可用会自动退出进程".format(
                            self._tray_id, kernel32.GetLastError()))
                    return

            self._added = True
            
            # ── 第二步：设置 v4 版本 ──
            nid.uVersion = NOTIFYICON_VERSION_4
            nid.uFlags = 0
            shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
            
            # ── 第三步：Windows 11 固定图标到任务栏 ──
            # 由调用方（主线程）用 QTimer 调度 _mark_tray_promoted
            # 原因：注册表记录由 explorer 异步创建，且后台线程 Timer 不可靠
            # ── 第三步：让图标常驻任务栏 ──
            # NIS_FORCEINLINE 在 Win10/11 是预留标志，NIM_MODIFY 带它必然
            # E_FAIL。之前每次都刷一条"强制内联失败"，看着像托盘坏了，
            # 其实图标早已添加成功 —— 直接走可靠路径：注册表关掉
            # EnableAutoTray，让所有托盘图标固定显示。
            ZeroFallLogger.info(
                "托盘图标已添加 (ID={}, cbSize={})".format(
                    self._tray_id, nid.cbSize))
            try:
                self._set_tray_always_visible()
            except Exception:
                pass
        
        except Exception as e:
            ZeroFallLogger.error(f"添加托盘图标异常: {e}")
    
    def _set_tray_always_visible(self):
        """通过注册表让托盘图标固定显示在任务栏（Windows 官方支持方式）
        
        设置 HKCU\\...\\Explorer\\EnableAutoTray = 0
        效果：任务栏显示所有托盘图标，不折叠到溢出区。
        """
        try:
            import winreg
            
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer"
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0,
                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
            )
            
            try:
                current, _ = winreg.QueryValueEx(key, "EnableAutoTray")
            except FileNotFoundError:
                current = None
            
            if current != 0:
                winreg.SetValueEx(key, "EnableAutoTray", 0, winreg.REG_DWORD, 0)
                ZeroFallLogger.info("已设置 EnableAutoTray=0（托盘图标将固定显示）")
            else:
                ZeroFallLogger.info("EnableAutoTray 已为 0")
            
            winreg.CloseKey(key)
        except Exception as e:
            ZeroFallLogger.warning(f"设置 EnableAutoTray 失败: {e}")
    
    def _mark_tray_promoted(self):
        """Windows 11 关键修复：设置 IsPromoted=1 让图标固定显示在任务栏
        
        路径: HKCU\\Control Panel\\NotifyIconSettings\\{hash}\\IsPromoted
        hash 由系统根据 exe 路径生成，遍历所有子键按 ExecutablePath 匹配。
        """
        # 只在状态变化时记录，避免每次轮询都刷一条
        try:
            import winreg
            
            # 当前 exe 的绝对路径
            if getattr(sys, 'frozen', False):
                exe_path = Path(sys.executable).resolve()
            else:
                exe_path = Path(sys.argv[0]).resolve()
            
            base_path = r"Control Panel\NotifyIconSettings"
            try:
                root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base_path, 0, winreg.KEY_READ)
            except FileNotFoundError:
                ZeroFallLogger.info("NotifyIconSettings 键不存在（非 Win11 或图标尚未注册）")
                return
            
            promoted = 0
            matched = 0
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, idx)
                    idx += 1
                except OSError:
                    break
                
                try:
                    # 以可写方式打开，便于设置 IsPromoted
                    sub = winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"{}\{}".format(base_path, sub_name), 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE
                    )
                except OSError:
                    continue
                
                try:
                    try:
                        exec_path, _ = winreg.QueryValueEx(sub, "ExecutablePath")
                    except FileNotFoundError:
                        exec_path = None
                    
                    if exec_path:
                        try:
                            same = Path(exec_path).resolve() == exe_path
                        except Exception:
                            same = (str(exec_path).lower() == str(exe_path).lower())
                        
                        if same:
                            matched += 1
                            # 已是 1 则跳过，避免重复写
                            try:
                                cur, _ = winreg.QueryValueEx(sub, "IsPromoted")
                            except FileNotFoundError:
                                cur = None
                            if cur != 1:
                                winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                                promoted += 1
                except Exception:
                    pass
                finally:
                    winreg.CloseKey(sub)
            
            winreg.CloseKey(root)
            
            if promoted:
                ZeroFallLogger.info("已固定 {} 个托盘图标到任务栏 (IsPromoted=1)".format(promoted))
            elif matched:
                # 同一状态只记一次：这个函数每 3 秒被调一次，全量打印会刷满日志
                if getattr(self, '_promoted_logged', None) != matched:
                    self._promoted_logged = matched
                    ZeroFallLogger.info(
                        "托盘图标已固定在任务栏（{} 条记录）".format(matched))
            else:
                ZeroFallLogger.info("未找到匹配的 NotifyIconSettings 记录（等待 explorer 创建）")
        except Exception as e:
            ZeroFallLogger.warning("设置 IsPromoted 失败: {}".format(e))
    
    def _show_context_menu(self):
        """显示右键菜单（在Qt主线程中调用）"""
        if self._context_menu:
            self._context_menu.popup(QCursor.pos())
    
    def show(self):
        """显示托盘图标（已在创建时自动显示）"""
        pass
    
    def hide(self):
        """隐藏并删除托盘图标"""
        self._running = False
        if self._added and self._hwnd:
            try:
                shell32 = ctypes.windll.shell32
                
                # 使用完整结构体（cbSize 必须正确，否则 NIM_DELETE 会失败导致图标残留）
                nid = NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
                nid.hWnd = self._hwnd
                nid.uID = self._tray_id
                
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            except:
                pass
            self._added = False
        
        # 销毁窗口
        if self._hwnd:
            try:
                user32 = ctypes.windll.user32
                user32.PostMessageW(self._hwnd, WM_DESTROY, 0, 0)
            except:
                pass
    
    def setToolTip(self, tooltip: str):
        """设置提示文本"""
        self.tooltip = tooltip
        # 需要重新创建图标
        if self._added:
            try:
                self._added = False
                self._add_tray_icon()
            except:
                pass
    
    def setContextMenu(self, menu: QMenu):
        """设置右键菜单"""
        self._context_menu = menu
    
    def __del__(self):
        try:
            self.hide()
            if self._hicon:
                ctypes.windll.user32.DestroyIcon(self._hicon)
        except:
            pass
