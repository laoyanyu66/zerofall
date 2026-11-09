# -*- coding: utf-8 -*-
"""验证 ACP agent 能否读写本地文件（自动修复的核心能力）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.acp_client import AcpClient, pick_model

WORK = Path(__file__).resolve().parent / '_acp_probe'
WORK.mkdir(exist_ok=True)
probe_file = WORK / 'acp_write_test.txt'
if probe_file.exists():
    probe_file.unlink()

TASK = (
    '请完成以下任务（只做这一件事）：\n'
    '1. 读取文件 {read}\n'
    '2. 创建文件 {write}，内容写两行：\n'
    '   line1=ACP_OK\n'
    '   line2={{"from":"acp"}}\n'
    '3. 回复"已完成"并附上你创建的文件绝对路径\n'
).format(read=str(WORK / 'sample_input.txt'), write=str(probe_file))

(WORK / 'sample_input.txt').write_text('这是给 agent 读取的样例输入文件。\n', encoding='utf-8')

print('=== 启动 ACP ===')
with AcpClient(profile='zf-acp', cwd=str(WORK)) as cli:
    cli.initialize()
    print('  initialize OK')
    sid, opts = cli.new_session(str(WORK))
    print('  session:', sid)
    models = cli.list_models(opts)
    print('  可用模型数:', len(models))
    value, name = pick_model(opts)
    print('  选定模型:', name, '=', value)
    if value:
        cli.set_model(sid, value)
        print('  set_config_option OK')
    print()
    print('=== 发任务（读+写文件）===')
    res, text = cli.prompt(sid, TASK, timeout=420)
    print('  stopReason:', res.get('stopReason'))
    print()
    print('--- agent 文本 ---')
    print(text[-1500:] if text else '(无文本)')

print()
print('=== 验证文件是否真的被创建 ===')
if probe_file.exists():
    print('  [OK] 文件已创建:')
    print('  ' + probe_file.read_text(encoding='utf-8').replace('\n', '\n  '))
else:
    print('  [FAIL] 文件未创建')
