# -*- coding: utf-8 -*-
"""快照生命周期健壮性测试

覆盖线上发现的三个真实故障：
  A. node_modules 膨胀（pkgs/dsh-tiangong/node_modules 275MB → 轻量快照 144MB）
  B. 孤儿索引条目导致溢出归档活锁
  C. 打包中断留下的 snapshot_xxx 目录（单个数百 MB）
  D. 归档连续失败上限（防无限重试）
"""
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


def test_node_modules_exclusion():
    print('=== A. node_modules 排除（修复快照膨胀）===')
    from modules.snapshot_manager import copy_for_snapshot

    root = Path(tempfile.mkdtemp(prefix='zf_nm_'))
    src, dst = root / 'src', root / 'dst'

    # 造：plugins/pkgs/<plugin>/node_modules 大目录（模拟 tiangong 275MB）
    big = src / 'plugins' / 'pkgs' / 'dsh-tiangong' / 'node_modules' / 'pkg-a'
    big.mkdir(parents=True)
    (big / 'index.js').write_text('x' * 20000, encoding='utf-8')
    (big / 'big.bin').write_bytes(b'0' * 200000)
    # 插件本体（必须保留）
    (src / 'plugins' / 'pkgs' / 'dsh-tiangong' / 'core').mkdir(parents=True)
    (src / 'plugins' / 'pkgs' / 'dsh-tiangong' / 'core' / 'effort.mjs').write_text(
        'export const A=1', encoding='utf-8')
    (src / 'plugins' / 'pkgs' / 'dsh-tiangong' / 'package.json').write_text(
        '{"name":"dsh-tiangong"}', encoding='utf-8')
    # profiles 下的 node_modules 也应排除
    pnm = src / 'plugins' / 'profiles' / 'desktop' / 'node_modules' / 'dep'
    pnm.mkdir(parents=True)
    (pnm / 'a.js').write_text('y' * 5000, encoding='utf-8')
    (src / 'plugins' / 'profiles' / 'desktop' / 'package.json').write_text(
        '{"name":"desktop"}', encoding='utf-8')
    # 运行时（始终排除）
    (src / 'plugins' / 'dsh-runtimes' / 'x').mkdir(parents=True)
    (src / 'plugins' / 'dsh-runtimes' / 'x' / 'big.bin').write_bytes(b'0' * 9999)

    stats = copy_for_snapshot(src, dst, include_runtime=False)

    def has(p):
        return (dst / p).exists()

    chk(not has('plugins/pkgs/dsh-tiangong/node_modules'), 'pkgs/*/node_modules 被排除')
    chk(not has('plugins/profiles/desktop/node_modules'), 'profiles/*/node_modules 被排除')
    chk(has('plugins/pkgs/dsh-tiangong/core/effort.mjs'), '插件源码 core/ 保留')
    chk(has('plugins/pkgs/dsh-tiangong/package.json'), '插件 package.json 保留')
    chk(has('plugins/profiles/desktop/package.json'), 'profile package.json 保留')
    chk(not has('plugins/dsh-runtimes'), 'dsh-runtimes 排除')
    chk(stats['skipped_dirs'] >= 3, '统计记录跳过目录数 ({})'.format(stats['skipped_dirs']))

    # 体积应显著下降
    copied = sum(f.stat().st_size for f in dst.rglob('*') if f.is_file())
    chk(copied < 30000, '复制体积只剩源码（{} 字节，不含 node_modules）'.format(copied))
    shutil.rmtree(root, ignore_errors=True)


def test_prune_missing_entries():
    print()
    print('=== B. 孤儿索引条目清理（修复归档活锁）===')
    from modules.snapshot_manager import SnapshotManager, SnapshotData

    root = Path(tempfile.mkdtemp(prefix='zf_prune_'))
    snap_dir = root / 'snaps'
    snap_dir.mkdir()

    class Cfg:
        deepseek_plugins_dir = str(root / 'plugins')
        deepseek_exe_path = str(root / 'DeepSeek.exe')
        snapshot_dir = str(snap_dir)
        backup_plugins = True
        backup_plugin_config = True
        backup_session_data = True
        backup_keys = False

    (root / 'plugins').mkdir()
    sm = SnapshotManager(Cfg())

    # 手工塞：2 个有效条目 + 2 个孤儿条目
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    valid1 = snap_dir / 'snapshot_20260101_000001.zip'
    valid2 = snap_dir / 'snapshot_20260101_000002.zip'
    for f in (valid1, valid2):
        with zipfile.ZipFile(f, 'w') as zf:
            zf.writestr('a.txt', 'x')

    def mk(sid, path, is_full=False):
        from modules.snapshot_manager import SnapshotStatus
        return SnapshotData(
            snapshot_id=sid, created_at=now,
            status=SnapshotStatus.HEALTHY,
            is_full=is_full,
            notes='t', size_mb=1.0, plugin_count=1,
            md5_checksum='deadbeef', archive_path=str(path),
        )

    sm.snapshots = {
        'snapshot_20260101_000001': mk('snapshot_20260101_000001', valid1),
        'snapshot_20260101_000002': mk('snapshot_20260101_000002', valid2),
        'snapshot_20261107_084210': mk('snapshot_20261107_084210',
                                       snap_dir / 'snapshot_20261107_084210.zip'),
        'snapshot_20261108_103712': mk('snapshot_20261108_103712',
                                       snap_dir / 'snapshot_20261108_103712.zip'),
    }
    sm._save_index()
    chk(len(sm.snapshots) == 4, '初始 4 条（2 有效 + 2 孤儿）')

    removed = sm.prune_missing_entries()
    chk(len(removed) == 2, '清理掉 2 个孤儿条目')
    chk(set(removed) == {'snapshot_20261107_084210', 'snapshot_20261108_103712'},
        '清的是文件缺失那两个')
    chk(len(sm.snapshots) == 2, '有效条目保留')
    chk('snapshot_20260101_000001' in sm.snapshots, '有效条目 1 还在')

    # 落盘后重载仍只有 2 条
    raw = json.loads((snap_dir / 'snapshot_index.json').read_text(encoding='utf-8'))
    chk(len(raw) == 2, '索引文件已同步为 2 条')

    # 再跑一次应无操作（幂等）
    chk(len(sm.prune_missing_entries()) == 0, '重复清理无副作用（幂等）')
    shutil.rmtree(root, ignore_errors=True)


def test_cleanup_staging_dirs():
    print()
    print('=== C. 遗留打包目录清理 ===')
    from modules.snapshot_manager import SnapshotManager

    root = Path(tempfile.mkdtemp(prefix='zf_stg_'))
    snap_dir = root / 'snaps'
    snap_dir.mkdir()

    class Cfg:
        deepseek_plugins_dir = str(root / 'plugins')
        deepseek_exe_path = str(root / 'DeepSeek.exe')
        snapshot_dir = str(snap_dir)
        backup_plugins = True
        backup_plugin_config = True
        backup_session_data = True
        backup_keys = False

    (root / 'plugins').mkdir()
    sm = SnapshotManager(Cfg())

    # 老的遗留目录（应删）
    old_dir = snap_dir / 'snapshot_20261108_095035'
    old_dir.mkdir()
    (old_dir / 'f.bin').write_bytes(b'0' * 5000)
    old_pruned = snap_dir / '_zerofall_pruned_20261108'
    old_pruned.mkdir()
    (old_pruned / 'g.bin').write_bytes(b'0' * 100)

    # 新的目录（进行中，不应删）
    fresh = snap_dir / 'snapshot_20990101_000000'
    fresh.mkdir()
    (fresh / 'inprogress.bin').write_bytes(b'0' * 100)

    # 正常快照 zip（不应动）
    z = snap_dir / 'snapshot_20260101_000001.zip'
    with zipfile.ZipFile(z, 'w') as zf:
        zf.writestr('a.txt', 'x')

    # 无关目录（不应动）
    other = snap_dir / 'logs'
    other.mkdir()

    # 把老目录的 mtime 推到 2 小时前
    past = time.time() - 7200
    os.utime(old_dir, (past, past))
    os.utime(old_pruned, (past, past))

    removed = sm.cleanup_staging_dirs(min_age_seconds=3600)
    chk(len(removed) == 2, '清掉 2 个老的遗留目录 ({})'.format(len(removed)))
    chk(not old_dir.exists(), '老的 snapshot_xxx 目录已删')
    chk(not old_pruned.exists(), '_zerofall_pruned_* 已删')
    chk(fresh.exists(), '新目录（可能进行中）保留')
    chk(z.exists(), '正常快照 zip 保留')
    chk(other.exists(), '无关目录 logs/ 保留')
    shutil.rmtree(root, ignore_errors=True)


def test_archive_fail_cap():
    print()
    print('=== D. 归档连续失败上限（防空转）===')
    # 直接验证主窗口里的上限逻辑（用替身最小复现）
    class Snap:
        def __init__(self, sid):
            self.snapshot_id = sid

    class SM:
        def __init__(self):
            self.deleted = []
            self.pruned = 0

        def prune_missing_entries(self):
            self.pruned += 1
            return []

        def get_overflow_snapshots(self):
            # 与 SnapshotManager 保持一致：返回 (轻量, 完整, 崩溃) 三元组
            return [Snap('bad-1')], [], []

        def delete_snapshot(self, sid):
            self.deleted.append(sid)
            return True

    class Cfg:
        cloud_overflow_enabled = True
        cloud_sync_dir = 'X:/fake'
        cloud_sync_encrypt = False

    calls = []

    class H:
        config = Cfg()
        snapshot_manager = SM()
        _archive_busy = False
        _archive_fail_count = {}

        def _archive_snapshot_to_cloud(self, s, d, e):
            calls.append(s.snapshot_id)
            return False       # 永远失败（模拟云盘不可用）

    h = H()
    # 取真实方法
    import ast
    src = (Path(__file__).resolve().parent.parent / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    lines = src.splitlines()
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_handle_overflow_to_cloud':
            fn = '\n'.join(lines[node.lineno - 1:node.end_lineno])
            break
    import textwrap
    ns = {'ZeroFallLogger': type('L', (), {
        'info': staticmethod(lambda *a, **k: None),
        'warning': staticmethod(lambda *a, **k: None),
        'error': staticmethod(lambda *a, **k: None)}),
        'Path': Path}
    exec(compile(textwrap.dedent(fn), '<m>', 'exec'), ns)
    H._handle_overflow_to_cloud = ns['_handle_overflow_to_cloud']

    for i in range(6):
        h._handle_overflow_to_cloud()

    chk(len(calls) == 3, '只真正尝试 3 次（上限 3），实际 {}'.format(len(calls)))
    chk(h._archive_fail_count.get('bad-1') == 3, '失败计数停在 3')
    chk(h.snapshot_manager.pruned == 6, '每轮仍会尝试清理孤儿条目')
    chk(not h.snapshot_manager.deleted, '失败时不删本地快照')


def test_reconcile_with_disk():
    print()
    print('=== E. 索引与磁盘对账（孤儿 zip 重新登记）===')
    from modules.snapshot_manager import SnapshotManager, SnapshotData, SnapshotStatus

    root = Path(tempfile.mkdtemp(prefix='zf_rec_'))
    snap_dir = root / 'snaps'
    snap_dir.mkdir()

    class Cfg:
        deepseek_plugins_dir = str(root / 'plugins')
        deepseek_exe_path = str(root / 'DeepSeek.exe')
        snapshot_dir = str(snap_dir)
        backup_plugins = True
        backup_plugin_config = True
        backup_session_data = True
        backup_keys = False

    (root / 'plugins').mkdir()
    sm = SnapshotManager(Cfg())

    # 磁盘上摆 3 个 zip：1 个已登记、2 个孤儿（1 轻量 + 1 完整）
    def mkzip(name, has_runtime):
        p = snap_dir / name
        with zipfile.ZipFile(p, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('plugins/a.txt', 'x')
            # 塞 ~1.5 MB 随机数据，保证 size_mb 能量出非零
            zf.writestr('plugins/blob.bin', os.urandom(1500 * 1024))
            if has_runtime:
                zf.writestr('primary-runtime/bin/node.exe', 'fake')
        # 把 mtime 推到 10 分钟前，绕开"可能正在写"的保护
        past = time.time() - 600
        os.utime(p, (past, past))
        return p

    known = mkzip('snapshot_20260101_000001.zip', False)
    orphan_light = mkzip('snapshot_20260102_000002.zip', False)
    orphan_full = mkzip('snapshot_20260103_000003.zip', True)

    sm.snapshots = {
        'snapshot_20260101_000001': SnapshotData(
            snapshot_id='snapshot_20260101_000001',
            created_at='20260101_000001',
            status=SnapshotStatus.HEALTHY, plugin_count=1, size_mb=1.0,
            md5_checksum='x', notes='known', archive_path=str(known), is_full=False),
    }
    sm._save_index()

    rec = sm.reconcile_with_disk()
    chk(len(rec['adopted']) == 2, '登记 2 个孤儿 zip ({})'.format(len(rec['adopted'])))
    chk(len(sm.snapshots) == 3, '索引变 3 条')
    chk('snapshot_20260102_000002' in sm.snapshots, '轻量孤儿已登记')
    chk('snapshot_20260103_000003' in sm.snapshots, '完整孤儿已登记')

    # 类型判定是否准确（靠 primary-runtime 判据）
    light_entry = sm.snapshots['snapshot_20260102_000002']
    full_entry = sm.snapshots['snapshot_20260103_000003']
    chk(light_entry.is_full is False, '无 primary-runtime → 判为轻量')
    chk(full_entry.is_full is True, '有 primary-runtime → 判为完整')
    chk(light_entry.created_at == '20260102_000002', '从文件名解析出时间戳')
    chk(light_entry.size_mb > 0, '记录体积')

    # 幂等
    rec2 = sm.reconcile_with_disk()
    chk(len(rec2['adopted']) == 0, '再跑一次不重复登记')
    chk(len(sm.snapshots) == 3, '条目数不变')

    # 正在写入的新 zip 不被登记
    fresh = snap_dir / 'snapshot_20260104_000004.zip'
    with zipfile.ZipFile(fresh, 'w') as zf:
        zf.writestr('plugins/b.txt', 'y')
    rec3 = sm.reconcile_with_disk()
    chk('snapshot_20260104_000004' in rec3['skipped'], '新写入的 zip 被跳过（保护）')
    chk('snapshot_20260104_000004' not in sm.snapshots, '未登记')

    # 坏包不登记
    bad = snap_dir / 'snapshot_20260105_000005.zip'
    bad.write_bytes(b'not a zip at all')
    past = time.time() - 600
    os.utime(bad, (past, past))
    rec4 = sm.reconcile_with_disk()
    chk('snapshot_20260105_000005' in rec4['skipped'], '坏包被跳过')
    chk('snapshot_20260105_000005' not in sm.snapshots, '坏包未登记')

    shutil.rmtree(root, ignore_errors=True)


def test_cleanup_corrupt_archives():
    print()
    print('=== F. 截断包清理（写了一半的快照 zip）===')
    from modules.snapshot_manager import SnapshotManager

    root = Path(tempfile.mkdtemp(prefix='zf_bad_'))
    snap_dir = root / 'snaps'
    snap_dir.mkdir()

    class Cfg:
        deepseek_plugins_dir = str(root / 'plugins')
        deepseek_exe_path = str(root / 'DeepSeek.exe')
        snapshot_dir = str(snap_dir)
        backup_plugins = True
        backup_plugin_config = True
        backup_session_data = True
        backup_keys = False

    (root / 'plugins').mkdir()
    sm = SnapshotManager(Cfg())

    # 模拟真实故障：文件头是 PK、但中央目录缺失（写到一半）
    trunc = snap_dir / 'snapshot_20261108_095035.zip'
    trunc.write_bytes(b'PK\x03\x04\x14\x00\x00\x00' + b'\x00' * 50000)
    trunc2 = snap_dir / 'snapshot_20261108_111841.zip'
    trunc2.write_bytes(b'PK\x03\x04\x14\x00\x00\x00' + b'\x00' * 30000)

    # 正常包（不能删）
    good = snap_dir / 'snapshot_20260101_000001.zip'
    with zipfile.ZipFile(good, 'w') as zf:
        zf.writestr('plugins/a.txt', 'x')

    # 正在写的（mtime 很新，不能删）
    fresh = snap_dir / 'snapshot_20260102_000002.zip'
    fresh.write_bytes(b'PK\x03\x04\x14\x00\x00\x00' + b'\x00' * 100)

    past = time.time() - 7200
    for p in (trunc, trunc2):
        os.utime(p, (past, past))

    removed = sm.cleanup_corrupt_archives(min_age_seconds=3600)
    chk(len(removed) == 2, '清掉 2 个截断包 ({})'.format(len(removed)))
    chk(not trunc.exists(), '截断包 1 已删')
    chk(not trunc2.exists(), '截断包 2 已删')
    chk(good.exists(), '完整包保留')
    chk(fresh.exists(), '正在写的包保留（mtime 保护）')

    # 干净环境下再跑应无操作
    chk(len(sm.cleanup_corrupt_archives(min_age_seconds=3600)) == 0, '重复执行无副作用')

    shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    test_node_modules_exclusion()
    test_prune_missing_entries()
    test_cleanup_staging_dirs()
    test_archive_fail_cap()
    test_reconcile_with_disk()
    test_cleanup_corrupt_archives()
    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    sys.exit(0 if not FAIL else 1)
