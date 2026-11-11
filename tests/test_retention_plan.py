# -*- coding: utf-8 -*-
"""保留上限与会话目录闸门的回归测试

两个真实事故的守卫：

1. 健康快照只增不减 —— `get_overflow_snapshots()` 没有任何调用方，
   云端归档一失败就没有任何本地上限。实测：190 GB 盘被 30 份快照（83 GB）
   + 3 个 zip（20.6 GB）填到 0 字节可用，之后的快照只剩 530 字节残包。
   → `plan_retention()` 提供本地硬上限。

2. `deepseek_session_dir` 为空时 `Path('')` 落到进程 cwd。ZeroFall 开机自启
   时 cwd 是 `C:\\Windows\\System32`，于是"会话数据"把整个 System32 打进快照，
   单份 13,985 MB（msedge.dll 316 MB / MRT.exe 220 MB / DriverStore / lxss）。
   → `_resolve_session_dir()` 对空值、系统目录、盘根、cwd、超大目录一律返回 None。
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_ret_'))

from modules import snapshot_manager as sm            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def snap(sid, created, is_full=False, status=None):
    return SimpleNamespace(snapshot_id=sid, created_at=created,
                           is_full=is_full,
                           status=status or sm.SnapshotStatus.HEALTHY)


print('== plan_retention: 轻量快照上限 ==')
lights = [snap('L{}'.format(i), '20261111_09{:02d}00'.format(i)) for i in range(5)]
doomed = sm.plan_retention({s.snapshot_id: s for s in lights}, max_light=2, max_full=4)
check('5 份轻量 / 上限 2 → 删 3 份', len(doomed) == 3, doomed)
check('删的是最老的 3 份', doomed == ['L0', 'L1', 'L2'], doomed)
check('最新一份永不删', 'L4' not in doomed, doomed)

print('== plan_retention: 完整快照上限 ==')
fulls = [snap('F{}'.format(i), '20261110_0{}0000'.format(i), is_full=True) for i in range(3)]
mixed = {s.snapshot_id: s for s in lights + fulls}
doomed = sm.plan_retention(mixed, max_light=2, max_full=1)
check('完整 3 份 / 上限 1 → 删最老 2 份', sorted(x for x in doomed if x.startswith('F')) == ['F0', 'F1'], doomed)
check('轻量部分不受完整上限影响', sorted(x for x in doomed if x.startswith('L')) == ['L0', 'L1', 'L2'], doomed)

print('== plan_retention: 保护与边界 ==')
doomed = sm.plan_retention(mixed, max_light=2, max_full=1, protected_ids={'F0'})
check('protected_ids 内的快照不删', 'F0' not in doomed, doomed)
check('保护后仍尽量压到上限', len([x for x in doomed if x.startswith('F')]) == 1, doomed)
doomed = sm.plan_retention(mixed, max_light=0, max_full=0)
check('上限 0 = 不启用该类上限（不是全删）', doomed == [], doomed)
doomed = sm.plan_retention(mixed, max_light=99, max_full=99)
check('上限够大 → 不删任何东西', doomed == [], doomed)
crashed = snap('C0', '20261111_000000', status=sm.SnapshotStatus.CRASHED)
doomed = sm.plan_retention({**mixed, 'C0': crashed}, max_light=1, max_full=1)
check('崩溃快照不归 plan_retention 管', 'C0' not in doomed, doomed)

print('== _resolve_session_dir: 路径闸门 ==')
mgr = object.__new__(sm.SnapshotManager)


def resolve(session_dir, cap_mb=512):
    mgr.config = SimpleNamespace(deepseek_session_dir=session_dir,
                                 session_backup_max_mb=cap_mb)
    return mgr._resolve_session_dir()


check('空配置 → None（不再退化成 cwd）', resolve('') is None)
check('None → None', resolve(None) is None)
check('System32 → None', resolve(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')) is None)
check('盘根 → None', resolve(Path(os.environ.get('SystemDrive', 'C:') + '\\')) is None)
check('当前工作目录 → None', resolve(os.getcwd()) is None)
check('不存在的路径 → None', resolve(os.path.join(tempfile.gettempdir(), 'zf_nope_zzz_1111')) is None)

tmp = Path(tempfile.mkdtemp(prefix='zf_sess_'))
(tmp / 'a.json').write_text('{"x":1}', encoding='utf-8')
resolved = resolve(str(tmp))
check('正常会话目录 → 返回解析后的绝对路径',
      resolved is not None and resolved == tmp.resolve(), resolved)

big = Path(tempfile.mkdtemp(prefix='zf_big_'))
with open(big / 'big.bin', 'wb') as f:
    f.write(b'\0' * (2 * 1024 * 1024))
check('超上限（2 MB > 1 MB）→ None', resolve(str(big), cap_mb=1) is None)
check('未超上限（2 MB < 512 MB）→ 正常返回', resolve(str(big), cap_mb=512) is not None)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
