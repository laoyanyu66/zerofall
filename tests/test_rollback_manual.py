# -*- coding: utf-8 -*-
"""连续回退转人工确认 + 换更早快照 测试

需求（用户原话）：把这个连续自动回退做成由人工确定的。

原来：连续自动回退超过 max_auto_rollback 次 → 一律停止，用户没得选。
现在：触到上限弹确认框，由人决定继续还是停手。

同时补上一个原本只写在注释里的承诺：
  "_tried_snapshots：已尝试回退过的快照 ID（失败时改用更早的，避免原地重试）"
这个集合以前从未被使用过 —— 回退后再崩溃，弹窗还是默认选同一个快照，
等于原地重试。这里把"换更早的"真正实现并锁住。
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_rb_')

from PyQt6.QtWidgets import QApplication               # noqa: E402

app = QApplication([])
app.setStyle('Fusion')
from ui import theme                                     # noqa: E402
app.setStyleSheet(theme.qss())

import ui.crash_dialog as CD                             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def snaps():
    """3 个完整 + 2 个轻量，按时间倒序（新→旧）"""
    out = []
    for i in range(5):
        out.append(SimpleNamespace(
            snapshot_id='snapshot_20261109_1{:05d}'.format(10 - i),
            created_at='20261109_1{:05d}'.format(10 - i),
            size_mb=200.0 - i,
            notes='周期完整快照' if i < 3 else '定时静默快照',
            is_full=(i < 3),
            archive_path='C:/fake/{}.zip'.format(i),
        ))
    return out


def make(exclude=None):
    return CD.CrashRestoreDialog(snaps(), None, countdown_seconds=600,
                                 exclude_ids=exclude)


def test_default_picks_newest_untried_full():
    print('\n[1] 默认选最新的「未试过」完整快照')
    d = make()
    check('默认选中第一个完整快照',
          d._selected_id == snaps()[0].snapshot_id, d._selected_id)
    d._timer.stop()
    d.close()


def test_excluded_skipped():
    print('\n[2] 已试过的快照会被跳过，改选更早的')
    newest = snaps()[0].snapshot_id
    d = make(exclude={newest})
    check('没有选中已试过的第一个',
          d._selected_id != newest, d._selected_id)
    check('改选了第二个（更早的完整快照）',
          d._selected_id == snaps()[1].snapshot_id, d._selected_id)
    d._timer.stop()
    d.close()


def test_all_full_excluded_falls_back_to_light():
    print('\n[3] 完整快照都试过了 → 退到未试过的轻量快照')
    tried = {s.snapshot_id for s in snaps()[:3]}      # 3 个完整全试过
    d = make(exclude=tried)
    check('选中了轻量快照', d._selected_id in tried or
          d._selected_id == snaps()[3].snapshot_id, d._selected_id)
    check('确实跳出了已试过集合', d._selected_id not in tried, d._selected_id)
    d._timer.stop()
    d.close()


def test_all_excluded_still_selects_something():
    print('\n[4] 全部试过 → 仍可选（不让用户无路可走）')
    tried = {s.snapshot_id for s in snaps()}
    d = make(exclude=tried)
    check('仍然有选中项', bool(d._selected_id), d._selected_id)
    check('手动恢复按钮可用', d._ok_btn.isEnabled())
    d._timer.stop()
    d.close()


def test_excluded_marked_in_list():
    print('\n[5] 列表里标注 [已试过]')
    newest = snaps()[0].snapshot_id
    d = make(exclude={newest})
    texts = [d._list.item(i).text() for i in range(d._list.count())]
    marked = [t for t in texts if '[已试过]' in t]
    check('有一条被标注', len(marked) == 1, len(marked))
    check('标注的是正确那条', newest in marked[0], marked[0][:60])
    # 未试过的应该排在前面
    check('已试过排在列表末尾',
          '[已试过]' in texts[-1], texts[-1][:60])
    d._timer.stop()
    d.close()


def test_ordered_untried_first():
    print('\n[6] 未试过的排在前面，完整优先')
    tried = {snaps()[1].snapshot_id}
    d = make(exclude=tried)
    first = d._list.item(0).text()
    check('首项不含已试过标记', '[已试过]' not in first, first[:50])
    check('首项是完整快照', '[完整]' in first, first[:50])
    d._timer.stop()
    d.close()


def test_retry_after_crash_changes_target():
    print('\n[7] 模拟"回退后又崩溃"：第二次不再用同一个快照')
    d1 = make()
    first_target = d1._selected_id
    d1._timer.stop(); d1.close()

    # 第一次回退成功后，主窗口会把目标记入 _tried_snapshots
    tried = {first_target}
    d2 = make(exclude=tried)
    second_target = d2._selected_id

    check('两次目标不同', first_target != second_target,
          (first_target, second_target))
    check('第二次是更早的快照', second_target == snaps()[1].snapshot_id,
          second_target)
    d2._timer.stop(); d2.close()


def test_countdown_confirm_still_works_with_exclude():
    print('\n[8] 带排除集合时，倒计时确认逻辑不受影响')
    newest = snaps()[0].snapshot_id
    d = make(exclude={newest})
    asked = {'n': 0}

    def fake_q(*a, **k):
        asked['n'] += 1
        return CD.QMessageBox.StandardButton.No

    orig = CD.QMessageBox.question
    CD.QMessageBox.question = staticmethod(fake_q)
    try:
        d._remaining = 1
        d._tick()
    finally:
        CD.QMessageBox.question = orig

    check('仍然弹了一次确认', asked['n'] == 1, asked['n'])
    check('选否后标记取消', d._auto_cancelled is True)
    d.close()


if __name__ == '__main__':
    print('=' * 66)
    print('连续回退转人工确认 + 换更早快照 测试')
    print('=' * 66)
    test_default_picks_newest_untried_full()
    test_excluded_skipped()
    test_all_full_excluded_falls_back_to_light()
    test_all_excluded_still_selects_something()
    test_excluded_marked_in_list()
    test_ordered_untried_first()
    test_retry_after_crash_changes_target()
    test_countdown_confirm_still_works_with_exclude()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
