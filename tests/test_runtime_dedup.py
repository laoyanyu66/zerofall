# -*- coding: utf-8 -*-
"""定时完整快照的运行时去重（2026-11-12）

只测 _full_snapshot_needs_runtime 这一个判据：
  always    -> 永远带
  never     -> 永远不带
  on_change -> 同一 .dsh_version 第二次起不带；版本一变又带
没有 .dsh_version 时保守带（宁多占盘也别缺运行时）。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.main_window import ZeroFallMainWindow as MW  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  {}".format(extra)))


class Host:
    """只借真实方法本体，不建整个主窗口"""
    _dsh_version_marker = MW._dsh_version_marker
    _runtime_signature = MW._runtime_signature
    _full_snapshot_needs_runtime = MW._full_snapshot_needs_runtime

    def __init__(self, config):
        self.config = config


def make_host(snapshot_dir, config_dir, mode):
    return Host(SimpleNamespace(snapshot_dir=str(snapshot_dir),
                                deepseek_config_dir=str(config_dir),
                                full_snapshot_runtime_mode=mode))


def call(host):
    return MW._full_snapshot_needs_runtime(host)


def main():
    root = Path(tempfile.mkdtemp(prefix="zf_rt_"))
    cfg = root / "runtime"
    cfg.mkdir()
    snaps = root / "snaps"
    snaps.mkdir()

    print("A. always / never")
    check("always → True", call(make_host(snaps, cfg, "always")) is True)
    check("never → True 不动盘", call(make_host(snaps, cfg, "never")) is False)
    check("always 不写状态文件", not (snaps / ".runtime_in_full.json").exists())

    print("B. on_change：没有版本号时保守带")
    check("无 .dsh_version → True", call(make_host(snaps, cfg, "on_change")) is True)
    check("确实写了状态文件", (snaps / ".runtime_in_full.json").exists())

    print("C. on_change：同版本第二次开始不带")
    (cfg / ".dsh_version").write_text("1.6.0\n", encoding="utf-8")
    first = call(make_host(snaps, cfg, "on_change"))
    check("版本首次出现 → True", first is True)
    state = json.loads((snaps / ".runtime_in_full.json").read_text(encoding="utf-8"))
    check("状态里记了版本", state.get("version") == "1.6.0", state)
    check("同版本再来 → False", call(make_host(snaps, cfg, "on_change")) is False)
    check("第三次仍是 False", call(make_host(snaps, cfg, "on_change")) is False)

    print("D. 版本升级 → 又带一次")
    (cfg / ".dsh_version").write_text("1.6.1\n", encoding="utf-8")
    check("版本变了 → True", call(make_host(snaps, cfg, "on_change")) is True)
    check("升级后同版本 → False", call(make_host(snaps, cfg, "on_change")) is False)

    print("E. 坏状态文件不崩")
    (snaps / ".runtime_in_full.json").write_text("{ not json", encoding="utf-8")
    check("坏 JSON → 退回带运行时", call(make_host(snaps, cfg, "on_change")) is True)

    print("F. 版本标记在快照目录也算（实测位置就是 <snapshot_dir>\.dsh_version）")
    (cfg / ".dsh_version").unlink()
    (snaps / ".dsh_version").write_text("0.2.2-rc.1\n", encoding="utf-8")
    check("快照目录的标记能被读到", call(make_host(snaps, cfg, "on_change")) is True)
    check("同一版本第二次 → False", call(make_host(snaps, cfg, "on_change")) is False)
    state = json.loads((snaps / ".runtime_in_full.json").read_text(encoding="utf-8"))
    check("状态里 signature = 版本号", state.get("signature") == "0.2.2-rc.1", state)

    print("G. 没有版本标记时退回运行时指纹（文件数+字节）")
    (snaps / ".dsh_version").unlink()
    (snaps / ".runtime_in_full.json").unlink()
    rt = cfg / "primary-runtime"
    (rt / "node" / "bin").mkdir(parents=True)
    (rt / "node" / "bin" / "node.exe").write_bytes(b"x" * 1024)
    check("首次见到运行时 → True", call(make_host(snaps, cfg, "on_change")) is True)
    check("运行时没变 → False", call(make_host(snaps, cfg, "on_change")) is False)
    (rt / "node" / "bin" / "npm.cmd").write_bytes(b"y" * 512)
    check("运行时里新增文件 → True", call(make_host(snaps, cfg, "on_change")) is True)
    check("新增后再问 → False", call(make_host(snaps, cfg, "on_change")) is False)

    print()
    print("结果：{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  - " + n)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
