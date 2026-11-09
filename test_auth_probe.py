# -*- coding: utf-8 -*-
"""认证自检与错误归类测试

为什么要这个自检：
  认证出问题时，ZeroFall 平时要等子控真的报错、并且跑完一整轮 ACP
  （几分钟）才会暴露出来，而且报的还是 "Internal error: turn failed: ..."
  这种看不出该做什么的原文。有了自检，几十秒内就能分清：
    没 key / 余额不足 / 限流 / 超时 / 其它
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.acp_client import classify_acp_error, reason_text

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def main():
    print('=== 1. 余额不足 ===')
    for m in ('Internal error: turn failed: Insufficient Balance (request_id: abc)',
              'insufficient_balance',
              'quota exceeded for this key'):
        chk(classify_acp_error(m) == 'balance', '识别为余额不足', m[:40])

    print()
    print('=== 2. 密钥缺失/无效 ===')
    for m in ('turn failed: invalid api key',
              'HTTP 401 Unauthorized',
              'no api key provided',
              'authentication failed'):
        chk(classify_acp_error(m) == 'auth', '识别为认证失败', m[:40])

    print()
    print('=== 3. 限流 / 超时 / 权限 ===')
    chk(classify_acp_error('429 Too Many Requests') == 'ratelimit', '限流')
    chk(classify_acp_error('request timed out after 60s') == 'timeout', '超时')
    chk(classify_acp_error('403 Forbidden') in ('forbidden', 'auth'), '无权限')

    print()
    print('=== 4. 未知错误不误判 ===')
    chk(classify_acp_error('something exploded') == 'other', '归为其它')
    chk(classify_acp_error('') == 'other', '空串归为其它')
    chk(classify_acp_error(None) == 'other', 'None 归为其它')

    print()
    print('=== 5. 给出可执行建议 ===')
    t_bal = reason_text('balance')
    chk('充值' in t_bal or '额度' in t_bal, '余额问题指向充/换路由', t_bal[:30])
    t_auth = reason_text('auth')
    chk('API Key' in t_auth and ('设置' in t_auth or 'txt' in t_auth),
        '认证问题指向具体填法', t_auth[:40])
    chk(reason_text('timeout') != '', '超时也有说明')
    chk(reason_text('nonexistent-kind') != '', '未知 kind 也有兜底说明')

    print()
    print('=== 6. 原因文案不含密钥形态 ===')
    blob = ' '.join(reason_text(k) for k in
                    ('balance', 'auth', 'forbidden', 'ratelimit', 'timeout', 'other'))
    chk('oc_sk' not in blob and 'sk-' not in blob, '文案里没有密钥样例')

    print()
    print('=== 7. probe_model 结构完整（不真正调用）===')
    from modules.acp_client import probe_model
    chk(callable(probe_model), 'probe_model 可调用')
    import inspect
    sig = inspect.signature(probe_model)
    chk('profile' in sig.parameters and 'timeout' in sig.parameters,
        '签名含 profile / timeout')

    print()
    print('=== 8. 分类大小写不敏感 ===')
    chk(classify_acp_error('INSUFFICIENT BALANCE') == 'balance', '大写也能识别')
    chk(classify_acp_error('Invalid API Key') == 'auth', '混合大小写能识别')

    print()
    print('=== 9. 多条错误里挑最有价值的（实测踩过的坑）===')
    from modules.acp_client import pick_informative_error
    # 真实场景：三条路由依次失败，最后一条是"模型选项无效"
    pairs = [
        ('session/prompt 失败: Internal error: turn failed: Insufficient Balance',
         'balance'),
        ('session/prompt 失败: Internal error: turn failed: Insufficient Balance',
         'balance'),
        ('session/set_config_option 失败: Invalid params: unknown model option: '
         '["opencode-go","deepseek-v4.1-flash"]', 'other'),
    ]
    msg, kind = pick_informative_error(pairs)
    chk(kind == 'balance', '不被"模型选项无效"盖掉，仍报余额不足', kind)
    chk('Insufficient Balance' in msg, '返回的是余额那条原文')

    print()
    print('=== 10. 只有无效参数时的兜底 ===')
    msg2, kind2 = pick_informative_error(
        [('Invalid params: unknown model option: x', 'other')])
    chk(msg2 != '' and kind2 == 'other', '全都无效时仍给出原文', msg2[:30])

    print()
    print('=== 11. 空输入 ===')
    m3, k3 = pick_informative_error([])
    chk(k3 == 'other', '空列表返回 other')
    chk(pick_informative_error(None)[1] == 'other', 'None 也安全')

    print()
    print('=== 12. 认证错误优先于限流 ===')
    m4, k4 = pick_informative_error([
        ('429 Too Many Requests', 'ratelimit'),
        ('invalid api key', 'auth'),
    ])
    chk(k4 == 'auth', '认证比限流更该优先报', k4)

    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
