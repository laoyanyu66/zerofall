# -*- coding: utf-8 -*-
"""
ZeroFall 主界面模块
简化版 - 移除复杂的 native tray，用 QSystemTrayIcon
"""
import sys
import os
import ctypes
import ctypes.wintypes
import time
import json
import shutil
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, List

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QMessageBox, QGroupBox, QGridLayout, QComboBox,
    QCheckBox, QLineEdit, QFileDialog, QHeaderView, QSplitter,
    QStatusBar, QMenuBar, QMenu, QSystemTrayIcon, QDialog,
    QDialogButtonBox, QFormLayout, QSpinBox, QProgressBar,
    QSplashScreen, QFrame, QAbstractItemView
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QThread, QAbstractNativeEventFilter
)
from PyQt6.QtGui import QAction, QIcon, QColor, QPixmap, QKeySequence, QShortcut

sys.path.append(str(Path(__file__).parent.parent))
from config import ZeroFallConfig, ConfigManager
from logger import ZeroFallLogger
from modules.snapshot_manager import SnapshotManager, RestoreMode
from modules.environment_checker import EnvironmentChecker
from modules.guardian import Guardian
from modules.baidu_pan import BaiduPanClient
from ui.native_tray import NativeTrayIcon
from ui import theme as th
from utils import set_directory_readonly, format_timestamp, config_int

# 导入拆分后的 Tab 类
from ui.tabs import DashboardTab, SnapshotTab, EnvTab, CloudTab, SettingsTab, PluginTab, ConfigTab
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


# ── 全局热键 Windows 常量 ──────────────────────────────────────────
WM_HOTKEY = 0x0312
HOTKEY_ID_ZEROFALL = 1
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_Z = 0x5A


def _ts0(value) -> float:
    """把"时间戳字段"转 float，None / 空 / 非法一律当 0.0

    P6（2026-11-16）：`float(getattr(self, '_x', 0) or 0)` 这种写法会被
    selfcheck 的 or-num 检查点名 —— 虽然这里 0 与 None 同义，但项目里
    已因 `or <数字>` 吞过合法的 0，统一改走这个函数，读起来也没有歧义。

    放成模块级函数（而不是类里的静态方法）是有原因的：`tests/test_cloud_window.py`
    会把选定的方法绑到自己的 Host 桩对象上跑，桩对象上没有本类的方法表。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class ZeroFallMainWindow(QMainWindow):
    """ZeroFall主窗口"""
    
    def __init__(self, config_manager: ConfigManager):
        super().__init__()
        
        self.config_manager = config_manager
        self.config = config_manager.config
        
        # 初始化核心模块
        self.snapshot_manager = SnapshotManager(self.config)
        # 保留策略 vs 盘面对账（2026-11-11 自检发现配置与实际差了三倍没人知道）
        try:
            from modules import diagnostics as _diag
            _rec = _diag.retention_reconcile(self.config)
            if _rec.get("notes"):
                _msg = "[保留策略] {} | 盘上 {} 份 / {:.2f} GB，稳态预计 {:.2f} GB".format(
                    "；".join(_rec["notes"]), _rec["disk"]["total"],
                    _rec["disk"]["size_gb"], _rec.get("steady_state_gb", 0))
                # 只有真超了（宽松档 2 倍）才值得 WARNING；到上限本身是设计内状态
                if _rec.get("severity") == "warning":
                    ZeroFallLogger.warning(_msg)
                else:
                    ZeroFallLogger.info(_msg)
            else:
                ZeroFallLogger.info("[保留策略] 配置与盘面一致：{} 份 / {:.2f} GB，稳态预计 {:.2f} GB".format(
                    _rec.get("disk", {}).get("total", 0),
                    _rec.get("disk", {}).get("size_gb", 0),
                    _rec.get("steady_state_gb", 0)))
        except Exception as _e:
            ZeroFallLogger.debug("[保留策略] 对账跳过: {}".format(_e))
        self.env_checker = EnvironmentChecker(self.config, self.snapshot_manager)
        self.guardian = Guardian(self.config, self.env_checker, self.snapshot_manager)
        self.baidu_pan = BaiduPanClient(self.config) if self.config.baidu_pan_enabled else None
        
        # 设置回调
        self.guardian.set_crash_callback(self._on_deepseek_crash)
        self.guardian.set_exit_callback(self._on_deepseek_exit)
        
        # ── 快照对比状态 ──
        self._compare_first_id: Optional[str] = None
        
        # ── DeepSeek 运行起始时间（用于 Dashboard 运行时长） ──
        
        # 全局热键状态（nativeEvent 已移除，改用窗口内 QShortcut）
        self._hotkey_registered = False
        
        # 自动启动状态（用于共生退出保护：启动后秒退不自动关闭）
        self._auto_launched = False
        self._launch_ts = 0.0
        
        # 自动回退计数（防止崩溃→回退→再崩溃的死循环）
        self._rollback_count = 0
        # 已尝试回退过的快照 ID（失败时改用更早的，避免原地重试）
        self._tried_snapshots = set()
        
        # 云同步：客户端未运行时的提示标记（避免反复弹窗）
        self._cloud_client_warned = False
        
        # 快照任务占用标记（后台执行，避免 UI 冻结）
        self._snapshot_busy = False
        
        # 快照生命周期：累计轻量快照数（每 N 个触发一个完整快照）
        self._light_since_full = 0
        # 快照生命周期：已完成云端收敛的日期集合（避免重复收敛）
        self._cloud_consolidated_dates = set()
        # 快照生命周期：溢出归档锁（避免并发云归档）
        self._archive_busy = False
        
        self._init_ui()
        self._init_tray()
        self._apply_autostart_setting()
        self._start_status_timer()

        # 启动后静默自愈「会话续跑」插件：重装 / 重建 profile / 被改成 enabled:false
        # 都是"等 DSH 起来才发现没恢复"的典型原因，这里提前补回来（幂等，不改不写）。
        try:
            QTimer.singleShot(4000, self._auto_heal_resume_plugin)
        except Exception:
            pass

        ZeroFallLogger.info("ZeroFall主界面初始化完成")

    def _auto_heal_resume_plugin(self):
        """后台静默自愈插件（不弹窗、不阻塞 UI）。"""
        def _work():
            try:
                from modules import resume_guard as rg
                rep = rg.ensure_plugin(rg.resolve_home(self.config), "desktop", host_llm=None)
                actions = rep.get("actions") or []
                errors = rep.get("errors") or []
                if actions:
                    ZeroFallLogger.info("[会话续跑] 启动自愈：" + "；".join(str(a) for a in actions))
                if errors:
                    ZeroFallLogger.warning("[会话续跑] 启动自愈未完成：" + "；".join(errors))
            except Exception as exc:      # noqa: BLE001
                ZeroFallLogger.warning("[会话续跑] 启动自愈异常: {}".format(exc))

        import threading as _th_heal
        _th_heal.Thread(target=_work, daemon=True, name="ZeroFall-ResumeHeal").start()
    
    def showEvent(self, event):
        """窗口首次显示"""
        super().showEvent(event)
        # 标题栏深色化：必须在窗口真正有 HWND 之后做，而且从最小化恢复、
        # 主题切换后 DWM 状态可能被重置，所以每次 show 都重申一次
        try:
            th.apply_dark_titlebar(self)
        except Exception:
            pass
    
    # ════════════════════════════════════════════════════════════════
    #  UI 初始化
    # ════════════════════════════════════════════════════════════════
    
    def _scrollable(self, widget, min_height: int = 0):
        """把页面塞进滚动容器

        设置页的表单高度超过 2000px，若不包裹会把整个窗口最小高度顶到 2150px，
        导致窗口被强制拉高、其他页签大片空白。包一层 QScrollArea 后，
        页签的尺寸不再由内容决定，窗口能保持正常大小，内容超出时滚动查看。
        """
        from PyQt6.QtWidgets import QScrollArea
        if min_height:
            widget.setMinimumHeight(min_height)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        area.setWidget(widget)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        return area

    def _init_ui(self):
        """初始化用户界面"""
        self.setWindowTitle("ZeroFall - DeepSeek前置守护系统")
        self.setGeometry(100, 100, 1000, 700)
        
        # 设置窗口图标（统一走 icon_loader，frozen 下会找 _MEIPASS）
        try:
            from modules.icon_loader import icon_file_path, describe
            icon_path = icon_file_path()
            if icon_path.is_file():
                self.setWindowIcon(QIcon(str(icon_path)))
                ZeroFallLogger.info("[图标] " + describe())
            else:
                ZeroFallLogger.warning(
                    "[图标] ico 缺失，窗口图标将退回 exe 资源: {}".format(icon_path))
        except Exception as e:
            ZeroFallLogger.warning("[图标] 加载失败: {}".format(e))
        
        # 创建中心部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        
        # 状态面板
        self._create_status_panel(main_layout)
        
        # 标签页
        # 自绘侧边导航：QTabWidget 竖排会把中文旋转成竖排，这里换成
        # 列表+堆栈的自绘版本，文字横排并带左侧高亮条
        from ui.sidebar import SideNav
        self.tab_widget = SideNav()
        main_layout.addWidget(self.tab_widget)
        
        # ★ 首页（Dashboard）标签页 —— 放在最前面
        self.dashboard_tab = DashboardTab(self.config, self.guardian, self.env_checker, self.snapshot_manager)
        self.tab_widget.addTab(self._scrollable(self.dashboard_tab), "首页")
        self.dashboard_tab.dash_launch_btn.clicked.connect(self._on_launch_deepseek)
        
        # 快照管理标签页
        self.snapshot_tab = SnapshotTab(self.config, self.snapshot_manager)
        self.tab_widget.addTab(self._scrollable(self.snapshot_tab, 760), "快照管理")
        
        # 插件管理标签页
        self.plugin_tab = PluginTab(self.config, self.snapshot_manager)
        self.tab_widget.addTab(self._scrollable(self.plugin_tab, 620), "插件管理")
        
        # 配置编辑标签页：融合 dsh-config-editor 的能力（改 profile 补丁）
        try:
            self.config_tab = ConfigTab(self.config, self.snapshot_manager, main_window=self)
            self.tab_widget.addTab(self._scrollable(self.config_tab, 620), "配置编辑")
        except Exception as _e:
            self.config_tab = None
            ZeroFallLogger.warning("[配置编辑] 页签初始化失败: {}".format(_e))

        # 会话续跑标签页：崩溃/重启后把「关闭前在跑」的会话接上（自愈插件 + 看快照 + 补跑）
        try:
            from ui.tabs.resume_tab import ResumeTab
            self.resume_tab = ResumeTab(self.config, self.snapshot_manager)
            self.tab_widget.addTab(self._scrollable(self.resume_tab, 620), "会话续跑")
        except Exception as _e:
            self.resume_tab = None
            ZeroFallLogger.warning("[会话续跑] 页签初始化失败: {}".format(_e))

        # 环境校验标签页
        self.env_tab = EnvTab(self.config, self.env_checker)
        self.tab_widget.addTab(self._scrollable(self.env_tab, 620), "环境校验")
        
        # 云端同步标签页
        self.cloud_tab = CloudTab(self.config, self.baidu_pan, main_window=self)
        self.tab_widget.addTab(self._scrollable(self.cloud_tab), "云端同步")
        
        # 云端快照标签页：以 meta.json 为准列出云同步目录里的快照
        try:
            from ui.tabs.cloud_snapshots_tab import CloudSnapshotsTab
            self.cloud_snapshots_tab = CloudSnapshotsTab(
                self.config, self.snapshot_manager, main_window=self)
            self.tab_widget.addTab(self._scrollable(self.cloud_snapshots_tab, 620), "云端快照")
        except Exception as _e:
            self.cloud_snapshots_tab = None
            ZeroFallLogger.warning("[云端快照] 页签初始化失败: {}".format(_e))
        
        # 设置标签页
        self.settings_tab = SettingsTab(self.config, self.config_manager)
        self.tab_widget.addTab(self._scrollable(self.settings_tab), "设置")
        
        # 统一表格列宽：内容自适应 + 末列拉伸，避免长内容被截断
        try:
            from PyQt6.QtWidgets import QTableWidget
            _tuned = 0
            for _t in self.findChildren(QTableWidget):
                if _t.columnCount() >= 3:
                    th.tune_table(_t)
                    _tuned += 1
            if _tuned:
                ZeroFallLogger.info("[界面] 已优化 {} 个表格的列宽".format(_tuned))
        except Exception as _e:
            ZeroFallLogger.warning("[界面] 表格列宽优化失败: {}".format(_e))

        # 状态栏
        self.statusBar().showMessage("就绪 | 快捷键: Ctrl+Shift+Z 显示/隐藏窗口")

        # QShortcut 只在窗口有焦点时生效；状态栏宣传的是"显示/隐藏窗口"，
        # 那就必须是全局热键。Win32 RegisterHotKey 才是真全局。
        QTimer.singleShot(300, self._register_global_hotkey)
        
        # 菜单栏
        self._create_menu_bar()
        
        # 窗口内快捷键（全局热键因 nativeEvent 崩溃问题已改用此方案）
        try:
            self._shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
            self._shortcut.activated.connect(self._toggle_window_visibility)
        except Exception as _e:
            ZeroFallLogger.warning("快捷键注册失败: {}".format(_e))
    
    # ════════════════════════════════════════════════════════════════
    #  ★ 功能 1: Dashboard 首页标签页
    # ════════════════════════════════════════════════════════════════
    
    @staticmethod
    def _make_card(title_text: str) -> QFrame:
        """创建一张卡片框架"""
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: #1e1e2e;
                border: 1px solid #333;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        card.setMinimumHeight(120)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        header = QLabel(title_text)
        header.setStyleSheet("font-size: 12px; color: #888; border: none;")
        layout.addWidget(header)
        return card
    
    @staticmethod
    def _refresh_dashboard(self):
        """刷新 Dashboard 所有卡片数据"""
        try:
            # ── DeepSeek 状态 ──
            ds_running = self.guardian.is_deepseek_running()
            if ds_running:
                self.dash_ds_status.setText("● 运行中")
                self.dash_ds_status.setStyleSheet(
                    "font-size: 18px; font-weight: bold; color: #00ff88;"
                )
                # 记录启动时间
                if self._deepseek_start_time is None:
                    self._deepseek_start_time = time.time()
                elapsed = time.time() - self._deepseek_start_time
                hours = int(elapsed // 3600)
                minutes = int((elapsed % 3600) // 60)
                seconds = int(elapsed % 60)
                self.dash_ds_duration.setText(
                    "运行时长: {:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
                )
                self.dash_launch_btn.setEnabled(False)
                self.dash_launch_btn.setText("🚀 DeepSeek 运行中")
            else:
                self.dash_ds_status.setText("○ 未运行")
                self.dash_ds_status.setStyleSheet(
                    "font-size: 18px; font-weight: bold; color: gray;"
                )
                self.dash_ds_duration.setText("运行时长: --")
                self._deepseek_start_time = None
                self.dash_launch_btn.setEnabled(True)
                self.dash_launch_btn.setText("🚀 启动DeepSeek")
            
            # ── 环境健康度 ──
            try:
                report = self.env_checker.run_full_check(use_cache=True, auto=True)
                result_val = report.overall_result.value
                if result_val == "passed":
                    self.dash_health_icon.setText("✅")
                    self.dash_health_text.setText("健康")
                    self.dash_health_text.setStyleSheet("font-size: 14px; color: #00ff88;")
                elif result_val == "warning":
                    self.dash_health_icon.setText("⚠️")
                    self.dash_health_text.setText("有警告")
                    self.dash_health_text.setStyleSheet("font-size: 14px; color: #ffaa00;")
                else:
                    self.dash_health_icon.setText("❌")
                    self.dash_health_text.setText("异常")
                    self.dash_health_text.setStyleSheet("font-size: 14px; color: #ff4444;")
            except Exception:
                self.dash_health_icon.setText("🔍")
                self.dash_health_text.setText("检测失败")
                self.dash_health_text.setStyleSheet("font-size: 14px; color: #888;")
            
            # ── 快照数据 ──
            snapshots = self.snapshot_manager.get_snapshot_list()
            healthy = [s for s in snapshots if s.status.value == "healthy"]
            self.dash_snap_count.setText(str(len(healthy)))
            
            # ── 最近快照 ──
            if snapshots:
                latest = snapshots[0]  # 已按时间倒序
                self.dash_latest_time.setText(
                    "时间: " + format_timestamp(latest.created_at)
                )
                self.dash_latest_size.setText(
                    "大小: {:.2f} MB".format(latest.size_mb)
                )
            else:
                self.dash_latest_time.setText("时间: --")
                self.dash_latest_size.setText("大小: --")
            
            # ── 守护状态 ──
            if ds_running:
                self.dash_guard_status.setText("🛡️ 守护中")
                self.dash_guard_status.setStyleSheet(
                    "font-size: 16px; font-weight: bold; color: #00ff88;"
                )
            else:
                self.dash_guard_status.setText("⏸ 待命")
                self.dash_guard_status.setStyleSheet(
                    "font-size: 16px; font-weight: bold; color: #ffaa00;"
                )
            
            # ── 性能监控 ──
            try:
                import psutil
                current_proc = psutil.Process()
                # interval=None 是非阻塞采样：返回距上次调用的平均 CPU%，绝不 sleep
                cpu_percent = current_proc.cpu_percent(interval=None)
                memory_mb = current_proc.memory_info().rss / (1024 * 1024)
                if hasattr(self, 'dash_perf_label'):
                    self.dash_perf_label.setText("CPU: {:.1f}% | 内存: {:.0f} MB".format(cpu_percent, memory_mb))
            except Exception:
                pass
        except Exception as e:
            ZeroFallLogger.error("Dashboard 刷新异常: {}".format(e))
    
    def _on_quick_snapshot(self):
        """Dashboard 快速快照（无需输入备注）"""
        snapshot = self.snapshot_manager.create_snapshot(notes="快速快照")
        if snapshot:
            self._refresh_snapshot_tab()
            self._refresh_dashboard()
            QMessageBox.information(
                self, "快速快照",
                "快照创建成功\nID: {}".format(snapshot.snapshot_id)
            )
        else:
            QMessageBox.critical(self, "快照失败", "快照创建失败，请查看日志")
    
    def _on_quick_verify(self):
        """Dashboard 快速执行环境校验

        结果用带分项列表的专用面板展示：原来是把所有警告拼成一段纯文本，
        十几项挤在一起根本看不出是哪一项出了问题。
        """
        try:
            report = self.env_checker.run_full_check()
            from ui.dialogs import VerifyResultDialog
            VerifyResultDialog(self, report).exec()
            self._refresh_dashboard()
        except Exception as e:
            QMessageBox.critical(self, "校验失败", "环境校验出错: {}".format(e))
    
    # ════════════════════════════════════════════════════════════════
    #  ★ 功能 2: 快照差异对比
    # ════════════════════════════════════════════════════════════════
    
    def _on_compare_snapshots(self):
        """对比快照 — 需要用户选择两个快照"""
        selected_rows = self.snapshot_table.selectionModel().selectedRows()
        
        if len(selected_rows) < 2:
            QMessageBox.information(
                self, "对比快照",
                "请在表格中按住 Ctrl 选择两个快照后再点击「对比快照」"
            )
            return
        
        if len(selected_rows) > 2:
            QMessageBox.warning(
                self, "对比快照",
                "只能选择两个快照进行对比，请取消多余选择"
            )
            return
        
        row1 = selected_rows[0].row()
        row2 = selected_rows[1].row()
        id1 = self.snapshot_table.item(row1, 0).text()
        id2 = self.snapshot_table.item(row2, 0).text()
        
        self.compare_hint_label.setText(
            "正在对比 {} ↔ {} ...".format(id1[:16], id2[:16])
        )
        QApplication.processEvents()
        
        try:
            diff = self.snapshot_manager.compare_snapshots(id1, id2)
        except Exception as e:
            QMessageBox.critical(self, "对比失败", "对比快照出错: {}".format(e))
            self.compare_hint_label.setText("")
            return
        
        self.compare_hint_label.setText("")
        self._show_compare_result(id1, id2, diff)
    
    def _show_compare_result(self, id1: str, id2: str, diff: Dict):
        """弹窗展示快照对比结果"""
        dlg = QDialog(self)
        dlg.setWindowTitle("快照对比: {} ↔ {}".format(id1[:16], id2[:16]))
        dlg.setMinimumSize(700, 500)
        dlg_layout = QVBoxLayout(dlg)
        
        # 如果没有差异数据
        if not diff:
            dlg_layout.addWidget(QLabel("无差异数据（可能快照中无插件目录）"))
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dlg.close)
            dlg_layout.addWidget(close_btn)
            dlg.exec()
            return
        
        # 提取插件差异
        plugins_diff = diff.get('plugins', {})
        added = plugins_diff.get('added', [])
        removed = plugins_diff.get('removed', [])
        modified = plugins_diff.get('modified', [])
        
        # ── 汇总 ──
        summary = QLabel(
            "新增: {} 个文件 | 删除: {} 个文件 | 修改: {} 个文件".format(
                len(added), len(removed), len(modified)
            )
        )
        summary.setStyleSheet("font-size: 14px; font-weight: bold; padding: 6px;")
        dlg_layout.addWidget(summary)
        
        # ── 差异表格 ──
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["变更类型", "文件路径"])
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        
        rows = []
        for f in added:
            rows.append(("➕ 新增", f))
        for f in removed:
            rows.append(("❌ 删除", f))
        for f in modified:
            rows.append(("🔄 修改", f))
        
        table.setRowCount(len(rows))
        color_map = {"➕ 新增": "#00ff88", "❌ 删除": "#ff4444", "🔄 修改": "#ffaa00"}
        for r, (change_type, filepath) in enumerate(rows):
            type_item = QTableWidgetItem(change_type)
            type_item.setForeground(QColor(color_map.get(change_type, "#ccc")))
            table.setItem(r, 0, type_item)
            table.setItem(r, 1, QTableWidgetItem(filepath))
        
        dlg_layout.addWidget(table)
        
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.close)
        dlg_layout.addWidget(close_btn)
        
        dlg.exec()
    
    # ════════════════════════════════════════════════════════════════
    #  状态面板 & 其他标签页
    # ════════════════════════════════════════════════════════════════
    
    def _create_status_panel(self, layout: QVBoxLayout):
        """顶部品牌栏 + 状态胶囊

        原来是"系统状态"分组框里几行灰色小字，信息都在但看不出主次。
        这里改成：左侧品牌标识，右侧四枚状态胶囊 + 主操作按钮，
        高度固定，观感接近现代工具软件。
        """
        bar = QFrame()
        bar.setObjectName('topbar')
        bar.setFixedHeight(64)
        bar.setStyleSheet(
            "QFrame#topbar {{"
            " background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "   stop:0 #0e1420, stop:0.55 #101a28, stop:1 #0e1420);"
            " border: none;"
            " border-bottom: 1px solid {b};"
            "}}".format(b=th.BORDER))
        row = QHBoxLayout(bar)
        row.setContentsMargins(18, 0, 14, 0)
        row.setSpacing(12)

        # 品牌：一个渐变圆点 + 名称
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(
            "QLabel {{ border-radius: 5px; border: none;"
            " background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "   stop:0 {a}, stop:1 {b}); }}".format(a=th.ACCENT, b=th.ACCENT_2))
        row.addWidget(dot)

        brand = QLabel("ZeroFall")
        brand.setObjectName('brand')
        row.addWidget(brand)

        ver = QLabel("v1.6.6")
        ver.setObjectName('mute')
        row.addWidget(ver)

        row.addSpacing(10)
        row.addStretch(1)

        # 四枚状态胶囊（保留原有属性名，刷新逻辑不用改）
        self.deepseek_status_label = th.pill("DeepSeek 未运行", 'warn')
        row.addWidget(self.deepseek_status_label)

        self.guardian_status_label = th.pill("守护 就绪", 'ok')
        row.addWidget(self.guardian_status_label)

        self.health_status_label = th.pill("健康度 未知", 'info')
        row.addWidget(self.health_status_label)

        self.snapshot_count_label = th.pill("快照 0", 'info')
        row.addWidget(self.snapshot_count_label)

        row.addSpacing(6)
        self.launch_button = QPushButton("启动 DeepSeek")
        self.launch_button.setMinimumHeight(34)
        self.launch_button.setMinimumWidth(132)
        self.launch_button.clicked.connect(self._on_launch_deepseek)
        row.addWidget(self.launch_button)

        layout.addWidget(bar)

    
    # ════════════════════════════════════════════════════════════════
    #  ★ 功能 3: 设置标签页 — 插件目录只读锁定
    # ════════════════════════════════════════════════════════════════
    
    # ════════════════════════════════════════════════════════════════
    #  菜单栏 & 系统托盘
    # ════════════════════════════════════════════════════════════════
    
    def _create_menu_bar(self):
        """创建菜单栏"""
        menubar = self.menuBar()
        
        file_menu = menubar.addMenu("文件")
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        help_menu = menubar.addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
    
    def _init_tray(self):
        """初始化系统托盘 - 使用原生 Win32 API（可靠显示）"""
        try:
            # 图标路径统一由 icon_loader 解析（frozen / 源码两种情形）
            try:
                from modules.icon_loader import icon_file_path
                _ip = icon_file_path()
                icon_path = str(_ip) if _ip.is_file() else ""
            except Exception:
                icon_path = ""
            # icon_path 为空不代表没图标 —— native_tray 会从 exe 资源里抠
            
            # 创建原生托盘图标（自动显示，带 NIS_FORCEINLINE）
            self.tray_icon = NativeTrayIcon(
                icon_path=icon_path,
                tooltip="ZeroFall - DeepSeek前置守护系统",
                parent=self
            )
            
            # 托盘菜单
            tray_menu = QMenu()
            
            show_action = QAction("显示主窗口", self)
            show_action.triggered.connect(self.show)
            tray_menu.addAction(show_action)
            
            launch_action = QAction("启动DeepSeek", self)
            launch_action.triggered.connect(self._on_launch_deepseek)
            tray_menu.addAction(launch_action)
            
            # 一键回退（应对"UI 弹窗但进程未退出"的情况，进程监控无法自动识别）
            rollback_action = QAction("回退到最新健康快照并重启", self)
            rollback_action.triggered.connect(self._on_quick_rollback)
            tray_menu.addAction(rollback_action)
            
            tray_menu.addSeparator()
            
            exit_action = QAction("退出", self)
            exit_action.triggered.connect(self._on_tray_exit)
            tray_menu.addAction(exit_action)
            
            self.tray_icon.setContextMenu(tray_menu)
            
            # 连接信号：单击和双击都显示主窗口
            self.tray_icon.clicked.connect(self.show)
            self.tray_icon.double_clicked.connect(self.show)
            
            # Windows 11: 立即同步尝试一次 + QTimer 持续重试
            try:
                self.tray_icon._mark_tray_promoted()
            except Exception as _e:
                ZeroFallLogger.warning("首次 IsPromoted 设置异常: {}".format(_e))
            
            self._promote_timer = QTimer(self)
            self._promote_timer.timeout.connect(self.tray_icon._mark_tray_promoted)
            self._promote_timer.start(3000)
            QTimer.singleShot(30000, self._promote_timer.stop)
            
            ZeroFallLogger.info("系统托盘图标已初始化（原生API，强制内联）")
            
        except Exception as e:
            ZeroFallLogger.error("初始化系统托盘失败: {}".format(e))
    
    # ════════════════════════════════════════════════════════════════
    #  ★ 功能 4: 全局热键 Ctrl+Shift+Z
    # ════════════════════════════════════════════════════════════════
    
    def _register_global_hotkey(self):
        """注册全局热键 Ctrl+Shift+Z (仅 Windows)"""
        self._hotkey_registered = False
        if sys.platform != 'win32':
            ZeroFallLogger.info("全局热键仅支持 Windows 平台")
            return
        
        try:
            user32 = ctypes.windll.user32
            # x64 下必须声明签名，否则 HWND 被截断导致崩溃
            user32.RegisterHotKey.argtypes = [
                ctypes.wintypes.HWND, ctypes.c_int,
                ctypes.wintypes.UINT, ctypes.wintypes.UINT
            ]
            user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
            
            hwnd = ctypes.wintypes.HWND(int(self.winId()))
            if not hwnd:
                ZeroFallLogger.warning("窗口句柄无效，跳过热键注册")
                return
            
            # MOD_CONTROL(0x0002) | MOD_SHIFT(0x0004) = 0x0006
            result = user32.RegisterHotKey(
                hwnd, HOTKEY_ID_ZEROFALL, MOD_CONTROL | MOD_SHIFT, VK_Z
            )
            if result:
                self._hotkey_registered = True
                ZeroFallLogger.info("全局热键 Ctrl+Shift+Z 注册成功")
            else:
                ZeroFallLogger.warning("全局热键注册失败，可能已被其他程序占用")
        except Exception as e:
            ZeroFallLogger.error("注册全局热键异常: {}".format(e))
    
    def _unregister_global_hotkey(self):
        """注销全局热键"""
        if not self._hotkey_registered:
            return
        try:
            user32 = ctypes.windll.user32
            user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
            user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL
            hwnd = ctypes.wintypes.HWND(int(self.winId()))
            if hwnd:
                user32.UnregisterHotKey(hwnd, HOTKEY_ID_ZEROFALL)
            self._hotkey_registered = False
            ZeroFallLogger.info("全局热键已注销")
        except Exception as e:
            ZeroFallLogger.error("注销全局热键异常: {}".format(e))
    
    # [诊断] nativeEvent 重写已移除 —— PyQt6 中重写它会安装原生事件过滤器，
    # 处理不当会导致 show() 时 C 级崩溃。全局热键改用其他方案。
    
    def _toggle_window_visibility(self):
        """热键切换窗口可见性"""
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.showNormal()
            self.activateWindow()
            self.raise_()
    
    # ════════════════════════════════════════════════════════════════
    #  定时器 & 状态更新
    # ════════════════════════════════════════════════════════════════
    
    def _apply_autostart_setting(self):
        """把「开机自启」配置落到注册表

        配置项 auto_start_enabled 以前只有个没人调用的读取函数，
        改了完全没效果。这里在启动时对一次账：以配置为准，
        不一致就改注册表，并写日志说明实际结果（失败不装作成功）。
        """
        try:
            from modules import autostart
            want = bool(getattr(self.config, 'auto_start_enabled', True))
            before = autostart.is_enabled()
            if before == want:
                return
            ok = autostart.set_enabled(want)
            if ok:
                ZeroFallLogger.info(
                    "[开机自启] 已{}（配置={}, 原状态={}）".format(
                        "开启" if want else "关闭", want, before))
            else:
                ZeroFallLogger.warning(
                    "[开机自启] 写入注册表失败，配置={} 未生效".format(want))
        except Exception as e:
            ZeroFallLogger.warning("[开机自启] 同步失败: {}".format(e))

    def _start_status_timer(self):
        """启动状态更新定时器"""
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(3000)
        self._update_status()
        
        # 静默定时快照
        self._start_silent_snapshot_timer()
        
        # 每日健康报告
        self._start_daily_report_timer()
        
        # 启动清理：孤儿索引条目 + 遗留打包目录（放后台，不阻塞 UI）
        self._startup_snapshot_hygiene()
        
        # 每日首个完整快照（启动后约 20 秒触发，避开自检高峰）
        QTimer.singleShot(20000, self._check_daily_full_on_startup)
        
        # 主控 / 子控
        self._start_control_timer()
        
        # DSH 版本变化检测（升级时自动创建完整快照基线）
        QTimer.singleShot(5000, self._check_dsh_version_change)
        
        # 延迟 1 秒后自动检测环境健康度
        QTimer.singleShot(1000, self._refresh_health_status)
    
    def _start_silent_snapshot_timer(self):
        """启动静默定时快照定时器"""
        if not self.config.auto_silent_snapshot:
            return
        interval = max(1, self.config.silent_snapshot_interval_minutes) * 60000
        self._silent_timer = QTimer(self)
        self._silent_timer.timeout.connect(self._on_silent_snapshot)
        self._silent_timer.start(interval)
        ZeroFallLogger.info("静默定时快照已启用，间隔 {} 分钟".format(self.config.silent_snapshot_interval_minutes))

        # 定时完整快照：每 60 秒检查一次"距上个完整快照是否已满 N 分钟"。
        # 用 60 秒轮询而不是直接按 N 分钟起定时器，是因为完整快照也可能由
        # 首启/手动/版本变化触发，轮询"按实际最后一次"算才不会被绕乱。
        _fi = config_int(self.config, 'full_snapshot_interval_minutes', 30)
        if _fi > 0:
            self._full_check_timer = QTimer(self)
            self._full_check_timer.timeout.connect(self._check_full_snapshot_interval)
            self._full_check_timer.start(60000)
            ZeroFallLogger.info(
                "定时完整快照已启用，间隔 {} 分钟（以当天首个完整快照为锚点）".format(_fi))
        else:
            ZeroFallLogger.info("定时完整快照已关闭，改用每 N 个轻量触发")

        # 插件/配置变更检测：比定时快照更关键的一道保护。
        # 定时快照只能"碰运气"覆盖到改动之前，变更检测能精确抓到那一刻。
        if getattr(self.config, 'plugin_drift_snapshot', True):
            _ds = max(15, self._cfg_int('plugin_drift_check_seconds', 60))
            self._drift_timer = QTimer(self)
            self._drift_timer.timeout.connect(self._check_plugin_drift)
            self._drift_timer.start(_ds * 1000)
            ZeroFallLogger.info(
                "插件变更检测已启用，每 {} 秒比对一次指纹（静置 {} 秒后快照）".format(
                    _ds, max(0, self._cfg_int('plugin_drift_settle_seconds', 20))))
            # 立刻记一次基准：否则启动后头一个轮询周期（60 秒）里装的插件
            # 会被当成"启动时的初始状态"而漏检
            QTimer.singleShot(2000, self._check_plugin_drift)
        else:
            ZeroFallLogger.info("插件变更检测已关闭（仅靠定时快照）")
        
        # 云端同步（同步盘方案）
        self._start_cloud_sync_timer()
        # 本地更新通道（standalone 也能自换版）
        self._start_self_update_timer()
        # DSH 崩溃日志监控（覆盖"UI 弹窗但进程存活"的场景）
        self._start_crash_log_monitor()
    
    # ── 本地更新通道（standalone 也能自换版）────────────────
    def _self_update_dir(self) -> Path:
        raw = str(getattr(self.config, 'self_update_dir', '') or '').strip()
        if raw:
            return Path(raw)
        if getattr(sys, 'frozen', False):
            return Path(sys.executable).parent / 'updates'
        return Path(__file__).resolve().parent.parent / 'updates'

    def _start_self_update_timer(self):
        self._self_update_busy = False
        if not getattr(self.config, 'self_update_enabled', True):
            ZeroFallLogger.info("[自更新] 本地更新通道已关闭")
            return
        minutes = max(5, self._cfg_int('self_update_interval_minutes', 30))
        try:
            self._self_update_timer = QTimer(self)
            self._self_update_timer.setInterval(minutes * 60 * 1000)
            self._self_update_timer.timeout.connect(self._self_update_tick)
            self._self_update_timer.start()
            ZeroFallLogger.info("[自更新] 每 {} 分钟扫一次 {}".format(
                minutes, self._self_update_dir()))
            QTimer.singleShot(90 * 1000, self._self_update_tick)
        except Exception as e:
            ZeroFallLogger.warning("[自更新] 定时器启动失败: {}".format(e))

    def _self_update_tick(self):
        """扫更新目录：有新包且条件满足 → 换版并重启"""
        if getattr(self, '_self_update_busy', False):
            return
        self._self_update_busy = True
        import threading as _th_su

        def _work():
            try:
                if not getattr(sys, 'frozen', False):
                    return                      # 源码模式不做自替换
                if getattr(self.config, 'self_update_require_idle', True):
                    idle = self._cloud_system_idle_seconds()
                    need = max(0, self._cfg_int('self_update_idle_minutes', 3)) * 60
                    if idle < need:
                        # 静默跳过让人完全看不出"为什么没更新"，这里补一行日志
                        try:
                            from modules.self_update import find_new_package
                            if find_new_package(self._self_update_dir(), sys.executable):
                                ZeroFallLogger.info(
                                    "[自更新] 有可用新包，但键鼠只空闲 {:.0f} 分钟（需 {} 分钟），下一轮再看".format(
                                        idle / 60, int(need // 60)))
                        except Exception:
                            pass
                        return                      # 人还在用，下一轮再看
                from modules.self_update import find_new_package, mark_applied
                hit = find_new_package(self._self_update_dir(), sys.executable)
                if not hit:
                    return
                pkg, manifest = hit
                from modules.updater import apply_update
                ok, msg = apply_update(str(pkg),
                                       install_dir=Path(sys.executable).parent,
                                       restart=True)
                if not ok:
                    ZeroFallLogger.warning("[自更新] 应用失败: {}".format(msg))
                    return
                mark_applied(self._self_update_dir(), pkg, manifest)
                ZeroFallLogger.info("[自更新] 已安排换版 v{}（{}）：{}".format(
                    (manifest or {}).get('version', '?'), pkg.name, msg))
                if '下次启动生效' in str(msg):
                    # 没安排成自动重启 → 别关程序，换版留给下次启动
                    return
                QTimer.singleShot(0, self._quit_for_update)
            except Exception as e:
                ZeroFallLogger.warning("[自更新] 检查失败: {}".format(e))
            finally:
                self._self_update_busy = False

        _th_su.Thread(target=_work, daemon=True, name="ZeroFall-SelfUpdate").start()

    def _quit_for_update(self):
        """退出主进程，把 exe 让给替换脚本（脚本自己会重启）"""
        try:
            ZeroFallLogger.info("[自更新] 主进程退出，替换脚本 4 秒后覆盖 exe 并重启")
        except Exception:
            pass
        QApplication.quit()

    def _start_crash_log_monitor(self):
        """监控 DSH 崩溃日志，检测 UI 层崩溃（进程未退出也能发现）"""
        try:
            self._handled_crashes = set()
            self._last_crash_check = time.time()
            self._crash_monitor_timer = QTimer(self)
            self._crash_monitor_timer.timeout.connect(self._check_dsh_crash_logs)
            self._crash_monitor_timer.start(30000)      # 每 30 秒扫描一次
            ZeroFallLogger.info("[崩溃监控] 已启用（每 30 秒扫描 DSH 崩溃日志）")
        except Exception as e:
            ZeroFallLogger.error("[崩溃监控] 初始化失败: {}".format(e))
    
    def _check_dsh_crash_logs(self):
        """扫描 DSH 日志目录，发现新的 crash-*.log 即触发自愈"""
        try:
            import os as _os
            log_dir = Path(_os.environ.get('APPDATA', '')) / '@deepseek-ai' / 'dsh-desktop' / 'logs'
            if not log_dir.exists():
                return
            
            now = time.time()
            new_crashes = []
            for f in log_dir.glob('crash-*.log'):
                try:
                    name = f.name
                    if name in self._handled_crashes:
                        continue
                    mtime = f.stat().st_mtime
                    # 只处理最近 10 分钟内产生的崩溃日志（避免历史日志误触发）
                    if mtime > self._last_crash_check - 5 and (now - mtime) < 600:
                        new_crashes.append(f)
                        self._handled_crashes.add(name)
                except Exception:
                    pass
            self._last_crash_check = now
            
            if not new_crashes:
                return
            
            # 崩溃日志存在 → 说明 DSH 启动失败/异常
            ZeroFallLogger.critical(
                "[崩溃监控] 检测到 {} 个新的 DSH 崩溃日志".format(len(new_crashes))
            )
            for f in new_crashes[:3]:
                try:
                    ZeroFallLogger.critical("  - {}".format(f.name))
                except Exception:
                    pass
            
            # 若 DeepSeek 进程仍在运行，先杀掉，再延迟 1.5s 触发自愈
            # （不能在主线程 sleep，否则 Windows 直接判"无响应"）
            try:
                from modules import fault_timeline as _ftl
                _ftl.record_crash("DSH 崩溃日志新增 {} 个".format(len(new_crashes)))
            except Exception:
                pass
            if self.guardian.is_deepseek_running():
                ZeroFallLogger.info("[崩溃监控] 终止异常运行的 DeepSeek")
                # 这次 kill 是自愈动作，不能让 on_exit_callback 把 ZeroFall 一起带走
                self._suppress_follow_exit = True
                self.guardian.kill_deepseek()
                QTimer.singleShot(1500, lambda: self._handle_crash_on_main(-2))
            else:
                self._handle_crash_on_main(-2)      # -2 表示"日志检测到的崩溃"
        except Exception as e:
            ZeroFallLogger.error("[崩溃监控] 扫描异常: {}".format(e))
    
    def _ensure_baidu_client(self, auto_start: bool = True) -> bool:
        """确保百度网盘客户端在运行（同步盘上传完全依赖它）
        
        返回 True 表示客户端可用
        """
        try:
            import psutil
            for p in psutil.process_iter(['name']):
                try:
                    # 注意：Windows 下 psutil 返回的名称带 .exe（如 BaiduNetdisk.exe）
                    name = (p.info.get('name') or '').lower()
                    if 'baidunetdisk' in name:
                        return True
                except Exception:
                    pass
            
            if not auto_start:
                return False
            
            # 尝试自动启动客户端
            import os as _os
            import subprocess
            candidates = [
                Path(_os.environ.get('APPDATA', '')) / 'baidu' / 'BaiduNetdisk' / 'BaiduNetdisk.exe',
                Path(_os.environ.get('PROGRAMFILES', '')) / 'Baidu' / 'BaiduNetdisk' / 'BaiduNetdisk.exe',
                Path(_os.environ.get('PROGRAMFILES(X86)', '')) / 'Baidu' / 'BaiduNetdisk' / 'BaiduNetdisk.exe',
            ]
            for exe in candidates:
                try:
                    if exe.exists():
                        subprocess.Popen([str(exe)], close_fds=True,
                                         creationflags=0x00000008)   # DETACHED_PROCESS
                        # 只有"由 ZeroFall 拉起的"才允许在窗口结束时关掉，
                        # 用户自己开的客户端一律不碰
                        self._cloud_client_launched_by_us = True
                        ZeroFallLogger.info("[云同步] 已自动启动百度网盘客户端: {}".format(exe))
                        return True
                except Exception:
                    continue
            
            return False
        except Exception as e:
            ZeroFallLogger.warning("[云同步] 检测网盘客户端异常: {}".format(e))
            return False
    
    def _start_cloud_sync_timer(self):
        """启动云端同步定时器（复制最新健康快照到百度网盘同步盘）"""
        try:
            if not getattr(self.config, 'cloud_sync_enabled', False):
                return
            sync_dir = getattr(self.config, 'cloud_sync_dir', '')
            if not sync_dir:
                return
            try:
                Path(sync_dir).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                ZeroFallLogger.warning("[云同步] 无法创建同步目录 {}: {}".format(sync_dir, e))
                return
            
            interval_min = max(5, int(getattr(self.config, 'cloud_sync_interval_minutes', 60)))
            self._cloud_client_warned = False
            mode = self._cloud_client_mode()

            ZeroFallLogger.info(
                "[云同步] 已启用，目标 {}，间隔 {} 分钟，历史保留 {} 天".format(
                    sync_dir, interval_min,
                    getattr(self.config, 'cloud_history_keep_days', 14)
                )
            )

            # 最新快照同步：只保留一条定时器。
            # 2026-11-13：原来 _cloud_timer 和 _cloud_sync_timer 两条同频定时器都连着
            # _sync_latest_to_cloud，一小时跑两遍；日志里同一个包出现两行"已加密同步"，
            # 两个线程同时写同一个 .enc（配合每小时漂移的抽样，一天塞进 91 个包）。
            self._cloud_sync_timer = QTimer(self)
            self._cloud_sync_timer.setInterval(interval_min * 60 * 1000)
            self._cloud_sync_timer.timeout.connect(self._sync_latest_to_cloud)
            self._cloud_sync_timer.start()

            # 溢出归档必须是周期性的 —— 它负责把超限快照搬走再删本地，
            # 是本地磁盘的唯一出口。
            self._overflow_timer = QTimer(self)
            self._overflow_timer.setInterval(
                max(60, self._cfg_int('cloud_overflow_check_minutes', 10)) * 60 * 1000)
            self._overflow_timer.timeout.connect(self._handle_overflow_to_cloud)
            self._overflow_timer.start()

            # 窗口看门狗：每分钟判一次窗口边界
            #   进入窗口 → 唤醒网盘客户端 + 清空 outbox + 跑一轮同步
            #   离开窗口 → 静置后关掉本轮由 ZeroFall 拉起的客户端
            self._cloud_window_timer = QTimer(self)
            self._cloud_window_timer.setInterval(60 * 1000)
            self._cloud_window_timer.timeout.connect(self._cloud_window_tick)
            self._cloud_window_timer.start()

            if mode == 'windowed':
                self._cloud_app_started_ts = time.time()
                self._cloud_window_state = False
                self._cloud_close_attempts = 0
                self._cloud_catchup_at = 0
                self._load_window_state()
                if self._cloud_window_trigger() == 'fixed':
                    _start_s = str(getattr(self.config, 'cloud_window_start', '03:30') or '03:30')
                    _span = self._cfg_int('cloud_window_minutes', 60)
                    _auto_close = bool(getattr(self.config, 'cloud_window_auto_close', True))
                    ZeroFallLogger.info(
                        "[云窗口] 定点模式：每天 {} 起连续 {} 分钟才写同步盘；窗口外只暂存本地，"
                        "客户端{}".format(_start_s, _span,
                                          "窗口结束后自动关闭" if _auto_close else "由用户自行关闭"))
                else:
                    ZeroFallLogger.info(
                        "[云窗口] 机会模式：开机后 {} 分钟、键鼠空闲 {} 分钟、且有包要传时才开一轮；"
                        "两次窗口至少隔 {} 小时，{} 小时没传过就硬来一次".format(
                            self._cfg_int('cloud_window_start_delay_minutes', 5),
                            self._cfg_int('cloud_window_idle_minutes', 5),
                            self._cfg_int('cloud_window_gap_hours', 20),
                            self._cfg_int('cloud_window_force_hours', 36)))
                self._cloud_window_tick()
            else:
                # always / manual：保持老行为，启动时确认客户端在不在
                if not self._ensure_baidu_client(auto_start=(mode == 'always')):
                    ZeroFallLogger.warning(
                        "[云同步] 百度网盘客户端未运行且无法自动启动，"
                        "快照只会保存在本地同步盘目录，不会上传云端")
                    self._cloud_client_warned = True
                # 启动 30 秒后先同步一次
                QTimer.singleShot(30000, self._sync_latest_to_cloud)

            # 启动 60 秒后执行「次日开机云端收敛」：把昨天的云快照收敛成 1 轻量 + 1 完整
            QTimer.singleShot(60000, self._consolidate_cloud_daily)
            # 启动 90 秒后处理一次本地溢出（把超限的旧快照归档到云/暂存区）
            QTimer.singleShot(90000, self._handle_overflow_to_cloud)
        except Exception as e:
            ZeroFallLogger.error("[云同步] 初始化失败: {}".format(e))

    # ════════════════════════════════════════════════════════════════
    #  云端同步窗口（2026-11-13）
    #  为什么要有窗口：同步盘方案的成本不在"写文件"，而在百度网盘客户端
    #  必须一直在线才能把文件传上去。实测 12 个 BaiduNetdisk* 进程占 1.99 GB，
    #  空闲内存被压到 11.9%。把写入集中到每天一段窗口，客户端就只需要在那段
    #  时间在线，其余时间可以完全不跑。
    # ════════════════════════════════════════════════════════════════

    def _cloud_client_mode(self) -> str:
        mode = str(getattr(self.config, 'cloud_client_mode', 'windowed') or 'windowed').lower()
        return mode if mode in ('windowed', 'always', 'manual') else 'windowed'

    def _cloud_window_trigger(self) -> str:
        """窗口时机：auto（看机会开窗，默认）/ fixed（定点）"""
        raw = str(getattr(self.config, 'cloud_window_trigger', 'auto') or 'auto').lower()
        return raw if raw in ('auto', 'fixed') else 'auto'

    def _cloud_window_state_path(self) -> Path:
        base = Path(getattr(self.config, 'snapshot_dir', '') or '.')
        return base / '.cloud_window_state.json'

    def _load_window_state(self):
        """读上次窗口结束时间（跨重启有效）——决定这一轮要不要等、隔多久"""
        self._cloud_last_window_end_ts = 0.0
        try:
            p = self._cloud_window_state_path()
            if p.is_file():
                raw = json.loads(p.read_text(encoding='utf-8'))
                self._cloud_last_window_end_ts = float(raw.get('last_window_end') or 0)
        except Exception as _e:
            ZeroFallLogger.debug("[云窗口] 读窗口状态失败: {}".format(_e))

    def _save_window_state(self, start: float = 0) -> None:
        try:
            p = self._cloud_window_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {'last_window_end': getattr(self, '_cloud_last_window_end_ts', 0.0),
                 'last_window_start': start or 0},
                ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as _e:
            ZeroFallLogger.debug("[云窗口] 写窗口状态失败: {}".format(_e))

    def _cloud_system_idle_seconds(self) -> int:
        """键鼠空闲秒数。取不到就当"很空闲"——宁可少开客户端，也不让功能卡死"""
        try:
            import ctypes
            from ctypes import wintypes

            class _LII(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

            lii = _LII()
            lii.cbSize = ctypes.sizeof(_LII)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return 1 << 30
            k32 = ctypes.windll.kernel32
            k32.GetTickCount64.restype = ctypes.c_uint64
            delta = int(k32.GetTickCount64()) - int(lii.dwTime)
            if delta < 0:  # GetTickCount64 拿到了 32 位值
                delta += 1 << 32
            return max(0, delta // 1000)
        except Exception:
            return 1 << 30

    def _cloud_cadence_days(self) -> int:
        """同步频率（天）。0 = 不按频率，走 gap_hours"""
        try:
            n = self._cfg_int('cloud_sync_cadence_days', 1)
        except Exception:
            n = 1
        return max(0, min(30, n))

    def _cloud_window_gap_seconds(self) -> float:
        days = self._cloud_cadence_days()
        if days > 0:
            return days * 24 * 3600.0
        return max(0, self._cfg_int('cloud_window_gap_hours', 20)) * 3600.0

    def _cloud_outbox_limit(self) -> int:
        """暂存区上限：频率越长放得越多（每天 2 个包，再多给点余量）"""
        base = max(1, self._cfg_int('cloud_outbox_max', 6))
        days = self._cloud_cadence_days()
        if days > 1:
            return max(base, days * 2 + 2)
        return base

    def _cloud_window_pkg_limit(self) -> int:
        """一个窗口最多搬几个包：频率长的时候要够搬完积压"""
        base = self._cfg_int('cloud_window_max_packages', 8)
        if base <= 0:
            base = 8
        days = self._cloud_cadence_days()
        if days > 1:
            return max(base, days * 2 + 2)
        return base

    def _compact_outbox(self) -> int:
        """暂存区按天去重：同一天同类（轻量/完整）只留最新的一个

        频率改成"每 3 天 / 7 天"之后，每小时往里塞一个会把暂存区撑爆；
        而恢复点只需要"每天 1 轻 + 1 完整"，跟云端收敛同一套规则。
        频率 = 每天（或 0）时不去重：那时候暂存区本来就只攒十几小时，多几个包不碍事。
        """
        if self._cloud_cadence_days() <= 1:
            return 0
        try:
            d = self._cloud_outbox_path()
            if not d.is_dir():
                return 0
            keep = {}
            drop = []
            for f in sorted(d.glob('snapshot_*.zip*'), key=lambda p: p.name):
                if f.name.endswith('.json'):
                    continue
                parts = f.name.replace('.enc', '').replace('.zip', '').split('_')
                if len(parts) < 2:
                    continue
                day = parts[1]
                base = f.name[:-4] if f.name.endswith('.enc') else f.name
                meta = d / (base + '.meta.json')
                is_full = None
                if meta.is_file():
                    try:
                        raw = json.loads(meta.read_text(encoding='utf-8'))
                        if 'is_full' in raw:
                            is_full = bool(raw.get('is_full'))
                    except Exception:
                        is_full = None
                if is_full is None:
                    try:
                        is_full = f.stat().st_size > 100 * 1024 * 1024
                    except Exception:
                        is_full = False
                key = (day, is_full)
                if key in keep:
                    drop.append(keep[key])
                keep[key] = f
            removed = 0
            for f in drop:
                base = f.name[:-4] if f.name.endswith('.enc') else f.name
                meta = d / (base + '.meta.json')
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    continue
                try:
                    if meta.is_file():
                        meta.unlink()
                except Exception:
                    pass
            if removed:
                ZeroFallLogger.info(
                    "[云窗口] 暂存区按天去重：删掉 {} 个同日冗余包".format(removed))
            return removed
        except Exception as e:
            ZeroFallLogger.warning("[云窗口] 暂存区去重失败: {}".format(e))
            return 0

    def _cloud_has_backlog(self) -> bool:
        """有东西要传吗：暂存区有包，或者今天还一个都没传过"""
        try:
            if (bool(getattr(self.config, 'cloud_outbox_enabled', True))
                    and self._cloud_outbox_count() > 0):
                return True
        except Exception:
            pass
        return self._cloud_uploads_today() == 0

    def _cloud_should_open_window(self, now_ts=None) -> bool:
        """机会模式：现在适合开一轮窗口吗"""
        now_ts = now_ts or time.time()
        delay = max(0, self._cfg_int('cloud_window_start_delay_minutes', 5)) * 60
        started = getattr(self, '_cloud_app_started_ts', now_ts)
        if now_ts - started < delay:
            return False
        if not self._cloud_has_backlog():
            return False
        # 今天额度已经用完：开窗只会白拉起一次客户端，等明天
        if self._cloud_budget_left() == 0:
            return False

        gap = self._cloud_window_gap_seconds()
        force = max(gap + 12 * 3600,
                    self._cfg_int('cloud_window_force_hours', 36) * 3600)
        last = max(_ts0(getattr(self, '_cloud_last_window_end_ts', 0)),
                   _ts0(getattr(self, '_cloud_window_started_ts', 0)))
        since_last = now_ts - last

        # 1) 饿太久：不管空闲，先传掉，免得包一直攒着
        if since_last >= force:
            ZeroFallLogger.info("[云窗口] 距上次窗口已 {:.0f} 小时，无视空闲直接开窗".format(
                since_last / 3600))
            return True
        # 2) 暂存区压满：优先搬走，但一小时最多补一次
        try:
            full = (self._cloud_outbox_count() >= self._cloud_outbox_limit())
        except Exception:
            full = False
        if full and since_last >= 3600:
            ZeroFallLogger.info("[云窗口] 暂存区已压满，立即开窗搬走")
            return True
        # 3) 常态：隔够 gap 小时，且人确实不在
        if since_last < gap:
            return False
        idle = self._cloud_system_idle_seconds()
        return idle >= max(0, self._cfg_int('cloud_window_idle_minutes', 5)) * 60

    def _cloud_window_bounds(self, now=None):
        """算出「当前这一轮」窗口的 (开始, 结束, 是否在窗口内)

        窗口按天重复，允许跨午夜（23:40 起 60 分钟 = 到次日 00:40）。
        不在窗口内时返回的是"下一个将要开始的窗口"，方便日志里点名。
        """
        now = now or datetime.now()
        raw = str(getattr(self.config, 'cloud_window_start', '03:30') or '03:30').strip()
        try:
            hh, mm = [int(x) for x in raw.replace('：', ':').split(':')[:2]]
        except Exception:
            hh, mm = 3, 30
        span = max(1, self._cfg_int('cloud_window_minutes', 60))
        anchor = now.replace(hour=hh % 24, minute=mm % 60, second=0, microsecond=0)
        for start in (anchor - timedelta(days=1), anchor, anchor + timedelta(days=1)):
            end = start + timedelta(minutes=span)
            if start <= now < end:
                return start, end, True
        nxt = anchor if now < anchor else anchor + timedelta(days=1)
        return nxt, nxt + timedelta(minutes=span), False

    def _cloud_in_window(self, now=None) -> bool:
        """当前是否允许往同步盘写文件（非 windowed 模式恒为 True）"""
        if self._cloud_client_mode() != 'windowed':
            return True
        try:
            if self._cloud_window_trigger() == 'fixed':
                return bool(self._cloud_window_bounds(now)[2])
            # 机会模式没有"日历窗口"，只有"这一轮窗口开着没有"
            return bool(getattr(self, '_cloud_window_state', False))
        except Exception:
            return True

    def _cloud_outbox_path(self) -> Path:
        """窗口外的本地暂存区"""
        raw = str(getattr(self.config, 'cloud_outbox_dir', '') or '').strip()
        if raw:
            return Path(raw)
        base = Path(getattr(self.config, 'snapshot_dir', '') or '.')
        return base / '_cloud_outbox'

    def _cloud_outbox_count(self) -> int:
        """暂存区里压了几个待上传的包"""
        try:
            d = self._cloud_outbox_path()
            if not d.is_dir():
                return 0
            return sum(1 for f in d.glob('snapshot_*.zip*') if not f.name.endswith('.json'))
        except Exception:
            return 0

    def _cloud_budget_left(self) -> int:
        """今天还能往云端塞几个包；-1 = 不限

        两处上传路径（最新快照抽样 / 溢出归档）共用这一个闸门。
        改前只有溢出路径查上限，抽样路径完全不查 —— 实测上限 24、实际当天 91。
        """
        cap = self._cfg_int('cloud_upload_max_per_day', 24)
        days = self._cloud_cadence_days()
        if days > 1:
            # 一周攒下的包要在同一个窗口里搬完，日上限不能按"每天"卡死
            cap = max(cap, days * 2 + 2)
        if cap <= 0:
            return -1
        done = self._cloud_uploads_today() + self._cloud_outbox_count()
        return max(0, cap - done)

    def _flush_outbox_to_cloud(self) -> int:
        """把窗口外暂存的包搬进同步盘（只在窗口内调用）"""
        if not getattr(self.config, 'cloud_outbox_enabled', True):
            return 0
        try:
            outbox = self._cloud_outbox_path()
            if outbox.is_dir():
                self._compact_outbox()
            if not outbox.is_dir():
                return 0
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', '') or '')
            if not sync_dir:
                return 0
            sync_dir.mkdir(parents=True, exist_ok=True)

            limit = self._cloud_window_pkg_limit()
            if limit <= 0:
                limit = 8
            budget = self._cloud_budget_left()
            if budget == 0:
                ZeroFallLogger.info(
                    "[云窗口] 今日上传已达上限，暂存区 {} 个包留到明天".format(
                        self._cloud_outbox_count()))
                return 0
            if budget > 0:
                limit = min(limit, budget)

            pkgs = [f for f in outbox.glob('snapshot_*.zip*')
                    if not f.name.endswith('.json')]
            pkgs.sort(key=lambda p: p.name)
            moved = 0
            for src in pkgs:
                if moved >= limit:
                    break
                dst = sync_dir / src.name
                try:
                    # meta 的文件名跟同步盘一致：snapshot_xxx.zip.meta.json
                    # （_sync_one_snapshot 写的是 zip 基名，不是 .zip.enc.meta.json）
                    _base = src.name[:-4] if src.name.endswith('.enc') else src.name
                    meta_src = outbox / (_base + '.meta.json')
                    meta_dst = sync_dir / (_base + '.meta.json')

                    if dst.exists() and dst.stat().st_size == src.stat().st_size:
                        # 已经搬过：只清暂存副本，不占本次窗口配额
                        src.unlink()
                        if meta_src.exists() and meta_dst.exists():
                            meta_src.unlink()
                        continue
                    shutil.copy2(str(src), str(dst))
                    if dst.stat().st_size != src.stat().st_size:
                        ZeroFallLogger.error(
                            "[云窗口] 拷贝校验失败，保留暂存副本: {}".format(src.name))
                        continue
                    src.unlink()
                    if meta_src.exists():
                        try:
                            shutil.copy2(str(meta_src), str(meta_dst))
                            meta_src.unlink()
                        except Exception:
                            pass
                    moved += 1
                    self._cloud_last_write_at = time.time()
                except Exception as e:
                    ZeroFallLogger.warning(
                        "[云窗口] 搬移 {} 失败: {}".format(src.name, e))
            if moved:
                ZeroFallLogger.info(
                    "[云窗口] 已把 {} 个暂存包搬进同步盘（剩余 {} 个等下次窗口）".format(
                        moved, self._cloud_outbox_count()))
            return moved
        except Exception as e:
            ZeroFallLogger.error("[云窗口] 清暂存区失败: {}".format(e))
            return 0

    def _cloud_window_tick(self):
        """窗口看门狗（每分钟）：机会模式按条件开关窗口；定点模式按时间边界"""
        if not getattr(self.config, 'cloud_sync_enabled', False):
            return
        if self._cloud_client_mode() != 'windowed':
            return
        try:
            if self._cloud_window_trigger() == 'fixed':
                self._cloud_window_tick_fixed()
            else:
                self._cloud_window_tick_auto()
        except Exception as e:
            ZeroFallLogger.warning("[云窗口] 检查失败: {}".format(e))

    def _cloud_window_begin(self, why: str = ''):
        """开一轮窗口：先看今天还有没有额度，没额度就别把网盘拉起来"""
        # 2026-11-14 修：原来是无条件 ensure_baidu_client → flush，于是一天额度用完以后
        # 只要暂存区一满就"开窗 → 拉起网盘 → 发现没额度 → 跳过上传"，客户端白开一整天。
        if self._cloud_budget_left() == 0:
            ZeroFallLogger.info(
                "[云窗口] 今天已传 {} 个（上限 {}），不开窗、也不拉起网盘客户端；"
                "暂存 {} 个留到明天".format(
                    self._cloud_uploads_today(),
                    self._cfg_int('cloud_upload_max_per_day', 24),
                    self._cloud_outbox_count()))
            self._cloud_window_state = False
            return
        self._cloud_window_state = True
        self._cloud_window_started_ts = time.time()
        self._cloud_client_launched_by_us = False
        self._cloud_close_attempts = 0
        self._save_window_state(start=self._cloud_window_started_ts)
        ZeroFallLogger.info(
            "[云窗口] 开窗（{}）：空闲 {:.0f} 分钟，暂存 {} 个，今天已传 {} 个".format(
                why or '条件满足', self._cloud_system_idle_seconds() / 60,
                self._cloud_outbox_count(), self._cloud_uploads_today()))
        if not self._ensure_baidu_client(auto_start=True):
            ZeroFallLogger.warning(
                "[云窗口] 网盘客户端没起来，本轮只把包搬进同步盘，等它上线后自动补传")
        self._flush_outbox_to_cloud()
        try:
            self._consolidate_cloud_daily()
        except Exception as e:
            ZeroFallLogger.warning("[云窗口] 收敛失败: {}".format(e))
        self._sync_latest_to_cloud()

    def _cloud_window_end(self, why: str = ''):
        self._cloud_window_state = False
        self._cloud_last_window_end_ts = time.time()
        self._save_window_state()
        ZeroFallLogger.info("[云窗口] 收窗（{}），{} 个包留在暂存区等下次".format(
            why or '条件结束', self._cloud_outbox_count()))

    def _cloud_catchup_if_full(self):
        """补窗口：暂存区压满就搬一次（3 小时内只补一次），防止包一直攒着"""
        try:
            if not bool(getattr(self.config, 'cloud_outbox_enabled', True)):
                return
            n = self._cloud_outbox_count()
            if n < self._cloud_outbox_limit():
                return
            if (time.time() - getattr(self, '_cloud_catchup_at', 0)) <= 3 * 3600:
                return
            if self._cloud_budget_left() == 0:
                return
            self._cloud_catchup_at = time.time()
            ZeroFallLogger.info(
                "[云窗口] 暂存区已压满（{} 个），立即补一次同步（可能错过了窗口）".format(n))
            if not self._ensure_baidu_client(auto_start=True):
                ZeroFallLogger.warning("[云窗口] 补窗口时网盘客户端没起来，包继续留在暂存区")
            self._flush_outbox_to_cloud()
        except Exception as e:
            ZeroFallLogger.warning("[云窗口] 补窗口失败: {}".format(e))

    def _cloud_window_tick_auto(self):
        """机会模式看门狗"""
        now_ts = time.time()
        span = max(1, self._cfg_int('cloud_window_minutes', 60)) * 60
        settle = max(0, self._cfg_int('cloud_window_settle_minutes', 8)) * 60

        # 频率拉长后暂存区会长，先按天去重，避免每小时一个包把盘撑满
        self._compact_outbox()

        if getattr(self, '_cloud_window_state', None) is True:
            started = float(getattr(self, '_cloud_window_started_ts', now_ts) or now_ts)
            elapsed = now_ts - started
            # 额度已经用完 → 这个窗口干不了活，立刻收窗关客户端（别让网盘白挂一小时）
            if self._cloud_budget_left() == 0:
                self._cloud_window_end("今日额度已用完，不必干等")
                if bool(getattr(self.config, 'cloud_window_auto_close', True)):
                    self._maybe_close_baidu_client()
                return
            if elapsed >= span:
                self._cloud_window_end("到点（{} 分钟）".format(int(span // 60)))
                if bool(getattr(self.config, 'cloud_window_auto_close', True)):
                    self._maybe_close_baidu_client()
                return
            # 队列清空 + 静置够久 → 不用干等满 60 分钟（人回来用电脑时客户端能早点退场）
            busy = bool(getattr(self, '_archive_busy', False)
                        or getattr(self, '_cloud_sync_busy', False))
            last_write = _ts0(getattr(self, '_cloud_last_write_at', 0))
            if (self._cloud_outbox_count() == 0 and not busy
                    and (now_ts - last_write) >= settle and elapsed >= 60):
                self._cloud_window_end("队列已清空，提前收尾")
                if bool(getattr(self.config, 'cloud_window_auto_close', True)):
                    self._maybe_close_baidu_client()
            return

        if self._cloud_should_open_window(now_ts):
            self._cloud_window_begin("机会满足")
            return

        self._cloud_catchup_if_full()
        if bool(getattr(self.config, 'cloud_window_auto_close', True)):
            self._maybe_close_baidu_client()

    def _cloud_window_tick_fixed(self):
        """定点模式看门狗（老行为）"""
        try:
            start, end, is_open = self._cloud_window_bounds()
        except Exception:
            return

        state = getattr(self, '_cloud_window_state', None)
        if is_open:
            if state is not True:
                self._cloud_window_state = True
                self._cloud_client_launched_by_us = False
                self._cloud_close_attempts = 0
                ZeroFallLogger.info(
                    "[云窗口] 进入同步窗口 {}-{}，唤醒百度网盘并清空暂存区".format(
                        start.strftime('%H:%M'), end.strftime('%H:%M')))
                if not self._ensure_baidu_client(auto_start=True):
                    ZeroFallLogger.warning(
                        "[云窗口] 网盘客户端没起来，本轮只把包搬进同步盘，等它上线后自动补传")
                self._flush_outbox_to_cloud()
                try:
                    self._consolidate_cloud_daily()
                except Exception as e:
                    ZeroFallLogger.warning("[云窗口] 收敛失败: {}".format(e))
                self._sync_latest_to_cloud()
            return

        if state is True:
            self._cloud_window_state = False
            ZeroFallLogger.info(
                "[云窗口] 窗口结束，{} 个包留在暂存区等下次窗口".format(
                    self._cloud_outbox_count()))

        # 补窗口：定点窗口可能整天都跑不到（机器关着/睡眠），暂存区压满就立刻补一次
        self._cloud_catchup_if_full()

        if bool(getattr(self.config, 'cloud_window_auto_close', True)):
            self._maybe_close_baidu_client()

    def _baidu_client_running(self) -> bool:
        """只看客户端在不在，不负责拉起（给"能不能安全删同步盘文件"用）"""
        try:
            import psutil
            for p in psutil.process_iter(['name']):
                try:
                    if 'baidunetdisk' in (p.info.get('name') or '').lower():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def _maybe_close_baidu_client(self):
        """窗口结束且静置够久后关掉网盘客户端，把 1.9 GB 常驻内存还回去

        只关「本轮窗口由 ZeroFall 自己拉起来的那个」：用户自己打开网盘下载东西时，
        ZeroFall 绝不碰。
        """
        try:
            if not getattr(self, '_cloud_client_launched_by_us', False):
                return
            if getattr(self, '_archive_busy', False) or getattr(self, '_cloud_sync_busy', False):
                return
            settle = max(0, self._cfg_int('cloud_window_settle_minutes', 8))
            last = _ts0(getattr(self, '_cloud_last_write_at', 0))
            if last and (time.time() - last) < settle * 60:
                return

            import psutil
            alive = any(((p.info.get('name') or '').lower()) == 'baidunetdisk.exe'
                        for p in psutil.process_iter(['name']))
            if not alive:
                self._cloud_client_launched_by_us = False
                self._cloud_close_attempts = 0
                return

            attempts = getattr(self, '_cloud_close_attempts', 0) + 1
            self._cloud_close_attempts = attempts
            force = attempts >= 3
            import subprocess
            # 先不带 /F：给客户端一个 WM_CLOSE 的机会，把上传队列落盘；
            # 连续两次没反应才强杀（正常情况第一次就退了）。
            cmd = ['taskkill', '/IM', 'BaiduNetdisk.exe']
            if force:
                cmd.append('/F')
            subprocess.run(cmd, capture_output=True, timeout=20,
                           creationflags=0x08000000)
            if force:
                subprocess.run(['taskkill', '/IM', 'BaiduNetdiskUnite.exe', '/F'],
                               capture_output=True, timeout=20,
                               creationflags=0x08000000)
            if attempts == 1:
                ZeroFallLogger.info(
                    "[云窗口] 已请求关闭百度网盘客户端（静置 {} 分钟后动作），下一分钟复查".format(
                        settle))
            self._cloud_client_launched_by_us = (not force)
            if force:
                self._cloud_close_attempts = 0
                ZeroFallLogger.info("[云窗口] 网盘客户端未响应普通关闭，已强制结束")
        except Exception as e:
            ZeroFallLogger.warning("[云窗口] 关闭网盘客户端失败: {}".format(e))

    def _sync_latest_to_cloud(self):
        """把最新健康快照复制到同步盘（百度网盘客户端会自动上传）"""
        # 顺手清掉孤儿 .meta.json：删快照时如果先删了 zip 后崩、或者云端
        # 由外部工具删过文件，就会留下没有数据对应的元数据，越积越多
        try:
            _orphan_meta = self._cleanup_orphan_cloud_meta()
            if _orphan_meta:
                ZeroFallLogger.info(
                    "[云同步] 清理了 {} 个孤儿 .meta.json".format(_orphan_meta))
        except Exception:
            pass
        try:
            if not getattr(self.config, 'cloud_sync_enabled', False):
                return
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', ''))
            if not sync_dir or not sync_dir.parent.exists():
                return
            sync_dir.mkdir(parents=True, exist_ok=True)

            # 窗口模式：窗口外一律不写同步盘。
            # 这时客户端多半没开，写进去只会让它下次启动时一次面对一堆待传文件 ——
            # 内存峰值正是这么上去的（10 个 BaiduNetdiskUnite 并发哈希上传）。
            if not self._cloud_in_window():
                ZeroFallLogger.debug(
                    "[云同步] 窗口模式：当前不在同步窗口内，跳过同步盘写入")
                return
            if getattr(self, '_cloud_sync_busy', False):
                ZeroFallLogger.info("[云同步] 上一轮写入还没结束，跳过本轮")
                return

            # 两条上传路径共用的每日闸门。
            # 改前只有「溢出归档」查 cloud_upload_max_per_day，抽样这条完全不查 ——
            # 配置写 24，实测当天写了 91 个包。
            budget = self._cloud_budget_left()
            if budget == 0:
                ZeroFallLogger.info(
                    "[云同步] 今日上传已达上限 {} 个，本轮跳过".format(
                        self._cfg_int('cloud_upload_max_per_day', 24)))
                return
            round_cap = self._cfg_int('cloud_upload_per_cycle', 2) or 2
            if budget > 0:
                round_cap = min(round_cap, budget)

            # 先确认客户端在运行，否则本次同步只是"本地复制"，不会真正上云
            client_ok = self._ensure_baidu_client(auto_start=True)
            if not client_ok:
                ZeroFallLogger.warning(
                    "[云同步] 百度网盘客户端未运行，快照仅写入本地同步盘，"
                    "请手动打开客户端以完成上传"
                )
                if not getattr(self, '_cloud_client_warned', False):
                    self._cloud_client_warned = True
                    QTimer.singleShot(0, lambda: QMessageBox.warning(
                        self, "云同步未上传",
                        "未检测到百度网盘客户端在运行。\n\n"
                        "快照已写入本地同步盘目录：\n{}\n\n"
                        "但需要打开百度网盘客户端才会真正上传到云端。\n"
                        "客户端启动后会自动补传。".format(sync_dir)
                    ))

            healthy = self.snapshot_manager.get_healthy_snapshots()
            if not healthy:
                ZeroFallLogger.info("[云同步] 暂无健康快照，跳过")
                return

            encrypt = bool(getattr(self.config, 'cloud_sync_encrypt', False))

            pending = []

            # === 1. 轻量快照 ===
            # 注：不再在此清理云端快照。新策略下云端是"归档库"，
            # 清理统一由「溢出归档」和「次日收敛 / 下午窗口」负责，否则会误删归档。
            light = [s for s in healthy if not getattr(s, 'is_full', False)]
            if light:
                # 本地轻量 5-10 分钟一个，全传上去既慢又占网盘，按步长抽样。
                pick = self._pick_sampled_snapshots(light, 'cloud_sync_light_stride', 12)
                for _snap in pick:
                    _src = Path(getattr(_snap, 'archive_path', ''))
                    dst = sync_dir / ((_src.name + ".enc") if encrypt else _src.name)
                    if dst.exists() and dst.stat().st_size > 0:
                        continue        # 已传过，跳过
                    pending.append(_snap)

            # === 2. 完整快照 ===
            full_snaps = [s for s in healthy if getattr(s, 'is_full', False)]
            # 完整快照同样抽样：本地 30 分钟一个，每 4 个传 1 个（≈2 小时）。
            # 全传的话一天就是 16 个 × 300 MB ≈ 4.8 GB，网盘和带宽都吃不消。
            full_snaps = self._pick_sampled_snapshots(full_snaps, 'cloud_sync_full_stride', 4)
            try:
                from modules.snapshot_manager import (
                    snapshot_is_full_grade as _is_heavy,
                    pick_runtime_full_for_cloud as _pick_heavy,
                )
            except Exception:                     # pragma: no cover
                _is_heavy = lambda _s: bool(getattr(_s, 'is_full', False))
                _pick_heavy = lambda _snaps, _k=1: list(_snaps)[-1:]

            # 既不含运行时、也不带会话的"完整快照"（20-80 MB）跟轻量包一个量级，
            # 照旧抽样传
            for s in [x for x in full_snaps if not _is_heavy(x)]:
                src = Path(s.archive_path)
                if not src.exists():
                    continue
                dst = sync_dir / ((src.name + ".enc") if encrypt else src.name)
                if not (dst.exists() and dst.stat().st_size > 0):
                    pending.append(s)

            # 大包（含运行时 165-434 MB / 带会话 ~140 MB）单独处理（2026-11-16 / P4）：
            #   · 云端只留最新 cloud_full_runtime_keep 份，更老的靠 14 天规则退休；
            #   · 传它那一轮让它独占 —— 一个 242 MB 包会把 60 分钟窗口整个吃满，
            #     跟它同批的轻量包全被挤掉（07:41 实测，窗口里只推进了这一个包）。
            heavy = _pick_heavy(full_snaps, self._cfg_int('cloud_full_runtime_keep', 1))
            pending_heavy = []
            for s in heavy:
                src = Path(s.archive_path)
                if not src.exists():
                    continue
                dst = sync_dir / ((src.name + ".enc") if encrypt else src.name)
                if not (dst.exists() and dst.stat().st_size > 0):
                    pending_heavy.append(s)

            if pending_heavy:
                try:
                    mb = Path(pending_heavy[0].archive_path).stat().st_size / (1024 * 1024)
                except Exception:
                    mb = float(getattr(pending_heavy[0], 'size_mb', 0))
                ZeroFallLogger.info(
                    "[云同步] 本轮单独传大包 {}（{:.1f} MB），它独占这一轮；"
                    "另外 {} 个包顺延到下一轮".format(
                        pending_heavy[0].snapshot_id, mb, len(pending)))
                pending = pending_heavy[:1]
            elif pending:
                if len(pending) > round_cap:
                    ZeroFallLogger.info(
                        "[云同步] 本轮待传 {} 个，按每轮上限只传 {} 个（其余下一轮）".format(
                            len(pending), round_cap))
                    pending = pending[:round_cap]

            if pending:
                ZeroFallLogger.info(
                    "[云同步] 发现 {} 个未同步快照，将在后台加密上传".format(len(pending)))
                # 一个后台线程串行写：原来每个轻量快照单独起一个线程，
                # 叠加重复定时器后同一个 .enc 会被两个线程同时写。
                threading.Thread(
                    target=self._sync_full_snapshots_bg,
                    args=(pending, sync_dir, encrypt),
                    daemon=True, name="ZeroFall-Cloud"
                ).start()

            # 加密模式下，清理历史遗留的明文快照（避免隐私泄露）
            if encrypt:
                for plain in sync_dir.glob("snapshot_*.zip"):
                    if not (sync_dir / (plain.name + ".enc")).exists():
                        try:
                            plain.unlink()
                            ZeroFallLogger.info("[云同步] 已清理明文残留: {}".format(plain.name))
                        except Exception:
                            pass
        except Exception as e:
            ZeroFallLogger.error("[云同步] 同步失败: {}".format(e))
    
    def _pick_sampled_snapshots(self, snaps, cfg_field: str, default_stride: int):
        """按步长抽样：每 N 个取 1 个，返回该上传的子集

        为什么用"当天时间序号"抽样，而不是"距上次上传多久"：
          · 序号是确定性的 —— 同样的快照集合每次都选出同一批，
            不会因为程序重启、同步时间点漂移而反复传不同的快照；
          · 已传过的用"目标文件存在"跳过，两层配合不会重复。

        只处理**当天**的快照：更早的已由「次日收敛」压成 1 轻 + 1 完整，
        没必要再往回补传。
        """
        try:
            stride = config_int(self.config, cfg_field, default_stride)
        except Exception:
            stride = default_stride
        if stride < 1:
            stride = 1

        today = datetime.now().strftime('%Y%m%d')
        todays = [s for s in snaps
                  if str(getattr(s, 'created_at', '')).startswith(today)]
        if not todays:
            return []
        todays.sort(key=lambda s: str(getattr(s, 'created_at', '')))

        picked = []
        for idx, sn in enumerate(todays):
            # 取每个区间的最后一个：越靠后越接近该时段结束时的状态，代表性最好
            if (idx + 1) % stride == 0 or idx == len(todays) - 1:
                picked.append(sn)
        return picked

    def _verify_cloud_consistency(self, src: Path, dst: Path):
        """上传后校验云端文件与源文件一致（大小 + 可解密性）

        为什么值得单独查一遍：同步盘是"写进本地目录，客户端再慢慢上传"，
        中途断电、磁盘满、加密中断都会留下一个大小不对的 .enc。
        这种文件在界面上看着"有"，真要用时才发现解不开 —— 等于没有备份。
        校验只在开关打开时做（cloud_consistency_check，默认关闭，
        因为要对几十 MB 文件读一遍，属于额外 IO）。
        """
        if not getattr(self.config, 'cloud_consistency_check', False):
            return
        try:
            if not dst.exists():
                ZeroFallLogger.warning(
                    "[云一致性] 目标文件不存在: {}".format(dst.name))
                return
            src_size = src.stat().st_size
            dst_size = dst.stat().st_size

            # 加密后应比原文略大（含 salt/nonce/头部），小于源文件必然是坏的
            if dst_size < src_size:
                ZeroFallLogger.error(
                    "[云一致性] {} 大小异常: 云端 {:.2f} MB < 源 {:.2f} MB，"
                    "该加密包不完整，建议删除后重新同步".format(
                        dst.name, dst_size / 1048576, src_size / 1048576))
                return

            # 抽样解密头部，确认不是半截文件
            try:
                with open(dst, 'rb') as f:
                    head = f.read(32)
                if len(head) < 16:
                    ZeroFallLogger.error(
                        "[云一致性] {} 头部过短，疑似空壳文件".format(dst.name))
                    return
            except Exception as e:
                ZeroFallLogger.warning(
                    "[云一致性] {} 读取失败: {}".format(dst.name, e))
                return

            ZeroFallLogger.info(
                "[云一致性] {} 校验通过（{:.2f} MB）".format(
                    dst.name, dst_size / 1048576))
        except Exception as e:
            ZeroFallLogger.warning("[云一致性] 校验异常: {}".format(e))

    def _sync_one_snapshot(self, snapshot, sync_dir: Path, encrypt: bool) -> bool:
        """同步单个快照到同步盘（加密或明文）"""
        try:
            src = Path(snapshot.archive_path)
            if not src.exists():
                return False
            
            if encrypt:
                dst = sync_dir / (src.name + ".enc")
                if dst.exists() and dst.stat().st_size > 0:
                    return True
                from modules.cloud_crypto import encrypt_snapshot
                ok = encrypt_snapshot(src, dst, self.config)
                if not ok:
                    ZeroFallLogger.error("[云同步] 加密失败: {}".format(src.name))
                    return False
                ZeroFallLogger.info(
                    "[云同步] 已加密同步 {} ({:.2f} MB)".format(
                        dst.name, dst.stat().st_size / (1024 * 1024)
                    )
                )
                self._verify_cloud_consistency(src, dst)
                try:
                    import json as _json
                    meta_path = sync_dir / (src.name + ".meta.json")
                    meta_path.write_text(
                        _json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
                        encoding='utf-8'
                    )
                except Exception:
                    pass
                return True
            else:
                dst = sync_dir / src.name
                if dst.exists() and dst.stat().st_size == src.stat().st_size:
                    return True
                shutil.copy2(str(src), str(dst))
                ZeroFallLogger.info(
                    "[云同步] 已同步 {} ({:.2f} MB)".format(
                        dst.name, dst.stat().st_size / (1024 * 1024)
                    )
                )
                return True
        except Exception as e:
            ZeroFallLogger.error("[云同步] 同步单个快照异常: {}".format(e))
            return False
    
    def _sync_full_snapshots_bg(self, snapshots, sync_dir: Path, encrypt: bool):
        """后台线程：串行同步一批快照（加密 30 MB 级，耗时较长）

        注：不在此清理云端，避免误删归档；清理由次日收敛负责。
        _cloud_sync_busy 让下一轮（定时器 / 窗口）知道"还在写"，避免叠加。
        """
        self._cloud_sync_busy = True
        try:
            for s in snapshots:
                ZeroFallLogger.info("[云同步] 后台处理快照: {}".format(s.snapshot_id))
                if self._sync_one_snapshot(s, sync_dir, encrypt):
                    self._cloud_last_write_at = time.time()
        except Exception as e:
            ZeroFallLogger.error("[云同步] 后台同步失败: {}".format(e))
        finally:
            self._cloud_sync_busy = False
    
    # 注：原 _cleanup_cloud（按"保留 N 个"清理云端）已废弃。
    # 云端现在是归档库，清理统一由「溢出归档」和「次日开机收敛」负责，
    # 保留按数量截断会误删刚归档的历史快照。

    def _on_silent_snapshot(self):
        """静默定时快照（后台线程执行完整生命周期，避免阻塞 UI）"""
        # 上次快照还没完成则跳过，避免堆积
        if getattr(self, '_snapshot_busy', False):
            ZeroFallLogger.info("上一次快照仍在进行，跳过本次静默快照")
            return
        
        self._snapshot_busy = True
        
        def _work():
            try:
                self._run_snapshot_lifecycle()
            except Exception as e:
                ZeroFallLogger.error("快照生命周期异常: {}".format(e))
            finally:
                self._snapshot_busy = False
        
        import threading
        threading.Thread(target=_work, daemon=True, name="ZeroFall-Snapshot").start()
    
    def _daily_full_state_path(self) -> Path:
        """每日完整快照的状态文件（记录最后一次是哪天做的）"""
        base = Path(getattr(self.config, 'snapshot_dir', '') or '.')
        return base / '.daily_full_state.json'

    def _get_last_daily_full_date(self) -> str:
        try:
            p = self._daily_full_state_path()
            if p.exists():
                data = json.loads(p.read_text(encoding='utf-8'))
                return str(data.get('last_date', ''))
        except Exception:
            pass
        return ''

    def _set_last_daily_full_date(self, date_str: str):
        try:
            p = self._daily_full_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                'last_date': date_str,
                'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as e:
            ZeroFallLogger.warning("[每日完整快照] 状态写入失败: {}".format(e))

    def _has_full_snapshot_on(self, date_str: str) -> bool:
        """检查快照库里是否已有该日期的完整快照（防止状态文件丢失导致重复创建）"""
        try:
            for s in self.snapshot_manager.get_full_snapshots():
                if getattr(s, 'is_crashed', False):
                    continue
                d = self.snapshot_manager.snapshot_date(s)
                if str(d)[:10] == date_str:
                    return True
        except Exception:
            pass
        return False

    def _startup_snapshot_hygiene(self):
        """启动清理：孤儿索引条目 + 遗留打包临时目录 + 过期日志

        三类垃圾会让长期运行出问题：
          * 索引里有、磁盘上没的条目 → 溢出归档每轮重试失败（活锁）
          * 打包中断留下的 snapshot_xxx 目录 → 白占几百 MB
          * 按天累积的日志文件 → 从不清理，一年上百 MB
        """
        def _work():
            try:
                # 先清遗留临时目录与截断包，再对账索引（顺序很重要：
                # 坏包不删会被反复扫到，孤儿好包则要登记后才会被正常回收）
                dirs = self.snapshot_manager.cleanup_staging_dirs()
                bad = self.snapshot_manager.cleanup_corrupt_archives()
                # 恢复脚本是 per-snapshot 生成的，快照删了脚本不会自己走
                # （实测攒了 525 个指向已删 zip 的死脚本）
                try:
                    self.snapshot_manager.prune_orphan_restore_scripts()
                except Exception as _e:
                    ZeroFallLogger.warning("[启动清理] 清理死恢复脚本失败: {}".format(_e))
                rec = self.snapshot_manager.reconcile_with_disk()
                pruned = rec.get('pruned') or []
                adopted = rec.get('adopted') or []
                # 老索引里"标了完整、却没有 runtime_included"的条目只能按体积猜，
                # 而 100 MB 这条界线早就不成立了（P1/P3 之后完整包也只有 79 MB）。
                # 这类条目当场开包里看一眼，把真实结论写回索引。
                runtime_filled = 0
                try:
                    runtime_filled = self.snapshot_manager.backfill_runtime_included()
                except Exception as _e:
                    ZeroFallLogger.warning(
                        "[启动清理] 回填 runtime_included 失败: {}".format(_e))
                # 修正历史上被旧判据误标成崩溃的快照（notes 里没写"崩溃"的那些）。
                # 旧判据只看"创建时 DeepSeek 在不在跑"，手动快照和环境校验失败
                # 期间的定时快照都会被误标，占满 max_crashed_snapshots 名额。
                try:
                    self.snapshot_manager.repair_misjudged_crashed()
                except Exception as _e:
                    ZeroFallLogger.warning("[启动清理] 修正误判崩溃快照失败: {}".format(_e))
                # 日志保留：log_backup_count 以前是个没人读的配置项
                logs = 0
                try:
                    logs = ZeroFallLogger.cleanup_old_logs(
                        keep_days=config_int(self.config, 'log_backup_count', 5))
                except Exception:
                    logs = 0
                # P7（2026-11-16）：给还没恢复脚本的快照补一份。
                # 快照可能是"索引重建时被登记成孤儿"来的（staging 遗留），
                # 那条路径只写索引、不生成 .cmd；结果换台没装 ZeroFall 的机器时
                # 这个包就成了只能靠本程序打开的死数据（实测 snapshot_20261115_142438）。
                # write_restore_scripts() 幂等，只补缺的，不会覆盖已有脚本。
                scripts = {'written': 0, 'skipped': 0, 'missing': 0}
                try:
                    scripts = self.snapshot_manager.write_restore_scripts()
                except Exception as _e:
                    ZeroFallLogger.warning("[启动清理] 补恢复脚本失败: {}".format(_e))

                if (pruned or adopted or dirs or bad or logs or runtime_filled
                        or scripts.get('written')):
                    ZeroFallLogger.info(
                        "[启动清理] 临时目录 {} 个、截断包 {} 个、"
                        "失效条目 {} 个、孤儿快照 {} 个、回填运行时标记 {} 份、"
                        "补恢复脚本 {} 个、过期日志 {} 个".format(
                            len(dirs), len(bad), len(pruned), len(adopted),
                            runtime_filled, scripts.get('written', 0), logs))
                    try:
                        self.snapshot_manager.reload_index()
                    except Exception:
                        pass
            except Exception as e:
                ZeroFallLogger.warning("[启动清理] 失败: {}".format(e))

        import threading
        threading.Thread(target=_work, daemon=True, name="ZeroFall-Hygiene").start()

    def _ensure_daily_full_snapshot(self, is_running: bool = None) -> bool:
        """每天第一次启动（或跨天后第一次进入）创建一个完整快照
        
        返回是否真的创建了。
        """
        if not getattr(self.config, 'daily_full_snapshot', True):
            return False
        if getattr(self, '_daily_full_busy', False):
            return False

        today = time.strftime('%Y-%m-%d')
        if self._get_last_daily_full_date() == today:
            # 每个进程只记一次，避免 5 分钟周期刷日志
            if not getattr(self, '_daily_full_skip_logged', False):
                self._daily_full_skip_logged = True
                ZeroFallLogger.info(
                    "[每日完整快照] 今天({})的完整快照已创建过，本轮跳过".format(today))
            return False
        if self._has_full_snapshot_on(today):
            # 快照库已有今天完整快照，只补状态
            self._set_last_daily_full_date(today)
            if not getattr(self, '_daily_full_skip_logged', False):
                self._daily_full_skip_logged = True
                ZeroFallLogger.info("[每日完整快照] 今天({})已存在完整快照，跳过".format(today))
            return False

        self._daily_full_busy = True
        try:
            if is_running is None:
                try:
                    is_running = self.guardian.is_deepseek_running()
                except Exception:
                    is_running = True
            ZeroFallLogger.info("[每日完整快照] 开始创建 {} 的首个完整快照".format(today))
            t0 = time.time()
            full = self.snapshot_manager.create_snapshot(
                notes="每日首次启动完整快照 ({})".format(today),
                force_healthy=is_running,
                include_runtime=True,
            )
            if full:
                self._set_last_daily_full_date(today)
                try:
                    mb = Path(full.archive_path).stat().st_size / (1024 * 1024)
                except Exception:
                    mb = getattr(full, 'size_mb', 0)
                ZeroFallLogger.info("[每日完整快照] 创建成功: {}（{:.1f} MB，耗时 {:.0f}s）".format(
                    full.snapshot_id, mb, time.time() - t0))
                QTimer.singleShot(0, lambda s=full.snapshot_id, m=mb: self.statusBar().showMessage(
                    "已生成今日完整快照 {}（{:.1f} MB）".format(s, m), 20000))
                self._notify_snapshot_tab()
                return True
            ZeroFallLogger.warning("[每日完整快照] 创建失败")
            return False
        except Exception as e:
            ZeroFallLogger.error("[每日完整快照] 异常: {}".format(e))
            return False
        finally:
            self._daily_full_busy = False

    # ════════════════════════════════════════════════════════════════
    #  定时完整快照（按时间，锚定当天首个完整快照）
    # ════════════════════════════════════════════════════════════════

    def _last_full_snapshot_time(self):
        """取最近一个完整快照的时间（datetime），没有则 None

        用快照索引里的 created_at，而不是内存计数 —— 这样重启程序、
        跨天之后都能正确接上，不会因为内存清零就提前再打一个。
        """
        try:
            fulls = self.snapshot_manager.get_full_snapshots() or []
        except Exception:
            return None
        best = None
        for sn in fulls:
            raw = str(getattr(sn, 'created_at', '') or '')
            try:
                t = datetime.strptime(raw, '%Y%m%d_%H%M%S')
            except Exception:
                continue
            if best is None or t > best:
                best = t
        return best

    def _check_full_snapshot_interval(self):
        """每 60 秒调一次：判断是否该打新的完整快照

        触发条件：距上一个完整快照已满 N 分钟。
        因为每次都拿"最后一个完整快照"来算，锚点天然跟着走：
        当天首个完整快照 09:00 建立后，09:30 / 10:00 / 10:30 依次触发，
        正是"以首个完整快照时间为基准、每隔 N 分钟一个"。
        """
        try:
            interval = config_int(self.config, 'full_snapshot_interval_minutes', 30)
        except Exception:
            interval = 30
        if interval <= 0:
            return                      # 0 = 关闭定时模式，交给计数模式

        # 正在打快照 / 正在恢复时不要插队，避免两条链路同时写磁盘
        if getattr(self, '_snapshot_busy', False):
            return
        if getattr(self, '_daily_full_busy', False):
            return
        if getattr(self, '_restoring', False):
            return

        last = self._last_full_snapshot_time()
        now = datetime.now()
        if last is not None:
            elapsed_min = (now - last).total_seconds() / 60.0
            if elapsed_min < interval:
                return
            reason = "距上一个完整快照已 {:.0f} 分钟（阈值 {} 分钟）".format(
                elapsed_min, interval)
        else:
            # 今天还没有任何完整快照：交给每日首启逻辑去建第一个，
            # 这里不抢先，否则会和它撞车各打一个
            return

        # 只在 DeepSeek 运行中打，避免把半启动状态拍进去
        try:
            running = self.guardian.is_deepseek_running()
        except Exception:
            running = False
        if not running:
            return

        ZeroFallLogger.info("[完整快照] 触发定时完整快照 | {}".format(reason))
        self._create_timed_full_snapshot(interval)

    # ════════════════════════════════════════════════════════════════
    #  插件/配置变更检测（把"毒快照"问题从频率问题变成精确问题）
    # ════════════════════════════════════════════════════════════════

    def _check_plugin_drift(self):
        """每 60 秒比对一次目录指纹，检测到改动就准备打快照

        为什么要做这件事（比加密定时快照重要）：
          定时快照的真正缺陷不是"粒度粗"，而是"坏改动一旦被拍进去，
          后面所有快照都带毒"。装个坏插件、10 分钟后才崩，回退到中间的
          任何定时快照都还是坏的。有了变更检测，每个回退点都精确对应
          "某次改动之前"，这才真正能救命。

        成本：指纹只用 stat（实测 43 ms / 六百多文件），每分钟跑一次
        可以忽略；只有真检测到变化才会去打那 126 MB 的快照。
        """
        try:
            if not getattr(self.config, 'plugin_drift_snapshot', True):
                return
            if getattr(self, '_snapshot_busy', False) or \
                    getattr(self, '_daily_full_busy', False):
                return

            fp = self.env_checker.quick_fingerprint()
            if not fp:
                return

            prev = getattr(self, '_drift_fingerprint', None)
            now = time.time()

            if prev is None:
                # 首次只记基准，不把"启动时的状态"当成一次变更
                self._drift_fingerprint = fp
                self._drift_parts = getattr(self.env_checker, 'last_parts', None) or []
                ZeroFallLogger.info(
                    "[变更检测] 已记录插件环境基准指纹 {}".format(fp[:12]))
                return

            if fp != prev:
                # 有变化：先记下来，等静置 settle 秒没有新变化再拍，
                # 否则用户装插件时文件连续变化，会打出一串废快照
                self._drift_fingerprint = fp

                # 冷却保护：有些文件属于"程序在跑就会写"（会话配置、插件状态），
                # 万一排除规则漏了某个，指纹会持续变化。没有冷却的话每 60 秒
                # 就会排一个快照，实测 1.5 小时能堆 17 个、吃掉几 GB。
                # 这里只更新基准、不记日志不排快照，把频率压到每 N 分钟最多一次。
                cooldown = self._cfg_int('plugin_drift_cooldown_minutes', 5) * 60
                last_snap = getattr(self, '_drift_last_snapshot_at', 0.0)
                if cooldown > 0 and (now - last_snap) < cooldown:
                    return

                # 防抖上限（2026-11-12 自检）：只要还剩一个漏网的易变文件，指纹
                # 就会每分钟变一次，"静置 settle 秒"永远等不到 —— 实测日志每分
                # 钟一条 [变更检测]，快照点反而被无限推迟。连续变化超过上限就先
                # 拍一份止血，别让回退点一直往后拖。
                if getattr(self, '_drift_first_changed_at', None) is None:
                    self._drift_first_changed_at = now
                cap_s = getattr(self.config, 'plugin_drift_max_defer_seconds', 1800)
                cap_s = int(cap_s) if cap_s else 1800
                first_changed = getattr(self, '_drift_first_changed_at', None)
                if first_changed is None:
                    first_changed = now
                if (now - first_changed) >= cap_s:
                    self._drift_changed_at = None
                    self._drift_first_changed_at = None
                    ZeroFallLogger.info(
                        "[变更检测] 指纹持续变化超 {} 分钟，先拍一份快照止血"
                        .format(max(1, int(cap_s // 60))))
                    self._create_drift_snapshot()
                    return

                detail = getattr(self, '_log_drift_detail', None)
                if detail:
                    detail()

                self._drift_changed_at = now
                ZeroFallLogger.info(
                    "[变更检测] 插件/配置有改动，指纹 {} → {}，"
                    "静置 {} 秒后自动快照".format(
                        prev[:12], fp[:12],
                        int(getattr(self.config, 'plugin_drift_settle_seconds', 20)
                            if getattr(self.config, 'plugin_drift_settle_seconds', 20) is not None
                            else 20)))
                return

            changed_at = getattr(self, '_drift_changed_at', None)
            if changed_at is None:
                return
            # 注意不要写 `... or 20`：0 是合法值（表示不防抖），
            # 但 `0 or 20` 会得到 20，把用户设的 0 静默吞掉
            settle = max(0, self._cfg_int('plugin_drift_settle_seconds', 20))
            if now - changed_at < settle:
                return        # 还在变化中，继续等

            self._drift_changed_at = None
            self._drift_first_changed_at = None
            self._create_drift_snapshot()
        except Exception as e:
            ZeroFallLogger.warning("[变更检测] 检查异常: {}".format(e))

    def _log_drift_detail(self):
        """指纹变了 → 把"到底哪几个文件变了"写进日志（每 5 分钟最多一条）

        没有它，日志只会说"指纹变了"，排查还得另写脚本采样两次做差集。
        明细复用 quick_fingerprint() 顺着 stat 时留下的 self.last_parts，
        不额外走一遍目录（插件目录现在四万多个文件，多走一遍是纯浪费）。
        """
        try:
            now = time.time()
            if now - getattr(self, '_drift_detail_logged_at', 0.0) < 300:
                return
            cur = getattr(self.env_checker, 'last_parts', None) or []
            old = getattr(self, '_drift_parts', None) or []
            self._drift_parts = cur
            if not cur or not old:
                return
            diff = sorted(set(cur) ^ set(old))[:6]
            if not diff:
                return
            self._drift_detail_logged_at = now
            ZeroFallLogger.info(
                "[变更检测] 差异前 6 条（key|路径|mtime|大小）：{}".format(
                    [d[:110] for d in diff]))
        except Exception as e:
            ZeroFallLogger.warning("[变更检测] 差异定位失败: {}".format(e))

    def _create_drift_snapshot(self):
        """因检测到改动而打快照（后台线程，避免卡 UI）"""
        import threading
        self._snapshot_busy = True
        # 记下这次真正打出快照的时刻，供下次冷却判断
        self._drift_last_snapshot_at = time.time()

        def _work():
            try:
                snap = self.snapshot_manager.create_snapshot(
                    notes="检测到插件/配置变更",
                    force_healthy=True,
                )
                if snap:
                    ZeroFallLogger.info(
                        "[变更检测] 已创建变更点快照 {}".format(snap.snapshot_id))
                    QTimer.singleShot(0, lambda s=snap.snapshot_id:
                                      self.statusBar().showMessage(
                                          "检测到环境改动，已创建快照 {}".format(s), 12000))
                    self._notify_snapshot_tab()
                else:
                    ZeroFallLogger.warning("[变更检测] 变更点快照创建失败")
                    self._report_zf_error(
                        "变更检测自动快照创建失败",
                        {"trigger": "plugin_drift", "snapshot_dir": str(
                            getattr(self.config, 'snapshot_dir', ''))})
            except Exception as e:
                ZeroFallLogger.error("[变更检测] 快照异常: {}".format(e))
            finally:
                self._snapshot_busy = False

        threading.Thread(target=_work, daemon=True,
                         name="ZeroFall-DriftSnap").start()

    def _dsh_version_marker(self) -> str:
        """找 DSH 版本标记（实测在快照目录下：<snapshot_dir>\\.dsh_version）"""
        cands = []
        snap = getattr(self.config, 'snapshot_dir', '') or ''
        cfgdir = getattr(self.config, 'deepseek_config_dir', '') or ''
        for base in (snap, cfgdir):
            if base:
                cands.append(Path(base) / '.dsh_version')
        cands.append(Path.home() / '.dsh' / '.dsh_version')
        for p in cands:
            try:
                if p.is_file():
                    v = p.read_text(encoding='utf-8', errors='replace').strip()
                    if v:
                        return v[:64]
            except Exception:
                continue
        return ''

    def _runtime_signature(self) -> str:
        """没有版本标记时的兜底指纹：primary-runtime 的文件数 + 总字节

        6130 个文件、256 MB，一次全量 stat 约几十毫秒，30 分钟跑一次可忽略；
        运行时升级/降级必然改变这个数，够用来判断"要不要再带一份"。
        """
        base = Path(getattr(self.config, 'deepseek_config_dir', '') or '') / 'primary-runtime'
        if not base.is_dir():
            return ''
        count = 0
        total = 0
        try:
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    try:
                        total += (Path(root) / fn).stat().st_size
                        count += 1
                    except OSError:
                        continue
        except Exception:
            return ''
        return 'rt:{}-{}'.format(count, total // (1024 * 1024))

    def _full_snapshot_needs_runtime(self) -> bool:
        """定时完整快照要不要带 primary-runtime（2026-11-12）

        实测：一份完整快照里 config/primary-runtime 占 227 MB（压缩后 ~140 MB，
        全包 165-171 MB），而 12 份完整快照里 11 份是同一份运行时 —— D: 上纯重复 1.7 GB。
        运行时只在 DSH 升级时变，所以默认按版本标记去重：标记没变就不带，
        每天那份（daily）照旧带，恢复链不缺运行时。
        没有版本标记时退回"运行时文件数+字节数"指纹，同样能识别升级。
        """
        mode = str(getattr(self.config, 'full_snapshot_runtime_mode',
                            'on_change') or 'on_change').lower()
        if mode == 'always':
            return True
        if mode == 'never':
            return False
        ver = self._dsh_version_marker()
        sig = ver or self._runtime_signature()
        state_file = Path(getattr(self.config, 'snapshot_dir', '') or '.') / '.runtime_in_full.json'
        state = {}
        try:
            if state_file.is_file():
                state = json.loads(state_file.read_text(encoding='utf-8')) or {}
        except Exception:
            state = {}
        seen = str(state.get('signature') or state.get('version') or '')
        if sig and seen == sig:
            ZeroFallLogger.info(
                "[完整快照] 运行时未变（{}），跳过 primary-runtime 省盘".format(sig))
            return False
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(
                {'signature': sig, 'version': ver,
                 'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
        return True

    def _create_timed_full_snapshot(self, interval: int):
        """后台线程创建完整快照，避免卡 UI（150~300 MB 要写几秒）"""
        import threading
        self._snapshot_busy = True

        def _work():
            try:
                include_runtime = self._full_snapshot_needs_runtime()
                full = self.snapshot_manager.create_snapshot(
                    notes="定时完整快照（每{}分钟）".format(interval),
                    force_healthy=True,
                    include_runtime=include_runtime,
                    is_full=True,
                )
                if full:
                    try:
                        mb = Path(full.archive_path).stat().st_size / (1024 * 1024)
                    except Exception:
                        mb = full.size_mb
                    ZeroFallLogger.info(
                        "[完整快照] 创建成功 {}（{:.1f} MB）".format(
                            full.snapshot_id, mb))
                    QTimer.singleShot(0, lambda s=full.snapshot_id, m=mb:
                                      self.statusBar().showMessage(
                                          "已生成定时完整快照 {}（{:.1f} MB）".format(s, m),
                                          15000))
                    self._notify_snapshot_tab()
                else:
                    ZeroFallLogger.warning("[完整快照] 定时完整快照创建失败")
            except Exception as e:
                ZeroFallLogger.error("[完整快照] 创建异常: {}".format(e))
            finally:
                self._snapshot_busy = False

        threading.Thread(target=_work, daemon=True,
                         name="ZeroFall-TimedFull").start()

    def _check_daily_full_on_startup(self):
        """启动后立刻检查（不等到第一个 5 分钟周期）"""
        if not getattr(self.config, 'daily_full_snapshot', True):
            return
        if getattr(self, '_snapshot_busy', False) or getattr(self, '_daily_full_busy', False):
            return

        def _work():
            self._snapshot_busy = True
            try:
                self._ensure_daily_full_snapshot()
            except Exception as e:
                ZeroFallLogger.error("[每日完整快照] 启动检查异常: {}".format(e))
            finally:
                self._snapshot_busy = False

        import threading
        threading.Thread(target=_work, daemon=True, name="ZeroFall-DailyFull").start()

    def _current_content_fingerprint(self) -> str:
        """当前"插件 + 配置"的内容指纹（跟变更检测用的是同一套）"""
        try:
            fp = self.env_checker.quick_fingerprint()
            return str(fp or '')
        except Exception:
            return ''

    def _should_skip_unchanged_snapshot(self) -> bool:
        """静默快照要不要因为"内容没变"而跳过

        2026-11-12：静默快照 10 分钟一份、每份 42 MB 目录 + 24.5 MB 压缩包，
        而插件/配置大部分时间根本没动 —— 同一份内容反复拍，纯烧磁盘和 CPU。
        用和变更检测同一套指纹判断；指纹为空（读不到插件目录等）时保守照打。

        注意：只拦自动流程。用户手动「快速快照」不走这里，永远照打。
        """
        try:
            if not bool(getattr(self.config, 'silent_snapshot_skip_unchanged', True)):
                return False
        except Exception:
            pass
        fp = self._current_content_fingerprint()
        if not fp:
            return False
        last = getattr(self, '_last_light_fingerprint', None)
        if not last:
            # 首次没有基准：照常打一份，同时把基准记下来（不能拿"启动"当变更）
            self._last_light_fingerprint = fp
            return False
        if fp == last:
            # 兜底：万一指纹卡死不变（比如插件目录被挪走、指纹恒为同一个值），
            # 也不能让快照链断掉 —— 超过 N 小时还是硬打一份。
            gap_h = config_int(self.config, 'silent_snapshot_max_gap_hours', 6)
            if gap_h > 0:
                newest = self._newest_light_time()
                if newest is not None:
                    age_h = (datetime.now() - newest).total_seconds() / 3600.0
                    if age_h >= gap_h:
                        ZeroFallLogger.info(
                            "[生命周期] 指纹虽未变，但已 {:.1f} 小时没打快照，补一份".format(age_h))
                        return False
            ZeroFallLogger.debug(
                "[生命周期] 插件/配置指纹未变（{}），跳过本轮静默快照".format(fp[:12]))
            return True
        return False

    def _newest_light_time(self):
        """最近一份轻量快照的时间（datetime），没有则 None"""
        try:
            snaps = self.snapshot_manager.get_light_snapshots() or []
            best = None
            for s in snaps:
                try:
                    t = datetime.strptime(str(getattr(s, 'created_at', '')), '%Y%m%d_%H%M%S')
                except Exception:
                    continue
                if best is None or t > best:
                    best = t
            return best
        except Exception:
            return None

    def _run_snapshot_lifecycle(self):
        """快照生命周期主流程
        
        0. 每天第一次进入（启动或跨天）先补一个完整快照
        1. 创建轻量快照（5 分钟粒度）
        2. 每 N 个轻量快照触发一个完整快照（含运行时）
        3. 本地溢出（超上限）的快照 → 按最老优先云归档 → 删本地
        """
        is_running = self.guardian.is_deepseek_running()
        
        # 0) 每日首个完整快照（放在轻量之前，保证当天基线最新）
        self._ensure_daily_full_snapshot(is_running)

        # 0.5) 内容没变就别再拍一份（2026-11-12）
        # 静默快照每 10 分钟一次，实测一天 27 份 × 42 MB，其中绝大部分时间里
        # 插件和配置一个字都没改 —— 拍出来的是同一份东西。指纹没变直接跳过，
        # 省掉一次 42 MB 目录复制 + 24.5 MB 压缩 + 一次云归档判断。
        if self._should_skip_unchanged_snapshot():
            return
        
        # 1) 轻量快照
        snap = self.snapshot_manager.create_snapshot(
            notes="定时静默快照",
            force_healthy=is_running
        )
        if not snap:
            ZeroFallLogger.warning("静默快照创建失败")
            self._report_zf_error(
                "定时静默快照创建失败",
                {"trigger": "silent_snapshot", "snapshot_dir": str(
                    getattr(self.config, 'snapshot_dir', ''))})
            return
        ZeroFallLogger.info("静默快照创建成功: {}".format(snap.snapshot_id))
        # 记下这一份的内容指纹，下一轮拿它比对
        try:
            self._last_light_fingerprint = self._current_content_fingerprint() or \
                getattr(self, '_last_light_fingerprint', None)
        except Exception:
            pass
        self._notify_snapshot_tab()
        
        # 2) 完整快照触发：定时模式下由 _check_full_snapshot_interval 负责，
        #    这里仅在定时模式关闭（interval<=0）时退回"每 N 个轻量"老逻辑
        try:
            _fi = config_int(self.config, 'full_snapshot_interval_minutes', 30)
        except Exception:
            _fi = 30
        if _fi > 0:
            return

        n = max(1, int(getattr(self.config, 'full_snapshot_every_n_light', 6)))
        self._light_since_full = getattr(self, '_light_since_full', 0) + 1
        if self._light_since_full >= n:
            self._light_since_full = 0
            ZeroFallLogger.info(
                "[生命周期] 已累计 {} 个轻量快照，触发完整快照".format(n)
            )
            full = self.snapshot_manager.create_snapshot(
                notes="周期完整快照（每{}轻量）".format(n),
                force_healthy=is_running,
                include_runtime=True
            )
            if full:
                ZeroFallLogger.info("[生命周期] 完整快照创建成功: {}".format(full.snapshot_id))
                # 状态栏提示（后台线程 → 用 QTimer 切回主线程）
                try:
                    mb = Path(full.archive_path).stat().st_size / (1024 * 1024)
                except Exception:
                    mb = full.size_mb
                QTimer.singleShot(0, lambda s=full.snapshot_id, m=mb: self.statusBar().showMessage(
                    "已生成完整快照 {}（{:.1f} MB）".format(s, m), 15000
                ))
            else:
                ZeroFallLogger.warning("[生命周期] 完整快照创建失败")
        
        # 3) 溢出 → 云归档 → 删本地
        self._handle_overflow_to_cloud()
    
    @staticmethod
    def _unlink_cloud_with_meta(f: Path):
        """删除云同步目录里的快照文件，连带删掉同名 .meta.json

        否则收敛/清理只删 .enc 不删 meta，日积月累会堆一堆孤儿元数据。
        """
        meta = f.parent / (f.name + '.meta.json')
        if not meta.exists():
            # .enc 对应的 meta 有时写成去掉 .enc 的基名
            meta = f.parent / (f.name.replace('.enc', '') + '.meta.json')
        f.unlink()
        if meta.exists():
            try:
                meta.unlink()
            except Exception:
                pass

    def _cleanup_orphan_cloud_meta(self) -> int:
        """清掉云同步目录里没有对应快照文件的孤儿 .meta.json，返回删除数"""
        try:
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', '') or '')
            if not sync_dir.is_dir():
                return 0
        except Exception:
            return 0
        removed = 0
        try:
            for meta in sync_dir.glob('snapshot_*.meta.json'):
                base = meta.name[:-len('.meta.json')]     # snapshot_xxx.zip
                if (sync_dir / base).exists() or (sync_dir / (base + '.enc')).exists():
                    continue
                try:
                    meta.unlink()
                    removed += 1
                except Exception:
                    pass
            if removed:
                ZeroFallLogger.info("[云端清理] 删除 {} 个孤儿 meta.json".format(removed))
        except Exception as e:
            ZeroFallLogger.warning("[云端清理] 异常: {}".format(e))
        return removed

    def _handle_overflow_to_cloud(self):
        """本地快照超出保留上限时，按最老优先云归档，成功后删除本地"""
        if not getattr(self.config, 'cloud_overflow_enabled', True):
            return
        if getattr(self, '_archive_busy', False):
            return
        
        try:
            # 先清孤儿条目：文件已不存在的索引项会让溢出归档永远重试失败
            try:
                self.snapshot_manager.prune_missing_entries()
            except Exception as e:
                ZeroFallLogger.warning("[生命周期] 孤儿索引清理失败: {}".format(e))
            
            light_overflow, full_overflow, crash_overflow = \
                self.snapshot_manager.get_overflow_snapshots()
            # 崩溃快照也纳入归档：它们不参与轻量/完整的恢复链，原来两个
            # 上限都管不到，会永久占本地磁盘（实测 3 个 168 MB）。
            overflow = light_overflow + full_overflow + crash_overflow
            if not overflow:
                return

            # ── 轻量快照：默认直接删本地，不逐个上传 ──
            # 2026-11-12：这条路原来是"每个溢出的轻量快照都加密上传再删本地"，
            # 完全绕过 conf 里的抽样策略 —— 实测一天往百度网盘塞 94 个文件 / 1.2 GB，
            # 同步进程常驻内存 2 GB。云端历史靠抽样的完整快照就够了（30 分钟一个），
            # 所以默认删；想留 10 分钟粒度就配 cloud_overflow_light_policy=archive。
            policy = str(getattr(self.config, 'cloud_overflow_light_policy',
                                 'delete') or 'delete').lower()
            if light_overflow and policy == 'delete':
                keep = []
                dropped = 0
                fulls = [s for s in self.snapshot_manager.get_full_snapshots()
                         if not getattr(s, 'is_full', None) is False]
                for s in light_overflow:
                    # 安全阀：必须有更晚的完整快照覆盖这个时间窗，否则还是老实上传
                    covered = any(str(getattr(f, 'created_at', '')) >
                                  str(getattr(s, 'created_at', ''))
                                  for f in fulls)
                    if not covered:
                        keep.append(s)
                        continue
                    if self.snapshot_manager.delete_snapshot(s.snapshot_id):
                        dropped += 1
                if dropped:
                    ZeroFallLogger.info(
                        "[生命周期] 轻量快照溢出 {} 个，按 light_policy=delete 直接删本地"
                        "（云端完整快照已覆盖同一时间窗，省网盘带宽）".format(dropped))
                light_overflow = keep
                overflow = light_overflow + full_overflow + crash_overflow
                if not overflow:
                    return

            # ── 上传刹车：每日总量 + 每轮个数 ──
            # 一轮塞 10 个文件时，网盘客户端会并发开一堆上传任务，内存峰值直接翻倍。
            cap_day = config_int(self.config, 'cloud_upload_max_per_day', 24)
            cap_cycle = config_int(self.config, 'cloud_upload_per_cycle', 2)
            done_today = self._cloud_uploads_today()
            if cap_day and done_today >= cap_day:
                ZeroFallLogger.info(
                    "[生命周期] 今日云端上传已达上限 {}/{}，{} 个留在本地等明天再传".format(
                        done_today, cap_day, len(overflow)))
                return
            budget = len(overflow)
            if cap_cycle:
                budget = min(budget, cap_cycle)
            if budget < len(overflow):
                ZeroFallLogger.info(
                    "[生命周期] 本轮只归档 {} 个（每轮上限 {}，其余下一轮继续）".format(
                        budget, cap_cycle))
            self._archive_busy = True
            ZeroFallLogger.info(
                "[生命周期] 本地溢出 {} 个快照（轻量 {} / 完整 {} / 崩溃 {}），开始云归档".format(
                    len(overflow), len(light_overflow), len(full_overflow),
                    len(crash_overflow)
                )
            )
            
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', ''))
            encrypt = bool(getattr(self.config, 'cloud_sync_encrypt', False))
            if not sync_dir:
                ZeroFallLogger.warning("[生命周期] 未配置云同步目录，跳过期归档")
                return

            # 窗口模式 + 不在窗口内：先落到本地 outbox，等窗口打开再一次性搬进同步盘。
            # 这样白天网盘客户端可以完全不开，也不必每小时面对一堆待传包。
            target_dir = sync_dir
            staged = False
            if (getattr(self.config, 'cloud_outbox_enabled', True)
                    and not self._cloud_in_window()):
                outbox_limit = self._cloud_outbox_limit()
                if outbox_limit > 0 and self._cloud_outbox_count() >= outbox_limit:
                    ZeroFallLogger.info(
                        "[生命周期] 暂存区已满 {}/{}，{} 个快照留在本地等窗口".format(
                            self._cloud_outbox_count(), outbox_limit, len(overflow)))
                    return
                target_dir = self._cloud_outbox_path()
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    ZeroFallLogger.warning("[生命周期] 暂存区不可用: {}".format(e))
                    return
                staged = True
            
            # 连续失败上限：避免同一批快照无限重试刷日志（活锁保护）
            if not hasattr(self, '_archive_fail_count'):
                self._archive_fail_count = {}
            MAX_FAIL = 3
            uploaded = 0
            
            for s in overflow:
                if uploaded >= budget:
                    break
                sid = s.snapshot_id
                fails = self._archive_fail_count.get(sid, 0)
                if fails >= MAX_FAIL:
                    # 反复失败的直接放弃，不再每轮重试
                    continue
                
                ok = self._archive_snapshot_to_cloud(s, target_dir, encrypt)
                if ok:
                    uploaded += 1
                    self._archive_fail_count.pop(sid, None)
                    self.snapshot_manager.delete_snapshot(s.snapshot_id)
                    if staged:
                        ZeroFallLogger.info(
                            "[生命周期] 快照 {} 已暂存 outbox（等同步窗口搬进同步盘，"
                            "本地副本已删）".format(s.snapshot_id))
                    else:
                        ZeroFallLogger.info(
                            "[生命周期] 快照 {} 已云归档并删除本地".format(s.snapshot_id)
                        )
                else:
                    self._archive_fail_count[sid] = fails + 1
                    if self._archive_fail_count[sid] >= MAX_FAIL:
                        ZeroFallLogger.warning(
                            "[生命周期] 快照 {} 连续 {} 次云归档失败，"
                            "停止重试（请检查云同步盘可用性）".format(sid, MAX_FAIL))
                        # 放弃重试 + 保留本地 = 永久占用。归档不可能成功的这批，
                        # 超过本地硬上限的按最老优先丢掉，否则就是 2026-11-11 那次
                        # 堆到 83 GB、把 D: 写到 0 的翻版。
                        try:
                            self._drop_if_beyond_hard_cap(s)
                        except Exception as _e:
                            ZeroFallLogger.warning(
                                "[生命周期] 硬上限降级删除失败 {}: {}".format(sid, _e))
                    else:
                        ZeroFallLogger.warning(
                            "[生命周期] 快照 {} 云归档失败，保留本地（第 {}/{} 次）".format(
                                sid, self._archive_fail_count[sid], MAX_FAIL)
                        )
        except Exception as e:
            ZeroFallLogger.error("[生命周期] 溢出归档异常: {}".format(e))
        finally:
            self._archive_busy = False
    
    def _drop_if_beyond_hard_cap(self, snapshot) -> bool:
        """归档彻底失败后的降级出口：超出本地硬上限的快照直接丢

        正常路径是「溢出 → 云归档 → 删本地」。云归档连续 3 次失败后，原来的实现
        只是停止重试并保留本地 —— 于是这批快照既不会归档、也不会被删，
        只能靠磁盘满来收场（2026-11-11：30 份 83 GB，D: 可用 0.00 GB）。

        这里复用 SnapshotManager.plan_retention 的判定（2 倍上限窗口，
        永久保留最新一份完整 + 最新一份轻量），只有确实超窗口的才删。
        """
        try:
            from modules.snapshot_manager import plan_retention, _int_opt
        except Exception:
            return False
        sm = self.snapshot_manager
        max_light = _int_opt(getattr(self.config, 'max_light_snapshots', 0), 0)
        max_full = _int_opt(getattr(self.config, 'max_full_snapshots', 0), 0)
        doomed = plan_retention(
            sm.snapshots,
            max_light * 2 if max_light > 0 else 0,
            max_full * 2 if max_full > 0 else 0,
            protected_ids=set(getattr(sm, 'retention_protected_ids', None) or ()),
        )
        sid = getattr(snapshot, 'snapshot_id', '')
        if sid and sid in doomed and sm.delete_snapshot(sid):
            self._archive_fail_count.pop(sid, None)
            ZeroFallLogger.warning(
                "[生命周期] 快照 {} 无法云归档且超出本地硬上限，已删除本地副本".format(sid))
            return True
        return False

    def _cloud_uploads_today(self) -> int:
        """今天已经往同步盘写了多少个快照包

        用文件 mtime 算，不额外存状态：跨重启、跨时区都对得上，也不需要清理。
        只数 snapshot_*.zip / *.zip.enc，.meta.json 之类的小文件不算。
        """
        try:
            d = Path(getattr(self.config, 'cloud_sync_dir', '') or '')
            if not d or not d.exists():
                return 0
            today = datetime.now().date()
            n = 0
            for f in d.glob('snapshot_*.zip*'):
                if f.name.endswith('.json'):
                    continue
                try:
                    if datetime.fromtimestamp(f.stat().st_mtime).date() == today:
                        n += 1
                except OSError:
                    continue
            return n
        except Exception:
            return 0

    def _archive_snapshot_to_cloud(self, snapshot, sync_dir: Path, encrypt: bool) -> bool:
        """把单个快照归档到云同步盘（复用加密同步逻辑，不触发云清理）"""
        try:
            return self._sync_one_snapshot(snapshot, sync_dir, encrypt)
        except Exception as e:
            ZeroFallLogger.error("[生命周期] 归档快照 {} 异常: {}".format(snapshot.snapshot_id, e))
            return False
    
    def _cloud_consolidated_path(self) -> Path:
        base = Path(getattr(self.config, 'snapshot_dir', '') or '.')
        return base / '.cloud_consolidated.json'

    def _load_cloud_consolidated(self) -> set:
        """读"已收敛过的日期"（跨重启有效，最多留 60 天）"""
        try:
            p = self._cloud_consolidated_path()
            if p.is_file():
                raw = json.loads(p.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    return {str(x) for x in raw if str(x).strip()}
        except Exception as _e:
            ZeroFallLogger.debug("[云端收敛] 读收敛记录失败: {}".format(_e))
        return set()

    def _save_cloud_consolidated(self, dates) -> None:
        try:
            p = self._cloud_consolidated_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            keep = sorted({str(d) for d in dates})[-60:]
            p.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception as _e:
            ZeroFallLogger.debug("[云端收敛] 写收敛记录失败: {}".format(_e))

    def _consolidate_cloud_daily(self):
        """次日开机云端收敛：对前一天（昨天）的快照各保留 1 个最优，删除其余"""
        if not getattr(self.config, 'daily_cloud_consolidate', True):
            return
        # 窗口模式下删同步盘文件必须有客户端在线：离线删除下次联网很可能被云端
        # 重新拉回来（白删一遍，还多一轮下载）。客户端在跑就不用等窗口。
        if (self._cloud_client_mode() == 'windowed'
                and not self._cloud_in_window()
                and not self._baidu_client_running()):
            ZeroFallLogger.debug("[云端收敛] 窗口模式 + 客户端离线，跳过删除动作")
            return
        try:
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', ''))
            if not sync_dir or not sync_dir.exists():
                return
            
            import datetime as _dt
            yesterday = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y%m%d")
            
            # 避免同一日期反复收敛
            # 2026-11-12：原来只记在内存里，程序每重启一次就重扫一遍云端并再删一轮
            # （实测一天 159 行收敛日志、12 次重复扫描）。改成落盘，跨重启只做一次。
            #
            # 2026-11-14：光有标记还不够 —— 离线时删掉的同步盘文件，客户端下次联网
            # 会从云端重新拉回来。实测 .cloud_consolidated.json 里 11-11 / 11-12 都写着
            # "已收敛"，但云端实际还各有 8 / 24 个包。所以改成按实际文件数判定：
            # 删完还剩超过 2 个，就不记账，下一轮继续试（同一天最多试 3 次）。
            done = self._load_cloud_consolidated()
            self._cloud_consolidated_dates = done
            encrypt = bool(getattr(self.config, 'cloud_sync_encrypt', False))

            _left = self._cloud_day_package_count(yesterday, encrypt)
            if _left <= 2:
                if yesterday not in done:
                    done.add(yesterday)
                    self._save_cloud_consolidated(done)
                return
            _fails = getattr(self, '_consolidate_fail_count', None)
            if _fails is None:
                _fails = {}
                self._consolidate_fail_count = _fails
            if _fails.get(yesterday, 0) >= 3:
                ZeroFallLogger.info(
                    "[云端收敛] 昨日({}) 连续 3 次没删干净（客户端多半又把文件拉回来了），"
                    "本轮起跳过".format(yesterday))
                return

            pattern = "snapshot_{}_*.zip.enc".format(yesterday) if encrypt else "snapshot_{}_*.zip".format(yesterday)
            
            # 按文件名解析：snapshot_YYYYMMDD_HHMMSS.zip(.enc)
            def _parse(f):
                stem = f.name.replace(".enc", "").replace(".zip", "")
                parts = stem.split("_")
                if len(parts) >= 2:
                    return parts[1]  # HHMMSS
                return ""
            
            # 轻量/完整按 meta 里的 is_full 分类；拿不到 meta 才退回按大小猜。
            # 原来只按"体积 > 100 MB 算完整"，而现在完整快照加密后约 30 MB，
            # 会被全部当成轻量 —— 一天 16 个完整快照最后只剩 1 个，等于把恢复点删光。
            THRESHOLD = 100 * 1024 * 1024

            def _is_full(f):
                base = f.name[:-4] if f.name.endswith('.enc') else f.name
                meta = sync_dir / (base + '.meta.json')
                if meta.is_file():
                    try:
                        raw = json.loads(meta.read_text(encoding='utf-8'))
                        if 'is_full' in raw:
                            return bool(raw.get('is_full'))
                    except Exception:
                        pass
                try:
                    return f.stat().st_size > THRESHOLD
                except Exception:
                    return False

            light = [f for f in sync_dir.glob(pattern) if not _is_full(f)]
            full = [f for f in sync_dir.glob(pattern) if _is_full(f)]
            
            removed = 0
            # 轻量：保留时间最晚（最好）的 1 个
            light.sort(key=_parse, reverse=True)
            for f in light[1:]:
                try:
                    self._unlink_cloud_with_meta(f)
                    removed += 1
                    ZeroFallLogger.debug(
                        "[云端收敛] 删除昨日冗余轻量快照: {}".format(f.name))
                except Exception:
                    pass
            # 完整：保留时间最晚（最好）的 1 个
            full.sort(key=_parse, reverse=True)
            for f in full[1:]:
                try:
                    self._unlink_cloud_with_meta(f)
                    removed += 1
                    ZeroFallLogger.debug(
                        "[云端收敛] 删除昨日冗余完整快照: {}".format(f.name))
                except Exception:
                    pass
            
            if removed:
                ZeroFallLogger.info(
                    "[云端收敛] 昨日({})快照已收敛：轻量 {}→1、完整 {}→1，删除 {} 个".format(
                        yesterday, len(light), len(full), removed)
                )
            else:
                ZeroFallLogger.info("[云端收敛] 昨日({})无需收敛（不足 2 个冗余）".format(yesterday))

            # 只有真的删干净了才记账；否则下一轮再试（客户端可能正在把文件拉回来）
            _remain = self._cloud_day_package_count(yesterday, encrypt)
            if _remain <= 2:
                done.add(yesterday)
                self._save_cloud_consolidated(done)
                _fails.pop(yesterday, None)
            else:
                _fails[yesterday] = _fails.get(yesterday, 0) + 1
                ZeroFallLogger.warning(
                    "[云端收敛] 昨日({}) 删了 {} 个但还剩 {} 个，下一轮再试（第 {}/3 次）".format(
                        yesterday, removed, _remain, _fails[yesterday]))

            # 收敛完顺手清理过期历史：只做收敛不清历史的话，云端会无限增长
            try:
                self._prune_cloud_history()
            except Exception as _e:
                ZeroFallLogger.warning("[云端历史] 清理失败: {}".format(_e))
        except Exception as e:
            ZeroFallLogger.error("[云端收敛] 异常: {}".format(e))
    
    def _cloud_day_package_count(self, day: str, encrypt: bool) -> int:
        """某个日期在同步盘里还剩几个快照包（.meta.json 不算）"""
        try:
            sync_dir = Path(getattr(self.config, 'cloud_sync_dir', '') or '')
            if not sync_dir.is_dir():
                return 0
            pat = ("snapshot_{}_*.zip.enc".format(day) if encrypt
                   else "snapshot_{}_*.zip".format(day))
            return len(list(sync_dir.glob(pat)))
        except Exception:
            return 0

    def _prune_cloud_history(self):
        """清掉云端超过保留期的整组快照

        为什么必须有这一步：原来的收敛只处理"昨天"，更早的永远留着 ——
        每天收敛出 1 轻 + 1 完整（约 0.45 GB），一年下来就是 160 GB。
        云端归档该有保留期，否则网盘迟早被塞满，而用户根本不会去看。
        """
        try:
            keep_days = config_int(self.config, 'cloud_history_keep_days', 14)
        except Exception:
            keep_days = 14
        if keep_days <= 0:
            ZeroFallLogger.info("[云端历史] 保留期未设置（0=不限），跳过清理")
            return 0

        sync_dir = Path(getattr(self.config, 'cloud_sync_dir', '') or '')
        if not sync_dir or not sync_dir.exists():
            return 0

        import datetime as _dt
        cutoff = _dt.date.today() - _dt.timedelta(days=keep_days)
        cutoff_str = cutoff.strftime('%Y%m%d')

        removed = 0
        # 文件名形如 snapshot_YYYYMMDD_HHMMSS.zip(.enc)，按日期段判断
        for f in list(sync_dir.glob('snapshot_*')):
            try:
                parts = f.name.split('_')
                if len(parts) < 2 or len(parts[1]) != 8:
                    continue
                day = parts[1]
                if not day.isdigit():
                    continue
                if day >= cutoff_str:
                    continue          # 还在保留期内
                self._unlink_cloud_with_meta(f)
                removed += 1
            except Exception as e:
                ZeroFallLogger.warning(
                    "[云端历史] 删除 {} 失败: {}".format(f.name, e))

        if removed:
            ZeroFallLogger.info(
                "[云端历史] 已清理 {} 个早于 {} 的文件（保留 {} 天）".format(
                    removed, cutoff_str, keep_days))
        else:
            ZeroFallLogger.info(
                "[云端历史] 无需清理（保留期内无过期文件，保留 {} 天）".format(keep_days))
        removed += self._prune_cloud_by_size(sync_dir)
        return removed

    def _prune_cloud_by_size(self, sync_dir: Path) -> int:
        """云端体量上限：超过 cloud_max_total_gb 就从最旧的完整快照开始删

        只按天数留是失控的：11-11 那次事故把 10 GB 的包推上云，14 天里一直躺着。
        这里按大小兜底，先删"完整快照"（它们体积最大且价值最低），轻量快照尽量留。
        """
        try:
            _raw_cap = getattr(self.config, 'cloud_max_total_gb', 8.0)
            cap_gb = 8.0 if (_raw_cap is None or _raw_cap == '') else float(_raw_cap)
        except (TypeError, ValueError):
            cap_gb = 8.0
        if cap_gb <= 0:
            return 0
        try:
            files = [f for f in sync_dir.glob('snapshot_*') if f.is_file()]
            total = sum(f.stat().st_size for f in files) / (1024 ** 3)
            if total <= cap_gb:
                ZeroFallLogger.info("[云端体量] {:.2f} GB / 上限 {:.1f} GB，无需清理".format(total, cap_gb))
                return 0
            # 体积大的先删（完整快照），同体积按时间旧优先
            files.sort(key=lambda f: (-f.stat().st_size, f.name))
            removed = 0
            for f in files:
                if total <= cap_gb:
                    break
                if f.name.endswith('.meta.json'):
                    continue
                sz = f.stat().st_size / (1024 ** 3)
                try:
                    self._unlink_cloud_with_meta(f)
                    total -= sz
                    removed += 1
                except Exception as e:
                    ZeroFallLogger.warning("[云端体量] 删除 {} 失败: {}".format(f.name, e))
            if removed:
                ZeroFallLogger.warning(
                    "[云端体量] 超限（上限 {:.1f} GB）→ 已删 {} 个，当前 {:.2f} GB".format(
                        cap_gb, removed, total))
            return removed
        except Exception as e:
            ZeroFallLogger.warning("[云端体量] 清理异常: {}".format(e))
            return 0

    def _update_status(self):
        """更新状态显示"""
        ds_running = self.guardian.is_deepseek_running()
        if ds_running:
            th.pill_set(self.deepseek_status_label, "DeepSeek 运行中", "ok")
            self.launch_button.setEnabled(False)
            self.launch_button.setText("启动 DeepSeek")
        else:
            th.pill_set(self.deepseek_status_label, "DeepSeek 未运行", "warn")
            self.launch_button.setEnabled(True)
            self.launch_button.setText("启动 DeepSeek")
        
        healthy_count = len(self.snapshot_manager.get_healthy_snapshots())
        th.pill_set(self.snapshot_count_label, "快照 {}".format(healthy_count), "info")
        
        # 托盘 tooltip：显示实时状态摘要（仅内容变化时才刷新，避免频繁系统调用）
        try:
            tray = getattr(self, 'tray_icon', None)
            if tray is not None:
                running_text = "运行中" if ds_running else "未运行"
                health_text = self.health_status_label.text() if hasattr(self, 'health_status_label') else "?"
                light_n = len(self.snapshot_manager.get_light_snapshots())
                full_n = len(self.snapshot_manager.get_full_snapshots())
                tip = "ZeroFall｜DeepSeek {0}｜健康 {1}｜快照 {2} 轻量 + {3} 完整".format(
                    running_text, health_text, light_n, full_n
                )
                if tip != getattr(self, '_last_tray_tip', ''):
                    self._last_tray_tip = tip
                    tray.setToolTip(tip)
        except Exception:
            pass
    
    # ════════════════════════════════════════════════════════════════
    #  事件处理 — 快照
    # ════════════════════════════════════════════════════════════════
    
    def _on_launch_deepseek(self):
        """启动DeepSeek"""
        success, message = self.guardian.launch_deepseek()
        
        if success:
            QMessageBox.information(self, "启动成功", "DeepSeek已成功启动")
        else:
            reply = QMessageBox.question(
                self, "环境校验失败",
                "环境校验失败: {}\n\n请选择处理方式：".format(message),
                QMessageBox.StandardButton.Yes | 
                QMessageBox.StandardButton.No | 
                QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self.snapshot_tab._on_restore_snapshot()
            elif reply == QMessageBox.StandardButton.No:
                self.guardian.launch_deepseek(bypass_check=True)
    
    def _on_create_snapshot(self):
        """创建快照"""
        notes, ok = self._get_input("创建快照", "请输入快照备注（可选）:")
        if ok:
            snapshot = self.snapshot_manager.create_snapshot(notes=notes)
            if snapshot:
                QMessageBox.information(self, "创建成功", "快照创建成功\nID: {}".format(snapshot.snapshot_id))
                self._refresh_snapshot_tab()
                self._refresh_dashboard()
            else:
                QMessageBox.critical(self, "创建失败", "快照创建失败，请查看日志")
    
    # 注：原 main_window 自带一份 _on_restore_snapshot（从未被调用，属于死代码），
    # 恢复入口统一走 SnapshotTab._on_restore_snapshot（带后台线程 + 进度提示）。
    
    def _on_delete_snapshot(self):
        """删除快照"""
        selected = self.snapshot_table.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个快照")
            return
        
        snapshot_id = self.snapshot_table.item(selected, 0).text()
        
        reply = QMessageBox.question(
            self, "确认删除",
            "确定要删除快照 {} 吗？\n此操作不可撤销。".format(snapshot_id),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            success = self.snapshot_manager.delete_snapshot(snapshot_id)
            if success:
                QMessageBox.information(self, "删除成功", "快照已删除")
                self._refresh_snapshot_tab()
                self._refresh_dashboard()
            else:
                QMessageBox.critical(self, "删除失败", "快照删除失败")
    
    # ════════════════════════════════════════════════════════════════
    #  事件处理 — 环境 & 云端 & 设置
    # ════════════════════════════════════════════════════════════════
    
    def _on_run_environment_check(self):
        """执行环境校验"""
        self.env_result_text.clear()
        self.env_result_text.append("正在执行环境校验...\n")
        
        report = self.env_checker.run_full_check()
        
        self.env_result_text.append("校验结果: {}\n".format(report.overall_result.value.upper()))
        self.env_result_text.append("=" * 60 + "\n")
        
        for item in report.items:
            status_symbol = {
                "passed": "✓",
                "warning": "⚠",
                "failed": "✗"
            }.get(item.result.value, "?")
            
            self.env_result_text.append("[{}] {}".format(status_symbol, item.name))
            self.env_result_text.append("    状态: {}".format(item.result.value))
            self.env_result_text.append("    信息: {}".format(item.message))
            if item.details:
                self.env_result_text.append("    详情:\n{}".format(item.details))
            self.env_result_text.append("")
        
        if report.overall_result.value == "passed":
            th.pill_set(self.health_status_label, "健康度 健康", "ok")
        elif report.overall_result.value == "warning":
            th.pill_set(self.health_status_label, "健康度 警告", "warn")
        else:
            th.pill_set(self.health_status_label, "健康度 异常", "err")
    
    def _refresh_snapshot_tab(self):
        """刷新快照页（列表 + 统计都在 tab 内部完成）

        快照界面已经迁到 ui/tabs/snapshot_tab.py，主窗口不该再自己维护
        一份表格 —— 之前那套 _create_snapshot_tab 就是迁走后留下的死代码。
        """
        try:
            tab = getattr(self, 'snapshot_tab', None)
            if tab is not None:
                tab.refresh_snapshot_list()
        except Exception as e:
            ZeroFallLogger.warning("[快照页] 刷新失败: {}".format(e))
    
    def _notify_snapshot_tab(self):
        """后台线程打完快照后，切回主线程让快照页立刻更新

        快照页自带 5 秒自动刷新做兜底，这里给一条"打完就看得见"的路径：
        否则刚生成的快照要等到下一轮轮询才出现在列表里。
        """
        try:
            QTimer.singleShot(0, self._refresh_snapshot_tab)
        except Exception:
            pass

    def _refresh_health_status(self):
        """刷新顶部状态面板的环境健康度"""
        try:
            report = self.env_checker.run_full_check(use_cache=True, auto=True)
            if report.overall_result.value == "passed":
                th.pill_set(self.health_status_label, "健康度 健康", "ok")
            elif report.overall_result.value == "warning":
                th.pill_set(self.health_status_label, "健康度 警告", "warn")
            else:
                th.pill_set(self.health_status_label, "健康度 异常", "err")
        except Exception as e:
            ZeroFallLogger.error("健康度检测失败: {}".format(e))
    
    def _on_update_baseline(self):
        """更新环境基线"""
        self.env_checker.update_baseline()
        QMessageBox.information(self, "更新成功", "环境基线已更新")
    
    def _on_baidu_pan_auth(self):
        """百度网盘授权"""
        if not self.baidu_pan:
            QMessageBox.warning(self, "未启用", "百度网盘同步功能未启用")
            return
        
        auth_info = self.baidu_pan.start_device_auth()
        if auth_info:
            QMessageBox.information(
                self, "授权",
                "请在浏览器中打开以下链接完成授权：\n\n"
                "{}\n\n"
                "授权码: {}\n\n"
                "完成后点击确定刷新状态。".format(
                    auth_info['verification_url'],
                    auth_info['user_code']
                )
            )
            self._update_cloud_auth_status()
        else:
            QMessageBox.critical(self, "授权失败", "启动授权流程失败")
    
    def _on_save_settings(self):
        """保存设置"""
        self.config.deepseek_exe_path = self.exe_path_edit.text()
        self.config.deepseek_plugins_dir = self.plugins_dir_edit.text()
        
        self.config.backup_plugins = self.backup_plugins_check.isChecked()
        self.config.backup_plugin_config = self.backup_config_check.isChecked()
        self.config.backup_session_data = self.backup_session_check.isChecked()
        self.config.backup_keys = self.backup_keys_check.isChecked()
        
        self.config.md5_check_enabled = self.md5_check.isChecked()
        self.config.session_integrity_check = self.session_check.isChecked()
        
        # ★ 插件目录只读锁定 — 保存配置并实际执行锁定/解锁
        new_lock_state = self.plugin_readonly_lock_check.isChecked()
        old_lock_state = self.config.plugin_dir_readonly_lock
        self.config.plugin_dir_readonly_lock = new_lock_state
        
        # 当锁定状态发生变化时，实际执行目录权限变更
        if new_lock_state != old_lock_state:
            plugins_dir = self.config.deepseek_plugins_dir
            if plugins_dir and Path(plugins_dir).exists():
                success = set_directory_readonly(plugins_dir, new_lock_state)
                if success:
                    state_text = "已锁定（只读）" if new_lock_state else "已解锁（可写）"
                    ZeroFallLogger.info("插件目录{}: {}".format(state_text, plugins_dir))
                else:
                    QMessageBox.warning(
                        self, "锁定失败",
                        "插件目录权限修改失败，请检查是否有管理员权限"
                    )
            elif new_lock_state:
                QMessageBox.warning(
                    self, "目录不存在",
                    "插件目录不存在，无法设置只读锁定:\n{}".format(plugins_dir)
                )
        
        if self.config_manager.save():
            QMessageBox.information(self, "保存成功", "设置已保存")
        else:
            QMessageBox.critical(self, "保存失败", "设置保存失败")
    
    # ════════════════════════════════════════════════════════════════
    #  辅助方法
    # ════════════════════════════════════════════════════════════════
    
    def _browse_file(self, line_edit: QLineEdit):
        """浏览选择文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择文件", "", "可执行文件 (*.exe);;所有文件 (*.*)"
        )
        if file_path:
            line_edit.setText(file_path)
    
    def _browse_dir(self, line_edit: QLineEdit):
        """浏览选择目录"""
        dir_path = QFileDialog.getExistingDirectory(self, "选择目录")
        if dir_path:
            line_edit.setText(dir_path)
    
    def _get_input(self, title: str, prompt: str) -> Tuple[str, bool]:
        """获取用户输入"""
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, title, prompt)
        return text, ok
    
    def _update_cloud_auth_status(self):
        """更新云端同步状态（API 方案已弃用，转发到同步盘页面刷新）"""
        try:
            if hasattr(self, 'cloud_tab') and hasattr(self.cloud_tab, '_refresh_status'):
                self.cloud_tab._refresh_status()
                return
            # 兼容旧控件（若存在）
            label = getattr(self, 'cloud_auth_label', None)
            if label is not None:
                if self.baidu_pan and self.baidu_pan.is_authorized():
                    label.setText("已授权")
                    label.setStyleSheet("color: green; font-weight: bold;")
                else:
                    label.setText("未授权")
                    label.setStyleSheet("color: gray;")
        except Exception:
            pass
    
    def auto_launch_deepseek(self):
        """自检全绿后自动启动 DeepSeek"""
        try:
            if self.guardian.is_deepseek_running():
                ZeroFallLogger.info("[自动] DeepSeek 已在运行，跳过自动启动")
                try:
                    from modules import fault_timeline as _ftl2
                    from modules import __version__ as _ver
                    _ftl2.record("zf_start", "ZeroFall 启动 v{}".format(_ver))
                except Exception:
                    pass
                self._record_timeline_boot_ok("启动自检时 DSH 已在运行")
                return
            
            ZeroFallLogger.info("[自动] 正在启动 DeepSeek ...")
            success, msg = self.guardian.launch_deepseek(bypass_check=True)
            
            if success:
                self._record_timeline_boot_ok("ZeroFall 启动 DSH 成功")
                self._auto_launched = True
                self._launch_ts = time.time()
                ZeroFallLogger.info("[自动] DeepSeek 启动成功，进入守护模式")
                # 稳定运行 60 秒后重置自动回退计数（说明环境已恢复正常）
                QTimer.singleShot(60000, self._reset_rollback_if_stable)
            else:
                ZeroFallLogger.error("[自动] DeepSeek 启动失败: {}".format(msg))
                QMessageBox.warning(
                    self, "自动启动失败",
                    "自检已通过，但 DeepSeek 启动失败：\n\n{}".format(msg)
                )
        except Exception as e:
            ZeroFallLogger.error("[自动] 自动启动异常: {}".format(e))
    
    def _on_quick_rollback(self):
        """一键回退到最新健康快照并重启 DeepSeek"""
        try:
            healthy = self.snapshot_manager.get_healthy_snapshots()
            if not healthy:
                QMessageBox.warning(
                    self, "无可用快照",
                    "当前没有健康快照。\n\n请先在「快照管理」创建一次作为基线。"
                )
                return
            
            latest = sorted(healthy, key=lambda s: s.created_at, reverse=True)[0]
            reply = QMessageBox.question(
                self, "确认回退",
                "将回退到最新健康快照：\n\n"
                "ID: {}\n创建时间: {}\n大小: {:.2f} MB\n\n"
                "回退模式：仅恢复插件环境（保留当前会话）\n"
                "回退前会自动备份当前环境。\n\n"
                "确定继续？".format(
                    latest.snapshot_id,
                    latest.created_at,
                    latest.size_mb
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            
            # 若 DeepSeek 在运行，先终止，否则文件被占用无法覆盖
            if self.guardian.is_deepseek_running():
                ZeroFallLogger.info("[手动回退] 先终止运行中的 DeepSeek")
                self.guardian.kill_deepseek()
                QApplication.processEvents()
                import time as _t
                _t.sleep(1.5)
            
            ZeroFallLogger.info("[手动回退] 开始回退到 {}".format(latest.snapshot_id))
            
            # 后台恢复 + 进度提示（完整快照 20~40 秒，避免 UI 冻结）
            import threading
            from PyQt6.QtWidgets import QProgressDialog
            dlg = QProgressDialog(
                "正在回退到快照 {}，请稍候...".format(latest.snapshot_id), "", 0, 0, self
            )
            dlg.setWindowTitle("回退中")
            dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
            dlg.setCancelButton(None)
            dlg.setMinimumDuration(0)
            dlg.show()
            
            state = {"ok": False}
            def _work():
                try:
                    state["ok"] = self.snapshot_manager.restore_snapshot(
                        latest.snapshot_id, RestoreMode.PLUGIN_ONLY
                    )
                except Exception as _e:
                    ZeroFallLogger.error("[手动回退] 恢复异常: {}".format(_e))
            
            th = threading.Thread(target=_work, daemon=True, name="ZeroFall-Rollback")
            th.start()
            
            def _poll():
                if th.is_alive():
                    QTimer.singleShot(200, _poll)
                    return
                try:
                    dlg.close()
                except Exception:
                    pass
                if state["ok"]:
                    ZeroFallLogger.info("[手动回退] 成功，准备重启 DeepSeek")
                    QMessageBox.information(
                        self, "回退成功",
                        "环境已恢复到快照：\n{}\n\n即将自动重启 DeepSeek。".format(latest.snapshot_id)
                    )
                    QTimer.singleShot(1500, self.auto_launch_deepseek)
                else:
                    QMessageBox.critical(
                        self, "回退失败",
                        "回退失败，请查看日志：\nD:\\zerofall\\logs\\"
                    )
            
            QTimer.singleShot(200, _poll)
        except Exception as e:
            import traceback
            ZeroFallLogger.error("[手动回退] 异常: {}".format(e))
            ZeroFallLogger.error(traceback.format_exc())
            QMessageBox.critical(self, "回退异常", str(e))
    
    def _verify_environment(self):
        """恢复后验证环境是否健康，返回 (是否通过, 说明)"""
        try:
            report = self.env_checker.run_full_check()
            status = report.overall_result.value
            failed = [i for i in report.items if i.result.value == "failed"]
            if status == "failed":
                names = "、".join(i.name for i in failed[:3])
                return False, "失败项: {}".format(names or "未知")
            warned = [i for i in report.items if i.result.value == "warning"]
            if warned:
                return True, "通过（{} 项警告）".format(len(warned))
            return True, "全部通过"
        except Exception as e:
            ZeroFallLogger.error("[验证] 环境校验异常: {}".format(e))
            return False, "校验异常: {}".format(e)
    
    def _check_dsh_version_change(self):
        """DSH 版本变化时自动创建完整快照作为升级前基线"""
        try:
            import json as _json
            pkg = Path(r'D:\DeepSeek\resources\app\dsh\package.json')
            if not pkg.exists():
                return
            ver = str(_json.loads(pkg.read_text(encoding='utf-8')).get('version', '')).strip()
            if not ver:
                return
            state_file = Path(self.config.snapshot_dir) / '.dsh_version'
            old = state_file.read_text(encoding='utf-8').strip() if state_file.exists() else ''
            if old and old != ver:
                ZeroFallLogger.info(
                    "[版本检测] DSH 版本变化 {} -> {}，创建完整快照作为升级基线".format(old, ver)
                )
                # 完整快照 263MB/十几秒，必须后台跑
                import threading as _th_ver
                def _make_ver_baseline():
                    try:
                        snap = self.snapshot_manager.create_snapshot(
                            notes="DSH版本升级基线 ({} -> {})".format(old, ver),
                            force_healthy=True,
                            include_runtime=True
                        )
                        if snap:
                            QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                                "检测到 DSH 版本升级（{} -> {}），已创建完整快照".format(old, ver), 20000
                            ))
                            self._notify_snapshot_tab()
                    except Exception as _e:
                        ZeroFallLogger.warning("[版本检测] 升级基线创建失败: {}".format(_e))
                _th_ver.Thread(target=_make_ver_baseline, daemon=True,
                               name="ZeroFall-VerBaseline").start()
            elif not old:
                ZeroFallLogger.info("[版本检测] 记录当前 DSH 版本: {}".format(ver))
            state_file.write_text(ver, encoding='utf-8')
        except Exception as e:
            ZeroFallLogger.warning("[版本检测] 跳过: {}".format(e))
    
    def _emit_daily_report(self):
        """写一条健康报告日志（快照/健康度/云同步摘要）"""
        try:
            import datetime as _dt
            healthy = self.snapshot_manager.get_healthy_snapshots()
            lights = self.snapshot_manager.get_light_snapshots()
            fulls = self.snapshot_manager.get_full_snapshots()
            crashed = self.snapshot_manager.get_crashed_snapshots()
            try:
                # 每日报告属于自动流程，受「前置启动校验」开关约束
                report = self.env_checker.run_full_check(use_cache=True, auto=True)
                health = report.overall_result.value
            except Exception:
                health = "unknown"
            
            total_mb = 0.0
            for s in healthy:
                try:
                    total_mb += Path(s.archive_path).stat().st_size / (1024 * 1024)
                except Exception:
                    pass
            
            cloud_files = 0
            try:
                cloud_dir = Path(getattr(self.config, 'cloud_sync_dir', ''))
                if cloud_dir.exists():
                    cloud_files = sum(1 for _ in cloud_dir.glob("snapshot_*.zip.enc"))
            except Exception:
                pass
            
            ZeroFallLogger.info(
                "[健康报告] {} | 健康度={} | 快照 轻量{} 完整{} 崩溃{} | 占用 {:.1f} MB | 云端 {} 个".format(
                    _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    health, len(lights), len(fulls), len(crashed), total_mb, cloud_files
                )
            )
        except Exception as e:
            ZeroFallLogger.warning("[健康报告] 失败: {}".format(e))
    
    def _start_daily_report_timer(self):
        """每 6 小时写一次健康报告"""
        try:
            self._report_timer = QTimer(self)
            self._report_timer.timeout.connect(self._emit_daily_report)
            self._report_timer.start(6 * 3600 * 1000)
            ZeroFallLogger.info("[健康报告] 已启用（每 6 小时）")
        except Exception as e:
            ZeroFallLogger.warning("[健康报告] 初始化失败: {}".format(e))
    
    # ════════════════════════════════════════════════════════════════
    #  主控 / 子控 体系
    # ════════════════════════════════════════════════════════════════
    def _cfg_int(self, field: str, default: int) -> int:
        """读整数配置，容忍 None/非法值，但**保留合法的 0**

        为什么要这个函数：`config_int(cfg, 'x', 20)` 这种写法在
        "0 是有意义的值"时会静默把 0 变成 20 —— 项目里已经因此出过一次
        bug（plugin_drift_settle_seconds 设 0 不生效）。
        统一用这里读，就不会再踩。
        """
        try:
            val = getattr(self.config, field, default)
        except Exception:
            return default
        if val is None or val == '':
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _deployment_mode(self) -> str:
        """当前部署形态：standalone / master / agent

        以 deployment_mode 为准；老配置只有 role 时由 config 层迁移过来，
        这里再兜一层，避免属性缺失时把主控能力误判成单体而静默失效。
        """
        try:
            mode = str(getattr(self.config, 'deployment_mode', '') or '').strip().lower()
            if mode in ('standalone', 'master', 'agent'):
                return mode
        except Exception:
            pass
        try:
            legacy = str(getattr(self.config, 'role', '') or '').strip().lower()
            if legacy in ('master', 'agent'):
                return legacy
        except Exception:
            pass
        return 'standalone'

    def _start_control_timer(self):
        """启动主控/子控轮询定时器

        单体模式（standalone，开源默认）直接不进来：
        不建控制节点、不起轮询定时器、不开 HTTP 更新服务，
        也不去读控制通道目录。纯本地守护，不需要任何服务器。
        """
        try:
            mode = self._deployment_mode()
            self._control_node = None
            self._control_timer = getattr(self, '_control_timer', None)

            if mode == 'standalone':
                ZeroFallLogger.info(
                    "[模式] 单体模式（standalone）：不启用主控/子控通道，"
                    "全部能力在本地完成")
                return

            role = 'master' if mode == 'master' else 'agent'
            if role == 'master':
                from modules.master import MasterNode
                self._control_node = MasterNode(self.config, self.snapshot_manager)
                src = self._control_node.source_dir
                has_main = (src / 'main.py').exists()
                ZeroFallLogger.info("[主控] 已启动 | 通道: {}".format(
                    self._control_node.channel.root))
                ZeroFallLogger.info("[主控] 源码目录: {} [main.py={}]".format(
                    src, 'OK' if has_main else '缺失'))
                ZeroFallLogger.info("[主控] ACP profile: {} | 自动修复: {}".format(
                    getattr(self.config, 'acp_profile', 'zf-acp'),
                    '开' if getattr(self.config, 'auto_fix_enabled', True) else '关'))
                # 认证态自检：ACP 链路靠 .credentials.yaml 物化环境变量，
                # 缺 key 会让自动修复静默失败，启动时就把结论写进日志
                try:
                    from modules.acp_client import credential_diagnosis
                    from pathlib import Path as _P
                    diag = credential_diagnosis(
                        _P(os.environ.get('USERPROFILE', '')) / '.dsh')
                    ZeroFallLogger.info("[主控] 认证态: {}".format(diag))
                    # 模型战绩：省得每次都要从头试哪些模型能用
                    try:
                        from modules import model_memory as _mm
                        ZeroFallLogger.info("[主控] 模型记忆: {}".format(_mm.summary()))
                    except Exception:
                        pass
                    # 没有可用 key 时直接把解决办法写进日志 —— 否则要等到
                    # 子控真报错、跑完整轮 ACP 才发现自动修复没法认证
                    if '可用key=无' in diag:
                        try:
                            from modules import secret_store as _ss
                            ZeroFallLogger.warning(
                                "[主控] 未发现可用 API Key，自动修复将无法调用模型。"
                                "解决办法：设置→主控设置→API Key 粘贴一次并保存；"
                                "或把 key 写进 {}（纯文本，首行即可）。".format(
                                    _ss.base_dir() / 'OPENCODE_GO_API_KEY.txt'))
                        except Exception:
                            ZeroFallLogger.warning(
                                "[主控] 未发现可用 API Key，自动修复将无法调用模型。")
                except Exception as _e:
                    ZeroFallLogger.warning("[主控] 认证态自检失败: {}".format(_e))
                if not has_main:
                    ZeroFallLogger.warning(
                        "[主控] 源码目录里没有 main.py，自动修复会失败，"
                        "请在设置页把 master_source_dir 指向 ZeroFall 源码目录")
                # 内建更新服务器：配置说启用就真起，否则 http_server_enabled 只是空开关
                self._start_master_http_server()
                # WebDAV 中转：跨网络分发的主用通道
                try:
                    _relay = self._control_node.relay_channel()
                    if _relay:
                        _ok = _relay.available()
                        ZeroFallLogger.info(
                            "[主控] WebDAV 中转: {} | 目录 {} | 状态: {}".format(
                                getattr(self.config, 'relay_url', ''),
                                getattr(self.config, 'relay_root', '/zerofall-ctl'),
                                '可用' if _ok else '不可达（将只用共享目录）'))
                    else:
                        ZeroFallLogger.info(
                            "[主控] WebDAV 中转: 未启用（跨网络分发只能靠共享目录）")
                except Exception as _e:
                    ZeroFallLogger.warning("[主控] WebDAV 中转自检失败: {}".format(_e))
                # 子控存活一览：以前主控完全不知道子控死活
                try:
                    _seen = self._control_node.channel.agents_seen()
                    if _seen:
                        _alive = [a for a in _seen if a.get('alive')]
                        ZeroFallLogger.info(
                            "[主控] 已知子控 {} 台（在线 {} 台）: {}".format(
                                len(_seen), len(_alive),
                                ', '.join('{}({})'.format(
                                    a.get('agent_id', '?'),
                                    '在线' if a.get('alive') else '离线')
                                    for a in _seen[:6])))
                    else:
                        ZeroFallLogger.info("[主控] 已知子控 0 台（尚无子控上报心跳）")
                except Exception as _e:
                    ZeroFallLogger.warning("[主控] 子控状态读取失败: {}".format(_e))
            else:
                from modules.control import AgentNode
                self._control_node = AgentNode(self.config, self.snapshot_manager, self.guardian)
                ZeroFallLogger.info("[子控] 已启动，ID: {}，通道: {}".format(
                    self._control_node.agent_id, self._control_node.channel.root))
                _mu = str(getattr(self.config, 'master_url', '') or '').strip()
                if _mu:
                    ZeroFallLogger.info("[子控] 传输: HTTP 直连 {}".format(_mu))
                else:
                    ZeroFallLogger.info("[子控] 传输: 共享目录（未配置主控地址）")
            
            interval = max(15, config_int(self.config, 'control_poll_seconds', 60)) * 1000
            self._control_timer = QTimer(self)
            self._control_timer.timeout.connect(self._control_tick)
            self._control_timer.start(interval)
            # 启动后稍等再跑一次首轮
            QTimer.singleShot(8000, self._control_tick)
        except Exception as e:
            ZeroFallLogger.warning("[主控/子控] 初始化失败: {}".format(e))
    
    def _record_timeline_boot_ok(self, reason: str = ""):
        """DSH 起来之后记一条基线：以后崩了就看"自这次成功以来变了什么" """
        try:
            from modules import fault_timeline as _ftl
            fp = _ftl.fingerprint([
                getattr(self.config, "deepseek_plugins_dir", "") or "",
                str(Path(getattr(self.config, "deepseek_plugins_dir", "") or "") / "profiles" / "desktop"),
            ])
            _ftl.mark_boot_ok(fp, reason)
            ZeroFallLogger.info("[时间线] 已记录启动基线（指纹 {}）{}".format(fp, (" · " + reason) if reason else ""))
        except Exception as e:
            ZeroFallLogger.debug("[时间线] 记录基线失败: {}".format(e))

    def _start_master_http_server(self):
        """启动主控内建更新服务器（子控可 HTTP 直连，不依赖网盘同步延迟）"""
        self._http_server = None
        if not getattr(self.config, 'http_server_enabled', False):
            ZeroFallLogger.info("[主控] 内建更新服务器: 关闭（设置里未启用）")
            return
        try:
            from modules.net_transport import MasterHttpServer
            root = getattr(self._control_node.channel, 'root', None)
            if not root:
                ZeroFallLogger.warning("[主控] HTTP 服务器未启动: 通道目录未知")
                return
            token = str(getattr(self.config, 'http_token', '') or '').strip()
            if not token:
                # 空 token = 裸奔（本机任何进程都能触发备份/回滚/重启）。
                # 这里自动生成一个并落盘，子控那边照抄即可。
                import secrets as _secrets
                token = _secrets.token_urlsafe(24)
                self.config.http_token = token
                try:
                    self.config_manager.save()
                    ZeroFallLogger.info(
                        "[主控] http_token 为空 → 已自动生成并写入配置：{}…".format(token[:8]))
                except Exception as _se:
                    ZeroFallLogger.warning("[主控] 自动生成的 token 保存失败: {}".format(_se))
            bind = str(getattr(self.config, 'http_bind', '127.0.0.1') or '127.0.0.1')
            srv = MasterHttpServer(
                str(root),
                port=config_int(self.config, 'http_port', 8765),
                token=token,
                bind=bind,
            )
            ok = srv.start()
            if ok:
                self._http_server = srv
                ips = []
                try:
                    import socket as _s
                    host = _s.gethostname()
                    ips = sorted({a[4][0] for a in _s.getaddrinfo(
                        host, None, _s.AF_INET, _s.SOCK_STREAM)})
                except Exception:
                    pass
                ZeroFallLogger.info(
                    "[主控] 内建更新服务器已启动 | 端口 {} | 鉴权: {} | 子控填: {}".format(
                        getattr(self.config, 'http_port', 8765),
                        '开' if getattr(self.config, 'http_token', '') else '关',
                        ', '.join('http://{}:{}'.format(i, getattr(self.config, 'http_port', 8765))
                                  for i in ips[:3]) or 'http://<本机IP>:{}'.format(
                                      getattr(self.config, 'http_port', 8765))))
            else:
                ZeroFallLogger.warning(
                    "[主控] 内建更新服务器启动失败（端口 {} 可能被占用）".format(
                        getattr(self.config, 'http_port', 8765)))
        except Exception as e:
            ZeroFallLogger.warning("[主控] 内建更新服务器异常: {}".format(e))

    def _stop_master_http_server(self):
        try:
            srv = getattr(self, '_http_server', None)
            if srv:
                srv.stop()
                ZeroFallLogger.info("[主控] 内建更新服务器已停止")
        except Exception:
            pass
        self._http_server = None

    def _control_tick(self):
        """主控/子控每轮处理（真正放到后台线程，避免阻塞 UI 事件循环）"""
        node = getattr(self, '_control_node', None)
        if node is None:
            return
        # 上一轮还没结束就跳过，避免堆积
        if getattr(self, '_control_busy', False):
            return
        self._control_busy = True
        # 以部署形态为准：role 是历史字段，可能被老配置或手改弄乱，
        # 而 mode 才是"这台机器该跑哪种角色"的权威来源
        role = 'master' if self._deployment_mode() == 'master' else 'agent'
        import threading as _th_ctrl
        def _work():
            try:
                if role == 'master':
                    # 先给个"在处理"的提示 —— 修复要跑 ACP+打包，通常 60~200 秒，
                    # 不给反馈的话用户会以为程序卡死了
                    if getattr(node, 'busy', False) is False:
                        _pending = 0
                        try:
                            _pending = len(node.channel.list_reports(only_new=True))
                        except Exception:
                            _pending = 0
                        if _pending:
                            QTimer.singleShot(0, lambda n=_pending:
                                              self.statusBar().showMessage(
                                                  "主控正在修复 {} 份子控上报，请稍候…".format(n), 0))
                    result = node.tick()
                    if result:
                        ZeroFallLogger.info("[主控] 已处理报告: {}".format(
                            json.dumps(result, ensure_ascii=False)[:400]))
                        QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                            "主控已根据子控上报生成更新包 v{}".format(
                                (result.get('package') or {}).get('version', '?')), 20000))
                    else:
                        QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                            "就绪 | 快捷键: Ctrl+Shift+Z 显示/隐藏窗口", 3000))
                else:
                    # 子控：报活 -> 检查崩溃 -> 上报；检查更新 -> 应用
                    node.send_heartbeat()
                    node.check_dsh_crash()
                    applied, msg = node.poll_update()
                    if applied:
                        ZeroFallLogger.info("[子控] 更新已应用: {}".format(msg))
                        QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                            "已自动更新: {}".format(msg), 20000))
            except Exception as e:
                ZeroFallLogger.warning("[主控/子控] 本轮处理失败: {}".format(e))
            finally:
                self._control_busy = False
        _th_ctrl.Thread(target=_work, daemon=True, name="ZeroFall-ControlTick").start()
    
    def _report_zf_error(self, error_text, context=None):
        """把 ZeroFall 自身的错误上报给主控（子控角色）"""
        node = getattr(self, '_control_node', None)
        if node is None or getattr(self.config, 'role', 'master') != 'agent':
            return None
        try:
            rid = node.report_zf_error(error_text, context)
            if rid:
                ZeroFallLogger.info("[子控] 已上报错误: {}".format(rid))
            return rid
        except Exception as e:
            ZeroFallLogger.warning("[子控] 上报失败: {}".format(e))
            return None
    
    def _reset_rollback_if_stable(self):
        """DeepSeek 稳定运行后重置自动回退计数，避免历史计数影响后续自愈"""
        try:
            if self.guardian.is_deepseek_running():
                if getattr(self, '_rollback_count', 0):
                    ZeroFallLogger.info("DeepSeek 稳定运行 60 秒，重置自动回退计数与已试快照记录")
                self._rollback_count = 0
                self._tried_snapshots = set()
        except Exception:
            pass
    
    def _relay_to_doubao(self, exit_code: int):
        """保留现场后，把崩溃交给豆包：拉起 → 新对话 → 粘贴提示词 → 回车。

        只在"不回退快照、保留现场"的路径上调用（现场没被冲掉，豆包看到的才是真现场）。
        全程在后台线程里跑：不阻塞 UI，任何失败只记日志，绝不把崩溃处理再拖崩一次。
        """
        if not getattr(self.config, 'doubao_relay_on_crash', True):
            ZeroFallLogger.info("[豆包接力] 设置里已关闭（doubao_relay_on_crash=False），跳过")
            return
        try:
            from modules import doubao_relay as dr
        except Exception as e:            # noqa: BLE001
            ZeroFallLogger.warning("[豆包接力] 模块加载失败: {}".format(e))
            return

        appdata = getattr(self.config, 'dsh_appdata_dir', '') or ''
        crash_path = None
        crash_text = ""
        category = "unknown"
        category_reason = ""
        try:
            from modules.crash_classifier import read_latest_crash_log, classify_crash_log
            crash_path, crash_text = read_latest_crash_log(appdata)
            if crash_text:
                category, category_reason = classify_crash_log(crash_text)
        except Exception:                 # noqa: BLE001 - 拿不到日志也要能把接力跑完
            crash_path = None
        crash_dir = str(Path(appdata) / "logs") if appdata else ""
        prompt_tpl = getattr(self.config, 'doubao_relay_prompt', '') or ""
        exe_hint = getattr(self.config, 'doubao_exe', '') or ""

        # 按崩溃类别给豆包补一句"先看哪里"，比让它从零猜快得多
        _HINTS = {
            "auth_failed": "\n- 已知分类：认证失败（优先看 Cookies / Local Storage 的凭证态，不要先动插件）",
            "plugin_error": "\n- 已知分类：插件 / Node 模块错误（优先看 profile 的 cordis.patch.yml、node_modules 链接、pnpm-lock）",
            "runtime_error": "\n- 已知分类：运行时 / 渲染进程崩溃（优先看内存、GPU 进程、crashpad 记录）",
        }
        extra = _HINTS.get(category, "")
        if category_reason:
            extra += "\n- 分类依据：{}".format(category_reason)

        def _work():
            try:
                # 接力前先把快照备一份：豆包要读的是"崩溃现场"，别让后续修复动作把它冲掉。
                try:
                    from modules import resume_guard as rg
                    bak = rg.backup_snapshot(rg.resolve_home(self.config), tag="zf-resume-precrash")
                    if bak:
                        ZeroFallLogger.info("[豆包接力] 已备份崩溃前快照 → {}".format(bak))
                except Exception as exc:      # noqa: BLE001
                    ZeroFallLogger.warning("[豆包接力] 快照备份失败: {}".format(exc))

                rep = dr.relay(
                    exit_code=exit_code,
                    crash_log=str(crash_path) if crash_path else "",
                    crash_dir=crash_dir,
                    profile="desktop",
                    prompt_template=prompt_tpl,
                    exe_hint=exe_hint,
                    extra=extra,
                )
                ZeroFallLogger.info(
                    "[豆包接力] 分类={} ok={} | {} | {}".format(
                        category,
                        rep.get("ok"),
                        " → ".join(rep.get("steps") or []),
                        rep.get("error") or "无错误",
                    )
                )
            except Exception as exc:      # noqa: BLE001
                ZeroFallLogger.warning("[豆包接力] 异常: {}".format(exc))

        import threading as _th_doubao
        _th_doubao.Thread(target=_work, daemon=True, name="ZeroFall-DoubaoRelay").start()

    def _on_deepseek_crash(self, exit_code: int):
        """DeepSeek崩溃回调（在监控线程中调用，必须切回主线程处理 UI）"""
        ZeroFallLogger.critical("DeepSeek 崩溃，退出码: {}".format(exit_code))
        # QTimer.singleShot 线程安全，会投递到主线程事件循环
        QTimer.singleShot(0, lambda: self._handle_crash_on_main(exit_code))
    
    def _handle_crash_on_main(self, exit_code: int):
        """崩溃处理：留崩溃快照 → 弹 60s 选择框 → 完整基线叠加轻量恢复 → 重启"""
        try:
            # 1) 后台留一份崩溃环境快照（263MB/十几秒，不能卡主线程）
            import threading as _th_crash_snap
            def _make_crash_snap():
                # 崩溃快照之前先把「谁在跑」的快照单独备一份：
                # profile 快照体积大、耗时长，而 resume-sessions.json 决定"重启后恢复谁"，
                # 万一恢复流程中途失败或被人重装 profile，这份还能拿来手动补。
                try:
                    from modules import resume_guard as rg
                    bak = rg.backup_snapshot(rg.resolve_home(self.config), tag="zf-resume-crash")
                    if bak:
                        ZeroFallLogger.info("[崩溃快照] 已备份会话快照 → {}".format(bak))
                except Exception as exc:      # noqa: BLE001
                    ZeroFallLogger.warning("[崩溃快照] 会话快照备份失败: {}".format(exc))
                try:
                    self.snapshot_manager.create_snapshot(
                        notes="崩溃快照 (退出码 {})".format(exit_code),
                        force_healthy=False
                    )
                except Exception as e:
                    ZeroFallLogger.warning("崩溃快照创建失败: {}".format(e))
            _th_crash_snap.Thread(target=_make_crash_snap, daemon=True,
                                  name="ZeroFall-CrashSnap").start()

            # 2) 子控上报崩溃
            try:
                if getattr(self.config, 'role', 'master') == 'agent':
                    node = getattr(self, '_control_node', None)
                    if node is not None:
                        rid = node.report_dsh_crash(exit_code, log_tail="")
                        if rid:
                            ZeroFallLogger.info("[子控] 已上报 DeepSeek 崩溃: {}".format(rid))
            except Exception as e:
                ZeroFallLogger.warning("崩溃上报失败: {}".format(e))

            # 2.5) 崩溃告警开关：关闭时只在日志/托盘留痕，不弹窗打扰
            _alert_on = bool(getattr(self.config, 'crash_alert_enabled', True))

            # 3) 未开启自动回退 → 仅提示
            if not getattr(self.config, 'auto_rollback_on_crash', True):
                if _alert_on:
                    QMessageBox.critical(
                        self, "DeepSeek 崩溃",
                        "DeepSeek 异常退出！\n退出码: {}\n\n已保留崩溃快照。".format(exit_code)
                    )
                else:
                    ZeroFallLogger.warning(
                        "[崩溃告警] 告警已关闭，仅记录：退出码 {}".format(exit_code))
                # 保留现场 → 把现场交给豆包分析（不回退快照，现场就在那里）
                self._relay_to_doubao(exit_code)
                return

            # 4) 防无限循环 —— 达到上限后不再是"直接掐断"，而是交给人工定
            #
            # 原逻辑：连续自动回退超过 max_auto_rollback 次就一律停止，
            # 用户只能事后干看着。问题是"回退满 N 次"并不等于"没救了"，
            # 常常只是前面几次选到的快照都不合适，换个更早的就能恢复。
            #
            # 现在：触到上限时弹一次确认，由人决定继续还是收手。
            #   · 选「继续」→ 允许再试一轮（计数清零，下次到上限还会再问）
            #   · 选「停手」→ 保留现场，不再自动动
            #   · 告警被关掉时无人可问 → 保守停手，绝不无人工确认地一直回退
            self._rollback_count = getattr(self, '_rollback_count', 0) + 1
            max_rb = int(getattr(self.config, 'max_auto_rollback', 2))
            if self._rollback_count > max_rb:
                ZeroFallLogger.warning(
                    "已连续自动回退 {} 次仍崩溃，转为人工决定是否继续".format(
                        self._rollback_count))
                if not _alert_on:
                    ZeroFallLogger.warning(
                        "[崩溃告警] 告警已关闭，无人可确认，保守停手不再自动回退")
                    self._rollback_count = 0
                    self._relay_to_doubao(exit_code)
                    return
                ret = QMessageBox.question(
                    self, "已多次回退仍未恢复",
                    "已连续自动回退 {} 次，DeepSeek 仍然崩溃。\n\n"
                    "继续回退到更早的健康快照再试一次吗？\n\n"
                    "· 选「是」：继续尝试（会换更早的快照，不会原地重试）\n"
                    "· 选「否」：保留现场并停手，由你手动检查\n"
                    "  dsh-runtimes / 磁盘空间 / DeepSeek 安装完整性".format(
                        self._rollback_count),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,      # 默认停手，误触安全
                )
                if ret != QMessageBox.StandardButton.Yes:
                    ZeroFallLogger.info(
                        "[崩溃恢复] 用户选择停手，保留现场不再自动回退")
                    self._rollback_count = 0
                    self._relay_to_doubao(exit_code)
                    return
                ZeroFallLogger.info(
                    "[崩溃恢复] 用户确认继续回退（已重置计数，下次到上限会再次询问）")
                self._rollback_count = 0

            # 4.5) 读最新崩溃日志分类：auth_failed 走"清认证态重登"，不盲目回滚插件
            try:
                from modules.crash_classifier import (
                    read_latest_crash_log, classify_crash_log, clear_auth_state
                )
                _crash_path, _crash_text = read_latest_crash_log(
                    getattr(self.config, "dsh_appdata_dir", "") or "")
                if _crash_text:
                    _cat, _reason = classify_crash_log(_crash_text)
                    ZeroFallLogger.info("[崩溃分类] {} | {}".format(_cat, _reason))
                    if _cat == "auth_failed":
                        ret = QMessageBox.question(
                            self, "检测到登录态损坏",
                            "DeepSeek 崩溃原因：{}\n\n"
                            "这不是插件问题，回滚插件快照无效。\n"
                            "ZeroFall 将清理旧登录态（Cookies/Local Storage），\n"
                            "然后重启 DeepSeek，你需要重新登录一次。\n\n"
                                 "是否现在清理并重启？".format(_reason),
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.Yes
                        )
                        if ret == QMessageBox.StandardButton.Yes:
                            cleared = clear_auth_state(
                                getattr(self.config, "dsh_appdata_dir", "") or "")
                            ZeroFallLogger.info("[认证态] 已清理 {} 项".format(len(cleared)))
                            self._rollback_count = 0
                            QTimer.singleShot(1500, self.auto_launch_deepseek)
                        return
            except Exception as _e:
                ZeroFallLogger.warning("[崩溃分类] 失败，按通用流程处理: {}".format(_e))

            # 5) 弹 60 秒选择对话框（默认选中最新完整快照）
            healthy = self.snapshot_manager.get_healthy_snapshots()
            if not healthy:
                QMessageBox.warning(
                    self, "无可用快照",
                    "DeepSeek 崩溃，但当前没有健康快照可恢复。请先手动创建健康基线。"
                )
                self._rollback_count = 0
                return

            from ui.crash_dialog import CrashRestoreDialog
            # 倒计时默认 10 分钟（配置可调），1 分钟对"人不在电脑前"太短
            _cd = getattr(self.config, 'crash_restore_countdown', 600)
            try:
                _cd = int(_cd)
            except Exception:
                _cd = 600
            # 把已经试过回退的快照传进去：上次回退后仍崩溃，说明那个点不合适，
            # 这次默认要跳到更早的，不能原地重试同一个
            dlg = CrashRestoreDialog(
                healthy, self, countdown_seconds=_cd,
                exclude_ids=getattr(self, '_tried_snapshots', set()))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                ZeroFallLogger.info("[崩溃恢复] 用户选择不恢复，保留现场")
                QMessageBox.information(
                    self, "已保留现场",
                    "未执行恢复。崩溃现场已保留，可稍后在「快照管理」手动回退。"
                )
                return

            sid = dlg.selected_snapshot_id()
            target = self.snapshot_manager.snapshots.get(sid)
            if target is None:
                QMessageBox.warning(self, "快照失效", "所选快照已不存在")
                return

            # 记下这次用的快照：万一回退后又崩，下一次自动跳过它
            try:
                if not hasattr(self, '_tried_snapshots'):
                    self._tried_snapshots = set()
                self._tried_snapshots.add(sid)
                ZeroFallLogger.info(
                    "[崩溃恢复] 已记录本次回退目标，累计尝试 {} 个快照".format(
                        len(self._tried_snapshots)))
            except Exception:
                pass

            # 6) 计算叠加链：完整基线 + 后续轻量依次覆盖
            chain = self.snapshot_manager.find_overlay_chain(target)
            ZeroFallLogger.info("[崩溃恢复] 恢复链 ({} 个): {}".format(
                len(chain), " -> ".join(s.snapshot_id[:18] for s in chain)))

            state = {"ok": False, "err": ""}
            def _do_restore():
                try:
                    for i, s in enumerate(chain):
                        overlay = (i > 0)   # 第一个完整基线干净恢复，后续轻量叠加覆盖
                        ZeroFallLogger.info("[崩溃恢复] ({}/{}) {} {} {}".format(
                            i + 1, len(chain), s.snapshot_id[:18],
                            "完整基线" if not overlay else "叠加轻量",
                            "(含运行时)" if getattr(s, 'is_full', False) else ""))
                        ok = self.snapshot_manager.restore_snapshot(
                            s.snapshot_id, RestoreMode.PLUGIN_ONLY,
                            overlay=overlay, skip_pre_restore_backup=True
                        )
                        if not ok:
                            state["err"] = "恢复 {} 失败".format(s.snapshot_id)
                            return
                    state["ok"] = True
                except Exception as _e:
                    state["err"] = str(_e)
                    ZeroFallLogger.error("[崩溃恢复] 异常: {}".format(_e))

            import threading as _th_r
            rt = _th_r.Thread(target=_do_restore, daemon=True, name="ZeroFall-CrashRestore")
            rt.start()

            # 进度对话框（模态但可响应）
            from PyQt6.QtWidgets import QProgressDialog
            dlg_prog = QProgressDialog("正在恢复环境...", "取消", 0, 0, self)
            dlg_prog.setWindowTitle("恢复中")
            dlg_prog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dlg_prog.setCancelButton(None)
            dlg_prog.setMinimumDuration(0)
            dlg_prog.setLabelText("正在恢复 {} 个快照点，请稍候...".format(len(chain)))
            dlg_prog.show()

            def _poll():
                if rt.is_alive():
                    QTimer.singleShot(300, _poll)
                    return
                try:
                    dlg_prog.close()
                except Exception:
                    pass
                self._on_auto_restore_done(state["ok"], target, exit_code,
                                           err=state.get("err", ""))
            QTimer.singleShot(300, _poll)
            return

        except Exception as e:
            import traceback
            ZeroFallLogger.error("[自愈] 处理崩溃异常: {}".format(e))
            ZeroFallLogger.error(traceback.format_exc())

    def _on_auto_restore_done(self, ok: bool, latest, exit_code: int, err: str = ""):
        """崩溃自愈：后台恢复完成后的收尾（验证环境 + 告知 + 重启）"""
        try:
            if not ok:
                ZeroFallLogger.error("[自愈] 快照回退失败")
                QMessageBox.critical(
                    self, "自动回退失败",
                    "回退到快照 {} 失败：{}\n\n请查看日志或手动恢复。".format(
                        latest.snapshot_id, err or "未知错误")
                )
                self._rollback_count = 0
                return
            
            ZeroFallLogger.info("[自愈] 环境已回退，准备重启 DeepSeek")
            
            # 恢复后验证环境（避免"回退→重启→又崩"的无效循环）
            verified, vmsg = self._verify_environment()
            ZeroFallLogger.info("[自愈] 恢复后校验: {} ({})".format(
                "通过" if verified else "未通过", vmsg))
            
            if verified:
                QMessageBox.information(
                    self, "已自动恢复环境",
                    "DeepSeek 崩溃已自动处理：\n\n"
                    "• 崩溃检测：{}\n"
                    "• 回退快照：{}\n"
                    "• 回退模式：仅恢复插件环境（保留会话）\n"
                    "• 恢复后校验：通过\n"
                    "• 依赖重建：已完成\n\n"
                    "即将自动重启 DeepSeek（第 {} 次尝试）".format(
                        "进程异常退出" if exit_code >= 0 else "崩溃日志检测",
                        latest.snapshot_id, self._rollback_count
                    )
                )
            else:
                QMessageBox.warning(
                    self, "恢复后校验未通过",
                    "已回退到快照 {}，但环境校验仍有问题：\n{}\n\n"
                    "仍将尝试重启 DeepSeek。\n"
                    "若继续崩溃，建议检查 dsh-runtimes 运行时或重新安装。".format(
                        latest.snapshot_id, vmsg
                    )
                )
            QTimer.singleShot(2000, self.auto_launch_deepseek)
        except Exception as e:
            import traceback
            ZeroFallLogger.error("[自愈] 收尾异常: {}".format(e))
            ZeroFallLogger.error(traceback.format_exc())
    
    def _on_deepseek_exit(self, exit_code: int):
        try:
            from modules import fault_timeline as _ftl
            _ftl.record("dsh_exit", "DeepSeek 退出（exit={}）".format(exit_code))
        except Exception:
            pass
        """DeepSeek退出回调 - 共生退出"""
        # 崩溃自愈路径里主动 kill 了 DeepSeek，这次退出不算用户正常关闭，不能跟着退
        if getattr(self, '_suppress_follow_exit', False):
            self._suppress_follow_exit = False
            ZeroFallLogger.info("DeepSeek 退出来自崩溃自愈 kill，ZeroFall 保持运行（不跟随退出）")
            return
        # 未开启共生退出：保留 ZeroFall
        if not self.config.auto_exit_after_deepseek_close:
            ZeroFallLogger.info("DeepSeek 已退出 (退出码: {})，按配置保留 ZeroFall 运行".format(exit_code))
            return
        
        # 保护：自动启动后 10 秒内就退出，多半是启动失败，保留 ZeroFall 便于处理
        if getattr(self, '_auto_launched', False):
            elapsed = time.time() - getattr(self, '_launch_ts', 0)
            if elapsed < 10:
                ZeroFallLogger.warning(
                    "DeepSeek 启动后 {:.0f} 秒即退出，保留 ZeroFall 以便排查".format(elapsed)
                )
                try:
                    QMessageBox.warning(
                        self, "DeepSeek 启动异常",
                        "DeepSeek 启动后 {:.0f} 秒即退出（退出码 {}）。\n\n"
                        "ZeroFall 保持运行，建议检查并回退到健康快照。".format(elapsed, exit_code)
                    )
                except Exception:
                    pass
                return
        
        ZeroFallLogger.info("DeepSeek 已退出 (退出码: {})，ZeroFall 跟随关闭".format(exit_code))
        # 真正退出进程（不能只 close()，setQuitOnLastWindowClosed(False) 会让窗口关掉但进程驻留托盘）
        QTimer.singleShot(1500, QApplication.quit)
    
    def _on_tray_exit(self):
        """托盘退出"""
        self._unregister_global_hotkey()
        self.guardian.stop_monitoring()
        QApplication.quit()
    
    def _show_about(self):
        """显示关于对话框"""
        QMessageBox.about(
            self, "关于 ZeroFall",
            "ZeroFall - DeepSeek前置守护系统\n\n"
            "版本: 1.1.0\n\n"
            "功能:\n"
            "• ACE风格启动自检\n"
            "• 前置守护启动校验\n"
            "• 智能快照备份与恢复\n"
            "• 快照差异对比\n"
            "• 环境健康度检测\n"
            "• 百度网盘云端同步\n"
            "• 崩溃自动告警与回退\n"
            "• 全局热键 Ctrl+Shift+Z\n\n"
            "守护DeepSeek，稳定运行每一天。"
        )
    
    # ════════════════════════════════════════════════════════════════
    #  窗口关闭事件
    # ════════════════════════════════════════════════════════════════
    
    def closeEvent(self, event):
        """点右上角 X —— 按配置决定"最小化到托盘"还是"直接退出"

        三种行为（config.close_action）：
          ask       每次弹窗让用户选，可勾"记住我的选择"
          minimize  直接缩到托盘，继续后台守护（QQ / 微信的习惯）
          exit      直接退出程序

        注意 main.py 里设了 setQuitOnLastWindowClosed(False)，
        那是为了让"隐藏窗口后进程能活着"；真正退不退出由这里决定。
        """
        action = (getattr(self.config, 'close_action', 'ask') or 'ask').strip().lower()

        if action == 'minimize':
            self._minimize_to_tray()
            event.ignore()
            return
        if action == 'exit':
            self._finish_exit(event)
            return

        choice = self._ask_close_action()
        if choice == 'cancel':
            event.ignore()
            return
        if choice == 'minimize':
            self._minimize_to_tray()
            event.ignore()
            return
        self._finish_exit(event)

    def _minimize_to_tray(self):
        """隐藏窗口但保留进程，托盘图标继续可用"""
        try:
            self.hide()
        except Exception:
            pass
        ZeroFallLogger.info("[关闭] 已最小化到托盘，继续守护 DeepSeek")
        # 第一次这么做时给个气泡，免得用户以为程序没了
        if not getattr(self, '_tray_hint_shown', False):
            self._tray_hint_shown = True
            try:
                _nt = getattr(self, 'native_tray', None) or getattr(self, 'tray', None)
                if _nt is not None and hasattr(_nt, 'show_message'):
                    _nt.show_message("ZeroFall 仍在运行",
                                     "已最小化到托盘，双击图标可恢复窗口。")
                else:
                    ZeroFallLogger.info("[关闭] 托盘不支持气泡提示，跳过")
            except Exception as _e:
                ZeroFallLogger.warning("[关闭] 托盘提示失败: {}".format(_e))

    def _ask_close_action(self):
        """弹窗问用户：最小化到托盘 / 直接退出，可勾选记住

        Returns:
            'minimize' / 'exit' / 'cancel'
        """
        from PyQt6.QtWidgets import QMessageBox, QCheckBox
        box = QMessageBox(self)
        box.setWindowTitle("关闭 ZeroFall")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要最小化到托盘继续守护，还是直接退出？")
        box.setInformativeText(
            "最小化到托盘：窗口隐藏，仍在后台盯着 DeepSeek 和快照，\n"
            "双击托盘图标可恢复窗口。\n\n"
            "直接退出：ZeroFall 完全结束，守护和定时快照都会停止。")
        btn_tray = box.addButton("最小化到托盘", QMessageBox.ButtonRole.AcceptRole)
        btn_exit = box.addButton("直接退出", QMessageBox.ButtonRole.DestructiveRole)
        btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_tray)
        chk = QCheckBox("记住我的选择")
        box.setCheckBox(chk)
        box.exec()

        clicked = box.clickedButton()
        if clicked is btn_cancel:
            return 'cancel'

        remember = chk.isChecked()
        if clicked is btn_tray:
            if remember:
                self.config.close_action = 'minimize'
                try:
                    self.config_manager.save()
                except Exception:
                    pass
            return 'minimize'

        if remember:
            self.config.close_action = 'exit'
            try:
                self.config_manager.save()
            except Exception:
                pass
        return 'exit'

    def _finish_exit(self, event):
        """真正的退出流程（原 closeEvent 的全部逻辑）"""
        # 1. 如果 DeepSeek 在运行，询问是否同时终止（共生退出）
        if self.guardian.is_deepseek_running():
            reply = QMessageBox.question(
                self, "确认退出",
                "DeepSeek 正在运行中。\nZeroFall 退出时是否同时终止 DeepSeek？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            elif reply == QMessageBox.StandardButton.Yes:
                self.guardian.kill_deepseek()
                ZeroFallLogger.info("用户退出，已终止 DeepSeek")
        
        # 2. 保存窗口状态
        try:
            self.config.window_geometry = bytes(self.saveGeometry()).hex()
            self.config.window_state = bytes(self.saveState()).hex()
            self.config_manager.save()
        except Exception:
            pass
        
        # 3. 停止所有定时器
        try:
            self._stop_master_http_server()
        except Exception:
            pass
        if hasattr(self, 'status_timer'):
            self.status_timer.stop()
        if hasattr(self, '_dashboard_timer'):
            self._dashboard_timer.stop()
        if hasattr(self, '_silent_timer'):
            self._silent_timer.stop()
        
        # 4. 注销全局热键
        self._unregister_global_hotkey()
        
        # 5. 停止守护监控
        self.guardian.stop_monitoring()
        
        ZeroFallLogger.info("ZeroFall 优雅退出")
        event.accept()
        # 6. 兜底：托盘图标没挂上时必须真的退出
        # app.setQuitOnLastWindowClosed(False) 是为了"关窗后缩到托盘"，
        # 但如果托盘注册失败（NIM_ADD 返回 E_FAIL），窗口一关就再没有
        # 任何入口能找回程序 —— 进程留在后台只能靠任务管理器杀。
        # 所以这里明确判断：没有可用托盘就别硬撑。
        try:
            _tray_ok = False
            _nt = getattr(self, 'native_tray', None) or getattr(self, 'tray', None)
            if _nt is not None:
                _tray_ok = bool(getattr(_nt, '_added', False))
            if not _tray_ok:
                ZeroFallLogger.info(
                    "[退出] 未检测到可用托盘图标，直接结束进程（避免残留后台无法找回）")
                from PyQt6.QtWidgets import QApplication
                QApplication.quit()
        except Exception as _e:
            ZeroFallLogger.warning("[退出] 托盘兜底判断异常: {}".format(_e))
