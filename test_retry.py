# -*- coding: utf-8 -*-
"""主控重试机制测试

解决的问题：
  认证态抖动（key 过期 / 额度耗尽 / 网关 429）都是**临时**故障。
  原先修复失败就直接 mark_processed，等于一次抖动就把报告永久吞掉，
  子控那边永远等不到更新包、也不知道为什么。

现在的语义：
  * 第 1、2 次失败 → 只记尝试次数，报告保留（不写 .done），等够间隔再试
  * 第 3 次失败     → 放弃，标记已处理并写明原因
  * 成功            → 清空尝试记录，正常标记已处理
  * 重试间隔 600 秒，避免每分钟猛敲
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.control import ControlChannel

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_retry_'))
    ch = ControlChannel(root)
    ch.ensure()

    print('=== 1. 尝试计数 ===')
    chk(ch.get_attempt('r1') == (0, 0.0), '新报告尝试次数为 0')
    chk(ch.bump_attempt('r1') == 1, '第一次失败 → 计数 1')
    chk(ch.bump_attempt('r1') == 2, '第二次失败 → 计数 2')
    chk(ch.get_attempt('r1')[0] == 2, '计数读回正确')

    print()
    print('=== 2. 重试间隔 ===')
    ok, count, wait = ch.should_retry('r1', min_interval_seconds=600, max_attempts=3)
    chk(ok is False, '刚失败过，本轮不重试')
    chk(count == 2, '返回已试次数 2')
    chk(0 < wait <= 600, '返回剩余等待秒数 ({})'.format(wait))

    # 把上次尝试时间推到 11 分钟前
    p = ch._attempt_file('r1')
    d = json.loads(p.read_text(encoding='utf-8'))
    d['last_at'] = time.time() - 660
    p.write_text(json.dumps(d), encoding='utf-8')
    ok2, count2, wait2 = ch.should_retry('r1', min_interval_seconds=600, max_attempts=3)
    chk(ok2 is True, '等够 600 秒后允许重试')
    chk(wait2 == 0, '不再需要等待')

    print()
    print('=== 3. 次数用尽后放弃 ===')
    ch.bump_attempt('r1')          # → 3
    ok3, count3, _ = ch.should_retry('r1', min_interval_seconds=600, max_attempts=3)
    chk(count3 == 3, '计数达到 3')
    chk(ok3 is False, '用尽次数后不再重试')

    print()
    print('=== 4. retry_later 不写 .done（报告保留）===')
    ch.retry_later('r9', {'ok': False, 'why': 'auth'})
    chk(not (ch.processed_dir / 'r9.done').exists(), '未写 processed/r9.done')
    chk((root / 'retry' / 'r9.json').exists(), '失败详情记到 retry/')
    detail = json.loads((root / 'retry' / 'r9.json').read_text(encoding='utf-8'))
    chk(detail.get('why') == 'auth', '失败原因被记录')

    print()
    print('=== 5. mark_processed 仍正常 ===')
    ch.mark_processed(root / 'reports' / 'r9.json', {'ok': True})
    chk((ch.processed_dir / 'r9.done').exists(), '成功后写 .done')
    done = json.loads((ch.processed_dir / 'r9.done').read_text(encoding='utf-8'))
    chk(done.get('ok') is True, '.done 内容正确')

    print()
    print('=== 6. clear_attempt ===')
    ch.clear_attempt('r1')
    chk(ch.get_attempt('r1') == (0, 0.0), '清空后计数归零')

    print()
    print('=== 7. 主控 _fail_or_retry 真实行为 ===')
    from modules.master import MasterNode

    class Cfg:
        role = 'master'
        control_channel_dir = str(root)
        master_source_dir = ''
        auto_fix_enabled = True
        master_self_update = False
        acp_profile = 'zf-acp'
        report_to_master = True

    class FakeMaster(MasterNode):
        def __init__(self, cfg):
            self.config = cfg
            self.channel = ControlChannel(Path(cfg.control_channel_dir))
            self.channel.ensure()
            self.busy = False
            self.last_result = None

    m = FakeMaster(Cfg())
    reports_dir = root / 'reports'
    reports_dir.mkdir(parents=True, exist_ok=True)
    rp = reports_dir / 'report_x.report.json'
    rp.write_text(json.dumps({'kind': 'zf_error'}), encoding='utf-8')

    # 第 1、2 次：应保留
    for i in (1, 2):
        res = m._fail_or_retry(rp, {'step': 'fix'}, '修复失败: auth 抖动')
        chk(res['retry']['attempt'] == i, '第 {} 次失败记录正确'.format(i))
        chk(res['retry']['gave_up'] is False, '第 {} 次不放弃'.format(i))
        chk(not (m.channel.processed_dir / 'report_x.report.done').exists(),
            '第 {} 次后报告仍保留'.format(i))

    # 第 3 次：应放弃并标记
    res3 = m._fail_or_retry(rp, {'step': 'fix'}, '修复失败: auth 抖动')
    chk(res3['retry']['attempt'] == 3, '第 3 次计数 3')
    chk(res3['retry']['gave_up'] is True, '第 3 次放弃')
    chk((m.channel.processed_dir / 'report_x.report.done').exists(),
        '放弃后标记已处理')
    chk(m.channel.get_attempt('report_x.report') == (0, 0.0), '放弃后清空尝试记录')

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
