# -*- coding: utf-8 -*-
"""ZeroFall 优化包部署：备份 → 落文件 → 自检 → 出声。

用法
----
    python deploy_optimize.py             # 全量：备份 + 自检
    python deploy_optimize.py --backup    # 只备份，不动文件
    python deploy_optimize.py --verify    # 只跑校验（不备份）
    python deploy_optimize.py --rollback <备份目录>

为什么不是 .ps1
---------------
本机 PowerShell 里 `&&`/`||` 存在、但 python 退出码要手动接；更关键的是
这些优化本身是 python 侧的（patch_io / crash_dump / 自检），用 python 驱动
一次跑完更省事，也不需要管理员。

改了什么
--------
    新增  modules/patch_io.py       写入互斥 + 写后自校验（回滚）
    新增  modules/crash_dump.py     renderer 崩溃现场采集
    改    modules/patch_guard.py    修复写入走 patch_io（带校验）
    改    modules/resume_guard.py   patch.yml 写入走 patch_io + LF 固定
    改    modules/guardian.py       启动前补丁体检 / ZF_WATCH_RENDERER 观测位
    改    ui/main_window.py         崩溃分类自愈分支 + 现场采集挂载
    改    selfcheck.py              新增 [11] 补丁文件可解析性

回滚
----
    备份目录里保存了改动前的原文件（相对路径保持），
    `python deploy_optimize.py --rollback .backup/optimize-bak-<时间戳>` 即可还原。
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: 本优化包真正动过的文件（相对 ROOT）。别用 rglob —— 备份时把 node_modules/
#: vendor/ 一起拷进来会慢到没法用，且那不是"我改的东西"。
TOUCHED = [
    "modules/patch_io.py",
    "modules/crash_dump.py",
    "modules/patch_guard.py",
    "modules/resume_guard.py",
    "modules/guardian.py",
    "ui/main_window.py",
    "selfcheck.py",
]

CHECKS = [
    ("语法编译", [sys.executable, "-m", "compileall", "-q", "modules", "ui", "selfcheck.py"]),
    ("patch_io 自测", [sys.executable, "modules/patch_io.py"]),
    ("patch_guard 自测", [sys.executable, "modules/patch_guard.py", "--self-test"]),
    ("crash_dump 自测", [sys.executable, "modules/crash_dump.py"]),
    ("补丁文件只读检查", [sys.executable, "modules/patch_guard.py", "--check"]),
    ("项目自检", [sys.executable, "selfcheck.py"]),
]


def backup_root() -> Path:
    return ROOT / ".backup" / "optimize-bak-{}".format(
        time.strftime("%Y%m%d-%H%M%S"))


def do_backup() -> Path:
    dst = backup_root()
    n = 0
    for rel in TOUCHED:
        src = ROOT / rel
        if not src.exists():
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        n += 1
    print("备份 {} 个文件 → {}".format(n, dst))
    return dst


def rollback(src_dir: str) -> int:
    src = Path(src_dir)
    if not src.is_absolute():
        src = ROOT / src
    if not src.is_dir():
        print("找不到备份目录：{}".format(src))
        return 2
    restored = 0
    for rel in TOUCHED:
        f = src / rel
        if not f.exists():
            continue
        dst = ROOT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        restored += 1
        print("  还原 {}".format(rel))
    print("已还原 {} 个文件；重启 ZeroFall 生效".format(restored))
    return 0


def run_checks() -> int:
    failed = []
    for name, cmd in CHECKS:
        print("\n── {} ──".format(name))
        proc = subprocess.run(cmd, cwd=str(ROOT))
        if proc.returncode != 0:
            failed.append(name)
    print("\n" + "=" * 60)
    if failed:
        print("失败 {} 项：{}".format(len(failed), "、".join(failed)))
        return 1
    print("全部通过（{} 项）".format(len(CHECKS)))
    print("观测开关：$env:ZF_WATCH_RENDERER=\"1\" 后启动 ZeroFall，即带 --disable-gpu")
    print("回滚：python deploy_optimize.py --rollback <备份目录>")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ZeroFall 优化包部署")
    parser.add_argument("--backup", action="store_true", help="只备份")
    parser.add_argument("--verify", action="store_true", help="只校验")
    parser.add_argument("--rollback", default="", help="从备份目录还原")
    args = parser.parse_args()

    if args.rollback:
        return rollback(args.rollback)
    if args.backup:
        do_backup()
        return 0
    if args.verify:
        return run_checks()

    do_backup()
    return run_checks()


if __name__ == "__main__":
    sys.exit(main())
