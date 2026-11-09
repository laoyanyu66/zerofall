# -*- coding: utf-8 -*-
"""把 ZeroFall 打包成"解压即用"的 zip（exe + 源码）"""
import json
import shutil
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
EXE = Path(r'D:\zerofall\ZeroFall.exe')
VERSION = 'v1.2.0'
OUT_DIR = Path(r'D:\zerofall')
STAGE = Path(r'D:\zerofall\_stage_build')
OUT_ZIP = OUT_DIR / 'ZeroFall_{}.zip'.format(VERSION)

# ── 源码要带走的文件/目录 ──
INCLUDE_FILES = [
    'main.py', 'config.py', 'utils.py', 'logger.py',
    'requirements.txt', 'ZeroFall.spec',
    'README.md', 'QUICKSTART.md', 'CHANGELOG_v1.1.0.md',
    'ZeroFall.ico', 'icon.png',
]
INCLUDE_DIRS = ['modules', 'ui']

# ── 工具脚本（放 source/tools/）──
TOOL_FILES = [
    'rebuild_index.py', 'test_restore_scenarios.py',
    'detect_model_effort.js', 'apply_real_effort_map.py',
    'verify_real_map.js', 'test_effort_guard.js',
    'inject_effort_guard.py', 'diag_model_effort.py',
    'generate_icon.py', 'model_effort_map.json',
]

# ── 忽略 ──
IGNORE_PATTERNS = ('__pycache__', '.pyc', '.pyo', 'build', 'dist', 'logs', '.git')


def should_skip(path: Path) -> bool:
    parts = path.parts
    return any(p in IGNORE_PATTERNS for p in parts)


def main():
    if not EXE.exists():
        print('ERROR: exe 不存在:', EXE)
        return 1
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)

    root = STAGE / 'ZeroFall_{}'.format(VERSION)
    root.mkdir()

    # 1) exe + 图标 + 启动脚本
    shutil.copy2(EXE, root / 'ZeroFall.exe')
    print('  + ZeroFall.exe ({:.2f} MB)'.format(EXE.stat().st_size / 1024 / 1024))
    shutil.copy2(SRC / 'ZeroFall.ico', root / 'ZeroFall.ico')
    print('  + ZeroFall.ico')

    # 2) 配置（补齐新字段，避免解压后走默认值出意外）
    cfg = json.loads((SRC / 'zerofall_config.json').read_text(encoding='utf-8'))
    defaults = {
        'max_light_snapshots': 48,
    'plugin_drift_snapshot': True,
    'plugin_drift_check_seconds': 60,
    'plugin_drift_settle_seconds': 20,
        'max_full_snapshots': 12,
        'full_snapshot_interval_minutes': 30,   # 完整快照间隔（分钟，锚定当天首个）
    'full_snapshot_every_n_light': 6,        # 仅在间隔=0 时生效（计数模式）
        'cloud_overflow_enabled': True,
        'daily_cloud_consolidate': True,
        'cloud_sync_dir': r'E:\BaiduSyncdisk\zerofall-snapshots',
        'cloud_sync_encrypt': True,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    (root / 'zerofall_config.json').write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print('  + zerofall_config.json')

    # 3) 启动脚本
    (root / '启动 ZeroFall.bat').write_text(
        '@echo off\r\n'
        'cd /d "%~dp0"\r\n'
        'start "" "ZeroFall.exe"\r\n',
        encoding='utf-8'
    )
    print('  + 启动 ZeroFall.bat')

    # 4) 使用说明
    shutil.copy2(SRC / 'README_PACKAGE.md', root / 'README.md')
    print('  + README.md')

    # 5) 源码
    src_dir = root / 'source'
    src_dir.mkdir()
    for name in INCLUDE_FILES:
        p = SRC / name
        if p.exists():
            shutil.copy2(p, src_dir / name)
    for d in INCLUDE_DIRS:
        sp = SRC / d
        if sp.exists():
            shutil.copytree(
                sp, src_dir / d,
                ignore=shutil.ignore_patterns(*IGNORE_PATTERNS)
            )
    print('  + source/ (代码)')

    # 6) 工具
    tools = src_dir / 'tools'
    tools.mkdir()
    for name in TOOL_FILES:
        p = SRC / name
        if p.exists():
            shutil.copy2(p, tools / name)
    print('  + source/tools/ (维护工具)')

    # 7) 打包
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()
    file_count = 0
    with zipfile.ZipFile(OUT_ZIP, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in root.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(STAGE))
                file_count += 1
    print()
    print('=== 打包完成 ===')
    print('  输出:', OUT_ZIP)
    print('  大小: {:.2f} MB'.format(OUT_ZIP.stat().st_size / 1024 / 1024))
    print('  文件数:', file_count)

    # 8) 清理暂存
    shutil.rmtree(STAGE, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
