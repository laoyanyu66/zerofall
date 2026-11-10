# -*- coding: utf-8 -*-
"""ZeroFall 设计系统

为什么单独抽一个文件：
  原来所有颜色/边框都散落在各个页签的 setStyleSheet 里，蓝的绿的灰的
  各写一套，视觉上不成体系，也改不动 —— 想统一调色得满项目搜字符串。

  这里集中定义：
    · 设计令牌（颜色、圆角、间距）
    · 一套完整 QSS（一次注入，全窗口生效）
    · 侧边栏导航样式（把传统横排页签改成左侧竖排）
    · 少量构件的工厂函数（状态胶囊、指标卡、分区标题）

  原则：偏深色 + 冷色渐变 + 细边框，信息密度保持，但层次靠留白和描边区分，
  不用大色块砸。
"""
import os

from PyQt6.QtWidgets import QLabel, QFrame, QVBoxLayout, QHBoxLayout

# ── 设计令牌 ──────────────────────────────────────────────
BG = "#0b0f17"          # 窗口底色（近黑偏蓝）
BG_ALT = "#0e1420"      # 侧栏/头部
SURFACE = "#151c28"     # 卡片
SURFACE_HI = "#1b2432"  # 卡片悬停
BORDER = "#232c3d"      # 常规描边
BORDER_HI = "#31405a"   # 强调描边

TEXT = "#e6edf6"
TEXT_DIM = "#93a1b5"
TEXT_MUTE = "#5d6b80"

ACCENT = "#22d3ee"      # 主强调：青
ACCENT_2 = "#3b82f6"    # 次强调：蓝
ACCENT_3 = "#a855f7"    # 第三强调：紫
OK = "#34d399"
WARN = "#fbbf24"
ERR = "#f87171"

RADIUS = 12
RADIUS_SM = 8


def qss() -> str:
    """全局样式表 —— 一次 setStyleSheet 覆盖整窗"""
    return f"""
/* ════════ 基础 ════════ */
QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {BG}; }}
/* QLabel 继承自 QFrame，会被父级的 QFrame 规则连带描边，必须显式清掉 */
QLabel {{ background: transparent; border: none; }}

QToolTip {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: 6px;
    padding: 6px 10px;
}}

/* ════════ 侧边栏导航（QTabWidget 竖排）════════ */
QTabWidget::pane {{
    border: none;
    background: transparent;
    padding: 0px;
}}
QTabWidget::tab-bar {{ alignment: left; }}

QTabBar {{
    background: {BG_ALT};
    border: none;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    border: none;
    border-left: 3px solid transparent;
    padding: 13px 20px 13px 17px;
    margin: 2px 6px 2px 0px;
    min-width: 88px;
    text-align: left;
    font-size: 13px;
}}
QTabBar::tab:hover {{
    background: rgba(34, 211, 238, 0.07);
    color: {TEXT};
}}
QTabBar::tab:selected {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(34, 211, 238, 0.16), stop:1 rgba(34, 211, 238, 0.02));
    color: {ACCENT};
    border-left: 3px solid {ACCENT};
    font-weight: 600;
}}

/* ════════ 卡片 / 分组 ════════ */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    margin-top: 20px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
    color: {TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 6px;
    padding: 0px 8px;
    color: {ACCENT};
    background: {SURFACE};
    border-radius: 4px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QFrame#card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#card:hover {{
    border: 1px solid {BORDER_HI};
    background: {SURFACE_HI};
}}

/* ════════ 按钮 ════════ */
QPushButton {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: {RADIUS_SM}px;
    padding: 8px 16px;
    font-size: 13px;
}}
QPushButton:hover {{
    background: rgba(34, 211, 238, 0.14);
    border: 1px solid {ACCENT};
    color: #ffffff;
}}
QPushButton:pressed {{ background: rgba(34, 211, 238, 0.24); }}
QPushButton:disabled {{
    background: #121924;
    color: {TEXT_MUTE};
    border: 1px solid {BORDER};
}}
QPushButton:default {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0891b2, stop:1 {ACCENT_2});
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}

/* ════════ 输入 ════════ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
    background: {BG_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 7px 10px;
    selection-background-color: {ACCENT_2};
    selection-color: #ffffff;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border: 1px solid {ACCENT};
    background: #0c131d;
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {TEXT_MUTE};
    background: #0d131c;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: {RADIUS_SM}px;
    selection-background-color: rgba(34, 211, 238, 0.22);
    outline: none;
    padding: 4px;
}}

/* ════════ 表格 ════════ */
QTableWidget, QTableView {{
    background: {SURFACE};
    alternate-background-color: #131a25;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    gridline-color: transparent;
    selection-background-color: rgba(34, 211, 238, 0.16);
    selection-color: #ffffff;
    outline: none;
}}
QTableWidget::item, QTableView::item {{
    padding: 6px 8px;
    border: none;
}}
QTableWidget::item:selected, QTableView::item:selected {{
    background: rgba(34, 211, 238, 0.18);
    color: #ffffff;
}}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {BG_ALT};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 9px 8px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QTableCornerButton::section {{ background: {BG_ALT}; border: none; }}

/* ════════ 列表 / 树 ════════ */
QListWidget, QTreeWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    outline: none;
    padding: 4px;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 6px; }}
QListWidget::item:hover {{ background: rgba(34, 211, 238, 0.08); }}
QListWidget::item:selected {{
    background: rgba(34, 211, 238, 0.18);
    color: #ffffff;
}}

/* ════════ 复选框 / 单选框 ════════ */
QCheckBox, QRadioButton {{
    spacing: 8px; color: {TEXT};
    padding: 3px 0px;
    background: transparent;
    border: none;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER_HI};
    border-radius: 4px;
    background: {BG_ALT};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border: 1px solid {ACCENT};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: 1px solid {ACCENT};
}}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_MUTE}; }}

/* ════════ 进度条 ════════ */
QProgressBar {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
    height: 16px;
    font-size: 11px;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {ACCENT}, stop:1 {ACCENT_2});
}}

/* ════════ 滚动条 ════════ */
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_HI};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_HI};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ════════ 状态栏 / 菜单 ════════ */
QStatusBar {{
    background: {BG_ALT};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
    font-size: 12px;
}}
QStatusBar::item {{ border: none; }}
QMenuBar {{ background: {BG_ALT}; color: {TEXT}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 6px 12px; background: transparent; }}
QMenuBar::item:selected {{ background: rgba(34, 211, 238, 0.14); border-radius: 6px; }}
QMenu {{
    background: {SURFACE_HI};
    color: {TEXT};
    border: 1px solid {BORDER_HI};
    border-radius: {RADIUS_SM}px;
    padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(34, 211, 238, 0.18); }}

/* ════════ 分隔线 ════════ */
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}

/* ════════ 文案层级 ════════ */
QLabel#h1 {{
    font-size: 21px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.4px;
}}
QLabel#h2 {{
    font-size: 15px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#dim {{ color: {TEXT_DIM}; font-size: 12.5px; }}
QLabel#mute {{ color: {TEXT_MUTE}; font-size: 11px; }}
QLabel#metric {{
    font-size: 30px;
    font-weight: 700;
    color: {ACCENT};
}}
QLabel#metric_ok {{ font-size: 30px; font-weight: 700; color: {OK}; }}
QLabel#metric_warn {{ font-size: 30px; font-weight: 700; color: {WARN}; }}
QLabel#brand {{
    font-size: 17px;
    font-weight: 700;
    color: {TEXT};
    letter-spacing: 0.6px;
}}
QLabel#brand_accent {{
    font-size: 17px;
    font-weight: 700;
    color: {ACCENT};
    letter-spacing: 0.6px;
}}
"""


# ── 构件工厂 ──────────────────────────────────────────────
def card(title: str = '', hint: str = '') -> QFrame:
    """统一卡片：顶部标题 + 可选说明，内容由调用方补进 layout"""
    f = QFrame()
    f.setObjectName('card')
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    if title:
        t = QLabel(title)
        t.setObjectName('h2')
        lay.addWidget(t)
    if hint:
        h = QLabel(hint)
        h.setObjectName('dim')
        h.setWordWrap(True)
        lay.addWidget(h)
    return f


def pill(text: str, kind: str = 'ok') -> QLabel:
    """状态胶囊：ok / warn / err / info"""
    color = {'ok': OK, 'warn': WARN, 'err': ERR,
             'info': ACCENT}.get(kind, ACCENT)
    lb = QLabel(text)
    lb.setStyleSheet(
        "QLabel {{"
        " background: rgba(255,255,255,0.04);"
        " border: 1px solid {c};"
        " border-radius: 11px;"
        " padding: 3px 12px;"
        " color: {c};"
        " font-size: 12px;"
        " font-weight: 600;"
        "}}".format(c=color))
    return lb


def pill_set(label: QLabel, text: str, kind: str = 'ok'):
    """更新胶囊的文字与配色

    直接 setText 会保留旧颜色，直接 setStyleSheet 又会把胶囊形状冲掉 ——
    所以统一走这里：文字和样式一起重刷。
    """
    try:
        fresh = pill(text, kind)
        label.setText(fresh.text())
        label.setStyleSheet(fresh.styleSheet())
    except Exception:
        try:
            label.setText(text)
        except Exception:
            pass


def section(title: str) -> QLabel:
    """分区小标题（左侧一条竖线）"""
    lb = QLabel(title)
    lb.setStyleSheet(
        "QLabel {"
        " color: {t};"
        " font-size: 12px;"
        " font-weight: 700;"
        " letter-spacing: 1px;"
        " border-left: 3px solid {a};"
        " padding-left: 8px;"
        "}".format(t=TEXT_DIM, a=ACCENT))
    return lb


def header(title: str, subtitle: str = '', right_widgets=None) -> QFrame:
    """页面头部：大标题 + 副标题 + 右侧挂件区"""
    f = QFrame()
    f.setStyleSheet("QFrame {{ background: transparent; border: none; }}")
    lay = QHBoxLayout(f)
    lay.setContentsMargins(2, 0, 2, 4)
    lay.setSpacing(10)

    left = QVBoxLayout()
    left.setSpacing(2)
    t = QLabel(title)
    t.setObjectName('h1')
    left.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName('dim')
        left.addWidget(s)
    lay.addLayout(left)
    lay.addStretch(1)
    for w in (right_widgets or []):
        lay.addWidget(w)
    return f


def divider() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet("QFrame {{ background: {}; border: none; }}".format(BORDER))
    return f


def tune_table(table, min_width: int = 70):
    """统一表格列宽策略：内容自适应 + 末列拉伸

    原来各页表格列宽写死或默认均分，长内容（MD5、文件名）会被截成
    "011f5ee..."，看着像数据缺失。统一成按内容自适应后，短列不浪费、
    长列完整显示，剩余空间给最后一列。
    """
    try:
        from PyQt6.QtWidgets import QHeaderView
        hh = table.horizontalHeader()
        for c in range(table.columnCount()):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
            if table.columnWidth(c) < min_width:
                table.setColumnWidth(c, min_width)
        hh.setStretchLastSection(True)
        table.verticalHeader().setDefaultSectionSize(28)
    except Exception:
        pass


def apply_dark_titlebar(window):
    """把 Windows 原生标题栏改成深色，和界面配色一致

    默认标题栏是浅色的，深色界面顶一条白杠很突兀。
    用 DWM 的两个属性：
      DWMWA_USE_IMMERSIVE_DARK_MODE (20，Win10 20H1+；旧版是 19)
      DWMWA_CAPTION_COLOR / DWMWA_TEXT_COLOR (35/36，仅 Win11)
    Win10 只认前者，Win11 两者都认 —— 所以两个都试，谁成功算谁。
    """
    if os.name != 'nt':
        return False
    try:
        import ctypes

        hwnd = int(window.winId())
        if not hwnd:
            return False

        dwm = ctypes.windll.dwmapi

        # 1) 深色模式标志：20 优先，19 是旧版编号
        enabled = ctypes.c_int(1)
        ok = False
        for attr in (20, 19):
            try:
                r = dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(enabled), ctypes.sizeof(enabled))
                if r == 0:
                    ok = True
                    break
            except Exception:
                continue

        # 2) Win11：直接指定标题栏/文字颜色（COLORREF = 0x00BBGGRR）
        def _colorref(hex_rgb: str) -> int:
            h = hex_rgb.lstrip('#')
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return (b << 16) | (g << 8) | r

        for attr, hex_color in ((35, BG_ALT), (36, TEXT)):
            try:
                c = ctypes.c_int(_colorref(hex_color))
                dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c),
                                          ctypes.sizeof(c))
            except Exception:
                pass

        return ok
    except Exception:
        return False
