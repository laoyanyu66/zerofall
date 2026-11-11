# -*- coding: utf-8 -*-
"""profile 健康诊断（7 项）—— 对齐 imxzt/dsh-config-manager 的事故清单

为什么要有它：DSH 装插件时 pnpm 一旦把某个包弄丢，而这个名字还留在
``dsh.profile.bundles`` 里，下次启动 ``resolveBundleDir()`` 会硬失败，DSH 随即
触发 profile 恢复 —— 把 bundles 重置成模板、``cordis.patch.yml`` 覆盖成模板，
**全部插件配置静默丢失**。这套检查就是在"重启之前"把这种情况喊出来。

七项（每一项都对应真实事故，不是假想检查）：
  1. manifest 可读性      BOM / 非法 JSON（DSH 在 JSON.parse 之前就中止）
  2. bundle 可解析性      声明了但磁盘没有 → 下次启动必硬失败
  3. bundles ↔ dependencies 孤儿声明 / 可用但未启用
  4. profile 重置形状     只剩模板 bundle + patch 只剩系统条目 → 已被重置
  5. lockfile 同步        声明的本地依赖是否在 pnpm-lock 里
  6. file:/link: 联接     被 pnpm 换成实体副本 → 改源码不再即时生效
  7. profile patch        悬空 insert: / 同一块内重复挂载（顶层重复是合法覆盖）

与 dsh-config-manager 的差异：它是外部 Node 工具 + bash/ps 脚本；这里全部用 Python
在 ZeroFall 进程内跑，不依赖 pnpm/git，只读不写。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules import dsh_config as dc

# 判定"这是 DSH 自管的补丁条目"用的 id 前缀/名单：被重置后通常只剩这些
_BUILTIN_BUNDLE_PREFIX = "@deepseek-ai/"
DUPLICATE_TOLERANT_KEYS = ("id", "name", "config")


def _result(cid: str, title: str, level: str, detail: str = "", fix: str = "") -> Dict[str, Any]:
    return {"id": cid, "title": title, "level": level, "detail": detail, "fix": fix}


def _profile_dir(home: Path, profile: str) -> Path:
    return Path(home) / "profiles" / profile


def _host_ai_base(host_base: Optional[Path]) -> Optional[Path]:
    if host_base:
        return Path(host_base)
    cand = Path(r"D:\DeepSeek\resources\app\dsh\node_modules\@deepseek-ai")
    return cand if cand.is_dir() else None


# ──────────────────────────────────────────────────────────────── 1. manifest


def check_manifest(pdir: Path) -> Dict[str, Any]:
    pkg = pdir / "package.json"
    if not pkg.is_file():
        return _result("manifest", "manifest 可读性", "error",
                       f"缺 package.json：{pkg}",
                       "从 package.json.bak-* 里挑最近的还原，或用 ZeroFall 的版本点回滚")
    raw = pkg.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return _result("manifest", "manifest 可读性", "error",
                       "package.json 带 UTF-8 BOM —— DSH 在 JSON.parse 之前就会中止",
                       "用无 BOM 的 UTF-8 重写该文件（ZeroFall 的预设/配置写入都是无 BOM）")
    try:
        json.loads(raw.decode("utf-8"))
    except Exception as e:
        return _result("manifest", "manifest 可读性", "error",
                       f"package.json 不是合法 JSON：{e}",
                       "修 JSON 语法；改前先记一个版本点")
    return _result("manifest", "manifest 可读性", "ok", str(pkg))


# ──────────────────────────────────────────────────────────────── 读取清单


def read_profile(pdir: Path) -> Dict[str, Any]:
    pkg = pdir / "package.json"
    try:
        doc = json.loads(pkg.read_text(encoding="utf-8-sig"))
    except Exception:
        doc = {}
    deps = doc.get("dependencies") or {}
    bundles = (((doc.get("dsh") or {}).get("profile") or {}).get("bundles")) or []
    return {"doc": doc, "deps": deps, "bundles": list(bundles)}


# ──────────────────────────────────────────────────────────────── 2. bundle 可解析


def check_bundles(pdir: Path, bundles: List[str],
                  host_base: Optional[Path] = None) -> Dict[str, Any]:
    nm = pdir / "node_modules"
    missing, ok = [], []
    for name in bundles:
        local = nm / name
        present = local.exists() or local.is_symlink()
        if not present and name.startswith(_BUILTIN_BUNDLE_PREFIX):
            base = _host_ai_base(host_base)
            short = name.split("/", 1)[1] if "/" in name else name
            present = bool(base and (base / short).exists())
        (ok if present else missing).append(name)
    if missing:
        return _result("bundles", "bundle 可解析性", "error",
                       "声明了但磁盘找不到（下次启动会硬失败 → 触发 profile 重置）：" + "、".join(missing),
                       "要么补装（pnpm install），要么从 package.json 的 bundles 里摘掉这几个名字")
    return _result("bundles", "bundle 可解析性", "ok", f"{len(ok)} 个 bundle 都可解析")


# ──────────────────────────────────────────────────────────────── 3. 一致性


def check_consistency(bundles: List[str], deps: Dict[str, str]) -> Dict[str, Any]:
    orphan = [b for b in bundles
              if b not in deps and not b.startswith(_BUILTIN_BUNDLE_PREFIX)]
    dormant = [d for d in deps if d not in bundles]
    if orphan:
        return _result("consistency", "bundles ↔ dependencies", "error",
                       "启用了但 dependencies 里没有（装不回来）：" + "、".join(orphan),
                       "把名字加进 dependencies，或从 bundles 里移除")
    if dormant:
        return _result("consistency", "bundles ↔ dependencies", "info",
                       "装了但没启用：" + "、".join(dormant),
                       "想启用就把名字加进 dsh.profile.bundles")
    return _result("consistency", "bundles ↔ dependencies", "ok",
                   f"{len(bundles)} 个 bundle 与依赖声明一致")


# ──────────────────────────────────────────────────────────────── 4. 重置形状


def check_reset_shape(pdir: Path, bundles: List[str]) -> Dict[str, Any]:
    patch = pdir / "cordis.patch.yml"
    entries: List[Dict[str, Any]] = []
    if patch.is_file():
        try:
            entries = dc.parse_entries(patch.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    local_bundles = [b for b in bundles if not b.startswith(_BUILTIN_BUNDLE_PREFIX)]
    if len(bundles) <= 2 and not local_bundles and len(entries) <= 2:
        backups = sorted(pdir.glob("package.json.bak-*")) + sorted(pdir.glob("cordis.patch.yml.bak-*"))
        hint = ("最近可用的备份/版本点：{}".format(backups[-1].name) if backups
                else "没有 .bak-* 备份，用 ZeroFall 的版本点（配置编辑 → 迁移与历史）回滚")
        return _result("reset", "profile 重置形状", "error",
                       "bundles 只剩 {} 个内建、patch 只剩 {} 条 —— 符合'被 DSH 重置过'的形状".format(
                           len(bundles), len(entries)),
                       hint)
    return _result("reset", "profile 重置形状", "ok",
                   "{} 个 bundle（本地 {} 个）、patch {} 条".format(
                       len(bundles), len(local_bundles), len(entries)))


# ──────────────────────────────────────────────────────────────── 5. lockfile


def check_lockfile(pdir: Path, deps: Dict[str, str]) -> Dict[str, Any]:
    lock = pdir / "pnpm-lock.yaml"
    if not lock.is_file():
        return _result("lock", "lockfile 同步", "info",
                       "没有 pnpm-lock.yaml（不一定是问题，纯 vendor 布局也能跑）")
    text = lock.read_text(encoding="utf-8", errors="replace")
    local = {k: v for k, v in deps.items() if str(v).startswith(("file:", "link:"))}
    absent = [k for k in local if k not in text]
    if absent:
        return _result("lock", "lockfile 同步", "warn",
                       "本地依赖不在锁文件里（安装时可能被重建）：" + "、".join(absent),
                       "跑一次 pnpm install --lockfile-only，或接受它并在版本点里留好配置")
    return _result("lock", "lockfile 同步", "ok", f"{len(local)} 个本地依赖都在锁文件里")


# ──────────────────────────────────────────────────────────────── 6. 联接


def _is_link(p: Path) -> bool:
    try:
        if p.is_symlink():
            return True
        st = p.stat(follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def check_file_links(pdir: Path, deps: Dict[str, str]) -> Dict[str, Any]:
    nm = pdir / "node_modules"
    local = {k: v for k, v in deps.items() if str(v).startswith(("file:", "link:"))}
    if not local:
        return _result("links", "file:/link: 联接", "ok", "没有本地路径依赖")
    broken, materialized, ok = [], [], []
    for name in local:
        target = nm / name
        if not target.exists() and not target.is_symlink():
            broken.append(name)
        elif _is_link(target):
            ok.append(name)
        else:
            materialized.append(name)
    if broken:
        return _result("links", "file:/link: 联接", "error",
                       "node_modules 里没有这些链接（改源码不会生效，启动时也可能解析失败）：" + "、".join(broken),
                       "pnpm install 重新建立联接")
    if materialized:
        return _result("links", "file:/link: 联接", "warn",
                       "联接被换成了实体副本（pnpm 的已知行为）：" + "、".join(materialized),
                       "删掉这几个目录后重新 pnpm install，才能恢复'改源码即时生效'")
    return _result("links", "file:/link: 联接", "ok", f"{len(ok)} 个本地依赖都是链接")


# ──────────────────────────────────────────────────────────────── 7. patch


def check_patch(pdir: Path) -> Dict[str, Any]:
    patch = pdir / "cordis.patch.yml"
    if not patch.is_file():
        return _result("patch", "profile patch", "error", "缺 cordis.patch.yml",
                       "从 cordis.patch.yml.bak-* 或版本点还原")
    try:
        entries = dc.parse_entries(patch.read_text(encoding="utf-8"))
    except Exception as e:
        return _result("patch", "profile patch", "error", f"YAML 解析失败：{e}",
                       "修 YAML；ZeroFall 的配置编辑页会在写入前校验")
    ids = {str(e.get("id")) for e in entries if e.get("id")}
    dangling, dup_in_block = [], []
    for e in entries:
        ins = e.get("insert")
        if isinstance(ins, list):
            for item in ins:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("id") or "")
                if iid and iid not in ids:
                    dangling.append(iid)
                seen = set()
                for sub in ins:
                    if isinstance(sub, dict) and str(sub.get("id")) == iid:
                        if iid in seen:
                            dup_in_block.append(iid)
                        seen.add(iid)
    if dangling:
        return _result("patch", "profile patch", "warn",
                       "insert: 里引用了不存在的 id：" + "、".join(sorted(set(dangling))),
                       "删掉这些 insert 项，或补上对应条目")
    if dup_in_block:
        return _result("patch", "profile patch", "warn",
                       "同一 insert 块内重复挂载：" + "、".join(sorted(set(dup_in_block))),
                       "块内去重（顶层重复是 DSH 的合法覆盖机制，不算问题）")
    return _result("patch", "profile patch", "ok", f"{len(entries)} 条，无悬空 insert / 块内重复")


# ──────────────────────────────────────────────────────────────── 汇总


def run_all(home: Path, profile: str = "desktop",
            host_base: Optional[Path] = None) -> Dict[str, Any]:
    pdir = _profile_dir(home, profile)
    checks: List[Dict[str, Any]] = []
    if not pdir.is_dir():
        return {"profile": profile, "dir": str(pdir), "checks": [
            _result("profile", "profile 目录", "error", f"找不到 {pdir}",
                    "确认 profile 名：$DSH_HOME/profiles/<name>")],
            "summary": {"error": 1, "warn": 0, "info": 0, "ok": 0}, "can_restart": False}

    checks.append(check_manifest(pdir))
    info = read_profile(pdir)
    checks.append(check_bundles(pdir, info["bundles"], host_base))
    checks.append(check_consistency(info["bundles"], info["deps"]))
    checks.append(check_reset_shape(pdir, info["bundles"]))
    checks.append(check_lockfile(pdir, info["deps"]))
    checks.append(check_file_links(pdir, info["deps"]))
    checks.append(check_patch(pdir))
    checks.append(check_workspace_refs(pdir))
    checks.append(check_storages(pdir))
    appdata = Path(os.environ.get("APPDATA", "")) / "@deepseek-ai" / "dsh-desktop"
    checks.append(check_credentials(pdir, appdata if appdata.is_dir() else None))

    summary = {"error": 0, "warn": 0, "info": 0, "ok": 0}
    for c in checks:
        summary[c["level"]] = summary.get(c["level"], 0) + 1
    return {
        "profile": profile,
        "dir": str(pdir),
        "checks": checks,
        "summary": summary,
        "can_restart": summary["error"] == 0,
        "verdict": ("可以重启" if summary["error"] == 0
                    else "先别重启：有 {} 项硬问题，重启会触发 profile 重置".format(summary["error"])),
    }


def format_lines(report: Dict[str, Any]) -> List[str]:
    icon = {"ok": "✓", "warn": "!", "error": "✗", "info": "·"}
    out = ["profile 健康诊断 · {}（{}）".format(report.get("profile"), report.get("dir")),
           "结论：{}".format(report.get("verdict", ""))]
    for c in report.get("checks", []):
        line = "{} {:<18} {}".format(icon.get(c["level"], "?"), c["title"], c["detail"])
        out.append(line.rstrip())
        if c.get("fix") and c["level"] in ("warn", "error"):
            out.append("    → " + c["fix"])
    s = report.get("summary", {})
    out.append("— 错误 {} / 警告 {} / 提示 {} / 通过 {}".format(
        s.get("error", 0), s.get("warn", 0), s.get("info", 0), s.get("ok", 0)))
    return out


# ──────────────────────────────────────────────────────────────── 按建议修正


def unresolvable_bundles(home: Path, profile: str = "desktop",
                         host_base: Optional[Path] = None) -> List[str]:
    """返回"声明了但磁盘上解析不到"的 bundle 名（即 check_bundles 报错的那批）"""
    pdir = _profile_dir(home, profile)
    info = read_profile(pdir)
    nm = pdir / "node_modules"
    base = _host_ai_base(host_base)
    out = []
    for name in info["bundles"]:
        local = nm / name
        present = local.exists() or local.is_symlink()
        if not present and name.startswith(_BUILTIN_BUNDLE_PREFIX):
            short = name.split("/", 1)[1] if "/" in name else name
            present = bool(base and (base / short).exists())
        if not present:
            out.append(name)
    return out


def neutralize_bundles(home: Path, profile: str = "desktop",
                       names: Optional[List[str]] = None,
                       host_base: Optional[Path] = None,
                       dry_run: bool = True) -> Dict[str, Any]:
    """把不可解析的 bundle 从 dsh.profile.bundles 里摘掉（依赖声明保留不动）

    这是"事故的直接成因"那一刀：DSH 启动时 resolveBundleDir() 找不到就会硬失败，
    接着触发 profile 重置。摘掉名字后启动能过，包本身仍在 dependencies 里，
    修好之后加回去即可。

    写前留 .bak-zf-config-*；dry_run=True 时只报告不写。
    """
    pdir = _profile_dir(home, profile)
    pkg = pdir / "package.json"
    if not pkg.is_file():
        raise FileNotFoundError(f"缺 package.json：{pkg}")
    targets = list(names) if names is not None else unresolvable_bundles(home, profile, host_base)
    if not targets:
        return {"removed": [], "kept": [], "changed": False, "backup": None,
                "message": "没有需要摘除的 bundle"}
    if dry_run:
        return {"removed": [], "would_remove": targets, "changed": False, "backup": None,
                "message": "试算：" + "、".join(targets)}

    raw = pkg.read_text(encoding="utf-8-sig")
    doc = json.loads(raw)
    prof = ((doc.get("dsh") or {}).get("profile") or {})
    bundles = list(prof.get("bundles") or [])
    kept = [b for b in bundles if b not in targets]
    removed = [b for b in bundles if b in targets]
    prof["bundles"] = kept
    doc.setdefault("dsh", {})["profile"] = prof

    from datetime import datetime as _dt
    bak = pkg.with_name("package.json.bak-zf-config-" + _dt.now().strftime("%Y%m%d-%H%M%S"))
    bak.write_bytes(pkg.read_bytes())
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp = pkg.with_name("package.json.zf-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, pkg)

    # 写后复核：JSON 能读、目标已不在 bundles 里
    check_doc = json.loads(pkg.read_text(encoding="utf-8-sig"))
    check_bundles = ((check_doc.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    bad = [b for b in targets if b in check_bundles]
    if bad:
        bak_bytes = bak.read_bytes()
        pkg.write_bytes(bak_bytes)
        raise RuntimeError("写后复核失败，已回滚：" + "、".join(bad))

    return {"removed": removed, "kept": kept, "changed": True, "backup": str(bak),
            "message": "已从 bundles 摘除 {} 个（依赖声明未动）".format(len(removed))}


# ──────────────────────────────────────── 8-10. 路径引用 / 凭据（换机后最常见的坑）


def _looks_like_path(v: str) -> bool:
    v = str(v or "")
    return bool(re.match(r"^[A-Za-z]:[\\/]", v)) or v.startswith("\\\\")


def _scan_json_paths(path: Path, limit: int = 200) -> List[str]:
    out: List[str] = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return out
    stack = [doc]
    while stack and len(out) < limit:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str) and _looks_like_path(cur):
            out.append(cur)
    return out


def check_workspace_refs(pdir: Path) -> Dict[str, Any]:
    """8. workspace 引用：storages/workspace.json 里登记的目录还在不在"""
    ws = pdir / "storages" / "workspace.json"
    if not ws.is_file():
        return _result("workspace", "workspace 引用", "info", "没有 storages/workspace.json（未登记工作区）")
    paths = _scan_json_paths(ws)
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        return _result("workspace", "workspace 引用", "warn",
                       "登记了但磁盘上没有的路径 {} 个：{}".format(
                           len(missing), "；".join(missing[:3])),
                       "在本机重建这些目录，或从 workspace.json 里删掉失效条目")
    return _result("workspace", "workspace 引用", "ok", "{} 个登记路径都存在".format(len(paths)))


def check_storages(pdir: Path) -> Dict[str, Any]:
    """9. storages 下的其它登记文件里有没有死路径"""
    root = pdir / "storages"
    if not root.is_dir():
        return _result("storages", "storages 路径引用", "info", "没有 storages 目录")
    missing, scanned = [], 0
    for f in sorted(root.glob("*.json"))[:20]:
        scanned += 1
        for p in _scan_json_paths(f):
            if not Path(p).exists():
                missing.append("{} → {}".format(f.name, p))
    if missing:
        return _result("storages", "storages 路径引用", "warn",
                       "{} 个登记路径已失效：{}".format(len(missing), "；".join(missing[:3])),
                       "这些多半是换机/挪目录留下的，清理或重建对应目录")
    return _result("storages", "storages 路径引用", "ok", "扫了 {} 个登记文件，路径都有效".format(scanned))


def check_credentials(pdir: Path, appdata: Optional[Path] = None) -> Dict[str, Any]:
    """10. 凭据：换机后最容易漏的一环"""
    cands = [pdir / "credentials.json", pdir / "credentials.yml", pdir / "settings.yaml",
             pdir / "auth.json"]
    hit = [c.name for c in cands if c.is_file()]
    auth_ok = False
    if appdata:
        auth_ok = (Path(appdata) / "Network" / "Cookies").is_file() or \
                  (Path(appdata) / "Local State").is_file()
    if hit or auth_ok:
        return _result("credentials", "凭据完整度", "ok",
                       "找到 {}{}".format("、".join(hit) or "无配置文件",
                                          "，登录态在" if auth_ok else "（无登录态缓存）"))
    return _result("credentials", "凭据完整度", "warn",
                   "没找到任何凭据文件，也没有登录态缓存",
                   "换机后需要重新登录 / 重新填 API Key")
