# -*- coding: utf-8 -*-
"""主控闭环验证：模拟子控上报 -> 主控 tick 处理 -> 更新包发布 -> 自更新计划

不真的重打包（耗时），用 dry-run 模式验证调度链路：
  * 主控能读到子控报告
  * ACP 修复环节被触发（用假 ACP 替身，验证参数组装）
  * 更新包能发布
  * 主控自更新能被安排
"""
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

WORK = Path(tempfile.mkdtemp(prefix='zf_closeloop_'))
SRC = Path(__file__).resolve().parent.parent
CHAN = WORK / 'channel'

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


# 造一个假的 ACP 客户端，验证主控传给它的 prompt 内容是否正确
CAPTURED = {}


class FakeAcp:
    def __init__(self, profile=None, cwd=None, timeout=None):
        if profile is not None:
            CAPTURED['profile'] = profile
        if cwd is not None:
            CAPTURED['cwd'] = cwd

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def initialize(self):
        return {}

    def new_session(self, cwd=None):
        return 'fake-session', [{'id': 'model', 'options': [
            {'group': 'g', 'options': [{'value': '["opencode-go","deepseek-v4.1-flash"]', 'name': 'M'}]}]}]

    def set_model(self, sid, value):
        CAPTURED['model'] = value
        return {}

    def prompt(self, sid, text, timeout=None):
        CAPTURED['prompt'] = text
        # 模拟 agent 真的改了一个文件
        target = Path(CAPTURED['cwd']) / 'modules' / 'sample_buggy.py'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('# fixed\n', encoding='utf-8')
        return {'stopReason': 'end_turn'}, '已修复'


def main():
    import modules.acp_client as acp_mod
    from modules.control import ControlChannel, AgentNode
    from modules.master import MasterNode
    import modules.master as master_mod

    original_acp = master_mod.FIX_BUILD_CMD
    master_mod.FIX_BUILD_CMD = ['cmd', '/c', 'echo', 'fake-build']   # 不真打包

    # 打桩 ACP
    def fake_acp_factory(profile=None, cwd=None, timeout=None):
        return FakeAcp(profile, cwd)
    master_mod.FIX_BUILD_CMD = original_acp

    print('=== 1. 子控上报 ===')

    class AgentCfg:
        role = 'agent'
        agent_id = 'agent-closeloop'
        control_channel_dir = str(CHAN)
        report_to_master = True
        report_zf_error = True
        report_dsh_crash = True
        auto_apply_update = True
        snapshot_dir = str(WORK / 'snap')
        app_version = '1.0.0.0'

    agent = AgentNode(AgentCfg())
    rid = agent.report_zf_error(
        'ZeroDivisionError: division by zero at modules/sample_buggy.py divide()')
    chk(bool(rid), '子控报告已写入 (id={})'.format(rid))

    print()
    print('=== 2. 主控 tick 处理 ===')
    src_copy = WORK / 'src'
    shutil.copytree(SRC, src_copy, ignore=shutil.ignore_patterns(
        'build', 'dist', '__pycache__', 'logs', '.git', '*.pyc'))

    class MasterCfg:
        role = 'master'
        control_channel_dir = str(CHAN)
        master_source_dir = str(src_copy)
        acp_profile = 'zf-acp'
        auto_fix_enabled = True
        master_self_update = True
        app_version = '1.0.0.0'

    master = MasterNode(MasterCfg())

    # 用假 ACP 替换真实调用
    def fake_run_fix(self, doc):
        payload = doc.get('payload') or {}
        from modules.master import (extract_file_hints, snapshot_py_hashes,
                                   diff_py_hashes, FIX_PROMPT)
        hints = extract_file_hints(payload.get('error', ''), self.source_dir)
        cand = '\n'.join('- {}'.format(h) for h in hints) or '- (无)'
        prompt = FIX_PROMPT.format(source_root=str(self.source_dir),
                                   report=json.dumps(payload, ensure_ascii=False)[:500],
                                   candidates=cand)
        CAPTURED['prompt'] = prompt
        CAPTURED['cwd'] = str(self.source_dir)
        before = snapshot_py_hashes(self.source_dir)
        FakeAcp().prompt('s', prompt)
        after = snapshot_py_hashes(self.source_dir)
        changed, added = diff_py_hashes(before, after)
        return {'ok': bool(changed or added),
                'changed_files': changed + added,
                'summary': 'fake', 'model': 'fake'}

    master_mod.MasterNode.run_fix = fake_run_fix

    # 假打包：直接产出一个 exe 文件
    def fake_rebuild(self):
        exe = self.source_dir / 'dist' / 'ZeroFall.exe'
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b'FAKE EXE ' * 100)
        return {'ok': True, 'exe': str(exe)}
    master_mod.MasterNode.rebuild_exe = fake_rebuild

    # 假自更新：只记录不真替换
    def fake_self_update(self, pkg, version):
        return {'ok': True, 'message': 'dry-run 自更新已安排 v' + version}
    master_mod.MasterNode.apply_self_update = fake_self_update

    result = master.tick()
    chk(result is not None, '主控 tick 产出了处理结果')
    if result:
        if result.get('traceback'):
            print()
            print('!!! 异常 traceback !!!')
            print(result['traceback'])
            print()
        print('  结果:', json.dumps({k: v for k, v in result.items() if k != 'traceback'},
                                     ensure_ascii=False)[:400])

        fix = result.get('fix') or {}
        chk(fix.get('ok'), '修复环节成功')
        chk(len(fix.get('changed_files') or []) == 1, '检测到 1 个文件改动')

        prompt = CAPTURED.get('prompt', '')
        chk(str(src_copy) in prompt, 'prompt 含源码目录绝对路径')
        chk('sample_buggy.py' in prompt, 'prompt 含候选文件绝对路径')
        chk('除数' not in prompt, 'prompt 结构正常')

        build = result.get('build') or {}
        chk(build.get('ok'), '重新打包环节成功')

        pkg = result.get('package') or {}
        chk(pkg.get('version') == '1.0.0.1', '版本号递增为 ' + str(pkg.get('version')))
        chk(Path(pkg.get('path', '')).exists(), '更新包已生成')

        chk(result.get('published') is True, '更新已发布到通道')
        idx = ControlChannel(CHAN).check_update()
        chk(idx and idx.get('version') == '1.0.0.1', '通道索引指向新版本')

        su = result.get('self_update') or {}
        chk(su.get('ok'), '主控自更新已安排: ' + str(su.get('message')))

    print()
    print('=== 3. 报告已标记处理 ===')
    chk(len(ControlChannel(CHAN).list_reports(only_new=True)) == 0, '报告被标记为已处理')

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
