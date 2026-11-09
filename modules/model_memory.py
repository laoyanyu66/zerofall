# -*- coding: utf-8 -*-
"""模型能力记忆

为什么需要：
  实测 opencode-go 下 12 个模型里只有 3 个真能出结果：
    可用     mimo-v2.6-flash / mimo-v2.6-pro / grok-4.7
    已弃用   mimo-v2.5-pro（410）、minimax-m2.7（410）
    地区限制 gpt-5.6-luna / gpt-6-luna / muse-spark-*（403）
    不可用   space-bunny / grok-4.6

  而每次修复都从"首选路由"重新试一遍，deepseek-account 余额不足要等
  一次往返、已弃用模型又各要一次往返 —— 光挑模型就浪费几十秒。

  这里把"哪个模型真的跑通过"记下来，下次直接排到最前面：
    · 成功过的 → 优先（最近成功过的更前）
    · 地区限制/已弃用 → 永久沉到最后，不再浪费往返
    · 余额不足 → 沉底但会过期（充值后应该重试）
"""
import json
import os
import time
from pathlib import Path

FILE_NAME = 'model_memory.json'

# 这些原因是"结构性"的，重试没意义，直接沉底
PERMANENT = ('region', 'deprecated', 'unavailable')
# 余额类会随充值变化，给它一个过期时间
BALANCE_TTL = 6 * 3600


def base_dir() -> Path:
    env = os.environ.get('ZF_DATA_DIR')
    if env:
        return Path(env)
    import sys
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def path() -> Path:
    return base_dir() / FILE_NAME


def load() -> dict:
    try:
        p = path()
        if not p.exists():
            return {'ok': {}, 'bad': {}}
        d = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(d, dict):
            return {'ok': {}, 'bad': {}}
        d.setdefault('ok', {})
        d.setdefault('bad', {})
        return d
    except Exception:
        return {'ok': {}, 'bad': {}}


def save(d: dict) -> bool:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        os.replace(str(tmp), str(p))
        return True
    except Exception:
        return False


def _key(route: str, model: str) -> str:
    return '{}|{}'.format(route or '', model or '')


def record_ok(route: str, model: str):
    """记一次成功"""
    d = load()
    k = _key(route, model)
    d['ok'][k] = int(time.time())
    d['bad'].pop(k, None)          # 曾经失败过但后来成功了，撤销黑名单
    save(d)


def record_fail(route: str, model: str, kind: str = 'other'):
    """记一次失败，kind 用 classify_acp_error 的分类"""
    d = load()
    k = _key(route, model)
    d['bad'][k] = {'ts': int(time.time()), 'kind': kind or 'other'}
    save(d)


def _is_dead(entry: dict, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    kind = entry.get('kind') or 'other'
    ts = float(entry.get('ts') or 0)
    if kind in PERMANENT:
        return True
    if kind == 'balance':
        return (now - ts) < BALANCE_TTL
    if kind == 'auth':
        return (now - ts) < BALANCE_TTL
    return False


def rank(candidates):
    """按记忆给候选排序，返回新列表

    candidates: [(value, name), ...]，value 形如 '["opencode-go","grok-4.7"]'
    """
    d = load()
    ok = d.get('ok') or {}
    bad = d.get('bad') or {}
    now = time.time()

    def route_of(value: str) -> str:
        return (value or '').split('"')[1] if '"' in (value or '') else ''

    def score(item):
        value, name = item
        k = _key(route_of(value), name)
        if k in ok:
            return (0, -float(ok[k]))          # 成功过的排最前，越近越前
        be = bad.get(k)
        if be and _is_dead(be, now):
            return (2, 0.0)                    # 已知失效，沉底
        if be:
            return (1, 0.0)                    # 失败过但可能恢复，中间
        return (1, 1.0)                        # 没试过的居中

    return sorted(list(candidates or []), key=score)


def summary() -> str:
    """给日志/界面用的一行摘要"""
    d = load()
    ok = d.get('ok') or {}
    bad = d.get('bad') or {}
    now = time.time()
    good = [k for k in ok]
    dead = [k for k, v in bad.items() if _is_dead(v, now)]
    txt = '已记住可用 {} 个'.format(len(good))
    if dead:
        txt += '、失效 {} 个'.format(len(dead))
    if good:
        txt += ' ｜ 最近可用: ' + ', '.join(
            sorted(good, key=lambda k: -float(ok[k]))[:2])
    return txt


def clear():
    try:
        p = path()
        if p.exists():
            p.unlink()
        return True
    except Exception:
        return False
