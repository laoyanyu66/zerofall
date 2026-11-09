# -*- coding: utf-8 -*-
"""模型能力记忆测试

背景（实测数据，2026-11-09）：
  opencode-go 下 12 个模型里只有 3 个真能出结果：
    可用     mimo-v2.6-flash / mimo-v2.6-pro / grok-4.7 / DeepSeek V4.1 Flash
    已弃用   mimo-v2.5-pro（410）、minimax-m2.7（410）
    地区限制 gpt-5.6-luna / gpt-6-luna / muse-spark-*（403 region）
    不可用   space-bunny / grok-4.6

  每次修复都从首选路由重试一遍的话，光挑模型就浪费几十秒。
  记忆层要让：跑通过的排最前、结构性失效的沉底、余额类会过期。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_SAND = tempfile.mkdtemp(prefix='zf_mem_')
os.environ['ZF_DATA_DIR'] = _SAND

from modules import model_memory

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def V(route, model):
    return json.dumps([route, model])


def main():
    print('=== 1. 初始为空 ===')
    model_memory.clear()
    chk(model_memory.load() == {'ok': {}, 'bad': {}}, '清空后是空的')
    chk('0' in model_memory.summary() or '可用 0' in model_memory.summary(),
        '摘要显示 0', model_memory.summary())

    print()
    print('=== 2. 成功记录会被持久化 ===')
    model_memory.record_ok('opencode-go', 'grok-4.7')
    d = model_memory.load()
    chk('opencode-go|grok-4.7' in d['ok'], '写入 ok 表')
    p = model_memory.path()
    chk(p.exists(), '落盘文件存在', p.name)
    chk('grok-4.7' in p.read_text(encoding='utf-8'), '文件内容含模型名')

    print()
    print('=== 3. 成功过的排最前 ===')
    cands = [(V('deepseek-account', 'deepseek-flash'), 'DeepSeek-V4.1-Flash'),
             (V('opencode-go', 'grok-4.7'), 'grok-4.7'),
             (V('opencode-go', 'space-bunny'), 'space-bunny')]
    ranked = model_memory.rank(cands)
    chk(ranked[0][1] == 'grok-4.7', '成功过的排第一', ranked[0][1])

    print()
    print('=== 4. 地区受限的沉底 ===')
    model_memory.record_fail('opencode-go', 'gpt-6-luna', 'region')
    ranked = model_memory.rank(
        [(V('opencode-go', 'gpt-6-luna'), 'gpt-6-luna'),
         (V('opencode-go', 'brand-new'), 'brand-new')])
    chk(ranked[-1][1] == 'gpt-6-luna', '地区受限排最后', ranked[-1][1])
    chk(ranked[0][1] == 'brand-new', '没试过的排在它前面', ranked[0][1])

    print()
    print('=== 5. 已弃用的也沉底 ===')
    model_memory.record_fail('opencode-go', 'mimo-v2.5-pro', 'deprecated')
    ranked = model_memory.rank(
        [(V('opencode-go', 'mimo-v2.5-pro'), 'mimo-v2.5-pro'),
         (V('opencode-go', 'mimo-v2.6-flash'), 'mimo-v2.6-flash')])
    chk(ranked[-1][1] == 'mimo-v2.5-pro', '已弃用排最后', ranked[-1][1])

    print()
    print('=== 6. 余额类会过期（充值后应重试）===')
    model_memory.record_fail('deepseek-account', 'deepseek-flash', 'balance')
    ranked = model_memory.rank(
        [(V('deepseek-account', 'deepseek-flash'), 'deepseek-flash')])
    chk(ranked[0][1] == 'deepseek-flash', '仍在候选里（不是永久剔除）')
    # 手工把时间改成很久以前
    d = model_memory.load()
    d['bad']['deepseek-account|deepseek-flash']['ts'] = 0
    model_memory.save(d)
    dead = model_memory._is_dead(d['bad']['deepseek-account|deepseek-flash'],
                                 __import__('time').time())
    chk(dead is False, '超过 TTL 后不再视为失效')

    print()
    print('=== 7. 后来的成功会撤销黑名单 ===')
    model_memory.record_fail('opencode-go', 'grok-4.7', 'region')
    chk('opencode-go|grok-4.7' in model_memory.load()['bad'], '先记一次失败')
    model_memory.record_ok('opencode-go', 'grok-4.7')
    chk('opencode-go|grok-4.7' not in model_memory.load()['bad'],
        '再成功后从黑名单移除')
    chk('opencode-go|grok-4.7' in model_memory.load()['ok'], '并记入成功表')

    print()
    print('=== 8. 最近成功的更靠前 ===')
    model_memory.clear()
    model_memory.record_ok('opencode-go', 'old-model')
    d = model_memory.load()
    d['ok']['opencode-go|old-model'] = 1
    model_memory.save(d)
    model_memory.record_ok('opencode-go', 'new-model')
    ranked = model_memory.rank(
        [(V('opencode-go', 'old-model'), 'old-model'),
         (V('opencode-go', 'new-model'), 'new-model')])
    chk(ranked[0][1] == 'new-model', '新的成功排更前', ranked[0][1])

    print()
    print('=== 9. 异常输入不崩 ===')
    chk(model_memory.rank([]) == [], '空列表安全')
    chk(model_memory.rank(None) == [], 'None 安全')
    chk(len(model_memory.rank([('bad-value', 'x')])) == 1, '畸形 value 不崩')
    model_memory.path().write_text('{ 坏掉的 json', encoding='utf-8')
    chk(model_memory.load() == {'ok': {}, 'bad': {}}, '文件损坏时返回空而不崩')

    print()
    print('=== 10. 摘要可读 ===')
    model_memory.clear()
    model_memory.record_ok('opencode-go', 'DeepSeek V4.1 Flash')
    model_memory.record_fail('opencode-go', 'gpt-6-luna', 'region')
    s = model_memory.summary()
    chk('可用' in s and '失效' in s, '摘要含可用/失效', s)
    chk('gpt-6-luna' not in s or '失效' in s, '摘要提到失效数')

    shutil.rmtree(_SAND, ignore_errors=True)

    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
