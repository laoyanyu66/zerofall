# -*- coding: utf-8 -*-
"""更新包：打包、分发、应用

更新包格式（zip）：
    manifest.json     版本信息 + 文件清单 + 校验
    ZeroFall.exe      新版可执行文件（子控替换这个）
    source.zip        可选：源码快照（主控自更新时重新打包用）

设计要点：
  * 子控只拿 exe 就能更新，不要求开发环境
  * 主控拿 exe + source.zip，既能分发也能自更新重打包
  * 应用前自动备份旧 exe，出问题可回滚
"""
import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_version_from_exe(exe_path) -> str:
    """从 exe 里读版本号（读不到就退回文件 mtime）"""
    try:
        import win32api
        info = win32api.GetFileVersionInfo(str(exe_path), '\\')
        ms = info['FileVersionMS']
        ls = info['FileVersionLS']
        return '{}.{}.{}.{}'.format(ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except Exception:
        try:
            return time.strftime('%Y%m%d%H%M%S', time.localtime(os.path.getmtime(exe_path)))
        except Exception:
            return '0.0.0.0'


class UpdatePackage:
    """更新包读写"""

    MANIFEST = 'manifest.json'
    EXE_NAME = 'ZeroFall.exe'
    SOURCE_NAME = 'source.zip'

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def build(out_zip, exe_path=None, source_dir=None, version=None,
              notes='', origin_agent='', error_report=None):
        """打包一个更新包"""
        out_zip = Path(out_zip)
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        if out_zip.exists():
            out_zip.unlink()

        manifest = {
            'version': version or read_version_from_exe(exe_path),
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'notes': notes,
            'origin_agent': origin_agent,
            'error_report': error_report or {},
            'exe': None,
            'source': None,
        }

        with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            if exe_path and Path(exe_path).exists():
                exe_path = Path(exe_path)
                data = exe_path.read_bytes()
                zf.writestr(UpdatePackage.EXE_NAME, data)
                manifest['exe'] = {
                    'name': UpdatePackage.EXE_NAME,
                    'size': len(data),
                    'sha256': sha256_bytes(data),
                }
            if source_dir and Path(source_dir).exists():
                import io
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as sz:
                    base = Path(source_dir)
                    for f in base.rglob('*'):
                        if not f.is_file():
                            continue
                        rel = f.relative_to(base)
                        parts = rel.parts
                        if parts[0] in ('build', 'dist', '__pycache__', 'logs', '.git'):
                            continue
                        if f.suffix in ('.pyc', '.pyo'):
                            continue
                        sz.write(f, rel.as_posix())
                data = buf.getvalue()
                zf.writestr(UpdatePackage.SOURCE_NAME, data)
                manifest['source'] = {
                    'name': UpdatePackage.SOURCE_NAME,
                    'size': len(data),
                    'sha256': sha256_bytes(data),
                }
            zf.writestr(UpdatePackage.MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))

        return out_zip, manifest

    def read_manifest(self):
        with zipfile.ZipFile(self.path) as zf:
            return json.loads(zf.read(self.MANIFEST).decode('utf-8'))

    def verify(self):
        """校验包完整性，返回 (ok, message, manifest)"""
        try:
            manifest = self.read_manifest()
        except Exception as e:
            return False, '无法读取 manifest: {}'.format(e), None
        try:
            with zipfile.ZipFile(self.path) as zf:
                for key in ('exe', 'source'):
                    meta = manifest.get(key)
                    if not meta:
                        continue
                    if meta['name'] not in zf.namelist():
                        return False, '缺少 {}'.format(meta['name']), manifest
                    data = zf.read(meta['name'])
                    if sha256_bytes(data) != meta['sha256']:
                        return False, '{} 校验和不符'.format(meta['name']), manifest
            return True, '校验通过', manifest
        except Exception as e:
            return False, '校验异常: {}'.format(e), None

    def extract(self, dest_dir):
        """解出 exe 与 source.zip"""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = {}
        with zipfile.ZipFile(self.path) as zf:
            if self.EXE_NAME in zf.namelist():
                out['exe'] = dest / self.EXE_NAME
                out['exe'].write_bytes(zf.read(self.EXE_NAME))
            if self.SOURCE_NAME in zf.namelist():
                out['source'] = dest / self.SOURCE_NAME
                out['source'].write_bytes(zf.read(self.SOURCE_NAME))
        return out


def _spawn_replace_and_restart(script_body: str, log_path: Path = None):
    """用独立的 PowerShell 进程完成"等主进程退出 -> 替换 exe -> 重启"。

    必须在主进程完全退出后替换，否则 exe 被占用写不进去。
    """
    log = log_path or (Path(os.environ.get('TEMP', '.')) / 'zerofall_update.log')
    script = (
        '$ErrorActionPreference = "Continue"\n'
        '$log = "{log}"\n'
        'Start-Sleep -Seconds 4\n'
        '{body}\n'
    ).format(log=str(log), body=script_body)

    ps = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    args = ['-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-Command', script]
    try:
        subprocess.Popen([str(ps)] + args, creationflags=0x08000000)  # DETACHED_PROCESS
        return True
    except Exception:
        return False


def apply_update(package_path, install_dir=None, restart=True, log_path=None):
    """应用更新：备份旧 exe -> 替换 -> 可选重启

    返回 (ok, message)
    """
    pkg = UpdatePackage(package_path)
    ok, msg, manifest = pkg.verify()
    if not ok:
        return False, '更新包校验失败: {}'.format(msg)

    if install_dir is None:
        install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent.parent
    install_dir = Path(install_dir)

    target_exe = install_dir / UpdatePackage.EXE_NAME
    if not target_exe.exists():
        # 开发模式：更新的是源码
        src = pkg.extract(install_dir / '_update_tmp')
        if 'source' in src:
            import zipfile
            with zipfile.ZipFile(src['source']) as zf:
                zf.extractall(install_dir)
            shutil.rmtree(install_dir / '_update_tmp', ignore_errors=True)
            return True, '源码已更新（开发模式）'
        return False, '找不到目标 exe 且包内无源码'

    version = manifest.get('version', '0')
    backup = install_dir / '{}.bak-{}'.format(UpdatePackage.EXE_NAME, version)
    try:
        if backup.exists():
            backup.unlink()
        shutil.copy2(target_exe, backup)
    except Exception as e:
        return False, '备份旧 exe 失败: {}'.format(e)

    extracted = pkg.extract(install_dir / '_update_tmp')
    new_exe = extracted.get('exe')
    if not new_exe or not new_exe.exists():
        return False, '更新包内没有 exe'

    if restart:
        body = (
            'try {{ Copy-Item "{new}" -Destination "{target}" -Force -ErrorAction Stop; '
            'Remove-Item "{tmp}" -Recurse -Force -ErrorAction SilentlyContinue; '
            'Start-Process "{target}"; '
            'Add-Content -Path $log -Value ("{v} updated at " + (Get-Date -Format "s")) }} '
            'catch {{ Add-Content -Path $log -Value ("FAILED: " + $_.Exception.Message) }}'
        ).format(new=str(new_exe), target=str(target_exe), tmp=str(install_dir / '_update_tmp'), v=version)
        if not _spawn_replace_and_restart(body, log_path):
            return False, '无法调度替换任务'
        return True, '已安排替换并重启（v{}）'.format(version)

    # 不重启：直接替换（要求当前不是该 exe 本身在跑）
    try:
        shutil.copy2(new_exe, target_exe)
        shutil.rmtree(install_dir / '_update_tmp', ignore_errors=True)
        return True, '已更新到 v{}（未重启）'.format(version)
    except Exception as e:
        return False, '替换失败: {}'.format(e)


import sys  # noqa: E402  (放在末尾避免循环引用告警)
