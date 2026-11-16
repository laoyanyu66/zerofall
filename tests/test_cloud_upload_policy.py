# -*- coding: utf-8 -*-
"""云同步上传策略（2026-11-12）

背景：百度网盘常开着同步时，zerofall-snapshots 一天新增 94 个文件 / 1.2 GB，
同步进程常驻内存被顶到 2 GB。根因是「本地溢出 → 云归档」把每个溢出的轻量快照
都单独加密上传，绕过了 conf 里的抽样策略。

这里验证新的三道闸：
  A. 轻量溢出默认直接删本地（云端完整快照已覆盖同一时间窗）
  B. 没有更晚的完整快照覆盖时，仍然老老实实上传（安全阀）
  C. policy=archive 时行为回到"逐个上传"
  D. 每日上传上限：到顶就整轮跳过，不动本地文件
  E. 每轮上限：一轮只传 N 个，剩下的下一轮继续
  F. _cloud_uploads_today 只数今天的快照包（不含 .meta.json / 昨天的）
"""
import ast
import os
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from utils import config_int  # noqa: E402
from ui.main_window import ZeroFallMainWindow as MW  # noqa: E402

PASS, FAIL = [], []


def chk(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  {}".format(extra)))


def load_method():
    """把真实的 _handle_overflow_to_cloud 抠出来执行（它依赖不少 UI 状态）"""
    src = (Path(__file__).resolve().parent.parent / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_handle_overflow_to_cloud':
            body = '\n'.join(src.splitlines()[node.lineno - 1:node.end_lineno])
            ns = {'ZeroFallLogger': type('L', (), {
                'info': staticmethod(lambda *a, **k: None),
                'warning': staticmethod(lambda *a, **k: None),
                'error': staticmethod(lambda *a, **k: None)}),
                'Path': Path, 'config_int': config_int, 'datetime': datetime}
            exec(compile(textwrap.dedent(body), '<m>', 'exec'), ns)
            return ns['_handle_overflow_to_cloud']
    raise RuntimeError('找不到 _handle_overflow_to_cloud')


class Snap:
    def __init__(self, sid, created_at, is_full=False):
        self.snapshot_id = sid
        self.created_at = created_at
        self.is_full = is_full
        self.archive_path = str(Path(tempfile.gettempdir()) / (sid + '.zip'))


class SM:
    def __init__(self, overflow, fulls):
        self._overflow = overflow
        self._fulls = fulls
        self.deleted = []

    def prune_missing_entries(self):
        pass

    def get_overflow_snapshots(self):
        return list(self._overflow), [], []

    def get_full_snapshots(self):
        return list(self._fulls)

    def delete_snapshot(self, sid):
        self.deleted.append(sid)
        return True


class Cfg:
    cloud_overflow_enabled = True
    cloud_sync_dir = str(Path(tempfile.gettempdir()))
    cloud_sync_encrypt = False
    cloud_overflow_light_policy = 'delete'
    cloud_upload_max_per_day = 0
    cloud_upload_per_cycle = 0
    cloud_outbox_enabled = False


def make_host(method, overflow, fulls, **cfg_over):
    cfg = Cfg()
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    calls = []

    class H:
        config = cfg
        snapshot_manager = SM(overflow, fulls)
        _archive_busy = False
        _archive_fail_count = {}

        def _archive_snapshot_to_cloud(self, s, d, e):
            calls.append(s.snapshot_id)
            return True

        def _cloud_uploads_today(self):
            return getattr(cfg, '_done_today', 0)

        # 2026-11-13 云窗口：本节测的是溢出闸门，固定为"在窗口内"，
        # 行为与改动前一致（窗口外走 outbox 的逻辑由 test_cloud_window.py 覆盖）。
        def _cloud_in_window(self):
            return True

        def _cfg_int(self, field, default):
            try:
                return int(getattr(cfg, field, default))
            except Exception:
                return default

    H._handle_overflow_to_cloud = method
    return H(), calls


def main():
    method = load_method()

    print("A. 轻量溢出 + 有更晚的完整快照 → 删本地、不上传")
    lights = [Snap('L1', '20261112_100000'), Snap('L2', '20261112_101000')]
    fulls = [Snap('F1', '20261112_103000', is_full=True)]
    h, calls = make_host(method, lights, fulls)
    h._handle_overflow_to_cloud()
    chk(sorted(h.snapshot_manager.deleted) == ['L1', 'L2'], '两个轻量都删了', h.snapshot_manager.deleted)
    chk(calls == [], '一个都没上传', calls)

    print("B. 安全阀：没有更晚的完整快照 → 仍然上传")
    lights = [Snap('L1', '20261112_120000')]
    fulls = [Snap('F1', '20261112_103000', is_full=True)]   # 比 L1 更早
    h, calls = make_host(method, lights, fulls)
    h._handle_overflow_to_cloud()
    chk(calls == ['L1'], '没被覆盖 → 上传', calls)
    chk(h.snapshot_manager.deleted == ['L1'], '上传成功后才删本地（正常归档流程）',
        h.snapshot_manager.deleted)

    print("C. policy=archive → 回到逐个上传")
    lights = [Snap('L1', '20261112_100000')]
    fulls = [Snap('F1', '20261112_103000', is_full=True)]
    h, calls = make_host(method, lights, fulls, cloud_overflow_light_policy='archive')
    h._handle_overflow_to_cloud()
    chk(calls == ['L1'], '照旧上传', calls)
    chk(h.snapshot_manager.deleted == ['L1'], '上传成功后删本地', h.snapshot_manager.deleted)

    print("D. 每日上限到顶 → 整轮跳过，不动本地")
    lights = [Snap('L1', '20261112_100000')]
    fulls = [Snap('F1', '20261112_103000', is_full=True)]
    h, calls = make_host(method, lights, fulls, cloud_overflow_light_policy='archive',
                         cloud_upload_max_per_day=5)
    h.config._done_today = 5
    h._handle_overflow_to_cloud()
    chk(calls == [] and h.snapshot_manager.deleted == [], '到顶不传也不删')

    print("E. 每轮上限：5 个溢出只传 2 个")
    lights = [Snap('L{}'.format(i), '20261112_09{:02d}00'.format(i)) for i in range(5)]
    fulls = [Snap('F1', '20261112_235959', is_full=True)]
    h, calls = make_host(method, lights, fulls, cloud_overflow_light_policy='archive',
                         cloud_upload_per_cycle=2)
    h._handle_overflow_to_cloud()
    chk(len(calls) == 2, '只传 2 个', calls)
    chk(len(h.snapshot_manager.deleted) == 2, '对应删 2 个', h.snapshot_manager.deleted)

    print("F. _cloud_uploads_today 只数今天的包")
    root = Path(tempfile.mkdtemp(prefix='zf_cup_'))
    today = datetime.now()
    for name in ('snapshot_20261112_100000.zip.enc', 'snapshot_20261112_110000.zip.enc'):
        p = root / name
        p.write_bytes(b'x')
        os.utime(p, None)
    meta = root / 'snapshot_20261112_100000.zip.meta.json'
    meta.write_bytes(b'{}')
    os.utime(meta, None)
    old = root / 'snapshot_20261111_100000.zip.enc'
    old.write_bytes(b'x')
    stamp = (today - timedelta(days=1)).timestamp()
    os.utime(old, (stamp, stamp))

    class HC:
        config = type('C', (), {'cloud_sync_dir': str(root)})()

    n = MW._cloud_uploads_today(HC())
    chk(n == 2, '只数今天的 2 个 .enc（排除 .meta.json 和昨天的）', n)

    print()
    print("结果：{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
