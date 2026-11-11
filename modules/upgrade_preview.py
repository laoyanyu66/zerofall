# -*- coding: utf-8 -*-
"""升级预览：DSH 要升到哪个版本，本机这些插件会怎样

对齐 dsh-plugin-ops 的 upgrade preview —— 它的核心价值是"先看会掉哪些插件"，
而不是等升级完启动崩溃。这里做三件事：
  1. 查宿主最新版本（npm `@deepseek-ai/dsh`）与本机版本对比
  2. 逐个 bundle：判本地源（file:/link:）/ npm 上有无此包 / 本机版本 vs 最新
  3. 给出每条的处置建议（需重装 / 需重建依赖 / 无变化 / 未发布到 npm）

只读 + 只查 npm registry，不动任何文件。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

NPM_HOST = "https://registry.npmjs.org"
CACHE_TTL = 600
CACHE_FILE_TTL = 6 * 3600          # 落盘缓存 6 小时：换机器/重启后不用重打一遍 npm
_cache: Dict[str, tuple] = {}


def _cache_path():
    import os
    from pathlib import Path
    raw = os.environ.get("ZF_DATA_DIR") or ""
    base = Path(raw) if raw else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZeroFall"
    return base / "upgrade_cache.json"


def _disk_cache() -> Dict[str, Any]:
    import json as _json
    try:
        p = _cache_path()
        if not p.is_file():
            return {}
        data = _json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _disk_put(name: str, value: Any) -> None:
    import json as _json
    import time as _t
    try:
        data = _disk_cache()
        data[name] = {"at": _t.time(), "value": value}
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _fetch_latest(name: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """查 npm 上某包的最新版本与被弃用标记；网络失败返回 None"""
    key = name
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    disk = _disk_cache().get(name)
    if disk and now - float(disk.get("at") or 0) < CACHE_FILE_TTL:
        value = disk.get("value")
        _cache[key] = (now, value)
        return value
    url = NPM_HOST + "/" + urllib.parse.quote(name, safe="")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ZeroFall/upgrade-preview"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
        latest = (doc.get("dist-tags") or {}).get("latest")
        ver = (doc.get("versions") or {}).get(latest) or {}
        out = {
            "latest": latest,
            "description": (ver.get("description") or "")[:120],
            "deprecated": bool(ver.get("deprecated")),
            "engines": ver.get("engines") or {},
        }
    except Exception:
        out = None
    _cache[key] = (now, out)
    _disk_put(name, out)
    return out


def _installed_version(pkg_dir: Path) -> str:
    pj = Path(pkg_dir) / "package.json"
    try:
        return str(json.loads(pj.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        return ""


def _cmp(a: str, b: str) -> str:
    """粗比版本：只用于"落后/相同/领先"的提示，不处理全量 semver"""
    def norm(v: str) -> tuple:
        core = v.lstrip("v").split("-")[0]
        parts = []
        for x in core.split("."):
            try:
                parts.append(int(x))
            except ValueError:
                parts.append(0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    if not a or not b:
        return "unknown"
    na, nb = norm(a), norm(b)
    if na == nb:
        return "same"
    return "behind" if na < nb else "ahead"


def host_info(config) -> Dict[str, Any]:
    """宿主版本（本机装的 DSH）与 npm 最新版"""
    installed = ""
    cfg_dir = Path(getattr(config, "deepseek_config_dir", "") or "")
    for cand in (cfg_dir / ".." / "package.json",
                 cfg_dir.parent / "app" / "package.json",
                 Path(r"D:\DeepSeek\resources\app\package.json")):
        try:
            if cand.is_file():
                doc = json.loads(cand.read_text(encoding="utf-8"))
                installed = str(doc.get("version") or "")
                if installed:
                    break
        except Exception:
            continue
    remote = _fetch_latest("@deepseek-ai/dsh") or {}
    latest = remote.get("latest") or ""
    return {"installed": installed, "latest": latest,
            "state": _cmp(installed, latest),
            "url": "https://www.npmjs.com/package/@deepseek-ai/dsh"}


def _read_profile(home: Path, profile: str) -> Dict[str, Any]:
    pj = Path(home) / "profiles" / profile / "package.json"
    try:
        doc = json.loads(pj.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"deps": {}, "bundles": [], "dir": str(pj.parent)}
    return {
        "deps": doc.get("dependencies") or {},
        "bundles": (((doc.get("dsh") or {}).get("profile") or {}).get("bundles")) or [],
        "dir": str(pj.parent),
    }


def preview(home: Path, profile: str = "desktop", config=None,
            offline: bool = False) -> Dict[str, Any]:
    home = Path(home)
    info = _read_profile(home, profile)
    rows: List[Dict[str, Any]] = []
    nm = Path(info["dir"]) / "node_modules"

    for name in sorted(set(list(info["deps"].keys()) + list(info["bundles"]))):
        spec = str(info["deps"].get(name) or "")
        enabled = name in info["bundles"]
        installed = _installed_version(nm / name) if (nm / name).exists() else ""
        row = {
            "name": name, "spec": spec, "enabled": enabled,
            "installed": installed, "latest": "", "state": "unknown",
            "action": "", "source": "npm",
        }
        if spec.startswith(("file:", "link:")):
            row["source"] = "local"
            row["state"] = "local-source"
            row["action"] = "本地源码：升级不受影响，但重装后要重跑 pnpm install"
        elif name.startswith("@deepseek-ai/"):
            row["source"] = "builtin"
            row["state"] = "builtin"
            row["action"] = "宿主自带，随 DSH 版本一起变"
        elif offline:
            row["state"] = "offline"
            row["action"] = "已跳过联网检查"
        else:
            remote = _fetch_latest(name)
            if remote is None:
                row["state"] = "not-on-npm"
                row["action"] = "npm 上查不到：私有源或已下架，升级时最容易掉"
            else:
                row["latest"] = remote.get("latest") or ""
                row["state"] = _cmp(installed, row["latest"])
                row["deprecated"] = bool(remote.get("deprecated"))
                if row["state"] == "behind":
                    row["action"] = "可升级到 {}".format(row["latest"])
                elif row["deprecated"]:
                    row["action"] = "该版本已被 npm 标记弃用，建议换替代品"
                elif not enabled:
                    row["action"] = "已安装但未在 bundles 里启用"
                else:
                    row["action"] = "已是最新"
        rows.append(row)

    risky = [r for r in rows if r["state"] in ("not-on-npm", "behind", "unknown")]
    return {
        "home": str(home), "profile": profile,
        "host": host_info(config) if config is not None else {},
        "rows": rows,
        "summary": {
            "total": len(rows),
            "risky": len(risky),
            "behind": sum(1 for r in rows if r["state"] == "behind"),
            "not_on_npm": sum(1 for r in rows if r["state"] == "not-on-npm"),
            "local": sum(1 for r in rows if r["state"] == "local-source"),
        },
        "risky": [r["name"] for r in risky],
    }


def format_lines(res: Dict[str, Any]) -> List[str]:
    h = res.get("host") or {}
    out = ["升级预览 · profile {}".format(res.get("profile"))]
    if h:
        out.append("宿主：本机 {} / npm 最新 {}（{}）".format(
            h.get("installed") or "未知", h.get("latest") or "未知",
            {"same": "已最新", "behind": "有新版", "ahead": "本机更新", "unknown": "无法判断"}.get(
                h.get("state", "unknown"), h.get("state"))))
    for r in res.get("rows", []):
        flag = {"behind": "↑", "not-on-npm": "!", "local-source": "·", "builtin": "·",
                "same": "✓", "ahead": "✓", "offline": "-", "unknown": "?"}.get(r["state"], "?")
        out.append("{} {:<32} {:>12} → {:<12} {}".format(
            flag, r["name"], r["installed"] or "-", r["latest"] or "-", r["action"]))
    s = res.get("summary", {})
    out.append("— 共 {} 个：落后 {} / npm 查不到 {} / 本地源 {}".format(
        s.get("total", 0), s.get("behind", 0), s.get("not_on_npm", 0), s.get("local", 0)))
    return out
