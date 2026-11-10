# -*- coding: utf-8 -*-
"""S3 / FTP 中转通道测试（都不需要网盘）

S3：自建迷你 S3 兼容服务，校验 SigV4 签名头结构，验证上传/下载往返。
FTP：用 ftplib 桩替代真实 FTP 服务，验证连接参数、目录创建、读写流向。
"""
import hashlib
import json
import re
import shutil
import socket
import os
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


class MiniS3:
    """够用的 S3 兼容端点：PUT/GET 对象，校验 Authorization 头格式"""

    def __init__(self, root: Path, ak, sk):
        self.root = root
        self.ak = ak
        self.sk = sk
        self.seen_auth = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _check(self):
                a = self.headers.get('Authorization', '')
                outer.seen_auth.append(a)
                if not a.startswith('AWS4-HMAC-SHA256 '):
                    return False
                if 'Credential={}/'.format(outer.ak) not in a:
                    return False
                # 必须带这三项
                for need in ('x-amz-content-sha256', 'x-amz-date',
                             'host;x-amz-content-sha256;x-amz-date'):
                    if need not in self.headers and need not in a:
                        return False
                return True

            def _path(self):
                return outer.root / self.path.lstrip('/')

            def _fail(self, code=403):
                self.send_response(code)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_PUT(self):
                if not self._check():
                    return self._fail()
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n)
                p = self._path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                if not self._check():
                    return self._fail()
                p = self._path()
                if not p.is_file():
                    return self._fail(404)
                data = p.read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._handler = H
        self._httpd = None

    def start(self, port):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', port), self._handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_s3():
    print('=== 1. S3 兼容对象存储 ===')
    from modules.net_transport import S3Channel

    sand = Path(tempfile.mkdtemp(prefix='zf_s3_'))
    s3root = sand / 's3'
    s3root.mkdir()
    port = free_port()
    srv = MiniS3(s3root, 'AKIDTEST', 'SECRETTEST')
    srv.start(port)
    time.sleep(0.2)

    ep = 'http://127.0.0.1:{}'.format(port)
    c = S3Channel(ep, 'zf-bucket', 'AKIDTEST', 'SECRETTEST', 'auto', 'zerofall-ctl')

    chk(c.available() is True, '端点可达')
    chk(srv.seen_auth and srv.seen_auth[0].startswith('AWS4-HMAC-SHA256 '),
        '发出 SigV4 签名头')
    a = srv.seen_auth[0] if srv.seen_auth else ''
    chk('Credential=AKIDTEST/' in a, '签名含 AccessKey')
    chk('SignedHeaders=host;x-amz-content-sha256;x-amz-date' in a,
        'SignedHeaders 正确')
    chk('Signature=' in a and len(a.split('Signature=')[-1]) == 64,
        '签名长度为 64 位十六进制',
        len(a.split('Signature=')[-1]) if 'Signature=' in a else '')
    chk('/s3/aws4_request' in a, 'scope 含 s3/aws4_request')

    print()
    print('  -- 往返：发布 → 拉清单 → 下载 --')
    pkg = sand / 'upd.zip'
    with zipfile.ZipFile(pkg, 'w') as zf:
        zf.writestr('manifest.json', json.dumps({'version': 'v3.0.0'}))
        zf.writestr('ZeroFall.exe', b'y' * 4000)
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()

    chk(c.publish_update('v3.0.0', pkg, {'notes': 'S3 中转'}) is True, '发布成功')
    chk((s3root / 'zf-bucket' / 'zerofall-ctl' / 'updates' / 'v3.0.0.zip').is_file(),
        '对象已写入 bucket 前缀路径')
    chk((s3root / 'zf-bucket' / 'zerofall-ctl' / 'updates' / 'index.json').is_file(),
        '清单已写入')

    idx = c.check_update()
    chk(idx and idx.get('version') == 'v3.0.0', '拉回清单版本正确')
    chk(idx and idx.get('sha256') == sha, '清单 sha256 正确')

    local = c.fetch_update(idx)
    chk(local is not None, '下载成功')
    if local:
        chk(hashlib.sha256(Path(local).read_bytes()).hexdigest() == sha,
            '下载内容校验通过')

    chk(c.write_report('agent-s3', 'zf_error', {'e': 'x'}) is True, '上报成功')
    reps = list((s3root / 'zf-bucket' / 'zerofall-ctl' / 'reports').glob('*.json'))
    chk(len(reps) == 1, '报告落到 reports/')
    chk(c.mark_applied('agent-s3', 'v3.0.0') is True, '回执成功')

    print()
    print('  -- 错误密钥应被拒 --')
    bad = S3Channel(ep, 'zf-bucket', 'WRONG', 'WRONG', 'auto', 'zerofall-ctl')
    chk(bad.publish_update('v9.9.9', pkg, {}) is False, '错密钥上传失败')
    chk(not (s3root / 'zf-bucket' / 'zerofall-ctl' / 'updates' / 'v9.9.9.zip').exists(),
        '错密钥没写入文件')

    srv.stop()
    shutil.rmtree(sand, ignore_errors=True)


def test_sigv4_stability():
    print()
    print('=== 2. SigV4 签名稳定性（同输入同签名）===')
    from modules.net_transport import S3Channel
    c = S3Channel('https://example.r2.cloudflarestorage.com', 'b', 'AK', 'SK',
                  'auto', 'zerofall-ctl')
    h1 = c._headers_for('PUT', '/b/zerofall-ctl/updates/x.zip', b'hello')
    time.sleep(0.01)
    h2 = c._headers_for('PUT', '/b/zerofall-ctl/updates/x.zip', b'hello')
    # 时间戳变了签名就会变，这里只校验结构稳定
    chk(h1['Authorization'].split('Signature=')[0] ==
        h2['Authorization'].split('Signature=')[0],
        '相同输入下 Credential/SignedHeaders 段稳定')
    chk(h1['x-amz-content-sha256'] == h2['x-amz-content-sha256'],
        'payload 哈希稳定')
    chk(h1['x-amz-content-sha256'] == hashlib.sha256(b'hello').hexdigest(),
        'payload 哈希算法正确')
    # 不同内容必须不同签名主体
    h3 = c._headers_for('PUT', '/b/zerofall-ctl/updates/x.zip', b'world')
    chk(h3['x-amz-content-sha256'] != h1['x-amz-content-sha256'],
        '不同内容哈希不同')
    # 路径编码
    h4 = c._headers_for('PUT', '/b/z erofall/x.zip', b'')
    chk('Signature=' in h4['Authorization'], '含空格路径也能签名')


def test_ftp():
    print()
    print('=== 3. FTP 中转（用桩替代真实服务）===')
    import modules.net_transport as nt

    calls = {'connect': [], 'login': [], 'mkd': [], 'stor': [], 'retr': [],
             'quit': 0}
    store = {}

    class FakeFTP:
        def __init__(self, timeout=30):
            calls['connect'].append(('init', timeout))

        def connect(self, host, port):
            calls['connect'].append((host, port))
            return '220'

        def login(self, user, password):
            calls['login'].append((user, password))
            return '230'

        def mkd(self, path):
            calls['mkd'].append(path)
            raise Exception('already exists')      # 模拟已存在

        def storbinary(self, cmd, fp):
            data = fp.read()
            calls['stor'].append(cmd)
            store[cmd.split(' ', 1)[1]] = data
            return '226'

        def retrbinary(self, cmd, cb):
            path = cmd.split(' ', 1)[1]
            if path not in store:
                raise Exception('550 not found')
            cb(store[path])
            return '226'

        def quit(self):
            calls['quit'] += 1

    class FakeTLS(FakeFTP):
        def prot_p(self):
            calls['connect'].append(('prot_p',))

    # net_transport 在 _conn() 内部 import ftplib，所以替换 sys.modules 里那份
    import ftplib as real_ftplib
    orig_ftp, orig_tls = real_ftplib.FTP, real_ftplib.FTP_TLS
    real_ftplib.FTP = FakeFTP
    real_ftplib.FTP_TLS = FakeTLS
    try:
        c = nt.FtpChannel('ftp.example.com', 'u1', 'p1', 2121, '/zerofall-ctl')
        chk(c.available() is True, '可连接')
        chk(('ftp.example.com', 2121) in calls['connect'], '端口参数正确传递')
        chk(('u1', 'p1') in calls['login'], '账号密码正确传递')

        c.ensure()
        chk(any('/zerofall-ctl/updates' in m for m in calls['mkd']),
            '创建 updates 目录')
        chk(any('/zerofall-ctl/reports' in m for m in calls['mkd']),
            '创建 reports 目录')

        sand = Path(tempfile.mkdtemp(prefix='zf_ftp_'))
        pkg = sand / 'u.zip'
        with zipfile.ZipFile(pkg, 'w') as zf:
            zf.writestr('a.txt', 'z' * 1000)
        sha = hashlib.sha256(pkg.read_bytes()).hexdigest()

        chk(c.publish_update('v4.0.0', pkg, {}) is True, '发布成功')
        chk(any('STOR /zerofall-ctl/updates/v4.0.0.zip' in s for s in calls['stor']),
            '更新包上传到正确路径')

        idx = c.check_update()
        chk(idx and idx.get('version') == 'v4.0.0', '拉回清单')
        chk(idx and idx.get('sha256') == sha, '清单 sha256 正确')

        local = c.fetch_update(idx)
        chk(local is not None, '下载成功')
        if local:
            chk(hashlib.sha256(Path(local).read_bytes()).hexdigest() == sha,
                '下载校验通过')

        # TLS 分支
        ct = nt.FtpChannel('ftp.example.com', 'u', 'p', 21, '/r', tls=True)
        chk(ct.available() is True, 'FTPS 可连接')
        chk(('prot_p',) in calls['connect'], 'FTPS 调用了 prot_p（加密数据通道）')
        shutil.rmtree(sand, ignore_errors=True)
    finally:
        real_ftplib.FTP = orig_ftp
        real_ftplib.FTP_TLS = orig_tls


def test_factory():
    print()
    print('=== 4. make_relay 按 relay_kind 分派 ===')
    from modules.net_transport import make_relay, WebDavChannel, S3Channel, FtpChannel

    class C:
        relay_enabled = True
        relay_kind = 'webdav'
        relay_url = 'https://dav.example.com/dav'
        relay_user = 'u'
        relay_password = 'p'
        relay_root = '/zerofall-ctl'
        s3_endpoint = 'https://s3.example.com'
        s3_bucket = 'bk'
        s3_access_key = 'ak'
        s3_secret_key = 'sk'
        s3_region = 'auto'
        ftp_host = 'ftp.example.com'
        ftp_port = 21
        ftp_user = 'fu'
        ftp_password = 'fp'
        ftp_tls = False

    r = make_relay(C())
    chk(isinstance(r, WebDavChannel), 'kind=webdav → WebDavChannel')

    C.relay_kind = 's3'
    r = make_relay(C())
    chk(isinstance(r, S3Channel), 'kind=s3 → S3Channel')

    C.relay_kind = 'ftp'
    r = make_relay(C())
    chk(isinstance(r, FtpChannel), 'kind=ftp → FtpChannel')

    C.relay_enabled = False
    chk(make_relay(C()) is None, '未启用 → None')

    C.relay_enabled = True
    C.relay_kind = 's3'
    C.s3_bucket = ''
    chk(make_relay(C()) is None, 'S3 缺 bucket → None（不静默误用）')


def main():
    test_s3()
    test_sigv4_stability()
    test_ftp()
    test_factory()
    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
