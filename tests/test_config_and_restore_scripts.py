# -*- coding: utf-8 -*-
"""P5 + P7 的回归测试（2026-11-16）

P5：源码模式必须继承 dist 那份现役配置，否则调试时云窗口字段退回默认值
    （实测分叉 9 项：cloud_client_mode / cloud_window_* / cloud_outbox_*），
    窗口化悄悄失效、百度网盘退回常驻 —— 现场极难发现。

P7：恢复脚本必须和快照一一对应。索引重建时被登记成孤儿的快照只写索引、
    不生成 .cmd，结果换台没装 ZeroFall 的机器时那个包就是死数据
    （实测 snapshot_20261115_142438）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_p57_'))

import config as zf_config                              # noqa: E402
from modules import snapshot_manager as sm              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_base(with_dist: bool):
    """造一个假的"项目根"：可选带 dist/zerofall_config.json"""
    base = Path(tempfile.mkdtemp(prefix='zf_cfg_'))
    (base / 'zerofall_config.json').write_text('{"cloud_client_mode": "always"}',
                                               encoding='utf-8')
    if with_dist:
        (base / 'dist').mkdir()
        (base / 'dist' / 'zerofall_config.json').write_text(
            '{"cloud_client_mode": "windowed"}', encoding='utf-8')
    return base


print('== P5：默认配置路径解析 ==')
prev_frozen = getattr(sys, 'frozen', False)
prev_env = os.environ.pop('ZF_CONFIG_PATH', None)
try:
    sys.frozen = False
    base = make_base(with_dist=True)
    p = zf_config.default_config_path(base)
    check('源码模式 + dist 有配置 → 读 dist 那份',
          p == base / 'dist' / 'zerofall_config.json', p)

    base2 = make_base(with_dist=False)
    p2 = zf_config.default_config_path(base2)
    check('源码模式 + dist 没配置 → 退回源码树那份',
          p2 == base2 / 'zerofall_config.json', p2)

    os.environ['ZF_CONFIG_PATH'] = str(base / 'custom.json')
    check('ZF_CONFIG_PATH 优先于一切',
          zf_config.default_config_path(base) == base / 'custom.json',
          zf_config.default_config_path(base))
    os.environ.pop('ZF_CONFIG_PATH', None)

    sys.frozen = True
    sys.executable = str(base / 'dist' / 'ZeroFall.exe')
    check('打包运行 → exe 同目录',
          zf_config.default_config_path() == base / 'dist' / 'zerofall_config.json',
          zf_config.default_config_path())
finally:
    os.environ.pop('ZF_CONFIG_PATH', None)
    if prev_env is not None:
        os.environ['ZF_CONFIG_PATH'] = prev_env
    sys.frozen = prev_frozen

print('== P5：ConfigManager 真的按这套解析 ==')
mgr = zf_config.ConfigManager()
check('本仓库里 ConfigManager 读到 dist 的现役配置',
      mgr.config_path == Path(zf_config.__file__).parent / 'dist' / 'zerofall_config.json',
      mgr.config_path)
live = json.loads(mgr.config_path.read_text(encoding='utf-8'))
check('现役配置里云窗口字段齐全（不再退化到默认值）',
      all(k in live for k in ('cloud_client_mode', 'cloud_window_start',
                              'cloud_window_minutes', 'cloud_outbox_enabled')),
      sorted([k for k in ('cloud_client_mode', 'cloud_window_start',
                          'cloud_window_minutes', 'cloud_outbox_enabled')
              if k not in live]))

print('== P7：恢复脚本与快照一一对应 ==')
root = Path(tempfile.mkdtemp(prefix='zf_rst_'))
cfg = SimpleNamespace(snapshot_dir=str(root), deepseek_plugins_dir=r'C:\fake\.dsh',
                      backup_plugins=True, backup_plugin_config=False,
                      backup_keys=False)
manager = sm.SnapshotManager(cfg)

arc = root / 'snapshot_20261115_142438.zip'
with __import__('zipfile').ZipFile(arc, 'w') as z:
    z.writestr('plugins/demo.txt', 'x')
manager.snapshots['snapshot_20261115_142438'] = sm.SnapshotData(
    snapshot_id='snapshot_20261115_142438', created_at='20261115_142438',
    status=sm.SnapshotStatus.HEALTHY, plugin_count=1, size_mb=1.0,
    md5_checksum='', archive_path=str(arc), is_full=False)

stats = manager.write_restore_scripts()
check('缺脚本的快照被补上（写出 1 份）', stats['written'] == 1, stats)
cmd = root / '恢复_snapshot_20261115_142438.cmd'
check('脚本文件真的落盘', cmd.is_file(), cmd)
if cmd.is_file():
    body = cmd.read_text(encoding='utf-8', errors='replace')
    check('脚本里引用了对应的 zip', 'snapshot_20261115_142438.zip' in body)
    check('脚本能脱离 ZeroFall 单独跑（用 Expand-Archive）',
          'Expand-Archive' in body)

stats2 = manager.write_restore_scripts()
check('重复调用是幂等的（不再重写）',
      stats2['written'] == 0 and stats2['skipped'] == 1, stats2)

# 索引里有、磁盘上没有的条目：应计入 missing，且不生成脚本
manager.snapshots['snapshot_21111111_000000'] = sm.SnapshotData(
    snapshot_id='snapshot_21111111_000000', created_at='21111111_000000',
    status=sm.SnapshotStatus.HEALTHY, plugin_count=0, size_mb=1.0,
    md5_checksum='', archive_path=str(root / 'snapshot_21111111_000000.zip'),
    is_full=False)
stats3 = manager.write_restore_scripts()
check('索引有、磁盘无 → 计入 missing 且不写脚本',
      stats3['missing'] == 1 and stats3['written'] == 0, stats3)

print('== P7：孤儿脚本仍然会被清掉（不误删有效脚本）==')
(root / '恢复_snapshot_20260101_000000.cmd').write_text('@echo off', encoding='utf-8')
removed = manager.prune_orphan_restore_scripts()
check('指向已不存在快照的脚本被清理', removed == 1, removed)
check('142438 的有效脚本没被误删', cmd.is_file())

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
