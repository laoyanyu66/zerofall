# -*- coding: utf-8 -*-
"""认证态测试

背景：
  桌面版 DSH 用 keyRef: 引用凭据服务；ZeroFall 的 ACP 会话走 llm-pi-ai
  直连路由，而 llm-pi-ai 只认 apiKeyEnv（环境变量名），没有 keyRef 机制。
  因此 ZeroFall 必须自己把 .credentials.yaml 里的密钥物化成环境变量。

覆盖：
  A. refs 段（当前写法）能取到 key
  B. records 段（旧写法）兜底
  C. 账号路由引用（DSH_ 前缀）不被误当 API key
  D. 凭据文件缺失/损坏时不抛异常
  E. 环境变量兜底
  F. 诊断摘要不含密钥内容（防日志泄密）
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 关键：把自持密钥库指向临时目录。
# 否则本文件里的假 key（FAKE）会被 load_credential_env 的 last-known-good
# 机制缓存进真实自持库，主控下次启动就会拿着一堆假 key 去认证。
_TMP_STORE = tempfile.mkdtemp(prefix='zf_auth_store_')
os.environ['ZF_DATA_DIR'] = _TMP_STORE

from modules.acp_client import load_credential_env, credential_diagnosis

PASS, FAIL = [], []
FAKE = 'oc_sk_TESTKEY_0123456789abcdef'


def chk(cond, desc):
    (PASS if cond else FAIL).append(desc)
    print(('  [OK]   ' if cond else '  [FAIL] ') + desc)


def write_cred(home: Path, text: str):
    (home / '.credentials.yaml').write_text(text, encoding='utf-8')


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_auth_'))

    # 清掉环境里可能存在的 key，保证测的是文件解析
    for n in ('OPENCODE_GO_API_KEY', 'OPENCODE_PERSONAL_API_KEY', 'DEEPSEEK_API_KEY'):
        os.environ.pop(n, None)

    # ═══ A. refs 段 ═══
    print('=== A. refs 段（当前 DSH 写法）===')
    h1 = root / 'a'
    h1.mkdir()
    write_cred(h1,
               'version: 1\n'
               'records: {}\n'
               'refs:\n'
               '  OPENCODE_GO_API_KEY: ' + FAKE + '\n'
               '  DSH_OPENCODE_GO_ACCOUNT_0D7D: ' + FAKE + '\n')
    env = load_credential_env(h1)
    chk(env.get('OPENCODE_GO_API_KEY') == FAKE, 'refs 里的 key 被注入')
    chk('DSH_OPENCODE_GO_ACCOUNT_0D7D' not in env, 'DSH_ 账号路由引用被过滤')

    # ═══ B. records 段兜底 ═══
    print()
    print('=== B. records 段（旧写法）兜底 ===')
    h2 = root / 'b'
    h2.mkdir()
    write_cred(h2,
               'version: 1\n'
               'records:\n'
               '  provider/opencode-go:\n'
               '    key: OPENCODE_PERSONAL_API_KEY\n'
               '    secret: ' + FAKE + '\n')
    env2 = load_credential_env(h2)
    chk(env2.get('OPENCODE_PERSONAL_API_KEY') == FAKE,
        'records 段的 key/secret 被识别')

    # ═══ C. DSH_ 前缀过滤 ═══
    print()
    print('=== C. 账号路由引用不被误用 ===')
    h3 = root / 'c'
    h3.mkdir()
    write_cred(h3,
               'version: 1\n'
               'records: {}\n'
               'refs:\n'
               '  DSH_OPENCODE_GO_ACCOUNT_2A54: ' + FAKE + '\n')
    env3 = load_credential_env(h3)
    leaked = [k for k in env3 if k.startswith('DSH_OPENCODE_GO_ACCOUNT')]
    chk(not leaked, 'DSH_ 前缀一律不进子进程环境')

    # ═══ D. 文件缺失/损坏 ═══
    print()
    print('=== D. 凭据文件缺失或损坏时不崩 ===')
    h4 = root / 'd'
    h4.mkdir()
    try:
        env4 = load_credential_env(h4)
        chk(isinstance(env4, dict), '文件缺失时返回 dict 不抛异常')
    except Exception as e:
        chk(False, '文件缺失时抛了异常: {}'.format(e))

    write_cred(h4, '这不是合法 yaml: [ unclosed')
    try:
        env5 = load_credential_env(h4)
        chk(isinstance(env5, dict), 'YAML 损坏时返回 dict 不抛异常')
    except Exception as e:
        chk(False, 'YAML 损坏时抛了异常: {}'.format(e))

    # ═══ E. 环境变量兜底 ═══
    print()
    print('=== E. 环境变量兜底 ===')
    h5 = root / 'e'
    h5.mkdir()
    os.environ['DEEPSEEK_API_KEY'] = 'env-fallback-key'
    try:
        env6 = load_credential_env(h5)
        chk(env6.get('DEEPSEEK_API_KEY') == 'env-fallback-key',
            '已在环境里的 key 不会被清掉')
    finally:
        os.environ.pop('DEEPSEEK_API_KEY', None)

    # refs 优先于环境变量
    h6 = root / 'f'
    h6.mkdir()
    os.environ['OPENCODE_GO_API_KEY'] = 'stale-env-value'
    write_cred(h6,
               'version: 1\n'
               'records: {}\n'
               'refs:\n'
               '  OPENCODE_GO_API_KEY: ' + FAKE + '\n')
    try:
        env7 = load_credential_env(h6)
        chk(env7.get('OPENCODE_GO_API_KEY') == FAKE,
            '凭据文件里的值覆盖过期环境变量')
    finally:
        os.environ.pop('OPENCODE_GO_API_KEY', None)

    # ═══ F. 诊断摘要不泄密 ═══
    print()
    print('=== F. 诊断摘要不含密钥内容 ===')
    diag = credential_diagnosis(h1)
    chk(FAKE not in diag, '摘要里不出现密钥原文')
    chk('OPENCODE_GO_API_KEY' in diag, '摘要里出现 key 名称')
    chk(str(len(FAKE)) in diag, '摘要里出现 key 长度（便于核对）')
    chk('凭据文件=OK' in diag, '摘要标明凭据文件状态')

    # ═══ G. 真实环境可用性（只报结论，不打印密钥）===
    print()
    print('=== G. 本机真实认证态 ===')
    real = Path(os.environ.get('USERPROFILE', '')) / '.dsh'
    diag_real = credential_diagnosis(real)
    print('  ' + diag_real)
    chk('OPENCODE_GO_API_KEY' in diag_real, '本机能取到 ACP 所需的 key')

    print()
    print('=' * 60)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    shutil.rmtree(root, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
