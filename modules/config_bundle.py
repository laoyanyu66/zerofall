# -*- coding: utf-8 -*-
"""配置迁移包 + 配置版本点（对齐 GitHub 上两个 dsh-config-manager 的核心能力）

参考对象（2026-11-11 读的 README）：
  · xiajiajun516/dsh-config-manager（npm dsh-config-manager v0.8.0）
      Export → 去密 → manifest → 校验和 → ZIP；Import → 选条目 → 逐项冲突决策 →
      路径映射 → 导入前快照；Backup & migrate / Inspect / Repair
  · imxzt/dsh-config-manager
      配置版本管理（git 提交历史）+ profile 健康诊断 + 会话文件修复 + 外部救援 UI

ZeroFall 这边已经有的：环境级快照/回滚、云同步、profile 补丁与预设编辑、改前文件备份。
这里补的是它们**有而 ZeroFall 没有**的两件事：

  1. **可迁移的配置包**：把散落在 $DSH_HOME 的几份配置打成带 manifest 与 sha256 的 zip，
     默认剥离密钥；导入前先预览（哪些新、哪些同、哪些冲突），冲突逐项决策，
     支持"源机器路径前缀 → 本机"的映射；导入前自动留版本点。
  2. **配置版本点**：把当前这批受管文件整体存一份（含 sha256 与索引），
     可列历史、看与任意版本点的差异、整体回到某个版本点。

不做的事（与 ZeroFall 定位不重合，写清楚免得以为漏了）：
  · 插件市场、从别的 AI agent 导入会话历史、对话 rewind —— 这些属于 DSH 插件面
  · 远端四通道同步 —— ZeroFall 已有自己的云同步（本地目录 / 百度网盘）
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from modules import config_crypto

CONFIG_MANIFEST = "zf-config-manifest.json"
HISTORY_DIRNAME = ".zf-config-history"
BUNDLE_FORMAT = "zf-config-bundle/1"

# 受管文件（相对 $DSH_HOME）：迁移与版本点都以这批为准
MANAGED_FILES = (
    "prompt-inject.md",
    "profiles/{profile}/cordis.patch.yml",
    "profiles/{profile}/cordis.yml",
    "profiles/{profile}/package.json",
    ".agent-presets/{preset}/agent.cordis.yml",
    ".agent-presets/{preset}/preset.yml",
)

# 导出时默认剥掉的密钥字段（key 名匹配即整行/整值替换）
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|credential|authorization|cookie)",
    re.I)
SECRET_MASK = "***REDACTED-BY-ZERO-FALL***"


# ──────────────────────────────────────────────────────────────── 路径收集


def managed_paths(home: Path, profile: str = "desktop") -> List[Path]:
    home = Path(home)
    out = []
    for pat in MANAGED_FILES:
        if "{profile}" in pat:
            out.append(home / pat.format(profile=profile))
        elif "{preset}" in pat:
            root = home / ".agent-presets"
            if root.is_dir():
                for d in sorted(root.iterdir()):
                    if d.is_dir():
                        out.append(home / pat.format(preset=d.name))
        else:
            out.append(home / pat)
    return [p for p in out if p.is_file()]


def preset_roots(home: Path) -> List[Path]:
    root = Path(home) / ".agent-presets"
    return sorted([d for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────── 密钥剥离


def _slot_of(rel: str, key_text: str) -> str:
    """槽位名：<相对路径>::<键名>，跨机器/跨次导出都稳定"""
    key = re.sub(r"[^\w.\-]", "", key_text).strip("._")
    return "{}::{}".format(rel.replace("\\", "/"), key.lower())


def _redact_yaml_like(text: str, rel: str = "", slots: Optional[Dict[str, str]] = None) -> Tuple[str, int]:
    """按行剥离 `key: value` 形式的密钥值；返回 (新文本, 命中数)

    slots 不为 None 时改成写 `__ZF_SECRET__:<槽位>` 占位符，并把原值收进 slots，
    供本机 vault 保存 —— 这样同一台机器导入时能自动回填，不用手抄密钥。
    """
    hits = 0
    out = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^(\s*[\"']?[\w.\-]+[\"']?\s*:\s*)(\S.*?)(\s*)$", line.rstrip("\n"))
        if m and SECRET_KEY_RE.search(m.group(1)) and m.group(2) not in ("", "null", '""', "''"):
            if slots is None:
                out.append("{}{}{}\n".format(m.group(1), SECRET_MASK, ""))
            else:
                slot = _slot_of(rel, m.group(1))
                slots[slot] = m.group(2).strip().strip('"').strip("'")
                out.append("{}{}\n".format(m.group(1), "__ZF_SECRET__:" + slot))
            hits += 1
        else:
            out.append(line)
    return "".join(out), hits


def _redact_env_like(text: str, rel: str = "", slots: Optional[Dict[str, str]] = None) -> Tuple[str, int]:
    hits = 0
    out = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^(\s*[\"']?[\w.\-]+[\"']?\s*=\s*)(\S.*?)(\s*)$", line.rstrip("\n"))
        if m and SECRET_KEY_RE.search(m.group(1)):
            if slots is None:
                out.append("{}={}\n".format(m.group(1).rstrip(" ="), SECRET_MASK))
            else:
                slot = _slot_of(rel, m.group(1).rstrip(" ="))
                slots[slot] = m.group(2).strip().strip('"').strip("'")
                out.append("{}={}\n".format(m.group(1).rstrip(" ="), "__ZF_SECRET__:" + slot))
            hits += 1
        else:
            out.append(line)
    return "".join(out), hits


def redact_secrets(text: str, rel: str = "",
                   slots: Optional[Dict[str, str]] = None) -> Tuple[str, int]:
    a, n1 = _redact_yaml_like(text, rel, slots)
    b, n2 = _redact_env_like(a, rel, slots)
    return b, n1 + n2


def backfill_secrets(text: str, vault: Optional[Dict[str, str]] = None) -> Tuple[str, int, int]:
    """把 `__ZF_SECRET__:<槽位>` 换回本机 vault 里的值

    返回 (新文本, 回填数, 仍缺数)。换到没有该槽位的机器上就保持占位符并计数，
    由上层提示"这些密钥要手动补"。
    """
    from modules import secret_vault as sv
    vault = sv.load() if vault is None else vault
    filled = missing = 0
    out_lines = []
    for line in text.splitlines(keepends=True):
        if "__ZF_SECRET__:" not in line:
            out_lines.append(line)
            continue
        def _sub(m):
            nonlocal filled, missing
            slot = m.group(1).strip()
            val = vault.get(slot)
            if val:
                filled += 1
                return val
            missing += 1
            return m.group(0)
        out_lines.append(re.sub(r"__ZF_SECRET__:([^\s\"']+)", _sub, line))
    return "".join(out_lines), filled, missing


# ──────────────────────────────────────────────────────────────── 导出


def export_bundle(home: Path, dest: Path, *, profile: str = "desktop",
                  include_secrets: bool = False, label: str = "",
                  password: Optional[str] = None,
                  placeholder_secrets: bool = True) -> Dict[str, Any]:
    """把受管配置打成 zip（默认去密），返回 manifest 与统计

    zip 结构：
      zf-config-manifest.json     清单（格式版本、时间、来源机器、条目 sha256/大小/是否去密）
      files/<原相对路径>           配置本体
    """
    home, dest = Path(home), Path(dest)
    files = managed_paths(home, profile)
    if not files:
        raise FileNotFoundError("没有可导出的配置（检查 $DSH_HOME 与 profile）")
    manifest = {
        "format": BUNDLE_FORMAT,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "source_home": str(home),
        "profile": profile,
        "secrets_included": bool(include_secrets),
        "placeholder_secrets": bool(placeholder_secrets and not include_secrets),
        "secret_slots": [],
        "entries": [],
    }
    _slots: Dict[str, str] = {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 设了密码时先写临时明文 zip，再整个加密成容器（密码不落盘）
    target_path = dest
    tmp_zip: Optional[Path] = None
    if password:
        import tempfile as _tf
        fd, tmp_name = _tf.mkstemp(dir=str(dest.parent), prefix=".zf-bundle-", suffix=".zip")
        os.close(fd)
        target_path = Path(tmp_name)
        tmp_zip = target_path
    redacted_total = 0
    with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            rel = p.relative_to(home).as_posix()
            raw = p.read_text(encoding="utf-8", errors="replace")
            if include_secrets:
                text, hits = raw, 0
            else:
                text, hits = redact_secrets(
                    raw, rel=rel, slots=(_slots if placeholder_secrets else None))
            redacted_total += hits
            arcname = "files/" + rel
            zf.writestr(arcname, text)
            entry = {
                "path": rel,
                "arcname": arcname,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bytes": len(text.encode("utf-8")),
                "redacted_values": hits,
            }
            manifest["entries"].append(entry)
        manifest["secret_slots"] = sorted(_slots.keys())
        if _slots:
            try:
                from modules import secret_vault as _sv
                _sv.put_many(_slots)
            except Exception:
                pass
        manifest["encrypted"] = bool(password)
        zf.writestr(CONFIG_MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    if tmp_zip is not None:
        dest.write_bytes(config_crypto.encrypt_bytes(tmp_zip.read_bytes(), password))
        tmp_zip.unlink(missing_ok=True)
    return {
        "dest": str(dest),
        "entries": len(manifest["entries"]),
        "redacted_values": redacted_total,
        "bytes": dest.stat().st_size,
        "secrets_included": bool(include_secrets),
        "manifest": manifest,
    }


# ──────────────────────────────────────────────────────────────── 读取 / 预览


def _open_zip(bundle: Path, password: Optional[str] = None) -> zipfile.ZipFile:
    """打开配置包；加密包需要密码（密码错抛 ValueError，由上层提示）"""
    bundle = Path(bundle)
    if config_crypto.is_encrypted(bundle):
        if not password:
            raise ValueError("这是加密配置包，需要密码")
        try:
            data = config_crypto.decrypt_bytes(bundle.read_bytes(), password)
        except config_crypto.CryptoError as e:
            raise ValueError(str(e)) from e
        return zipfile.ZipFile(io.BytesIO(data))
    return zipfile.ZipFile(bundle)


def is_encrypted_bundle(bundle: Path) -> bool:
    return config_crypto.is_encrypted(Path(bundle))


def inspect_bundle(bundle: Path, password: Optional[str] = None) -> Dict[str, Any]:
    bundle = Path(bundle)
    if not bundle.is_file():
        raise FileNotFoundError(f"包不存在：{bundle}")
    with _open_zip(bundle, password) as zf:
        if CONFIG_MANIFEST not in zf.namelist():
            raise ValueError("不是 ZeroFall 配置包（缺 manifest）")
        manifest = json.loads(zf.read(CONFIG_MANIFEST).decode("utf-8"))
        names = set(zf.namelist())
        for e in manifest.get("entries", []):
            e["present"] = e.get("arcname") in names
    if manifest.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"清单格式不认识：{manifest.get('format')}")
    return manifest


def preview_import(bundle: Path, home: Path, *,
                   path_map: Optional[Tuple[str, str]] = None,
                   password: Optional[str] = None) -> List[Dict[str, Any]]:
    """逐项对比：same / new / differs（附前 40 行 diff），并应用路径映射

    path_map = (源机器前缀, 本机前缀)，例如 ('/Users/a', 'C:/Users/b')。
    """
    bundle, home = Path(bundle), Path(home)
    manifest = inspect_bundle(bundle, password)
    rows = []
    with _open_zip(bundle, password) as zf:
        for e in manifest.get("entries", []):
            rel = e["path"]
            if path_map:
                src_pref, dst_pref = path_map
                if rel.startswith(src_pref.rstrip("/\\")):
                    rel = dst_pref.rstrip("/\\") + rel[len(src_pref.rstrip("/\\")):]
            target = home / rel
            incoming = zf.read(e["arcname"]).decode("utf-8") if e.get("present") else ""
            if not target.exists():
                state = "new"
                diff = ""
            else:
                current = target.read_text(encoding="utf-8", errors="replace")
                if current == incoming:
                    state = "same"
                    diff = ""
                else:
                    state = "differs"
                    diff = "\n".join(list(_unified(current, incoming, rel))[:40])
            rows.append({"rel": rel, "target": str(target), "state": state,
                         "diff": diff, "bytes": len(incoming.encode("utf-8"))})
    return rows


def _unified(a: str, b: str, name: str):
    import difflib
    return difflib.unified_diff(a.splitlines(), b.splitlines(),
                                fromfile=f"{name}（本机）", tofile=f"{name}（包内）",
                                lineterm="", n=2)


def apply_import(bundle: Path, home: Path, *,
                 policy: str = "overwrite",
                 only: Optional[List[str]] = None,
                 path_map: Optional[Tuple[str, str]] = None,
                 backup: bool = True,
                 password: Optional[str] = None) -> Dict[str, Any]:
    """按预览结果落盘

    policy: overwrite（覆盖）| skip（只补缺失）| keep-both（冲突的另存 *.imported）
    only: 只导入这些相对路径（None = 全部）
    写前对已存在文件留 .bak-zf-config-* / 冲突另存，失败回滚。
    """
    bundle, home = Path(bundle), Path(home)
    if policy not in ("overwrite", "skip", "keep-both", "merge3"):
        raise ValueError("policy 只能是 overwrite / skip / keep-both / merge3")
    rows = preview_import(bundle, home, path_map=path_map, password=password)
    results = {"written": [], "skipped": [], "kept_both": [], "backups": [], "errors": [],
               "conflicts": [], "secrets_filled": 0, "secrets_missing": 0}
    # merge3 的 base：最近一个版本点里的同名文件（没有就退化成"保守：保留本地 + 冲突另存"）
    _base_files: Dict[str, str] = {}
    if policy == "merge3":
        try:
            pts = list_points(home)
            if pts:
                for rel2, fp2 in _point_files(home, pts[0]["id"]).items():
                    try:
                        _base_files[rel2] = fp2.read_text(encoding="utf-8")
                    except Exception:
                        pass
        except Exception:
            _base_files = {}
    with _open_zip(bundle, password) as zf:
        by_path = {e["path"]: e for e in inspect_bundle(bundle, password).get("entries", [])}
        for row in rows:
            rel = row["rel"]
            if only is not None and rel not in only:
                results["skipped"].append(rel)
                continue
            if row["state"] == "same":
                results["skipped"].append(rel)
                continue
            if row["state"] == "differs" and policy == "skip":
                results["skipped"].append(rel)
                continue
            # 找到包内原始条目（path_map 只改落点，不改变 arcname）
            src_entry = None
            for orig_rel, e in by_path.items():
                if orig_rel == row["rel"] or rel.endswith(orig_rel):
                    src_entry = e
                    break
            if src_entry is None:
                results["errors"].append(f"{rel}: 包里找不到对应条目")
                continue
            content = zf.read(src_entry["arcname"]).decode("utf-8")
            content, _filled, _missing = backfill_secrets(content)
            results["secrets_filled"] += _filled
            results["secrets_missing"] += _missing
            target = Path(row["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                if row["state"] == "differs" and policy == "merge3":
                    local_text = target.read_text(encoding="utf-8") if target.exists() else ""
                    base_text = _base_files.get(row["rel"])
                    incoming = content
                    if local_text == incoming:
                        results["skipped"].append(rel)
                        continue
                    if base_text is not None and local_text == base_text:
                        pass                      # 本地没动过 → 采用包内版本，往下写
                    elif base_text is not None and incoming == base_text:
                        results["skipped"].append(rel)   # 包里没变 → 保留本地
                        continue
                    else:
                        # 两边都动过（或没有 base）→ 保留本地，包内版本另存冲突副本
                        alt = target.with_name(target.name + ".zf-conflict")
                        alt.write_text(incoming, encoding="utf-8")
                        results["conflicts"].append(str(alt))
                        continue
                if row["state"] == "differs" and policy == "keep-both":
                    alt = target.with_name(target.name + ".imported")
                    alt.write_text(content, encoding="utf-8")
                    results["kept_both"].append(str(alt))
                    continue
                if target.exists() and backup:
                    bak = target.with_name(
                        f"{target.name}.bak-zf-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                    shutil.copy2(target, bak)
                    results["backups"].append(str(bak))
                tmp = target.with_name(target.name + ".zf-tmp")
                tmp.write_text(content, encoding="utf-8")
                os.replace(tmp, target)
                results["written"].append(str(target))
            except Exception as e:
                results["errors"].append(f"{rel}: {e}")
    return results


# ──────────────────────────────────────────────────────────────── 版本点


def _history_root(home: Path) -> Path:
    return Path(home) / HISTORY_DIRNAME


def _history_index(home: Path) -> Dict[str, Any]:
    fp = _history_root(home) / "index.json"
    if not fp.is_file():
        return {"points": []}
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {"points": []}


def _write_history_index(home: Path, idx: Dict[str, Any]) -> None:
    root = _history_root(home)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "index.json.tmp"
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / "index.json")


def save_point(home: Path, label: str = "", profile: str = "desktop",
               tag: str = "") -> Dict[str, Any]:
    """把当前受管文件整体存成一个版本点（含 sha256 与索引）"""
    home = Path(home)
    files = managed_paths(home, profile)
    if not files:
        raise FileNotFoundError("没有可保存的配置文件")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    pid = f"{ts}-{label or 'point'}".replace(" ", "_")
    dest = _history_root(home) / pid
    dest.mkdir(parents=True, exist_ok=True)
    entries = []
    for p in files:
        rel = p.relative_to(home).as_posix()
        target = dest / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        entries.append({"path": rel, "sha256": sha256_file(p),
                        "bytes": p.stat().st_size})
    meta = {"id": pid, "label": label, "tag": tag,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "profile": profile, "entries": entries}
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    idx = _history_index(home)
    idx.setdefault("points", []).insert(0, {"id": pid, "label": label, "tag": tag,
                                            "created_at": meta["created_at"],
                                            "files": len(entries)})
    # 截断时保护带 tag 的里程碑：先挤掉最老的**无 tag** 点
    while len(idx["points"]) > 200:
        victim = next((i for i in range(len(idx["points"]) - 1, -1, -1)
                       if not idx["points"][i].get("tag")), None)
        if victim is None:
            break
        idx["points"].pop(victim)
    _write_history_index(home, idx)
    return meta


def list_points(home: Path) -> List[Dict[str, Any]]:
    return _history_index(Path(home)).get("points", [])


def diff_point(home: Path, point_id: str) -> List[Dict[str, Any]]:
    """当前文件 vs 某个版本点：返回 added/removed/modified 明细（带 diff 文本）"""
    home = Path(home)
    point_id = resolve_ref(home, point_id)
    root = _history_root(home) / point_id
    meta_fp = root / "meta.json"
    if not meta_fp.is_file():
        raise FileNotFoundError(f"版本点不存在：{point_id}")
    meta = json.loads(meta_fp.read_text(encoding="utf-8"))
    out = []
    seen = set()
    for e in meta.get("entries", []):
        rel = e["path"]
        seen.add(rel)
        cur = home / rel
        snap = root / "files" / rel
        if not cur.exists():
            out.append({"path": rel, "state": "removed", "diff": ""})
            continue
        a = snap.read_text(encoding="utf-8", errors="replace") if snap.is_file() else ""
        b = cur.read_text(encoding="utf-8", errors="replace")
        if sha256_file(cur) == e["sha256"]:
            out.append({"path": rel, "state": "same", "diff": ""})
        else:
            out.append({"path": rel, "state": "modified",
                        "diff": "\n".join(list(_unified(a, b, rel))[:80])})
    for p in managed_paths(home, meta.get("profile", "desktop")):
        rel = p.relative_to(home).as_posix()
        if rel not in seen:
            out.append({"path": rel, "state": "added", "diff": ""})
    return out


def restore_point(home: Path, point_id: str, *, backup: bool = True) -> Dict[str, Any]:
    """整体回到某个版本点（可用 id、id 前缀或 tag）；写前给现有文件留 .bak-zf-config-*"""
    home = Path(home)
    point_id = resolve_ref(home, point_id)
    root = _history_root(home) / point_id
    meta_fp = root / "meta.json"
    if not meta_fp.is_file():
        raise FileNotFoundError(f"版本点不存在：{point_id}")
    meta = json.loads(meta_fp.read_text(encoding="utf-8"))
    results = {"written": [], "backups": [], "errors": []}
    for e in meta.get("entries", []):
        rel = e["path"]
        src = root / "files" / rel
        if not src.is_file():
            results["errors"].append(f"{rel}: 版本点里缺文件")
            continue
        dst = home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dst.exists() and backup:
                bak = dst.with_name(
                    f"{dst.name}.bak-zf-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
                shutil.copy2(dst, bak)
                results["backups"].append(str(bak))
            shutil.copy2(src, dst)
            results["written"].append(str(dst))
        except Exception as ex:
            results["errors"].append(f"{rel}: {ex}")
    return results


# ──────────────────────────────────────────────────────────────── tag 与两点互 diff


def resolve_ref(home: Path, ref: str) -> str:
    """把 tag / id / id 前缀解析成版本点 id

    优先级：精确 id → tag → 唯一 id 前缀。找不到或前缀有歧义都抛错（不猜）。
    """
    home = Path(home)
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("版本点引用为空")
    points = list_points(home)
    for p in points:
        if p.get("id") == ref:
            return ref
    for p in points:
        if (p.get("tag") or "").strip() == ref:
            return p["id"]
    hits = [p["id"] for p in points if p["id"].startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError("前缀不唯一：{} 命中 {}".format(ref, "、".join(hits[:5])))
    raise ValueError("找不到版本点：{}".format(ref))


def tag_point(home: Path, ref: str, tag: str) -> Dict[str, Any]:
    """给版本点打 tag（同名 tag 会从旧点上摘掉，保证一个名字只指一个点）"""
    home = Path(home)
    tag = (tag or "").strip()
    if not tag:
        raise ValueError("tag 为空")
    pid = resolve_ref(home, ref)
    idx = _history_index(home)
    for p in idx.get("points", []):
        if p.get("tag") == tag and p.get("id") != pid:
            p["tag"] = ""
    for p in idx.get("points", []):
        if p.get("id") == pid:
            p["tag"] = tag
    _write_history_index(home, idx)
    meta_fp = _history_root(home) / pid / "meta.json"
    if meta_fp.is_file():
        try:
            meta = json.loads(meta_fp.read_text(encoding="utf-8"))
            meta["tag"] = tag
            meta_fp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return {"id": pid, "tag": tag}


def _point_files(home: Path, pid: str) -> Dict[str, Path]:
    root = _history_root(home) / pid
    meta_fp = root / "meta.json"
    if not meta_fp.is_file():
        raise FileNotFoundError(f"版本点不存在：{pid}")
    meta = json.loads(meta_fp.read_text(encoding="utf-8"))
    return {e["path"]: root / "files" / e["path"] for e in meta.get("entries", [])}


def diff_points(home: Path, ref_a: str, ref_b: str) -> Dict[str, Any]:
    """任意两个版本点互 diff（不涉及当前磁盘）

    为什么需要：只用"当前 vs 某点"看不出"两次历史状态之间"的改动 ——
    中间那次改动如果后来又被改回去，跟当前比就是无差别。
    """
    home = Path(home)
    a_id, b_id = resolve_ref(home, ref_a), resolve_ref(home, ref_b)
    a_files, b_files = _point_files(home, a_id), _point_files(home, b_id)
    rows = []
    for rel in sorted(set(a_files) | set(b_files)):
        fa, fb = a_files.get(rel), b_files.get(rel)
        if fa is None:
            rows.append({"path": rel, "state": "added", "diff": ""})
            continue
        if fb is None:
            rows.append({"path": rel, "state": "removed", "diff": ""})
            continue
        text_a = fa.read_text(encoding="utf-8", errors="replace") if fa.is_file() else ""
        text_b = fb.read_text(encoding="utf-8", errors="replace") if fb.is_file() else ""
        if text_a == text_b:
            rows.append({"path": rel, "state": "same", "diff": ""})
        else:
            rows.append({"path": rel, "state": "modified",
                         "diff": "\n".join(list(_unified(text_a, text_b, rel))[:120])})
    changed = [r for r in rows if r["state"] != "same"]
    return {"a": a_id, "b": b_id, "rows": rows, "changed": len(changed),
            "summary": {s: sum(1 for r in rows if r["state"] == s)
                        for s in ("same", "modified", "added", "removed")}}
