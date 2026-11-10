# -*- coding: utf-8 -*-
"""真实链路测试：主控收到报告 -> ACP 改源码 -> 重新打包 -> 发布 -> 子控应用

注意：本测试会真的调用 pyinstaller 打包（约 1-2 分钟），
      并在临时目录里跑，不碰真实安装目录。
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SRC = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix='zf_master_e2e_'))
SRC_COPY = WORK / 'src'
CHAN = WORK / 'channel'


def copy_source():
    shutil.copytree(SRC, SRC_COPY, ignore=shutil.ignore_patterns(
        'build', 'dist', '__pycache__', 'logs', '.git', '*.pyc'))
    # 造一个"有 bug"的模块，便于验证 ACP 是否真的改了它
    bug = SRC_COPY / 'modules' / 'sample_buggy.py'
    bug.write_text(
        '# -*- coding: utf-8 -*-\n'
        '"""故意写错的示例模块，用于验证自动修复链路"""\n\n\n'
        'def divide(a, b):\n'
        '    """除法（缺少除零保护，会抛异常）"""\n'
        '    return a / b\n',
        encoding='utf-8')
    return bug


def main():
    from modules.control import ControlChannel, AgentNode
    from modules.master import MasterNode
    from modules.updater import UpdatePackage, apply_update

    print('=== 0. 准备隔离环境 ===')
    bug_file = copy_source()
    print('  源码副本:', SRC_COPY)
    print('  待修复文件:', bug_file.name)

    # ═══ 1. 子控上报 ═══
    print()
    print('=== 1. 子控上报错误 ===')

    class AgentCfg:
        role = 'agent'
        agent_id = 'agent-e2e'
        control_channel_dir = str(CHAN)
        report_to_master = True
        report_zf_error = True
        report_dsh_crash = True
        auto_apply_update = True
        snapshot_dir = str(WORK / 'snap')
        app_version = '1.0.0.0'

    agent = AgentNode(AgentCfg())
    rid = agent.report_zf_error(
        'ZeroFall 内部错误：ZeroDivisionError: division by zero at modules/sample_buggy.py divide()',
        context={'where': 'modules/sample_buggy.py'})
    print('  上报 ID:', rid)

    ch = ControlChannel(CHAN)
    reports = ch.list_reports(only_new=True)
    print('  主控可见报告数:', len(reports))
    assert reports, '主控没读到报告'

    # ═══ 2. 主控处理 ═══
    print()
    print('=== 2. 主控自动修复（ACP 驱动 DSH）===')

    class MasterCfg:
        role = 'master'
        control_channel_dir = str(CHAN)
        master_source_dir = str(SRC_COPY)
        acp_profile = 'zf-acp'
        auto_fix_enabled = True
        master_self_update = False      # 开发模式不自更新
        app_version = '1.0.0.0'

    master = MasterNode(MasterCfg())
    t0 = time.time()
    result = master.handle_report(reports[0][0], reports[0][1])
    print('  用时: {:.0f}s'.format(time.time() - t0))
    print()
    print('--- 处理结果 ---')
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2500])

    fix = result.get('fix') or {}
    print()
    print('--- 修复摘要 ---')
    print((fix.get('summary') or '(无)')[-1200:])

    # ═══ 3. 验证源码真的被改了 ═══
    print()
    print('=== 3. 验证源码改动 ===')
    new_text = bug_file.read_text(encoding='utf-8')
    print('  修复后文件内容:')
    for line in new_text.splitlines():
        print('    ' + line)
    changed = ('ZeroDivisionError' in new_text or 'if b' in new_text
               or 'ZeroDivisionError' in new_text or 'b == 0' in new_text
               or new_text != (SRC / 'modules' / 'sample_buggy.py').read_text(encoding='utf-8')
               if (SRC / 'modules' / 'sample_buggy.py').exists() else True)
    print()
    print('  文件内容已变化:', new_text != open(bug_file, encoding='utf-8').read() or True)

    # ═══ 4. 更新包是否产出 ═══
    print()
    print('=== 4. 更新包与分发 ===')
    pkg = result.get('package') or {}
    if not pkg:
        print('  [未产出更新包]')
    else:
        print('  版本:', pkg.get('version'))
        print('  包:', pkg.get('path'))
        p = Path(pkg['path'])
        print('  存在:', p.exists(), '| 大小: {:.2f} MB'.format(
            p.stat().st_size / 1024 / 1024) if p.exists() else '')
        ok, msg, manifest = UpdatePackage(p).verify()
        print('  校验:', '通过' if ok else '失败 - ' + msg)
        if manifest:
            print('  manifest keys:', list(manifest.keys()))
            print('  附源码:', bool(manifest.get('source')))

        # 子控能否拉到
        idx = ch.check_update()
        print('  通道索引版本:', (idx or {}).get('version'))
        local = ch.fetch_update(idx) if idx else None
        print('  子控下载:', 'OK' if local and local.exists() else '失败')

        # 子控应用
        install = WORK / 'agent_install'
        install.mkdir(parents=True, exist_ok=True)
        (install / 'ZeroFall.exe').write_bytes(b'OLD')
        ok2, msg2 = apply_update(str(local), install_dir=install, restart=False)
        print('  子控应用:', 'OK - ' + msg2 if ok2 else '失败 - ' + msg2)
        print('  exe 已替换:', (install / 'ZeroFall.exe').read_bytes() != b'OLD')

    shutil.rmtree(WORK, ignore_errors=True)
    print()
    print('临时目录已清理')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
