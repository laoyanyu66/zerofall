# -*- coding: utf-8 -*-
"""UI 截图工具：把每个页签离屏渲染成 PNG，便于肉眼核对视觉

用法： python ui_shot.py [宽] [高]
输出： %TEMP%\\zf_ui\\page_N.png

为什么需要：改样式最容易"看着代码没问题，实际糊成一团"。
离屏渲染 + 截图能直接看到真实效果，不用反复手动开程序。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(  # 脚本在 tools/ 下，模块在上一级
    os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('ZF_DATA_DIR', r'D:\zerofall')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase


def load_cn_font(app):
    """离屏环境没有字体目录，必须手工挂一个中文字体，否则全是方框"""
    cands = [
        r'C:\Windows\Fonts\msyh.ttc',
        r'C:\Windows\Fonts\msyhbd.ttc',
        r'C:\Windows\Fonts\simhei.ttf',
        r'C:\Windows\Fonts\simsun.ttc',
        r'C:\Windows\Fonts\Deng.ttf',
    ]
    for p in cands:
        if os.path.exists(p):
            fid = QFontDatabase.addApplicationFont(p)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                app.setFont(QFont(fams[0], 10))
                print('  已加载字体: {} -> {}'.format(os.path.basename(p), fams[0]))
                return fams[0]
    print('  警告: 没找到中文字体，截图会显示方框')
    return None


def main():
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 780

    app = QApplication([])
    app.setStyle('Fusion')
    load_cn_font(app)

    from ui import theme
    app.setStyleSheet(theme.qss())

    from config import ConfigManager
    cm = ConfigManager()
    cm.load()

    from ui.main_window import ZeroFallMainWindow
    win = ZeroFallMainWindow(cm)
    win.resize(w, h)
    win.show()
    for _ in range(4):
        app.processEvents()

    out = os.path.join(os.environ.get('TEMP', '.'), 'zf_ui')
    os.makedirs(out, exist_ok=True)

    n = win.tab_widget.count()
    print('  页签 {} 个，窗口 {}x{}，实际抓图 {}x{}'.format(
        n, w, h, win.width(), win.height()))
    for i in range(n):
        name = win.tab_widget.tabText(i)
        win.tab_widget.setCurrentIndex(i)
        for _ in range(4):
            app.processEvents()
        path = os.path.join(out, 'page_{}.png'.format(i))
        ok = win.grab().save(path)
        print('  {:<8} -> page_{}.png  ({})'.format(
            name, i, 'OK' if ok else '保存失败'))
    print('  输出目录:', out)


if __name__ == '__main__':
    main()
