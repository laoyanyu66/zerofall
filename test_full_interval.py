# -*- coding: utf-8 -*-
"""定时完整快照测试

需求（用户原话）：当天第一次使用时自动建立一个完整快照，然后以这个完整
快照的时间为起点，后面按三十分钟一次建立一个完整快照。

三条核心语义：
  1. 触发依据是**时间**，不是轻量快照个数
  2. 锚点是"当天首个完整快照"，之后每隔 N 分钟一个 —— 因为每次都拿
     "最后一个完整快照"来算，锚点天然跟着走
  3. 定时模式开启时，"每 N 个轻量"老逻辑必须让位，否则会重复打快照

另外要防的坑：
  · DeepSeek 没在跑时不该打（会把半启动状态拍进去）
  · 正在打快照 / 正在恢复时不该插队（两条链路同时写磁盘）
  · 完全没有完整快照时不该抢在"每日首启"前面打（会撞车打两个）
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_fsi_')

from PyQt6.QtWidgets import QApplication             # noqa: E402

app = QApplication.instance() or QApplication([])
from ui import theme                                   # noqa: E402
app.setStyleSheet(theme.qss())

from config import ZeroFallConfig                      # noqa: E402
import ui.main_window as MW                            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


class FakeSnap:
    def __init__(self, created_at, is_full=True):
        self.created_at = created_at
        self.is_full = is_full
        self.snapshot_id = 'snapshot_' + created_at
        self.size_mb = 200.0
        self.archive_path = 'C:/fake/x.zip'


class Harness:
    """最小可用的假主窗口：只装被测方法需要的属性"""

    def __init__(self, config, fulls=None, running=True):
        self.config = config
        self.snapshot_manager = SimpleNamespace(
            get_full_snapshots=lambda: list(fulls or []))
        self.guardian = SimpleNamespace(is_deepseek_running=lambda: running)
        self._snapshot_busy = False
        self._daily_full_busy = False
        self._restoring = False
        self.created = []          # 记录"被要求创建"的调用

    # 把被测方法绑到假对象上
    _last_full_snapshot_time = MW.ZeroFallMainWindow._last_full_snapshot_time
    _check_full_snapshot_interval = MW.ZeroFallMainWindow._check_full_snapshot_interval

    def _create_timed_full_snapshot(self, interval):
        self.created.append(interval)


def cfg(interval=30):
    c = ZeroFallConfig()
    c.full_snapshot_interval_minutes = interval
    return c


def ago(minutes):
    return (datetime.now() - timedelta(minutes=minutes)).strftime('%Y%m%d_%H%M%S')


def test_interval_field_default():
    print('\n[1] 配置默认值')
    c = ZeroFallConfig()
    check('间隔默认 30 分钟', c.full_snapshot_interval_minutes == 30,
          c.full_snapshot_interval_minutes)
    check('保留上限默认 12 个', c.max_full_snapshots == 12, c.max_full_snapshots)
    check('每日首启默认开', c.daily_full_snapshot is True)


def test_last_full_time():
    print('\n[2] 能取到最后一个完整快照的时间')
    h = Harness(cfg(), [FakeSnap(ago(120)), FakeSnap(ago(10)), FakeSnap(ago(60))])
    t = h._last_full_snapshot_time()
    check('取到的是最近的', t is not None and
          abs((datetime.now() - t).total_seconds() / 60 - 10) < 2, t)

    h2 = Harness(cfg(), [])
    check('没有完整快照时返回 None', h2._last_full_snapshot_time() is None)


def test_not_due_no_trigger():
    print('\n[3] 距上次不足间隔 → 不触发')
    h = Harness(cfg(30), [FakeSnap(ago(10))])
    h._check_full_snapshot_interval()
    check('没有创建', h.created == [], h.created)


def test_due_triggers():
    print('\n[4] 满 30 分钟 → 触发一次')
    h = Harness(cfg(30), [FakeSnap(ago(31))])
    h._check_full_snapshot_interval()
    check('触发了 1 次', len(h.created) == 1, h.created)
    check('传入的间隔是 30', h.created and h.created[0] == 30, h.created)


def test_anchor_follows_last():
    print('\n[5] 锚点跟着"最后一个完整快照"走（模拟连续两轮）')
    # 第一轮：上次在 31 分钟前 → 该打
    h = Harness(cfg(30), [FakeSnap(ago(31))])
    h._check_full_snapshot_interval()
    check('第一轮触发', len(h.created) == 1)

    # 假设刚打完（时间=现在），第二轮立刻检查不该再打
    h.snapshot_manager.get_full_snapshots = lambda: [FakeSnap(ago(0))]
    h._check_full_snapshot_interval()
    check('刚打完不重复触发', len(h.created) == 1, h.created)

    # 再过 31 分钟，应该再触发
    h.snapshot_manager.get_full_snapshots = lambda: [FakeSnap(ago(31))]
    h._check_full_snapshot_interval()
    check('下一轮到点又触发', len(h.created) == 2, h.created)


def test_no_full_snapshot_defers_to_daily():
    print('\n[6] 完全没有完整快照 → 让给"每日首启"逻辑，不抢先')
    h = Harness(cfg(30), [])
    h._check_full_snapshot_interval()
    check('没有抢先创建', h.created == [], h.created)


def test_not_running_skips():
    print('\n[7] DeepSeek 没在运行 → 跳过')
    h = Harness(cfg(30), [FakeSnap(ago(60))], running=False)
    h._check_full_snapshot_interval()
    check('没有创建', h.created == [], h.created)


def test_busy_skips():
    print('\n[8] 正在打快照/恢复 → 不插队')
    for attr in ('_snapshot_busy', '_daily_full_busy', '_restoring'):
        h = Harness(cfg(30), [FakeSnap(ago(60))])
        setattr(h, attr, True)
        h._check_full_snapshot_interval()
        check('{} 为真时不触发'.format(attr), h.created == [], h.created)


def test_interval_zero_disables():
    print('\n[9] 间隔设为 0 → 关闭定时模式')
    h = Harness(cfg(0), [FakeSnap(ago(999))])
    h._check_full_snapshot_interval()
    check('不触发', h.created == [], h.created)


def test_counting_mode_yields():
    print('\n[10] 定时模式开启时，"每 N 轻量"必须让位')
    import inspect
    src = inspect.getsource(MW.ZeroFallMainWindow._run_snapshot_lifecycle)
    check('快照生命周期里有 interval>0 的提前返回',
          'if _fi > 0:' in src and 'return' in src,
          '缺少让位逻辑')
    i_guard = src.find('if _fi > 0:')
    i_count = src.find('full_snapshot_every_n_light')
    check('让位判断在计数逻辑之前',
          i_guard != -1 and i_count != -1 and i_guard < i_count,
          (i_guard, i_count))


def test_timer_registered():
    print('\n[11] 60 秒检查定时器已注册')
    import inspect
    src = inspect.getsource(MW.ZeroFallMainWindow._start_silent_snapshot_timer)
    check('存在 _full_check_timer', '_full_check_timer' in src)
    check('间隔是 60 秒', '60000' in src, '应为 60000 毫秒')
    check('接到检查方法', '_check_full_snapshot_interval' in src)


if __name__ == '__main__':
    print('=' * 66)
    print('定时完整快照测试（30 分钟锚点）')
    print('=' * 66)
    test_interval_field_default()
    test_last_full_time()
    test_not_due_no_trigger()
    test_due_triggers()
    test_anchor_follows_last()
    test_no_full_snapshot_defers_to_daily()
    test_not_running_skips()
    test_busy_skips()
    test_interval_zero_disables()
    test_counting_mode_yields()
    test_timer_registered()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
