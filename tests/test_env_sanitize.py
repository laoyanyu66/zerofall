# -*- coding: utf-8 -*-
"""PyInstaller onefile 环境隔离的回归测试

事故：ZeroFall（onefile）运行中环境里带着 _PYI_PARENT_PROCESS_LEVEL 等簿记变量，
启动 DSH 时被继承；再从那条链启动 ZeroFall，bootloader 以为自己是子进程、
去找已退出的父 PID，弹：
    Security validation failure: invalid originating onefile parent process (PID not found)!

守卫三条：
  1. strip_pyinstaller_env() 能从当前进程环境里清掉这些键
  2. sanitized_child_env() 给子进程的 env 里没有这些键（其它变量保持）
  3. guardian 启动 DSH、main 启动时确实调用了它们（静态检查，防止以后被删掉）
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_env_'))

import utils                                          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


PYI_KEYS = ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE",
            "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS", "_MEIPASS2")

print("== 1. sanitized_child_env ==")
os.environ["_PYI_PARENT_PROCESS_LEVEL"] = "1"
os.environ["_PYI_ARCHIVE_FILE"] = r"C:\x\ZeroFall.exe"
os.environ["_MEIPASS"] = r"C:\Temp\_MEI123"
os.environ["ZF_MARKER"] = "kept"
env = utils.sanitized_child_env()
check("子进程 env 不含 _PYI_*", not any(k.startswith("_PYI_") for k in env), [k for k in env if k.startswith('_PYI_')])
check("子进程 env 不含 _MEIPASS", "_MEIPASS" not in env)
check("其它变量保留", env.get("ZF_MARKER") == "kept")
check("PATH 保留", "PATH" in env or "Path" in env)
check("不修改当前进程环境", os.environ.get("ZF_MARKER") == "kept" and "_PYI_PARENT_PROCESS_LEVEL" in os.environ)

print("== 2. strip_pyinstaller_env ==")
removed = utils.strip_pyinstaller_env()
check("返回被删的键", set(removed) >= {"_PYI_PARENT_PROCESS_LEVEL", "_PYI_ARCHIVE_FILE", "_MEIPASS"}, removed)
check("当前进程里已清掉", not any(k in os.environ for k in PYI_KEYS),
      [k for k in PYI_KEYS if k in os.environ])
check("保留无关变量", os.environ.get("ZF_MARKER") == "kept")
removed2 = utils.strip_pyinstaller_env()
check("再调用一次是幂等的", removed2 == [], removed2)

print("== 3. 调用点静态检查 ==")
g = (ROOT / "modules" / "guardian.py").read_text(encoding="utf-8")
m = (ROOT / "main.py").read_text(encoding="utf-8")
check("guardian 启动 DSH 用 sanitized_child_env", "sanitized_child_env()" in g)
check("guardian 不再裸 Popen([exe_path])", "Popen([exe_path])" not in g)
check("main 启动时调用 strip_pyinstaller_env", "strip_pyinstaller_env()" in m)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
