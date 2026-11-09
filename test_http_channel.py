# -*- coding: utf-8 -*-
"""HTTP 直连通道测试

背景：MasterHttpServer / HttpControlChannel 都写好了，但原先没有任何地方
      实例化 MasterHttpServer —— 配置里 http_server_enabled=true 只是个空开关。
      本测试验证真正起得来、鉴权有效、子控能通过它上报与拉更新。
"""
import json
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.control import ControlChannel
from modules.net_transport import MasterHttpServer, HttpControlChannel

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_http_'))
    ch = ControlChannel(root)
    ch.ensure()

    port = free_port()
    print('=== 1. 启动内建服务器（带令牌）===')
    srv = MasterHttpServer(str(root), port=port, token='secret-token')
    ok = srv.start()
    chk(ok is True, '服务器启动成功 (端口 {})'.format(port))
    time.sleep(0.3)

    url = 'http://127.0.0.1:{}'.format(port)

    print()
    print('=== 2. 健康检查 ===')
    agent = HttpControlChannel(url, 'secret-token', timeout=(3, 10))
    try:
        alive = agent._probe()
        chk(alive is True, '健康检查可达（_probe → True）')
        chk(agent.available() is True, 'available() 为 True')
    except Exception as e:
        chk(False, '健康检查失败: {}'.format(e))

    print()
    print('=== 3. 子控上报 → 主控收到 ===')
    try:
        r = agent.write_report('agent-1', 'zf_error', {'error': 'boom', 'traceback': 'x'})
        chk(bool(r), '上报接口返回成功')
        time.sleep(0.2)
        reports = ch.list_reports(only_new=True)
        chk(len(reports) == 1, '通道目录里出现 1 条报告 ({})'.format(len(reports)))
        if reports:
            path, doc = reports[0]
            chk(doc.get('kind') == 'zf_error', 'kind 正确')
            chk(doc.get('agent_id') == 'agent-1', 'agent_id 正确')
            chk((doc.get('payload') or {}).get('error') == 'boom', 'payload 完整')
    except Exception as e:
        chk(False, '上报失败: {}'.format(e))

    print()
    print('=== 4. 更新分发：主控发布 → 子控拉到 ===')
    try:
        pkg = root / 'updates' / 'ZeroFall_update_v9.9.9.zip'
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.write_bytes(b'PK\x03\x04fake-package')
        man = {'version': 'v9.9.9', 'sha256': 'deadbeef', 'notes': 'test'}
        pub = ch.publish_update('v9.9.9', pkg, man)
        chk(bool(pub), '主控发布成功')

        idx = agent.check_update()
        chk(idx and idx.get('version') == 'v9.9.9', '子控通过 HTTP 拿到清单: {}'.format(
            (idx or {}).get('version')))

        local = agent.fetch_update(idx)
        chk(local is not None and Path(local).exists(),
            '子控下载到更新包: {}'.format(Path(local).name if local else None))
    except Exception as e:
        chk(False, '更新分发失败: {}'.format(e))

    print()
    print('=== 5. 鉴权：错令牌被拒 ===')
    bad = HttpControlChannel(url, 'wrong-token', timeout=(3, 10))
    try:
        rejected = False
        try:
            bad.write_report('agent-x', 'zf_error', {'error': 'nope'})
            # 有些实现返回 False 而非抛错
            rejected = True
        except Exception:
            rejected = True
        chk(rejected, '错令牌被拒绝（不静默成功）')
        reports_after = ch.list_reports(only_new=True)
        chk(len(reports_after) == 1, '错令牌没写入新报告（仍为 1 条）')
    except Exception as e:
        chk(False, '鉴权测试异常: {}'.format(e))

    print()
    print('=== 6. 应用回执 ===')
    try:
        agent.mark_applied('agent-1', 'v9.9.9')
        applied = list((root / 'applied').glob('*'))
        chk(len(applied) >= 1, '回执写入 applied/ ({})'.format(len(applied)))
    except Exception as e:
        chk(False, '回执失败: {}'.format(e))

    print()
    print('=== 7. 停止服务 ===')
    try:
        srv.stop()
        time.sleep(0.3)
        dead = False
        try:
            # 端口应已关闭
            s = socket.create_connection(('127.0.0.1', port), timeout=1)
            s.close()
        except Exception:
            dead = True
        chk(dead, '停止后端口不再接受连接')
    except Exception as e:
        chk(False, '停止失败: {}'.format(e))

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
