# -*- coding: utf-8 -*-
"""主控/子控体系总验收（不跑真实 ACP 与 pyinstaller，快）"""
import subprocess
import sys
from pathlib import Path

# 子进程输出里可能含无法解码的字节（errors='replace' 会变成 U+FFFD），
# 而本进程 stdout 在 Windows 下是 GBK，直接 print 会 UnicodeEncodeError。
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).parent
SUITES = [
    ('通道/更新包/版本 单元测试', 'test_control_e2e.py'),
    ('主控调度闭环（修复->打包->发布->自更新）', 'test_closeloop.py'),
]

def run(name, script):
    print()
    print('=' * 66)
    print('  ' + name)
    print('=' * 66)
    p = subprocess.run([sys.executable, str(HERE / script)], cwd=str(HERE),
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=600)
    out = (p.stdout or '') + (p.stderr or '')
    tail = [l for l in out.splitlines() if l.strip()][-6:]
    for l in tail:
        try:
            print('  ' + l)
        except UnicodeEncodeError:
            print('  ' + l.encode('utf-8', 'replace').decode('ascii', 'replace'))
    return p.returncode == 0

if __name__ == '__main__':
    results = [(n, run(n, s)) for n, s in SUITES]
    print()
    print('=' * 66)
    print('  总验收结果')
    print('=' * 66)
    for n, ok in results:
        print('  {}  {}'.format('[PASS]' if ok else '[FAIL]', n))
    sys.exit(0 if all(ok for _, ok in results) else 1)
