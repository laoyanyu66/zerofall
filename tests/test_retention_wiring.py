# -*- coding: utf-8 -*-
"""保留上限的接线测试：真的调 _cleanup_old_snapshots()，看它删不删

两级语义（都验证）：

1. **宽松档（默认）**：只压到上限的 2 倍。中间那段留给
   `main_window._handle_overflow_to_cloud` 的「溢出 → 云归档 → 删本地」，
   本地不能抢在归档前面把历史删掉。
2. **紧急档**：可用空间不足 4 GB（测试里用 `retention_emergency` 标志模拟），
   直接压到保留上限 —— 否则下一次快照写不下，只会留下几百字节残包。

无论哪一档，最新一份完整/轻量、以及 `retention_protected_ids` 点名的都不许动。
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_retw_'))

from modules import snapshot_manager as sm            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def mk(root, sid, is_full=False):
    p = root / 'snapshot_{}.zip'.format(sid)
    p.write_bytes(b'PK\x05\x06' + b'\0' * 18)
    return sm.SnapshotData(
        snapshot_id=sid, created_at=sid, status=sm.SnapshotStatus.HEALTHY,
        plugin_count=0, size_mb=0.0, md5_checksum='', archive_path=str(p),
        is_full=is_full)


def build(max_light, max_full, n_light, n_full, base_day=10):
    root = Path(tempfile.mkdtemp(prefix='zf_snapdir_'))
    cfg = SimpleNamespace(snapshot_dir=str(root), max_light_snapshots=max_light,
                          max_full_snapshots=max_full, max_crashed_snapshots=1,
                          backup_session_data=False, backup_keys=False)
    mgr = sm.SnapshotManager(cfg)
    light_ids = [datetime(2026, 11, base_day, 9, i).strftime('%Y%m%d_%H%M%S')
                 for i in range(n_light)]
    full_ids = [datetime(2026, 11, base_day + 1, 9, i).strftime('%Y%m%d_%H%M%S')
                for i in range(n_full)]
    for sid in light_ids:
        mgr.snapshots[sid] = mk(root, sid)
    for sid in full_ids:
        mgr.snapshots[sid] = mk(root, sid, is_full=True)
    return root, mgr, sorted(light_ids), sorted(full_ids)


def live(root):
    return sorted(p.stem.replace('snapshot_', '') for p in root.glob('snapshot_*.zip'))


print('== 宽松档：只压到 2 倍上限 ==')
root, mgr, lights, fulls = build(3, 2, 8, 5)
mgr._cleanup_old_snapshots()
left = live(root)
check('8 份轻量 / 上限 3 → 留 6（=2 倍窗口）', [s for s in left if s in lights] == lights[-6:],
      [s for s in left if s in lights])
check('5 份完整 / 上限 2 → 留 4（=2 倍窗口）', [s for s in left if s in fulls] == fulls[-4:],
      [s for s in left if s in fulls])
check('索引同步（10 → 10 条都在窗口内）', len(mgr.snapshots) == 10, len(mgr.snapshots))

print('== 宽松档：没到 2 倍就不该动 ==')
root, mgr, lights, fulls = build(3, 2, 5, 3)
mgr._cleanup_old_snapshots()
check('5 份轻量 / 上限 3 → 一份都不删（留给云归档）', live(root) == sorted(lights + fulls), live(root))

print('== 紧急档：压到上限 ==')
root, mgr, lights, fulls = build(3, 2, 8, 5)
mgr.retention_emergency = True
mgr._cleanup_old_snapshots()
left = live(root)
check('轻量压到 3 份', [s for s in left if s in lights] == lights[-3:],
      [s for s in left if s in lights])
check('完整压到 2 份', [s for s in left if s in fulls] == fulls[-2:],
      [s for s in left if s in fulls])
check('最新一份完整一定还在', fulls[-1] in left, left)

print('== 保护位 ==')
root, mgr, lights, fulls = build(1, 1, 4, 2)
mgr.retention_emergency = True
mgr.retention_protected_ids = {lights[0]}
mgr._cleanup_old_snapshots()
left = live(root)
check('protected_ids 内最老的那份没被删', lights[0] in left, left)
check('其余按上限收敛（1 保护 + 1 最新轻量 + 1 完整）', len(left) == 3, left)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
