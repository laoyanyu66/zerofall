# -*- coding: utf-8 -*-
"""崩溃恢复倒计时测试

需求：崩溃后的自动回退等待时间，从 1 分钟改为 10 分钟。
原因：崩溃时用户往往不在电脑前，60 秒太短 ——
      还没看清是哪个快照就被自动恢复了，反而拿不到想要的恢复点。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from PyQt6.QtWidgets import QApplication

app = QApplication([])

from ui.crash_dialog import CrashRestoreDialog

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def fake_snaps(n=3):
    """构造最小可用的快照桩（对话框只用到 snapshot_id / timestamp / is_full / notes）"""
    import datetime as dt

    class S:
        def __init__(self, i, full):
            self.snapshot_id = 'snapshot_20261109_1{:04d}'.format(i)
            self.timestamp = dt.datetime.now() - dt.timedelta(minutes=i * 5)
            # 对话框真实用到的字段是 created_at（字符串）
            self.created_at = self.timestamp.strftime('%Y%m%d_%H%M%S')
            self.is_full = full
            self.notes = '测试快照{}'.format(i)
            self.size_mb = 50.76
            self.status = type('X', (), {'value': 'healthy'})()

    return [S(1, True), S(2, False), S(3, False)][:n]


print('=== 1. 默认倒计时是 10 分钟 ===')
chk(CrashRestoreDialog.DEFAULT_COUNTDOWN == 600,
    '类默认值 = 600 秒（10 分钟）', CrashRestoreDialog.DEFAULT_COUNTDOWN)

d = CrashRestoreDialog(fake_snaps())
chk(d._countdown_total == 600, '实例倒计时 = 600', d._countdown_total)
chk(d._remaining == 600, '剩余 = 600', d._remaining)
chk(d._timer.isActive() is True, '定时器已启动')

print()
print('=== 2. 剩余时间显示成分:秒，而不是「600 秒」===')
chk(d._fmt_remaining(600) == '10 分 00 秒', '600 → 10 分 00 秒', d._fmt_remaining(600))
chk(d._fmt_remaining(59) == '59 秒', '59 → 59 秒', d._fmt_remaining(59))
chk(d._fmt_remaining(90) == '1 分 30 秒', '90 → 1 分 30 秒', d._fmt_remaining(90))
chk(d._fmt_remaining(60) == '1 分 00 秒', '60 → 1 分 00 秒', d._fmt_remaining(60))

print()
print('=== 3. 界面文案含「分」===')
d._update_ui()
txt = d._countdown_label.text()
chk('分' in txt, '倒计时标签显示分钟', txt[:40])

print()
print('=== 4. 可配置：传 120 秒 ===')
d2 = CrashRestoreDialog(fake_snaps(), countdown_seconds=120)
chk(d2._countdown_total == 120, '倒计时 = 120', d2._countdown_total)
d2._update_ui()
chk('2 分 00 秒' in d2._countdown_label.text(), '显示 2 分 00 秒',
    d2._countdown_label.text()[:40])

print()
print('=== 5. 传 0 = 不限时，必须手动点 ===')
d3 = CrashRestoreDialog(fake_snaps(), countdown_seconds=0)
chk(d3._countdown_total == 0, '倒计时 = 0', d3._countdown_total)
chk(d3._timer.isActive() is False, '定时器未启动（不会自动恢复）')
d3._tick()          # 即便被误调也不该推进
chk(d3._remaining == 0, '手动调用 tick 也不会自减', d3._remaining)
d3._update_ui()
chk('不限时' in d3._countdown_label.text(), '提示不限时',
    d3._countdown_label.text()[:40])

print()
print('=== 6. 每秒递减，且到 0 前不会提前触发 ===')
d4 = CrashRestoreDialog(fake_snaps(), countdown_seconds=5)
for i in range(4):
    d4._tick()
chk(d4._remaining == 1, '递减 4 次后剩 1 秒', d4._remaining)
chk(d4._timer.isActive() is True, '还剩 1 秒时定时器仍在跑')

print()
print('=== 7. 非法参数回落到默认，不崩 ===')
d5 = CrashRestoreDialog(fake_snaps(), countdown_seconds='abc')
chk(d5._countdown_total == 600, '非法值回落 600', d5._countdown_total)

print()
print('=== 8. 主窗口传入配置值 ===')
import ast
src = (Path('ui/main_window.py')).read_text(encoding='utf-8')
chk('crash_restore_countdown' in src, '主窗口读取 crash_restore_countdown 配置')
chk('countdown_seconds=_cd' in src, '把配置值传给对话框')

import config as cfgmod
import dataclasses
field_names = [f.name for f in dataclasses.fields(cfgmod.ZeroFallConfig)]
chk('crash_restore_countdown' in field_names, '配置项已定义')
_default = cfgmod.ZeroFallConfig()
chk(_default.crash_restore_countdown == 600,
    '默认值 600', _default.crash_restore_countdown)

print()
print('=' * 62)
print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
for f in FAIL:
    print('  - ' + f)
sys.exit(0 if not FAIL else 1)
