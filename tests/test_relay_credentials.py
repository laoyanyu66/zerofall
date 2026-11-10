# -*- coding: utf-8 -*-
"""子控专用凭据测试（账号隔离）

背景：
  存储位置必须主控子控相同，否则互相看不到文件；
  但"账号"可以拆开 —— 子控只拿到读写自己那部分的密钥，
  即便机器被翻看，也拿不到能伪造更新包的权限。

验证：
  A. 主控用主控凭据
  B. 子控填了专用凭据 → 用专用的
  C. 子控没填 → 回落到主控凭据（保证开箱可用）
  D. 专用凭据为空字符串/空白 → 视为没填
  E. 三种后端（webdav/s3/ftp）都生效
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules.net_transport import make_relay, WebDavChannel, S3Channel, FtpChannel

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    tail = ('  ' + str(extra)) if extra != '' else ''
    print('  {} {}'.format('[OK]  ' if cond else '[FAIL]', desc) + tail)


class Cfg:
    relay_enabled = True
    relay_kind = 'webdav'
    relay_root = '/zerofall-ctl'
    # 主控凭据
    relay_url = 'https://dav.example.com/dav'
    relay_user = 'MASTER@example.com'
    relay_password = 'MASTER-PASS'
    # 子控专用
    relay_agent_user = ''
    relay_agent_password = ''
    # S3
    s3_endpoint = 'https://s3.example.com'
    s3_bucket = 'zf'
    s3_access_key = 'MASTER-AK'
    s3_secret_key = 'MASTER-SK'
    s3_region = 'auto'
    s3_agent_access_key = ''
    s3_agent_secret_key = ''
    # FTP
    ftp_host = 'ftp.example.com'
    ftp_port = 21
    ftp_user = 'master_ftp'
    ftp_password = 'master_ftp_pass'
    ftp_agent_user = ''
    ftp_agent_password = ''
    ftp_tls = False
    role = 'master'


def reset():
    Cfg.relay_agent_user = ''
    Cfg.relay_agent_password = ''
    Cfg.s3_agent_access_key = ''
    Cfg.s3_agent_secret_key = ''
    Cfg.ftp_agent_user = ''
    Cfg.ftp_agent_password = ''


def main():
    # ═══ A. 主控用主控凭据 ═══
    print('=== A. 主控角色 ===')
    reset()
    Cfg.role = 'master'
    Cfg.relay_agent_user = 'AGENT@example.com'      # 即便配了专用，主控也不该用
    Cfg.relay_agent_password = 'AGENT-PASS'
    r = make_relay(Cfg())
    chk(isinstance(r, WebDavChannel), 'WebDAV 通道已构造')
    chk(r.user == 'MASTER@example.com', '主控用主控账号', r.user)
    chk(r.password == 'MASTER-PASS', '主控用主控密码')

    # ═══ B. 子控填了专用凭据 ═══
    print()
    print('=== B. 子控 + 已填专用凭据 ===')
    Cfg.role = 'agent'
    r = make_relay(Cfg())
    chk(r.user == 'AGENT@example.com', '子控用自己的账号', r.user)
    chk(r.password == 'AGENT-PASS', '子控用自己的密码')

    # ═══ C. 子控没填 → 回落共用 ═══
    print()
    print('=== C. 子控 + 未填专用（回落到共用）===')
    Cfg.relay_agent_user = ''
    Cfg.relay_agent_password = ''
    r = make_relay(Cfg())
    chk(r.user == 'MASTER@example.com', '回落到共用账号（开箱可用）', r.user)

    # ═══ D. 空白字符串视为没填 ═══
    print()
    print('=== D. 空白字符不算填了 ===')
    Cfg.relay_agent_user = '   '
    Cfg.relay_agent_password = '  '
    r = make_relay(Cfg())
    chk(r.user == 'MASTER@example.com', '纯空格回落到共用账号', repr(r.user))

    # ═══ E. 只填账号不填密码 ═══
    print()
    print('=== E. 只填了账号、密码为空 ===')
    Cfg.relay_agent_user = 'AGENT@example.com'
    Cfg.relay_agent_password = ''
    r = make_relay(Cfg())
    chk(r.user == 'AGENT@example.com', '账号用子控的')
    chk(r.password == '', '密码为空（由服务端决定是否拒绝）')

    # ═══ F. S3 ═══
    print()
    print('=== F. S3 后端凭据隔离 ===')
    reset()
    Cfg.relay_kind = 's3'
    Cfg.role = 'master'
    Cfg.s3_agent_access_key = 'AGENT-AK'
    Cfg.s3_agent_secret_key = 'AGENT-SK'
    r = make_relay(Cfg())
    chk(isinstance(r, S3Channel), 'S3 通道已构造')
    chk(r.ak == 'MASTER-AK', '主控用主控 AK', r.ak)

    Cfg.role = 'agent'
    r = make_relay(Cfg())
    chk(r.ak == 'AGENT-AK', '子控用子控 AK', r.ak)
    chk(r.sk == 'AGENT-SK', '子控用子控 SK')
    chk(r.bucket == 'zf', '存储位置（桶）仍然相同 → 能互相看到')

    Cfg.s3_agent_access_key = ''
    r = make_relay(Cfg())
    chk(r.ak == 'MASTER-AK', '子控未填则回落共用 AK')

    # ═══ G. FTP ═══
    print()
    print('=== G. FTP 后端凭据隔离 ===')
    reset()
    Cfg.relay_kind = 'ftp'
    Cfg.role = 'master'
    Cfg.ftp_agent_user = 'agent_ftp'
    Cfg.ftp_agent_password = 'agent_ftp_pass'
    r = make_relay(Cfg())
    chk(isinstance(r, FtpChannel), 'FTP 通道已构造')
    chk(r.user == 'master_ftp', '主控用主控 FTP 账号', r.user)

    Cfg.role = 'agent'
    r = make_relay(Cfg())
    chk(r.user == 'agent_ftp', '子控用子控 FTP 账号', r.user)
    chk(r.host == 'ftp.example.com', '服务器地址相同 → 同一目录')

    # ═══ H. 关键安全属性 ═══
    print()
    print('=== H. 安全属性：子控拿不到主控密钥 ===')
    reset()
    Cfg.relay_kind = 's3'
    Cfg.role = 'agent'
    Cfg.s3_agent_access_key = 'AGENT-AK'
    Cfg.s3_agent_secret_key = 'AGENT-SK'
    r = make_relay(Cfg())
    blob = '{}|{}|{}'.format(r.ak, r.sk, getattr(r, 'bucket', ''))
    chk('MASTER-AK' not in blob, '子控通道里不含主控 AK')
    chk('MASTER-SK' not in blob, '子控通道里不含主控 SK')
    chk('AGENT-AK' in blob, '只含子控自己的 AK')

    print()
    print('=' * 62)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
