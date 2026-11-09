# -*- coding: utf-8 -*-
"""插件变更检测测试

核心动机：定时快照的真正缺陷不是"粒度粗"，而是"坏改动一旦被拍进快照，
后面所有快照都带毒"。装个坏插件、10 分钟后才崩，回退到中间的任何一个
定时快照都还是坏的。

所以策略改成两条腿：
  · 定时轻量快照 5 → 10 分钟（只做兜底，降一半 IO）
  · 变更检测：60 秒比对目录指纹，检测到改动立刻打快照
    → 每个回退点精确对应"某次改动之前"

必须锁住的行为：
  1. 首次只记基准，不把启动状态当成"变更"
  2. 指纹变化 → 进入待拍状态，不立刻拍（防抖）
  3. 静置够久且无新变化 → 才拍快照
  4. 开关关闭 → 什么都不做
  5. 指纹只用 stat，不读内容（性能前提）
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_drift_')

from PyQt6.QtWidgets import QApplication            # noqa: E402

app = QApplication.instance() or QApplication([])

from config import ZeroFallConfig                     # noqa: E402
from modules.environment_checker import EnvironmentChecker   # noqa: E402
import ui.main_window as MW                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_env(plug_files=3):
    """造一个假插件目录"""
    root = Path(tempfile.mkdtemp(prefix='zf_plug_'))
    plug = root / 'plugins'
    plug.mkdir()
    for i in range(plug_files):
        (plug / 'plugin{}.js'.format(i)).write_text('v1-' + 'x' * 50, encoding='utf-8')
    cfg_dir = root / 'config'
    cfg_dir.mkdir()
    (cfg_dir / 'cordis.yml').write_text('a: 1', encoding='utf-8')
    return root, plug, cfg_dir


def make_checker(plug, cfg_dir):
    c = ZeroFallConfig()
    c.deepseek_plugins_dir = str(plug)
    c.deepseek_config_dir = str(cfg_dir)
    c.snapshot_dir = str(Path(plug).parent / 'snaps')
    return EnvironmentChecker(c)


class H:
    """最小假窗口：只装变更检测需要的属性"""
    _check_plugin_drift = MW.ZeroFallMainWindow._check_plugin_drift
    _cfg_int = MW.ZeroFallMainWindow._cfg_int

    def __init__(self, checker, enabled=True, settle=20):
        c = ZeroFallConfig()
        c.plugin_drift_snapshot = enabled
        c.plugin_drift_settle_seconds = settle
        self.config = c
        self.env_checker = checker
        self._snapshot_busy = False
        self._daily_full_busy = False
        self.made = []          # 记录打快照的调用

    def _create_drift_snapshot(self):
        self.made.append(time.time())


def test_fingerprint_cheap_and_stable():
    print('\n[1] 指纹稳定且便宜')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    t0 = time.time()
    fp1 = ec.quick_fingerprint()
    dt = (time.time() - t0) * 1000
    fp2 = ec.quick_fingerprint()
    check('指纹非空', bool(fp1), fp1)
    check('两次一致', fp1 == fp2)
    check('耗时 < 500ms（只 stat）', dt < 500, '{:.0f} ms'.format(dt))


def test_fingerprint_changes_on_modify():
    print('\n[2] 改文件内容 → 指纹变化')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    fp1 = ec.quick_fingerprint()
    time.sleep(0.01)
    (plug / 'plugin0.js').write_text('v2-changed-content', encoding='utf-8')
    fp2 = ec.quick_fingerprint()
    check('指纹变了', fp1 != fp2, (fp1[:8], fp2[:8]))


def test_fingerprint_ignores_excluded():
    print('\n[3] 日志等易变文件不影响指纹')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    fp1 = ec.quick_fingerprint()
    (plug / 'debug.log').write_text('noise', encoding='utf-8')      # 排除项
    os.makedirs(plug / 'sessions', exist_ok=True)
    (plug / 'sessions' / 'session.jsonl').write_text('{}', encoding='utf-8')
    fp2 = ec.quick_fingerprint()
    check('写 .log 不改变指纹', fp1 == fp2, (fp1[:8], fp2[:8]))


def test_first_run_records_baseline_only():
    print('\n[4] 首次只记基准，不打快照')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    h = H(ec)
    h._check_plugin_drift()
    check('没有打快照', h.made == [], h.made)
    check('记录了基准', getattr(h, '_drift_fingerprint', None) is not None)


def test_change_enters_pending_not_immediate():
    print('\n[5] 检测到改动 → 先进入待拍，不立刻拍（防抖）')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    h = H(ec, settle=20)
    h._check_plugin_drift()                      # 记基准
    (plug / 'plugin0.js').write_text('changed!', encoding='utf-8')
    h._check_plugin_drift()                      # 发现变化
    check('没有立刻打快照', h.made == [], h.made)
    check('进入待拍状态', getattr(h, '_drift_changed_at', None) is not None)


def test_settled_then_snapshot():
    print('\n[6] 静置够久且无新变化 → 拍快照')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    h = H(ec, settle=0)                          # settle=0 便于测试
    h._check_plugin_drift()                      # 记基准
    (plug / 'plugin0.js').write_text('changed!', encoding='utf-8')
    h._check_plugin_drift()                      # 发现变化，进入待拍
    time.sleep(0.02)
    h._check_plugin_drift()                      # 指纹未再变 + 已静置 → 拍
    check('拍了 1 次快照', len(h.made) == 1, h.made)
    h._check_plugin_drift()                      # 再来一轮不该重复拍
    check('不重复拍', len(h.made) == 1, h.made)


def test_disabled_does_nothing():
    print('\n[7] 开关关闭 → 什么都不做')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    h = H(ec, enabled=False)
    h._check_plugin_drift()
    (plug / 'plugin0.js').write_text('changed!', encoding='utf-8')
    h._check_plugin_drift()
    h._check_plugin_drift()
    check('没有记录基准', getattr(h, '_drift_fingerprint', None) is None)
    check('没有打快照', h.made == [], h.made)


def test_busy_skips():
    print('\n[8] 正在打快照时不检查')
    root, plug, cfg_dir = make_env()
    ec = make_checker(plug, cfg_dir)
    h = H(ec, settle=0)
    h._snapshot_busy = True
    h._check_plugin_drift()
    check('忙碌时没记基准', getattr(h, '_drift_fingerprint', None) is None)


def test_config_defaults():
    print('\n[9] 配置默认值')
    c = ZeroFallConfig()
    check('轻量间隔 10 分钟', c.silent_snapshot_interval_minutes == 10,
          c.silent_snapshot_interval_minutes)
    check('轻量上限 48 个', c.max_light_snapshots == 48, c.max_light_snapshots)
    check('覆盖 8 小时',
          c.max_light_snapshots * c.silent_snapshot_interval_minutes / 60 == 8.0)
    check('变更检测默认开', c.plugin_drift_snapshot is True)
    check('检查间隔 60 秒', c.plugin_drift_check_seconds == 60)
    check('防抖 20 秒', c.plugin_drift_settle_seconds == 20)


def test_timer_registered():
    print('\n[10] 定时器已注册')
    import inspect
    src = inspect.getsource(MW.ZeroFallMainWindow._start_silent_snapshot_timer)
    check('存在 _drift_timer', '_drift_timer' in src)
    check('接到 _check_plugin_drift', '_check_plugin_drift' in src)


if __name__ == '__main__':
    print('=' * 66)
    print('插件变更检测测试')
    print('=' * 66)
    test_fingerprint_cheap_and_stable()
    test_fingerprint_changes_on_modify()
    test_fingerprint_ignores_excluded()
    test_first_run_records_baseline_only()
    test_change_enters_pending_not_immediate()
    test_settled_then_snapshot()
    test_disabled_does_nothing()
    test_busy_skips()
    test_config_defaults()
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
