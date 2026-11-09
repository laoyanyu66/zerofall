# -*- coding: utf-8 -*-
"""验证回退的两种场景：
  场景A：改造了原有插件（改文件 + 新增文件）→ 回退必须完全还原
  场景B：装了新插件 → 回退必须清除
"""
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import ConfigManager
from modules.snapshot_manager import SnapshotManager

cm = ConfigManager(r'D:\zerofall\zerofall_config.json')
cfg = cm.load()
sm = SnapshotManager(cfg)

root = Path(tempfile.mkdtemp(prefix='zf_restore_test_'))
snap = root / 'snapshot' / 'plugins'
target = root / 'live_dsh'
snap.mkdir(parents=True)
target.mkdir()

# ---------- 构造"快照时的干净环境" ----------
(snap / 'dsh-luopan').mkdir()
(snap / 'dsh-luopan' / 'index.js').write_text('ORIGINAL index', encoding='utf-8')
(snap / 'dsh-luopan' / 'client.js').write_text('ORIGINAL client', encoding='utf-8')
(snap / 'dsh-tier-router').mkdir()
(snap / 'dsh-tier-router' / 'index.js').write_text('ORIGINAL router', encoding='utf-8')
(snap / 'profiles').mkdir()
(snap / 'profiles' / 'cordis.patch.yml').write_text('ORIGINAL patch', encoding='utf-8')

# 复刻到 target（模拟"快照那一刻"的现场）
shutil.copytree(snap, target, dirs_exist_ok=True)

# ---------- 场景A：改造原有插件 ----------
(target / 'dsh-luopan' / 'index.js').write_text('MODIFIED broken index', encoding='utf-8')
(target / 'dsh-luopan' / 'new-bad-file.js').write_text('user added file', encoding='utf-8')
(target / 'dsh-luopan' / 'subdir').mkdir()
(target / 'dsh-luopan' / 'subdir' / 'extra.js').write_text('extra', encoding='utf-8')
(target / 'profiles' / 'cordis.patch.yml').write_text('MODIFIED broken patch', encoding='utf-8')
# 原有的 client.js 被用户删掉
(target / 'dsh-luopan' / 'client.js').unlink()

# ---------- 场景B：装了新插件 ----------
(target / 'dsh-newplugin').mkdir()
(target / 'dsh-newplugin' / 'index.js').write_text('new crashing plugin', encoding='utf-8')

# ---------- 保护目录 ----------
for prot in ('dsh-runtimes', 'cache', 'attachments'):
    (target / prot).mkdir()
    (target / prot / 'keep.txt').write_text('keep me', encoding='utf-8')

print('=== 回退前（被改坏的现场）===')
print('  dsh-luopan/index.js  :', (target / 'dsh-luopan' / 'index.js').read_text(encoding='utf-8'))
print('  dsh-luopan 内文件    :', sorted(p.name for p in (target / 'dsh-luopan').iterdir()))
print('  profiles/patch       :', (target / 'profiles' / 'cordis.patch.yml').read_text(encoding='utf-8'))
print('  顶层目录             :', sorted(p.name for p in target.iterdir() if p.is_dir()))

# ---------- 执行回退流程 ----------
pruned = sm._prune_extra_entries(snap, target)
cleaned = sm._restore_dir_clean(snap, target)

print()
print('=== 回退动作 ===')
print('  移走的新增条目 ({}):'.format(len(pruned)), pruned)
print('  删除重建的同名条目数:', cleaned)

print()
print('=== 回退后 ===')
print('  dsh-luopan/index.js  :', (target / 'dsh-luopan' / 'index.js').read_text(encoding='utf-8'))
print('  dsh-luopan/client.js :', (target / 'dsh-luopan' / 'client.js').read_text(encoding='utf-8') if (target/'dsh-luopan'/'client.js').exists() else 'MISSING')
print('  dsh-luopan 内文件    :', sorted(p.name for p in (target / 'dsh-luopan').iterdir()))
print('  profiles/patch       :', (target / 'profiles' / 'cordis.patch.yml').read_text(encoding='utf-8'))
print('  顶层目录             :', sorted(p.name for p in target.iterdir() if p.is_dir()))

print()
print('=== 断言 ===')
ok = True
def chk(cond, desc):
    global ok
    print(('  [OK]  ' if cond else '  [FAIL]') + ' ' + desc)
    ok = ok and cond

# 场景A
chk((target / 'dsh-luopan' / 'index.js').read_text(encoding='utf-8') == 'ORIGINAL index', '改坏的 index.js 已还原为原始内容')
chk((target / 'dsh-luopan' / 'client.js').exists(), '被删掉的 client.js 已恢复')
chk(not (target / 'dsh-luopan' / 'new-bad-file.js').exists(), '改造时新增的 new-bad-file.js 已清除')
chk(not (target / 'dsh-luopan' / 'subdir').exists(), '改造时新增的 subdir 已清除')
chk((target / 'profiles' / 'cordis.patch.yml').read_text(encoding='utf-8') == 'ORIGINAL patch', '改坏的 cordis.patch.yml 已还原')
# 场景B
chk(not (target / 'dsh-newplugin').exists(), '新装的 dsh-newplugin 已移走')
# 保护
chk((target / 'dsh-runtimes').exists(), '保护目录 dsh-runtimes 保留')
chk((target / 'cache').exists(), '保护目录 cache 保留')
chk((target / 'attachments').exists(), '保护目录 attachments 保留')
chk((target / 'dsh-tier-router' / 'index.js').read_text(encoding='utf-8') == 'ORIGINAL router', '未改动的插件保持原样')

print()
print('最终结果:', '全部通过 ' if ok else '存在失败 ')

shutil.rmtree(root, ignore_errors=True)
print('临时目录已清理')
