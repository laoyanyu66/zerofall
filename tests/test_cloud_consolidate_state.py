# -*- coding: utf-8 -*-
"""云端每日收敛的"已收敛日期"落盘（2026-11-12）

原来只记在内存：程序每重启一次就把昨天的云端快照重扫一遍、再删一轮，
实测一天 159 行收敛日志、12 次重复扫描。这里验证落盘后的行为。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ui.main_window import ZeroFallMainWindow as MW  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  {}".format(extra)))


class Host:
    _cloud_consolidated_path = MW._cloud_consolidated_path
    _load_cloud_consolidated = MW._load_cloud_consolidated
    _save_cloud_consolidated = MW._save_cloud_consolidated

    def __init__(self, snap_dir):
        self.config = SimpleNamespace(snapshot_dir=str(snap_dir))


def main():
    root = Path(tempfile.mkdtemp(prefix="zf_ccon_"))
    h = Host(root)

    print("A. 空目录")
    check("没有记录时返回空集合", h._load_cloud_consolidated() == set())
    check("路径在快照目录下", h._cloud_consolidated_path().parent == root,
          h._cloud_consolidated_path())

    print("B. 写读往返")
    h._save_cloud_consolidated({"20261111", "20261112"})
    got = h._load_cloud_consolidated()
    check("两个日期都读回来", got == {"20261111", "20261112"}, got)
    raw = json.loads((root / ".cloud_consolidated.json").read_text(encoding="utf-8"))
    check("落盘是 JSON 数组且有序", raw == ["20261111", "20261112"], raw)

    print("C. 跨实例（等价于重启程序）")
    h2 = Host(root)
    check("新实例读到同一份记录", "20261111" in h2._load_cloud_consolidated())

    print("D. 去重 + 上限 60 天")
    h._save_cloud_consolidated({"20260101", "20260101", "20260102"})
    check("重复日期只留一个", h._load_cloud_consolidated() == {"20260101", "20260102"})
    many = {"2026{:04d}".format(i) for i in range(1, 80)}
    h._save_cloud_consolidated(many)
    kept = h._load_cloud_consolidated()
    check("最多保留 60 条", len(kept) == 60, len(kept))
    newest = "2026{:04d}".format(79)
    oldest = "2026{:04d}".format(1)
    check("保留的是最新的 60 条", newest in kept and oldest not in kept,
          (newest in kept, oldest in kept))

    print("E. 坏文件不崩")
    (root / ".cloud_consolidated.json").write_text("{ 不是数组", encoding="utf-8")
    check("坏 JSON → 空集合", h._load_cloud_consolidated() == set())

    print()
    print("结果：{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  - " + n)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
