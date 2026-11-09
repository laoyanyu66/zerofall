# -*- coding: utf-8 -*-
"""主控：收到子控报告 -> 自动让 DSH 修源码 -> 打包更新 -> 分发 -> 自更新

流程：
  1. 扫描通道里的未处理报告
  2. 拉起 ACP 会话（cwd = ZeroFall 源码目录），把错误详情交给 DSH agent 修
  3. 重新打包 exe（pyinstaller），生成更新包（含 exe + 源码快照）
  4. 发布到通道，所有子控自动拉到
  5. 主控自己也应用更新并重启
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import re
import hashlib
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from logger import ZeroFallLogger
from modules.control import ControlChannel, version_tuple
from modules.updater import UpdatePackage, apply_update, read_version_from_exe

# 修复任务模板
FIX_PROMPT = """你是 ZeroFall（DeepSeek Harness 守护/快照/回退工具）的维护工程师。

某个子控实例上报了错误。请在**指定的源码目录**内修复它。

=== 源码目录（绝对路径，所有文件操作都基于这里）===
{source_root}

=== 错误报告 ===
{report}
=== 报告结束 ===

=== 可能相关的文件（绝对路径）===
{candidates}

修复要求：
1. 先读上面的候选文件，确认真实问题后**只改必要的那几行**，不要重构、不要改无关代码。
2. 如果候选文件都不相关，就在 {source_root} 里用 grep 搜索报错里的关键符号定位。
3. 改完必须用文件写入工具真正落盘（不要只在回答里贴代码）。
4. 保持现有代码风格（中文注释、函数式风格）。
5. 最后用不超过 6 行说明：根因、改了哪个文件、怎么验证。

现在开始。
"""

FIX_BUILD_CMD = [
    'pyinstaller', '--onefile', '--windowed', '--icon=ZeroFall.ico',
    '--add-data', 'ZeroFall.ico;.', '--name=ZeroFall', 'main.py',
    '--clean', '--noconfirm',
]

# 从报错文本里挑出可能相关的源文件
_FILE_HINT_RE = re.compile(
    r'([A-Za-z0-9_\-./\\]+\.(?:py|js|mjs|json|yml|yaml|ini|cfg))'
)


def extract_file_hints(text, source_root, limit=12):
    """从报错文本里提取可能的文件路径，返回绝对路径列表

    文件不存在时也给提示（agent 可能需要新建或该路径本就是线索）。
    """
    hints = []
    root = Path(source_root)
    for m in _FILE_HINT_RE.finditer(text or ''):
        raw = m.group(1).replace('\\', '/').lstrip('./')
        if not raw or raw.startswith('/'):
            continue
        cand = root / raw
        if cand.exists():
            hints.append(cand)
            continue
        # 只给文件名的话，在源码树里找同名文件
        name = Path(raw).name
        if len(name) > 3:
            found = False
            try:
                for f in root.rglob(name):
                    if any(p in ('build', 'dist', '__pycache__', '.git') for p in f.parts):
                        continue
                    hints.append(f)
                    found = True
                    break
            except Exception:
                pass
            if not found and '/' in raw:
                # 相对路径本身就是线索
                hints.append(cand)
        if len(hints) >= limit:
            break
    # 去重且保持顺序
    seen, out = set(), []
    for h in hints:
        s = str(h)
        if s not in seen:
            seen.add(s)
            out.append(h)
    return out[:limit]


def snapshot_py_hashes(source_root):
    """记录源码树里所有 .py 的哈希，用于事后判断有没有真的改动"""
    out = {}
    root = Path(source_root)
    for f in root.rglob('*.py'):
        if any(p in ('build', 'dist', '__pycache__', '.git') for p in f.parts):
            continue
        try:
            out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
        except Exception:
            pass
    return out


def diff_py_hashes(before, after):
    """返回 (改动列表, 新增列表)"""
    changed = [p for p in after if p in before and after[p] != before[p]]
    added = [p for p in after if p not in before]
    return changed, added


class MasterNode:
    """主控行为"""

    def __init__(self, config, snapshot_manager=None):
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.channel = ControlChannel(getattr(config, 'control_channel_dir', ''))
        self.source_dir = self.detect_source_dir()
        self.current_version = self.read_current_version()
        self.busy = False
        self.last_result = None

    # ── 基本信息 ────────────────────────────────────────────
    def detect_source_dir(self):
        """找 ZeroFall 源码目录"""
        cfg_dir = getattr(self.config, 'master_source_dir', '')
        if cfg_dir and Path(cfg_dir).is_dir():
            return Path(cfg_dir)
        if getattr(sys, 'frozen', False):
            # 打包后：exe 同级有 source/ 就用它
            for cand in (Path(sys.executable).parent / 'source',
                         Path(sys.executable).parent):
                if (cand / 'main.py').exists():
                    return cand
            return Path(sys.executable).parent
        return Path(__file__).parent.parent

    def read_current_version(self):
        try:
            if getattr(sys, 'frozen', False):
                return read_version_from_exe(sys.executable)
        except Exception:
            pass
        return getattr(self.config, 'app_version', '0.0.0.0')

    # ── 主循环 ──────────────────────────────────────────────
    def tick(self):
        """定时调用：处理待办报告。返回本次做了什么"""
        if self.busy:
            return None
        if not getattr(self.config, 'auto_fix_enabled', True):
            return None
        if not self.channel.available():
            return None
        reports = self.channel.list_reports(only_new=True)
        if not reports:
            # 顺手清理过期认领（主控崩溃残留），避免 claim 文件越积越多
            try:
                self.channel.cleanup_claims()
            except Exception:
                pass
            return None

        # 挑第一条「该处理」的报告：新报告立刻处理；失败过的要等够重试间隔
        target = None
        owner = '{}:{}'.format(socket.gethostname(), os.getpid())
        for path, doc in reports:
            rid = Path(path).stem
            ok, count, wait = self.channel.should_retry(
                rid, min_interval_seconds=600, max_attempts=3)
            if ok:
                # 原子认领：抢不到说明另一个主控正在处理，跳过它
                if not self.channel.claim_report(rid, owner):
                    ZeroFallLogger.info(
                        '[主控] 报告 {} 已被其它主控实例认领，跳过'.format(rid[:24]))
                    continue
                target = (path, doc)
                break
            if count >= 3:
                # 已放弃的报告：兜底补标记（正常情况下 _fail_or_retry 已处理）
                self.channel.mark_processed(path, {
                    'ok': False, 'gave_up': True,
                    'note': '重试次数已用尽'})
        if target is None:
            return None

        self.busy = True
        try:
            path, doc = target
            return self.handle_report(path, doc)
        finally:
            self.busy = False
            # 无论成败都释放认领，交给 .done / retry 状态决定后续
            try:
                self.channel.release_claim(Path(path).stem)
            except Exception:
                pass

    def _fail_or_retry(self, report_path, result, reason):
        """失败处理：先给重试机会，用尽次数才标记已处理

        认证态抖动（key 过期 / 额度耗尽 / 网关 429）都是临时故障，
        直接标记已处理等于把这条报告永久吞掉。所以：
          * 未达上限 → 只记一次尝试，保留报告，下轮再试
          * 达到上限 → 标记已处理（附失败原因），不再重试
        """
        rid = Path(report_path).stem
        count = self.channel.bump_attempt(rid)
        max_attempts = 3
        result['retry'] = {'attempt': count, 'max': max_attempts, 'reason': reason}
        if count >= max_attempts:
            result['retry']['gave_up'] = True
            self.channel.clear_attempt(rid)
            self.channel.mark_processed(report_path, result)
            ZeroFallLogger.warning(
                "[主控] 报告 {} 已失败 {} 次，放弃重试（{}）".format(
                    rid, count, reason))
        else:
            result['retry']['gave_up'] = False
            self.channel.retry_later(rid, result)
            ZeroFallLogger.warning(
                "[主控] 报告 {} 第 {} 次失败（{}），保留待下轮重试".format(
                    rid, count, reason))
        self.last_result = result
        return result

    def relay_channel(self):
        """按配置构造中转通道（未启用则返回 None）

        支持 WebDAV / S3 对象存储 / FTP 三种后端，由 relay_kind 决定。
        """
        try:
            from modules.net_transport import make_relay
            return make_relay(self.config)
        except Exception as e:
            ZeroFallLogger.warning("[主控] 中转通道构造失败: {}".format(e))
            return None

    def handle_report(self, report_path, doc):
        """处理单条报告：修复 -> 打包 -> 发布 -> 自更新"""
        kind = doc.get('kind')
        agent_id = doc.get('agent_id', 'unknown')
        result = {'report_id': doc.get('report_id'), 'agent_id': agent_id, 'kind': kind}

        try:
            if kind == 'heartbeat':
                # 心跳只更新存活表，不写 .done —— 否则每分钟一个文件，
                # 一年下来 processed/ 会被心跳标记塞满
                agent_id = doc.get('agent_id') or 'unknown'
                self.channel.record_agent(agent_id, {
                    'hostname': doc.get('hostname', ''),
                    'platform': doc.get('platform', ''),
                    'zerofall_version': doc.get('zerofall_version', ''),
                    'payload': doc.get('payload', {}),
                })
                try:
                    Path(report_path).unlink()
                except Exception:
                    self.channel.mark_processed(
                        report_path, {'ok': True, 'skipped': 'heartbeat'})
                return None

            # 1) 修复
            fix = self.run_fix(doc)
            result['fix'] = fix
            if not fix.get('ok'):
                return self._fail_or_retry(
                    report_path, result,
                    '修复失败: {}'.format(fix.get('error') or fix.get('stop_reason') or '未知'))

            # 2) 重新打包 exe
            build = self.rebuild_exe()
            result['build'] = build
            if not build.get('ok'):
                return self._fail_or_retry(
                    report_path, result,
                    '打包失败: {}'.format(build.get('error') or '未知'))

            # 3) 打包更新包
            version = self.next_version()
            out_zip = Path(self.channel.root) / 'build' / 'ZeroFall_update_{}.zip'.format(version)
            pkg_path, manifest = UpdatePackage.build(
                out_zip,
                exe_path=build['exe'],
                source_dir=self.source_dir,
                version=version,
                notes=fix.get('summary', '')[:500],
                origin_agent=agent_id,
                error_report={'kind': kind, 'error': (doc.get('payload') or {}).get('error', '')},
            )
            result['package'] = {'path': str(pkg_path), 'version': version}

            # 4) 发布
            published = self.channel.publish_update(version, pkg_path, manifest)
            result['published'] = published

            # 4b) 同步到 WebDAV 中转（跨网络子控靠它拉取）
            try:
                relay = self.relay_channel()
                if relay:
                    ok_relay = relay.publish_update(version, pkg_path, manifest)
                    result['relay_published'] = ok_relay
                    if ok_relay:
                        ZeroFallLogger.info(
                            "[主控] 更新包已同步到 WebDAV 中转 v{}".format(version))
                    else:
                        ZeroFallLogger.warning("[主控] WebDAV 中转发布失败")
            except Exception as e:
                result['relay_published'] = False
                ZeroFallLogger.warning("[主控] WebDAV 中转发布异常: {}".format(e))

            # 5) 自更新
            if getattr(self.config, 'master_self_update', True):
                result['self_update'] = self.apply_self_update(pkg_path, version)

            self.channel.clear_attempt(Path(report_path).stem)
            self.channel.mark_processed(report_path, result)
            self.last_result = result
            return result
        except Exception as e:
            result['error'] = str(e)
            result['traceback'] = traceback.format_exc()[-2000:]
            return self._fail_or_retry(report_path, result, '处理异常: {}'.format(e))
            return result

    # ── ACP 修复 ────────────────────────────────────────────
    def run_fix(self, doc):
        """用 ACP 驱动 DSH 修源码"""
        profile = getattr(self.config, 'acp_profile', 'zf-acp')
        payload = doc.get('payload') or {}
        report_text = json.dumps(payload, ensure_ascii=False, indent=2)[:6000]

        # 从报错里提取候选文件（绝对路径）
        hints = extract_file_hints(
            '{} {}'.format(payload.get('error', ''), payload.get('log_tail', '')),
            self.source_dir)
        if hints:
            cand_text = '\n'.join('- {}'.format(h) for h in hints)
        else:
            cand_text = '- （报错里没识别出文件路径，请自行 grep 定位）'

        prompt = FIX_PROMPT.format(
            source_root=str(self.source_dir),
            report=report_text,
            candidates=cand_text)

        # 记录改前哈希
        before = snapshot_py_hashes(self.source_dir)

        try:
            from modules.acp_client import AcpClient, pick_models
            with AcpClient(profile=profile, cwd=str(self.source_dir)) as cli:
                cli.initialize()
                sid, opts = cli.new_session(str(self.source_dir))
                # 一条路由不行就换另一条：余额不足/密钥失效都是常见且可切换的故障
                candidates = pick_models(opts)
                if not candidates:
                    return {'ok': False, 'error': 'ACP 没有可用的模型路由'}
                res = text = None
                name = None
                last_err = None
                tried = []
                errors = []
                for value, cand_name in candidates:
                    tried.append(cand_name or value)
                    _route = value.split('"')[1] if '"' in value else ''
                    try:
                        cli.set_model(sid, value)
                        res, text = cli.prompt(sid, prompt, timeout=1500)
                        name = cand_name
                        last_err = None
                        try:
                            from modules import model_memory
                            model_memory.record_ok(_route, cand_name or value)
                        except Exception:
                            pass
                        ZeroFallLogger.info(
                            '[主控] 使用模型 {} 完成修复会话'.format(cand_name))
                        break
                    except Exception as e:
                        last_err = str(e)
                        low = last_err.lower()
                        try:
                            from modules.acp_client import classify_acp_error
                            _kind = classify_acp_error(last_err)
                        except Exception:
                            _kind = 'other'
                        errors.append((last_err, _kind))
                        try:
                            from modules import model_memory
                            model_memory.record_fail(_route, cand_name or value,
                                                     _kind)
                        except Exception:
                            pass
                        ZeroFallLogger.warning(
                            '[主控] 模型 {} 不可用: {}'.format(cand_name, last_err[:160]))
                        # 这些属于"换个模型可能就好了"，值得继续试
                        switchable = _kind in ('auth', 'balance', 'forbidden',
                                               'deprecated', 'region',
                                               'unavailable') \
                            or any(k in low for k in (
                                'unknown model option', 'invalid params'))
                        if not switchable:
                            break
                if last_err:
                    # 别让最后一条"模型选项无效"盖掉"余额不足"这种真正的线索
                    try:
                        from modules.acp_client import (pick_informative_error,
                                                        reason_text)
                        msg, kind = pick_informative_error(errors)
                        hint = reason_text(kind)
                    except Exception:
                        msg, kind, hint = last_err, 'other', ''
                    return {'ok': False,
                            'error': '所有模型路由均失败: {} | 已尝试: {}'.format(
                                msg or last_err, ', '.join(tried)),
                            'kind': kind,
                            'hint': hint,
                            'tried': tried}
                stop = (res or {}).get('stopReason')
        except Exception as e:
            return {'ok': False, 'error': str(e)}

        # 验证是否真的产生了改动
        after = snapshot_py_hashes(self.source_dir)
        changed, added = diff_py_hashes(before, after)
        modified = changed + added
        if not modified:
            return {
                'ok': False,
                'error': 'ACP 会话结束但源码没有任何改动（stopReason={}）'.format(stop),
                'stop_reason': stop,
                'model': name,
                'summary': (text or '')[-1500:],
            }
        return {
            'ok': True,
            'stop_reason': stop,
            'model': name,
            'changed_files': [str(p) for p in modified],
            'summary': (text or '')[-3000:],
        }

    # ── 重新打包 ────────────────────────────────────────────
    def rebuild_exe(self):
        """用 pyinstaller 重新打包 exe"""
        src = self.source_dir
        if not (src / 'main.py').exists():
            return {'ok': False, 'error': '找不到源码 main.py（目录 {}）'.format(src)}
        exe_out = src / 'dist' / 'ZeroFall.exe'
        try:
            r = subprocess.run(
                FIX_BUILD_CMD, cwd=str(src), capture_output=True, text=True,
                encoding='utf-8', errors='replace', timeout=1800,
            )
            if not exe_out.exists():
                return {'ok': False, 'error': '打包未产出 exe', 'stderr': (r.stderr or '')[-1500:]}
            return {'ok': True, 'exe': str(exe_out), 'stderr': (r.stderr or '')[-500:]}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def next_version(self):
        """版本号递增：1.2.3 -> 1.2.4"""
        t = list(version_tuple(self.current_version))
        t[3] += 1
        return '{}.{}.{}.{}'.format(*t)

    # ── 自更新 ──────────────────────────────────────────────
    def apply_self_update(self, pkg_path, version):
        """主控把自己也更新掉（替换 exe 并重启）"""
        try:
            install_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) \
                else self.source_dir
            if not getattr(sys, 'frozen', False):
                # 开发模式：只更新源码，不替换自己
                return {'ok': False, 'skipped': '开发模式，跳过自更新'}
            ok, msg = apply_update(str(pkg_path), install_dir=install_dir, restart=True)
            return {'ok': ok, 'message': msg}
        except Exception as e:
            return {'ok': False, 'error': str(e)}
