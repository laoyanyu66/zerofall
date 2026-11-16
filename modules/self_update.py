# -*- coding: utf-8 -*-
r"""本地更新通道（standalone 模式也能自更新）

背景：内置的主控/子控通道只在 deployment_mode=master/agent 时启用，
standalone（开源默认、也是本机在用的形态）连控制节点都不建，
于是"自动换版"这条路一直是断的 —— 每次升级都得人点一次 UAC。

这里补一条不依赖主控的通道，目录约定：
    <install_dir>\updates\*.zip        （UpdatePackage 格式）
谁把包放进去，运行中的 ZeroFall 就自己换版并重启。

关键点：替换动作由 updater.apply_update 完成 —— 它会拉起一个独立的
PowerShell，等主进程退出后再覆盖 exe。ZeroFall 本身是提权的，子进程继承
管理员令牌，所以覆盖 exe 不会再弹 UAC。

防呆三条：
  1. 只认校验通过的包（sha256 对不上直接跳过）；
  2. 只认"比当前 exe 新"的包（按包内 created_at 与 exe mtime 比），
     已经应用过的包按 sha256 记账，重启后不会重复装；
  3. 默认要求键鼠空闲几分钟才动手，避免正在用电脑时把 DSH 会话切断。
"""
import hashlib
import json
import time
from pathlib import Path

STATE_NAME = '.self_update_state.json'


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ''


def _created_ts(manifest, pkg_path) -> float:
    """包里的 created_at -> epoch；解析不了就退回文件 mtime"""
    raw = str((manifest or {}).get('created_at') or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except Exception:
            continue
    try:
        return Path(pkg_path).stat().st_mtime
    except Exception:
        return 0.0


def load_state(updates_dir) -> dict:
    try:
        p = Path(updates_dir) / STATE_NAME
        if p.is_file():
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                data.setdefault('applied', [])
                return data
    except Exception:
        pass
    return {'applied': []}


def save_state(updates_dir, state: dict) -> None:
    try:
        d = Path(updates_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / STATE_NAME
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(p)
    except Exception:
        pass


def find_new_package(updates_dir, current_exe, min_gap_seconds=120):
    """扫更新目录，返回 (包路径, manifest) 或 None

    min_gap_seconds：包生成时间要比当前 exe 的 mtime 新至少这么多秒，
    防止"同一个包反复被认成新版本"。
    """
    from modules.updater import UpdatePackage

    d = Path(updates_dir) if updates_dir else None
    if not d or not d.is_dir():
        return None
    try:
        cur_mtime = Path(current_exe).stat().st_mtime
    except Exception:
        cur_mtime = 0.0

    state = load_state(d)
    applied = set(state.get('applied') or [])

    best = None
    for f in sorted(d.glob('*.zip')):
        try:
            sha = _sha256_file(f)
            if not sha:
                continue
            pkg = UpdatePackage(f)
            ok, _msg, manifest = pkg.verify()
            if not ok or not manifest:
                continue
            if not manifest.get('exe'):
                continue          # 没有 exe 的包（纯源码包）不适用于换版
            if sha in applied:
                continue
            ts = _created_ts(manifest, f)
            if ts <= cur_mtime + min_gap_seconds:
                continue
            if best is None or ts > best[0]:
                best = (ts, f, manifest, sha)
        except Exception:
            continue

    if not best:
        return None
    return best[1], best[2]


def mark_applied(updates_dir, pkg_path, manifest=None) -> None:
    """记账：这个包已经装过了，重启后别再装一次"""
    try:
        state = load_state(updates_dir)
        sha = _sha256_file(pkg_path)
        if sha and sha not in state.get('applied', []):
            state.setdefault('applied', []).append(sha)
            state['applied'] = state['applied'][-50:]
        state['last_applied_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        state['last_version'] = str((manifest or {}).get('version') or '')
        save_state(updates_dir, state)
    except Exception:
        pass
