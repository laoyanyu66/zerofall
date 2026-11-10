# -*- coding: utf-8 -*-
"""
Dashboard Tab - 首页标签页（卡片式布局）
"""
import time
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGridLayout, QFrame, QMessageBox, QApplication
)
from PyQt6.QtCore import Qt, QTimer
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class DashboardTab(QWidget):
    """Dashboard 首页标签页 - 卡片式布局"""
    
    def __init__(self, config, guardian, env_checker, snapshot_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.guardian = guardian
        self.env_checker = env_checker
        self.snapshot_manager = snapshot_manager
        self._deepseek_start_time = None
        
        self._init_ui()
        self._start_dashboard_timer()
    
    def _init_ui(self):
        """初始化用户界面

        视觉思路：
          · 顶部是标题 + 一行说明，不用大色块
          · 六张卡片分两行三列，每张只讲一件事，数字放大
          · 状态用「色点 + 文字」表达，不依赖 emoji
            （emoji 在部分字体下渲染成方框，反而更丑）
          · 快捷按钮交给全局主题，不再各自涂色
        """
        from ui import theme as th

        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(14)
        root_layout.setContentsMargins(20, 16, 20, 16)

        root_layout.addWidget(th.header(
            "控制台",
            "DeepSeek 运行态、环境健康、快照与守护一屏总览"))

        grid = QGridLayout()
        grid.setSpacing(14)

        # ── 卡 1：DeepSeek 运行状态 ──
        self.dash_ds_card = self._make_card("DeepSeek 状态")
        self.dash_ds_status = QLabel("未运行")
        self.dash_ds_status.setObjectName('metric_warn')
        self.dash_ds_duration = QLabel("运行时长 --")
        self.dash_ds_duration.setObjectName('dim')
        self.dash_ds_card.layout().addWidget(self.dash_ds_status)
        self.dash_ds_card.layout().addWidget(self.dash_ds_duration)
        self.dash_ds_card.layout().addStretch()
        grid.addWidget(self.dash_ds_card, 0, 0)

        # ── 卡 2：环境健康度 ──
        self.dash_health_card = self._make_card("环境健康度")
        self.dash_health_icon = QLabel("●")
        self.dash_health_icon.setObjectName('metric')
        self.dash_health_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dash_health_icon.setStyleSheet(
            "QLabel {{ color: {}; font-size: 22px; border: none; }}".format(
                th.TEXT_MUTE))
        self.dash_health_text = QLabel("待检测")
        self.dash_health_text.setObjectName('dim')
        self.dash_health_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dash_health_card.layout().addWidget(self.dash_health_icon)
        self.dash_health_card.layout().addWidget(self.dash_health_text)
        grid.addWidget(self.dash_health_card, 0, 1)

        # ── 卡 3：健康快照数量 ──
        self.dash_snap_card = self._make_card("健康快照")
        self.dash_snap_count = QLabel("0")
        self.dash_snap_count.setObjectName('metric_ok')
        self.dash_snap_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dash_snap_card.layout().addWidget(self.dash_snap_count)
        grid.addWidget(self.dash_snap_card, 0, 2)

        # ── 卡 4：最近快照 ──
        self.dash_latest_card = self._make_card("最近快照")
        self.dash_latest_time = QLabel("时间 --")
        self.dash_latest_time.setObjectName('dim')
        self.dash_latest_size = QLabel("大小 --")
        self.dash_latest_size.setObjectName('dim')
        self.dash_latest_card.layout().addWidget(self.dash_latest_time)
        self.dash_latest_card.layout().addWidget(self.dash_latest_size)
        self.dash_latest_card.layout().addStretch()
        grid.addWidget(self.dash_latest_card, 1, 0)

        # ── 卡 5：守护状态 ──
        self.dash_guard_card = self._make_card("守护状态")
        self.dash_guard_status = QLabel("就绪")
        self.dash_guard_status.setObjectName('h2')
        self.dash_guard_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dash_perf_label = QLabel("CPU -- | 内存 -- MB")
        self.dash_perf_label.setObjectName('mute')
        self.dash_perf_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dash_guard_card.layout().addWidget(self.dash_guard_status)
        self.dash_guard_card.layout().addWidget(self.dash_perf_label)
        grid.addWidget(self.dash_guard_card, 1, 1)

        # ── 卡 6：快捷操作 ──
        quick_card = self._make_card("快捷操作")
        btn_grid = QGridLayout()
        btn_grid.setSpacing(8)

        self.dash_quick_snap_btn = QPushButton("快速快照")
        self.dash_quick_snap_btn.setMinimumHeight(34)
        self.dash_quick_snap_btn.clicked.connect(self._on_quick_snapshot)
        btn_grid.addWidget(self.dash_quick_snap_btn, 0, 0)

        self.dash_launch_btn = QPushButton("启动 DeepSeek")
        self.dash_launch_btn.setMinimumHeight(34)
        self.dash_launch_btn.setDefault(True)
        btn_grid.addWidget(self.dash_launch_btn, 0, 1)

        self.dash_verify_btn = QPushButton("执行环境校验")
        self.dash_verify_btn.setMinimumHeight(34)
        self.dash_verify_btn.clicked.connect(self._on_quick_verify)
        btn_grid.addWidget(self.dash_verify_btn, 1, 0, 1, 2)

        quick_card.layout().addLayout(btn_grid)
        quick_card.layout().addStretch()
        grid.addWidget(quick_card, 1, 2)

        for c in range(3):
            grid.setColumnStretch(c, 1)
        root_layout.addLayout(grid)
        root_layout.addStretch()

        self.refresh_dashboard()

    @staticmethod
    def _make_card(title_text):
        """创建一张卡片框架"""
        card = QFrame()
        # 必须用 #card 限定：裸写 QFrame{} 会连带把卡片里所有 QLabel
        # （QLabel 是 QFrame 子类）都画成带边框的小方框
        card.setObjectName('card')
        card.setMinimumHeight(126)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        header = QLabel(title_text)
        header.setStyleSheet("font-size: 12px; color: #888; border: none;")
        layout.addWidget(header)
        return card
    
    @staticmethod
    def _quick_btn_style(bg_color):
        return (
            "QPushButton {{"
            "  background-color: {bg}; color: white; font-weight: bold;"
            "  padding: 8px; border-radius: 5px; border: none;"
            "}}"
            "QPushButton:hover {{ background-color: {bg}; opacity: 0.85; }}"
        ).format(bg=bg_color)
    
    def _start_dashboard_timer(self):
        """启动 Dashboard 5 秒刷新定时器"""
        self._dashboard_timer = QTimer(self)
        self._dashboard_timer.timeout.connect(self.refresh_dashboard)
        self._dashboard_timer.start(5000)
    
    @staticmethod
    def _set_metric_kind(label, kind: str):
        """切换标签的语义样式（metric_ok / metric_warn / h2_ok / h2_warn）"""
        from ui import theme as th
        colors = {
            'metric_ok': th.OK, 'metric_warn': th.WARN,
            'h2_ok': th.OK, 'h2_warn': th.WARN,
        }
        size = 30 if kind.startswith('metric') else 15
        label.setStyleSheet(
            "QLabel {{ color: {}; font-size: {}px; font-weight: 700;"
            " border: none; }}".format(colors.get(kind, th.TEXT), size))

    def refresh_dashboard(self):
        """刷新 Dashboard 所有卡片数据"""
        try:
            # DeepSeek 状态
            ds_running = self.guardian.is_deepseek_running()
            if ds_running:
                self.dash_ds_status.setText("运行中")
                self._set_metric_kind(self.dash_ds_status, 'metric_ok')
                # 记录启动时间
                if self._deepseek_start_time is None:
                    self._deepseek_start_time = time.time()
                elapsed = time.time() - self._deepseek_start_time
                hours = int(elapsed // 3600)
                minutes = int((elapsed % 3600) // 60)
                seconds = int(elapsed % 60)
                self.dash_ds_duration.setText(
                    "运行时长 {:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
                )
                self.dash_launch_btn.setEnabled(False)
                self.dash_launch_btn.setText("DeepSeek 运行中")
            else:
                self.dash_ds_status.setText("未运行")
                self._set_metric_kind(self.dash_ds_status, 'metric_warn')
                self.dash_ds_duration.setText("运行时长 --")
                self._deepseek_start_time = None
                self.dash_launch_btn.setEnabled(True)
                self.dash_launch_btn.setText("启动 DeepSeek")
            
            # 环境健康度
            try:
                report = self.env_checker.run_full_check(use_cache=True)
                result_val = report.overall_result.value
                from ui import theme as _th
                if result_val == "passed":
                    self.dash_health_icon.setText("●")
                    self.dash_health_icon.setStyleSheet(
                        "QLabel {{ color: {}; font-size: 22px; border: none; }}".format(_th.OK))
                    self.dash_health_text.setText("健康")
                elif result_val == "warning":
                    self.dash_health_icon.setText("●")
                    self.dash_health_icon.setStyleSheet(
                        "QLabel {{ color: {}; font-size: 22px; border: none; }}".format(_th.WARN))
                    self.dash_health_text.setText("有警告")
                else:
                    self.dash_health_icon.setText("●")
                    self.dash_health_icon.setStyleSheet(
                        "QLabel {{ color: {}; font-size: 22px; border: none; }}".format(_th.ERR))
                    self.dash_health_text.setText("异常")
            except Exception:
                self.dash_health_icon.setText("●")
                self.dash_health_icon.setStyleSheet(
                    "QLabel {{ color: #5d6b80; font-size: 22px; border: none; }}")
                self.dash_health_text.setText("检测失败")
            
            # 快照数据
            snapshots = self.snapshot_manager.get_snapshot_list()
            healthy = [s for s in snapshots if s.status.value == "healthy"]
            self.dash_snap_count.setText(str(len(healthy)))
            
            # 最近快照
            if snapshots:
                latest = snapshots[0]  # 已按时间倒序
                from utils import format_timestamp
                self.dash_latest_time.setText(
                    "时间 " + format_timestamp(latest.created_at)
                )
                self.dash_latest_size.setText(
                    "大小 {:.2f} MB".format(latest.size_mb)
                )
            else:
                self.dash_latest_time.setText("时间 --")
                self.dash_latest_size.setText("大小 --")
            
            # 守护状态
            if ds_running:
                self.dash_guard_status.setText("守护中")
                self._set_metric_kind(self.dash_guard_status, 'h2_ok')
            else:
                self.dash_guard_status.setText("待命")
                self._set_metric_kind(self.dash_guard_status, 'h2_warn')
        except Exception as e:
            from logger import ZeroFallLogger
            ZeroFallLogger.error("Dashboard 刷新异常: {}".format(e))
    
    def _on_quick_snapshot(self):
        """Dashboard 快速快照（无需输入备注）"""
        snapshot = self.snapshot_manager.create_snapshot(notes="快速快照")
        if snapshot:
            self.refresh_dashboard()
            QMessageBox.information(
                self, "快速快照",
                "快照创建成功\nID: {}".format(snapshot.snapshot_id)
            )
        else:
            QMessageBox.critical(self, "快照失败", "快照创建失败，请查看日志")
    
    def _on_quick_verify(self):
        """Dashboard 快速执行环境校验"""
        try:
            report = self.env_checker.run_full_check()
            result_val = report.overall_result.value
            if result_val == "passed":
                QMessageBox.information(self, "校验结果", "✅ 环境健康，一切正常")
            elif result_val == "warning":
                items = ["⚠️ {}: {}".format(i.name, i.message) for i in report.items
                         if i.result.value != "passed"]
                QMessageBox.warning(
                    self, "校验结果",
                    "环境有警告:\n\n" + "\n".join(items[:10])
                )
            else:
                items = ["❌ {}: {}".format(i.name, i.message) for i in report.items
                         if i.result.value == "failed"]
                QMessageBox.critical(
                    self, "校验结果",
                    "环境异常:\n\n" + "\n".join(items[:10])
                )
            self.refresh_dashboard()
        except Exception as e:
            QMessageBox.critical(self, "校验失败", "环境校验出错: {}".format(e))
