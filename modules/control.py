# -*- coding: utf-8 -*-
"""主控 / 子控 通道与逻辑

角色（配置项 role）：
  master  主控：接收子控错误报告 -> 自动用 DSH 生成修复包 -> 分发给所有子控 -> 自己也更新
  agent   子控：可选上报 ZF 错误 / DSH 崩溃 -> 接收更新包 -> 应用并重启

通信介质：共享目录（默认走百度网盘同步盘，跨机器天然可用）
  <channel>/
    reports/<agent_id>/<ts>_<kind>.json    子控写报告
    processed/<report_id>.done             主控标记已处理
    updates/index.json                     主控写：最新更新包指针
    updates/<version>.zip                  主控写：更新包本体
    applied/<agent_id>.json                子控写：已应用到哪个版本

也支持局域网 HTTP 直连（master_url 非空时主控会起一个最小 HTTP 服务）。
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from logger import ZeroFallLogger
except Exception:                                    # 独立脚本/测试环境下兜底
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)

    class ZeroFallLogger:                            # noqa: D401
        @staticmethod
        def info(m):
            _logging.info(m)

        @staticmethod
        def warning(m):
            _logging.warning(m)

        @staticmethod
        def error(m):
            _logging.error(m)

ZERO_FALL_AGENT_PREFIX = 'zerofall-ctl'


# ════════════════════════════════════════════════════════════════
#  通道
# ════════════════════════════════════════════════════════════════
class ControlChannel:
    """共享目录通道"""

    def __init__(self, root):
        self.root = Path(root)
        self.reports_dir = self.root / 'reports'
        self.processed_dir = self.root / 'processed'
        self.updates_dir = self.root / 'updates'
        self.applied_dir = self.root / 'applied'

    def ensure(self):
        for d in (self.root, self.reports_dir, self.processed_dir,
                  self.updates_dir, self.applied_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    def available(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / '.probe'
            probe.write_text('1', encoding='utf-8')
            probe.unlink()
            return True
        except Exception:
            return False

    # ── 报告 ────────────────────────────────────────────────
    def write_report(self, agent_id, kind, payload):
        """子控上报。kind: zf_error / dsh_crash / heartbeat"""
        self.ensure()
        folder = self.reports_dir / agent_id
        folder.mkdir(parents=True, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        rid = '{}_{}_{}'.format(ts, kind, uuid.uuid4().hex[:6])
        doc = {
            'report_id': rid,
            'agent_id': agent_id,
            'kind': kind,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'hostname': socket.gethostname(),
            'platform': platform.platform(),
            'zerofall_version': payload.pop('zerofall_version', ''),
            'payload': payload,
        }
        path = folder / (rid + '.json')
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        return rid, path

    # ── 报告认领（防多主控重复修复同一份）──────────────
    def claim_report(self, rid, owner, ttl_seconds=1800):
        """原子认领一份报告

        为什么必须做：主控修复一份报告要跑 ACP + 打包，约 60~200 秒。
        这期间报告还没被标记 .done，如果同一通道上有第二个主控实例
        （比如换了台机器、或误开了两个），它会捡到同一份报告再修一遍 ——
        白跑几分钟，还会重复发版覆盖前一个包。

        用 O_EXCL 原子创建，跨进程跨机器都有效。
        过期时间兜底：认领者崩溃后 30 分钟自动释放，不会永久卡住。
        """
        try:
            d = self.root / 'claims'
            d.mkdir(parents=True, exist_ok=True)
            p = d / (rid + '.claim')
            now = time.time()
            payload = json.dumps({
                'owner': owner,
                'pid': os.getpid(),
                'host': socket.gethostname(),
                'at': now,
                'at_str': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False).encode('utf-8')
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                # 已有人认领：看是否过期
                try:
                    cur = json.loads(p.read_text(encoding='utf-8'))
                    age = now - float(cur.get('at', 0) or 0)
                    if age > ttl_seconds or cur.get('owner') == owner:
                        p.write_bytes(payload)      # 过期/自己重入 → 接管
                        return True
                except Exception:
                    p.write_bytes(payload)
                    return True
                return False
        except Exception:
            return True                              # 认领机制本身出错不能堵死流程

    def release_claim(self, rid):
        try:
            p = self.root / 'claims' / (rid + '.claim')
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def cleanup_claims(self, ttl_seconds=1800):
        """清掉过期认领（主控崩溃后残留）"""
        n = 0
        try:
            d = self.root / 'claims'
            if not d.exists():
                return 0
            now = time.time()
            for f in d.glob('*.claim'):
                try:
                    if now - f.stat().st_mtime > ttl_seconds:
                        f.unlink()
                        n += 1
                except Exception:
                    pass
        except Exception:
            pass
        return n

    # ── 子控存活记录 ────────────────────────────────────
    def record_agent(self, agent_id, info):
        """记录子控最后活跃时间（心跳落这里，不占报告队列）"""
        try:
            d = self.root / 'agents'
            d.mkdir(parents=True, exist_ok=True)
            doc = dict(info or {})
            doc['agent_id'] = agent_id
            doc['last_seen'] = time.strftime('%Y-%m-%d %H:%M:%S')
            doc['last_seen_ts'] = time.time()
            (d / (agent_id + '.json')).write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
            return True
        except Exception:
            return False

    def agents_seen(self, alive_seconds=900):
        """返回已知子控及在线状态"""
        out = []
        try:
            d = self.root / 'agents'
            if not d.exists():
                return out
            now = time.time()
            for f in d.glob('*.json'):
                try:
                    doc = json.loads(f.read_text(encoding='utf-8'))
                except Exception:
                    continue
                ts = float(doc.get('last_seen_ts', 0) or 0)
                doc['alive'] = (now - ts) < alive_seconds
                doc['idle_seconds'] = int(now - ts)
                out.append(doc)
        except Exception:
            pass
        return sorted(out, key=lambda x: x.get('last_seen_ts', 0), reverse=True)

    def list_reports(self, only_new=True):
        """主控读取未处理的报告"""
        self.ensure()
        out = []
        if not self.reports_dir.exists():
            return out
        for agent_dir in self.reports_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            for f in agent_dir.glob('*.json'):
                if only_new and (self.processed_dir / (f.stem + '.done')).exists():
                    continue
                try:
                    doc = json.loads(f.read_text(encoding='utf-8'))
                    out.append((f, doc))
                except Exception:
                    continue
        out.sort(key=lambda x: x[1].get('created_at', ''))
        return out

    def mark_processed(self, report_path, result=None):
        rid = Path(report_path).stem
        try:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
            (self.processed_dir / (rid + '.done')).write_text(
                json.dumps(result or {'ok': True, 'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                           ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    # ── 重试计数（失败的报告不能直接标记已处理，否则一次抖动就永久丢掉）──
    def _attempt_file(self, rid):
        return self.root / 'attempts' / (rid + '.json')

    def get_attempt(self, rid):
        """返回 (次数, 上次尝试时间戳)"""
        try:
            p = self._attempt_file(rid)
            if p.exists():
                d = json.loads(p.read_text(encoding='utf-8'))
                return int(d.get('count', 0)), float(d.get('last_at', 0))
        except Exception:
            pass
        return 0, 0.0

    def bump_attempt(self, rid):
        """记录一次失败尝试，返回累计次数"""
        count, _ = self.get_attempt(rid)
        count += 1
        try:
            p = self._attempt_file(rid)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                'count': count,
                'last_at': time.time(),
                'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
        return count

    def clear_attempt(self, rid):
        try:
            p = self._attempt_file(rid)
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def should_retry(self, rid, min_interval_seconds=600, max_attempts=3):
        """判断一条失败的报告现在该不该再试

        返回 (是否重试, 已试次数, 还需等待的秒数)
        """
        count, last_at = self.get_attempt(rid)
        if count >= max_attempts:
            return False, count, 0
        waited = time.time() - last_at
        if last_at and waited < min_interval_seconds:
            return False, count, int(min_interval_seconds - waited)
        return True, count, 0

    def retry_later(self, rid, result=None):
        """记录一次失败但保留报告（不写 .done，下轮还会读到）"""
        try:
            d = self.root / 'retry'
            d.mkdir(parents=True, exist_ok=True)
            (d / (rid + '.json')).write_text(
                json.dumps(result or {'ok': False},
                           ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    # ── 更新 ────────────────────────────────────────────────
    def publish_update(self, version, zip_path, manifest):
        """主控发布更新"""
        self.ensure()
        dest = self.updates_dir / '{}.zip'.format(version)
        try:
            shutil.copy2(zip_path, dest)
        except Exception:
            return False
        idx = {
            'version': version,
            'file': dest.name,
            'size': dest.stat().st_size,
            'sha256': __import__('hashlib').sha256(dest.read_bytes()).hexdigest(),
            'manifest': manifest,
            'published_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        (self.updates_dir / 'index.json').write_text(
            json.dumps(idx, ensure_ascii=False, indent=2), encoding='utf-8')
        return True

    def check_update(self):
        """子控查询是否有新版本"""
        self.ensure()
        idx = self.updates_dir / 'index.json'
        if not idx.exists():
            return None
        try:
            return json.loads(idx.read_text(encoding='utf-8'))
        except Exception:
            return None

    def fetch_update(self, idx):
        """下载更新包到本地缓存"""
        self.ensure()
        src = self.updates_dir / idx.get('file', '')
        if not src.exists():
            return None
        cache = Path(os.environ.get('TEMP', '.')) / 'zerofall_updates'
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / src.name
        shutil.copy2(src, dest)
        return dest

    def mark_applied(self, agent_id, version):
        self.ensure()
        try:
            (self.applied_dir / (agent_id + '.json')).write_text(
                json.dumps({'version': version, 'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                           ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass


class HybridChannel:
    """双通道：HTTP 直连优先，不通时自动退回共享目录，恢复后再切回

    为什么需要：
      跨网络时子控填了主控地址（穿透/Tailscale），一旦网络抖动或穿透掉线，
      纯 HTTP 通道会让子控**静默失联** —— check_update 失败被上层当成
      "暂无更新"，表面上一切正常，实际再也收不到更新包，比不填地址还糟。

    策略：
      * available() 连续 2 次探测失败 → 降级到共享目录（网盘通道）
      * 探测恢复 → 立即切回 HTTP
      * 任何一次操作抛异常 → 当场降级并重试一次，不让异常冒到上层
    """

    def __init__(self, config):
        self._dir = ControlChannel(getattr(config, 'control_channel_dir', ''))
        self._http = None
        self._dav = None
        self._fails = 0
        self.switches = 0
        url = str(getattr(config, 'master_url', '') or '').strip()
        if url:
            try:
                from modules.net_transport import HttpControlChannel
                self._http = HttpControlChannel(
                    url, getattr(config, 'http_token', '') or '')
            except Exception as e:
                ZeroFallLogger.warning(
                    '[子控] HTTP 通道初始化失败，跳过: {}'.format(e))
        # 存储型中转：跨网络的主用通道（HTTP 不可达时靠它）
        # 支持三种后端：WebDAV 网盘 / S3 对象存储 / FTP
        if getattr(config, 'relay_enabled', False):
            try:
                from modules.net_transport import make_relay
                self._dav = make_relay(config)
                if self._dav is None:
                    ZeroFallLogger.warning(
                        '[子控] 中转已启用但参数不完整（kind={}），跳过'.format(
                            getattr(config, 'relay_kind', 'webdav')))
            except Exception as e:
                ZeroFallLogger.warning(
                    '[子控] 中转初始化失败，跳过: {}'.format(e))
        # 优先级：HTTP 直连（最快）> WebDAV 中转（跨网络）> 共享目录（兜底）
        if self._http:
            self.active = 'http'
        elif self._dav:
            self.active = 'dav'
        else:
            self.active = 'dir'

    # ── 对外表现 ──
    @property
    def root(self):
        return getattr(self._dir, 'root', '')

    @property
    def master_url(self):
        return getattr(self._http, 'base', '') if self._http else ''

    def ensure(self):
        try:
            self._dir.ensure()
        except Exception:
            pass

    def _http_alive(self):
        if not self._http:
            return False
        try:
            return bool(self._http.available())
        except Exception:
            return False

    def _dav_alive(self):
        if not self._dav:
            return False
        try:
            return bool(self._dav.available())
        except Exception:
            return False

    def available(self):
        # 1) 有 HTTP 先探 HTTP
        if self._http:
            if self._http_alive():
                if self.active != 'http':
                    self.active = 'http'
                    self.switches += 1
                    ZeroFallLogger.info('[子控] HTTP 通道恢复，已切回直连')
                self._fails = 0
                return True
            self._fails += 1
            # 2) HTTP 不通 → 试 WebDAV 中转
            if self._dav and self._dav_alive():
                if self.active != 'dav':
                    self.active = 'dav'
                    self.switches += 1
                    ZeroFallLogger.warning(
                        '[子控] HTTP 不可达，切换到 WebDAV 中转通道')
                return True
            if self._fails >= 2 and self.active != 'dir':
                self.active = 'dir'
                self.switches += 1
                ZeroFallLogger.warning(
                    '[子控] HTTP/WebDAV 均不通（连续 {} 次），降级到共享目录'.format(
                        self._fails))
                return self._dir.available()
            # 还没到降级阈值：保持当前策略，返回目录可用性
            return self._dir.available()

        # 没有 HTTP：WebDAV 优先于共享目录
        if self._dav:
            if self._dav_alive():
                if self.active != 'dav':
                    self.active = 'dav'
                    self.switches += 1
                    ZeroFallLogger.info('[子控] 使用 WebDAV 中转通道')
                return True
            self._fails += 1
            if self._fails >= 2 and self.active != 'dir':
                self.active = 'dir'
                self.switches += 1
                ZeroFallLogger.warning(
                    '[子控] WebDAV 不通（连续 {} 次），降级到共享目录'.format(self._fails))
            return self._dir.available()

        return self._dir.available()

    def _cur(self):
        if self.active == 'http' and self._http:
            return self._http
        if self.active == 'dav' and self._dav:
            return self._dav
        return self._dir

    def _call(self, name, *args, **kwargs):
        ch = self._cur()
        try:
            return getattr(ch, name)(*args, **kwargs)
        except Exception as e:
            # HTTP 挂了 → 先试 WebDAV，再退目录
            if ch is self._http and self._dav:
                self.active = 'dav'
                self.switches += 1
                ZeroFallLogger.warning(
                    '[子控] HTTP 调用 {} 异常，改走 WebDAV 中转: {}'.format(name, e))
                try:
                    return getattr(self._dav, name)(*args, **kwargs)
                except Exception as e2:
                    self.active = 'dir'
                    ZeroFallLogger.warning(
                        '[子控] WebDAV 也失败，改走共享目录: {}'.format(e2))
                    return getattr(self._dir, name)(*args, **kwargs)
            if ch is self._dav:
                self.active = 'dir'
                self._fails = 99
                self.switches += 1
                ZeroFallLogger.warning(
                    '[子控] WebDAV 调用 {} 异常，改走共享目录: {}'.format(name, e))
                return getattr(self._dir, name)(*args, **kwargs)
            raise

    # ── 代理到当前活跃通道 ──
    def write_report(self, *a, **kw):
        return self._call('write_report', *a, **kw)

    def check_update(self, *a, **kw):
        return self._call('check_update', *a, **kw)

    def fetch_update(self, *a, **kw):
        return self._call('fetch_update', *a, **kw)

    def mark_applied(self, *a, **kw):
        return self._call('mark_applied', *a, **kw)

    def list_reports(self, *a, **kw):
        return self._dir.list_reports(*a, **kw)

    def mark_processed(self, *a, **kw):
        return self._dir.mark_processed(*a, **kw)

    def publish_update(self, *a, **kw):
        return self._dir.publish_update(*a, **kw)

    def publish_relay(self, version, zip_path, manifest):
        """把更新包也发到 WebDAV 中转（主控跨网络分发用）"""
        if not self._dav:
            return False
        return self._dav.publish_update(version, zip_path, manifest)

    @property
    def relay(self):
        return self._dav

    def get_attempt(self, *a, **kw):
        return self._dir.get_attempt(*a, **kw)

    def bump_attempt(self, *a, **kw):
        return self._dir.bump_attempt(*a, **kw)

    def clear_attempt(self, *a, **kw):
        return self._dir.clear_attempt(*a, **kw)

    def should_retry(self, *a, **kw):
        return self._dir.should_retry(*a, **kw)

    def retry_later(self, *a, **kw):
        return self._dir.retry_later(*a, **kw)


def make_agent_channel(config):
    """按配置决定子控用哪种传输

    master_url 非空 -> 优先 HTTP 直连主控内建更新服务器（推荐，零第三方账号）
    master_url 为空 -> 只用共享目录（网盘同步盘 / 局域网共享 / NAS 都行）

    两种情况都返回 HybridChannel，好处是运行期能自动降级/恢复，
    不会因为穿透掉线而彻底失联。
    """
    return HybridChannel(config)


def version_tuple(v):
    """'1.2.0' -> (1,2,0)"""
    parts = []
    for p in str(v or '0').replace('-', '.').split('.'):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


# ════════════════════════════════════════════════════════════════
#  子控
# ════════════════════════════════════════════════════════════════
class AgentNode:
    """子控行为"""

    def __init__(self, config, snapshot_manager=None, guardian=None):
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.guardian = guardian
        self.agent_id = getattr(config, 'agent_id', '') or self.default_agent_id()
        # 按配置选传输：master_url 非空走 HTTP 直连，否则退回共享目录
        self.channel = make_agent_channel(config)
        self.current_version = self.read_current_version()
        self._last_reported = {}

    @staticmethod
    def default_agent_id():
        return '{}-{}'.format(socket.gethostname(), uuid.uuid4().hex[:4])

    def read_current_version(self):
        from modules.updater import read_version_from_exe
        try:
            if getattr(sys, 'frozen', False):
                return read_version_from_exe(sys.executable)
        except Exception:
            pass
        return getattr(self.config, 'app_version', '0.0.0.0')

    # ── 上报 ────────────────────────────────────────────────
    def send_heartbeat(self):
        """向主控报到一次，让主控知道这台子控还活着、跑的是哪个版本

        不走 report()：心跳和"报错"是两件事，报错受 report_to_master
        开关约束，而存活状态应该始终可见（主控靠它判断子控是否掉线）。
        """
        try:
            from modules.updater import read_version_from_exe     # noqa: F401
        except Exception:
            pass
        info = {
            'zerofall_version': self.current_version,
            'role': 'agent',
            'http_active': getattr(self.channel, 'active', 'dir'),
            'transport': getattr(self.channel, 'active', 'dir'),
        }
        try:
            self.channel.write_report(self.agent_id, 'heartbeat', dict(info))
            return True
        except Exception:
            return False

    # 同类上报的限流窗口（秒）。
    #   zf_error  ：10 分钟，同类报错容易成串刷出来，限流有意义
    #   dsh_crash ：0，靠崩溃事件序号去重就够了。限流在这里只会
    #               把真实崩溃的上报往后拖最多 10 分钟，反而有害
    REPORT_RATE_LIMIT = {'zf_error': 600, 'dsh_crash': 0}

    def report(self, kind, payload):
        """上报一条错误/崩溃。开关由配置控制"""
        if not getattr(self.config, 'report_to_master', True):
            return None
        if kind == 'zf_error' and not getattr(self.config, 'report_zf_error', True):
            return None
        if kind == 'dsh_crash' and not getattr(self.config, 'report_dsh_crash', True):
            return None

        # 限流：只有窗口 > 0 的类型才生效
        window = self.REPORT_RATE_LIMIT.get(kind, 600)
        key = kind + ':' + str(payload.get('signature', ''))[:80]
        now = time.time()
        if window > 0 and key in self._last_reported \
                and now - self._last_reported[key] < window:
            return None
        self._last_reported[key] = now

        payload['zerofall_version'] = self.current_version
        try:
            return self.channel.write_report(self.agent_id, kind, payload)[0]
        except Exception:
            return None

    def report_zf_error(self, error_text, context=None):
        return self.report('zf_error', {
            'signature': str(error_text)[:200],
            'error': str(error_text)[:4000],
            'context': context or {},
        })

    def report_dsh_crash(self, exit_code, log_tail=None):
        return self.report('dsh_crash', {
            'signature': 'crash:{}'.format(exit_code),
            'exit_code': exit_code,
            'log_tail': (log_tail or '')[-4000:],
        })

    # ── 更新 ────────────────────────────────────────────────
    def poll_update(self, install_dir=None, restart=True):
        """检查并应用新版本。返回 (applied:bool, message:str)

        install_dir 默认 None = 当前 exe 所在目录（生产行为）；
        测试时可指向沙箱目录，避免误伤真实安装。
        """
        if not getattr(self.config, 'auto_apply_update', True):
            return False, '自动更新已关闭'
        idx = self.channel.check_update()
        if not idx:
            return False, '暂无更新'
        new_v = idx.get('version', '0')
        if version_tuple(new_v) <= version_tuple(self.current_version):
            return False, '已是最新 v{}'.format(self.current_version)
        local = self.channel.fetch_update(idx)
        if not local:
            return False, '更新包下载失败'
        from modules.updater import apply_update
        ok, msg = apply_update(str(local), install_dir=install_dir, restart=restart)
        if ok:
            self.current_version = new_v
            self.channel.mark_applied(self.agent_id, new_v)
        return ok, msg

    def check_dsh_crash(self):
        """检查 DSH 是否崩溃退出，是则上报 —— 同一个崩溃只上报一次

        原实现只要状态里躺着 last_exit_crash 就上报，而本方法每轮心跳都会
        被调用，于是同一个崩溃会被反复报到主控，主控每次都去建会话修同一个
        问题，白白烧额度。

        现在用崩溃事件序号去重：
          · 每崩一次，record_exit() 把 crash_seq +1
          · 上报成功后把 crash_reported_seq 记到该序号
          · 只有 crash_seq > crash_reported_seq 才上报
        序号存在状态文件里，所以重启进程也不会重复上报；
        而上报失败（返回 None）时不记账，下一轮还能重试。
        """
        try:
            from modules.environment_checker import EnvironmentChecker
            checker = EnvironmentChecker(self.config, self.snapshot_manager)
            pending = checker.pending_crash_report()
            if not pending:
                return None
            exit_code, seq = pending
            rid = self.report_dsh_crash(exit_code)
            if rid:
                checker.mark_crash_reported(seq)
                ZeroFallLogger.info(
                    "[崩溃上报] 已上报崩溃事件 #{}（退出码 {}）".format(seq, exit_code))
            else:
                ZeroFallLogger.info(
                    "[崩溃上报] 事件 #{} 本次未发出（开关关闭或被限流），下轮重试".format(seq))
            return rid
        except Exception as e:
            ZeroFallLogger.warning("[崩溃上报] 检查失败: {}".format(e))
        return None

