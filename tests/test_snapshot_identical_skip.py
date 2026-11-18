# -*- coding: utf-8 -*-
"""跨快照「内容相同就跳过」的回归测试（2026-11-17）

与 test_snapshot_dedupe.py 的区别：那份测的是**一份快照内部**的重复文件去重
（同内容只落盘一份 + 清单）。这里测的是**跨时间**的两份快照 —— 内容完全一样时
干脆不再产新包。实测 58 份 × 14.8 MB ≈ 2.68 GB，其中 20:31 / 20:40 / 20:41 三份
几乎相同，白占 30 MB。

锁住的行为：
  1. 轻量快照内容与上一份一致 → 跳过，返回上一份，磁盘上不增包
  2. 内容一变（改了文件）→ 照常产新包
  3. 完整快照不参与去重（它是定时恢复点，宁可多存一份）
  4. 崩溃现场（notes 含"崩溃"）不参与去重
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_dupskip_'))

PASS, FAIL = [], []


def check(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ('' if cond else '  ' + str(extra)))


def make_env(tag):
    root = Path(tempfile.mkdtemp(prefix='zf_ds_' + tag + '_'))
    dsh = root / ".dsh"
    (dsh / "profiles" / "desktop").mkdir(parents=True)
    (dsh / "profiles" / "desktop" / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (dsh / "profiles" / "desktop" / "a.js").write_text("// v1\n", encoding="utf-8")
    (root / "runtime").mkdir(exist_ok=True)
    cfg = ZeroFallConfig()
    cfg.deepseek_plugins_dir = str(dsh)
    cfg.deepseek_config_dir = str(root / "runtime")
    cfg.deepseek_session_dir = str(dsh / "sessions")
    cfg.backup_session_data = False
    cfg.snapshot_dir = str(root / "snaps")
    cfg.dsh_appdata_dir = ""
    cfg.backup_plugin_config = False
    return cfg, dsh


from config import ZeroFallConfig                                   # noqa: E402
from modules import snapshot_manager as sm                          # noqa: E402

cfg, dsh = make_env('a')
mgr = sm.SnapshotManager(cfg)

print('== 1. 内容相同的第二份轻量快照被跳过 ==')
s1 = mgr.create_snapshot('轻量1', include_runtime=False, is_full=False)
s2 = mgr.create_snapshot('轻量2', include_runtime=False, is_full=False)
zips = list(Path(cfg.snapshot_dir).glob("*.zip"))
check('第一份已生成', s1 is not None)
check('第二份复用上份（同一个 id）',
      s2 is not None and s1 is not None and s2.snapshot_id == s1.snapshot_id,
      (getattr(s1, 'snapshot_id', None), getattr(s2, 'snapshot_id', None)))
check('磁盘上只有一个包', len(zips) == 1, [p.name for p in zips])

print('== 2. 内容变了就照常产新包 ==')
time.sleep(1)
(dsh / "profiles" / "desktop" / "a.js").write_text("// v2 changed\n", encoding="utf-8")
s3 = mgr.create_snapshot('轻量3', include_runtime=False, is_full=False)
zips = list(Path(cfg.snapshot_dir).glob("*.zip"))
check('产出了新快照', s3 is not None and s3.snapshot_id != getattr(s1, 'snapshot_id', None),
      getattr(s3, 'snapshot_id', None))
check('磁盘上变成两个包', len(zips) == 2, [p.name for p in zips])

print('== 3. 完整快照不参与去重 ==')
s4 = mgr.create_snapshot('完整', include_runtime=False, is_full=True)
s5 = mgr.create_snapshot('完整再打', include_runtime=False, is_full=True)
check('完整快照每份都真的生成',
      s4 is not None and s5 is not None and s4.snapshot_id != s5.snapshot_id,
      (getattr(s4, 'snapshot_id', None), getattr(s5, 'snapshot_id', None)))

print('== 4. 崩溃现场不参与去重 ==')
s6 = mgr.create_snapshot('崩溃快照 (退出码 1)', include_runtime=False, is_full=False)
s7 = mgr.create_snapshot('崩溃快照 (退出码 1) 再打', include_runtime=False, is_full=False)
check('崩溃快照每份都真的生成',
      s6 is not None and s7 is not None and s6.snapshot_id != s7.snapshot_id,
      (getattr(s6, 'snapshot_id', None), getattr(s7, 'snapshot_id', None)))

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
sys.exit(0 if not FAIL else 1)
