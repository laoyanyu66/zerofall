# -*- coding: utf-8 -*-
"""恢复后的路径改写（换机器 / 改用户名 / 挪 $DSH_HOME 位置）

问题：快照里存的是"当时那台机器"的绝对路径。换台机器或改了用户名，
恢复回来的 `workspace.json`、插件配置、MCP 配置里全是旧路径，
DSH 启动后一堆"目录不存在"。

做法：快照创建时把源路径写进 `_zf_meta.json`；恢复时读出来，与本机当前
路径比对，不同就把文本文件里的旧前缀换成新前缀（改之前留 `.bak-zf-rewrite`）。
只碰文本类文件，二进制一律跳过。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

META_NAME = "_zf_meta.json"

TEXT_SUFFIXES = {
    ".yml", ".yaml", ".json", ".jsonl", ".md", ".txt", ".toml", ".ini", ".cfg",
    ".js", ".cjs", ".mjs", ".ts", ".ps1", ".cmd", ".bat", ".sh", ".env",
}

SKIP_DIR_NAMES = {
    "node_modules", ".git", "cache", "Cache", "logs", "dsh-runtimes",
    "primary-runtime", "attachments",
}


def write_meta(snapshot_dir: Path, config) -> Optional[Path]:
    """在快照目录里记下源机器的关键路径"""
    try:
        meta = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dsh_home": str(getattr(config, "deepseek_plugins_dir", "") or ""),
            "config_dir": str(getattr(config, "deepseek_config_dir", "") or ""),
            "session_dir": str(getattr(config, "deepseek_session_dir", "") or ""),
            "user_home": str(Path.home()),
            "appdata": str(getattr(config, "dsh_appdata_dir", "") or ""),
        }
        p = Path(snapshot_dir) / META_NAME
        p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return p
    except Exception:
        return None


def read_meta(extracted_dir: Path) -> Dict[str, str]:
    p = Path(extracted_dir) / META_NAME
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pairs(meta: Dict[str, str], config) -> List[Tuple[str, str]]:
    """旧前缀 → 新前缀；只保留真正变了的"""
    out: List[Tuple[str, str]] = []
    mapping = (
        ("dsh_home", getattr(config, "deepseek_plugins_dir", "")),
        ("config_dir", getattr(config, "deepseek_config_dir", "")),
        ("session_dir", getattr(config, "deepseek_session_dir", "")),
        ("user_home", str(Path.home())),
        ("appdata", getattr(config, "dsh_appdata_dir", "")),
    )
    for key, new in mapping:
        old = (meta.get(key) or "").strip()
        new = str(new or "").strip()
        if not old or not new or old == new:
            continue
        out.append((old, new))
    # 长的先替换，避免 dsh_home 是 user_home 子串时被半截替换
    out.sort(key=lambda x: -len(x[0]))
    return out


def _iter_text_files(root: Path, limit: int = 4000):
    n = 0
    for p in Path(root).rglob("*"):
        if n >= limit:
            return
        if not p.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        n += 1
        yield p


def rewrite_tree(root: Path, meta: Dict[str, str], config, *,
                 dry_run: bool = False) -> Dict[str, object]:
    """把 root 下文本文件里的旧前缀改成新前缀"""
    pairs = _pairs(meta, config)
    result = {"pairs": [list(p) for p in pairs], "files_scanned": 0,
              "files_changed": 0, "replacements": 0, "changed": [], "dry_run": dry_run}
    if not pairs:
        return result
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    for f in _iter_text_files(root):
        result["files_scanned"] += 1
        try:
            raw = f.read_bytes()
            if b"\x00" in raw[:2048]:        # 二进制兜底
                continue
            text = raw.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        hits = 0
        for old, new in pairs:
            # 文件里可能是 JSON/YAML 的原样路径，也可能是被转义成双反斜杠的形态
            variants = ((old, new), (old.replace("\\", "\\\\"), new.replace("\\", "\\\\")))
            for o, n in variants:
                if o and o in new_text:
                    hits += new_text.count(o)
                    new_text = new_text.replace(o, n)
        if not hits:
            continue
        result["files_changed"] += 1
        result["replacements"] += hits
        result["changed"].append(str(f))
        if dry_run:
            continue
        try:
            bak = f.with_name(f.name + ".bak-zf-rewrite-" + ts)
            bak.write_bytes(raw)
            f.write_text(new_text, encoding="utf-8")
        except OSError:
            continue
    return result


def rewrite_after_restore(extracted_dir: Path, config,
                          targets: Optional[List[Path]] = None) -> Dict[str, object]:
    """恢复流程的入口：读快照 meta，对本机恢复目标做改写"""
    meta = read_meta(extracted_dir)
    if not meta:
        return {"pairs": [], "files_scanned": 0, "files_changed": 0,
                "replacements": 0, "changed": [], "skipped": "快照里没有 _zf_meta.json"}
    dirs = targets or [Path(getattr(config, "deepseek_plugins_dir", "") or ""),
                       Path(getattr(config, "deepseek_config_dir", "") or "")]
    out = {"pairs": [], "files_scanned": 0, "files_changed": 0,
           "replacements": 0, "changed": []}
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        r = rewrite_tree(Path(d), meta, config)
        out["pairs"] = r["pairs"]
        for k in ("files_scanned", "files_changed", "replacements"):
            out[k] += r[k]
        out["changed"].extend(r["changed"])
    return out
