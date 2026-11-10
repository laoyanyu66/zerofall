# -*- coding: utf-8 -*-
"""云端加密密码测试

**背景**：原来 5 个地方写死了同一串密码
    "ZeroFall_Cloud_Backup_2024_Secure"
单机自用没事，一开源就是真漏洞：任何人拿到源码就能解开任何人
上传到网盘的 .enc 快照。AES 只剩下"防了个包装"。

改成每台机器随机生成，但要解决两个硬问题：

  1. **不能丢数据**：用户已有的云端快照是用旧密码加密的，
     直接换密码等于让它们全部变成废文件
  2. **不能悄悄失败**：密钥库写不进去时必须留痕，否则下次
     生成新密码，旧文件就永远解不开了

所以这里要锁住：
  · 首次调用生成随机密码且长度足够
  · 同一进程内多次调用返回同一个（不能每次都变）
  · 配置显式指定时优先用配置
  · 解密：新密码失败 → 自动回退旧密码，并报告用了哪个
  · 加密→解密往返一致
  · 代码里不再出现写死的密码（除了 legacy 兼容常量）
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 把密钥库指到临时目录，别污染真实 secrets.json
_TMP = tempfile.mkdtemp(prefix='zf_crypto_')
os.environ['ZF_DATA_DIR'] = _TMP

import modules.secret_store as SS          # noqa: E402
from modules import cloud_crypto as CC     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


class FakeCfg:
    def __init__(self, pw=''):
        self.cloud_encrypt_password = pw


def reset_store():
    """清掉密钥库里的密码，模拟首次运行"""
    try:
        SS.clear(CC._STORE_KEY)
    except Exception:
        pass


def test_generated_and_stable():
    print('\n[1] 首次生成随机密码，且稳定')
    reset_store()
    pw1 = CC.get_cloud_password()
    pw2 = CC.get_cloud_password()
    check('生成了密码', bool(pw1), pw1)
    check('长度足够（>=32）', len(pw1) >= 32, len(pw1))
    check('两次调用一致', pw1 == pw2, (pw1, pw2))
    check('不是旧固定密码', pw1 != CC.LEGACY_PASSWORD)


def test_persisted_across_calls():
    print('\n[2] 密码落在密钥库里（重启后能复用）')
    reset_store()
    pw = CC.get_cloud_password()
    stored = SS.get(CC._STORE_KEY)
    check('已写入密钥库', stored == pw, stored)


def test_explicit_config_wins():
    print('\n[3] 配置显式指定时优先')
    reset_store()
    cfg = FakeCfg(pw='my-own-password-abcdefgh')
    pw = CC.get_cloud_password(cfg)
    check('用了配置里的', pw == 'my-own-password-abcdefgh', pw)
    # 没指定时回到随机
    check('空配置回退到随机', CC.get_cloud_password(FakeCfg()) != '', '')


def test_roundtrip_encrypt_decrypt():
    print('\n[4] 加解密往返')
    from modules.cloud_crypto import encrypt_snapshot, decrypt_snapshot
    tmp = Path(tempfile.mkdtemp(prefix='zf_rt_'))
    src = tmp / 'snap.zip'
    payload = b'ZeroFall snapshot payload ' * 500
    src.write_bytes(payload)
    enc = tmp / 'snap.zip.enc'
    dec = tmp / 'snap.back.zip'

    ok_enc = encrypt_snapshot(src, enc, FakeCfg())
    check('加密成功', ok_enc)
    check('密文与明文不同', enc.read_bytes() != payload)

    ok_dec, source = decrypt_snapshot(enc, dec, FakeCfg())
    check('解密成功', ok_dec, source)
    check('用的是本机密码', source == 'current', source)
    check('内容一致', dec.read_bytes() == payload)


def test_legacy_fallback():
    print('\n[5] 旧固定密码的文件仍能解开')
    from utils import encrypt_file
    from modules.cloud_crypto import decrypt_snapshot
    tmp = Path(tempfile.mkdtemp(prefix='zf_lg_'))
    src = tmp / 'old.zip'
    payload = b'old snapshot encrypted with legacy password'
    src.write_bytes(payload)
    enc = tmp / 'old.zip.enc'
    # 用旧固定密码加密（模拟历史文件）
    ok = encrypt_file(str(src), str(enc), CC.LEGACY_PASSWORD)
    check('模拟历史文件已生成', ok)

    dec = tmp / 'old.out.zip'
    ok2, source = decrypt_snapshot(enc, dec, FakeCfg())
    check('能解开历史文件', ok2, source)
    check('标记为 legacy', source == 'legacy', source)
    check('内容一致', dec.read_bytes() == payload)


def test_wrong_file_fails():
    print('\n[6] 坏文件/密码不对 → 明确失败（不静默）')
    from modules.cloud_crypto import decrypt_snapshot
    tmp = Path(tempfile.mkdtemp(prefix='zf_bad_'))
    junk = tmp / 'junk.enc'
    junk.write_bytes(b'not encrypted at all')
    ok, source = decrypt_snapshot(junk, tmp / 'out.zip', FakeCfg())
    check('返回失败', ok is False, ok)
    check('标记 failed', source == 'failed', source)


def test_describe_no_leak():
    print('\n[7] 诊断信息不泄露密码本身')
    d = CC.describe(FakeCfg())
    pw = CC.get_cloud_password(FakeCfg())
    check('包含"加密密码"', '加密密码' in d, d)
    check('不包含密码明文', pw not in d, d)
    print('     {}'.format(d))


def test_no_hardcoded_in_callsites():
    print('\n[8] 调用点不再写死密码')
    bad = []
    for p in Path('.').rglob('*.py'):
        if any(x in p.parts for x in ('build', 'dist', '__pycache__',
                                      '_stage_standalone', '_stage_master',
                                      '_stage_agent', 'tools')):
            continue
        if p.name in ('test_cloud_crypto.py',):
            continue
        try:
            text = p.read_text(encoding='utf-8')
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if 'ZeroFall_Cloud_Backup_2024_Secure' in line:
                # 只允许 cloud_crypto.py 里的 legacy 常量和文档说明
                if p.name == 'cloud_crypto.py':
                    continue
                bad.append('{}:{}'.format(p, i))
    check('只有 cloud_crypto 保留 legacy 常量', not bad, bad[:4])


def test_callsites_use_module():
    print('\n[9] 加密/解密都走 cloud_crypto')
    checks = [
        ('modules/baidu_pan.py', ['encrypt_snapshot', 'decrypt_snapshot']),
        ('ui/main_window.py', ['encrypt_snapshot']),
        ('ui/tabs/cloud_snapshots_tab.py', ['decrypt_snapshot']),
        ('ui/tabs/snapshot_tab.py', ['decrypt_snapshot']),
    ]
    for fn, need in checks:
        p = Path(fn)
        if not p.exists():
            continue
        text = p.read_text(encoding='utf-8')
        missing = [n for n in need if n not in text]
        check('{} 已接入'.format(fn), not missing, missing)


if __name__ == '__main__':
    print('=' * 66)
    print('云端加密密码测试（开源前的安全前提）')
    print('=' * 66)
    test_generated_and_stable()
    test_persisted_across_calls()
    test_explicit_config_wins()
    test_roundtrip_encrypt_decrypt()
    test_legacy_fallback()
    test_wrong_file_fails()
    test_describe_no_leak()
    test_no_hardcoded_in_callsites()
    test_callsites_use_module()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
