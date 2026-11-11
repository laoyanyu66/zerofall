# -*- coding: utf-8 -*-
"""云端老格式迁移：Fernet(.enc, ×1.33) → ZFCH1（二进制 AES-256-GCM，×1.0）

为什么要迁移：老格式是 Fernet（AES-CBC + HMAC）再套一层 base64，实测 120.9 MB
的快照变成 161.2 MB。新容器只有几十字节/块的头部开销。22 份老包 1.65 GB，
迁完约 1.24 GB，省 0.4 GB。

安全三条：
  1. 先解密到临时文件并校验能解（必要时回退旧固定密码）
  2. 新容器加密完**再解一次**，与原文做 sha256 比对
  3. 全部通过才 os.replace 覆盖原文件；任何一步失败都保留原件、清掉临时文件

不改 .meta.json：界面按文件 magic 判断格式，元数据里没有格式字段。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import cloud_crypto

LEFTOVERS: List[str] = []


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _decrypt_any(src: Path, dst: Path, password: str) -> bool:
    """按老格式解（当前密码 → 旧固定密码）"""
    from utils import decrypt_file
    for pw in (password, cloud_crypto.LEGACY_PASSWORD):
        try:
            if decrypt_file(str(src), str(dst), pw):
                return True
        except Exception:
            continue
    return False


def migrate_file(path: Path, config=None, dry_run: bool = False) -> Dict[str, Any]:
    path = Path(path)
    before = path.stat().st_size if path.is_file() else 0
    if not path.is_file():
        return {"file": path.name, "status": "missing"}
    if cloud_crypto.is_v2(path):
        return {"file": path.name, "status": "already", "before": before, "after": before}

    password = cloud_crypto.get_cloud_password(config)
    plain = path.with_name(path.name + ".zf-plain")
    new_enc = path.with_name(path.name + ".zf-new")
    verify = path.with_name(path.name + ".zf-verify")
    try:
        if not _decrypt_any(path, plain, password):
            return {"file": path.name, "status": "failed-decrypt", "before": before}
        digest = _sha256(plain)
        if not cloud_crypto.encrypt_file_v2(str(plain), str(new_enc), password):
            return {"file": path.name, "status": "failed-encrypt", "before": before}
        if not cloud_crypto.decrypt_file_v2(str(new_enc), str(verify), password):
            return {"file": path.name, "status": "failed-verify-decrypt", "before": before}
        if _sha256(verify) != digest:
            return {"file": path.name, "status": "failed-verify-hash", "before": before}

        after = new_enc.stat().st_size
        if dry_run:
            return {"file": path.name, "status": "would-migrate",
                    "before": before, "after": after,
                    "saved": before - after}
        os.replace(str(new_enc), str(path))
        return {"file": path.name, "status": "migrated", "before": before, "after": after,
                "saved": before - after}
    finally:
        for tmp in (plain, verify, new_enc):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError as e:
                # 同步盘客户端可能短暂占用文件：静默吞掉会让明文残留在同步目录里
                LEFTOVERS.append(str(tmp))
                print("[cloud_migrate] 临时文件删除失败（{}），请稍后清理: {}".format(e, tmp))


def migrate_dir(directory: Path, config=None, dry_run: bool = True,
                limit: Optional[int] = None) -> Dict[str, Any]:
    directory = Path(directory)
    files = sorted(directory.glob("*.enc")) if directory.is_dir() else []
    if limit:
        files = files[:limit]
    rows: List[Dict[str, Any]] = [migrate_file(f, config, dry_run) for f in files]
    stats = {"total": len(rows), "migrated": 0, "would": 0, "already": 0, "failed": 0,
             "before": 0, "after": 0, "saved": 0}
    for r in rows:
        st = r["status"]
        if st == "migrated":
            stats["migrated"] += 1
        elif st == "would-migrate":
            stats["would"] += 1
        elif st == "already":
            stats["already"] += 1
        elif st.startswith("failed"):
            stats["failed"] += 1
        stats["before"] += int(r.get("before") or 0)
        stats["after"] += int(r.get("after") or 0)
    stats["saved"] = stats["before"] - stats["after"]
    stats["saved_gb"] = round(stats["saved"] / (1024 ** 3), 3)
    stats["dry_run"] = dry_run
    stats["rows"] = rows
    return stats


def format_lines(res: Dict[str, Any]) -> List[str]:
    out = ["云端格式迁移（{}）".format("试算" if res.get("dry_run") else "已执行")]
    for r in res.get("rows", []):
        if r["status"] == "already":
            continue
        out.append("  {:<12} {:<42} {:>9.1f} MB → {:>9.1f} MB".format(
            r["status"], r["file"],
            (r.get("before") or 0) / (1024 ** 2), (r.get("after") or 0) / (1024 ** 2)))
    out.append("— 共 {} 份：迁移 {} / 试算 {} / 已是新格式 {} / 失败 {}，预计省 {:.3f} GB".format(
        res.get("total", 0), res.get("migrated", 0), res.get("would", 0),
        res.get("already", 0), res.get("failed", 0), res.get("saved_gb", 0)))
    return out

def rekey_file(path: Path, old_pw: str, new_pw: str, dry_run: bool = False) -> Dict[str, Any]:
    """把一份 ZFCH1 从旧密码换成新密码（密码分裂的修复路径）

    2026-11-11 的真实教训：迁移脚本先是从源码树跑的，secret_store 就新生成了
    一把孤儿密码，把两份云端包加成了那把密码 —— 同一目录里出现两把钥匙开不同的包。
    这个函数就是用来把"孤儿密码"加密的包重新归队到主密码。
    """
    path = Path(path)
    if not path.is_file():
        return {"file": path.name, "status": "missing"}
    if not cloud_crypto.is_v2(path):
        return {"file": path.name, "status": "not-v2"}
    plain = path.with_name(path.name + ".zf-plain")
    new_enc = path.with_name(path.name + ".zf-new")
    verify = path.with_name(path.name + ".zf-verify")
    try:
        if not cloud_crypto.decrypt_file_v2(str(path), str(plain), old_pw):
            return {"file": path.name, "status": "old-pw-mismatch"}
        digest = _sha256(plain)
        if not cloud_crypto.encrypt_file_v2(str(plain), str(new_enc), new_pw):
            return {"file": path.name, "status": "failed-encrypt"}
        if not cloud_crypto.decrypt_file_v2(str(new_enc), str(verify), new_pw):
            return {"file": path.name, "status": "failed-verify-decrypt"}
        if _sha256(verify) != digest:
            return {"file": path.name, "status": "failed-verify-hash"}
        if dry_run:
            return {"file": path.name, "status": "would-rekey"}
        os.replace(str(new_enc), str(path))
        return {"file": path.name, "status": "rekeyed"}
    finally:
        for tmp in (plain, verify, new_enc):
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass


def rekey_dir(directory: Path, old_pw: str, new_pw: str, dry_run: bool = True) -> Dict[str, Any]:
    directory = Path(directory)
    rows = [rekey_file(f, old_pw, new_pw, dry_run)
            for f in sorted(directory.glob("*.enc")) if directory.is_dir()]
    out = {"total": len(rows), "rekeyed": 0, "would": 0, "skipped": 0,
           "dry_run": dry_run, "rows": rows}
    for r in rows:
        if r["status"] == "rekeyed":
            out["rekeyed"] += 1
        elif r["status"] == "would-rekey":
            out["would"] += 1
        else:
            out["skipped"] += 1
    return out


def cleanup_leftovers(directory: Path, dry_run: bool = True) -> Dict[str, Any]:
    """清掉上次中断留下的 .zf-plain / .zf-new / .zf-verify

    为什么必须有：同步盘（百度网盘）会占用刚写完的文件，unlink 失败时如果只是
    print 一句，**明文快照就会躺在会同步到云的目录里** —— 那是真泄漏，不只是垃圾。
    """
    directory = Path(directory)
    found = []
    if directory.is_dir():
        for pat in ("*.zf-plain", "*.zf-new", "*.zf-verify"):
            found.extend(directory.glob(pat))
    removed, freed = 0, 0
    for f in found:
        try:
            sz = f.stat().st_size
            if not dry_run:
                f.unlink()
                removed += 1
                freed += sz
        except OSError:
            continue
    return {"found": len(found), "removed": removed,
            "freed_mb": round(freed / (1024 * 1024), 1), "dry_run": dry_run,
            "files": [str(f) for f in found]}
