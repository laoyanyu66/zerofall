# -*- coding: utf-8 -*-
"""只测 ACP 修复环节：给定报错 -> 源码被改 -> 改动检测生效"""
import json
import shutil
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.master import (extract_file_hints, snapshot_py_hashes,
                            diff_py_hashes, MasterNode)

# 这个用例会真的调一次 LLM（走 ACP），所以需要 API 凭据。
# 凭据来自 secret_store，而 secret_store 的位置由 ZF_DATA_DIR 决定：
#   - 外部已设 ZF_DATA_DIR（比如跑测试时导出了部署目录）→ 直接用
#   - 否则退回到本机默认部署目录 D:\zerofall（存在才用）
# 两处都拿不到 key 就 SKIP —— 这是环境缺件，不是代码坏了，
# 不该让整套测试报红。
import os as _os
if not _os.environ.get('ZF_DATA_DIR'):
    _deploy = Path(r'D:\zerofall')
    if _deploy.is_dir() and (_deploy / 'secrets.json').exists():
        _os.environ['ZF_DATA_DIR'] = str(_deploy)

try:
    from modules import secret_store as _ss
    _has_key = any(_ss.get(_k) for _k in _ss.KNOWN_KEYS)
except Exception:
    _has_key = False
if not _has_key:
    print('SKIP: 没有可用的 API 凭据（secret_store 为空）')
    print('      本用例需要真实调用一次 LLM。设置 ZF_DATA_DIR 指向含')
    print('      secrets.json 的部署目录，或 export OPENCODE_GO_API_KEY 后重跑。')
    sys.exit(0)

WORK = Path(tempfile.mkdtemp(prefix='zf_fixonly_'))
SRC = WORK / 'src'
SRC.mkdir()
BUG = SRC / 'modules'
BUG.mkdir()
(BUG / 'sample_buggy.py').write_text(
    '# -*- coding: utf-8 -*-\n'
    '"""故意写错的示例模块，用于验证自动修复链路"""\n\n\n'
    'def divide(a, b):\n'
    '    """除法（缺少除零保护，会抛异常）"""\n'
    '    return a / b\n',
    encoding='utf-8')

REPORT = {
    'signature': 'ZeroDivisionError',
    'error': 'ZeroFall 内部错误：ZeroDivisionError: division by zero at modules/sample_buggy.py divide()',
}


class Cfg:
    role = 'master'
    control_channel_dir = str(WORK / 'chan')
    master_source_dir = str(SRC)
    acp_profile = 'zf-acp'
    auto_fix_enabled = True
    master_self_update = False
    app_version = '1.0.0.0'


print('=== 1. 文件提示提取 ===')
hints = extract_file_hints(REPORT['error'], SRC)
for h in hints:
    print('  ->', h)
assert any(h.name == 'sample_buggy.py' for h in hints), '没提取到目标文件'

print()
print('=== 2. 跑 ACP 修复 ===')
m = MasterNode(Cfg())
t0 = time.time()
res = m.run_fix({'payload': REPORT})
print('  用时: {:.0f}s'.format(time.time() - t0))
print()
print(json.dumps({k: v for k, v in res.items() if k != 'summary'}, ensure_ascii=False, indent=2)[:1500])
print()
print('--- 修复说明 ---')
print((res.get('summary') or '(无)')[-1000:])

print()
print('=== 3. 文件现状 ===')
for line in (BUG / 'sample_buggy.py').read_text(encoding='utf-8').splitlines():
    print('  ' + line)

ok = res.get('ok') and res.get('changed_files')
print()
print('=== 判定:', 'PASS 修复成功且改动被检测' if ok else 'FAIL', '===')
shutil.rmtree(WORK, ignore_errors=True)
raise SystemExit(0 if ok else 1)
