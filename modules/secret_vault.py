# -*- coding: utf-8 -*-
"""本机密钥库（用于迁移包的「占位符回填」）

背景：导出配置时把密钥值换成 `__ZF_SECRET__:<slot>` 占位符更安全，但导入到
**同一台机器**时如果还得手动一个个填回去，那这个安全设计就变成折磨。
折中：本机留一份加密库，记"本机当时的明文值"；导入时占位符自动回填本机现值，
换到别的机器（库里没有）就保留占位符并在报告里提示补录。

密钥派生：机器绑定材料（MachineGuid + 用户名 + 固定盐）→ SHA-256 → AES-256-GCM。
落盘：`$ZF_DATA_DIR/secret_vault.zfv`（容器复用 config_crypto 的格式）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import config_crypto

VAULT_NAME = "secret_vault.zfv"
PX = "__ZF_SECRET__"


def _data_dir() -> Path:
    raw = os.environ.get("ZF_DATA_DIR") or ""
    if raw:
        return Path(raw)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZeroFall"


def _machine_material() -> str:
    parts = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            parts.append(str(winreg.QueryValueEx(k, "MachineGuid")[0]))
    except Exception:
        parts.append("noguid")
    parts.append(os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    parts.append(os.environ.get("COMPUTERNAME", "host"))
    return "|".join(parts)


def _vault_key() -> str:
    """把机器材料变成密码形态，交给 config_crypto 的 scrypt 派生"""
    h = hashlib.sha256(("zerofall-vault-v1|" + _machine_material()).encode("utf-8"))
    return base64.urlsafe_b64encode(h.digest()).decode("ascii")


def vault_path() -> Path:
    return _data_dir() / VAULT_NAME


def load() -> Dict[str, str]:
    p = vault_path()
    if not p.is_file():
        return {}
    try:
        blob = p.read_bytes()
        raw = config_crypto.decrypt_bytes(blob, _vault_key())
        data = json.loads(raw.decode("utf-8"))
        return {str(k): str(v) for k, v in (data.get("secrets") or {}).items()}
    except Exception:
        return {}


def save(secrets: Dict[str, str]) -> bool:
    p = vault_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"secrets": secrets}, ensure_ascii=False).encode("utf-8")
        p.write_bytes(config_crypto.encrypt_bytes(payload, _vault_key()))
        return True
    except Exception:
        return False


def put(slot: str, value: str) -> bool:
    if not slot or value is None or value == "":
        return False
    data = load()
    data[slot] = value
    return save(data)


def put_many(pairs: Dict[str, str]) -> int:
    data = load()
    n = 0
    for k, v in (pairs or {}).items():
        if k and v:
            data[k] = v
            n += 1
    if n:
        save(data)
    return n


def get(slot: str) -> Optional[str]:
    return load().get(slot)


def slots() -> List[str]:
    return sorted(load().keys())


def has(slot: str) -> bool:
    return slot in load()


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PX)


def slot_of(value: str) -> str:
    return value[len(PX):].lstrip(":") if is_placeholder(value) else ""


def placeholder(slot: str) -> str:
    return "{}:{}".format(PX, slot)
