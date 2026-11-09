# -*- coding: utf-8 -*-
"""诊断包导出

开源分发后，"把崩溃数据自动上报给作者"是不合适的：隐私责任、成本、
信任问题都落在维护者头上。正确做法是让用户**自己导出**一份诊断包，
贴到 GitHub Issue 里。

这个模块负责打包这些东西：
  · 最近日志（截断，避免几 MB）
  · 配置快照（**强制脱敏**：所有 key/token/密码类字段替换成掩码）
  · 插件清单（名字+版本+状态，不含插件代码）
  · 环境校验结果
  · 快照列表摘要
  · 系统信息（OS / Python / exe 版本 / 屏幕）

刻意不做的事：
  · 不打包任何插件源码、会话文件、快照压缩包本体
  · 不打包明文凭据 —— 脱敏是硬约束，测试里有专门断言
"""
import json
import os
import platform
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

# 键名里出现这些词的配置项一律掩码
_SENSITIVE_HINTS = (
    'key', 'token', 'secret', 'password', 'passwd', 'credential',
    'auth', 'private', 'cookie', 'session_id', 'app_key', 'refresh',
)

_MASK = '***REDACTED***'


def _is_sensitive(key: str) -> bool:
    k = str(key).lower()
    return any(h in k for h in _SENSITIVE_HINTS)


def redact(obj):
    """递归脱敏：敏感键的值换成掩码

    对字典/列表/嵌套结构都生效，避免某些配置把 key 放在 payload 里逃过检查。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _is_sensitive(k):
                # 保留"有没有值"的信息，方便判断是不是没配，但不泄露内容
                if isinstance(v, (str, int, float)) and str(v):
                    out[k] = _MASK
                else:
                    out[k] = v
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, tuple):
        return [redact(x) for x in obj]
    return obj


def _system_info(config) -> dict:
    info = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'platform': platform.platform(),
        'windows_version': platform.version(),
        'python': sys.version.split()[0],
        'frozen': bool(getattr(sys, 'frozen', False)),
        'executable': sys.executable,
        'cwd': os.getcwd(),
    }
    try:
        from modules import __version__ as mod_ver  # type: ignore  # noqa: F401
    except Exception:
        pass
    info['app_version'] = getattr(config, 'app_version', '')
    info['deployment_mode'] = getattr(config, 'deployment_mode', 'standalone')
    info['role'] = getattr(config, 'role', '')
    return info


def _config_dump(config) -> dict:
    """配置全量导出（已脱敏）"""
    try:
        if hasattr(config, 'to_dict'):
            data = config.to_dict()
        else:
            data = dict(getattr(config, '__dict__', {}))
    except Exception as e:
        return {'_error': '导出配置失败: {}'.format(e)}
    return redact(data)


def _tail_logs(log_dir: Path, max_lines: int = 1200) -> str:
    """取最近一个日志文件的尾部若干行"""
    try:
        if not log_dir.exists():
            return '（日志目录不存在）'
        logs = sorted(log_dir.glob('*.log'), key=lambda p: p.stat().st_mtime)
        if not logs:
            return '（没有日志文件）'
        latest = logs[-1]
        with open(latest, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        head = '=== {} （共 {} 行，取最后 {} 行）===\n'.format(
            latest.name, len(lines), min(len(lines), max_lines))
        return head + ''.join(lines[-max_lines:])
    except Exception as e:
        return '（读取日志失败: {}）'.format(e)


def _plugins_dump(config) -> list:
    """插件清单：只取元信息，不含代码"""
    out = []
    try:
        pdir = Path(getattr(config, 'deepseek_plugins_dir', '') or '')
        if not pdir.exists():
            return [{'note': '插件目录不存在'}]
        for child in sorted(pdir.iterdir())[:200]:
            entry = {
                'name': child.name,
                'type': 'dir' if child.is_dir() else 'file',
            }
            manifest = child / 'package.json' if child.is_dir() else None
            if manifest and manifest.exists():
                try:
                    meta = json.loads(manifest.read_text(
                        encoding='utf-8', errors='ignore'))
                    entry['version'] = meta.get('version', '')
                    entry['description'] = str(meta.get('description', ''))[:120]
                except Exception:
                    entry['version'] = '(解析失败)'
            out.append(entry)
    except Exception as e:
        out.append({'error': str(e)})
    return out


def _env_report(config) -> dict:
    """跑一次环境校验并转成可读字典"""
    try:
        from modules.environment_checker import EnvironmentChecker
        ec = EnvironmentChecker(config)
        rep = ec.run_full_check(auto=False)
        return {
            'overall': rep.overall_result.value,
            'items': [
                {'name': i.name, 'result': i.result.value,
                 'message': i.message, 'details': (i.details or '')[:800]}
                for i in rep.items
            ],
        }
    except Exception as e:
        return {'error': '环境校验失败: {}'.format(e)}


def _snapshots_dump(config) -> dict:
    """快照摘要：只要时间/类型/大小，不含内容"""
    try:
        from modules.snapshot_manager import SnapshotManager
        sm = SnapshotManager(config)
        snaps = list(getattr(sm, 'snapshots', {}).values())
        items = []
        for s in snaps[:60]:
            items.append({
                'id': getattr(s, 'snapshot_id', ''),
                'created_at': getattr(s, 'created_at', ''),
                'is_full': bool(getattr(s, 'is_full', False)),
                'size_mb': round(float(getattr(s, 'size_mb', 0) or 0), 2),
                'healthy': bool(getattr(s, 'is_healthy', True)),
                'notes': str(getattr(s, 'notes', ''))[:80],
            })
        return {'count': len(snaps), 'items': items}
    except Exception as e:
        return {'error': '读取快照失败: {}'.format(e)}


def build_diagnostic_bundle(config, dest_path=None, log_dir=None,
                            include_env_check=True) -> Path:
    """生成诊断包 zip，返回文件路径

    Args:
        config: ZeroFallConfig
        dest_path: 目标 zip 路径；None 则放到桌面
        log_dir: 日志目录；None 用 config 推导
        include_env_check: 是否跑一次环境校验（会扫目录，稍慢）
    """
    if dest_path is None:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        desktop = Path(os.path.expanduser('~')) / 'Desktop'
        base = desktop if desktop.exists() else Path(tempfile.gettempdir())
        dest_path = base / 'ZeroFall_diagnostic_{}.zip'.format(stamp)
    dest = Path(dest_path)

    if log_dir is None:
        try:
            log_dir = Path(getattr(config, 'snapshot_dir', '') or '').parent / 'logs'
        except Exception:
            log_dir = Path('logs')

    dest.parent.mkdir(parents=True, exist_ok=True)

    cfg_dict = _config_dump(config)
    sysinfo = _system_info(config)
    plugins = _plugins_dump(config)
    snaps = _snapshots_dump(config)
    logs = _tail_logs(Path(log_dir))
    env = _env_report(config) if include_env_check else {'skipped': True}

    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('system.json', json.dumps(sysinfo, ensure_ascii=False, indent=2))
        z.writestr('config.json', json.dumps(cfg_dict, ensure_ascii=False, indent=2))
        z.writestr('plugins.json', json.dumps(plugins, ensure_ascii=False, indent=2))
        z.writestr('snapshots.json', json.dumps(snaps, ensure_ascii=False, indent=2))
        z.writestr('environment.json', json.dumps(env, ensure_ascii=False, indent=2))
        z.writestr('logs.txt', logs)
        z.writestr('README.txt', (
            'ZeroFall 诊断包\n'
            '================\n'
            '提 Issue 时请附上这个 zip。内容说明：\n'
            '  system.json       系统与版本信息\n'
            '  config.json       配置快照（密钥/令牌类字段已脱敏）\n'
            '  plugins.json      插件清单（仅名称/版本，不含插件代码）\n'
            '  snapshots.json    快照列表摘要（不含快照内容）\n'
            '  environment.json  环境校验结果\n'
            '  logs.txt          最近日志尾部\n\n'
            '不包含：插件源码、会话文件、快照压缩包、任何明文凭据。\n'
            '发送前建议自己解压看一眼，确认没有不想外传的路径信息。\n'
        ))
    return dest
