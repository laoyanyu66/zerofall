# -*- coding: utf-8 -*-
"""家庭清洁：收拾备份/改写留下的残渣

为什么单独一个模块：ZeroFall 和 DSH 插件生态都会在 `$DSH_HOME` 下留
`.bak-*`（配置改动前备份）、`.zf-tmp`（原子写临时文件）、`.zf-conflict`
（三方合并冲突副本）。单个很小（实测 57 个 / 1.2 MB），但只增不减，
时间久了没人敢删 —— 这里给一个可预览、可回滚边界的清理入口。

安全边界：
  · 只动这三种后缀，不碰任何业务文件
  · 默认跳过 node_modules / .git / .zf-config-history（版本点自己管理）
  · dry_run 默认 True，先看清单再删
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PATTERNS = ("*.bak-*", "*.zf-tmp", "*.zf-conflict")
SKIP_DIRS = {"node_modules", ".git", ".zf-config-history", "cache", "Cache", "logs",
             "dsh-runtimes", "attachments", "backups"}


def _iter(home: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(str(home)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for name in filenames:
            for pat in PATTERNS:
                if base.joinpath(name).match(pat):
                    yield base / name
                    break


def scan(home: Path, min_age_days: float = 0) -> Dict[str, Any]:
    home = Path(home)
    now = time.time()
    items: List[Dict[str, Any]] = []
    total = 0
    for p in _iter(home):
        try:
            st = p.stat()
        except OSError:
            continue
        age = (now - st.st_mtime) / 86400.0
        if age < min_age_days:
            continue
        items.append({"path": str(p), "bytes": st.st_size, "age_days": round(age, 1),
                      "kind": _kind(p.name)})
        total += st.st_size
    by_kind: Dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1
    items.sort(key=lambda x: -x["bytes"])
    return {"home": str(home), "count": len(items), "bytes": total,
            "mb": round(total / (1024 * 1024), 2), "by_kind": by_kind, "items": items}


def _kind(name: str) -> str:
    if ".zf-tmp" in name:
        return "临时文件"
    if ".zf-conflict" in name:
        return "冲突副本"
    if "bak-zf-rewrite" in name:
        return "路径改写备份"
    if "bak-zf-config" in name:
        return "配置导入备份"
    return "普通备份"


def delete(home: Path, min_age_days: float = 0, dry_run: bool = True,
           limit: int = 5000) -> Dict[str, Any]:
    data = scan(home, min_age_days)
    removed, freed, errors = 0, 0, []
    if not dry_run:
        for it in data["items"][:limit]:
            try:
                Path(it["path"]).unlink()
                removed += 1
                freed += it["bytes"]
            except OSError as e:
                errors.append("{}: {}".format(it["path"], e))
    return {"dry_run": dry_run, "candidates": data["count"], "candidate_mb": data["mb"],
            "removed": removed, "freed_mb": round(freed / (1024 * 1024), 2),
            "errors": errors[:10], "by_kind": data["by_kind"]}
