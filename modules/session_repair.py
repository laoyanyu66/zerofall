# -*- coding: utf-8 -*-
"""DSH 会话文件扫描与修复（session.v4.jsonl.zstd）

对齐 imxzt/dsh-config-manager 的 F 段（替代已移除的 undo_scan），处理两种损坏：

  1. **单帧布局违规（legacy）**：整份日志压成一帧。DSH 的写入器是「一条记录一帧」，
     单帧会让增量追加/恢复逻辑对不上 —— 修法：拆成「header 帧 + 其余帧」。
  2. **synthetic-closer 重叠**：崩溃恢复留下的合成收尾帧导致 seq 重复 ——
     修法：按 seq 去重（保留首次出现）。

实现要点：
  · zstd 的读写全交给 `tools/session_zstd.cjs`（Node 24 自带 zstd，且必须先按
    RFC 8878 走帧头算边界——Node 的 zstdDecompressSync 只解第一帧）
  · 修复前**必做** `.bak-<时间戳>` 备份；修复后三重校验（能再解、每行合法 JSON、无重复 seq），
    任一不过就回滚备份并报错
  · 只读扫描随时可跑；修复要显式传 apply=True
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOL_REL = Path("tools") / "session_zstd.cjs"
SESSION_GLOB = "session.v4.jsonl.zstd"


class SessionRepairError(Exception):
    pass


def find_node(plugins_dir: str = "") -> Optional[str]:
    """找能解 zstd 的 node

    不能只写死路径：DSH 换安装位置、换机器、或用别的 runtime 目录都找不到。
    顺序：$DSH_HOME/dsh-runtimes/*/… → 常见安装目录下已知布局 → PATH。
    """
    cands: List[Path] = []
    if plugins_dir:
        base = Path(plugins_dir)
        for pat in ("dsh-runtimes/*/dependencies/node/bin/node.exe",
                    "dsh-runtimes/*/node/bin/node.exe",
                    "runtime/*/dependencies/node/bin/node.exe"):
            try:
                cands.extend(sorted(base.glob(pat)))
            except OSError:
                pass
    for root in (Path(r"D:\DeepSeek\resources"), Path(r"C:\Program Files\DeepSeek"),
                 Path(r"C:\Program Files (x86)\DeepSeek")):
        if not root.is_dir():
            continue
        for pat in ("runtime/*/dependencies/node/bin/node.exe",
                    "runtime/primary-runtime/dependencies/node/bin/node.exe",
                    "app/dsh/node_modules/.bin/node.exe"):
            try:
                cands.extend(sorted(root.glob(pat)))
            except OSError:
                pass
    cands.append(Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" /
                 "dependencies" / "node" / "bin" / "node.exe")
    for c in cands:
        try:
            if Path(c).is_file():
                return str(c)
        except OSError:
            continue
    return shutil.which("node")


def tool_path() -> Path:
    """帧助手的位置：源码运行时在项目根 tools/，onefile 运行时在 _MEIPASS/tools/"""
    import sys as _sys
    base = getattr(_sys, "_MEIPASS", None)
    if base:
        p = Path(base) / TOOL_REL
        if p.is_file():
            return p
    return Path(__file__).resolve().parent.parent / TOOL_REL


def _run(node: str, args: List[str], timeout: int = 180) -> Dict[str, Any]:
    tool = tool_path()
    if not tool.is_file():
        raise SessionRepairError(f"缺会话帧助手：{tool}")
    proc = subprocess.run([node, str(tool)] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        raise SessionRepairError(f"助手无输出（exit={proc.returncode}）：{(proc.stderr or '')[:200]}")
    try:
        data = json.loads(out[-1])
    except Exception as e:
        raise SessionRepairError(f"助手输出不是 JSON：{out[-1][:200]}") from e
    if "error" in data:
        raise SessionRepairError(data["error"])
    return data


def session_files(home: Path) -> List[Path]:
    root = Path(home) / "sessions"
    if not root.is_dir():
        return []
    return sorted(root.rglob(SESSION_GLOB))


def generation_of(path: Path) -> int:
    """从文件名取会话格式代际：session.v4.jsonl.zstd → 4"""
    import re as _re
    m = _re.search(r"session\.v(\d+)\.", Path(path).name)
    return int(m.group(1)) if m else 0


def scan_one(path: Path, node: Optional[str] = None) -> Dict[str, Any]:
    gen = generation_of(path)
    if gen != 4:
        # 其它代际的帧布局没有实测样本：不解析、不猜，只报出来
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        return {"path": str(path), "name": Path(path).parent.name,
                "bytes": size, "frames": 0, "lines": 0, "valid_json": 0,
                "invalid_json": 0, "dup_seq": 0, "frame_error_count": 0,
                "single_frame_layout": False, "generation": gen,
                "state": "unsupported_generation"}
    node = node or find_node()
    if not node:
        raise SessionRepairError("找不到 node（用于 zstd 解帧）")
    stats = _run(node, ["stats", str(path)])
    stats["name"] = path.parent.name or path.name
    stats["path"] = str(path)
    stats["state"] = classify(stats)
    stats["generation"] = gen
    return stats


def classify(stats: Dict[str, Any]) -> str:
    if stats.get("frame_error_count"):
        return "frame_error"
    if stats.get("invalid_json"):
        return "invalid_json"
    if stats.get("single_frame_layout"):
        return "legacy_single_frame"
    if stats.get("dup_seq"):
        return "seq_overlap"
    return "ok"


STATE_LABEL = {
    "ok": "正常",
    "legacy_single_frame": "单帧布局（应拆帧）",
    "seq_overlap": "seq 重复（合成收尾帧）",
    "invalid_json": "有非法 JSON 行（只报不修）",
    "frame_error": "帧结构损坏（只报不修）",
    "missing": "文件不存在",
    "unsupported_generation": "非 v4 代际（只报不改）",
}


def scan_all(home: Path, node: Optional[str] = None) -> Dict[str, Any]:
    node = node or find_node()
    files = session_files(home)
    rows, errors = [], []
    for f in files:
        try:
            rows.append(scan_one(f, node))
        except Exception as e:
            errors.append({"path": str(f), "error": str(e)})
    summary = {"ok": 0, "legacy_single_frame": 0, "seq_overlap": 0,
               "invalid_json": 0, "frame_error": 0, "unsupported_generation": 0}
    for r in rows:
        summary[r["state"]] = summary.get(r["state"], 0) + 1
    return {"node": node, "files": rows, "errors": errors,
            "summary": summary, "total": len(rows),
            "repairable": sum(1 for r in rows if r["state"] in ("legacy_single_frame", "seq_overlap"))}


def repair_one(path: Path, node: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
    """修复单个会话文件

    apply=False 时只做"要不要修/怎么修"的判定（dry-run）。
    """
    node = node or find_node()
    if not node:
        raise SessionRepairError("找不到 node（用于 zstd 解帧）")
    before = scan_one(path, node)
    state = before["state"]
    if state == "ok":
        return {"path": str(path), "action": "none", "message": "已经正常", "before": before}
    if state in ("invalid_json", "frame_error"):
        return {"path": str(path), "action": "refuse",
                "message": "这一种只报不修（数据可能是真损坏，乱改更糟）", "before": before}
    if state == "unsupported_generation":
        return {"path": str(path), "action": "refuse",
                "message": "会话格式为 v{}，只处理 v4（没有样本的布局不猜着改）".format(
                    before.get("generation", 0)), "before": before}

    mode = "--dedupe-seq" if state == "seq_overlap" else ""
    args = ["rewrite", str(path), "__DST__"] + ([mode] if mode else [])
    if not apply:
        return {"path": str(path), "action": "would_repair",
                "message": ("将按 seq 去重重写" if state == "seq_overlap" else "将拆成一条记录一帧"),
                "before": before}

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{ts}")
    shutil.copy2(path, bak)
    tmp = path.with_name(f"{path.name}.zf-tmp")
    try:
        res = _run(node, ["rewrite", str(path), str(tmp)] + ([mode] if mode else []))
        after = _run(node, ["stats", str(tmp)])
    except Exception as e:
        tmp.unlink(missing_ok=True)
        bak.unlink(missing_ok=True)
        raise SessionRepairError(f"重写失败：{e}") from e

    problems = []
    if after.get("invalid_json"):
        problems.append(f"仍有 {after['invalid_json']} 行非法 JSON")
    if after.get("dup_seq"):
        problems.append(f"仍有 {after['dup_seq']} 个重复 seq")
    if after.get("frame_error_count"):
        problems.append(f"仍有 {after['frame_error_count']} 个帧错误")
    if after.get("lines", 0) != res.get("lines_written", -1):
        problems.append("行数与重写结果不一致")
    if problems:
        tmp.unlink(missing_ok=True)
        bak.unlink(missing_ok=True)
        raise SessionRepairError("修复后校验不过，已放弃并保留原件：" + "；".join(problems))

    os.replace(tmp, path)
    return {"path": str(path), "action": "repaired", "backup": str(bak),
            "before": before, "after": after, "dropped_dup_seq": res.get("dropped_dup_seq", 0),
            "frames_written": res.get("frames_written")}


def repair_all(home: Path, node: Optional[str] = None, apply: bool = False) -> Dict[str, Any]:
    scan = scan_all(home, node)
    results = []
    for row in scan["files"]:
        if row["state"] not in ("legacy_single_frame", "seq_overlap"):
            continue
        try:
            results.append(repair_one(Path(row["path"]), node, apply=apply))
        except Exception as e:
            results.append({"path": row["path"], "action": "error", "message": str(e)})
    return {"scan": scan, "results": results,
            "repaired": sum(1 for r in results if r["action"] == "repaired"),
            "would": sum(1 for r in results if r["action"] == "would_repair"),
            "errors": sum(1 for r in results if r["action"] == "error")}
