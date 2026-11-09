# -*- coding: utf-8 -*-
"""ZeroFall 崩溃恢复选择对话框

DeepSeek 异常退出后弹出：
  · 列出所有健康快照（完整 + 轻量），默认选中最新完整快照
  · 默认 10 分钟倒计时，给用户充足时间判断要不要恢复
  · **倒计时结束不直接回退**：先弹一次最终确认，用户确认后才回退
  · 也可以主动取消（不恢复，保留崩溃现场）

为什么倒计时结束还要再确认一次：
  自动回退会覆盖当前插件环境并重启 DeepSeek，属于不可逆操作。
  如果用户刚好在 10 分钟末尾点了鼠标、或者根本没看到这个窗口，
  直接执行会让人很被动。加一道确认，把"自动"变成"自动提议、人工批准"。
  同时确认框默认落在「暂不」上，误触也安全。
"""
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QFrame, QMessageBox,
)

from logger import ZeroFallLogger
from ui import theme as th
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class CrashRestoreDialog(QDialog):
    """崩溃后选择恢复点的对话框（默认 10 分钟倒计时 + 回退前最终确认）"""

    DEFAULT_COUNTDOWN = 600  # 秒（10 分钟：留足时间判断要不要恢复）

    def __init__(self, healthy_snapshots, parent=None, countdown_seconds=None,
                 exclude_ids=None):
        """
        Args:
            healthy_snapshots: List[SnapshotData]，已按时间倒序
            countdown_seconds: 覆盖默认倒计时秒数（<=0 表示不自动恢复，纯手动）
            exclude_ids: 已经试过回退的快照 ID 集合。这些会排在列表末尾并
                         标注「已试过」，默认选中会跳过它们 —— 否则回退失败
                         再次崩溃时又选同一个，等于原地重试。
        """
        super().__init__(parent)
        self._snapshots = healthy_snapshots or []
        self._exclude_ids = set(exclude_ids or ())
        try:
            _cd = (self.DEFAULT_COUNTDOWN if countdown_seconds is None
                   else int(countdown_seconds))
        except Exception:
            _cd = self.DEFAULT_COUNTDOWN
        self._countdown_total = _cd
        self._remaining = _cd
        self._selected_id = None
        self._auto_cancelled = False      # 用户在最终确认里选了「暂不」
        self._confirming = False          # 防止确认框重复弹出

        self.setWindowTitle("DeepSeek 崩溃 - 选择恢复点")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(660, 480)
        self.setStyleSheet(self._qss())

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        if self._countdown_total > 0:
            self._timer.start(1000)
        self._update_ui()

    # ── 样式（与主界面同一套令牌）──
    @staticmethod
    def _qss() -> str:
        return f"""
QDialog {{ background: {th.BG}; }}
QLabel {{ color: {th.TEXT_DIM}; font-size: 13px; background: transparent; }}
QLabel#dlgTitle {{ color: {th.TEXT}; font-size: 17px; font-weight: 700; }}
QLabel#dlgHint {{ color: {th.TEXT_MUTE}; font-size: 12px; }}
QLabel#dlgCount {{
    color: {th.ACCENT}; font-size: 13px; font-weight: 700;
    background: rgba(34, 211, 238, 0.07);
    border: 1px solid rgba(34, 211, 238, 0.32);
    border-radius: 8px; padding: 8px 12px;
}}
QListWidget {{
    background: {th.SURFACE}; border: 1px solid {th.BORDER};
    border-radius: {th.RADIUS_SM}px; padding: 6px;
    font-size: 13px; color: {th.TEXT}; outline: none;
}}
QListWidget::item {{
    padding: 9px 8px; border-radius: 6px; margin: 2px 0px;
}}
QListWidget::item:hover {{ background: rgba(34, 211, 238, 0.07); }}
QListWidget::item:selected {{
    background: rgba(34, 211, 238, 0.16);
    color: {th.ACCENT}; font-weight: 600;
}}
QPushButton {{
    background: transparent; color: {th.TEXT_DIM};
    border: 1px solid {th.BORDER_HI}; border-radius: {th.RADIUS_SM}px;
    padding: 9px 18px; font-size: 13px;
}}
QPushButton:hover {{
    color: {th.TEXT}; border-color: {th.ACCENT};
    background: rgba(34, 211, 238, 0.08);
}}
QPushButton#primary {{
    color: #08111c; font-weight: 700;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {th.ACCENT}, stop:1 {th.ACCENT_2});
    border: none;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {th.ACCENT_2}, stop:1 {th.ACCENT});
}}
QPushButton#primary:disabled {{
    background: {th.SURFACE_HI}; color: {th.TEXT_MUTE};
}}
QPushButton#danger:hover {{
    color: {th.ERR}; border-color: {th.ERR};
    background: rgba(248, 113, 113, 0.10);
}}
"""

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(12)

        title = QLabel("DeepSeek 异常退出")
        title.setObjectName('dlgTitle')
        lay.addWidget(title)

        desc = QLabel(
            "ZeroFall 已保留崩溃现场快照。请选择要恢复到的环境点：\n"
            "· 完整快照：含运行时，体积大、恢复慢但最稳\n"
            "· 轻量快照：仅插件/配置，恢复快，会自动以最近完整快照为基线叠加")
        desc.setObjectName('dlgHint')
        desc.setWordWrap(True)
        lay.addWidget(desc)

        self._list = QListWidget()
        lay.addWidget(self._list, 1)

        self._countdown_label = QLabel()
        self._countdown_label.setObjectName('dlgCount')
        self._countdown_label.setWordWrap(True)
        lay.addWidget(self._countdown_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        self._cancel_btn = QPushButton("不恢复，保留现场")
        self._cancel_btn.setObjectName('danger')
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)

        self._ok_btn = QPushButton()
        self._ok_btn.setObjectName('primary')
        self._ok_btn.setMinimumHeight(36)
        self._ok_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self._ok_btn)

        lay.addLayout(btn_row)

        # 必须在按钮建好之后再填充：空列表分支要禁用 _ok_btn，
        # 顺序错了会在"没有任何健康快照"时直接 AttributeError 崩掉
        self._populate()

    # ── 填充列表 ──
    def _populate(self):
        self._list.clear()

        # 排序：未试过的优先，其次完整优先；已试过的沉到最底并标注
        def _rank(s):
            tried = 1 if s.snapshot_id in self._exclude_ids else 0
            full = 0 if getattr(s, 'is_full', False) else 1
            return (tried, full)

        # 两段排序：先按时间倒序，再用稳定排序按 (是否试过, 是否完整) 分组，
        # 这样每组内部仍是"最新的在前"，不会出现时间倒错
        by_time = sorted(self._snapshots,
                         key=lambda s: getattr(s, 'created_at', ''),
                         reverse=True)
        ordered = sorted(by_time, key=_rank)

        # 默认选择：第一个"未试过"的完整快照；没有就退到第一个未试过的
        default_id = None
        for s in ordered:
            if s.snapshot_id in self._exclude_ids:
                continue
            if getattr(s, 'is_full', False):
                default_id = s.snapshot_id
                break
        if default_id is None:
            for s in ordered:
                if s.snapshot_id not in self._exclude_ids:
                    default_id = s.snapshot_id
                    break
        if default_id is None and ordered:
            # 全部快照都试过了：不能一个都不选，否则用户点「立即恢复」只会
            # 收到"请先选择"的空提示。退而选最新的那个，列表里仍标着
            # [已试过]，用户看得见风险。
            default_id = ordered[0].snapshot_id

        for s in ordered:
            tag = "完整" if getattr(s, 'is_full', False) else "轻量"
            try:
                t = datetime.strptime(s.created_at, "%Y%m%d_%H%M%S").strftime("%m-%d %H:%M")
            except Exception:
                t = s.created_at
            note = ("  |  " + s.notes) if s.notes else ""
            tried = s.snapshot_id in self._exclude_ids
            mark = "  [已试过]" if tried else ""
            # 不要截断 ID：snapshot_20261109_153919 共 23 字符，
            # 截到 18 会把时分秒切掉，一列快照看起来完全一样，没法区分
            text = "[{}] {}  ·  {}  ·  {:.1f} MB{}{}".format(
                tag, t, s.snapshot_id, s.size_mb, note, mark)
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, s.snapshot_id)
            if tried:
                item.setForeground(Qt.GlobalColor.gray)
            self._list.addItem(item)
            if default_id and s.snapshot_id == default_id:
                self._list.setCurrentItem(item)
                self._selected_id = s.snapshot_id

        if self._list.count() == 0:
            empty = QListWidgetItem("（无健康快照可用）")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._list.addItem(empty)
            self._ok_btn.setEnabled(False)

        self._list.currentItemChanged.connect(self._on_select_changed)

    def _on_select_changed(self, cur, prev):
        if cur is None:
            return
        sid = cur.data(Qt.ItemDataRole.UserRole)
        if sid:
            self._selected_id = sid
            self._update_ui()

    # ── 倒计时 ──
    def _tick(self):
        if self._countdown_total <= 0:      # 0 = 不自动恢复，等用户自己决定
            return
        if self._auto_cancelled:            # 用户已放弃自动恢复，停表
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self._ask_final_confirm()
            return
        self._update_ui()

    def _ask_final_confirm(self):
        """倒计时结束 → 回退前最后一次人工确认

        这是"自动提议、人工批准"的关键一步：
          · 确认 → accept()，主窗口继续执行回退
          · 暂不 → 停止自动恢复，对话框留在原地等用户手动决定
        """
        if self._confirming:
            return
        self._confirming = True
        try:
            if not self._selected_id:
                # 没选中就退而取最新一个，避免确认了却没目标
                if self._list.count() > 0:
                    self._list.setCurrentRow(0)
                if not self._selected_id:
                    ZeroFallLogger.warning("[崩溃恢复] 无可用快照，自动恢复放弃")
                    self._auto_cancelled = True
                    self._update_ui()
                    return

            ZeroFallLogger.info(
                "[崩溃恢复] {} 分钟倒计时结束，等待用户最终确认（目标 {}）".format(
                    max(1, self._countdown_total // 60), self._selected_id))

            ret = QMessageBox.question(
                self,
                "确认执行回退",
                "恢复倒计时已结束。\n\n"
                "即将回退到快照：\n{}\n\n"
                "回退会覆盖当前插件环境，并在完成后自动重启 DeepSeek。\n"
                "确认现在执行吗？（选「否」则保留现场，由你手动决定）".format(
                    self._selected_id),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,      # 默认落在「否」，误触安全
            )

            if ret == QMessageBox.StandardButton.Yes:
                ZeroFallLogger.info("[崩溃恢复] 用户确认，执行回退")
                self.accept()
            else:
                self._auto_cancelled = True
                ZeroFallLogger.info("[崩溃恢复] 用户取消，保留现场等待手动处理")
                self._update_ui()
        finally:
            self._confirming = False

    def _fmt_remaining(self, secs):
        """把剩余秒数显示成人看得懂的样子：超过 1 分钟用 分 秒"""
        try:
            secs = int(secs)
        except Exception:
            return str(secs)
        if secs >= 60:
            return "{} 分 {:02d} 秒".format(secs // 60, secs % 60)
        return "{} 秒".format(secs)

    def _update_ui(self):
        if self._auto_cancelled:
            self._ok_btn.setText("立即恢复")
            self._countdown_label.setText(
                "已取消自动恢复，崩溃现场已保留。选择快照后点「立即恢复」手动执行。")
            return
        if self._countdown_total <= 0:
            self._ok_btn.setText("立即恢复")
            self._countdown_label.setText("不限时，请选择快照后点「立即恢复」")
            return
        left = self._fmt_remaining(self._remaining)
        if self._selected_id:
            self._ok_btn.setText("立即恢复 ({})".format(left))
            self._countdown_label.setText(
                "倒计时 {}：到点后会先弹确认框，确认后才回退并重启 DeepSeek".format(left))
        else:
            self._ok_btn.setText("立即恢复 ({})".format(left))
            self._countdown_label.setText(
                "未选择快照，{} 后将按列表第一个执行（仍需你在确认框里确认）".format(left))

    # ── 按钮 ──
    def _on_confirm(self):
        """手动点「立即恢复」：用户已主动发起，无需再二次确认"""
        if not self._selected_id:
            QMessageBox.warning(self, "未选择", "请先在列表中选择一个快照")
            return
        self._timer.stop()
        self.accept()

    def _on_cancel(self):
        self._timer.stop()
        self._selected_id = None
        self.reject()

    def selected_snapshot_id(self):
        return self._selected_id

    def showEvent(self, e):
        super().showEvent(e)
        try:
            th.apply_dark_titlebar(self)
        except Exception:
            pass
