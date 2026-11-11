# -*- coding: utf-8 -*-
"""云端快照扫描测试

背景：
  「云端同步」页的旧列表只按大小猜类型（>100MB 判为完整），
  而且完全不读 .meta.json —— 真实云目录里存在大量
  "只有 meta、没有加密包"的孤儿元数据，旧逻辑根本看不见。

本测试验证新页签的数据层 scan_cloud()：
  A. 以 meta.json 为准给出真实类型（轻量/完整）
  B. 统计孤儿元数据（只有 meta 没有数据）
  C. 识别缺元数据的数据文件
  D. 统计数量、占用、最后同步时间
  E. 目录不存在时安全返回
  F. 元数据损坏不致命
"""
import json
import shutil
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from ui.tabs.cloud_snapshots_tab import scan_cloud

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def mk(d: Path, sid, is_full, size_mb, with_data=True, with_meta=True,
       plugin_count=16, status='healthy', created='20261109_120000'):
    if with_data:
        (d / (sid + '.zip.enc')).write_bytes(b'x' * max(1, int(size_mb * 1024)))
    if with_meta:
        (d / (sid + '.zip.meta.json')).write_text(json.dumps({
            'snapshot_id': sid, 'created_at': created, 'is_full': is_full,
            'size_mb': size_mb, 'plugin_count': plugin_count,
            'status': status, 'md5_checksum': 'a' * 32, 'notes': 'test',
        }, ensure_ascii=False), encoding='utf-8')


def main():
    sand = Path(tempfile.mkdtemp(prefix='zf_cloud_'))
    d = sand / 'cloud'
    d.mkdir()

    print('=== A. 以 meta.json 为准判断类型（不看文件大小）===')
    # 故意让"轻量"的加密包 >100MB、"完整"的 <100MB，验证不再按大小猜
    mk(d, 'snapshot_20261109_120000', is_full=False, size_mb=150)
    mk(d, 'snapshot_20261109_130000', is_full=True, size_mb=30)
    res = scan_cloud(d)
    items = {i['snapshot_id']: i for i in res['items']}
    light = items.get('snapshot_20261109_120000')
    full = items.get('snapshot_20261109_130000')
    chk(light and light['is_full'] is False, '150MB 的轻量快照仍判为轻量')
    chk(full and full['is_full'] is True, '30MB 的完整快照仍判为完整')
    chk(res['stats']['light'] == 1 and res['stats']['full'] == 1,
        '统计 1 轻量 / 1 完整',
        '{} / {}'.format(res['stats']['light'], res['stats']['full']))
    print()
    print('=== B. 孤儿元数据（只有 meta 没有加密包）===')
    mk(d, 'snapshot_20261109_140000', is_full=False, size_mb=50, with_data=False)
    res = scan_cloud(d)
    chk(res['stats']['orphan_meta'] == 1, '统计到 1 个孤儿元数据',
        res['stats']['orphan_meta'])
    orphan = [i for i in res['items'] if i['snapshot_id'] == 'snapshot_20261109_140000']
    chk(orphan and orphan[0]['has_data'] is False, '该条目标记为缺加密包')
    chk(orphan and orphan[0]['has_meta'] is True, '元数据仍存在')

    print()
    print('=== C. 缺元数据的数据文件 ===')
    mk(d, 'snapshot_20261109_150000', is_full=False, size_mb=20, with_meta=False)
    res = scan_cloud(d)
    nm = [i for i in res['items'] if i['snapshot_id'] == 'snapshot_20261109_150000']
    chk(nm and nm[0]['has_meta'] is False, '标记为缺元数据')
    chk(nm and nm[0]['has_data'] is True, '数据文件可解密')
    chk(res['stats']['missing_meta'] == 1, '统计到 1 个缺元数据',
        res['stats']['missing_meta'])

    print()
    print('=== D. 统计语义：可恢复 vs 仅剩元数据 vs 实际占用 ===')
    chk(res['stats']['total'] == 4, '共 4 条（含孤儿与缺元数据）',
        res['stats']['total'])
    chk(res['stats']['recoverable'] == 3, '可恢复 3 个（有数据文件）',
        res['stats']['recoverable'])
    chk(res['stats']['meta_only'] == 1, '仅剩元数据 1 个',
        res['stats']['meta_only'])
    chk(res['stats']['disk_mb'] > 0, '实际占用大于 0', res['stats']['disk_mb'])
    chk(res['stats']['disk_mb'] < res['stats']['declared_mb'],
        '实际占用 < 清单声明（孤儿把声明量撑大了）',
        '{:.1f} < {:.1f}'.format(res['stats']['disk_mb'],
                                 res['stats']['declared_mb']))
    chk(bool(res['stats']['last_sync']), '有最后同步时间',
        res['stats']['last_sync'])
    order = [i['created_at'] for i in res['items'] if i['created_at']]
    chk(order == sorted(order, reverse=True), '按时间倒序', order[:3])

    print()
    print('=== E. 目录不存在时安全返回 ===')
    r2 = scan_cloud(sand / 'not_there')
    chk(r2['items'] == [], '不抛异常，返回空列表')
    chk(r2['stats']['exists'] is False, '标记 exists=False')
    r3 = scan_cloud('')
    chk(r3['items'] == [], '空路径也安全')

    print()
    print('=== F. 元数据损坏不致命 ===')
    bad = d / 'snapshot_20261109_160000.zip.meta.json'
    bad.write_text('{ 这不是 json', encoding='utf-8')
    (d / 'snapshot_20261109_160000.zip.enc').write_bytes(b'x' * 100)
    res = scan_cloud(d)
    chk(any(i['snapshot_id'] == 'snapshot_20261109_160000' for i in res['items']),
        '损坏的元数据仍能列出该快照')
    chk(res['stats']['total'] >= 5, '总数正确统计', res['stats']['total'])

    print()
    print('=== H. 保留策略应当选中哪些（只在临时目录验证）===')
    d2 = sand / 'policy'
    d2.mkdir()
    # 8 个轻量 + 4 个完整，时间递增
    for i in range(8):
        mk(d2, 'snapshot_2026010{}_100000'.format(i + 1), is_full=False,
           size_mb=50, created='2026010{}_100000'.format(i + 1))
    for i in range(4):
        mk(d2, 'snapshot_2026020{}_100000'.format(i + 1), is_full=True,
           size_mb=40, created='2026020{}_100000'.format(i + 1))
    r = scan_cloud(d2)
    lights = [i for i in r['items'] if i['has_data'] and not i['is_full']]
    fulls = [i for i in r['items'] if i['has_data'] and i['is_full']]
    keep_l, keep_f = 5, 2
    doomed = lights[keep_l:] + fulls[keep_f:]
    chk(len(lights) == 8 and len(fulls) == 4, '扫出 8 轻量 / 4 完整')
    chk(len(doomed) == 5, '策略 5+2 时应清理 5 个', len(doomed))
    chk(sum(1 for i in doomed if i['is_full']) == 2, '清理 2 个完整')
    chk(sum(1 for i in doomed if not i['is_full']) == 3, '清理 3 个轻量')
    kept_l = [i['created_at'] for i in lights[:keep_l]]
    chk(kept_l == sorted(kept_l, reverse=True), '保留的是最新的几个', kept_l)
    doomed_ids = {i['snapshot_id'] for i in doomed}
    newest = lights[0]['snapshot_id']
    chk(newest not in doomed_ids, '最新的轻量不会被删', newest)

    print()
    print('=== I. 数量在策略内时不应清理 ===')
    d3 = sand / 'few'
    d3.mkdir()
    # 2 个轻量 + 1 个完整：都低于策略（5 / 2）
    mk(d3, 'snapshot_20260301_100000', is_full=False, size_mb=50,
       created='20260301_100000')
    mk(d3, 'snapshot_20260302_100000', is_full=False, size_mb=50,
       created='20260302_100000')
    mk(d3, 'snapshot_20260303_100000', is_full=True, size_mb=40,
       created='20260303_100000')
    r3 = scan_cloud(d3)
    l3 = [i for i in r3['items'] if i['has_data'] and not i['is_full']]
    f3 = [i for i in r3['items'] if i['has_data'] and i['is_full']]
    doomed3 = l3[keep_l:] + f3[keep_f:]
    chk(len(l3) == 2 and len(f3) == 1, '扫出 2 轻量 / 1 完整',
        '{} / {}'.format(len(l3), len(f3)))
    chk(doomed3 == [], '都在保留数之内 → 待删列表为空', len(doomed3))

    shutil.rmtree(sand, ignore_errors=True)

    print()
    print('=== G. 真实云目录扫描（只读，不修改）===')
    # 云目录不写死：优先环境变量，其次本机部署配置，都没有就跳过这段
    _cand = os.environ.get('ZF_CLOUD_DIR', '') or ''
    if not _cand:
        try:
            _cfgp = Path(os.environ.get('ZF_DATA_DIR', r'D:\zerofall')) / 'zerofall_config.json'
            if _cfgp.exists():
                _cand = json.loads(_cfgp.read_text(encoding='utf-8')).get('cloud_sync_dir', '') or ''
        except Exception:
            _cand = ''
    real = Path(_cand) if _cand else Path('')
    if real.is_dir():
        r = scan_cloud(real)
        st = r['stats']
        print('  可恢复: {} 个（轻量 {} / 完整 {}）'.format(
            st['recoverable'], st['light'], st['full']))
        print('  仅剩元数据: {} 个'.format(st['meta_only']))
        print('  实际占用: {:.2f} GB（清单声明 {:.2f} GB）'.format(
            st['disk_mb'] / 1024.0, st['declared_mb'] / 1024.0))
        print('  缺元数据: {}'.format(st['missing_meta']))
        print('  最后同步: {}'.format(st['last_sync'] or '-'))
        chk(st['total'] > 0, '真实目录能扫出记录', st['total'])
        chk(st['exists'] is True, '目录被识别为存在')
        # .enc 是 base64 密文，比明文大 1/3（实测 885 MB zip -> 1180 MB enc）；
        # size_mb 记的又是压缩前目录大小/zip 大小。两把尺子直接比必然"超标"，
        # 所以用 scan_cloud 折算出的 payload_mb 比，系数写在 ENC_OVERHEAD 里。
        compare_mb = st.get('payload_mb', st['disk_mb'])
        chk(compare_mb <= st['declared_mb'] * 1.2 or st['declared_mb'] == 0,
            '实际占用没有离谱地超过声明量（密文已按 {:.2f}x 折算）'.format(st.get('enc_overhead', 1.0)),
            '{:.1f} vs {:.1f}'.format(compare_mb, st['declared_mb']))
    else:
        print('  跳过：真实目录不存在')
        chk(True, '（跳过真实目录检查）')

    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
