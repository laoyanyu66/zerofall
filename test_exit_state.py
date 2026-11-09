# -*- coding: utf-8 -*-
"""上次退出状态判定测试

背景：状态文件里残留 last_exit_crash=true 且退出码 4294967295
（0xFFFFFFFF，拿不到退出码的占位值），会让环境校验每次都 failed。
这个测试锁定修复后的语义：

  1. 没有崩溃标记              → PASSED
  2. 有标记，但 DeepSeek 在跑   → WARNING，且标记被清掉
  3. 有标记，DeepSeek 也没在跑  → FAILED
  4. 0xFFFFFFFF 应被识别为"退出码未知"，不当成崩溃确证
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_tmp = tempfile.mkdtemp(prefix='zf_exit_test_')
os.environ['ZF_DATA_DIR'] = _tmp

from config import ZeroFallConfig                    # noqa: E402
from modules.environment_checker import EnvironmentChecker, CheckResult  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_checker(snapshot_dir, state):
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(snapshot_dir)
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    d = Path(snapshot_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / '.environment_state.json').write_text(
        json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return EnvironmentChecker(cfg), d / '.environment_state.json'


def test_no_crash_flag():
    print('\n[1] 没有崩溃标记 → PASSED')
    sd = Path(_tmp) / 's1'
    ec, _ = make_checker(sd, {'last_exit_code': 0, 'last_exit_crash': False})
    item = ec.check_last_exit()
    check('结果为 PASSED', item.result == CheckResult.PASSED,
          '实得 {}'.format(item.result))
    check('信息含"正常退出"', '正常退出' in item.message)


def test_crash_but_running():
    print('\n[2] 有崩溃标记，但 DeepSeek 正在运行 → WARNING 且标记被清')
    sd = Path(_tmp) / 's2'
    ec, sf = make_checker(
        sd, {'last_exit_code': 4294967295, 'last_exit_crash': True})
    with mock.patch('modules.environment_checker.get_process_info',
                    return_value={'pid': 1234}):
        item = ec.check_last_exit()
    check('结果为 WARNING', item.result == CheckResult.WARNING,
          '实得 {}'.format(item.result))
    check('说明当前在运行', '正在运行' in item.message)
    state = json.loads(sf.read_text(encoding='utf-8'))
    check('崩溃标记已清除', state['last_exit_crash'] is False,
          '实得 {}'.format(state.get('last_exit_crash')))
    check('退出码已归零', state['last_exit_code'] == 0,
          '实得 {}'.format(state.get('last_exit_code')))


def test_crash_and_not_running():
    print('\n[3] 有崩溃标记，DeepSeek 未运行 → FAILED')
    sd = Path(_tmp) / 's3'
    ec, sf = make_checker(
        sd, {'last_exit_code': 1, 'last_exit_crash': True})
    with mock.patch('modules.environment_checker.get_process_info',
                    return_value=None):
        item = ec.check_last_exit()
    check('结果为 FAILED', item.result == CheckResult.FAILED,
          '实得 {}'.format(item.result))
    check('提示当前未运行', '未运行' in item.message)
    state = json.loads(sf.read_text(encoding='utf-8'))
    check('未运行时不擅自清标记', state['last_exit_crash'] is True,
          '实得 {}'.format(state.get('last_exit_crash')))


def test_unknown_code_text():
    print('\n[4] 0xFFFFFFFF 识别为退出码未知')
    sd = Path(_tmp) / 's4'
    ec, _ = make_checker(
        sd, {'last_exit_code': 4294967295, 'last_exit_crash': True})
    with mock.patch('modules.environment_checker.get_process_info',
                    return_value={'pid': 1}):
        item = ec.check_last_exit()
    check('详情标注"未知"', '未知' in (item.details or ''),
          '实得 {!r}'.format(item.details))
    check('不把 4294967295 当崩溃确证', '4294967295' not in (item.details or ''),
          '实得 {!r}'.format(item.details))


def test_overall_not_failed_when_running():
    print('\n[5] 运行中时整体结论不应是 FAILED')
    sd = Path(_tmp) / 's5'
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = str(sd)
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    sd.mkdir(parents=True, exist_ok=True)
    (sd / '.environment_state.json').write_text(
        json.dumps({'last_exit_code': 4294967295, 'last_exit_crash': True}),
        encoding='utf-8')
    ec = EnvironmentChecker(cfg)
    with mock.patch('modules.environment_checker.get_process_info',
                    return_value={'pid': 1}):
        item = ec.check_last_exit()
    check('不返回 FAILED', item.result != CheckResult.FAILED,
          '实得 {}'.format(item.result))


def test_unknown_exe_path_degrades():
    print('\n[6] 未配置 DeepSeek 路径 → WARNING（不报 FAILED）')
    sd = Path(_tmp) / 's6'
    ec, _ = make_checker(
        sd, {'last_exit_code': 4294967295, 'last_exit_crash': True})
    ec.config.deepseek_exe_path = ''
    item = ec.check_last_exit()
    check('结果为 WARNING', item.result == CheckResult.WARNING,
          '实得 {}'.format(item.result))
    check('说明状态未知', '未知' in item.message, item.message)


if __name__ == '__main__':
    print('=' * 62)
    print('上次退出状态判定测试')
    print('=' * 62)
    test_no_crash_flag()
    test_crash_but_running()
    test_crash_and_not_running()
    test_unknown_code_text()
    test_overall_not_failed_when_running()
    test_unknown_exe_path_degrades()
    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 62)
    sys.exit(0 if not FAIL else 1)
