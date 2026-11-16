# -*- coding: utf-8 -*-
"""豆包接力（modules/doubao_relay.py）的回归测试

不真的把消息发给豆包：所有"按键/发送"都用 dry_run 或 send_keys=False 走一遍，
只验证文案拼装、可执行文件发现、剪贴板读写与失败兜底不会抛异常。

唯一会动真实系统的地方是剪贴板 —— 测完把原内容放回去。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("ZF_DATA_DIR", tempfile.mkdtemp(prefix="zf_doubao_"))

from modules import doubao_relay as dr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def main():
    print("A. 文案拼装")
    p = dr.build_prompt(exit_code=3758096392, crash_log=r"C:\x\logs\crash-a.log",
                        crash_dir=r"C:\x\logs", profile="desktop")
    check("含用户原话", "dsh又崩溃了，帮我分析一下是什么原因，并帮我修复优化一下" in p)
    check("含退出码", "3758096392" in p)
    check("含崩溃日志路径", "crash-a.log" in p)
    check("含 profile", "desktop" in p)
    check("缺日志时有占位", "未找到最新的崩溃日志" in dr.build_prompt(exit_code=1))

    weird = dr.build_prompt(exit_code=1, template="只有 {bad_placeholder}")
    check("坏模板不抛异常", isinstance(weird, str) and weird)

    print("B. 找豆包")
    exe = dr.find_exe()
    check("能定位豆包可执行文件", exe is not None and Path(exe).is_file(), exe)
    check("配置优先：给了不存在的路径会退回自动查找",
          dr.find_exe(r"C:\nope\Doubao.exe") is not None)
    check("is_running 返回布尔", isinstance(dr.is_running(), bool))

    print("C. 窗口探测不抛")
    hwnd = dr.find_main_window()
    check("find_main_window 返回 None 或句柄", hwnd is None or isinstance(hwnd, int), hwnd)
    if dr.is_running():
        check("豆包在跑时能拿到主窗口", isinstance(hwnd, int), hwnd)

    print("D. 剪贴板往返（测完还原）")
    original = dr.get_clipboard_text()
    marker = "zf-doubao-relay-test-测试"
    ok = dr.set_clipboard(marker)
    check("写入剪贴板成功", ok)
    check("读回一致", dr.get_clipboard_text() == marker, dr.get_clipboard_text()[:40])
    dr.set_clipboard(original)
    check("已还原原剪贴板", dr.get_clipboard_text() == original)

    print("E. dry-run 全流程")
    rep = dr.relay(exit_code=1234, crash_log="crash.log", dry_run=True)
    check("dry-run ok", rep["ok"] is True, rep)
    check("dry-run 有提示词", "dsh又崩溃了" in rep["prompt"])
    check("dry-run 记录了 exe", "exe" in rep)
    check("dry-run 不按键（steps 说明）", any("dry-run" in s for s in rep["steps"]), rep["steps"])

    print("F. 不按键模式：只放剪贴板")
    original = dr.get_clipboard_text()
    rep2 = dr.relay(exit_code=9, crash_dir="C:/logs", send_keys=False)
    check("send_keys=False 时不抛", isinstance(rep2, dict))
    if rep2.get("hwnd"):
        check("提示词已进剪贴板", "dsh又崩溃了" in dr.get_clipboard_text())
    else:
        check("没窗口时也不崩", "error" in rep2)
    dr.set_clipboard(original)

    print("G. 找不到豆包时优雅失败")
    rep3 = dr.relay(exit_code=1, exe_hint=r"C:\definitely\not\here.exe", dry_run=True)
    check("dry-run 仍 ok", rep3["ok"] is True)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
