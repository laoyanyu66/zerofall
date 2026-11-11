# -*- coding: utf-8 -*-
"""配置迁移包的加密容器（对齐 xiajiajun516/dsh-config-manager 的加密备份语义）

格式（自描述、单文件）：
    b"ZFCE1\\n"                       magic + 版本
    4 字节大端 header 长度
    header（UTF-8 JSON）：{"kdf": "scrypt", "n":…, "r":…, "p":…,
                          "salt": base64, "nonce": base64, "created_at": …}
    ciphertext || GCM tag(16B)        对「内层 ZIP 的原始字节」做 AES-256-GCM

为什么这么设计：
  · 密钥由密码 + 每份文件独立随机 salt 派生（scrypt n=2^14），密码**不落盘**
  · GCM 自带完整性校验：密码错、文件被改，都会在解密时直接失败（不是悄悄读出乱码）
  · 头部明文只放 KDF 参数与随机量，不含任何配置内容；manifest 在内层 ZIP 里一起加密
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

MAGIC = b"ZFCE1\n"
HEADER_LEN_BYTES = 4
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
NONCE_LEN = 12


class CryptoError(Exception):
    """加密/解密失败（密码错、文件损坏、格式不对）"""


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    import hashlib
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=KEY_LEN)


def is_encrypted(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def encrypt_bytes(payload: bytes, password: str) -> bytes:
    """把任意字节加密成一个自描述容器"""
    if not password:
        raise CryptoError("密码为空")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception as e:  # pragma: no cover - 依赖缺失
        raise CryptoError("缺 cryptography 依赖：{}".format(e)) from e

    salt = os.urandom(16)
    nonce = os.urandom(NONCE_LEN)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    ct = AESGCM(key).encrypt(nonce, payload, None)
    header = {
        "kdf": "scrypt",
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    hb = json.dumps(header, ensure_ascii=False).encode("utf-8")
    return MAGIC + len(hb).to_bytes(HEADER_LEN_BYTES, "big") + hb + ct


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    """解容器；密码错或内容被改会抛 CryptoError"""
    if not blob.startswith(MAGIC):
        raise CryptoError("不是 ZeroFall 加密配置包（magic 不符）")
    off = len(MAGIC)
    if len(blob) < off + HEADER_LEN_BYTES:
        raise CryptoError("文件截断：缺 header 长度")
    hlen = int.from_bytes(blob[off:off + HEADER_LEN_BYTES], "big")
    off += HEADER_LEN_BYTES
    if len(blob) < off + hlen + 16:
        raise CryptoError("文件截断：header/密文不完整")
    try:
        header = json.loads(blob[off:off + hlen].decode("utf-8"))
    except Exception as e:
        raise CryptoError("header 解析失败：{}".format(e)) from e
    off += hlen
    ct = blob[off:]
    try:
        salt = base64.b64decode(header["salt"])
        nonce = base64.b64decode(header["nonce"])
        n, r, p = int(header["n"]), int(header["r"]), int(header["p"])
    except Exception as e:
        raise CryptoError("header 字段缺失：{}".format(e)) from e

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _derive(password, salt, n, r, p)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception:
        raise CryptoError("密码不对，或文件被改动过（GCM 校验失败）")


def read_header(path: Path) -> Dict[str, Any]:
    """只读头部（不需要密码），用于在界面上显示"这是加密包、什么时候做的" """
    with open(path, "rb") as f:
        blob = f.read(4096)
    if not blob.startswith(MAGIC):
        raise CryptoError("不是加密包")
    off = len(MAGIC)
    hlen = int.from_bytes(blob[off:off + HEADER_LEN_BYTES], "big")
    off += HEADER_LEN_BYTES
    return json.loads(blob[off:off + hlen].decode("utf-8"))
