# -*- coding: utf-8 -*-
"""打包 ZeroFall 发行版

  --mode standalone 单体版（开源默认：不连通道、不上报、无主控能力）
--mode master  主控版（含主控/子控全部功能，默认 role=master）
  --mode agent   子控版（role=agent，打开上报开关，体积一样）

产出：
  dist/ZeroFall.exe
  <桌面>/ZeroFall_v<ver>_<mode>.zip   （exe + 源码 + 配置 + 说明）
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

SRC = Path(__file__).parent
VERSION = 'v1.3.0'

# 发行包包含
INCLUDE_FILES = [
    'main.py', 'config.py', 'utils.py', 'logger.py',
    'requirements.txt', 'ZeroFall.spec', 'ZeroFall.ico', 'icon.png',
    'README.md', 'QUICKSTART.md',
]
INCLUDE_DIRS = ['modules', 'ui']
TOOL_FILES = [
    'rebuild_index.py', 'test_restore_scenarios.py', 'test_control_e2e.py',
    'test_acp_fileops.py', 'test_fix_only.py',
    'detect_model_effort.js', 'apply_real_effort_map.py',
    'verify_real_map.js', 'test_effort_guard.js',
    'inject_effort_guard.py', 'diag_model_effort.py', 'generate_icon.py',
    'model_effort_map.json',
]
IGNORE = ('__pycache__', '.pyc', '.pyo', 'build', 'dist', 'logs', '.git')


def build(mode, out_dir, desktop=True):
    import zipfile
    import time

    # 模板优先用 example（干净、不含本机路径）；没有再退回实际配置
    _example = SRC / 'zerofall_config.example.json'
    _actual = SRC / 'zerofall_config.json'
    _tpl = _example if _example.exists() else _actual
    if _tpl is _actual:
        print('  [警告] 没有 zerofall_config.example.json，改用实际配置（会带上本机路径）')
    cfg = json.loads(_tpl.read_text(encoding='utf-8-sig'))

    # 无论模板从哪来，本机相关路径一律清空 —— 发布出去的包不该有
    # 开发者的用户名、盘符和网盘目录。首次运行由程序自己探测。
    for _k in ('deepseek_exe_path', 'deepseek_plugins_dir', 'deepseek_config_dir',
               'dsh_appdata_dir', 'snapshot_dir', 'cloud_sync_dir',
               'control_channel_dir', 'master_source_dir'):
        if _k in cfg:
            cfg[_k] = ''
    # 角色必须强制覆盖（源配置里可能残留上一次的值）
    # 部署形态决定这台机器开机后跑什么：
    #   standalone 不碰控制通道（开源默认，用户拿到手就是纯本地工具）
    cfg['deployment_mode'] = mode
    cfg['role'] = 'master' if mode == 'master' else 'agent'
    # 不要 setdefault 一个具体盘符 —— 别人机器上没有这个目录。
    # 空值 = 未配置控制通道，主控/子控功能由用户自己填路径。
    if mode == 'standalone':
        # 单体版：不向任何人上报任何数据
        cfg['report_to_master'] = False
        cfg['report_zf_error'] = False
        cfg['report_dsh_crash'] = False
        cfg['http_server_enabled'] = False
        cfg['auto_fix_enabled'] = False
        cfg['master_self_update'] = False
    else:
        cfg.setdefault('report_to_master', True)
    cfg.setdefault('report_zf_error', True)
    cfg.setdefault('report_dsh_crash', True)
    cfg.setdefault('auto_apply_update', True)
    cfg.setdefault('auto_fix_enabled', True)
    cfg.setdefault('master_self_update', True)
    cfg.setdefault('acp_profile', 'zf-acp')

    stage = Path(out_dir) / '_stage_{}'.format(mode)
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    root = stage / 'ZeroFall_{}_{}'.format(VERSION, mode)
    root.mkdir(parents=True)

    exe = SRC / 'dist' / 'ZeroFall.exe'
    if not exe.exists():
        raise SystemExit('找不到 dist/ZeroFall.exe，请先 pyinstaller 打包')
    shutil.copy2(exe, root / 'ZeroFall.exe')

    # 图标也在 exe 旁边放一份。
    # 运行本身不依赖它（图标已经打进 exe 的 PE 资源，托盘走 _MEIPASS），
    # 但做快捷方式、改图标、做绿色版时手边得有个文件，省得去 source/ 里翻。
    _ico = SRC / 'ZeroFall.ico'
    if _ico.exists():
        shutil.copy2(_ico, root / 'ZeroFall.ico')

    (root / 'zerofall_config.json').write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

    (root / '启动 ZeroFall.bat').write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nstart "" "ZeroFall.exe"\r\n', encoding='utf-8')

    readme = SRC / 'README_PACKAGE.md'
    if readme.exists():
        shutil.copy2(readme, root / 'README.md')

    sd = root / 'source'
    sd.mkdir()
    for n in INCLUDE_FILES:
        p = SRC / n
        if p.exists():
            shutil.copy2(p, sd / n)
    for d in INCLUDE_DIRS:
        p = SRC / d
        if p.exists():
            shutil.copytree(p, sd / d, ignore=shutil.ignore_patterns(*IGNORE))
    tools = sd / 'tools'
    tools.mkdir()
    for n in TOOL_FILES:
        # 开发脚本已整理到 tools/ 子目录；根目录还留着时也能取到，
        # 免得哪天把文件挪回去就静默漏打包
        p = SRC / n
        if not p.exists():
            p = SRC / 'tools' / n
        if p.exists():
            shutil.copy2(p, tools / n)

    out_zip = Path(out_dir) / 'ZeroFall_{}_{}.zip'.format(VERSION, mode)
    if out_zip.exists():
        out_zip.unlink()
    n = 0
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in root.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(stage))
                n += 1
    shutil.rmtree(stage, ignore_errors=True)

    print('  输出: {}'.format(out_zip))
    print('  大小: {:.2f} MB | 文件数: {}'.format(out_zip.stat().st_size / 1024 / 1024, n))
    return out_zip


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['standalone', 'master', 'agent', 'both'], default='both')
    ap.add_argument('--out', default=r'D:\zerofall')
    args = ap.parse_args()
    modes = ['master', 'agent'] if args.mode == 'both' else [args.mode]
    for m in modes:
        print('=== 打包 {} 版 ==='.format(m))
        build(m, args.out)
