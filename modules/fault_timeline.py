# -*- coding: utf-8 -*-
"""故障时间线：上次成功启动以来到底变了什么

对齐 dsh-plugin-ops 的 fault timeline（"attribution baseline"）。
思路很简单但很管用：每次 DSH **成功起来**就记一条基线（插件目录 + 配置的指纹），
之后每次崩溃/异常/关键变更再记一条。崩的时候不用猜，直接看"自上次成功以来
发生了什么"。

存储：`$ZF_DATA_DIR/timeline.jsonl`，一行一条，追加写，超 5000 行自动旋转。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_LINES = 5000
SKIP_DIRS = {"node_modules", ".git", "cache", "Cache", "logs", "attachments",
             "dsh-runtimes", "primary-runtime", "rea-engines", "rea-jdk", "tools",
             "skills", "backups"}


def _data_dir() -> Path:
    raw = os.environ.get("ZF_DATA_DIR") or ""
    return Path(raw) if raw else Path(
        os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZeroFall"


def timeline_path() -> Path:
    return _data_dir() / "timeline.jsonl"


def fingerprint(paths: List[str], limit_files: int = 6000) -> str:
    """对一组目录做轻量指纹：相对路径 + 大小 + mtime（不读内容，快）"""
    h = hashlib.sha256()
    n = 0
    for raw in paths:
        root = Path(raw or "")
        if not root.is_dir():
            continue
        h.update(str(root).lower().encode("utf-8"))
        for p in root.rglob("*"):
            if n >= limit_files:
                break
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                if not p.is_file():
                    continue
                st = p.stat()
            except OSError:
                continue
            n += 1
            h.update("{}|{}|{}".format(
                str(p.relative_to(root)).lower(), st.st_size, int(st.st_mtime)).encode("utf-8"))
    return h.hexdigest()[:16]


def _append(entry: Dict[str, Any]) -> None:
    p = timeline_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _rotate(p)
    except OSError:
        pass


def _rotate(p: Path) -> None:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= MAX_LINES:
            return
        keep = lines[-MAX_LINES:]
        p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def record(kind: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "kind": kind, "detail": detail}
    entry.update({k: v for k, v in extra.items() if v is not None})
    _append(entry)
    return entry


def mark_boot_ok(fp: str, version: str = "") -> Dict[str, Any]:
    return record("boot_ok", "DSH 启动成功" + (" ({})".format(version) if version else ""),
                  fingerprint=fp)


def record_crash(reason: str = "", fp: str = "") -> Dict[str, Any]:
    return record("crash", reason, fingerprint=fp or None)


def record_change(what: str, fp: str = "") -> Dict[str, Any]:
    return record("change", what, fingerprint=fp or None)


def read_all(limit: int = 500) -> List[Dict[str, Any]]:
    p = timeline_path()
    if not p.is_file():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except OSError:
        return []
    return out


def last_ok() -> Optional[Dict[str, Any]]:
    for e in reversed(read_all(limit=MAX_LINES)):
        if e.get("kind") == "boot_ok":
            return e
    return None


def since_last_ok(limit: int = 60) -> Dict[str, Any]:
    """上次成功启动之后的条目（不含那条本身）"""
    entries = read_all(limit=MAX_LINES)
    idx = None
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("kind") == "boot_ok":
            idx = i
            break
    if idx is None:
        tail = entries[-limit:]
        return {"baseline": None, "entries": tail, "count": len(tail)}
    tail = entries[idx + 1:][-limit:]
    return {"baseline": entries[idx], "entries": tail, "count": len(tail)}


def format_lines(res: Dict[str, Any]) -> List[str]:
    base = res.get("baseline")
    out = ["故障时间线"]
    out.append("基线：" + ("{} 启动成功（指纹 {}）".format(base.get("ts"), base.get("fingerprint"))
                          if base else "还没有成功启动记录"))
    if not res.get("entries"):
        out.append("自基线以来没有新事件 ✓")
    for e in res.get("entries", []):
        out.append("  {}  {:<10} {}".format(e.get("ts", ""), e.get("kind", ""),
                                            (e.get("detail") or "")[:110]))
    out.append("— 自基线以来 {} 条".format(res.get("count", 0)))
    return out
