# -*- coding: utf-8 -*-
"""对话框外观截图

对话框是"用户主动看到的界面"，最怕写完了没人真看过长什么样。
这里离屏渲染各种对话框，直接出图核对。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(  # 脚本在 tools/ 下，模块在上一级
    os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ.setdefault('ZF_DATA_DIR', r'D:\zerofall')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont, QFontDatabase


def load_font(app):
    for p in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\simhei.ttf'):
        if os.path.exists(p):
            fid = QFontDatabase.addApplicationFont(p)
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                app.setFont(QFont(fams[0], 10))
                return


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    load_font(app)
    from ui import theme
    app.setStyleSheet(theme.qss())

    out = os.path.join(os.environ.get('TEMP', '.'), 'zf_ui')
    os.makedirs(out, exist_ok=True)

    from ui import dialogs as D

    # 1) 提示 / 警告 / 错误
    cases = [
        ('info', '提示', '快照已创建完成。', None),
        ('warning', '注意', '未检测到百度网盘客户端在运行。\n快照已写入本地同步盘，客户端启动后会自动补传。', None),
        ('error', '错误', '快照创建失败，磁盘剩余空间不足。\n请清理磁盘后重试。',
         '可用空间: 0.8 GB / 需要: 1.2 GB'),
    ]
    for kind, title, body, detail in cases:
        dlg = D._NoticeDialog(None, title, body, kind, detail)
        dlg.adjustSize()
        dlg.show()
        for _ in range(3):
            app.processEvents()
        path = os.path.join(out, 'dlg_{}.png'.format(kind))
        dlg.grab().save(path)
        print('  {} -> {}'.format(kind, os.path.basename(path)))
        dlg.close()

    # 2) 询问框
    dlg = D._ConfirmDialog(None, '确认退出',
                           'DeepSeek 正在运行中。\nZeroFall 退出时是否同时终止 DeepSeek？',
                           D.StandardButton.Yes | D.StandardButton.No
                           | D.StandardButton.Cancel, D.StandardButton.Yes)
    dlg.adjustSize()
    dlg.show()
    for _ in range(3):
        app.processEvents()
    dlg.grab().save(os.path.join(out, 'dlg_question.png'))
    print('  question -> dlg_question.png')
    dlg.close()

    # 3) 环境校验结果面板
    from config import ConfigManager
    cm = ConfigManager(r'D:\zerofall\zerofall_config.json')
    cm.load()
    from modules.snapshot_manager import SnapshotManager
    from modules.environment_checker import EnvironmentChecker
    ec = EnvironmentChecker(cm.config, SnapshotManager(cm.config))
    report = ec.run_full_check()

    vd = D.VerifyResultDialog(None, report)
    vd.adjustSize()
    vd.show()
    for _ in range(4):
        app.processEvents()
    vd.grab().save(os.path.join(out, 'dlg_verify.png'))
    print('  verify -> dlg_verify.png   ({} 项, 结论 {})'.format(
        len(report.items), report.overall_result.value))
    vd.close()
    print('  输出目录:', out)


if __name__ == '__main__':
    main()
