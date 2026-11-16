# -*- coding: utf-8 -*-
"""把新构建打成更新包，丢进本地更新通道

用法：
    python publish_update.py                     # 默认 dist_new\\ZeroFall.exe -> dist\\updates
    python publish_update.py --exe <path> --out <dir> --notes "改了什么"

更新包版本号取 exe 的 mtime（YYYYMMDDHHMMSS），和运行时
modules.updater.read_version_from_exe 读出来的完全一致 —— 这样"哪个更新"
判断不依赖人工填版本，构建时间晚就一定更新。

打包完成后不需要再做任何事：运行中的 ZeroFall 会在下一轮检查
（默认 30 分钟，或启动 90 秒后）自己换版重启。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from modules.updater import UpdatePackage, read_version_from_exe  # noqa: E402

ROOT = Path(__file__).parent
DEFAULT_EXE = ROOT / 'dist_new' / 'ZeroFall.exe'
DEFAULT_OUT = ROOT / 'dist' / 'updates'


def publish(exe_path, out_dir, notes=''):
    exe = Path(exe_path)
    if not exe.is_file():
        raise SystemExit('找不到 exe: {}'.format(exe))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    ver = read_version_from_exe(exe)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    pkg_path = out / 'ZeroFall_update_{}_{}.zip'.format(ver, stamp)

    path, manifest = UpdatePackage.build(
        pkg_path, exe_path=exe, version=ver,
        notes=notes or '本地更新通道自动发布',
        origin_agent='local-publisher')
    print('已发布: {}'.format(path))
    print('  version = {}'.format(manifest.get('version')))
    print('  exe     = {} bytes / sha256 {}'.format(
        manifest['exe']['size'], manifest['exe']['sha256'][:16] + '…'))
    print('  created = {}'.format(manifest.get('created_at')))
    print('运行中的 ZeroFall 会在下一轮检查时自动换版（默认 30 分钟内）。')
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', default=str(DEFAULT_EXE))
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--notes', default='')
    a = ap.parse_args()
    publish(a.exe, a.out, a.notes)


if __name__ == '__main__':
    main()
