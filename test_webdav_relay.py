# -*- coding: utf-8 -*-
"""WebDAV 中转通道测试（跨网络方案）

场景：主控在 CGNAT 后面，HTTP 直连不可达；子控不装穿透工具。
      双方都只出站访问一个 WebDAV 服务，用它当"中转柜"。

本测试用本地迷你 WebDAV 服务模拟坚果云/Nextcloud，
验证：建目录、上传、下载、sha256 校验、清单、回执、鉴权、降级。
"""
import base64
import hashlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + ('  ' + extra if extra else ''))


class MiniDav:
    """够用的 WebDAV：GET / PUT / MKCOL / PROPFIND / DELETE，可选 Basic 鉴权"""

    def __init__(self, root: Path, user='', password=''):
        self.root = root
        self.user = user
        self.password = password
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def _auth(self):
                if not outer.user:
                    return True
                h = self.headers.get('Authorization', '')
                if not h.startswith('Basic '):
                    return False
                try:
                    raw = base64.b64decode(h[6:]).decode('utf-8')
                    u, _, p = raw.partition(':')
                    return u == outer.user and p == outer.password
                except Exception:
                    return False

            def _path(self):
                return outer.root / unquote(self.path.lstrip('/'))

            def _deny(self):
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="dav"')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_PROPFIND(self):
                if not self._auth():
                    return self._deny()
                p = self._path()
                code = 207 if p.exists() else 404
                self.send_response(code)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_MKCOL(self):
                if not self._auth():
                    return self._deny()
                p = self._path()
                if p.exists():
                    self.send_response(405)
                else:
                    try:
                        p.mkdir(parents=True, exist_ok=True)
                        self.send_response(201)
                    except Exception:
                        self.send_response(500)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_PUT(self):
                if not self._auth():
                    return self._deny()
                p = self._path()
                n = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(n)
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(body)
                    self.send_response(201)
                except Exception:
                    self.send_response(500)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                if not self._auth():
                    return self._deny()
                p = self._path()
                if not p.is_file():
                    self.send_response(404)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                data = p.read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_DELETE(self):
                if not self._auth():
                    return self._deny()
                p = self._path()
                try:
                    if p.is_file():
                        p.unlink()
                    self.send_response(204)
                except Exception:
                    self.send_response(500)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self._handler = H
        self._httpd = None

    def start(self, port):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', port), self._handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    sand = Path(tempfile.mkdtemp(prefix='zf_dav_'))
    davroot = sand / 'dav'
    davroot.mkdir()
    port = free_port()

    print('=== 1. 起迷你 WebDAV（模拟坚果云）===')
    dav = MiniDav(davroot, user='me@example.com', password='app-pass')
    dav.start(port)
    time.sleep(0.2)
    url = 'http://127.0.0.1:{}'.format(port)
    print('  地址: {}  账号: me@example.com'.format(url))
    chk(True, 'WebDAV 服务已启动')

    from modules.net_transport import WebDavChannel

    print()
    print('=== 2. 通道可用性 + 自动建目录 ===')
    m = WebDavChannel(url, 'me@example.com', 'app-pass', '/zerofall-ctl')
    a = WebDavChannel(url, 'me@example.com', 'app-pass', '/zerofall-ctl')
    chk(m.available() is True, '主控侧能连上 WebDAV')
    m.ensure()
    for sub in ('updates', 'reports', 'applied'):
        chk((davroot / 'zerofall-ctl' / sub).is_dir(), '目录已创建: {}'.format(sub))

    print()
    print('=== 3. 主控发布更新包 ===')
    pkg = sand / 'ZeroFall_update_v2.0.0.zip'
    with zipfile.ZipFile(pkg, 'w') as zf:
        zf.writestr('manifest.json', json.dumps({'version': 'v2.0.0'}))
        zf.writestr('ZeroFall.exe', b'x' * 5000)
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()
    ok = m.publish_update('v2.0.0', pkg, {'version': 'v2.0.0', 'notes': '跨网络推送'})
    chk(ok is True, '发布成功')
    chk((davroot / 'zerofall-ctl' / 'updates' / 'v2.0.0.zip').is_file(), '包已上传到 WebDAV')
    chk((davroot / 'zerofall-ctl' / 'updates' / 'index.json').is_file(), '清单已上传')

    print()
    print('=== 4. 子控拉清单 ===')
    idx = a.check_update()
    chk(isinstance(idx, dict), '清单是 dict（与其它通道同形）')
    if idx:
        chk(idx.get('version') == 'v2.0.0', '版本正确: {}'.format(idx.get('version')))
        chk(idx.get('sha256') == sha, '清单里的 sha256 正确')
        chk(idx.get('file') == 'v2.0.0.zip', '文件名正确')

    print()
    print('=== 5. 子控下载 + sha256 校验 ===')
    local = a.fetch_update(idx)
    chk(local is not None, '下载成功')
    if local:
        got = hashlib.sha256(Path(local).read_bytes()).hexdigest()
        chk(got == sha, '下载内容 sha256 与清单一致')
        chk(Path(local).stat().st_size == pkg.stat().st_size, '大小一致')

    print()
    print('=== 6. sha256 不符则丢弃 ===')
    bad_idx = dict(idx)
    bad_idx['sha256'] = '0' * 64
    chk(a.fetch_update(bad_idx) is None, 'sha256 不匹配时拒绝安装')

    print()
    print('=== 7. 子控上报 → 主控可见 ===')
    okr = a.write_report('agent-remote-01', 'dsh_crash',
                         {'exit_code': 1, 'log_tail': 'boom'})
    chk(okr is True, '上报成功')
    rep = davroot / 'zerofall-ctl' / 'reports'
    files = list(rep.glob('*.json'))
    chk(len(files) == 1, 'WebDAV 上出现 1 条报告')
    if files:
        doc = json.loads(files[0].read_text(encoding='utf-8'))
        chk(doc.get('agent_id') == 'agent-remote-01', 'agent_id 正确')
        chk(doc.get('kind') == 'dsh_crash', 'kind 正确')
        chk((doc.get('payload') or {}).get('exit_code') == 1, 'payload 完整')

    print()
    print('=== 8. 应用回执 ===')
    chk(a.mark_applied('agent-remote-01', 'v2.0.0') is True, '回执写入成功')
    ap = davroot / 'zerofall-ctl' / 'applied' / 'agent-remote-01.json'
    chk(ap.is_file(), '回执文件存在')
    if ap.is_file():
        chk(json.loads(ap.read_text(encoding='utf-8')).get('version') == 'v2.0.0', '回执版本正确')

    print()
    print('=== 9. 鉴权：错密码被拒 ===')
    bad = WebDavChannel(url, 'me@example.com', 'wrong', '/zerofall-ctl')
    # available 对 401 仍返回 True（服务在线），但上传必须失败
    chk(bad.publish_update('v9.9.9', pkg, {}) is False, '错密码无法发布')
    chk(not (davroot / 'zerofall-ctl' / 'updates' / 'v9.9.9.zip').exists(), '错密码没写入文件')
    chk(bad.check_update() is None, '错密码读不到清单')

    print()
    print('=== 10. HybridChannel：HTTP 不可达 → 自动走 WebDAV ===')
    from modules.control import HybridChannel

    class Cfg:
        control_channel_dir = str(sand / 'dir_chan')
        master_url = ''                      # 模拟"没有局域网可达地址"
        http_token = ''
        relay_enabled = True
        relay_url = url
        relay_user = 'me@example.com'
        relay_password = 'app-pass'
        relay_root = '/zerofall-ctl'

    (sand / 'dir_chan').mkdir()
    hy = HybridChannel(Cfg())
    chk(hy.active == 'dav', '没有 master_url 时直接选 WebDAV')
    chk(hy.available() is True, '可用')
    idx2 = hy.check_update()
    chk(idx2 and idx2.get('version') == 'v2.0.0', '通过 Hybrid 拿到清单')

    print()
    print('=== 11. HTTP 配了但打不通 → 降级到 WebDAV ===')
    Cfg.master_url = 'http://127.0.0.1:1'    # 必然连不上
    hy2 = HybridChannel(Cfg())
    chk(hy2.active == 'http', '初始优先 HTTP')
    hy2.available()
    hy2.available()                          # 连续两次失败触发切换
    chk(hy2.active in ('dav', 'dir'), 'HTTP 打不通后已降级（当前 {}）'.format(hy2.active))
    chk(hy2.check_update() is not None, '降级后仍能拿到更新')

    print()
    print('=== 12. 全都不通 → 退到共享目录 ===')

    class Cfg2(Cfg):
        master_url = 'http://127.0.0.1:1'
        relay_url = 'http://127.0.0.1:2'

    hy3 = HybridChannel(Cfg2())
    hy3.available()
    hy3.available()
    chk(hy3.active == 'dir', '三条通道都不通时落到共享目录（当前 {}）'.format(hy3.active))

    dav.stop()
    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(sand, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
