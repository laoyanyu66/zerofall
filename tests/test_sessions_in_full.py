# -*- coding: utf-8 -*-
"""每份完整快照都带会话（2026-11-16 二次修正）的回归测试

起因：`backup_session_data=True` 以前名存实亡 —— `_should_include_sessions()`
收的是 `include_runtime`（=运行时版本变了没有），于是只有"换 DSH 版本那天"的
完整快照才带会话；实测 52 份快照里只有 1 份有 `sessions/`。

修正后必须锁住的行为：
  1. 完整快照（is_full=True，哪怕没带运行时）→ 一定带 sessions/
  2. 轻量快照（is_full=False）→ 一定不带
  3. 调用方显式 include_sessions 优先于配置
  4. `backup_session_data=False` 时一律不带
  5. 带会话的完整快照算"大包"：保留策略吃 max_full，云端独占一轮窗口
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_sessfull_'))

from config import ZeroFallConfig                                   # noqa: E402
from modules import snapshot_manager as sm                          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_env(tag):
    root = Path(tempfile.mkdtemp(prefix='zf_sf_' + tag + '_'))
    dsh = root / ".dsh"
    (dsh / "profiles" / "desktop").mkdir(parents=True)
    (dsh / "profiles" / "desktop" / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    sessions = dsh / "sessions" / "proj"
    sessions.mkdir(parents=True)
    (sessions / "session-1.jsonl.zstd").write_bytes(b"\x28\xb5\x2f\xfd" + b"s" * 20480)
    cfg = ZeroFallConfig()
    cfg.deepseek_plugins_dir = str(dsh)
    cfg.deepseek_config_dir = str(root / "runtime")
    (root / "runtime").mkdir(exist_ok=True)
    cfg.deepseek_session_dir = str(dsh / "sessions")
    cfg.backup_session_data = True
    cfg.sessions_only_in_full = True
    cfg.snapshot_dir = str(root / "snaps")
    cfg.dsh_appdata_dir = ""
    cfg.backup_plugin_config = False
    return cfg


print('== 1. 完整快照不带运行时也要带会话（本次修正的核心）==')
cfg = make_env('a')
mgr = sm.SnapshotManager(cfg)
full = mgr.create_snapshot('完整无运行时', include_runtime=False, is_full=True)
check('快照已生成', full is not None)
if full:
    with zipfile.ZipFile(full.archive_path) as z:
        names = z.namelist()
    check('is_full=True 且 include_runtime=False 仍带 sessions/',
          any(n.startswith('sessions/') for n in names),
          [n for n in names if n.startswith('sessions/')][:3])
    check('has_session_data 记为 True', full.has_session_data is True,
          full.has_session_data)
    check('is_full / runtime_included 各自独立',
          full.is_full is True and full.runtime_included is False,
          (full.is_full, full.runtime_included))

print('== 2. 轻量快照不带会话 ==')
light = mgr.create_snapshot('轻量', include_runtime=False, is_full=False)
if light:
    with zipfile.ZipFile(light.archive_path) as z:
        lnames = z.namelist()
    check('轻量快照没有 sessions/', not any(n.startswith('sessions/') for n in lnames),
          [n for n in lnames if n.startswith('sessions/')][:3])
    check('has_session_data=False', light.has_session_data is False)
    check('完整包比轻量包大（会话确实进包了）', full.size_mb > light.size_mb,
          (full.size_mb, light.size_mb))

print('== 3. 显式传参优先于配置 ==')
explicit_off = mgr.create_snapshot('完整但显式不要会话', include_runtime=False,
                                   is_full=True, include_sessions=False)
if explicit_off:
    with zipfile.ZipFile(explicit_off.archive_path) as z:
        enames = z.namelist()
    check('include_sessions=False 时不带 sessions/',
          not any(n.startswith('sessions/') for n in enames))
    check('has_session_data=False', explicit_off.has_session_data is False)

print('== 4. 总开关关掉后一律不带 ==')
cfg_off = make_env('b')
cfg_off.backup_session_data = False
mgr_off = sm.SnapshotManager(cfg_off)
off = mgr_off.create_snapshot('关掉会话备份', include_runtime=False, is_full=True)
if off:
    with zipfile.ZipFile(off.archive_path) as z:
        onames = z.namelist()
    check('backup_session_data=False 时完整快照也不带',
          not any(n.startswith('sessions/') for n in onames))
    check('_should_include_sessions 直判为 False',
          mgr_off._should_include_sessions(True) is False)

print('== 5. 带会话的完整快照算"大包" ==')
p = sm.snapshot_is_full_grade
check('带会话、无运行时 → 大包',
      p({'is_full': True, 'runtime_included': False,
         'has_session_data': True, 'size_mb': 140.0}) is True)
check('只有运行时 → 大包',
      p({'is_full': True, 'runtime_included': True,
         'has_session_data': False, 'size_mb': 243.0}) is True)
check('既没运行时也没会话的完整快照 → 不算大包',
      p({'is_full': True, 'runtime_included': False,
         'has_session_data': False, 'size_mb': 48.0}) is False)
check('轻量快照 → 不算大包',
      p({'is_full': False, 'has_session_data': False, 'size_mb': 14.0}) is False)
check('snapshot_has_sessions 直判',
      sm.snapshot_has_sessions({'has_session_data': True}) is True
      and sm.snapshot_has_sessions({'has_session_data': False}) is False)
check('云端只留最新大包（会话包会在候选里）',
      sm.pick_runtime_full_for_cloud(
          [{'snapshot_id': 'L', 'created_at': '20261116_100000',
            'is_full': False, 'has_session_data': False, 'size_mb': 14.0},
           {'snapshot_id': 'S1', 'created_at': '20261116_101000',
            'is_full': True, 'runtime_included': False,
            'has_session_data': True, 'size_mb': 140.0}], keep=1)[0]['snapshot_id'] == 'S1')

print('== 6. 保留策略：会话包吃 max_full，不挤轻量名额 ==')
snaps = {}
for i in range(3):
    created = '20261116_10{}00'.format(i + 1)      # 10:10 / 10:20 / 10:30
    sid = 'snapshot_' + created
    snaps[sid] = sm.SnapshotData(snapshot_id=sid, created_at=created,
                                 status=sm.SnapshotStatus.HEALTHY, plugin_count=1,
                                 size_mb=14.0, md5_checksum='', archive_path='',
                                 is_full=False)
sess = sm.SnapshotData(snapshot_id='snapshot_20261116_110000',
                       created_at='20261116_110000', status=sm.SnapshotStatus.HEALTHY,
                       plugin_count=1, size_mb=140.0, md5_checksum='', archive_path='',
                       is_full=True, runtime_included=False, has_session_data=True)
snaps[sess.snapshot_id] = sess
doomed = sm.plan_retention(snaps, max_light=2, max_full=12)
check('轻量超额 → 删最老轻量', 'snapshot_20261116_10100' in doomed, doomed)
check('会话完整包不在轻量配额里被淘汰', sess.snapshot_id not in doomed, doomed)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
