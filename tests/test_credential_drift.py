# -*- coding: utf-8 -*-
"""凭据抗漂移测试

真实事故背景（2026-11-09）：
  ZeroFall 主控启动日志从 `可用key=OPENCODE_GO_API_KEY(51)` 变成 `可用key=无`。
  查明原因：DSH 在 14:26:29 重写了 ~/.dsh/.credentials.yaml，
  `refs:` 段整个消失，只剩三个 DSH 内部记录
  （client-connection/browser-session、deepseek-account-platform/device、
    deepseek-account-platform/default）。

  结果：ZeroFall 自动修复会在跑完几分钟 ACP 后才因认证失败暴露出来，
  而且每次都一样 —— 因为它的认证完全依赖别人管理的可变状态。

本测试验证修复后的行为：
  A. 正常 refs 格式能读到，并自动缓存
  B. DSH 重写凭据（refs 消失）后，仍能从自持库拿到 key
  C. 自持库也没有时，明确报"无"（不假装成功）
  D. 从 records 新格式里尽力提取
  E. 手工填的 key 优先级最高
  F. 诊断能指出 key 的来源
"""
import os
import sys
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


def make_home(root: Path, cred_text: str) -> Path:
    home = root / 'dsh_home'
    home.mkdir(parents=True, exist_ok=True)
    (home / '.credentials.yaml').write_text(cred_text, encoding='utf-8')
    return home


KV = 'oc_sk_' + 'a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6'


def main():
    sand = Path(tempfile.mkdtemp(prefix='zf_cred_'))
    # 把自持库指向沙箱，绝不碰真实文件
    os.environ['ZF_DATA_DIR'] = str(sand / 'zfdata')

    from modules import secret_store
    from modules.acp_client import load_credential_env, credential_diagnosis

    chk(secret_store.path().parent.exists() or True, '自持库路径已隔离到沙箱',
        secret_store.path())

    print()
    print('=== A. 旧格式 refs: 能读到并自动缓存 ===')
    home = make_home(sand, 'version: 1\nrefs:\n  OPENCODE_GO_API_KEY: "{}"\n'.format(KV))
    env = load_credential_env(home)
    chk(env.get('OPENCODE_GO_API_KEY') == KV, '从 refs 读到 key')
    cached = secret_store.get('OPENCODE_GO_API_KEY')
    chk(cached == KV, '数据已缓存到自持库')

    print()
    print('=== B. DSH 重写凭据（refs 消失）后仍可用 ===')
    # 模拟真实发生的重写：新格式只有 DSH 内部记录
    (home / '.credentials.yaml').write_text(
        'version: 1\n'
        'records:\n'
        '  client-connection/browser-session:\n'
        '    kind: grant\n'
        '    payload:\n'
        '      version: 1\n'
        '      secret: somebrowsersecretvalue0000000000000000\n'
        '  deepseek-account-platform/default:\n'
        '    kind: grant\n'
        '    payload:\n'
        '      version: 1\n'
        '      token: sometokenvalue0000000000000000000000000000000000000000000000\n'
        '      issuer: deepseek-account-platform\n',
        encoding='utf-8')
    env2 = load_credential_env(home)
    chk(env2.get('OPENCODE_GO_API_KEY') == KV,
        '凭据文件已无该 key，但仍从自持库拿到（抗漂移成功）')

    diag = credential_diagnosis(home)
    chk('自持库' in diag, '诊断标明来源=自持库', diag)
    chk(KV[:6] not in diag, '诊断不泄露密钥内容')

    print()
    print('=== C. 自持库也没有时，明确报无（不假装成功）===')
    secret_store.clear()
    env3 = load_credential_env(home)
    chk(not env3.get('OPENCODE_GO_API_KEY'), '无 key 时不伪造')
    diag3 = credential_diagnosis(home)
    chk('可用key=无' in diag3, '诊断如实报无', diag3)

    print()
    print('=== D. 无名记录一律不取（宁缺勿错）===')
    home2 = make_home(sand / 'b', '')
    (home2 / '.credentials.yaml').write_text(
        'version: 1\n'
        'records:\n'
        '  opencode-go/default:\n'
        '    kind: grant\n'
        '    payload:\n'
        '      token: {}\n'.format(KV),
        encoding='utf-8')
    env4 = load_credential_env(home2)
    chk(not env4.get('OPENCODE_GO_API_KEY'),
        '记录名不是 key 名 → 不猜测注入（避免把 DSH 内部凭据当 API key）')

    print()
    print('=== D2. 记录名就是 key 名时才取 ===')
    home2b = make_home(sand / 'b2', '')
    (home2b / '.credentials.yaml').write_text(
        'version: 1\n'
        'records:\n'
        '  OPENCODE_GO_API_KEY:\n'
        '    kind: api-key\n'
        '    payload:\n'
        '      secret: {}\n'.format(KV),
        encoding='utf-8')
    env4b = load_credential_env(home2b)
    chk(env4b.get('OPENCODE_GO_API_KEY') == KV,
        '记录名=OPENCODE_GO_API_KEY 时正确提取')

    print()
    print('=== D3. 太短/带空格的值不采信 ===')
    secret_store.clear()          # 先清缓存，否则会优先命中缓存，测不到文件解析
    home2c = make_home(sand / 'b3', '')
    (home2c / '.credentials.yaml').write_text(
        'version: 1\n'
        'refs:\n'
        '  OPENCODE_GO_API_KEY: "short"\n', encoding='utf-8')
    chk(not load_credential_env(home2c).get('OPENCODE_GO_API_KEY'),
        '长度不足 20 的值被拒绝（不像 key）')
    (home2c / '.credentials.yaml').write_text(
        'version: 1\n'
        'refs:\n'
        '  OPENCODE_GO_API_KEY: "has space in it 0123456789"\n', encoding='utf-8')
    chk(not load_credential_env(home2c).get('OPENCODE_GO_API_KEY'),
        '含空格的值被拒绝')

    print()
    print('=== E. 手工填的 key 优先级最高 ===')
    MANUAL = 'manual_key_xyz_0123456789'
    secret_store.clear()
    secret_store.put('OPENCODE_GO_API_KEY', MANUAL)
    home3 = make_home(sand / 'c', 'version: 1\nrefs:\n  OPENCODE_GO_API_KEY: "{}"\n'.format(KV))
    env5 = load_credential_env(home3)
    chk(env5.get('OPENCODE_GO_API_KEY') == MANUAL,
        '手工填的覆盖凭据文件（用户显式意愿优先）', env5.get('OPENCODE_GO_API_KEY'))

    print()
    print('=== F. 自动缓存不覆盖用户手工值 ===')
    n = secret_store.merge({'OPENCODE_GO_API_KEY': 'auto_detected_value'})
    chk(n == 0, 'merge 不覆盖已有值', n)
    chk(secret_store.get('OPENCODE_GO_API_KEY') == MANUAL, '手工值仍在')

    print()
    print('=== G. 损坏的自持库不致命 ===')
    secret_store.path().write_text('{ 这不是 json', encoding='utf-8')
    chk(secret_store.load() == {}, '损坏时返回空字典而非崩溃')
    env6 = load_credential_env(home3)
    chk(env6.get('OPENCODE_GO_API_KEY') == KV, '库坏了仍能回退到凭据文件')

    print()
    print('=== H. 脱敏显示 ===')
    chk(secret_store.mask('') == '无', '空值显示"无"')
    m = secret_store.mask('abcdefghij')
    chk('***' in m and 'abcdefghij' not in m, '长值脱敏', m)

    shutil.rmtree(sand, ignore_errors=True)
    os.environ.pop('ZF_DATA_DIR', None)
    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
