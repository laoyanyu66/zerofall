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
    """【已弃用，保留兼容】用 PowerShell 等主进程退出后替换 exe。

    2026-11-15 实测这条路在真实运行环境下不成立：父进程一退出，这个子进程就被
    一并带走，既没替换也没日志。换版改走 `_arm_restart_after_update`（一次性计划任务）。
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
        # 0x08000000 = CREATE_NO_WINDOW，不是 DETACHED_PROCESS（后者是 0x8）。
        # 这里要的只是"别闪控制台窗口"；这个 PowerShell 没有被 job 绑定，
        # 父进程退出后它会自己把脚本跑完，不需要真正的 DETACHED_PROCESS。
        subprocess.Popen([str(ps)] + args, creationflags=0x08000000)
        return True
    except Exception:
        return False


RESTART_TASK_NAME = 'ZeroFall-Restart-After-Update'


def _arm_restart_after_update(target_exe, delay_seconds: int = 8):
    """安排"延迟启动新 exe"：一次性计划任务，跟当前进程的生死解耦。

    为什么不用 subprocess 直接在退出前拉起来：实测父进程退出会把这个子进程一起
    带走（见 `_spawn_replace_and_restart` 的注释）。计划任务由 Task Scheduler
    服务拉起，父进程退不退出都不影响它。

    返回 (是否成功, 说明)；失败不影响"exe 已经换成新版"这个事实。
    """
    exe = Path(target_exe)
    runner = exe.parent / 'updates' / 'restart-after-update.cmd'
    try:
        runner.parent.mkdir(parents=True, exist_ok=True)
        body = (
            '@echo off\r\n'
            'ping -n {d} 127.0.0.1 >nul\r\n'
            'start "" "{exe}"\r\n'
            'schtasks /Delete /TN "{tn}" /F >nul 2>&1\r\n'
        ).format(d=int(delay_seconds) + 1, exe=exe, tn=RESTART_TASK_NAME)
        runner.write_bytes(body.encode('ascii', 'ignore'))
    except Exception as e:
        return False, '写重启脚本失败: {}'.format(e)

    schtasks = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'schtasks.exe'
    st = time.strftime('%H:%M', time.localtime(time.time() + 300))
    try:
        r1 = subprocess.run([str(schtasks), '/Create', '/F', '/TN', RESTART_TASK_NAME,
                             '/TR', str(runner), '/SC', 'ONCE', '/ST', st],
                            creationflags=0x08000000, capture_output=True, text=True)
        if r1.returncode != 0:
            return False, '建任务失败: {}'.format(
                ((r1.stderr or '') + (r1.stdout or '')).strip()[:120])
        r2 = subprocess.run([str(schtasks), '/Run', '/TN', RESTART_TASK_NAME],
                            creationflags=0x08000000, capture_output=True, text=True)
        if r2.returncode != 0:
            return False, '触发任务失败: {}'.format(
                ((r2.stderr or '') + (r2.stdout or '')).strip()[:120])
    except Exception as e:
        return False, '调度重启失败: {}'.format(e)
    return True, '计划任务 {} 已触发'.format(RESTART_TASK_NAME)


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
        # 2026-11-15 修：原来靠"主进程退出后再复制"的外部 PowerShell —— 实测那一刻
        # 子进程被一并带走（%TEMP% 没有 zerofall_update.log、exe 没换、只剩
        # dist\_update_tmp\ZeroFall.exe 和一份 .bak）。于是"自更新"看起来成功，
        # 版本其实永远停在旧的。
        #
        # 新做法分两步，都不依赖"父进程死后再干活"：
        #   1. 本进程内先改名让开句柄，再拷新 exe（Windows 允许给运行中的 exe 改名）；
        #   2. 重启交给一次性计划任务（Task Scheduler 拉起的进程不受父进程退出影响）。
        stamp = time.strftime('%Y%m%d-%H%M%S')
        old_path = install_dir / '{}.old-{}'.format(UpdatePackage.EXE_NAME, stamp)
        try:
            os.replace(str(target_exe), str(old_path))     # 同卷改名，运行中的映像不受影响
            shutil.copy2(str(new_exe), str(target_exe))
            shutil.rmtree(install_dir / '_update_tmp', ignore_errors=True)
        except Exception as e:
            try:                                           # 尽力回滚，别把现场弄坏
                if not target_exe.exists() and old_path.exists():
                    os.replace(str(old_path), str(target_exe))
            except Exception:
                pass
            return False, '替换 exe 失败: {}'.format(e)

        armed, detail = _arm_restart_after_update(target_exe)
        if armed:
            return True, '已替换并安排重启（v{}，{}）'.format(version, detail)
        # 任务没建成也不能把程序关掉：exe 已经是新的，下次启动就是新版本
        return True, '已替换 exe（v{}），但这轮没能安排自动重启（{}），下次启动生效'.format(version, detail)

    # 不重启：直接替换（要求当前不是该 exe 本身在跑）
    try:
        shutil.copy2(new_exe, target_exe)
        shutil.rmtree(install_dir / '_update_tmp', ignore_errors=True)
        return True, '已更新到 v{}（未重启）'.format(version)
    except Exception as e:
        return False, '替换失败: {}'.format(e)


import sys  # noqa: E402  (放在末尾避免循环引用告警)
