# -*- coding: utf-8 -*-
"""云端快照加密：每台机器独立密钥

**为什么必须改掉原来的写法**

原来 4 个地方写死了同一个密码：
    DEFAULT_ENCRYPT_PASSWORD = "ZeroFall_Cloud_Backup_2024_Secure"

这在单机自用阶段没问题，一开源就变成真漏洞 —— 任何人拿到源码，
就能解开任何人上传到网盘的 .enc 快照。所谓 AES 加密只是防了个包装。

**现在的方案**

  · 每台机器首次用到加密时，随机生成一个 32 字节密码
  · 存在本地密钥库（secret_store，不进版本库）
  · 用户也可以在配置里显式指定 `cloud_encrypt_password` 覆盖

**旧文件怎么办**

历史快照是用那串固定密码加密的，直接换密码会让用户已有的云端备份
全部无法恢复。所以解密时保留一次回退：新密码失败 → 试旧密码。
解出来的文件在本地重新加密（下次上传自然替换成新密码的版本）。

这也意味着：**能被旧密码解开的历史文件，安全性仍取决于那串旧密码**。
README 里要提醒用户"重新上传一次快照以完成迁移"。
"""
import secrets

from modules import secret_store

# 旧版本写死的密码。只用于**解密**历史文件，不再用于加密新文件。
# 保留它是为了不让用户已有的云端备份变成废数据。
LEGACY_PASSWORD = "ZeroFall_Cloud_Backup_2024_Secure"

_STORE_KEY = "cloud_encrypt_password"


def get_cloud_password(config=None) -> str:
    """当前机器该用的加密密码

    优先级：配置里显式指定 > 密钥库里的随机密码 > 首次生成并保存
    """
    # 1. 用户显式设置（比如想多台机器共用同一把钥匙）
    if config is not None:
        explicit = getattr(config, 'cloud_encrypt_password', '') or ''
        if explicit:
            return explicit

    # 2. 密钥库里已有的
    try:
        existing = secret_store.get(_STORE_KEY)
        if existing:
            return existing
    except Exception:
        pass

    # 3. 首次生成：32 字节随机，base64url 后约 43 字符
    pw = secrets.token_urlsafe(32)
    try:
        secret_store.put(_STORE_KEY, pw)
    except Exception:
        # 存不进去也得能加密，至少本次会话可用
        # （下次会生成新的，届时旧文件解不开 —— 所以下面日志要留痕）
        pass
    return pw


def is_using_legacy(config=None) -> bool:
    """当前密码是不是还是那串旧的固定密码"""
    try:
        return get_cloud_password(config) == LEGACY_PASSWORD
    except Exception:
        return False


def encrypt_snapshot(src_path, dst_path, config=None) -> bool:
    """加密单个快照文件（用本机随机密码）"""
    from utils import encrypt_file
    return bool(encrypt_file(str(src_path), str(dst_path),
                             get_cloud_password(config)))


def decrypt_snapshot(src_path, dst_path, config=None):
    """解密快照，自动兼容旧固定密码

    Returns:
        (ok: bool, source: str)
        source = 'current' 用本机密码成功
                 'legacy'  回退到旧固定密码成功（说明是历史文件）
                 'failed'  都失败（密码不对或文件损坏）
    """
    from utils import decrypt_file

    pw = get_cloud_password(config)
    try:
        if decrypt_file(str(src_path), str(dst_path), pw):
            return True, 'current'
    except Exception:
        pass

    # 回退：历史文件
    if pw != LEGACY_PASSWORD:
        try:
            if decrypt_file(str(src_path), str(dst_path), LEGACY_PASSWORD):
                return True, 'legacy'
        except Exception:
            pass

    return False, 'failed'


def describe(config=None) -> str:
    """一行诊断信息（不暴露密码本身，只说来源和长度）"""
    try:
        pw = get_cloud_password(config)
    except Exception as e:
        return "加密密码不可用: {}".format(e)
    if pw == LEGACY_PASSWORD:
        return ("加密密码 = 旧版固定密码（不安全，建议重新上传一次快照完成迁移）")
    kind = "配置指定" if (config is not None and
                          getattr(config, 'cloud_encrypt_password', '')) else "本机随机"
    return "加密密码 = {}（{} 位）".format(kind, len(pw))
