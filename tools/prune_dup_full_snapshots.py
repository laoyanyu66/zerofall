# -*- coding: utf-8 -*-
"""一次性清理：删掉 6 份"同一运行时的重复完整快照"（本地副本，云端 .enc 已有）

背景：去重上线前，定时完整快照每 30 分钟带一份 227 MB 的 primary-runtime，
攒下 10 份 165-171 MB 的重复完整快照（1.64 GB）。现在运行时没变就不带了，
这些老包纯属占盘，而其中 6 份在云端已有加密副本（E:\BaiduSyncdisk\zerofall-snapshots）。

安全约束：
  * 只删"云端有 .enc 副本"的
  * 保留最新 2 份带运行时的（114316 / 111210）和 2 份云端没有的（232842 / 100934）
  * 走 SnapshotManager.delete_snapshot()，同步更新 snapshot_index.json，不留孤儿条目
必须先把 ZeroFall 关掉再跑，否则两边各写一份索引会打架。
"""
import sys
import os
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from modules.snapshot_manager import SnapshotManager  # noqa: E402

SNAP_DIR = r"D:\dsh备份"
CLOUD_DIR = r"E:\BaiduSyncdisk\zerofall-snapshots"
TARGETS = [
    "snapshot_20261112_073305",
    "snapshot_20261112_080439",
    "snapshot_20261112_083615",
    "snapshot_20261112_090634",
    "snapshot_20261112_093834",
    "snapshot_20261112_104034",
]


def main():
    cfg = SimpleNamespace(
        snapshot_dir=SNAP_DIR,
        max_light_snapshots=48,
        max_full_snapshots=12,
        silent_snapshot_interval_minutes=10,
        full_snapshot_interval_minutes=30,
    )
    sm = SnapshotManager(cfg)
    cloud = Path(CLOUD_DIR)
    freed = 0.0
    for sid in TARGETS:
        enc = cloud / (sid + ".zip.enc")
        if not enc.exists():
            print("  跳过（云端没有副本）: {}".format(sid))
            continue
        data = sm.snapshots.get(sid)
        size = 0.0
        if data:
            try:
                size = Path(data.archive_path).stat().st_size / (1024 ** 3)
            except OSError:
                size = 0.0
        ok = sm.delete_snapshot(sid)
        print("  {} {}  {:.2f} GB".format("删除" if ok else "失败", sid, size))
        if ok:
            freed += size
    print("合计释放约 {:.2f} GB".format(freed))
    print("剩余快照 {} 份".format(len(sm.snapshots)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
