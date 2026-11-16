# -*- coding: utf-8 -*-
"""P4：完整快照的配额语义 + 云端大包独占（2026-11-16）

守卫的两件事：

1. "完整快照"名不副实 —— 定时完整快照在 DSH 运行时没变时会跳过
   primary-runtime 省盘，产物只有 20-80 MB，却照旧标 is_full=True 去挤
   max_full_snapshots 那 12 个名额。实测每天一条
   "完整快照超出配置 21/12"，而盘上 21 份里有 19 份跟轻量包一样大。
   → snapshot_has_runtime() 分清"真带运行时"和"只是标了完整"，
     plan_retention / get_overflow_snapshots 按实际内容分档。

2. 含运行时的完整包 165-242 MB，一个就把 60 分钟的云同步窗口吃满，
   同批轻量包全被挤到下一轮（07:41 实测窗口里只推进了这一个包）。
   → pick_runtime_full_for_cloud() 只留最新 N 份，传它那一轮让它独占。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_p4_'))

from modules import snapshot_manager as sm            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def snap(sid, created, is_full=False, runtime_included=None, size_mb=0.0,
         status=None):
    return SimpleNamespace(snapshot_id=sid, created_at=created,
                           is_full=is_full, runtime_included=runtime_included,
                           size_mb=size_mb,
                           status=status or sm.SnapshotStatus.HEALTHY)


print('== snapshot_has_runtime：判据 ==')
check('轻量快照 → 不含运行时',
      sm.snapshot_has_runtime(snap('L1', '20261116_090000')) is False)
check('新索引 runtime_included=False（跳过运行时）→ 不含',
      sm.snapshot_has_runtime(snap('F1', '20261116_090000', True, False, 48.1)) is False)
check('新索引 runtime_included=True → 含',
      sm.snapshot_has_runtime(snap('F2', '20261116_090000', True, True, 242.4)) is True)
check('新索引写 False 但体积很大 → 仍信字段（不含）',
      sm.snapshot_has_runtime(snap('F3', '20261116_090000', True, False, 300.0)) is False)
check('老索引无字段 + 242 MB → 含运行时（粗判）',
      sm.snapshot_has_runtime(snap('F4', '20261116_090000', True, None, 242.4)) is True)
check('老索引无字段 + 48 MB → 不含（粗判）',
      sm.snapshot_has_runtime(snap('F5', '20261116_090000', True, None, 48.1)) is False)
check('老索引无字段 + 体积未知 → 保守当完整',
      sm.snapshot_has_runtime(snap('F6', '20261116_090000', True, None, 0.0)) is True)
check('dict 探针（diagnostics 用）也能判',
      sm.snapshot_has_runtime({'is_full': True, 'runtime_included': False,
                               'size_mb': 48.1}) is False)

print('== plan_retention：按实际内容分档 ==')
lights = [snap('L{}'.format(i), '20261116_09{:02d}00'.format(i)) for i in range(5)]
real_full = [snap('RF{}'.format(i), '20261116_1{}0000'.format(i), True, True, 242.0)
             for i in range(3)]
degraded = [snap('DF{}'.format(i), '20261116_2{}0000'.format(i), True, False, 48.0)
            for i in range(3)]
all_snaps = {s.snapshot_id: s for s in lights + real_full + degraded}

doomed = sm.plan_retention(all_snaps, max_light=2, max_full=1)
check('真完整 3 份 / 上限 1 → 删最老 2 份',
      sorted(x for x in doomed if x.startswith('RF')) == ['RF0', 'RF1'], doomed)
check('无运行时完整不占完整名额（仍按轻量删 3 份）',
      sorted(x for x in doomed if x.startswith('DF')) == ['DF0'], doomed)
check('轻量桶（轻量 + 无运行时完整）一起压到上限 2：删最老 5 份轻量',
      sorted(x for x in doomed if x.startswith('L')) == ['L0', 'L1', 'L2', 'L3', 'L4'],
      doomed)
check('清完之后轻量口径正好剩 2 份',
      len(doomed) == 8, doomed)

doomed = sm.plan_retention(
    {s.snapshot_id: s for s in degraded}, max_light=99, max_full=1)
check('10 份以下的无运行时完整不吃 max_full（上限 1 也不删）', doomed == [], doomed)

doomed = sm.plan_retention(all_snaps, max_light=2, max_full=1, protected_ids={'RF0'})
check('protected 的真完整不删', 'RF0' not in doomed, doomed)
check('保护后仍压到上限', len([x for x in doomed if x.startswith('RF')]) == 1, doomed)

doomed = sm.plan_retention(all_snaps, max_light=99, max_full=99)
check('上限够大 → 一份不删', doomed == [], doomed)

print('== get_overflow_snapshots：无运行时完整算轻量桶 ==')
mgr = object.__new__(sm.SnapshotManager)
mgr.config = SimpleNamespace(max_light_snapshots=2, max_full_snapshots=1,
                             max_crashed_snapshots=1)
mgr.snapshots = dict(all_snaps)
light_ov, full_ov, crash_ov = mgr.get_overflow_snapshots()
check('轻量溢出桶里有轻量 + 无运行时完整',
      sorted([s.snapshot_id for s in light_ov]) ==
      ['DF0', 'L0', 'L1', 'L2', 'L3', 'L4'],
      [s.snapshot_id for s in light_ov])
check('完整溢出桶只放真完整',
      sorted([s.snapshot_id for s in full_ov]) == ['RF0', 'RF1'],
      [s.snapshot_id for s in full_ov])
check('崩溃桶为空', crash_ov == [], crash_ov)

print('== pick_runtime_full_for_cloud：云端只留最新 N 份 ==')
picked = sm.pick_runtime_full_for_cloud(lights + real_full + degraded, keep=1)
check('keep=1 → 只选最新一份真完整',
      [s.snapshot_id for s in picked] == ['RF2'], [s.snapshot_id for s in picked])
picked = sm.pick_runtime_full_for_cloud(lights + real_full + degraded, keep=2)
check('keep=2 → 最新两份',
      [s.snapshot_id for s in picked] == ['RF1', 'RF2'], [s.snapshot_id for s in picked])
check('keep=0 → 一份都不传',
      sm.pick_runtime_full_for_cloud(real_full, keep=0) == [])
check('没有真完整 → 空（不会把无运行时的当大包）',
      sm.pick_runtime_full_for_cloud(lights + degraded, keep=1) == [])
check('keep 传坏值 → 退回 1',
      len(sm.pick_runtime_full_for_cloud(real_full, keep='x')) == 1)

print('== diagnostics.retention_reconcile：告警措辞与计数 ==')
try:
    from modules import diagnostics as diag
    tmp = Path(tempfile.mkdtemp(prefix='zf_p4_reconcile_'))
    for name, mb in (('snapshot_20261116_090000', 48.1),
                     ('snapshot_20261116_100000', 20.1),
                     ('snapshot_20261116_110000', 242.4)):
        (tmp / (name + '.zip')).write_bytes(b'x' * 1024)
    idx = {
        'snapshot_20261116_090000': {
            'snapshot_id': 'snapshot_20261116_090000',
            'is_full': True, 'runtime_included': False, 'size_mb': 48.1},
        'snapshot_20261116_100000': {
            'snapshot_id': 'snapshot_20261116_100000',
            'is_full': False, 'runtime_included': None, 'size_mb': 20.1},
        'snapshot_20261116_110000': {
            'snapshot_id': 'snapshot_20261116_110000',
            'is_full': True, 'runtime_included': True, 'size_mb': 242.4},
    }
    (tmp / 'snapshot_index.json').write_text(json.dumps(idx), encoding='utf-8')
    cfg = SimpleNamespace(snapshot_dir=str(tmp), max_light_snapshots=48,
                          max_full_snapshots=12, silent_snapshot_interval_minutes=10,
                          full_snapshot_interval_minutes=30)
    rep = diag.retention_reconcile(cfg, snapshot_dir=str(tmp))
    check('无运行时完整计入轻量桶（2 份）', rep['disk']['light'] == 2, rep['disk'])
    check('真完整单独计 1 份', rep['disk']['full'] == 1, rep['disk'])
    check('无运行时完整数量单独可读', rep['disk']['full_degraded'] == 1, rep['disk'])
    check('不再报"完整快照超出配置"',
          all('超出' not in n for n in rep['notes']), rep['notes'])
    check('告警级别不被误报成 warning', rep['severity'] == 'info', rep['severity'])
    check('notes 里有"完整快照（含运行时）"的写法',
          any('含运行时' in n for n in rep['notes']), rep['notes'])
    # 稳态估算按"实际份数"算，不能拿 12 × 242 MB 去吓自己：
    # 只有 1 份真完整时，max_full 写 12 或 1 应该算出同一个数
    cfg1 = SimpleNamespace(snapshot_dir=str(tmp), max_light_snapshots=48,
                           max_full_snapshots=1, silent_snapshot_interval_minutes=10,
                           full_snapshot_interval_minutes=30)
    rep1 = diag.retention_reconcile(cfg1, snapshot_dir=str(tmp))
    check('稳态预估按实际份数（max_full=12 与 1 结果相同）',
          rep['steady_state_gb'] == rep1['steady_state_gb'],
          (rep['steady_state_gb'], rep1['steady_state_gb']))
except Exception as e:                                   # pragma: no cover
    check('diagnostics.retention_reconcile 可运行', False, repr(e))

print('== 索引回填：缺 runtime_included 的完整快照按包内实况补 ==')
import zipfile                                            # noqa: E402
bk = Path(tempfile.mkdtemp(prefix='zf_backfill_'))       # noqa: E402


def _mkzip(name, members):
    p = bk / name
    with zipfile.ZipFile(p, 'w') as z:
        for m in members:
            z.writestr(m, 'x')
    return str(p)


bk_cfg = SimpleNamespace(snapshot_dir=str(bk))
bk_mgr = sm.SnapshotManager(bk_cfg)
bk_mgr._save_index = lambda: None          # 不落盘，只测判定
cases = {
    # 真带运行时：包里有 config/primary-runtime/...
    'snapshot_20261116_081300': (_mkzip('snapshot_20261116_081300.zip',
                                        ['config/primary-runtime/node.exe']), True),
    # 只是标了完整、运行时被跳过
    'snapshot_20261116_084332': (_mkzip('snapshot_20261116_084332.zip',
                                        ['config/plugins/a.js', '.zf_full']), False),
}
for sid, (arc, _expect) in cases.items():
    bk_mgr.snapshots[sid] = sm.SnapshotData(
        snapshot_id=sid, created_at=sid.replace('snapshot_', ''),
        status=sm.SnapshotStatus.HEALTHY, plugin_count=0, size_mb=79.6,
        md5_checksum='', archive_path=arc, is_full=True, runtime_included=None)
# 轻量快照不参与回填；包已不在的完整快照也不猜
bk_mgr.snapshots['snapshot_20260101_000000'] = sm.SnapshotData(
    snapshot_id='snapshot_20260101_000000', created_at='20260101_000000',
    status=sm.SnapshotStatus.HEALTHY, plugin_count=0, size_mb=48.1,
    md5_checksum='', archive_path='', is_full=False)
bk_mgr.snapshots['snapshot_20260102_000000'] = sm.SnapshotData(
    snapshot_id='snapshot_20260102_000000', created_at='20260102_000000',
    status=sm.SnapshotStatus.HEALTHY, plugin_count=0, size_mb=79.6,
    md5_checksum='', archive_path=str(bk / 'not-there.zip'), is_full=True,
    runtime_included=None)

filled = bk_mgr.backfill_runtime_included(save=False)
check('只回填"标了完整且缺字段"的两份', filled == 2, filled)
check('真带运行时的包被判成 True（79.6 MB 也不会被体积误判）',
      bk_mgr.snapshots['snapshot_20261116_081300'].runtime_included is True,
      bk_mgr.snapshots['snapshot_20261116_081300'].runtime_included)
check('运行时被跳过的包判成 False',
      bk_mgr.snapshots['snapshot_20261116_084332'].runtime_included is False,
      bk_mgr.snapshots['snapshot_20261116_084332'].runtime_included)
check('回填后 snapshot_has_runtime 与包内实况一致',
      sm.snapshot_has_runtime(bk_mgr.snapshots['snapshot_20261116_081300']) is True)
check('轻量快照不被写字段',
      bk_mgr.snapshots['snapshot_20260101_000000'].runtime_included is None)
check('包不在的完整快照不瞎猜（仍是 None，靠体积兜底）',
      bk_mgr.snapshots['snapshot_20260102_000000'].runtime_included is None)
check('重复调用幂等（第二次回填 0 份）',
      bk_mgr.backfill_runtime_included(save=False) == 0)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
