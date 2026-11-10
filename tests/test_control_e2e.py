# -*- coding: utf-8 -*-
"""主控/子控端到端集成测试

覆盖：
  A. 通道：子控写报告 -> 主控读到 -> 标记已处理
  B. 开关：关闭上报后不再写报告
  C. 更新包：打包 -> 校验 -> 发布 -> 子控拉到 -> 应用（不重启）
  D. 版本比较：新版本才会被应用
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

from modules.control import ControlChannel, AgentNode, version_tuple
from modules.updater import UpdatePackage, apply_update, read_version_from_exe

PASS, FAIL = [], []


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_ctl_test_'))
    chan_root = root / 'channel'

    # ═══ A. 通道 ═══
    print('=== A. 通道：报告写入/读取/标记 ===')
    ch = ControlChannel(chan_root)
    ch.ensure()
    chk(ch.available(), '通道目录可用')

    rid, path = ch.write_report('agent-01', 'zf_error', {
        'signature': 'boom', 'error': 'TypeError: xxx', 'zerofall_version': '1.2.0.0',
    })
    chk(path.exists(), '报告文件已写入: {}'.format(path.name))

    reports = ch.list_reports(only_new=True)
    chk(len(reports) == 1, '主控读到 1 条未处理报告')
    chk(reports[0][1]['kind'] == 'zf_error', '报告 kind 正确')
    chk(reports[0][1]['payload']['error'] == 'TypeError: xxx', '报告 payload 完整')

    ch.mark_processed(reports[0][0], {'ok': True})
    chk(len(ch.list_reports(only_new=True)) == 0, '标记后不再重复出现')
    chk(len(ch.list_reports(only_new=False)) == 1, '但原始记录仍保留')

    # 多子控
    ch.write_report('agent-02', 'dsh_crash', {'signature': 'c', 'exit_code': -1})
    chk(len(ch.list_reports(only_new=True)) == 1, '第二个子控的报告独立可见')

    # ═══ B. 上报开关 ═══
    print()
    print('=== B. 上报开关 ===')

    class Cfg:
        role = 'agent'
        agent_id = 'agent-test'
        control_channel_dir = str(chan_root)
        report_to_master = True
        report_zf_error = True
        report_dsh_crash = False
        auto_apply_update = True
        snapshot_dir = str(root / 'snap')
        app_version = '1.2.0.0'

    cfg = Cfg()
    agent = AgentNode(cfg)
    before = len(ch.list_reports(only_new=False))
    r1 = agent.report_zf_error('新的错误 A')
    after = len(ch.list_reports(only_new=False))
    chk(r1 is not None and after == before + 1, '允许的类型（zf_error）成功上报')

    r2 = agent.report_dsh_crash(-1)
    after2 = len(ch.list_reports(only_new=False))
    chk(r2 is None and after2 == after, '禁用的类型（dsh_crash）被拦截')

    cfg.report_to_master = False
    agent2 = AgentNode(cfg)
    r3 = agent2.report_zf_error('新的错误 B')
    chk(r3 is None, '总开关关闭后不上报')

    # 去重
    cfg.report_to_master = True
    agent3 = AgentNode(cfg)
    n0 = len(ch.list_reports(only_new=False))
    agent3.report_zf_error('重复错误 X')
    agent3.report_zf_error('重复错误 X')
    n1 = len(ch.list_reports(only_new=False))
    chk(n1 == n0 + 1, '相同签名 10 分钟内去重（只报 1 条）')

    # ═══ C. 更新包全流程 ═══
    print()
    print('=== C. 更新包：打包 -> 校验 -> 发布 -> 拉取 -> 应用 ===')
    fake_exe = root / 'ZeroFall.exe'
    fake_exe.write_bytes(b'MZ' + b'\x00' * 4096)      # 假的 exe
    src_dir = root / 'src'
    (src_dir / 'modules').mkdir(parents=True)
    (src_dir / 'main.py').write_text('print("v2")', encoding='utf-8')
    (src_dir / 'modules' / 'a.py').write_text('X=1', encoding='utf-8')
    (src_dir / 'logs').mkdir()
    (src_dir / 'logs' / 'x.log').write_text('noise', encoding='utf-8')
    (src_dir / 'build').mkdir()
    (src_dir / 'build' / 'junk.pyc').write_text('junk', encoding='utf-8')

    pkg_path = root / 'upd.zip'
    p, manifest = UpdatePackage.build(pkg_path, exe_path=fake_exe, source_dir=src_dir,
                                      version='1.2.1.0', notes='修复崩溃')
    chk(pkg_path.exists(), '更新包已生成')
    chk(manifest['version'] == '1.2.1.0', 'manifest 版本正确')
    chk(manifest['exe']['sha256'], 'manifest 含 exe 校验和')

    import zipfile
    with zipfile.ZipFile(pkg_path) as zf:
        names = zf.namelist()
    chk('manifest.json' in names, '含 manifest.json')
    chk('ZeroFall.exe' in names, '含 ZeroFall.exe')
    chk('source.zip' in names, '含 source.zip')
    with zipfile.ZipFile(pkg_path) as zf:
        inner = zipfile.ZipFile(__import__('io').BytesIO(zf.read('source.zip')))
        inner_names = inner.namelist()
    chk('main.py' in inner_names, '源码含 main.py')
    chk(not any(n.startswith('logs/') for n in inner_names), '源码排除了 logs/')
    chk(not any(n.startswith('build/') for n in inner_names), '源码排除了 build/')

    pkg = UpdatePackage(pkg_path)
    ok, msg, m2 = pkg.verify()
    chk(ok, '更新包校验通过: ' + msg)

    # 篡改测试
    bad = root / 'bad.zip'
    with zipfile.ZipFile(pkg_path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    items['ZeroFall.exe'] = b'TAMPERED'
    with zipfile.ZipFile(bad, 'w', zipfile.ZIP_DEFLATED) as zf:
        for n, d in items.items():
            zf.writestr(n, d)
    ok2, msg2, _ = UpdatePackage(bad).verify()
    chk(not ok2, '被篡改的包被拒绝: ' + msg2[:50])

    # 发布
    published = ch.publish_update('1.2.1.0', pkg_path, manifest)
    chk(published, '更新已发布到通道')
    idx = ch.check_update()
    chk(idx and idx['version'] == '1.2.1.0', '子控能读到更新索引')
    chk(idx and idx['sha256'], '索引含 sha256')

    # 拉取 + 应用（不重启）
    install = root / 'install'
    install.mkdir()
    (install / 'ZeroFall.exe').write_bytes(b'OLD EXE CONTENT')
    local = ch.fetch_update(idx)
    chk(local and local.exists(), '子控下载到更新包')
    ok3, msg3 = apply_update(str(local), install_dir=install, restart=False)
    chk(ok3, '应用成功: ' + msg3)
    chk((install / 'ZeroFall.exe').read_bytes() != b'OLD EXE CONTENT', 'exe 已被替换')
    chk((install / 'ZeroFall.exe.bak-1.2.1.0').exists(), '旧 exe 已备份（可回滚）')

    # ═══ D. 版本比较 ═══
    print()
    print('=== D. 版本比较与跳过旧版本 ===')
    chk(version_tuple('1.2.1.0') > version_tuple('1.2.0.0'), '1.2.1.0 > 1.2.0.0')
    chk(version_tuple('1.2.0.0') == version_tuple('1.2.0'), '1.2.0 == 1.2.0')
    chk(not version_tuple('1.2.0.0') > version_tuple('1.2.0.0'), '相同版本不触发更新')

    class Cfg2(Cfg):
        agent_id = 'agent-ver'
        app_version = '1.2.1.0'      # 已经是最新
    agent4 = AgentNode(Cfg2())
    applied, msg4 = agent4.poll_update()
    chk(not applied and '最新' in msg4, '已是最新时跳过: ' + msg4)

    class Cfg3(Cfg):
        agent_id = 'agent-old'
        app_version = '1.0.0.0'
        auto_apply_update = False
    agent5 = AgentNode(Cfg3())
    applied2, msg5 = agent5.poll_update()
    chk(not applied2 and '关闭' in msg5, '自动更新关闭时不应用: ' + msg5)

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  - ' + f)
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
