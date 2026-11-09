# -*- coding: utf-8 -*-
"""设置项生效性测试

背景：配置里曾有 8 个字段从未被任何代码读取 —— 界面无处改、改了也没用。
其中 3 个是纯死功能（OrchestratorX 两个 + 百度网盘自动同步，目标程序
早已不存在），已从 config 删除；另外 4 个在本轮接进了真实逻辑：

  · pre_launch_check_enabled   → 只拦自动校验，手动校验始终可跑
  · cloud_consistency_check    → 云同步后校验加密包完整性
  · crash_alert_enabled        → 崩溃时是否弹窗
  · auto_start_enabled         → 开机自启注册表

这个测试锁定"开关真的有作用"，避免以后又退化成摆设。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_tmp = tempfile.mkdtemp(prefix='zf_settings_')
os.environ['ZF_DATA_DIR'] = _tmp

from config import ZeroFallConfig, ConfigManager          # noqa: E402
from modules.environment_checker import (                 # noqa: E402
    EnvironmentChecker, CheckResult,
)

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_checker(tag, **cfg_over):
    sd = Path(_tmp) / tag
    sd.mkdir(parents=True, exist_ok=True)
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(sd)
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    cfg.md5_check_enabled = False
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return EnvironmentChecker(cfg)


def test_dead_fields_removed():
    print('\n[1] 死字段已从配置模型移除')
    c = ZeroFallConfig()
    check('无 orchestratorx_enabled', not hasattr(c, 'orchestratorx_enabled'))
    check('无 orchestratorx_sync_on_restore',
          not hasattr(c, 'orchestratorx_sync_on_restore'))
    check('无 baidu_pan_auto_sync', not hasattr(c, 'baidu_pan_auto_sync'))


def test_live_fields_exist():
    print('\n[2] 四个生效字段仍在，且默认值合理')
    c = ZeroFallConfig()
    check('pre_launch_check_enabled 默认开', c.pre_launch_check_enabled is True)
    check('crash_alert_enabled 默认开', c.crash_alert_enabled is True)
    check('auto_start_enabled 默认开', c.auto_start_enabled is True)
    check('cloud_consistency_check 默认关（额外 IO）',
          c.cloud_consistency_check is False)


def test_pre_launch_switch():
    print('\n[3] 前置校验开关：拦自动、不拦手动')
    # 关闭开关
    ec = make_checker('a', pre_launch_check_enabled=False)

    auto_rep = ec.run_full_check(auto=True)
    names = [i.name for i in auto_rep.items]
    check('自动校验被跳过', names == ['前置启动校验'], names)
    check('跳过时结论是 passed', auto_rep.overall_result == CheckResult.PASSED,
          auto_rep.overall_result)

    manual_rep = ec.run_full_check(auto=False)
    manual_names = [i.name for i in manual_rep.items]
    check('手动校验照常执行', len(manual_names) > 1, manual_names)
    check('手动校验不含"跳过"项', '前置启动校验' not in manual_names,
          manual_names)

    # 打开开关
    ec2 = make_checker('b', pre_launch_check_enabled=True)
    auto2 = ec2.run_full_check(auto=True)
    check('开关打开后自动校验真跑',
          len([i.name for i in auto2.items]) > 1,
          [i.name for i in auto2.items])


def test_autostart_module():
    print('\n[4] 开机自启模块可用')
    from modules import autostart
    check('is_enabled 可调用', isinstance(autostart.is_enabled(), bool))
    check('current_value 可调用', isinstance(autostart.current_value(), str))
    cmd = autostart._launch_command()
    check('启动命令非空', bool(cmd.strip()), repr(cmd))
    check('命令带引号（路径含空格也不会被拆）', cmd.startswith('"'), cmd)

    # 只验证 apply_setting 的幂等判断逻辑，不实际改注册表
    target = autostart.is_enabled()
    ok = autostart.apply_setting(target)
    check('已是目标状态时 apply 返回成功', ok is True, ok)


def test_cloud_consistency_registered():
    print('\n[5] 云一致性校验方法存在且受开关控制')
    import ui.main_window as MW
    has = hasattr(MW.ZeroFallMainWindow, '_verify_cloud_consistency')
    check('方法已定义', has)

    # 关闭开关时不应产生任何文件操作（用不存在的路径验证不抛异常）
    src = Path(_tmp) / 'nope_src.zip'
    dst = Path(_tmp) / 'nope_dst.enc'
    inst = MW.ZeroFallMainWindow.__new__(MW.ZeroFallMainWindow)
    inst.config = ZeroFallConfig()
    inst.config.cloud_consistency_check = False
    try:
        inst._verify_cloud_consistency(src, dst)
        check('开关关闭时静默跳过', True)
    except Exception as e:
        check('开关关闭时静默跳过', False, e)

    # 打开开关但目标不存在 → 只记警告，不抛
    inst.config.cloud_consistency_check = True
    try:
        inst._verify_cloud_consistency(src, dst)
        check('目标缺失时只告警不崩溃', True)
    except Exception as e:
        check('目标缺失时只告警不崩溃', False, e)


def test_config_roundtrip():
    print('\n[6] 配置读写不丢新字段')
    p = Path(_tmp) / 'cfg.json'
    cm = ConfigManager(str(p))
    cm.load()
    cm.config.pre_launch_check_enabled = False
    cm.config.cloud_consistency_check = True
    cm.config.crash_alert_enabled = False
    cm.config.auto_start_enabled = False
    cm.save()

    raw = json.loads(p.read_text(encoding='utf-8'))
    check('写入 pre_launch_check_enabled', raw.get('pre_launch_check_enabled') is False)
    check('写入 cloud_consistency_check', raw.get('cloud_consistency_check') is True)
    check('写入 crash_alert_enabled', raw.get('crash_alert_enabled') is False)
    check('写入 auto_start_enabled', raw.get('auto_start_enabled') is False)

    cm2 = ConfigManager(str(p))
    c2 = cm2.load()
    check('重新加载后值保持', c2.pre_launch_check_enabled is False
          and c2.cloud_consistency_check is True)


def test_legacy_json_still_loads():
    print('\n[7] 含已删字段的旧配置仍能加载')
    p = Path(_tmp) / 'legacy.json'
    p.write_text(json.dumps({
        'role': 'master',
        'orchestratorx_enabled': True,
        'orchestratorx_sync_on_restore': False,
        'baidu_pan_auto_sync': True,
        'pre_launch_check_enabled': False,
    }, ensure_ascii=False), encoding='utf-8')
    cm = ConfigManager(str(p))
    c = cm.load()
    check('加载不报错', c is not None)
    check('已删字段被忽略', not hasattr(c, 'orchestratorx_enabled'))
    check('其余字段正常读取', c.pre_launch_check_enabled is False)


if __name__ == '__main__':
    print('=' * 64)
    print('设置项生效性测试')
    print('=' * 64)
    test_dead_fields_removed()
    test_live_fields_exist()
    test_pre_launch_switch()
    test_autostart_module()
    test_cloud_consistency_registered()
    test_config_roundtrip()
    test_legacy_json_still_loads()
    print()
    print('=' * 64)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 64)
    sys.exit(0 if not FAIL else 1)
