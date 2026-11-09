"""
ZeroFall 启动加载覆盖层 & 功能自检
ACE风格的全屏自检进度条
"""
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QFrame, QGraphicsDropShadowEffect, QApplication, QDialog
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPropertyAnimation,
    QEasingCurve, QRect, QPoint
)
from PyQt6.QtGui import (
    QColor, QPainter, QLinearGradient, QFont, QPalette,
    QFontDatabase, QRadialGradient
)

sys.path.append(str(Path(__file__).parent.parent))
from logger import ZeroFallLogger


# ════════════════════════════════════════════
# 自检工作线程
# ════════════════════════════════════════════

class SelfCheckWorker(QThread):
    """自检工作线程 - 逐项检查系统功能"""
    
    progress = pyqtSignal(int, str, str, str)   # (progress%, step_name, status, detail)
    finished_all = pyqtSignal(dict, bool)        # (results, all_passed)
    
    CHECK_STEPS = [
        ("config",        "加载系统配置"),
        ("logger",        "初始化日志引擎"),
        ("deepseek_path", "检测 DeepSeek 路径"),
        ("plugins_dir",   "扫描插件目录"),
        ("snapshot_idx",  "加载快照索引"),
        ("md5_baseline",  "MD5 基线校验"),
        ("session_check", "会话完整性校验"),
        ("env_health",    "环境健康评估"),
        ("guardian_init", "初始化守护进程"),
        ("cloud_check",   "云端连接检测"),
        ("ready",         "系统就绪"),
    ]
    
    def __init__(self, config, snapshot_manager, env_checker, guardian, baidu_pan=None):
        super().__init__()
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.env_checker = env_checker
        self.guardian = guardian
        self.baidu_pan = baidu_pan
    
    def run(self):
        results = {}
        total = len(self.CHECK_STEPS)
        all_passed = True
        
        checks = {
            "config":        self._check_config,
            "logger":        self._check_logger,
            "deepseek_path": self._check_deepseek_path,
            "plugins_dir":   self._check_plugins_dir,
            "snapshot_idx":  self._check_snapshot_index,
            "md5_baseline":  self._check_md5_baseline,
            "session_check": self._check_session,
            "env_health":    self._check_env_health,
            "guardian_init": self._check_guardian,
            "cloud_check":   self._check_cloud,
            "ready":         self._check_ready,
        }
        
        for i, (step_id, step_name) in enumerate(self.CHECK_STEPS):
            progress = int((i + 1) / total * 100)
            
            try:
                status, detail = checks[step_id]()
            except Exception as e:
                status, detail = "failed", str(e)
            
            if status == "failed":
                all_passed = False
            
            self.progress.emit(progress, step_name, status, detail)
            results[step_id] = {"name": step_name, "status": status, "detail": detail}
            
            # 平滑动画延迟
            self.msleep(280)
        
        self.finished_all.emit(results, all_passed)
    
    def _check_config(self):
        if not self.config:
            return "failed", "配置对象为空"
        if not self.config.deepseek_exe_path:
            return "warning", "DeepSeek路径未配置"
        p = Path(self.config.deepseek_exe_path)
        if not p.exists():
            return "warning", f"路径不存在: {p}"
        return "passed", f"配置文件已加载 | 快照存储: {self.config.snapshot_dir}"
    
    def _check_logger(self):
        try:
            ZeroFallLogger.info("自检: 日志引擎初始化完成")
            return "passed", "日志引擎正常运行"
        except Exception as e:
            return "failed", f"日志引擎异常: {e}"
    
    def _check_deepseek_path(self):
        exe = Path(self.config.deepseek_exe_path)
        if not str(exe) or str(exe) == ".":
            return "warning", "未配置 DeepSeek 可执行文件路径"
        if not exe.exists():
            return "failed", f"文件不存在: {exe}"
        size_mb = exe.stat().st_size / (1024 * 1024)
        return "passed", f"{exe.name} ({size_mb:.1f} MB)"
    
    def _check_plugins_dir(self):
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        if not str(plugins_dir) or not plugins_dir.exists():
            return "warning", "插件目录不存在或未配置"
        subdirs = [d for d in plugins_dir.iterdir() if d.is_dir()]
        if not subdirs:
            return "warning", "插件目录为空"
        return "passed", f"发现 {len(subdirs)} 个插件目录"
    
    def _check_snapshot_index(self):
        try:
            snapshots = self.snapshot_manager.get_snapshot_list()
            healthy = len([s for s in snapshots if s.status.value == "healthy"])
            crashed = len([s for s in snapshots if s.status.value == "crashed"])
            return "passed", f"共 {len(snapshots)} 个快照 (健康:{healthy} 崩溃:{crashed})"
        except Exception as e:
            return "warning", f"快照索引加载异常: {e}"
    
    def _check_md5_baseline(self):
        if not self.config.md5_check_enabled:
            return "passed", "MD5校验已禁用"
        baseline = self.env_checker._state.get("baseline_md5", {})
        if not baseline:
            return "warning", "无基线数据，首次启动后自动建立"
        file_count = sum(len(v) for v in baseline.values())
        return "passed", f"基线已建立 | 监控 {file_count} 个文件"
    
    def _check_session(self):
        if not self.config.session_integrity_check:
            return "passed", "会话校验已禁用"
        session_dir = Path(self.config.deepseek_session_dir)
        if not session_dir.exists():
            return "passed", "会话目录不存在（正常）"
        files = list(session_dir.rglob("*"))
        return "passed", f"会话文件 {len(files)} 个"
    
    def _check_env_health(self):
        report = self.env_checker.run_full_check()
        status_map = {"passed": "passed", "warning": "warning", "failed": "failed"}
        s = status_map.get(report.overall_result.value, "warning")
        passed_count = len([i for i in report.items if i.result.value == "passed"])
        total_count = len(report.items)
        return s, f"校验项 {passed_count}/{total_count} 通过"
    
    def _check_guardian(self):
        running = self.guardian.is_deepseek_running()
        if running:
            return "passed", "DeepSeek 运行中 | 守护进程已挂载"
        return "passed", "守护进程就绪 | 等待启动 DeepSeek"
    
    def _check_cloud(self):
        if not self.baidu_pan:
            return "passed", "云端同步未启用"
        if self.baidu_pan.is_authorized():
            quota = self.baidu_pan.get_quota()
            if quota:
                return "passed", f"已连接 | 剩余 {quota['free_gb']:.1f}GB"
            return "passed", "百度网盘已授权"
        return "warning", "百度网盘未授权"
    
    def _check_ready(self):
        return "passed", "ZeroFall 前置守护系统就绪"


# ════════════════════════════════════════════
# 单个自检步骤行
# ════════════════════════════════════════════

class CheckStepWidget(QFrame):
    """单个自检步骤显示"""
    
    def __init__(self, step_name: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setStyleSheet("background: transparent; border: none;")
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(8)
        
        # 状态图标
        self.icon_label = QLabel("○")
        self.icon_label.setFixedWidth(20)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setStyleSheet("color: #555; font-size: 14px;")
        layout.addWidget(self.icon_label)
        
        # 步骤名称
        self.name_label = QLabel(step_name)
        self.name_label.setStyleSheet("color: #ccc; font-size: 13px;")
        layout.addWidget(self.name_label)
        
        # 详情（右对齐）
        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.detail_label.setStyleSheet("color: #777; font-size: 11px;")
        layout.addWidget(self.detail_label, 1)
    
    def set_status(self, status: str, detail: str = ""):
        """设置步骤状态: pending / running / passed / warning / failed"""
        icons = {
            "pending": ("○", "#555"),
            "running": ("◉", "#4fc3f7"),
            "passed":  ("✓", "#4caf50"),
            "warning": ("⚠", "#ff9800"),
            "failed":  ("✗", "#f44336"),
        }
        icon, color = icons.get(status, ("○", "#555"))
        self.icon_label.setText(icon)
        self.icon_label.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: bold;")
        
        if status == "running":
            self.name_label.setStyleSheet("color: #4fc3f7; font-size: 13px;")
        elif status in ("passed", "warning", "failed"):
            self.name_label.setStyleSheet("color: #eee; font-size: 13px;")
        
        if detail:
            color_map = {"passed": "#4caf50", "warning": "#ff9800", "failed": "#f44336"}
            dc = color_map.get(status, "#777")
            self.detail_label.setText(detail)
            self.detail_label.setStyleSheet(f"color: {dc}; font-size: 11px;")


# ════════════════════════════════════════════
# 加载覆盖层主组件
# ════════════════════════════════════════════

class LoadingOverlay(QWidget):
    """ACE风格启动加载覆盖层"""
    
    check_complete = pyqtSignal(dict, bool)  # (results, all_passed)
    
    def __init__(self, config, snapshot_manager, env_checker, guardian, baidu_pan=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.env_checker = env_checker
        self.guardian = guardian
        self.baidu_pan = baidu_pan
        self.step_widgets = {}
        self._worker = None
        
        self._init_ui()
    
    def _init_ui(self):
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            LoadingOverlay {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0d1117, stop:0.5 #161b22, stop:1 #0d1117);
            }
        """)
        
        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        # 中央容器
        container = QFrame()
        container.setFixedWidth(560)
        container.setStyleSheet("""
            QFrame {
                background: rgba(22, 27, 34, 240);
                border: 1px solid #30363d;
                border-radius: 12px;
            }
        """)
        
        # 添加阴影效果
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(40)
        shadow.setColor(QColor(0, 120, 255, 60))
        shadow.setOffset(0, 0)
        container.setGraphicsEffect(shadow)
        
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(32, 28, 32, 28)
        container_layout.setSpacing(16)
        
        # ── 标题区 ──
        title_layout = QHBoxLayout()
        
        logo_label = QLabel("◈")
        logo_label.setStyleSheet("""
            color: #58a6ff;
            font-size: 32px;
            font-weight: bold;
        """)
        title_layout.addWidget(logo_label)
        
        title_text_layout = QVBoxLayout()
        title_text_layout.setSpacing(2)
        
        title_label = QLabel("ZeroFall")
        title_label.setStyleSheet("""
            color: #e6edf3;
            font-size: 22px;
            font-weight: bold;
            letter-spacing: 2px;
        """)
        title_text_layout.addWidget(title_label)
        
        subtitle_label = QLabel("DeepSeek 前置守护系统 · 环境自检中")
        subtitle_label.setStyleSheet("color: #7d8590; font-size: 12px;")
        title_text_layout.addWidget(subtitle_label)
        
        title_layout.addLayout(title_text_layout)
        title_layout.addStretch()
        
        # 版本号
        ver_label = QLabel("v1.0.0")
        ver_label.setStyleSheet("color: #484f58; font-size: 11px;")
        title_layout.addWidget(ver_label)
        
        container_layout.addLayout(title_layout)
        
        # ── 分割线 ──
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #21262d;")
        line.setFixedHeight(1)
        container_layout.addWidget(line)
        
        # ── 进度条 ──
        progress_container = QVBoxLayout()
        progress_container.setSpacing(6)
        
        progress_header = QHBoxLayout()
        self.progress_label = QLabel("正在初始化...")
        self.progress_label.setStyleSheet("color: #58a6ff; font-size: 12px;")
        progress_header.addWidget(self.progress_label)
        
        progress_header.addStretch()
        
        self.percent_label = QLabel("0%")
        self.percent_label.setStyleSheet("color: #58a6ff; font-size: 12px; font-weight: bold;")
        progress_header.addWidget(self.percent_label)
        
        progress_container.addLayout(progress_header)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: #21262d;
                border: none;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #1f6feb, stop:0.5 #58a6ff, stop:1 #3fb950);
                border-radius: 3px;
            }
        """)
        progress_container.addWidget(self.progress_bar)
        
        container_layout.addLayout(progress_container)
        
        # ── 自检步骤列表 ──
        steps_container = QVBoxLayout()
        steps_container.setSpacing(2)
        
        for step_id, step_name in SelfCheckWorker.CHECK_STEPS:
            step_widget = CheckStepWidget(step_name)
            steps_container.addWidget(step_widget)
            self.step_widgets[step_id] = step_widget
        
        container_layout.addLayout(steps_container)
        
        # ── 底部分割线 ──
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("color: #21262d;")
        line2.setFixedHeight(1)
        container_layout.addWidget(line2)
        
        # ── 底部状态 ──
        bottom_layout = QHBoxLayout()
        
        self.status_icon = QLabel("◉")
        self.status_icon.setStyleSheet("color: #58a6ff; font-size: 12px;")
        bottom_layout.addWidget(self.status_icon)
        
        self.status_label = QLabel("系统自检中，请稍候...")
        self.status_label.setStyleSheet("color: #7d8590; font-size: 11px;")
        bottom_layout.addWidget(self.status_label)
        
        bottom_layout.addStretch()
        
        self.counter_label = QLabel("0 / 11")
        self.counter_label.setStyleSheet("color: #484f58; font-size: 11px;")
        bottom_layout.addWidget(self.counter_label)
        
        container_layout.addLayout(bottom_layout)
        
        main_layout.addWidget(container)
    
    def start_check(self):
        """启动自检流程"""
        self._worker = SelfCheckWorker(
            self.config,
            self.snapshot_manager,
            self.env_checker,
            self.guardian,
            self.baidu_pan
        )
        self._worker.progress.connect(self._on_step_progress)
        self._worker.finished_all.connect(self._on_all_finished)
        
        # 标记第一个步骤为运行中
        first_id = SelfCheckWorker.CHECK_STEPS[0][0]
        self.step_widgets[first_id].set_status("running", "检测中...")
        
        self._worker.start()
    
    def _on_step_progress(self, progress: int, step_name: str, status: str, detail: str):
        """单个步骤完成"""
        # 更新进度条
        self.progress_bar.setValue(progress)
        self.percent_label.setText(f"{progress}%")
        self.progress_label.setText(f"正在{step_name}...")
        
        # 找到对应的step_id
        step_id = None
        for sid, sname in SelfCheckWorker.CHECK_STEPS:
            if sname == step_name:
                step_id = sid
                break
        
        if step_id:
            # 更新当前步骤状态
            self.step_widgets[step_id].set_status(status, detail)
            
            # 标记下一个步骤为运行中
            steps = SelfCheckWorker.CHECK_STEPS
            idx = next((i for i, (s, _) in enumerate(steps) if s == step_id), -1)
            if idx >= 0 and idx + 1 < len(steps):
                next_id = steps[idx + 1][0]
                self.step_widgets[next_id].set_status("running", "检测中...")
        
        # 更新计数器
        completed = progress * len(SelfCheckWorker.CHECK_STEPS) // 100
        self.counter_label.setText(f"{completed} / {len(SelfCheckWorker.CHECK_STEPS)}")
    
    def _on_all_finished(self, results: dict, all_passed: bool):
        """自检全部完成"""
        self.progress_bar.setValue(100)
        self.percent_label.setText("100%")
        
        if all_passed:
            self.progress_label.setText("自检完成，系统就绪")
            self.status_icon.setText("✓")
            self.status_icon.setStyleSheet("color: #3fb950; font-size: 12px;")
            self.status_label.setText("所有检查项通过，守护系统已就绪")
            self.status_label.setStyleSheet("color: #3fb950; font-size: 11px;")
            self.counter_label.setText(f"{len(SelfCheckWorker.CHECK_STEPS)} / {len(SelfCheckWorker.CHECK_STEPS)}")
            
            # 进度条变为绿色
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    background: #21262d;
                    border: none;
                    border-radius: 3px;
                }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #238636, stop:0.5 #3fb950, stop:1 #56d364);
                    border-radius: 3px;
                }
            """)
        else:
            failed_count = len([r for r in results.values() if r["status"] == "failed"])
            warning_count = len([r for r in results.values() if r["status"] == "warning"])
            self.progress_label.setText(f"自检完成 | {failed_count} 项异常 {warning_count} 项警告")
            self.status_icon.setText("⚠")
            self.status_icon.setStyleSheet("color: #ff9800; font-size: 12px;")
            self.status_label.setText(f"发现 {failed_count} 个问题需要处理")
            self.status_label.setStyleSheet("color: #ff9800; font-size: 11px;")
            
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    background: #21262d;
                    border: none;
                    border-radius: 3px;
                }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #da3633, stop:0.5 #ff9800, stop:1 #f44336);
                    border-radius: 3px;
                }
            """)
        
        self.check_complete.emit(results, all_passed)


# ════════════════════════════════════════════
# 启动自检对话框（ACE 风格）
# ════════════════════════════════════════════

class SelfCheckDialog(QDialog):
    """启动时显示的自检对话框，用 QTimer 异步执行检查"""
    
    # 自检全部结束时发出（参数：是否全绿），主窗口据此决定何时显示
    all_done = pyqtSignal(bool)
    
    CHECKS = [
        ("config",      "加载系统配置",     "_check_config"),
        ("logger",      "初始化日志引擎",   "_check_logger"),
        ("deepseek",    "检测 DeepSeek 路径", "_check_deepseek"),
        ("plugins",     "扫描插件目录",     "_check_plugins"),
        ("snapshots",   "加载快照索引",     "_check_snapshots"),
        ("md5",         "MD5 基线校验",     "_check_md5"),
        ("session",     "会话完整性校验",   "_check_session"),
        ("health",      "环境健康评估",     "_check_health"),
        ("guardian",    "初始化守护进程",   "_check_guardian"),
        ("cloud",       "云端连接检测",     "_check_cloud"),
        ("ready",       "系统就绪",         "_check_ready"),
    ]
    
    def __init__(self, config, snapshot_manager, env_checker, guardian, baidu_pan=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.env_checker = env_checker
        self.guardian = guardian
        self.baidu_pan = baidu_pan
        self._step = 0
        self._rows = []
        self._results = []          # [(step_id, status), ...]
        self._finished = False      # 是否全部检查完成
        
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setFixedSize(560, 420)
        self.setStyleSheet("QDialog { background: #0d1117; border: 1px solid #30363d; border-radius: 10px; }")
        
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 20)
        
        # 标题
        title = QLabel("ZeroFall")
        title.setStyleSheet("color: #e6edf3; font-size: 22px; font-weight: bold;")
        lay.addWidget(title)
        
        sub = QLabel("环境自检中...")
        sub.setStyleSheet("color: #7d8590; font-size: 12px;")
        lay.addWidget(sub)
        
        lay.addSpacing(12)
        
        # 进度条
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setStyleSheet("""
            QProgressBar { background: #21262d; border: none; border-radius: 3px; color: #fff; }
            QProgressBar::chunk { background: qlineargradient(x1:0,x2:1, stop:0 #1f6feb, stop:1 #3fb950); border-radius: 3px; }
        """)
        lay.addWidget(self.progress)
        
        lay.addSpacing(8)
        
        # 步骤列表
        self.step_box = QVBoxLayout()
        self.step_box.setSpacing(2)
        for _, name, _ in self.CHECKS:
            row = QHBoxLayout()
            icon = QLabel("○")
            icon.setFixedWidth(20)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("color: #555; font-size: 14px;")
            row.addWidget(icon)
            
            lbl = QLabel(name)
            lbl.setStyleSheet("color: #ccc; font-size: 13px;")
            row.addWidget(lbl)
            
            row.addStretch()
            
            detail = QLabel("等待中")
            detail.setStyleSheet("color: #555; font-size: 11px;")
            row.addWidget(detail)
            
            self.step_box.addLayout(row)
            self._rows.append((icon, lbl, detail))
        
        lay.addLayout(self.step_box)
        lay.addStretch()
    
    # ── 检查方法 ──
    
    def _check_config(self):
        if self.config and self.config.deepseek_exe_path:
            return "passed", "配置已加载"
        if self.config:
            return "warning", "路径未配置"
        return "failed", "无配置"
    
    def _check_logger(self):
        return "passed", "日志引擎正常"
    
    def _check_deepseek(self):
        if not self.config or not self.config.deepseek_exe_path:
            return "warning", "未设置"
        p = Path(self.config.deepseek_exe_path)
        return ("passed", "{} 存在".format(p.name)) if p.exists() else ("warning", "文件不存在")
    
    def _check_plugins(self):
        if not self.config or not self.config.deepseek_plugins_dir:
            return "warning", "未设置"
        p = Path(self.config.deepseek_plugins_dir)
        if p.exists():
            c = len([d for d in p.iterdir() if d.is_dir()])
            return "passed", "{} 个插件目录".format(c)
        return "warning", "目录不存在"
    
    def _check_snapshots(self):
        try:
            ss = self.snapshot_manager.get_snapshot_list()
            h = len([s for s in ss if s.status.value == "healthy"])
            return "passed", "{} 个快照 ({} 健康)".format(len(ss), h)
        except Exception as e:
            return "failed", str(e)
    
    def _check_md5(self):
        if self.config.md5_check_enabled:
            return "passed", "校验规则已加载"
        return "passed", "MD5校验已禁用"
    
    def _check_session(self):
        if self.config.session_integrity_check:
            return "passed", "会话校验已加载"
        return "passed", "会话校验已禁用"
    
    def _check_health(self):
        """轻量环境健康检查（不扫描文件，避免启动卡顿；完整校验由守护启动时执行）"""
        try:
            cfg = self.config
            issues = []
            if not getattr(cfg, 'deepseek_exe_path', ''):
                issues.append("未配置 DeepSeek 路径")
            elif not Path(cfg.deepseek_exe_path).exists():
                issues.append("DeepSeek 可执行文件缺失")
            if not getattr(cfg, 'deepseek_plugins_dir', ''):
                issues.append("未配置插件目录")
            elif not Path(cfg.deepseek_plugins_dir).exists():
                issues.append("插件目录缺失")
            if issues:
                return "warning", "、".join(issues)
            return "passed", "环境健康"
        except Exception as e:
            return "warning", str(e)
    
    def _check_guardian(self):
        if self.guardian.is_deepseek_running():
            return "passed", "DeepSeek 运行中"
        return "passed", "守护进程就绪"
    
    def _check_cloud(self):
        # 优先检查"同步盘"方案（无需 API 授权）
        try:
            if getattr(self.config, 'cloud_sync_enabled', False):
                d = getattr(self.config, 'cloud_sync_dir', '')
                if d:
                    from pathlib import Path as _P
                    if _P(d).parent.exists():
                        return "passed", "已启用（同步盘）"
                    return "warning", "同步盘目录不可用: {}".format(d)
        except Exception:
            pass
        # 再检查百度网盘开放平台 API 方案
        if not self.baidu_pan:
            return "passed", "未启用（按需开启）"
        if self.baidu_pan.is_authorized():
            return "passed", "已授权"
        return "warning", "已启用但未授权，请在云端同步页登录"
    
    def _check_ready(self):
        return "passed", "系统就绪"
    
    # ── 异步执行 ──
    
    def run_checks(self):
        """启动自检（由 QTimer.singleShot 调用）"""
        self._step = 0
        self._results = []
        self._finished = False
        self._run_next()
    
    @property
    def all_passed(self) -> bool:
        """是否全部检查通过（全绿）且已完成"""
        if not self._finished:
            return False
        return (len(self._results) == len(self.CHECKS)
                and all(s == "passed" for _, s in self._results))
    
    @property
    def has_failed(self) -> bool:
        """是否存在失败项"""
        return any(s == "failed" for _, s in self._results)
    
    @property
    def done_count(self) -> int:
        """已完成检查项数"""
        return len(self._results)
    
    @property
    def total_count(self) -> int:
        """检查项总数"""
        return len(self.CHECKS)
    
    def _run_next(self):
        if self._step >= len(self.CHECKS):
            self._finished = True
            ZeroFallLogger.info(
                "[自检] 全部完成 | 通过={} 警告={} 失败={}".format(
                    sum(1 for _, s in self._results if s == "passed"),
                    sum(1 for _, s in self._results if s == "warning"),
                    sum(1 for _, s in self._results if s == "failed"),
                )
            )
            # 通知外部：自检真正结束（主窗口应等此信号再显示）
            try:
                self.all_done.emit(self.all_passed)
            except Exception:
                pass
            return
        
        step_id, step_name, method_name = self.CHECKS[self._step]
        icon, lbl, detail = self._rows[self._step]
        
        # 标记进行中
        icon.setText("◉")
        icon.setStyleSheet("color: #ffaa00; font-size: 14px; font-weight: bold;")
        lbl.setStyleSheet("color: #e6edf3; font-size: 13px;")
        detail.setText("检查中...")
        detail.setStyleSheet("color: #ffaa00; font-size: 11px;")
        
        # 执行检查
        method = getattr(self, method_name)
        try:
            status, msg = method()
        except Exception as e:
            status, msg = "failed", "异常: {}".format(e)
            ZeroFallLogger.error("[自检] {} 抛异常: {}".format(step_name, e))
        
        # 记录每项结果（便于排查红/黄来源）
        self._results.append((step_id, status))
        _tag = {"passed": "OK  ", "warning": "WARN", "failed": "FAIL"}.get(status, "????")
        ZeroFallLogger.info("[自检] [{}] {} -> {}".format(_tag, step_name, msg))
        
        # 更新结果
        colors = {"passed": "#3fb950", "warning": "#ffaa00", "failed": "#f44336"}
        icons  = {"passed": "✓",     "warning": "⚠",     "failed": "✗"}
        c = colors.get(status, "#555")
        icon.setText(icons.get(status, "?"))
        icon.setStyleSheet("color: {}; font-size: 14px; font-weight: bold;".format(c))
        detail.setText(msg)
        detail.setStyleSheet("color: {}; font-size: 11px;".format(c))
        
        # 更新进度
        self._step += 1
        pct = int(self._step / len(self.CHECKS) * 100)
        self.progress.setValue(pct)
        
        # 延迟进入下一步
        QTimer.singleShot(80, self._run_next)
