# -*- coding: utf-8 -*-
"""QSS 抠细节实验：确认哪种写法能让 QLabel 背景透明

问题：卡片里的 QLabel 显示成比卡片更暗的小色块。
采样发现标签背景是窗口底色 #0b0f17，而不是卡片面 #151c28 ——
说明 `QLabel { background: transparent }` 没盖过 `QWidget { background: ... }`。

这里用最小窗口逐个试写法，直接采样像素判断，不靠猜。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(  # 脚本在 tools/ 下，模块在上一级
    os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PyQt6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout
from PyQt6.QtGui import QFont, QFontDatabase

SURFACE = '#151c28'
BG = '#0b0f17'

VARIANTS = {
    'A 仅 background: transparent': """
QWidget { background: %s; color: #e6edf6; }
QFrame#card { background: %s; border: 1px solid #232c3d; border-radius: 12px; }
QLabel { background: transparent; }
""" % (BG, SURFACE),

    'B background-color: transparent': """
QWidget { background: %s; color: #e6edf6; }
QFrame#card { background: %s; border: 1px solid #232c3d; border-radius: 12px; }
QLabel { background-color: transparent; }
""" % (BG, SURFACE),

    'C 后代选择器 QFrame#card QLabel': """
QWidget { background: %s; color: #e6edf6; }
QFrame#card { background: %s; border: 1px solid #232c3d; border-radius: 12px; }
QFrame#card QLabel { background: transparent; border: none; }
""" % (BG, SURFACE),

    'D objectName 限定 QLabel#dim': """
QWidget { background: %s; color: #e6edf6; }
QFrame#card { background: %s; border: 1px solid #232c3d; border-radius: 12px; }
QLabel { background: transparent; border: none; }
QLabel#dim { color: #93a1b5; background: transparent; }
""" % (BG, SURFACE),

    'E 不给 QWidget 设背景': """
QWidget { color: #e6edf6; }
QFrame#card { background: %s; border: 1px solid #232c3d; border-radius: 12px; }
""" % SURFACE,
}


def sample(px, x, y):
    im = px.toImage()
    c = im.pixelColor(x, y)
    return '#{:02x}{:02x}{:02x}'.format(c.red(), c.green(), c.blue())


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    fid = QFontDatabase.addApplicationFont(r'C:\Windows\Fonts\msyh.ttc')
    if fid >= 0:
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0], 10))

    for name, qss in VARIANTS.items():
        w = QFrame()
        w.resize(320, 120)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        card = QFrame()
        card.setObjectName('card')
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        title = QLabel('标题文字')
        body = QLabel('正文 1234')
        body.setObjectName('dim')
        cl.addWidget(title)
        cl.addWidget(body)
        lay.addWidget(card)

        w.setStyleSheet(qss)
        w.show()
        for _ in range(3):
            app.processEvents()

        px = w.grab()
        # 取卡片内标题所在行的空白处（避开文字笔画）
        card_bg = sample(px, 260, 40)
        label_bg = sample(px, 260, 26)
        ok = (label_bg == card_bg)
        print('  {:<34} 卡片={} 标签={}  {}'.format(
            name, card_bg, label_bg, 'OK 透明' if ok else '✗ 有色块'))


if __name__ == '__main__':
    main()
