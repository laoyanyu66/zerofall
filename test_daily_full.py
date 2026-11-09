# -*- coding: utf-8 -*-
"""每日首个完整快照 逻辑测试

不启动 GUI，用最小替身验证：
  A. 首次进入 -> 创建完整快照 + 记录日期
  B. 同一天再进入 -> 不重复创建
  C. 状态文件丢失但库里有当天完整快照 -> 跳过且补状态
  D. 跨天 -> 新建
  E. 开关关闭 -> 不创建
  F. 创建失败 -> 不写状态（下次还会重试）
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


# ── 替身 ────────────────────────────────────────────────────
class FakeSnap:
    def __init__(self, sid, is_full, date):
        self.snapshot_id = sid
        self.is_full = is_full
        self.is_crashed = False
        self.created_at = date + ' 09:00:00'
        self.archive_path = ''
        self.size_mb = 100.0


class FakeSnapshotManager:
    def __init__(self):
        self.full = []          # list[FakeSnap]
        self.created = []       # 每次创建记录
        self.fail_next = False

    def create_snapshot(self, notes='', force_healthy=False, include_runtime=False):
        if self.fail_next:
            self.fail_next = False
            return None
        sid = 'snap-{}'.format(len(self.created) + 1)
        self.created.append({'id': sid, 'notes': notes, 'include_runtime': include_runtime,
                             'force_healthy': force_healthy})
        if include_runtime:
            self.full.append(FakeSnap(sid, True, time.strftime('%Y-%m-%d')))
        return FakeSnap(sid, include_runtime, time.strftime('%Y-%m-%d'))

    def get_full_snapshots(self):
        return list(self.full)

    @staticmethod
    def snapshot_date(s):
        return str(s.created_at)[:10]


class FakeGuardian:
    @staticmethod
    def is_deepseek_running():
        return True


class FakeConfig:
    def __init__(self, d, enabled=True):
        self.snapshot_dir = d
        self.daily_full_snapshot = enabled


class FakeStatusBar:
    def showMessage(self, *a, **k):
        pass


# ── 把 MainWindow 的方法抠出来单独跑 ────────────────────────
class Harness:
    """只实现被测试用到的那几个方法（直接从 main_window.py 复制语义）"""

    def __init__(self, cfg, sm):
        self.config = cfg
        self.snapshot_manager = sm
        self.guardian = FakeGuardian()
        self._daily_full_busy = False
        self._snapshot_busy = False

    # ↓ 直接复用真实实现（用 exec 从源码里取，保证测的是同一份逻辑）
    pass


def load_real_methods():
    """从 ui/main_window.py 抽出 _ensure_daily_full_snapshot 等方法源码"""
    src = (Path(__file__).parent / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    names = ['_daily_full_state_path', '_get_last_daily_full_date',
             '_set_last_daily_full_date', '_has_full_snapshot_on',
             '_ensure_daily_full_snapshot']
    import ast
    tree = ast.parse(src)
    lines = src.splitlines()
    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            start = node.lineno - 1
            end = node.end_lineno
            chunks.append((node.lineno, '\n'.join(lines[start:end])))
    chunks.sort()
    import textwrap
    return '\n\n'.join(textwrap.dedent(c[1]) for c in chunks)


class QTimerStub:
    @staticmethod
    def singleShot(ms, fn):
        pass


class PathStub:
    pass


def build_harness(cfg, sm):
    code = load_real_methods()
    ns = {
        'Path': Path,
        'json': json,
        'time': time,
        'QTimer': QTimerStub,
        'ZeroFallLogger': type('L', (), {
            'info': staticmethod(lambda *a, **k: None),
            'warning': staticmethod(lambda *a, **k: None),
            'error': staticmethod(lambda *a, **k: None),
        }),
    }
    exec(compile(code, '<main_window_slice>', 'exec'), ns)

    class H:
        pass
    for n in ('_daily_full_state_path', '_get_last_daily_full_date',
              '_set_last_daily_full_date', '_has_full_snapshot_on',
              '_ensure_daily_full_snapshot'):
        setattr(H, n, ns[n])
    h = H()
    h.config = cfg
    h.snapshot_manager = sm
    h.guardian = FakeGuardian()
    h._daily_full_busy = False
    h.statusBar = lambda: FakeStatusBar()
    return h


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_daily_'))
    today = time.strftime('%Y-%m-%d')

    # ═══ A. 首次进入 ═══
    print('=== A. 首次进入 -> 创建完整快照 ===')
    sm = FakeSnapshotManager()
    cfg = FakeConfig(str(root))
    h = build_harness(cfg, sm)
    chk(h._get_last_daily_full_date() == '', '初始无状态记录')
    made = h._ensure_daily_full_snapshot(True)
    chk(made is True, '创建了完整快照')
    chk(len(sm.created) == 1, '只创建 1 次')
    chk(sm.created[0]['include_runtime'] is True, 'include_runtime=True（含运行时）')
    chk('每日首次启动' in sm.created[0]['notes'], '备注标明每日首次启动')
    chk(h._get_last_daily_full_date() == today, '状态记录今天的日期')
    state_file = root / '.daily_full_state.json'
    chk(state_file.exists(), '状态文件已落盘')

    # ═══ B. 同一天再进入 ═══
    print()
    print('=== B. 同一天再进入 -> 不重复 ===')
    h2 = build_harness(cfg, sm)
    chk(h2._get_last_daily_full_date() == today, '新实例读到今天的记录')
    made2 = h2._ensure_daily_full_snapshot(True)
    chk(made2 is False, '不重复创建')
    chk(len(sm.created) == 1, '创建次数仍是 1')

    # ═══ C. 状态丢失但库里有 ═══
    print()
    print('=== C. 状态文件丢失 + 库里已有当天完整快照 -> 跳过并补状态 ===')
    state_file.unlink()
    h3 = build_harness(cfg, sm)
    chk(h3._get_last_daily_full_date() == '', '状态确实丢了')
    made3 = h3._ensure_daily_full_snapshot(True)
    chk(made3 is False, '检测到库里已有，不重复创建')
    chk(len(sm.created) == 1, '创建次数仍是 1')
    chk(h3._get_last_daily_full_date() == today, '状态被补回来')

    # ═══ D. 跨天 ═══
    print()
    print('=== D. 跨天 -> 新建 ===')
    h3._set_last_daily_full_date('2020-01-01')
    sm.full.clear()                       # 库里没有今天的
    made4 = h3._ensure_daily_full_snapshot(True)
    chk(made4 is True, '昨天记录 -> 今天会新建')
    chk(len(sm.created) == 2, '创建次数变 2')
    chk(h3._get_last_daily_full_date() == today, '状态更新为今天')

    # ═══ E. 开关关闭 ═══
    print()
    print('=== E. 开关关闭 -> 不创建 ===')
    sm2 = FakeSnapshotManager()
    cfg_off = FakeConfig(str(root / 'off'), enabled=False)
    h4 = build_harness(cfg_off, sm2)
    made5 = h4._ensure_daily_full_snapshot(True)
    chk(made5 is False, '开关关闭时不创建')
    chk(len(sm2.created) == 0, '没有任何创建')

    # ═══ F. 创建失败不写状态 ═══
    print()
    print('=== F. 创建失败 -> 不写状态，下次重试 ===')
    sm3 = FakeSnapshotManager()
    cfg3 = FakeConfig(str(root / 'fail'))
    h5 = build_harness(cfg3, sm3)
    sm3.fail_next = True
    made6 = h5._ensure_daily_full_snapshot(True)
    chk(made6 is False, '创建失败返回 False')
    chk(h5._get_last_daily_full_date() == '', '失败时不写状态（不会误判已完成）')
    made7 = h5._ensure_daily_full_snapshot(True)
    chk(made7 is True, '下次进入会重试并成功')
    chk(h5._get_last_daily_full_date() == today, '成功后写入状态')

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
