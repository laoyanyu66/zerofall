# -*- coding: utf-8 -*-
"""快照去重与运行时垃圾排除测试（2026-11-16）

覆盖本轮自检发现的两件事：

  A. 内容去重：三份 dsh-rea-plugin-9.9.9.tgz 二进制完全相同（MD5 D3A12DB579D8，
     各 2,463,028 B），以前每个快照都白背 7.05 MB（占包体 27.1%）。
     现在同内容只存一份 + 清单，恢复时必须补齐。

  B. 排除表统一：指纹早就排掉浏览器 profile / 轮转日志，快照却照样打进包里。
     另外三个"每分钟心跳"文件没排进指纹，导致"内容没变就跳过静默快照"
     永远命中不了（11-15 实测 430 次判定 / 143 份快照 / 约 4.6 GB 写入）。

必须锁住的行为：
  1. 同内容大文件只存一份，清单能指出副本从哪儿补
  2. 小文件不去重（省不出体积，还多一层依赖）
  3. 恢复补齐后目录树完整（后续 _prune/_restore_dir_clean 依赖这一点）
  4. 运行时垃圾（*.pma / load_statistics.db-wal / relay-logs / SmartScreen）不进包
  5. 心跳文件改动不改变指纹；真实插件改动仍然改变指纹
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc + ('' if cond else '  ' + str(extra)))


def _mkroot(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


def test_dedupe_identical_files():
    print('=== A1. 同内容大文件只存一份 ===')
    from modules.snapshot_manager import copy_for_snapshot, DEDUPE_MANIFEST_NAME

    root = _mkroot('zf_dedup_')
    src, dst = root / 'src', root / 'dst'
    blob = os.urandom(300 * 1024)          # > 256 KB 阈值，且内容不可压缩
    for prof in ('desktop', 'zf-acp', 'zf-fixer'):
        d = src / 'profiles' / prof / 'plugins'
        d.mkdir(parents=True)
        (d / 'dsh-rea-plugin-9.9.9.tgz').write_bytes(blob)

    stats = copy_for_snapshot(src, dst, include_runtime=False)

    files = [p.relative_to(dst).as_posix() for p in dst.rglob('*.tgz')]
    chk(len(files) == 1, '三份相同 tgz 只落盘一份', files)
    chk(stats.get('deduped') == 2, '统计记下 2 个去重', stats)
    chk(stats.get('dedup_bytes') == 2 * len(blob), '去重字节数正确', stats)

    manifest = dst / DEDUPE_MANIFEST_NAME
    entries = json.loads(manifest.read_text(encoding='utf-8')).get('entries', {})
    chk(len(entries) == 2, '清单记录 2 个副本', entries)
    chk(files and entries.get(
        'profiles/zf-acp/plugins/dsh-rea-plugin-9.9.9.tgz') == 'profiles/desktop/plugins/dsh-rea-plugin-9.9.9.tgz',
        '清单指向已落盘的那一份', entries)

    shutil.rmtree(root, ignore_errors=True)


def test_dedupe_skips_small_and_different():
    print('=== A2. 小文件不去重 / 同尺寸不同内容不误去重 ===')
    from modules.snapshot_manager import copy_for_snapshot

    root = _mkroot('zf_dedup2_')
    src, dst = root / 'src', root / 'dst'
    d1, d2 = src / 'a', src / 'b'
    d1.mkdir(parents=True); d2.mkdir(parents=True)
    (d1 / 'small.bin').write_bytes(b'x' * 1000)
    (d2 / 'small.bin').write_bytes(b'x' * 1000)          # 同内容但太小
    (d1 / 'big1.bin').write_bytes(b'A' * (300 * 1024))
    (d2 / 'big2.bin').write_bytes(b'B' * (300 * 1024))    # 同尺寸、不同内容

    stats = copy_for_snapshot(src, dst, include_runtime=False)

    chk((dst / 'a' / 'small.bin').exists() and (dst / 'b' / 'small.bin').exists(),
        '小文件两份都保留', stats)
    chk((dst / 'a' / 'big1.bin').exists() and (dst / 'b' / 'big2.bin').exists(),
        '同尺寸不同内容都保留', stats)
    chk(stats.get('deduped') == 0, '没有误去重', stats)

    shutil.rmtree(root, ignore_errors=True)


def test_materialize_restores_copies():
    print('=== A3. 恢复时把副本补齐 ===')
    from modules.snapshot_manager import (copy_for_snapshot, materialize_dedup,
                                          DEDUPE_MANIFEST_NAME)

    root = _mkroot('zf_mat_')
    src, dst = root / 'src', root / 'dst'
    payload = os.urandom(300 * 1024)
    for prof in ('desktop', 'zf-acp', 'zf-fixer'):
        d = src / 'profiles' / prof / 'plugins'
        d.mkdir(parents=True)
        (d / 'pkg.tgz').write_bytes(payload)

    copy_for_snapshot(src, dst, include_runtime=False)
    # 模拟"解压出来的快照"
    snap = root / 'snap'
    shutil.copytree(dst, snap)

    before = len(list((snap / 'profiles').rglob('pkg.tgz')))
    chk(before == 1, '补齐前只有一份', before)

    restored = materialize_dedup(snap)
    after = list((snap / 'profiles').rglob('pkg.tgz'))
    chk(restored == 2, '补齐 2 个副本', restored)
    chk(len(after) == 3, '补齐后三份都在', len(after))
    chk(all(p.read_bytes() == payload for p in after), '补齐内容与原件一致')

    # 再补一次应无操作（幂等）
    chk(materialize_dedup(snap) == 0, '重复补齐无副作用')
    chk(not (snap / DEDUPE_MANIFEST_NAME).is_dir(), '清单本身不是目录')

    shutil.rmtree(root, ignore_errors=True)


def test_runtime_junk_excluded():
    print('=== B1. 运行时垃圾不进快照 ===')
    from modules.snapshot_manager import copy_for_snapshot

    root = _mkroot('zf_junk_')
    src, dst = root / 'src', root / 'dst'
    base = src / 'douyin-browser' / 'cli-profile'
    (base / 'SmartScreen' / 'RemoteData').mkdir(parents=True)
    (base / 'BrowserMetrics').mkdir(parents=True)
    (base / 'Default').mkdir(parents=True)
    (base / 'Crashpad').mkdir(parents=True)
    (src / 'nightly' / 'relay-logs').mkdir(parents=True)

    (base / 'BrowserMetrics-spare.pma').write_bytes(b'\0' * 1024)
    (base / 'BrowserMetrics' / 'BrowserMetrics-X.pma').write_bytes(b'\0' * 1024)
    (base / 'Default' / 'load_statistics.db-wal').write_bytes(b'w' * 1024)
    (base / 'Default' / 'load_statistics.db').write_bytes(b'd' * 1024)
    (base / 'SmartScreen' / 'RemoteData' / 'topTraffic_1').write_bytes(b't' * 1024)
    (base / 'Crashpad' / 'reports.db').write_bytes(b'c' * 1024)
    (src / 'nightly' / 'relay-logs' / 'relay.log.1').write_bytes(b'l' * 1024)
    # 必须留下的东西
    (base / 'Default' / 'Cookies').write_bytes(b'ck')
    (src / 'nightly' / 'relay-status.json').write_text('{}', encoding='utf-8')

    stats = copy_for_snapshot(src, dst, include_runtime=False)

    names = [p.name for p in dst.rglob('*') if p.is_file()]
    chk('BrowserMetrics-spare.pma' not in names, '.pma 被排除', names)
    chk('BrowserMetrics-X.pma' not in names, 'BrowserMetrics 目录被排除', names)
    chk('load_statistics.db-wal' not in names, '统计库 WAL 被排除', names)
    chk('load_statistics.db' not in names, '统计库本体被排除', names)
    chk('topTraffic_1' not in names, 'SmartScreen 数据被排除', names)
    chk('reports.db' not in names, 'Crashpad 目录被排除', names)
    chk('relay.log.1' not in names, '轮转日志被排除（旧规则只看 .log 后缀会漏）', names)
    chk('Cookies' in names, '浏览器登录态仍然保留')
    chk('relay-status.json' in names, '心跳文件仍在快照里（只是不参与指纹）')
    chk(stats.get('skipped_dirs', 0) >= 4, '跳过目录计数', stats)

    shutil.rmtree(root, ignore_errors=True)


def test_fingerprint_ignores_heartbeats():
    print('=== B2. 心跳文件不再触发"环境被改" ===')
    from modules.environment_checker import EnvironmentChecker

    root = _mkroot('zf_fp_')
    plug = root / 'plugins'
    plug.mkdir()
    for i in range(3):
        (plug / 'plugin{}.js'.format(i)).write_text('v1-' + 'x' * 50, encoding='utf-8')

    # 心跳与浏览器运行时
    (plug / 'nightly').mkdir()
    (plug / 'nightly' / 'relay-status.json').write_text('{"a":1}', encoding='utf-8')
    (plug / 'nightly' / 'relay-logs').mkdir()
    (plug / 'nightly' / 'relay-logs' / 'relay.log.1').write_text('log', encoding='utf-8')
    (plug / 'guardian' / 'state').mkdir(parents=True)
    (plug / 'guardian' / 'state' / 'last-boot.json').write_text('{"t":1}', encoding='utf-8')
    (plug / 'dsh-windows-mcp.versions.json').write_text('{"v":1}', encoding='utf-8')
    (plug / 'douyin-browser' / 'cli-profile' / 'Default').mkdir(parents=True)
    (plug / 'douyin-browser' / 'cli-profile' / 'Default' / 'load_statistics.db-wal').write_bytes(b'w')

    cfg = SimpleNamespace(deepseek_plugins_dir=str(plug),
                          deepseek_config_dir=str(root / 'config'),
                          snapshot_dir=str(root / 'snaps'))
    (root / 'config').mkdir()
    (root / 'config' / 'cordis.yml').write_text('a: 1', encoding='utf-8')
    ec = EnvironmentChecker(cfg)

    fp1 = ec.quick_fingerprint()
    chk(bool(fp1), '指纹非空', fp1)

    # 心跳文件反复重写（每轮都算一次）
    for i in range(3):
        (plug / 'nightly' / 'relay-status.json').write_text('{"a":%d}' % i, encoding='utf-8')
        (plug / 'guardian' / 'state' / 'last-boot.json').write_text('{"t":%d}' % i, encoding='utf-8')
        (plug / 'dsh-windows-mcp.versions.json').write_text('{"v":%d}' % i, encoding='utf-8')
        (plug / 'douyin-browser' / 'cli-profile' / 'Default' / 'load_statistics.db-wal').write_bytes(str(i).encode() * 10)
        fp_n = ec.quick_fingerprint()
        chk(fp_n == fp1, '心跳第 {} 次重写后指纹不变'.format(i + 1), (fp1[:8], fp_n[:8]))

    # 真正的插件改动必须还能被检测到
    (plug / 'plugin1.js').write_text('v2-real-change', encoding='utf-8')
    fp3 = ec.quick_fingerprint()
    chk(fp3 != fp1, '真实插件改动仍然改变指纹', (fp1[:8], fp3[:8]))

    shutil.rmtree(root, ignore_errors=True)


def test_shared_rules_module():
    print('=== C. 两张表同源（避免下次再分叉）===')
    from modules import volatile_rules as vr
    from modules.environment_checker import MD5_EXCLUDE_FILES, MD5_EXCLUDE_REL_PREFIXES

    chk('relay-status.json' in MD5_EXCLUDE_FILES, '指纹表含心跳文件')
    chk('*.pma' in MD5_EXCLUDE_FILES, '指纹表含 Chromium 转储')
    chk('nightly/relay-logs/' in MD5_EXCLUDE_REL_PREFIXES, '指纹表含心跳日志目录')
    chk('douyin-browser/cli-profile/' in MD5_EXCLUDE_REL_PREFIXES, '指纹表含浏览器 profile')
    chk(vr.is_snapshot_junk_file('relay.log.2'), '轮转日志识别到两位序号')
    chk(not vr.is_snapshot_junk_file('relay.log'), '普通 .log 走原有后缀规则')
    chk(vr.is_snapshot_junk_file('load_statistics.db-shm'), '统计库 shm 也算垃圾')
    chk(not vr.is_snapshot_junk_dir('Default'), '浏览器 Default 目录不算垃圾')


def test_small_file_dedupe():
    print('=== A4. 小文件也去重（二次自检：阈值 256 KB → 4 KB）===')
    from modules.snapshot_manager import (copy_for_snapshot, materialize_dedup,
                                          DEDUPE_MIN_BYTES, DEDUPE_MANIFEST_NAME)

    chk(DEDUPE_MIN_BYTES == 4 * 1024, '阈值已降到 4 KB', DEDUPE_MIN_BYTES)

    root = _mkroot('zf_small_')
    src, dst = root / 'src', root / 'dst'
    payload = b'settings: v1\n' * 400          # 约 5.4 KB，过了 4 KB 阈值
    tiny = b'tiny' * 100                       # 400 B，不到阈值
    for name in ('a', 'b'):
        d = src / name
        d.mkdir(parents=True)
        (d / 'settings.yaml.bak').write_bytes(payload)
        (d / 'tiny.cfg').write_bytes(tiny)

    stats = copy_for_snapshot(src, dst, include_runtime=False)
    chk(stats.get('deduped') == 1, '5.4 KB 的重复文件被去重', stats)
    chk((dst / 'a' / 'tiny.cfg').exists() and (dst / 'b' / 'tiny.cfg').exists(),
        '400 B 的小文件仍然两份都在（清单成本大于收益）')

    entries = json.loads((dst / DEDUPE_MANIFEST_NAME).read_text(encoding='utf-8'))['entries']
    chk(len(entries) == 1 and 'b/settings.yaml.bak' in entries, '清单记录副本指向', entries)

    # 解压后补齐，两份内容都要在
    snap = root / 'snap'
    shutil.copytree(dst, snap)
    chk(materialize_dedup(snap) == 1, '补齐 1 个副本')
    got = (snap / 'b' / 'settings.yaml.bak').read_bytes()
    chk(got == payload, '补齐内容与原件一致')

    shutil.rmtree(root, ignore_errors=True)


def test_browser_profile_cache_excluded():
    print('=== B3. 浏览器 profile 里的缓存/历史不进包，登录态保留 ===')
    from modules.snapshot_manager import copy_for_snapshot
    from modules import volatile_rules as vr

    root = _mkroot('zf_prof_')
    src, dst = root / 'src', root / 'dst'
    prof = src / 'douyin-browser' / 'cli-profile'
    (prof / 'ShaderCache').mkdir(parents=True)
    (prof / 'Default' / 'Service Worker' / 'CacheStorage').mkdir(parents=True)
    (prof / 'Default' / 'Local Storage' / 'leveldb').mkdir(parents=True)
    (prof / 'Default' / 'Nurturing').mkdir(parents=True)

    (prof / 'ShaderCache' / 'data_1').write_bytes(b'0' * 1024)
    (prof / 'Default' / 'Service Worker' / 'CacheStorage' / 'b6c2').write_bytes(b'c' * 1024)
    (prof / 'Default' / 'Nurturing' / 'campaign_history').write_bytes(b'n' * 1024)
    (prof / 'Default' / 'History').write_bytes(b'h' * 1024)
    (prof / 'Default' / 'Web Data').write_bytes(b'w' * 1024)
    (prof / 'Default' / 'DIPS-wal').write_bytes(b'd' * 1024)
    (prof / 'Default' / 'Edge Profile Picture.png').write_bytes(b'p' * 1024)
    # 必须留下的：登录态
    (prof / 'Default' / 'Cookies').write_bytes(b'ck')
    (prof / 'Default' / 'Login Data').write_bytes(b'ld')
    (prof / 'Local State').write_bytes(b'ls')
    (prof / 'Default' / 'Local Storage' / 'leveldb' / '000005.ldb').write_bytes(b'l' * 1024)
    # 别的插件里也有叫 History 的文件 —— 不能误伤
    (src / 'someplugin').mkdir()
    (src / 'someplugin' / 'History').write_bytes(b's' * 1024)

    copy_for_snapshot(src, dst, include_runtime=False)

    names = [p.name for p in dst.rglob('*') if p.is_file()]
    chk('data_1' not in names, 'ShaderCache 整棵目录被排除', names)
    chk('b6c2' not in names, 'Service Worker CacheStorage 被排除', names)
    chk(not (dst / 'douyin-browser' / 'cli-profile' / 'Default' / 'History').exists(),
        'profile 里的 History 被排除')
    chk('Web Data' not in names, 'Web Data 被排除', names)
    chk('DIPS-wal' not in names, 'DIPS-wal 被排除', names)
    chk('Edge Profile Picture.png' not in names, '1 MB 的预览图被排除', names)
    chk('campaign_history' not in names, 'Nurturing 运营数据被排除', names)
    chk('Cookies' in names and 'Login Data' in names and 'Local State' in names,
        '登录态三件套仍然保留', names)
    chk('000005.ldb' in names, 'Local Storage 仍然保留（token 就放这儿）')
    chk((dst / 'someplugin' / 'History').exists(),
        '非 profile 目录下同名的 History 不被误伤')
    chk(vr.is_snapshot_junk_in_browser_profile('cli-profile/Default/History'),
        '规则判定：profile 内 History 算垃圾')
    chk(not vr.is_snapshot_junk_in_browser_profile('someplugin/History'),
        '规则判定：profile 外 History 不算垃圾')

    shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    test_dedupe_identical_files()
    test_dedupe_skips_small_and_different()
    test_materialize_restores_copies()
    test_small_file_dedupe()
    test_runtime_junk_excluded()
    test_browser_profile_cache_excluded()
    test_fingerprint_ignores_heartbeats()
    test_shared_rules_module()
    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    sys.exit(0 if not FAIL else 1)
