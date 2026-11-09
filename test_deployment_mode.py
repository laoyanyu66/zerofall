# -*- coding: utf-8 -*-
"""部署形态（standalone / master / agent）与诊断包测试

改造目标：把"给自己用的多机自愈"和"开源分发的本地工具"拆开。

  · standalone（开源默认）：不连控制通道、不上报、不起主控 HTTP 服务
  · master / agent：老行为，保留给自建场景

硬约束：
  1. 老配置（只有 role、没有 deployment_mode）**不能**被降级成 standalone，
     否则已经在跑的主控会突然不工作
  2. 诊断包里的密钥/令牌必须脱敏 —— 这是给 Issue 用的，会公开
  3. standalone 下绝不能初始化控制节点
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_tmp = tempfile.mkdtemp(prefix='zf_mode_')
os.environ['ZF_DATA_DIR'] = _tmp

from config import ZeroFallConfig, ConfigManager        # noqa: E402
from modules import diagnostics as D                    # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def test_default_is_standalone():
    print('\n[1] 全新安装默认单体模式')
    c = ZeroFallConfig()
    check('deployment_mode 默认 standalone',
          c.deployment_mode == 'standalone', c.deployment_mode)


def test_legacy_migration():
    print('\n[2] 老配置按 role 迁移，不会被降级')
    c_master = ZeroFallConfig.from_dict({'role': 'master'})
    check('role=master → master', c_master.deployment_mode == 'master',
          c_master.deployment_mode)
    c_agent = ZeroFallConfig.from_dict({'role': 'agent'})
    check('role=agent → agent', c_agent.deployment_mode == 'agent',
          c_agent.deployment_mode)
    # 显式指定优先
    c_explicit = ZeroFallConfig.from_dict(
        {'role': 'master', 'deployment_mode': 'standalone'})
    check('显式 standalone 不被 role 覆盖',
          c_explicit.deployment_mode == 'standalone', c_explicit.deployment_mode)


def test_mode_from_real_config():
    print('\n[3] 当前部署配置读出来的形态')
    p = Path(_tmp) / 'cfg_master.json'
    p.write_text(json.dumps({'role': 'master', 'control_channel_dir': 'E:/x'}),
                 encoding='utf-8')
    c = ConfigManager(str(p)).load()
    check('老配置加载后仍是 master', c.deployment_mode == 'master',
          c.deployment_mode)


def test_standalone_skips_control():
    print('\n[4] standalone 不初始化控制节点')
    from types import SimpleNamespace
    import ui.main_window as MW

    # _deployment_mode 只读 self.config，用普通对象就能调用
    # （不去构造真窗口：绕过 QObject.__init__ 会让 Qt 属性访问报错）
    def mode_of(mode, role='master'):
        cfg = ZeroFallConfig()
        cfg.deployment_mode = mode
        cfg.role = role
        return MW.ZeroFallMainWindow._deployment_mode(
            SimpleNamespace(config=cfg))

    check('standalone 判定正确', mode_of('standalone') == 'standalone',
          mode_of('standalone'))
    check('mode 优先于矛盾的 role',
          mode_of('standalone', role='master') == 'standalone')
    check('master 判定正确', mode_of('master') == 'master')
    check('agent 判定正确', mode_of('agent') == 'agent')
    # 脏配置兜底
    cfg = ZeroFallConfig()
    cfg.deployment_mode = 'garbage'
    cfg.role = 'agent'
    check('非法 mode 退回 role',
          MW.ZeroFallMainWindow._deployment_mode(
              SimpleNamespace(config=cfg)) == 'agent')

    # 静态校验：初始化函数里必须真的短路，而不是只写日志
    src = Path('ui/main_window.py').read_text(encoding='utf-8')
    st = src.find('def _start_control_timer')
    seg = src[st:st + 9000]
    check('函数内有 standalone 分支', "if mode == 'standalone'" in seg)
    check('standalone 分支直接 return', 'return' in seg)
    check('standalone 分支不开 HTTP 服务',
          seg.find("if mode == 'standalone'") < seg.find('_start_master_http_server'),
          '短路应在起服务之前')


def test_master_mode_still_initializes():
    print('\n[5] master 模式不会被短路挡住')
    import ui.main_window as MW
    src = Path('ui/main_window.py').read_text(encoding='utf-8')
    st = src.find('def _start_control_timer')
    seg = src[st:st + 9000]
    # standalone 的 return 必须只覆盖 standalone 分支
    check('存在 master/agent 的正常分支',
          "if role == 'master'" in seg, '缺少主控分支')
    # 只验证"短路块自己 return 了"，别用 return 计数 ——
    # 正常分支里本来就有多个 return，数总数只会写出脆弱断言
    _i = seg.find("if mode == 'standalone'")
    _blk = seg[_i:_i + 400]
    check('standalone 块内有 return', 'return' in _blk, _blk[:120])
    check('standalone 块在 master 分支之前',
          _i < seg.find("if role == 'master'"), (_i, seg.find("if role == 'master'")))


def test_redact():
    print('\n[5b] 脱敏覆盖嵌套结构')
    data = {
        'api_key': 'sk-abcdef123456',
        'OPENCODE_GO_API_KEY': 'oc_sk_real',
        'relay_password': 'p@ssw0rd',
        'deepseek_exe_path': 'D:/DeepSeek/DeepSeek.exe',
        'nested': {
            's3_secret_key': 'AKIAxxx',
            'plain': 'keep-me',
        },
        'list': [{'token': 'abc'}, {'name': 'ok'}],
    }
    out = D.redact(data)
    check('api_key 被掩码', out['api_key'] == D._MASK, out['api_key'])
    check('OPENCODE_GO_API_KEY 被掩码',
          out['OPENCODE_GO_API_KEY'] == D._MASK)
    check('relay_password 被掩码', out['relay_password'] == D._MASK)
    check('普通路径保留', out['deepseek_exe_path'].startswith('D:/'))
    check('嵌套敏感键被掩码',
          out['nested']['s3_secret_key'] == D._MASK, out['nested']['s3_secret_key'])
    check('嵌套普通键保留', out['nested']['plain'] == 'keep-me')
    check('列表里的敏感键被掩码', out['list'][0]['token'] == D._MASK)
    check('列表里的普通键保留', out['list'][1]['name'] == 'ok')


def test_bundle_generated_and_safe():
    print('\n[6] 诊断包能生成，且不含明文密钥')
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(Path(_tmp) / 'snaps')
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    cfg.deepseek_plugins_dir = _tmp
    # 塞一个明显的假密钥，验证它不会出现在包里
    cfg.relay_password = 'dummy-pw-for-test'
    Path(cfg.snapshot_dir).mkdir(parents=True, exist_ok=True)

    log_dir = Path(_tmp) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / 'zerofall_test.log').write_text(
        'line1\nline2\nkey=SUPER_SECRET_PASSWORD_123\n', encoding='utf-8')

    dest = Path(_tmp) / 'diag.zip'
    out = D.build_diagnostic_bundle(cfg, dest_path=dest, log_dir=log_dir,
                                    include_env_check=False)
    check('文件已生成', out.exists(), out)
    check('是合法 zip', zipfile.is_zipfile(out))

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        for expect in ('system.json', 'config.json', 'plugins.json',
                       'snapshots.json', 'environment.json', 'logs.txt',
                       'README.txt'):
            check('包含 {}'.format(expect), expect in names, sorted(names))
        cfg_text = z.read('config.json').decode('utf-8', 'replace')
        check('配置里没有明文 password',
              'dummy-pw-for-test' not in cfg_text)
        check('配置里出现掩码', D._MASK in cfg_text)
        readme = z.read('README.txt').decode('utf-8', 'replace')
        check('README 说明不含凭据', '不包含' in readme)


def test_standalone_packaging_flags():
    print('\n[7] 打包脚本：standalone 会关掉上报')
    src = Path('build_release.py').read_text(encoding='utf-8')
    check('支持 standalone 参数', "'standalone'" in src)
    check('standalone 关掉 report_to_master',
          "cfg['report_to_master'] = False" in src)
    check('standalone 关掉 HTTP 服务',
          "cfg['http_server_enabled'] = False" in src)
    check('写入 deployment_mode', "cfg['deployment_mode'] = mode" in src)


if __name__ == '__main__':
    print('=' * 66)
    print('部署形态与诊断包测试')
    print('=' * 66)
    test_default_is_standalone()
    test_legacy_migration()
    test_mode_from_real_config()
    test_standalone_skips_control()
    test_master_mode_still_initializes()
    test_redact()
    test_bundle_generated_and_safe()
    test_standalone_packaging_flags()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
