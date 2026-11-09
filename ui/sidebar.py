# -*- coding: utf-8 -*-
"""侧边导航（QListWidget + QStackedWidget）

为什么不用 QTabWidget 竖排：
  QTabWidget 在 West/East 位置会把 tab 文字**旋转 90 度**，
  中文就变成一个一个字竖着垒起来，非常难看，而且没法加选中指示条。

  这里用 列表 + 堆栈 自己拼：
    左侧 QListWidget 显示横排文字 + 选中态左侧高亮条
    右侧 QStackedWidget 放页面

  对外暴露与 QTabWidget 同名的方法（addTab / setCurrentIndex / tabText /
  count / widget / removeTab ...），所以主窗口和页签代码都不必改。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QListWidget, QListWidgetItem, QStackedWidget, QLabel,
)

from ui import theme as th


class SideNav(QWidget):
    """兼容 QTabWidget 常用接口的侧边导航"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._labels = []

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._list = QListWidget()
        self._list.setObjectName('sidenav')
        self._list.setFixedWidth(150)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.setStyleSheet(f"""
QListWidget#sidenav {{
    background: {th.BG_ALT};
    border: none;
    border-right: 1px solid {th.BORDER};
    outline: none;
    padding: 10px 8px 10px 0px;
}}
QListWidget#sidenav::item {{
    color: {th.TEXT_DIM};
    border: none;
    border-left: 3px solid transparent;
    border-radius: 0px 8px 8px 0px;
    padding: 12px 10px 12px 12px;
    margin: 2px 0px 2px 0px;
    font-size: 13px;
}}
QListWidget#sidenav::item:hover {{
    background: rgba(34, 211, 238, 0.07);
    color: {th.TEXT};
}}
QListWidget#sidenav::item:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(34, 211, 238, 0.18), stop:1 rgba(34, 211, 238, 0.02));
    color: {th.ACCENT};
    border-left: 3px solid {th.ACCENT};
    font-weight: 600;
}}
""")
        lay.addWidget(self._list)

        self._stack = QStackedWidget()
        lay.addWidget(self._stack, 1)

        self._list.currentRowChanged.connect(self._on_row)

    # ── QTabWidget 兼容接口 ──────────────────────────────
    def addTab(self, widget, label: str):
        self._labels.append(label)
        self._stack.addWidget(widget)
        item = QListWidgetItem(self._fmt(label))
        item.setSizeHint(item.sizeHint().expandedTo(
            item.sizeHint().__class__(150, 42)))
        self._list.addItem(item)
        if self._stack.count() == 1:
            self._list.setCurrentRow(0)
        return self._stack.count() - 1

    def insertTab(self, index: int, widget, label: str):
        self._labels.insert(index, label)
        self._stack.insertWidget(index, widget)
        item = QListWidgetItem(self._fmt(label))
        self._list.insertItem(index, item)
        return index

    def removeTab(self, index: int):
        if 0 <= index < self._stack.count():
            self._stack.removeWidget(self._stack.widget(index))
            self._list.takeItem(index)
            self._labels.pop(index)

    def count(self) -> int:
        return self._stack.count()

    def widget(self, index: int):
        return self._stack.widget(index)

    def currentWidget(self):
        return self._stack.currentWidget()

    def setCurrentWidget(self, widget):
        i = self._stack.indexOf(widget)
        if i >= 0:
            self.setCurrentIndex(i)

    def currentIndex(self) -> int:
        return self._stack.currentIndex()

    def setCurrentIndex(self, index: int):
        if 0 <= index < self._stack.count():
            self._list.setCurrentRow(index)

    def tabText(self, index: int) -> str:
        return self._labels[index] if 0 <= index < len(self._labels) else ''

    def setTabText(self, index: int, text: str):
        if 0 <= index < len(self._labels):
            self._labels[index] = text
            it = self._list.item(index)
            if it:
                it.setText(self._fmt(text))

    def setTabPosition(self, *a, **k):
        """兼容旧调用：侧边导航本来就是竖排，无需处理"""
        return None

    def setDocumentMode(self, *a, **k):
        return None

    def setUsesScrollButtons(self, *a, **k):
        return None

    def setIconSize(self, *a, **k):
        return None

    def setMovable(self, *a, **k):
        return None

    # ── 内部 ─────────────────────────────────────────────
    @staticmethod
    def _fmt(label: str) -> str:
        # 前置两个空格做出缩进感，与选中条配合更像现代导航
        return '   {}'.format(label)

    def _on_row(self, row: int):
        if row >= 0:
            self._stack.setCurrentIndex(row)


class NavSection(QLabel):
    """侧边栏分组标签（备用）"""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(
            "QLabel {{ color: {}; font-size: 11px; font-weight: 700;"
            " letter-spacing: 1px; padding: 10px 0px 4px 15px; }}".format(
                th.TEXT_MUTE))
