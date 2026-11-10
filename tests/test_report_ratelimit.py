# -*- coding: utf-8 -*-
"""上报限流分层测试

背景：AgentNode.report() 里原来对所有类型都用"同类 10 分钟只报一次"。
对 zf_error 合理（同类报错容易成串刷屏），但对 dsh_crash 有害 ——
崩溃已经用事件序号做过永久去重，再加 10 分钟窗口只会把真实崩溃的
上报往后拖，最多晚 10 分钟才到主控。

修复后：dsh_crash 窗口 0（不额外限流），zf_error 保持 600 秒。

覆盖：
  1. dsh_crash 连续两次（不同签名）都能发出
  2. dsh_crash 相同签名也不被限流挡住（去重交给序号层）
  3. zf_error 相同内容第二次被挡
  4. zf_error 不同内容不受影响
  5. 配置开关关闭时不发送
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_ratelimit_')

from config import ZeroFallConfig          # noqa: E402
import modules.control as C                # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


class FakeChannel:
    """假通道：只记录发出去的报告"""

    def __init__(self):
        self.sent = []

    def write_report(self, agent_id, kind, payload):
        self.sent.append((kind, dict(payload)))
        return ('rid-{}'.format(len(self.sent)), None)


def make_node(**overrides):
    cfg = ZeroFallConfig()
    cfg.report_to_master = True
    cfg.report_zf_error = True
    cfg.report_dsh_crash = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    node = C.AgentNode.__new__(C.AgentNode)      # 跳过 __init__ 的重量级依赖
    node.config = cfg
    node.channel = FakeChannel()
    node.agent_id = 'test-agent'
    node.current_version = '1.3.0'
    node._last_reported = {}
    return node


def test_crash_not_throttled():
    print('\n[1] dsh_crash 不同退出码 → 都能发出')
    n = make_node()
    r1 = n.report_dsh_crash(1)
    r2 = n.report_dsh_crash(2)
    check('第一次发出', r1 is not None, '实得 {}'.format(r1))
    check('第二次也发出', r2 is not None, '实得 {}'.format(r2))
    check('通道收到两条', len(n.channel.sent) == 2, len(n.channel.sent))


def test_crash_same_signature_not_throttled():
    print('\n[2] dsh_crash 相同退出码 → 也不被限流挡（去重靠序号层）')
    n = make_node()
    n.report_dsh_crash(-1)
    n.report_dsh_crash(-1)
    check('两条都发出', len(n.channel.sent) == 2, len(n.channel.sent))


def test_zf_error_throttled():
    print('\n[3] zf_error 相同内容 → 第二次被挡')
    n = make_node()
    r1 = n.report_zf_error('boom same message')
    r2 = n.report_zf_error('boom same message')
    check('第一次发出', r1 is not None, '实得 {}'.format(r1))
    check('第二次被限流挡下', r2 is None, '实得 {}'.format(r2))
    check('通道只收到一条', len(n.channel.sent) == 1, len(n.channel.sent))


def test_zf_error_different_content():
    print('\n[4] zf_error 不同内容 → 不受影响')
    n = make_node()
    n.report_zf_error('error A')
    n.report_zf_error('error B')
    check('两条都发出', len(n.channel.sent) == 2, len(n.channel.sent))


def test_switches():
    print('\n[5] 开关关闭时不发送')
    n = make_node(report_dsh_crash=False)
    check('崩溃上报关闭 → 不发', n.report_dsh_crash(1) is None)
    check('通道为空', len(n.channel.sent) == 0, len(n.channel.sent))

    n2 = make_node(report_to_master=False)
    check('总开关关闭 → 不发', n2.report_zf_error('x') is None)

    n3 = make_node(report_zf_error=False)
    check('zf_error 开关关闭 → 不发', n3.report_zf_error('x') is None)
    check('但崩溃仍可发', n3.report_dsh_crash(1) is not None)


def test_window_table():
    print('\n[6] 限流窗口配置符合预期')
    check('dsh_crash 窗口为 0',
          C.AgentNode.REPORT_RATE_LIMIT.get('dsh_crash') == 0,
          C.AgentNode.REPORT_RATE_LIMIT.get('dsh_crash'))
    check('zf_error 窗口为 600',
          C.AgentNode.REPORT_RATE_LIMIT.get('zf_error') == 600,
          C.AgentNode.REPORT_RATE_LIMIT.get('zf_error'))
    check('未知类型默认有窗口',
          C.AgentNode.REPORT_RATE_LIMIT.get('whatever', 600) == 600)


if __name__ == '__main__':
    print('=' * 64)
    print('上报限流分层测试')
    print('=' * 64)
    test_crash_not_throttled()
    test_crash_same_signature_not_throttled()
    test_zf_error_throttled()
    test_zf_error_different_content()
    test_switches()
    test_window_table()
    print()
    print('=' * 64)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 64)
    sys.exit(0 if not FAIL else 1)
