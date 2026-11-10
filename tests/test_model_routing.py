# -*- coding: utf-8 -*-
"""模型路由选择与自动切换测试

真实事故（2026-11-09）：
  主控 zf-acp 写死 `pick_model(..., route_prefix='["opencode-go"')`，
  而 DSH 桌面版早已改用 `deepseek-account` 认证，并把
  OPENCODE_GO_API_KEY 从凭据文件里删掉了。
  结果：主控永远挑 opencode-go → 没有 key → 跑完几分钟 ACP 才失败。

  实测还发现：deepseek-account 认证能过，但账户余额不足（Insufficient Balance）。
  所以正确策略是「按优先级试多条路由」，而不是钉死一条。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 隔离模型记忆：pick_models() 会用 model_memory 重排候选，
# 若读真实记忆文件，测试结果会随"上次哪个模型跑成功过"漂移。
# 这里换成临时目录并清空，让排序回到纯静态优先级（可复现）。
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_routing_')
from modules import model_memory as _mm      # noqa: E402
_mm.clear()

from modules.acp_client import pick_model, pick_models

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def V(route, model):
    return json.dumps([route, model])


def opts(*groups):
    return [{'id': 'model', 'name': 'Model', 'options': [
        {'group': g, 'options': [{'value': V(r, m), 'name': n} for r, m, n in items]}
        for g, items in groups
    ]}]


def main():
    print('=== 1. 两种路由都在：优先 deepseek-account ===')
    o = opts(
        ('deepseek-account', [('deepseek-account', 'deepseek-flash', 'DeepSeek-V4.1-Flash'),
                              ('deepseek-account', 'deepseek-v4-pro', 'DeepSeek-V4-Pro')]),
        ('opencode-go', [('opencode-go', 'deepseek-v4.1-flash', 'deepseek-v4.1-flash')]),
    )
    v, n = pick_model(o)
    chk('deepseek-account' in v, '首选 deepseek-account（自持账号认证）', v)
    chk('flash' in v, 'flash 优先于 pro', v)

    print()
    print('=== 2. 候选列表按优先级完整覆盖 ===')
    cs = pick_models(o)
    chk(len(cs) == 3, '三个模型全部进候选', len(cs))
    chk('deepseek-account' in cs[0][0], '第一个是 deepseek-account')
    chk('opencode-go' in cs[-1][0], '最后一个才是 opencode-go（兜底）')

    print()
    print('=== 3. 只有 opencode-go：仍能选中它 ===')
    o2 = opts(('opencode-go', [('opencode-go', 'deepseek-v4.1-flash', 'flash')]))
    v2, n2 = pick_model(o2)
    chk('opencode-go' in v2, '单路由时不会挑空', v2)
    chk(len(pick_models(o2)) == 1, '候选只有 1 个')

    print()
    print('=== 4. 只有 deepseek-account ===')
    o3 = opts(('deepseek-account', [('deepseek-account', 'deepseek-v4-pro', 'V4-Pro')]))
    v3, n3 = pick_model(o3)
    chk('deepseek-account' in v3, '能选中 account 路由', v3)

    print()
    print('=== 5. 空配置不崩 ===')
    chk(pick_model([]) == (None, None), '空列表返回 (None, None)')
    chk(pick_models([]) == [], 'pick_models 返回空列表')
    chk(pick_model([{'id': 'other', 'options': []}]) == (None, None),
        '没有 model 项时返回 None')

    print()
    print('=== 6. 未知路由也能兜底选出 ===')
    o4 = opts(('some-new-provider', [('some-new-provider', 'x-model', 'X')]))
    v4, n4 = pick_model(o4)
    chk(v4 is not None and 'x-model' in v4, '陌生路由不会导致无模型可用', v4)

    print()
    print('=== 7. 老调用方传 route_prefix 仍兼容 ===')
    v5, n5 = pick_model(o, route_prefix='["opencode-go"')
    chk('opencode-go' in v5, '显式 route_prefix 优先级最高', v5)

    print()
    print('=== 8. 顺序去重 ===')
    cs2 = pick_models(o)
    vals = [v for v, _ in cs2]
    chk(len(vals) == len(set(vals)), '候选里没有重复项')
    chk(len(pick_models(o + o)) == 3, '重复输入不产生重复候选',
        len(pick_models(o + o)))

    print()
    print('=== 9. set_model 传的是可选值之一（结构检查）===')
    chk(all(v.startswith('["') and v.endswith('"]') for v, _ in cs2),
        '每个候选都是合法 JSON 数组字符串')


    print()
    print('=== 10. 模型记忆生效：可用的会被提前 ===')
    # 上面所有断言都在"记忆为空"的前提下验证静态优先级。
    # 这里验证动态部分：一旦某个模型被记成功过，它应排到候选最前面 ——
    # 这正是当初解决 deepseek-account 余额不足时想要的行为。
    from modules import model_memory as mm
    mm.clear()
    mm.record_ok('opencode-go', 'deepseek-v4.1-flash')
    cs9 = pick_models(o)
    chk('opencode-go' in cs9[0][0], '记忆可用的模型被排到第一', cs9[0][0])
    chk(len(cs9) == 3, '排序不影响候选数量', len(cs9))

    print()
    print('=== 11. 记忆里标记失效的会被排到后面 ===')
    mm.clear()
    mm.record_fail('deepseek-account', 'deepseek-flash', 'region')
    cs10 = pick_models(o)
    chk('opencode-go' in cs10[0][0] or 'v4-pro' in cs10[0][0],
        '失效模型不再排第一', cs10[0][0])
    mm.clear()

    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1



if __name__ == '__main__':
    raise SystemExit(main())
