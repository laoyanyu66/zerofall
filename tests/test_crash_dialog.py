# -*- coding: utf-8 -*-
"""崩溃恢复倒计时与最终确认测试

需求（用户原话）：崩溃后的倒计时从一分钟搞成十分钟，到了十分钟后回退前，
跳一个最终确认弹窗，确认了才能回退。这里把三条语义锁死：

  1. 默认倒计时是 600 秒（10 分钟），不是 60
  2. 倒计时走完不直接回退 —— 必须先弹确认框
  3. 确认框选「暂不」→ 不回退；选「是」→ 才回退
  4. 手动点「立即恢复」不需要二次确认（用户已主动发起）
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_crashdlg_')

from PyQt6.QtWidgets import QApplication, QDialog           # noqa: E402

app = QApplication([])
app.setStyle('Fusion')
from ui import theme                                          # noqa: E402
app.setStyleSheet(theme.qss())

import ui.crash_dialog as CD                                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def snaps(n=3):
    """构造假快照：1 个完整 + 若干轻量，按时间倒序"""
    out = []
    for i in range(n):
        out.append(SimpleNamespace(
            snapshot_id='snapshot_20261109_1{:05d}'.format(i),
            created_at='20261109_1{:05d}'.format(i),
            size_mb=80.0 + i,
            notes='测试快照',
            is_full=(i == 0),
            archive_path='C:/fake/snap_{}.zip'.format(i),
        ))
    return out


def make_dialog(countdown=600, with_snaps=True):
    return CD.CrashRestoreDialog(snaps() if with_snaps else [], None,
                                 countdown_seconds=countdown)


def test_default_countdown():
    print('\n[1] 默认倒计时是 10 分钟')
    check('DEFAULT_COUNTDOWN == 600',
          CD.CrashRestoreDialog.DEFAULT_COUNTDOWN == 600,
          CD.CrashRestoreDialog.DEFAULT_COUNTDOWN)
    d = CD.CrashRestoreDialog(snaps(), None)      # 不传 countdown，用默认
    check('实例倒计时为 600 秒', d._countdown_total == 600, d._countdown_total)
    check('剩余为 600 秒', d._remaining == 600, d._remaining)
    d._timer.stop()
    d.close()


def test_countdown_display():
    print('\n[2] 倒计时按分钟显示，并说明会先确认')
    d = make_dialog(600)
    d._remaining = 600
    d._update_ui()
    txt = d._countdown_label.text()
    check('显示为 10 分', '10 分' in txt, txt)
    check('提示会先弹确认框', '确认' in txt, txt)
    check('_fmt_remaining(600) 含分钟',
          '10 分' in d._fmt_remaining(600), d._fmt_remaining(600))
    check('_fmt_remaining(59) 用秒',
          d._fmt_remaining(59) == '59 秒', d._fmt_remaining(59))
    d._timer.stop()
    d.close()


def test_timeout_requires_confirm_yes():
    print('\n[3] 倒计时到点 → 弹确认 → 选「是」才回退')
    d = make_dialog(600)
    d._selected_id = d._snapshots[0].snapshot_id
    asked = {'n': 0, 'buttons': None, 'default': None}

    def fake_question(parent, title, text, buttons=None, defaultButton=None,
                      *a, **k):
        asked['n'] += 1
        asked['buttons'] = buttons
        asked['default'] = defaultButton
        asked['text'] = text
        return CD.QMessageBox.StandardButton.Yes

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(fake_question)
    try:
        d._remaining = 1
        d._tick()                    # 触发到点
    finally:
        CD.QMessageBox.question = orig

    check('确实弹了确认框', asked['n'] == 1, asked['n'])
    check('提供了是/否两个选项',
          asked['buttons'] == (CD.QMessageBox.StandardButton.Yes
                               | CD.QMessageBox.StandardButton.No),
          asked['buttons'])
    check('默认按钮是「否」（误触安全）',
          asked['default'] == CD.QMessageBox.StandardButton.No,
          asked['default'])
    check('确认文本含目标快照 ID',
          d._snapshots[0].snapshot_id in asked.get('text', ''))
    check('选「是」后对话框被 accept',
          d.result() == QDialog.DialogCode.Accepted, d.result())
    d.close()


def test_timeout_confirm_no_blocks_rollback():
    print('\n[4] 倒计时到点 → 选「暂不」→ 不回退，保留现场')
    d = make_dialog(600)
    d._selected_id = d._snapshots[0].snapshot_id

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(lambda *a, **k: CD.QMessageBox.StandardButton.No)
    try:
        d._remaining = 1
        d._tick()
    finally:
        CD.QMessageBox.question = orig

    check('未被 accept（不回退）',
          d.result() != QDialog.DialogCode.Accepted, d.result())
    check('标记为已取消自动恢复', d._auto_cancelled is True)
    txt = d._countdown_label.text()
    check('提示已取消自动恢复', '已取消' in txt, txt)
    check('拒绝后仍可手动恢复（按钮可用）', d._ok_btn.isEnabled())
    d.close()


def test_manual_confirm_no_second_prompt():
    print('\n[5] 手动点立即恢复 → 不再二次确认')
    d = make_dialog(600)
    d._selected_id = d._snapshots[0].snapshot_id
    asked = {'n': 0}

    def fake_question(*a, **k):
        asked['n'] += 1
        return CD.QMessageBox.StandardButton.Yes

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(fake_question)
    try:
        d._on_confirm()
    finally:
        CD.QMessageBox.question = orig

    check('没有弹确认框', asked['n'] == 0, asked['n'])
    check('直接 accept', d.result() == QDialog.DialogCode.Accepted, d.result())
    d.close()


def test_no_snapshot_case():
    print('\n[6] 无可用快照 → 不执行回退')
    d = make_dialog(600, with_snaps=False)
    asked = {'n': 0}

    def fake_question(*a, **k):
        asked['n'] += 1
        return CD.QMessageBox.StandardButton.Yes

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(fake_question)
    try:
        d._remaining = 1
        d._tick()
    finally:
        CD.QMessageBox.question = orig

    check('没弹确认框（无目标不该问）', asked['n'] == 0, asked['n'])
    check('不回退', d.result() != QDialog.DialogCode.Accepted)
    check('标记已取消', d._auto_cancelled is True)
    d.close()


def test_no_auto_recovery_mode():
    print('\n[7] countdown<=0 → 纯手动，不计时')
    d = make_dialog(0)
    check('倒计时总数为 0', d._countdown_total == 0)
    check('定时器未启动', not d._timer.isActive())
    check('提示不限时', '不限时' in d._countdown_label.text(),
          d._countdown_label.text())
    d.close()


def test_timer_stops_after_cancel():
    print('\n[8] 取消自动恢复后计时器停住，不再反复弹确认')
    d = make_dialog(600)
    d._selected_id = d._snapshots[0].snapshot_id
    asked = {'n': 0}

    def fake_question(*a, **k):
        asked['n'] += 1
        return CD.QMessageBox.StandardButton.No

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(fake_question)
    try:
        d._remaining = 1
        d._tick()
        # 之后再 tick 若干次，不应再弹确认
        for _ in range(5):
            d._tick()
    finally:
        CD.QMessageBox.question = orig

    check('只弹了一次确认框', asked['n'] == 1, asked['n'])
    check('定时器未在运行', not d._timer.isActive())
    d.close()


def test_dialog_renders():
    print('\n[9] 对话框能正常渲染（样式未破坏）')
    d = make_dialog(600)
    d.resize(660, 480)
    d.show()
    for _ in range(3):
        app.processEvents()
    pm = d.grab()
    check('可截图', not pm.isNull())
    check('尺寸合理', pm.width() > 400 and pm.height() > 300,
          (pm.width(), pm.height()))
    d._timer.stop()
    d.close()


if __name__ == '__main__':
    print('=' * 66)
    print('崩溃恢复倒计时与最终确认测试')
    print('=' * 66)
    test_default_countdown()
    test_countdown_display()
    test_timeout_requires_confirm_yes()
    test_timeout_confirm_no_blocks_rollback()
    test_manual_confirm_no_second_prompt()
    test_no_snapshot_case()
    test_no_auto_recovery_mode()
    test_timer_stops_after_cancel()
    test_dialog_renders()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
