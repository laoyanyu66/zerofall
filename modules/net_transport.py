# -*- coding: utf-8 -*-
"""ZeroFall HTTP 更新投递（官方 App 式的「更新清单」机制）

设计参照 Chrome / VS Code / Slack 等桌面应用的自动更新：
  1. 客户端定时拉一个更新清单（manifest / feed）
  2. 比对版本号，有新版才下载
  3. 下载后校验 sha256，再替换自身

本模块让**主控 ZeroFall 自己充当更新服务器**，不需要第三方账号、不需要
公网服务器、不需要共享网盘。子控只需要在设置里填一个地址，例如：

    http://192.168.1.10:8765

跨网络用法：在两台机器上装 Tailscale / ZeroTier 之类的组网工具，
然后填对方的内网地址即可（相当于自带内网穿透）。

接口（全部挂在 /zf 前缀下）
    GET  /zf/health              健康检查，返回主控版本与角色
    GET  /zf/index.json          更新清单（等价通道目录里的 updates/index.json）
    GET  /zf/updates/<name>      更新包本体
    POST /zf/report              子控上报（写入 reports/）
    POST /zf/applied             子控应用回执（写入 applied/）
    GET  /zf/pending?agent_id=X  子控查询自己已应用版本（可选，排障用）

鉴权：配置了 http_token 时，所有请求必须带 X-ZF-Token 头。
安全：只读静态段做了路径穿越防护；POST 段限制长度。
"""
import hashlib
import hmac
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

API_PREFIX = '/zf'
MAX_UPLOAD_BYTES = 2 * 1024 * 1024      # 上报正文上限 2 MB
CHUNK = 256 * 1024


def _log(msg):
    try:
        from logger import ZeroFallLogger
        ZeroFallLogger.info(msg)
    except Exception:
        pass


def _log_debug(msg):
    try:
        from logger import ZeroFallLogger
        ZeroFallLogger.debug(msg)
    except Exception:
        pass


def _log_warn(msg):
    try:
        from logger import ZeroFallLogger
        ZeroFallLogger.warning(msg)
    except Exception:
        pass


def local_ip_list():
    """本机所有可用的局域网 IPv4（用于把地址显示给用户）"""
    ips = []
    try:
        # 主出口 IP（不会真的发包）
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith('127.'):
                ips.append(ip)
    except Exception:
        pass
    return ips


# ════════════════════════════════════════════════════════════════
#  主控侧：内建 HTTP 服务
# ════════════════════════════════════════════════════════════════
class MasterHttpServer:
    """把通道目录以只读 HTTP 方式暴露给子控（另带两个 POST 上报端点）"""

    def __init__(self, channel_root, port=8765, token='', bind='0.0.0.0'):
        self.root = Path(channel_root)
        self.port = int(port)
        self.token = token or ''
        self.bind = bind
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()

    # ── 生命周期 ────────────────────────────────────────────
    def start(self):
        if self._httpd is not None:
            return True
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            # 静默默认的 stderr 访问日志
            def log_message(self, fmt, *args):
                _log_debug('[HTTP] ' + (fmt % args))

            # ── 鉴权 ────────────────────────────────────────
            def _auth_ok(self):
                if not server.token:
                    return True
                return self.headers.get('X-ZF-Token', '') == server.token

            def _deny(self, code=401, msg='unauthorized'):
                body = json.dumps({'ok': False, 'error': msg}).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            # ── GET ─────────────────────────────────────────
            def do_GET(self):
                if not self._auth_ok():
                    return self._deny()

                parsed = urlparse(self.path)
                path = parsed.path
                if not path.startswith(API_PREFIX):
                    return self._deny(404, 'not found')
                rel = path[len(API_PREFIX):].lstrip('/')

                if rel in ('health', 'health/'):
                    return self._json(200, {
                        'ok': True,
                        'role': 'master',
                        'version': server.master_version,
                        'server_time': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'channel': str(server.root),
                    })

                if rel in ('index.json', 'index'):
                    idx_file = server.root / 'updates' / 'index.json'
                    if not idx_file.exists():
                        return self._json(404, {'ok': False, 'error': 'no update published'})
                    return self._send_file(idx_file, 'application/json; charset=utf-8')

                if rel.startswith('updates/'):
                    name = rel[len('updates/'):]
                    return self._send_update(name)

                if rel in ('pending', 'pending/'):
                    qs = parse_qs(parsed.query or '')
                    agent = (qs.get('agent_id') or [''])[0]
                    return self._json(200, server.pending_info(agent))

                return self._deny(404, 'not found')

            # ── POST ────────────────────────────────────────
            def do_POST(self):
                if not self._auth_ok():
                    return self._deny()

                parsed = urlparse(self.path)
                rel = parsed.path[len(API_PREFIX):].lstrip('/') if parsed.path.startswith(API_PREFIX) else ''
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_UPLOAD_BYTES:
                    return self._deny(413, 'bad body length')
                raw = self.rfile.read(length)
                try:
                    doc = json.loads(raw.decode('utf-8'))
                except Exception:
                    return self._deny(400, 'bad json')

                if rel == 'report':
                    rid = server.ingest_report(doc)
                    return self._json(200, {'ok': True, 'report_id': rid})

                if rel == 'applied':
                    server.ingest_applied(doc)
                    return self._json(200, {'ok': True})

                return self._deny(404, 'not found')

            # ── 工具 ────────────────────────────────────────
            def _send_file(self, path: Path, ctype='application/octet-stream'):
                try:
                    size = path.stat().st_size
                except Exception:
                    return self._deny(404, 'not found')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(size))
                self.send_header('Accept-Ranges', 'none')
                self.end_headers()
                try:
                    with open(path, 'rb') as f:
                        while True:
                            chunk = f.read(CHUNK)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except Exception:
                    pass

            def _send_update(self, name):
                # 防路径穿越
                if not name or '/' in name or '\\' in name or '..' in name:
                    return self._deny(400, 'bad name')
                if not name.startswith('snapshot') and not name[0].isdigit() and not name.startswith('ZeroFall'):
                    # 允许 1.0.0.1.zip 这种版本名
                    pass
                target = server.root / 'updates' / name
                if not target.is_file():
                    return self._deny(404, 'not found')
                return self._send_file(target)

        try:
            self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        except OSError as e:
            _log_warn('[HTTP更新服务] 端口 {} 绑定失败: {}'.format(self.port, e))
            self._httpd = None
            return False

        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name='ZeroFall-HttpServer')
        self._thread.start()

        for ip in local_ip_list()[:3]:
            _log('[HTTP更新服务] 子控可填地址: http://{}:{}{}'.format(ip, self.port, API_PREFIX))
        _log('[HTTP更新服务] 已监听 {}:{} | 鉴权: {}'.format(
            self.bind, self.port, '开启' if self.token else '关闭'))
        return True

    def stop(self):
        with self._lock:
            if self._httpd is not None:
                try:
                    self._httpd.shutdown()
                    self._httpd.server_close()
                except Exception:
                    pass
                self._httpd = None
                _log('[HTTP更新服务] 已停止')

    def is_running(self):
        return self._httpd is not None

    # ── 数据写入（与通道目录同构）──────────────────────────
    master_version = ''

    def ingest_report(self, doc):
        """接收子控上报，落成和目录通道一样的文件结构"""
        agent_id = str(doc.get('agent_id') or 'unknown')
        kind = str(doc.get('kind') or 'zf_error')
        ts = time.strftime('%Y%m%d_%H%M%S')
        rid = doc.get('report_id') or '{}_{}_{}'.format(ts, kind, uuid.uuid4().hex[:6])
        doc['report_id'] = rid
        doc.setdefault('received_at', time.strftime('%Y-%m-%d %H:%M:%S'))
        folder = self.root / 'reports' / agent_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (rid + '.json')).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        _log('[HTTP更新服务] 收到上报 {} / {}'.format(agent_id, kind))
        return rid

    def ingest_applied(self, doc):
        agent_id = str(doc.get('agent_id') or 'unknown')
        folder = self.root / 'applied'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (agent_id + '.json')).write_text(
            json.dumps({
                'version': doc.get('version'),
                'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'via': 'http',
            }, ensure_ascii=False, indent=2), encoding='utf-8')

    def pending_info(self, agent_id):
        info = {'ok': True, 'agent_id': agent_id, 'latest': None, 'applied': None}
        idx = self.root / 'updates' / 'index.json'
        if idx.exists():
            try:
                info['latest'] = json.loads(idx.read_text(encoding='utf-8')).get('latest')
            except Exception:
                pass
        if agent_id:
            f = self.root / 'applied' / (agent_id + '.json')
            if f.exists():
                try:
                    info['applied'] = json.loads(f.read_text(encoding='utf-8')).get('version')
                except Exception:
                    pass
        return info


# ════════════════════════════════════════════════════════════════
#  子控侧：HTTP 通道（与 ControlChannel 同接口）
# ════════════════════════════════════════════════════════════════
class WebDavChannel:
    """WebDAV 中转通道：子控零安装、跨网络可用的更新分发

    为什么需要：
      主控在运营商 CGNAT 后面，开端口无效；子控又不能装穿透工具。
      两边都没有公网可达性，直连物理上不可能，必须有一个"双方都能往外连"
      的中转。

    WebDAV 就是这样一个中转：主控把更新包 PUT 上去，子控 GET 下来。
    双方只需出站 HTTPS，不需要任何入站端口、不需要同局域网、不需要装客户端。
    兼容坚果云、Nextcloud、群晖 NAS、各类网盘的 WebDAV 接口。

    目录约定（与共享目录通道完全一致）：
        <root>/updates/index.json     更新清单
        <root>/updates/<file>         更新包本体
        <root>/reports/<id>.json      子控上报
        <root>/applied/<agent>.json   应用回执
    """

    def __init__(self, base_url, user='', password='', root='/zerofall-ctl',
                 timeout=(5, 30)):
        self.base = base_url.rstrip('/')
        self.user = user or ''
        self.password = password or ''
        self.root = (root or '/zerofall-ctl').rstrip('/')
        self.timeout = timeout
        self._made = set()

    # ── 基础 ──
    def _auth(self):
        return (self.user, self.password) if self.user else None

    def _url(self, path=''):
        p = path if path.startswith('/') else '/' + path
        return '{}{}{}'.format(self.base, self.root, p)

    def _mkdirs(self, path):
        """按需 MKCOL 逐级建目录（已存在会返回 405，忽略）"""
        import requests
        d = path.rstrip('/')
        if not d or d in self._made:
            return True
        parts = [x for x in d.split('/') if x]
        cur = ''
        for seg in parts:
            cur += '/' + seg
            if cur in self._made:
                continue
            try:
                u = '{}{}'.format(self.base, cur)
                r = requests.request('MKCOL', u, auth=self._auth(),
                                     timeout=self.timeout)
                if r.status_code in (201, 301, 405):
                    self._made.add(cur)
            except Exception:
                pass
        return True

    def ensure(self):
        self._mkdirs(self.root)
        for sub in ('/updates', '/reports', '/applied'):
            self._mkdirs(self.root + sub)
        return True

    def available(self):
        try:
            import requests
            r = requests.request('PROPFIND', self._url(''),
                                 auth=self._auth(), timeout=self.timeout,
                                 headers={'Depth': '0'})
            return r.status_code in (200, 207, 401, 403, 404)
        except Exception:
            return False

    def _put(self, path, data, content_type='application/octet-stream'):
        import requests
        # 确保目标目录存在（MKCOL 逐级）
        if '/' in path:
            sub = path.rsplit('/', 1)[0]
            self._mkdirs(self.root + '/' + sub)
        else:
            self._mkdirs(self.root)
        u = self._url('/' + path)
        r = requests.put(u, data=data, auth=self._auth(), timeout=self.timeout,
                         headers={'Content-Type': content_type})
        return r.status_code in (200, 201, 204)

    def _get(self, path):
        import requests
        u = self._url('/' + path)
        r = requests.get(u, auth=self._auth(), timeout=self.timeout)
        if r.status_code != 200:
            return None
        return r.content

    # ── 子控：上报 ──
    def write_report(self, agent_id, kind, payload):
        import uuid
        rid = 'report_{}'.format(uuid.uuid4().hex[:12])
        doc = {
            'report_id': rid,
            'agent_id': agent_id,
            'kind': kind,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'payload': payload or {},
        }
        ok = self._put('reports/{}.json'.format(rid),
                       json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'),
                       'application/json')
        if ok:
            _log('[WebDAV] 已上报 {} / {}'.format(agent_id, kind))
        else:
            _log_warn('[WebDAV] 上报失败')
        return ok

    # ── 子控：拉更新 ──
    def check_update(self):
        """返回 dict|None（与其它通道同形）"""
        raw = self._get('updates/index.json')
        if not raw:
            return None
        try:
            d = json.loads(raw.decode('utf-8'))
        except Exception:
            return None
        if not isinstance(d, dict) or not d.get('version'):
            return None
        return d

    def fetch_update(self, idx):
        import hashlib
        import tempfile
        name = idx.get('file') or '{}.zip'.format(idx.get('version'))
        blob = self._get('updates/{}'.format(name))
        if not blob:
            _log_warn('[WebDAV] 下载更新包失败: {}'.format(name))
            return None
        expect = (idx.get('sha256') or '').lower()
        actual = hashlib.sha256(blob).hexdigest()
        if expect and actual != expect:
            _log_warn('[WebDAV] sha256 不匹配，丢弃（期望 {} 实际 {}）'.format(
                expect[:16], actual[:16]))
            return None
        dest = Path(tempfile.gettempdir()) / 'zf_upd_{}'.format(name)
        dest.write_bytes(blob)
        _log('[WebDAV] 已下载 {}（{:.1f} MB）'.format(name, len(blob) / 1048576))
        return str(dest)

    def mark_applied(self, agent_id, version):
        import time as _t
        doc = {'agent_id': agent_id, 'version': version,
               'applied_at': _t.strftime('%Y-%m-%d %H:%M:%S')}
        return self._put('applied/{}.json'.format(agent_id),
                         json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'),
                         'application/json')

    # ── 主控：发布 ──
    def publish_update(self, version, zip_path, manifest):
        import hashlib
        zip_path = Path(zip_path)
        if not zip_path.exists():
            _log_warn('[WebDAV] 更新包不存在: {}'.format(zip_path))
            return False
        blob = zip_path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        name = '{}.zip'.format(version)
        if not self._put('updates/' + name, blob, 'application/zip'):
            return False
        index = {
            'version': version,
            'file': name,
            'size': len(blob),
            'sha256': sha,
            'manifest': manifest or {},
            'published_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        ok = self._put('updates/index.json',
                       json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'),
                       'application/json')
        if ok:
            _log('[WebDAV] 已发布 {}（{:.1f} MB）'.format(version, len(blob) / 1048576))
        return ok


class RelayChannelBase:
    """存储型中转的公共逻辑

    子类只需实现三个动作：put_object / get_object / available。
    其余（上报、清单、下载校验、回执、发布）在这里统一实现，
    保证 WebDAV / S3 / FTP 三种后端对上层完全同形。
    """

    root = '/zerofall-ctl'

    # ── 子类必须实现 ──
    def put_object(self, path, data, content_type='application/octet-stream'):
        raise NotImplementedError

    def get_object(self, path):
        raise NotImplementedError

    def available(self):
        raise NotImplementedError

    def ensure(self):
        return True

    # ── 公共实现 ──
    def write_report(self, agent_id, kind, payload):
        import uuid
        rid = 'report_{}'.format(uuid.uuid4().hex[:12])
        doc = {
            'report_id': rid,
            'agent_id': agent_id,
            'kind': kind,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'payload': payload or {},
        }
        ok = self.put_object(
            'reports/{}.json'.format(rid),
            json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'),
            'application/json')
        if ok:
            _log('[{}] 已上报 {} / {}'.format(self.label(), agent_id, kind))
        else:
            _log_warn('[{}] 上报失败'.format(self.label()))
        return ok

    def check_update(self):
        raw = self.get_object('updates/index.json')
        if not raw:
            return None
        try:
            d = json.loads(raw.decode('utf-8'))
        except Exception:
            return None
        if not isinstance(d, dict) or not d.get('version'):
            return None
        return d

    def fetch_update(self, idx):
        import tempfile
        name = idx.get('file') or '{}.zip'.format(idx.get('version'))
        blob = self.get_object('updates/{}'.format(name))
        if not blob:
            _log_warn('[{}] 下载更新包失败: {}'.format(self.label(), name))
            return None
        expect = (idx.get('sha256') or '').lower()
        actual = hashlib.sha256(blob).hexdigest()
        if expect and actual != expect:
            _log_warn('[{}] sha256 不匹配，丢弃（期望 {} 实际 {}）'.format(
                self.label(), expect[:16], actual[:16]))
            return None
        dest = Path(tempfile.gettempdir()) / 'zf_upd_{}'.format(name)
        dest.write_bytes(blob)
        _log('[{}] 已下载 {}（{:.1f} MB）'.format(
            self.label(), name, len(blob) / 1048576))
        return str(dest)

    def mark_applied(self, agent_id, version):
        doc = {'agent_id': agent_id, 'version': version,
               'applied_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        return self.put_object(
            'applied/{}.json'.format(agent_id),
            json.dumps(doc, ensure_ascii=False, indent=2).encode('utf-8'),
            'application/json')

    def publish_update(self, version, zip_path, manifest):
        zip_path = Path(zip_path)
        if not zip_path.exists():
            _log_warn('[{}] 更新包不存在: {}'.format(self.label(), zip_path))
            return False
        blob = zip_path.read_bytes()
        sha = hashlib.sha256(blob).hexdigest()
        name = '{}.zip'.format(version)
        if not self.put_object('updates/' + name, blob, 'application/zip'):
            return False
        index = {
            'version': version,
            'file': name,
            'size': len(blob),
            'sha256': sha,
            'manifest': manifest or {},
            'published_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        ok = self.put_object(
            'updates/index.json',
            json.dumps(index, ensure_ascii=False, indent=2).encode('utf-8'),
            'application/json')
        if ok:
            _log('[{}] 已发布 {}（{:.1f} MB）'.format(
                self.label(), version, len(blob) / 1048576))
        return ok

    def label(self):
        return self.__class__.__name__


class S3Channel(RelayChannelBase):
    """S3 兼容对象存储中转（Cloudflare R2 / B2 / MinIO / 阿里云 OSS / 腾讯 COS）

    为什么值得支持：这类服务免费用量足够（R2 10GB 且出站免费、B2 10GB），
    完全不是"网盘"，只要一对 AK/SK 就能用，且天生跨网络。

    只用 requests 手写 AWS Signature V4，不引入 boto3（避免 exe 增大）。
    采用 path-style 寻址：<endpoint>/<bucket>/<key>，兼容 MinIO 与 OSS。
    """

    def __init__(self, endpoint, bucket, ak, sk, region='auto', root='zerofall-ctl',
                 timeout=(5, 60)):
        self.endpoint = endpoint.rstrip('/')
        if not self.endpoint.startswith(('http://', 'https://')):
            self.endpoint = 'https://' + self.endpoint
        self.bucket = bucket
        self.ak = ak
        self.sk = sk
        self.region = region or 'auto'
        self.root = (root or 'zerofall-ctl').strip('/')
        self.timeout = timeout

    def label(self):
        return 'S3'

    # ── SigV4 ──
    @staticmethod
    def _sign(key, msg):
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

    def _headers_for(self, method, path, payload):
        import datetime as _dt
        amz_date = _dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        date_stamp = amz_date[:8]
        payload_hash = hashlib.sha256(payload or b'').hexdigest()
        host = self.endpoint.split('://', 1)[-1].split('/')[0]
        # 路径规范化：逐段 URL 编码
        canon_uri = '/' + '/'.join(
            quote(seg, safe='-_.~') for seg in path.split('/') if seg != ''
        ) if path else '/'
        signed_headers = 'host;x-amz-content-sha256;x-amz-date'
        canonical_headers = (
            'host:{}\n'
            'x-amz-content-sha256:{}\n'
            'x-amz-date:{}\n'
        ).format(host, payload_hash, amz_date)
        canonical_request = '\n'.join([
            method, canon_uri, '', canonical_headers, signed_headers, payload_hash,
        ])
        scope = '{}/{}/s3/aws4_request'.format(date_stamp, self.region)
        string_to_sign = '\n'.join([
            'AWS4-HMAC-SHA256', amz_date, scope,
            hashlib.sha256(canonical_request.encode('utf-8')).hexdigest(),
        ])
        k_date = self._sign(('AWS4' + self.sk).encode('utf-8'), date_stamp)
        k_region = self._sign(k_date, self.region)
        k_service = self._sign(k_region, 's3')
        k_signing = self._sign(k_service, 'aws4_request')
        signature = hmac.new(
            k_signing, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
        return {
            'Host': host,
            'x-amz-date': amz_date,
            'x-amz-content-sha256': payload_hash,
            'Authorization': (
                'AWS4-HMAC-SHA256 Credential={}/{}, '
                'SignedHeaders={}, Signature={}'
            ).format(self.ak, scope, signed_headers, signature),
        }

    def _url_for(self, path):
        key = '/'.join(x for x in (self.root, path) if x)
        return '{}/{}/{}'.format(self.endpoint, self.bucket, key)

    def available(self):
        try:
            import requests
            h = self._headers_for('GET', '/{}/'.format(self.bucket), b'')
            r = requests.get('{}/{}/'.format(self.endpoint, self.bucket),
                             headers=h, timeout=self.timeout)
            return r.status_code in (200, 403, 404)   # 403=密钥错但端点通
        except Exception:
            return False

    def put_object(self, path, data, content_type='application/octet-stream'):
        import requests
        url = self._url_for(path)
        key_path = url.split(self.endpoint, 1)[-1]
        h = self._headers_for('PUT', key_path, data)
        h['Content-Type'] = content_type
        try:
            r = requests.put(url, data=data, headers=h, timeout=self.timeout)
            return r.status_code in (200, 201, 204)
        except Exception as e:
            _log_warn('[S3] 上传 {} 失败: {}'.format(path, e))
            return False

    def get_object(self, path):
        import requests
        url = self._url_for(path)
        key_path = url.split(self.endpoint, 1)[-1]
        h = self._headers_for('GET', key_path, b'')
        try:
            r = requests.get(url, headers=h, timeout=self.timeout)
            if r.status_code != 200:
                return None
            return r.content
        except Exception:
            return None


class FtpChannel(RelayChannelBase):
    """FTP / FTPS 中转（只用标准库 ftplib）

    适合有虚拟主机空间或 NAS FTP 的场合 —— 完全不是网盘，
    也不需要任何第三方 SDK。每次操作独立连接，避免长连接被服务端断开。
    """

    def __init__(self, host, user='', password='', port=21, root='/zerofall-ctl',
                 tls=False, timeout=30):
        self.host = host
        self.user = user or 'anonymous'
        self.password = password or ''
        self.port = int(port or 21)
        self.root = (root or '/zerofall-ctl')
        if not self.root.startswith('/'):
            self.root = '/' + self.root
        self.root = self.root.rstrip('/')
        self.tls = bool(tls)
        self.timeout = timeout

    def label(self):
        return 'FTP'

    def _conn(self):
        import ftplib
        if self.tls:
            c = ftplib.FTP_TLS(timeout=self.timeout)
            c.connect(self.host, self.port)
            c.login(self.user, self.password)
            c.prot_p()
        else:
            c = ftplib.FTP(timeout=self.timeout)
            c.connect(self.host, self.port)
            c.login(self.user, self.password)
        return c

    def _mkdirs(self, c, path):
        cur = ''
        for seg in [x for x in path.split('/') if x]:
            cur += '/' + seg
            try:
                c.mkd(cur)
            except Exception:
                pass

    def available(self):
        try:
            c = self._conn()
            c.quit()
            return True
        except Exception:
            return False

    def ensure(self):
        try:
            c = self._conn()
            self._mkdirs(c, self.root + '/updates')
            self._mkdirs(c, self.root + '/reports')
            self._mkdirs(c, self.root + '/applied')
            c.quit()
        except Exception:
            pass
        return True

    def put_object(self, path, data, content_type='application/octet-stream'):
        import io
        try:
            c = self._conn()
            full = '{}/{}'.format(self.root, path)
            self._mkdirs(c, full.rsplit('/', 1)[0])
            c.storbinary('STOR ' + full, io.BytesIO(data))
            c.quit()
            return True
        except Exception as e:
            _log_warn('[FTP] 上传 {} 失败: {}'.format(path, e))
            return False

    def get_object(self, path):
        import io
        try:
            c = self._conn()
            buf = io.BytesIO()
            c.retrbinary('RETR {}/{}'.format(self.root, path), buf.write)
            c.quit()
            return buf.getvalue()
        except Exception:
            return None


def make_relay(config):
    """按 relay_kind 构造中转通道；未启用返回 None

    relay_kind: webdav / s3 / ftp

    子控角色若填了专用凭据（relay_agent_* / s3_agent_* / ftp_agent_*），
    就优先用它 —— 存储位置仍是同一个，但子控只拿到自己那套权限。
    这样即使子控机器被翻看，也拿不到能改更新包的写权限。
    """
    if not getattr(config, 'relay_enabled', False):
        return None
    kind = str(getattr(config, 'relay_kind', 'webdav') or 'webdav').lower()
    is_agent = str(getattr(config, 'role', 'master') or 'master').lower() == 'agent'

    def pick_pair(shared_u, shared_p, agent_u, agent_p):
        """凭据成对取，绝不混搭

        错配（子控账号 + 主控密码）会认证失败且极难排查，
        所以只要填了子控账号，就整对用子控的（密码为空也照用）。
        """
        if is_agent:
            au = str(getattr(config, agent_u, '') or '').strip()
            if au:
                return au, str(getattr(config, agent_p, '') or '')
        return (str(getattr(config, shared_u, '') or '').strip(),
                str(getattr(config, shared_p, '') or ''))

    try:
        if kind == 's3':
            ep = str(getattr(config, 's3_endpoint', '') or '').strip()
            bk = str(getattr(config, 's3_bucket', '') or '').strip()
            if not ep or not bk:
                return None
            _ak, _sk = pick_pair('s3_access_key', 's3_secret_key',
                                 's3_agent_access_key', 's3_agent_secret_key')
            return S3Channel(
                ep, bk, _ak, _sk,
                getattr(config, 's3_region', 'auto') or 'auto',
                getattr(config, 'relay_root', 'zerofall-ctl') or 'zerofall-ctl',
            )
        if kind == 'ftp':
            host = str(getattr(config, 'ftp_host', '') or '').strip()
            if not host:
                return None
            _fu, _fp = pick_pair('ftp_user', 'ftp_password',
                                 'ftp_agent_user', 'ftp_agent_password')
            return FtpChannel(
                host, _fu, _fp,
                getattr(config, 'ftp_port', 21) or 21,
                getattr(config, 'relay_root', '/zerofall-ctl') or '/zerofall-ctl',
                getattr(config, 'ftp_tls', False),
            )
        # 默认 webdav
        url = str(getattr(config, 'relay_url', '') or '').strip()
        if not url:
            return None
        _ru, _rp = pick_pair('relay_user', 'relay_password',
                             'relay_agent_user', 'relay_agent_password')
        return WebDavChannel(
            url, _ru, _rp,
            getattr(config, 'relay_root', '/zerofall-ctl') or '/zerofall-ctl',
        )
    except Exception as e:
        _log_warn('[中转] 构造失败: {}'.format(e))
        return None


class HttpControlChannel:
    """通过 HTTP 从主控拉更新、上报错误

    只实现 AgentNode 用到的方法，接口与 ControlChannel 保持一致，
    这样 AgentNode 不用改一行代码就能切换传输方式。
    """

    def __init__(self, master_url, token='', timeout=(3, 10)):
        url = (master_url or '').strip().rstrip('/')
        # 允许用户只填 IP:端口
        if url and not url.startswith(('http://', 'https://')):
            url = 'http://' + url
        # 自动补 /zf
        if url and not url.endswith(API_PREFIX):
            url = url + API_PREFIX
        self.base = url
        self.root = Path(url)          # 兼容 MasterNode 里 channel.root 的用法
        self.token = token or ''
        self.timeout = timeout
        self._session = None

    # ── 兼容 ControlChannel 的空实现 ────────────────────────
    def ensure(self):
        return True

    def available(self):
        return bool(self.base) and self._probe()

    def _probe(self):
        try:
            r = self._request('GET', '/health')
            return bool(r and r.get('ok'))
        except Exception:
            return False

    # ── 底层请求 ────────────────────────────────────────────
    def _headers(self):
        h = {'User-Agent': 'ZeroFall-Agent/1.5.1'}
        if self.token:
            h['X-ZF-Token'] = self.token
        return h

    def _request(self, method, path, body=None, timeout=None):
        import requests
        url = self.base + path
        # timeout 支持 (connect, read) 元组；旧的标量值也兼容
        kw = dict(timeout=timeout or self.timeout, headers=self._headers())
        if body is not None:
            kw['data'] = json.dumps(body, ensure_ascii=False).encode('utf-8')
            kw['headers'] = dict(kw['headers'])
            kw['headers']['Content-Type'] = 'application/json; charset=utf-8'
        if method == 'GET':
            resp = requests.get(url, **kw)
        else:
            resp = requests.post(url, **kw)
        resp.raise_for_status()
        return resp.json()

    # ── 上报 ────────────────────────────────────────────────
    def write_report(self, agent_id, kind, payload):
        import platform
        doc = {
            'agent_id': agent_id,
            'kind': kind,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'hostname': socket.gethostname(),
            'platform': platform.platform(),
            'zerofall_version': (payload or {}).pop('zerofall_version', ''),
            'payload': payload or {},
        }
        r = self._request('POST', '/report', doc)
        rid = (r or {}).get('report_id') or 'http-unknown'
        return rid, self.base + '/report'

    def mark_applied(self, agent_id, version):
        try:
            self._request('POST', '/applied', {'agent_id': agent_id, 'version': version})
        except Exception:
            pass

    # ── 更新 ────────────────────────────────────────────────
    def check_update(self):
        """子控查询是否有新版本，返回 dict 或 None

        必须与 ControlChannel.check_update() 同形（dict|None）：
        AgentNode.poll_update 用 idx.get('version') 取值，
        若这里返回 tuple 会在 .get 上直接崩掉。
        """
        idx, _info = self.check_update_ex()
        return idx

    def check_update_ex(self):
        """返回 (info_dict|None, error_str)

        主控发布的 index.json 结构：
            {version, file, size, sha256, manifest, published_at}
        """
        try:
            raw = self._request('GET', '/index.json')
        except Exception as e:
            return None, '拉取更新清单失败: {}'.format(e)
        if not isinstance(raw, dict) or not raw.get('version'):
            return None, '主控还没发布过更新包'
        latest = str(raw.get('version'))
        manifest = raw.get('manifest') or {}
        return {
            'latest': latest,
            'version': latest,
            'file': raw.get('file') or '{}.zip'.format(latest),
            'size': raw.get('size') or 0,
            'sha256': (raw.get('sha256') or '').lower(),
            'manifest': manifest,
            'notes': manifest.get('notes', ''),
        }, ''

    def fetch_update(self, idx):
        """下载更新包到临时文件并校验 sha256，返回本地路径"""
        latest = idx.get('latest') or idx.get('version')
        if not latest:
            return None
        expect = (idx.get('sha256') or '').lower()
        name = idx.get('file') or '{}.zip'.format(latest)
        import requests
        url = '{}/updates/{}'.format(self.base, name)
        tmp = Path(__import__('tempfile').gettempdir()) / 'zf_upd_{}.zip'.format(latest)
        h = 0
        sha = hashlib.sha256()
        try:
            with requests.get(url, headers=self._headers(), timeout=self.timeout, stream=True) as r:
                r.raise_for_status()
                total = int(r.headers.get('Content-Length') or 0)
                with open(tmp, 'wb') as f:
                    for chunk in r.iter_content(CHUNK):
                        if not chunk:
                            continue
                        f.write(chunk)
                        sha.update(chunk)
                        h += len(chunk)
            if expect and sha.hexdigest().lower() != expect:
                try:
                    tmp.unlink()
                except Exception:
                    pass
                _log_warn('[HTTP更新] sha256 不匹配，丢弃（期望 {} 实际 {}）'.format(
                    expect[:16], sha.hexdigest()[:16]))
                return None
            _log('[HTTP更新] 已下载 {}（{:.1f} MB）'.format(name, h / 1048576))
            return str(tmp)
        except Exception as e:
            _log_warn('[HTTP更新] 下载失败: {}'.format(e))
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return None
