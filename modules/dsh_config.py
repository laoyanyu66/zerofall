# -*- coding: utf-8 -*-
"""DSH profile 配置编辑（融合 @deepseek-ai/dsh-config-editor 的语义）

DSH 官方那套（`@deepseek-ai/dsh-config-editor`）做三件事：
  1. 只改 profile 层的 `cordis.patch.yml`（顶层 YAML 序列）
  2. 按 entry id 定位「最后一条不含 insert 的同 id 行」，把插件整体 config 写进去；
     config 与继承值相同时删掉这一行，恢复"跟随上层默认"
  3. 原子写 + 失败回滚 + 触发 Loader reconcile

ZeroFall 里没有 Loader（改完要靠重启/热重载生效），所以这里保留 1、2、3 的落盘部分，
不碰 reconcile；另外按 ZeroFall 的习惯加两件事：写前备份、变更前后 diff。

**为什么不用 ruamel/PyYAML 整文件重写**：实测把 13.7 KB 的 profile 补丁读进
round-trip 再 dump，缩进与序列风格会漂（495~660 行 diff），配置文件被"格式化"一遍
比改错还糟。所以这里做**按行的块替换**：只动目标 entry 的 `config:` 块，
其余字节原样保留（注释、`!!js` 表达式、引号风格全部不动），
改完再用 PyYAML 整篇解析校验一次。
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # PyYAML 是硬依赖；缺了就给明确报错，不要静默降级
    import yaml
except Exception as _e:  # pragma: no cover - 依赖缺失时的提示
    raise ImportError("dsh_config 需要 PyYAML（requirements.txt 里已声明）") from _e

JS_TAG = "tag:yaml.org,2002:js"


class ConfigError(Exception):
    """配置编辑失败（语法、结构、目标不存在等）"""


class _Loader(yaml.SafeLoader):
    """保留 !!js 表达式：DSH 用它在补丁里写表达式，解析成普通字符串即可"""


def _js_ctor(loader, node):
    return loader.construct_scalar(node)


_Loader.add_constructor(JS_TAG, _js_ctor)
try:  # 老写法/包名变体
    _Loader.add_constructor("!!js", _js_ctor)
except Exception:
    pass


# ──────────────────────────────────────────────────────────────── 解析


def parse_entries(text: str) -> List[Dict[str, Any]]:
    """解析 profile 补丁为 entry 列表；结构不对直接抛 ConfigError"""
    if not text.strip():
        return []
    try:
        doc = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 语法错误：{e}") from e
    if doc is None:
        return []
    if not isinstance(doc, list):
        raise ConfigError("profile 补丁必须是顶层 YAML 序列（以 `- id:` 开头）")
    out = []
    for i, row in enumerate(doc):
        if not isinstance(row, dict):
            raise ConfigError(f"第 {i + 1} 个条目不是映射，profile 补丁里应全是 `- id:` 行")
        out.append(row)
    return out


def validate(text: str) -> Tuple[bool, str]:
    """整篇校验：能被解析、是序列、每行有 id"""
    try:
        rows = parse_entries(text)
    except ConfigError as e:
        return False, str(e)
    for i, row in enumerate(rows):
        if "id" not in row:
            return False, f"第 {i + 1} 个条目缺少 id"
        if "insert" in row:
            continue
        if "config" in row and not isinstance(row["config"], (dict, list, type(None))):
            return False, f"条目 {row.get('id')} 的 config 不是映射"
    return True, "结构 OK"


# ──────────────────────────────────────────────────────────────── 行级定位


_ITEM_RE = re.compile(r"^-\s")
_KEY_RE = re.compile(r"^(\s+)([A-Za-z0-9_.\-]+)\s*:")


def _entry_spans(lines: List[str]) -> List[Tuple[int, int]]:
    """返回每个顶层条目的 [start, end) 行区间（end 为下一个 `- ` 行）"""
    starts = [i for i, ln in enumerate(lines) if _ITEM_RE.match(ln)]
    spans = []
    for idx, s in enumerate(starts):
        e = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        # 尾部空行/注释不计入条目（但保留在文件里）
        while e > s + 1 and not lines[e - 1].strip():
            e -= 1
        spans.append((s, e))
    return spans


def _entry_id(lines: List[str], start: int, end: int) -> str:
    for i in range(start, end):
        m = re.match(r"^-\s+id\s*:\s*(.+?)\s*$", lines[i])
        if m:
            raw = m.group(1).strip()
            if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
                raw = raw[1:-1]
            return raw
    return ""


def find_entry(text: str, entry_id: str) -> Optional[Tuple[int, int]]:
    """定位 entry 的 [start, end) 行区间；没有返回 None"""
    lines = text.splitlines()
    spans = _entry_spans(lines)
    hit = None
    for s, e in spans:                    # 取最后一条，与官方 findLastIndex 一致
        if _entry_id(lines, s, e) == entry_id:
            hit = (s, e)
    return hit


def _config_block(lines: List[str], start: int, end: int) -> Optional[Tuple[int, int]]:
    """在条目区间内找 `  config:` 块，返回 (config 行号, 块结束行号)"""
    cfg_start = None
    for i in range(start, end):
        if re.match(r"^\s{1,4}config\s*:\s*$", lines[i]) or re.match(r"^\s{1,4}config\s*:\s*\S", lines[i]):
            cfg_start = i
            break
    if cfg_start is None:
        return None
    base_indent = len(lines[cfg_start]) - len(lines[cfg_start].lstrip())
    i = cfg_start + 1
    while i < end:
        ln = lines[i]
        if ln.strip():
            indent = len(ln) - len(ln.lstrip())
            if indent <= base_indent:
                break
        i += 1
    while i > cfg_start + 1 and not lines[i - 1].strip():
        i -= 1
    return cfg_start, i


# ──────────────────────────────────────────────────────────────── 文本操作


def get_entry_config(text: str, entry_id: str) -> Dict[str, Any]:
    rows = parse_entries(text)
    hit = None
    for row in rows:                       # 最后一条生效，与 findLastIndex 一致
        if row.get("id") == entry_id and "insert" not in row:
            hit = row
    if hit is None:
        raise ConfigError(f"找不到条目 {entry_id}")
    cfg = hit.get("config")
    return cfg if isinstance(cfg, dict) else {}


def config_block_text(text: str, entry_id: str) -> str:
    """返回该条目 config 块的**原文片段**（去掉基准缩进）

    用于界面回显：保留 `!!js` 表达式和原有排版，用户看到的与文件里一致，
    改完再走 set_entry_config_text 落盘，不会被"格式化"一遍。
    """
    lines = text.splitlines()
    span = find_entry(text, entry_id)
    if span is None:
        raise ConfigError(f"找不到条目 {entry_id}")
    cfg = _config_block(lines, *span)
    if cfg is None:
        return ""
    start, end = cfg
    header = lines[start]
    inline = header.split(":", 1)[1].strip() if ":" in header else ""
    if inline:
        return inline + "\n"
    body = lines[start + 1:end]
    indents = [len(l) - len(l.lstrip()) for l in body if l.strip()]
    cut = min(indents) if indents else 0
    out = "\n".join((l[cut:] if l.strip() else "") for l in body)
    return (out.rstrip() + "\n") if out.strip() else ""

def has_override(text: str, entry_id: str) -> bool:
    rows = parse_entries(text)
    return any(row.get("id") == entry_id and "insert" not in row and "config" in row
               for row in rows)


def _indent_block(fragment: str, indent: int) -> List[str]:
    out = []
    for ln in fragment.splitlines():
        out.append((" " * indent + ln) if ln.strip() else "")
    return out


def set_entry_config_text(text: str, entry_id: str,
                          config_text: str) -> str:
    """把 entry 的 config 块替换成 config_text（YAML 片段，2 空格基准）

    config_text 为空 → 删掉 config 块（回到"跟随上层默认"）。
    只替换目标块，其余字节不动。
    """
    frag = (config_text or "").strip("\n")
    if frag.strip():
        try:
            parsed = yaml.load(frag, Loader=_Loader)
        except yaml.YAMLError as e:
            raise ConfigError(f"config 片段语法错误：{e}") from e
        if parsed is not None and not isinstance(parsed, dict):
            raise ConfigError("config 片段必须是映射（key: value）")

    lines = text.splitlines()
    span = find_entry(text, entry_id)
    if span is None:
        if not frag.strip():
            raise ConfigError(f"找不到条目 {entry_id}")
        block = ["- id: {}".format(entry_id), "  config:"] + _indent_block(frag, 4)
        body = "\n".join(lines).rstrip("\n")
        return (body + "\n\n" if body else "") + "\n".join(block) + "\n"

    start, end = span
    cfg = _config_block(lines, start, end)
    entry_indent = len(lines[start]) - len(lines[start].lstrip()) + 2   # `- ` 之后键的缩进
    cfg_indent = entry_indent + 2

    if cfg is None:
        if not frag.strip():
            return text
        # 没有 config 块：插到条目末尾（end 之前）
        insert_at = end
        block = [" " * entry_indent + "config:"] + _indent_block(frag, cfg_indent)
        new_lines = lines[:insert_at] + block + lines[insert_at:]
        return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")

    cfg_start, cfg_end = cfg
    if not frag.strip():
        new_lines = lines[:cfg_start] + lines[cfg_end:]
    else:
        block = [lines[cfg_start]] + _indent_block(frag, cfg_indent)
        new_lines = lines[:cfg_start] + block + lines[cfg_end:]
    out = "\n".join(new_lines)
    return out + "\n" if text.endswith("\n") else out


def set_entry_disabled_text(text: str, entry_id: str, disabled: bool) -> str:
    """给条目加/去掉顶层 `disabled:` 键（行级插入/删除，不动其它内容）"""
    lines = text.splitlines()
    span = find_entry(text, entry_id)
    if span is None:
        raise ConfigError(f"找不到条目 {entry_id}")
    start, end = span
    key = None
    for i in range(start, end):
        m = _KEY_RE.match(lines[i])
        if m and m.group(2) == "disabled":
            key = i
            break
    if disabled:
        if key is not None:
            indent = len(lines[key]) - len(lines[key].lstrip())
            lines[key] = " " * indent + "disabled: true"
        else:
            entry_indent = len(lines[start]) - len(lines[start].lstrip()) + 2
            lines.insert(start + 1, " " * entry_indent + "disabled: true")
    else:
        if key is None:
            return text
        del lines[key]
    out = "\n".join(lines)
    return out + "\n" if text.endswith("\n") else out


def remove_override_text(text: str, entry_id: str) -> str:
    """删掉 entry 的 config 块；条目只剩 id/name 时整条删掉（与官方一致）"""
    out = set_entry_config_text(text, entry_id, "")
    rows = parse_entries(out)
    span = find_entry(out, entry_id)
    if span is None:
        return out
    lines = out.splitlines()
    start, end = span
    body_lines = [ln for ln in lines[start:end] if ln.strip()]
    keys = {(_KEY_RE.match(ln).group(2) if _KEY_RE.match(ln) else None) for ln in body_lines}
    keys.discard(None)
    if keys and keys <= {"id", "name"}:
        del lines[start:end]
        # 顺带吃掉紧随其后的一条空行，避免留双空行
        if start < len(lines) and not lines[start].strip():
            del lines[start]
        out2 = "\n".join(lines)
        return out2 + "\n" if out.endswith("\n") else out2
    return out


# ──────────────────────────────────────────────────────────────── 落盘


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def backup_file(path: Path, tag: str = "zf-config") -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{tag}-{ts}")
    shutil.copy2(path, bak)
    return bak


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".zf-cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply_text(path: Path, new_text: str, *, auto_backup: bool = True,
               validator=None) -> Dict[str, Any]:
    """校验 → 备份 → 原子写；失败时保持原文件不动

    validator: callable(text) -> (ok, msg)。默认是 profile 补丁的结构校验；
    写 prompt-inject.md / agent 预设这类文件时传它们各自的校验器
    （否则 markdown 会被当成"不是 YAML 序列"直接拒掉）。
    """
    path = Path(path)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    check = validator if validator is not None else validate
    ok, msg = check(new_text)
    if not ok:
        raise ConfigError(msg)
    if before == new_text:
        return {"changed": False, "backup": None, "before": before, "after": new_text,
                "message": "内容无变化"}
    bak = None
    if auto_backup and path.exists():
        bak = backup_file(path)
    atomic_write(path, new_text)
    # 写后回读确认（磁盘上真的是这份内容）
    disk = path.read_text(encoding="utf-8")
    if disk != new_text:
        if bak is not None:
            shutil.copy2(bak, path)
        raise ConfigError("写后回读不一致，已还原备份")
    return {"changed": True, "backup": str(bak) if bak else None,
            "before": before, "after": new_text, "message": "已写入"}


def unified_diff(before: str, after: str, name: str = "cordis.patch.yml") -> str:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                fromfile=f"{name} (改前)", tofile=f"{name} (改后)",
                                lineterm="", n=2)
    return "\n".join(diff)


# ──────────────────────────────────────────────────────────────── profile 定位


def resolve_profile_dir(plugins_dir: str, profile: str = "desktop") -> Optional[Path]:
    """$DSH_HOME/profiles/<profile>，存在 cordis.patch.yml 才算找到"""
    if not plugins_dir:
        return None
    base = Path(plugins_dir) / "profiles"
    cand = base / profile
    if (cand / "cordis.patch.yml").exists():
        return cand
    return None


def list_profiles(plugins_dir: str) -> List[str]:
    base = Path(plugins_dir) / "profiles"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and (p / "cordis.patch.yml").exists())


def patch_path(profile_dir: Path) -> Path:
    return Path(profile_dir) / "cordis.patch.yml"

# ──────────────────────────────────────────────────────────────── 其它 DSH 配置面
#
# 「配置管理」不只有 profile 补丁：DSH 还有提示词注入稿和 agent 预设，
# 它们同样是纯文本、同样会被手改坏、同样需要"改前备份 + 改后可回滚"。
# 下面这组函数把这三类文件统一到同一套 metrics / save / backup / restore 上。

INJECT_NAME = "prompt-inject.md"
PRESET_ROOT = ".agent-presets"
MANAGED_BACKUP_MARKS = (".bak-zf-config-", ".bak-before-", ".bak-before-restore-")


def inject_path(plugins_dir: str) -> Path:
    """$DSH_HOME/prompt-inject.md（dsh-purge 每步重贴的那份）"""
    return Path(plugins_dir) / INJECT_NAME


def estimate_tokens(text: str) -> int:
    """可复现的 token 估算：CJK 按 1 token/字，其余按 3.6 字符/token。

    与 dsh-purge 的 estimateInjectTokens 同一口径，两边数字可以直接对比
    （16,847 字节的中英混排稿 ≈ 4,489 token/步）。
    """
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    rest = max(0, len(text) - cjk)
    return int(round(cjk + rest / 3.6))


def text_metrics(text: str) -> Dict[str, Any]:
    return {
        "bytes": len(text.encode("utf-8")),
        "chars": len(text),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "tokens": estimate_tokens(text),
    }


def preset_root(plugins_dir: str) -> Path:
    return Path(plugins_dir) / PRESET_ROOT


def list_presets(plugins_dir: str) -> List[Dict[str, Any]]:
    """列出 $DSH_HOME/.agent-presets 下的预设（agent.cordis.yml / preset.yml）"""
    root = preset_root(plugins_dir)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        files = [f.name for f in d.iterdir() if f.is_file() and f.suffix in (".yml", ".yaml")]
        if not files:
            continue
        newest = max((f.stat().st_mtime for f in d.iterdir() if f.is_file()), default=0)
        out.append({"name": d.name, "dir": str(d), "files": sorted(files),
                    "mtime": newest})
    return out


def preset_file(plugins_dir: str, name: str, filename: str = "agent.cordis.yml") -> Path:
    return preset_root(plugins_dir) / name / filename


def validate_yaml_text(text: str) -> Tuple[bool, str]:
    """通用 YAML 校验（预设不一定是序列，只查语法）"""
    try:
        yaml.load(text, Loader=_Loader)
        return True, "语法 OK"
    except yaml.YAMLError as e:
        return False, str(e)


def save_text(path: Path, text: str, *, validator=None, tag: str = "zf-config") -> Dict[str, Any]:
    """统一的"校验 → 备份 → 原子写 → 回读"入口

    validator: callable(text) -> (ok, msg)；None 表示只要求非空。
    """
    path = Path(path)
    if validator is not None:
        ok, msg = validator(text)
        if not ok:
            raise ConfigError(msg)
    elif not text.strip():
        raise ConfigError("内容为空，拒绝写入")
    return apply_text(path, text, auto_backup=True,
                      validator=validator or (lambda t: (True, "OK")))


def list_backups(paths: List[Path]) -> List[Dict[str, Any]]:
    """列出与这些文件相关的备份（按时间倒序）"""
    seen = {}
    for p in paths:
        p = Path(p)
        parent, name = p.parent, p.name
        if not parent.is_dir():
            continue
        for f in parent.iterdir():
            if not f.is_file() or not f.name.startswith(name):
                continue
            if not any(mark in f.name for mark in MANAGED_BACKUP_MARKS):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            seen[str(f)] = {"path": str(f), "of": str(p), "name": f.name,
                            "size": st.st_size, "mtime": st.st_mtime}
    return sorted(seen.values(), key=lambda d: d["mtime"], reverse=True)


def target_of_backup(backup: Path) -> Path:
    """从 foo.yml.bak-zf-config-20261111-101500 反推 foo.yml"""
    name = Path(backup).name
    for mark in MANAGED_BACKUP_MARKS:
        idx = name.find(mark)
        if idx > 0:
            return Path(backup).parent / name[:idx]
    return Path(backup).parent / name.split(".bak-")[0]


def restore_backup(backup: Path, target: Optional[Path] = None) -> Dict[str, Any]:
    """把备份还原回目标文件；还原前再给当前文件留一份 .bak-before-restore-*"""
    backup = Path(backup)
    if not backup.is_file():
        raise ConfigError(f"备份不存在：{backup}")
    target = Path(target) if target else target_of_backup(backup)
    text = backup.read_text(encoding="utf-8")
    if target.suffix in (".yml", ".yaml"):
        ok, msg = validate_yaml_text(text)
        if not ok:
            raise ConfigError(f"备份内容不是合法 YAML，已中止：{msg}")
    safety = None
    if target.exists():
        safety = backup_file(target, tag="before-restore")
    atomic_write(target, text)
    return {"target": str(target), "safety": str(safety) if safety else None,
            "bytes": len(text.encode("utf-8"))}

def prune_backups(paths: List[Path], keep: int = 10,
                  older_than_days: Optional[int] = None) -> Dict[str, Any]:
    """按目标文件分组裁掉旧备份

    keep：每个目标文件最多保留多少份（按 mtime 倒序，保留最新的）
    older_than_days：给了就再按天数淘汰（即便没超 keep，太老的也删）

    返回 {"removed": [...], "kept": n, "freed_bytes": n}。
    只删 MANAGED_BACKUP_MARKS 命名的文件——用户自己的 .bak 不碰。
    """
    import time as _time
    rows = list_backups([Path(p) for p in paths])
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["of"], []).append(r)
    removed, kept, freed = [], 0, 0
    cutoff = None
    if older_than_days and older_than_days > 0:
        cutoff = _time.time() - older_than_days * 86400
    for _target, items in groups.items():
        items.sort(key=lambda d: d["mtime"], reverse=True)
        for idx, r in enumerate(items):
            too_many = idx >= max(1, int(keep))
            too_old = cutoff is not None and r["mtime"] < cutoff
            if not (too_many or too_old):
                kept += 1
                continue
            try:
                Path(r["path"]).unlink()
                removed.append(r["path"])
                freed += int(r.get("size") or 0)
            except OSError:
                kept += 1
    return {"removed": removed, "kept": kept, "freed_bytes": freed}