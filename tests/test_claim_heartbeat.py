# -*- coding: utf-8 -*-
"""并发认领 / 心跳存活 / 日志保留 测试

这三项是审计中发现的真实短板：
  1. 主控修复一份报告要 60~200 秒，期间报告未被标记完成；
     若同一通道上有第二个主控实例，会捡到同一份再修一遍（白跑 + 重复发版）
  2. heartbeat 这个 kind 一直存在，但从来没有任何一方发送过 ——
     主控完全不知道子控是死是活
  3. log_max_size_mb / log_backup_count 两个配置项从来没被读取，
     日志按天累积且永不清理
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def main():
    from modules.control import ControlChannel

    sand = Path(tempfile.mkdtemp(prefix='zf_claim_'))
    chan = ControlChannel(str(sand / 'chan'))
    chan.ensure()

    print('=== 1. 报告认领：同一份只能被一个主控拿到 ===')
    rid, path = chan.write_report('agent-A', 'zf_error', {'msg': 'boom'})
    ok1 = chan.claim_report(rid, 'master-1')
    ok2 = chan.claim_report(rid, 'master-2')
    chk(ok1 is True, '第一个主控认领成功')
    chk(ok2 is False, '第二个主控认领被拒（不会重复修复）')

    print()
    print('=== 2. 认领者释放后，别人可以接手 ===')
    chan.release_claim(rid)
    ok3 = chan.claim_report(rid, 'master-2')
    chk(ok3 is True, '释放后另一主控可接手')
    chan.release_claim(rid)

    print()
    print('=== 3. 同一主控重入不算冲突 ===')
    chan.claim_report(rid, 'master-1')
    again = chan.claim_report(rid, 'master-1')
    chk(again is True, '自己重入被放行（避免把自己锁死）')
    chan.release_claim(rid)

    print()
    print('=== 4. 认领过期自动释放（防主控崩溃永久卡住）===')
    chan.claim_report(rid, 'dead-master')
    claim_file = sand / 'chan' / 'claims' / (rid + '.claim')
    doc = json.loads(claim_file.read_text(encoding='utf-8'))
    doc['at'] = time.time() - 4000          # 假装 66 分钟前认领的
    claim_file.write_text(json.dumps(doc), encoding='utf-8')
    ok = chan.claim_report(rid, 'master-new', ttl_seconds=1800)
    chk(ok is True, '过期认领可被新主控接管')
    chan.release_claim(rid)

    print()
    print('=== 5. cleanup_claims 清理过期残留 ===')
    chan.claim_report(rid, 'crashed')
    claim_file.write_text(json.dumps({'owner': 'x', 'at': time.time() - 99999}),
                          encoding='utf-8')
    os.utime(str(claim_file), (time.time() - 99999, time.time() - 99999))
    n = chan.cleanup_claims(ttl_seconds=1800)
    chk(n == 1, '共清理 1 个过期认领', n)
    chk(not claim_file.exists(), '残留文件已删除')

    print()
    print('=== 6. 心跳：写入存活表，不占报告队列 ===')
    class Cfg:
        report_to_master = True
        report_zf_error = True
        report_dsh_crash = True
        agent_id = 'agent-remote-9'
        control_channel_dir = str(sand / 'chan')
        master_url = ''
        http_token = ''
        relay_enabled = False
        app_version = 'v1.3.0'

    from modules.control import AgentNode
    node = AgentNode(Cfg())
    sent = node.send_heartbeat()
    chk(sent is True, '心跳发送成功')

    # 主控侧处理
    reports = chan.list_reports(only_new=True)
    hb = [r for r in reports if r[1].get('kind') == 'heartbeat']
    chk(len(hb) >= 1, '主控能收到心跳报告', len(hb))

    # 模拟 master 处理心跳
    if hb:
        p, d = hb[0]
        chan.record_agent(d['agent_id'], {
            'hostname': d.get('hostname', ''),
            'zerofall_version': d.get('zerofall_version', ''),
        })
        try:
            Path(p).unlink()
        except Exception:
            pass

    seen = chan.agents_seen()
    chk(len(seen) >= 1, '存活表里有记录', len(seen))
    if seen:
        a = seen[0]
        chk(a.get('agent_id') == 'agent-remote-9', 'agent_id 正确', a.get('agent_id'))
        chk(a.get('alive') is True, '判定为在线')
        chk(a.get('zerofall_version') == 'v1.3.0', '带上了版本号',
            a.get('zerofall_version'))
        chk('last_seen' in a, '有最后活跃时间', a.get('last_seen'))

    print()
    print('=== 7. 心跳不写 .done（不堵塞 processed 目录）===')
    done_files = list((sand / 'chan' / 'processed').glob('*heartbeat*'))
    chk(len(done_files) == 0, '没有心跳残留的 .done 文件', len(done_files))

    print()
    print('=== 8. 离线判定 ===')
    d = sand / 'chan' / 'agents'
    f = d / 'agent-old.json'
    f.write_text(json.dumps({
        'agent_id': 'agent-old', 'last_seen_ts': time.time() - 5000,
        'last_seen': '2026-01-01 00:00:00'}), encoding='utf-8')
    seen2 = {x['agent_id']: x for x in chan.agents_seen(alive_seconds=900)}
    chk(seen2.get('agent-old', {}).get('alive') is False, '5 分钟无心跳判为离线')
    chk(seen2['agent-old']['idle_seconds'] >= 4900, '空闲秒数正确',
        seen2['agent-old']['idle_seconds'])

    print()
    print('=== 9. 多子控并存 ===')
    for aid in ('agent-B', 'agent-C'):
        chan.record_agent(aid, {'hostname': aid})
    seen3 = chan.agents_seen()
    chk(len(seen3) >= 3, '三台子控都能列出', len(seen3))
    chk(seen3[0].get('last_seen_ts', 0) >= seen3[-1].get('last_seen_ts', 0),
        '按最后活跃时间倒序')

    print()
    print('=== 10. 日志保留（配置项落地）===')
    from logger import ZeroFallLogger
    logdir = sand / 'logs'
    logdir.mkdir()
    old = logdir / 'zerofall_20200101.log'
    old.write_text('old', encoding='utf-8')
    os.utime(str(old), (time.time() - 86400 * 30,) * 2)
    new = logdir / 'zerofall_{}.log'.format(time.strftime('%Y%m%d'))
    new.write_text('new', encoding='utf-8')
    removed = ZeroFallLogger.cleanup_old_logs(str(logdir), keep_days=5)
    chk(removed >= 1, '清掉了 30 天前的日志', removed)
    chk(not old.exists(), '旧日志文件已删除')
    chk(new.exists(), '今天的日志保留')

    shutil.rmtree(sand, ignore_errors=True)
    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
