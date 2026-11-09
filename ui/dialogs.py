# -*- coding: utf-8 -*-
"""统一风格的对话框

为什么重写：Qt 自带 QMessageBox 是系统浅色外观，跟 ZeroFall 的深色面板
放在一起像两个软件。这里做一套自绘对话框，外观与主界面同一套设计语言：

  · 无边框圆角卡片 + 青色微光描边
  · 状态用色点表达（成功/警告/错误/询问），不用 emoji
  · 主按钮渐变强调，次按钮幽灵描边
  · 可拖动、Esc 关闭、Enter 确认

对外沿用 QMessageBox 的调用形状（information / warning / critical /
question / about + StandardButton 枚举），所以各处既有调用只需换掉
导入的名字，不用改调用代码。
"""
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget, QMessageBox, QGraphicsDropShadowEffect,
)

from ui import theme as th

# 复用 Qt 的枚举，保证调用方写的 QMessageBox.StandardButton.Yes 仍然有效
StandardButton = QMessageBox.StandardButton

_KIND_STYLE = {
    'info':    (th.ACCENT, 'i', '提示'),
    'success': (th.OK,     '✓', '完成'),
    'warning': (th.WARN,   '!', '注意'),
    'error':   (th.ERR,    '×', '错误'),
    'question': (th.ACCENT_2, '?', '确认'),
}


class _DialogBase(QDialog):
    """无边框深色卡片对话框基类"""

    def __init__(self, parent=None, kind='info', title='', body='',
                 min_width=460, max_width=680):
        super().__init__(parent)
        self._drag_pos = None
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(True)

        color, mark, _ = _KIND_STYLE.get(kind, _KIND_STYLE['info'])

        # 外层留白给阴影
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)

        self.card = QFrame()
        self.card.setObjectName('zfDialogCard')
        self.card.setStyleSheet(f"""
QFrame#zfDialogCard {{
    background: {th.SURFACE};
    border: 1px solid {th.BORDER_HI};
    border-radius: {th.RADIUS}px;
}}
""")
        self.card.setMinimumWidth(min_width)
        self.card.setMaximumWidth(max_width)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.card.setGraphicsEffect(shadow)

        outer.addWidget(self.card)

        body_lay = QVBoxLayout(self.card)
        body_lay.setContentsMargins(22, 18, 22, 18)
        body_lay.setSpacing(14)

        # ── 顶部：状态徽标 + 标题 + 关闭 ──
        head = QHBoxLayout()
        head.setSpacing(10)

        self.badge = QLabel(mark)
        self.badge.setFixedSize(24, 24)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setStyleSheet(f"""
QLabel {{
    color: {color};
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid {color};
    border-radius: 12px;
    font-size: 13px;
    font-weight: 700;
}}
""")
        head.addWidget(self.badge)

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"color: {th.TEXT}; font-size: 15px; font-weight: 700;")
        head.addWidget(self.title_label, 1)

        close_btn = QPushButton('✕')
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
QPushButton {{
    color: {th.TEXT_MUTE}; background: transparent;
    border: none; border-radius: 12px; font-size: 13px;
}}
QPushButton:hover {{ color: {th.ERR}; background: rgba(248,113,113,0.12); }}
""")
        close_btn.clicked.connect(self.reject)
        head.addWidget(close_btn)
        body_lay.addLayout(head)

        # ── 正文 ──
        self.content = QWidget()
        # 必须透明：QWidget 默认会继承窗口底色，在卡片上表现为一块深色矩形
        self.content.setStyleSheet("background: transparent; border: none;")
        self.content_lay = QVBoxLayout(self.content)
        self.content_lay.setContentsMargins(0, 0, 0, 0)
        self.content_lay.setSpacing(8)

        if body:
            self.add_text(body)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 高度先给个上限占位，真正高度在 showEvent 里按内容算
        self.scroll.setMaximumHeight(420)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")
        self.scroll.setWidget(self.content)
        body_lay.addWidget(self.scroll)

        # ── 底部按钮行（子类往里加）──
        self.footer = QHBoxLayout()
        self.footer.setSpacing(10)
        self.footer.addStretch()
        body_lay.addLayout(self.footer)

    # ── 内容辅助 ──
    def add_text(self, text: str, kind='normal'):
        colors = {'normal': th.TEXT_DIM, 'dim': th.TEXT_MUTE,
                  'warn': th.WARN, 'err': th.ERR, 'ok': th.OK}
        lb = QLabel(text)
        lb.setWordWrap(True)
        lb.setTextFormat(Qt.TextFormat.PlainText)
        lb.setStyleSheet(
            f"color: {colors.get(kind, th.TEXT_DIM)}; font-size: 13px;"
            " line-height: 150%;")
        lb.setSizePolicy(QSizePolicy.Policy.Preferred,
                         QSizePolicy.Policy.Minimum)
        self.content_lay.addWidget(lb)
        return lb

    def add_primary(self, text, slot, danger=False):
        color = th.ERR if danger else th.ACCENT
        btn = QPushButton(text)
        btn.setMinimumHeight(34)
        btn.setMinimumWidth(96)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
QPushButton {{
    color: #08111c;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {color}, stop:1 {th.ACCENT_2});
    border: none; border-radius: 8px;
    font-size: 13px; font-weight: 700; padding: 0 16px;
}}
QPushButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {th.ACCENT_2}, stop:1 {color});
}}
QPushButton:pressed {{ padding-top: 2px; }}
""")
        btn.clicked.connect(slot)
        btn.setDefault(True)
        self.footer.addWidget(btn)
        return btn

    def add_ghost(self, text, slot):
        btn = QPushButton(text)
        btn.setMinimumHeight(34)
        btn.setMinimumWidth(88)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(f"""
QPushButton {{
    color: {th.TEXT_DIM}; background: transparent;
    border: 1px solid {th.BORDER_HI}; border-radius: 8px;
    font-size: 13px; padding: 0 14px;
}}
QPushButton:hover {{
    color: {th.TEXT}; border-color: {th.ACCENT};
    background: rgba(34, 211, 238, 0.08);
}}
""")
        btn.clicked.connect(slot)
        self.footer.addWidget(btn)
        return btn

    # ── 拖动 ──
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        e.accept()

    def showEvent(self, e):
        super().showEvent(e)
        # 按内容高度调滚动区：内容少时不留下大片空白，
        # 内容多时封顶出现滚动条（固定死高度会把最后一项切一半）
        try:
            need = self.content.sizeHint().height()
            self.scroll.setFixedHeight(max(48, min(need + 4, 420)))
            self.adjustSize()
        except Exception:
            pass
        try:
            th.apply_dark_titlebar(self)
        except Exception:
            pass
        # 居中到父窗口
        par = self.parent()
        if par is not None:
            try:
                geo = par.window().frameGeometry()
                self.move(geo.center().x() - self.width() // 2,
                          geo.center().y() - self.height() // 2)
            except Exception:
                pass


class _NoticeDialog(_DialogBase):
    """单按钮通知框"""

    def __init__(self, parent, title, text, kind='info', detail=None):
        super().__init__(parent, kind=kind, title=title, body=text)
        if detail:
            self.add_text(detail, kind='dim')
        self.add_primary('知道了', self.accept)


class _ConfirmDialog(_DialogBase):
    """询问框，返回 StandardButton"""

    def __init__(self, parent, title, text, buttons, default_button):
        super().__init__(parent, kind='question', title=title, body=text)
        self._result = StandardButton.No
        labels = {
            StandardButton.Yes: ('是', True),
            StandardButton.No: ('否', False),
            StandardButton.Ok: ('确定', True),
            StandardButton.Cancel: ('取消', False),
        }
        order = []
        for btn in (StandardButton.Yes, StandardButton.No,
                    StandardButton.Ok, StandardButton.Cancel):
            if buttons & btn:
                order.append(btn)

        # 先摆次按钮，主按钮最后加 → 落在最右侧
        primaries = [b for b in order if labels.get(b, ('', False))[1]]
        secondaries = [b for b in order if not labels.get(b, ('', False))[1]]
        for btn in secondaries:
            text_lb, _ = labels.get(btn, (str(btn), False))
            self.add_ghost(text_lb, lambda _=False, b=btn: self._pick(b))
        for btn in primaries:
            text_lb, _ = labels.get(btn, (str(btn), True))
            self.add_primary(text_lb, lambda _=False, b=btn: self._pick(b))

    def _pick(self, btn):
        self._result = btn
        self.accept()

    def result_button(self):
        return self._result


def _run(dlg):
    dlg.exec()
    return dlg


# ── 对外接口（与 QMessageBox 静态方法同形）──────────────────
class ZFMessageBox:
    """QMessageBox 的替代实现，调用形状保持一致"""

    StandardButton = StandardButton
    ButtonRole = QMessageBox.ButtonRole

    @staticmethod
    def information(parent, title, text, *args, **kwargs):
        _run(_NoticeDialog(parent, title, text, 'info'))
        return StandardButton.Ok

    @staticmethod
    def warning(parent, title, text, *args, **kwargs):
        _run(_NoticeDialog(parent, title, text, 'warning'))
        return StandardButton.Ok

    @staticmethod
    def critical(parent, title, text, *args, **kwargs):
        _run(_NoticeDialog(parent, title, text, 'error'))
        return StandardButton.Ok

    @staticmethod
    def about(parent, title, text, *args, **kwargs):
        _run(_NoticeDialog(parent, title, text, 'info'))
        return StandardButton.Ok

    @staticmethod
    def question(parent, title, text, buttons=None, defaultButton=None, *args, **kwargs):
        if buttons is None:
            buttons = StandardButton.Yes | StandardButton.No
        dlg = _ConfirmDialog(parent, title, text, buttons,
                             defaultButton or StandardButton.Yes)
        _run(dlg)
        return dlg.result_button()


class VerifyResultDialog(_DialogBase):
    """环境校验结果（分项列表，比一坨纯文本清楚得多）"""

    def __init__(self, parent, report):
        overall = report.overall_result.value
        kind = {'passed': 'success', 'warning': 'warning'}.get(overall, 'error')
        title = {'passed': '环境校验通过',
                 'warning': '环境校验有警告'}.get(overall, '环境校验异常')
        super().__init__(parent, kind=kind, title=title,
                         body='共检查 {} 项'.format(len(report.items)),
                         min_width=560, max_width=720)

        counts = {'passed': 0, 'warning': 0, 'failed': 0}
        for it in report.items:
            counts[it.result.value] = counts.get(it.result.value, 0) + 1

        # 汇总胶囊
        row = QHBoxLayout()
        row.setSpacing(8)
        for name, val, color, key in (
            ('通过', counts.get('passed', 0), th.OK, 'passed'),
            ('警告', counts.get('warning', 0), th.WARN, 'warning'),
            ('失败', counts.get('failed', 0), th.ERR, 'failed'),
        ):
            pill = QLabel('{} {}'.format(name, val))
            pill.setStyleSheet(f"""
QLabel {{
    color: {color};
    background: rgba(255,255,255,0.04);
    border: 1px solid {color};
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 700;
}}
""")
            row.addWidget(pill)
        row.addStretch()
        self.content_lay.addLayout(row)

        # 分项
        for it in report.items:
            self.content_lay.addWidget(self._item_row(it))

        self.add_primary('关闭', self.accept)

    @staticmethod
    def _item_row(item):
        color = {'passed': th.OK, 'warning': th.WARN}.get(
            item.result.value, th.ERR)
        wrap = QFrame()
        wrap.setStyleSheet(f"""
QFrame {{
    background: rgba(255,255,255,0.025);
    border: none;
    border-left: 2px solid {color};
    border-radius: 6px;
}}
""")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        dot = QLabel('●')
        dot.setStyleSheet(f"color: {color}; font-size: 9px; border: none;")
        top.addWidget(dot)
        name = QLabel(item.name)
        name.setStyleSheet(
            f"color: {th.TEXT}; font-size: 12.5px; font-weight: 600; border: none;")
        top.addWidget(name, 1)
        state = QLabel({'passed': '通过', 'warning': '警告'}.get(
            item.result.value, '失败'))
        state.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 700; border: none;")
        top.addWidget(state)
        lay.addLayout(top)

        if item.message:
            msg = QLabel(item.message)
            msg.setWordWrap(True)
            msg.setStyleSheet(
                f"color: {th.TEXT_DIM}; font-size: 12px; border: none;")
            lay.addWidget(msg)

        if item.details and item.result.value != 'passed':
            det = QLabel(str(item.details).splitlines()[0][:160])
            det.setWordWrap(True)
            det.setStyleSheet(
                f"color: {th.TEXT_MUTE}; font-size: 11px; border: none;")
            lay.addWidget(det)
        return wrap
