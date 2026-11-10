# -*- coding: utf-8 -*-
"""崩溃上报去重测试

背景：AgentNode.check_dsh_crash() 每轮心跳都会调用，原实现只要状态里
有 last_exit_crash 就上报一次 —— 同一个崩溃会被反复报到主控，主控每次
都去建会话修同一个问题。修复后按崩溃事件序号去重，这里把行为锁住。

覆盖：
  1. 没有崩溃           → 不上报
  2. 一次崩溃           → 只上报一次
  3. 同一崩溃重复检查    → 不再上报
  4. 新崩溃             → 会再上报一次
  5. 上报失败不记账      → 下轮可重试
  6. 老状态文件兼容      → 升级后补报一次，之后不再报
  7. crash_seq 持久化    → 新建 checker 实例（相当于重启）也不会重复报
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_tmp = tempfile.mkdtemp(prefix='zf_crash_once_')
os.environ['ZF_DATA_DIR'] = _tmp

from config import ZeroFallConfig                                   # noqa: E402
from modules.environment_checker import EnvironmentChecker          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make(tag, state=None):
    sd = Path(_tmp) / tag
    sd.mkdir(parents=True, exist_ok=True)
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(sd)
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    if state is not None:
        (sd / '.environment_state.json').write_text(
            json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return EnvironmentChecker(cfg), sd


def test_no_crash():
    print('\n[1] 没有崩溃 → 不上报')
    ec, _ = make('a', {'last_exit_code': 0, 'last_exit_crash': False})
    check('pending 为 None', ec.pending_crash_report() is None)


def test_single_crash_once():
    print('\n[2] 一次崩溃 → 只上报一次')
    ec, _ = make('b', {'last_exit_code': 0, 'last_exit_crash': False})
    ec.record_exit(1, True)
    p1 = ec.pending_crash_report()
    check('第一次取出待上报', p1 is not None, '实得 {}'.format(p1))
    check('退出码正确', p1 and p1[0] == 1, '实得 {}'.format(p1))
    check('序号为 1', p1 and p1[1] == 1, '实得 {}'.format(p1))
    ec.mark_crash_reported(p1[1])
    print('\n[3] 重复检查 → 不再上报')
    check('第二次 pending 为 None', ec.pending_crash_report() is None)
    check('第三次 pending 仍为 None', ec.pending_crash_report() is None)


def test_second_crash_reports_again():
    print('\n[4] 新崩溃 → 会再上报一次')
    ec, _ = make('c', {'last_exit_code': 0, 'last_exit_crash': False})
    ec.record_exit(1, True)
    p = ec.pending_crash_report()
    ec.mark_crash_reported(p[1])
    ec.record_exit(2, True)          # 又一次崩溃
    p2 = ec.pending_crash_report()
    check('新崩溃可上报', p2 is not None, '实得 {}'.format(p2))
    check('序号递增到 2', p2 and p2[1] == 2, '实得 {}'.format(p2))
    check('退出码是新的', p2 and p2[0] == 2, '实得 {}'.format(p2))
    ec.mark_crash_reported(p2[1])
    check('报完又静默', ec.pending_crash_report() is None)


def test_failed_report_retries():
    print('\n[5] 上报失败不记账 → 下轮可重试')
    ec, _ = make('d', {'last_exit_code': 0, 'last_exit_crash': False})
    ec.record_exit(3, True)
    p1 = ec.pending_crash_report()
    # 不调用 mark（模拟上报返回 None）
    p2 = ec.pending_crash_report()
    check('仍可取到同一事件', p2 is not None and p2[1] == p1[1],
          'p1={} p2={}'.format(p1, p2))
    ec.mark_crash_reported(p2[1])
    check('记账后静默', ec.pending_crash_report() is None)


def test_legacy_state():
    print('\n[6] 老状态文件（无序号字段）→ 补报一次后静默')
    ec, sd = make('e', {'last_exit_crash': True, 'last_exit_code': 4294967295})
    p = ec.pending_crash_report()
    check('能取到历史崩溃', p is not None, '实得 {}'.format(p))
    check('序号按 1 处理', p and p[1] == 1, '实得 {}'.format(p))
    ec.mark_crash_reported(p[1])
    check('补报后静默', ec.pending_crash_report() is None)
    state = json.loads((sd / '.environment_state.json').read_text(encoding='utf-8'))
    check('已写入 crash_reported_seq', state.get('crash_reported_seq') == 1,
          '实得 {}'.format(state.get('crash_reported_seq')))


def test_persistence_across_restart():
    print('\n[7] 模拟重启（新建 checker）→ 不会重复上报')
    tag = 'f'
    ec, sd = make(tag, {'last_exit_code': 0, 'last_exit_crash': False})
    ec.record_exit(9, True)
    p = ec.pending_crash_report()
    ec.mark_crash_reported(p[1])

    # 新建实例 = 进程重启后重新读状态文件
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(sd)
    ec2 = EnvironmentChecker(cfg)
    check('重启后 pending 为 None', ec2.pending_crash_report() is None)


def test_normal_exit_does_not_bump_seq():
    print('\n[8] 正常退出不产生新崩溃事件')
    ec, _ = make('g', {'last_exit_code': 0, 'last_exit_crash': False})
    ec.record_exit(1, True)
    p = ec.pending_crash_report()
    ec.mark_crash_reported(p[1])
    ec.record_exit(0, False)          # 正常退出
    check('正常退出后无待上报', ec.pending_crash_report() is None)


if __name__ == '__main__':
    print('=' * 64)
    print('崩溃上报去重测试')
    print('=' * 64)
    test_no_crash()
    test_single_crash_once()
    test_second_crash_reports_again()
    test_failed_report_retries()
    test_legacy_state()
    test_persistence_across_restart()
    test_normal_exit_does_not_bump_seq()
    print()
    print('=' * 64)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 64)
    sys.exit(0 if not FAIL else 1)
