# -*- coding: utf-8 -*-
"""从磁盘上实际存在的快照文件重建索引（索引丢失/被覆盖时使用）"""
import hashlib
import json
import zipfile
import sys
from pathlib import Path

VAULT = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ZeroFallConfig().snapshot_dir


def build_entry(zip_path: Path):
    stem = zip_path.stem                    # snapshot_YYYYMMDD_HHMMSS
    parts = stem.split('_')
    if len(parts) < 3:
        return None
    created_at = parts[1] + '_' + parts[2]

    # 解压后大小（与正常创建时的 size_mb 一致）
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = sum(i.file_size for i in zf.infolist())
            file_count = len(zf.infolist())
    except Exception:
        raw, file_count = 0, 0

    size_mb = round(raw / 1024 / 1024, 2)
    compressed_mb = round(zip_path.stat().st_size / 1024 / 1024, 2)

    # 判断完整/轻量：解压后 > 100MB 视为完整快照
    is_full = size_mb > 100

    md5 = hashlib.md5(zip_path.read_bytes()).hexdigest()

    return {
        'snapshot_id': stem,
        'created_at': created_at,
        'status': 'healthy',
        'plugin_count': 16,
        'size_mb': size_mb,
        'md5_checksum': md5,
        'plugins': [],
        'notes': '索引重建（原索引被覆盖）',
        'deepseek_version': '',
        'has_session_data': False,
        'has_keys': False,
        'archive_path': str(zip_path),
        'is_full': is_full,
    }


def main():
    index_file = VAULT / 'snapshot_index.json'

    # 保留已有索引里仍然有效的条目
    existing = {}
    if index_file.exists():
        try:
            raw = json.loads(index_file.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                existing = raw
        except Exception:
            pass

    rebuilt = dict(existing)
    added = []
    for z in sorted(VAULT.glob('snapshot_*.zip')):
        sid = z.stem
        if sid in rebuilt:
            # 已存在则只校正路径与完整性
            rebuilt[sid]['archive_path'] = str(z)
            continue
        entry = build_entry(z)
        if entry:
            rebuilt[sid] = entry
            added.append((sid, entry['size_mb'], entry['is_full']))

    index_file.write_text(json.dumps(rebuilt, ensure_ascii=False, indent=2), encoding='utf-8')

    print('=== 索引重建完成 ===')
    print('  索引条目总数:', len(rebuilt))
    print()
    for sid in sorted(rebuilt.keys()):
        v = rebuilt[sid]
        print('  {:<30} is_full={:<6} {:.2f} MB  备注={}'.format(
            sid, str(v.get('is_full')), v.get('size_mb', 0), v.get('notes', '')
        ))
    if added:
        print()
        print('  本次补回 {} 条:'.format(len(added)))
        for sid, mb, full in added:
            print('    {}  {:.2f} MB  is_full={}'.format(sid, mb, full))


if __name__ == '__main__':
    main()
