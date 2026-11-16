# -*- coding: utf-8 -*-
"""会话续跑守护 —— 「崩溃/重启后把关闭前在跑的会话接上」

要解决的问题
------------
DSH 桌面端的关闭、崩溃、被强制重启，都是**没有退出钩子**的：
进程一没，正在跑的 turn、已 arm 的 goal 续行全丢。DSH 自己的安全默认是
「重启后绝不自行复活工作」（agent-loop 的 resume 只恢复 live，goal 一律 disarmed），
所以「继续跑」这道权限必须由外部显式补回来。

ZeroFall 在这里负责三件事（都在 DSH 进程外，纯文件 + 可选 HTTP）：

1. **自愈插件**：`dsh-resume-sessions` 装进 profile（vendor 源码 + package.json 依赖 +
   node_modules 链接 + cordis.patch.yml 的 insert 行），并保证 `enabled: true`。
   —— 修复动作（重装 profile、重建 bundles、把 profile 重置成模板、被人改成 enabled:false）
   最容易把这一行冲掉，冲掉了就静默不恢复。

2. **看住快照**：`~/.dsh/resume-sessions.json` 是插件在"进入 running 的那一帧"
   同步落盘的。崩溃不影响它，但"修复"会：换 DSH_HOME、清目录、回滚旧备份。
   所以在任何修复动作之前先备份它，修完再按需还原。

3. **读出「关闭前在跑的」**：只认 `status == "running"`（以及 goal 还 active 的），
   其余 idle 的历史会话永远不出现在这里的候选里 —— 与插件侧语义保持一致，避免
   "一重启把所有会话都拉起来"。

插件侧的崩溃防护（`boot.restoring` + 崩溃计数 + `maxSessions: 1` + 8MB 日志上限）
保证它自己不会把 host 拖死；ZeroFall 只负责"让它还在、让它被看见"。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_NAME = "dsh-resume-sessions"
PLUGIN_ID = "resume-sessions"
PLUGIN_DEP = f"file:./vendor/{PLUGIN_NAME}"
STATE_FILE = "resume-sessions.json"

#: 插件运行时需要的 host 包（拿它的 createUserMessage 构造续跑消息）
DEFAULT_HOST_LLM = Path(r"D:\DeepSeek\resources\app\dsh\node_modules\@deepseek-ai\dsh-llm")

#: 装到 profile 里的默认条目。
#:
#: 这段文本必须和**当前 profile 里真正跑着的那份**保持一致：
#: 自愈的路径是「条目丢了 → 用这段文本重新插回去」，插回去的内容一旦比现役配置弱，
#: 就是在没人看见的情况下把防护降级（比如少了 continueOnlyFirst，就会同时向模型发多路请求）。
#: 逐项对应关系见下面 PATCH_BLOCK 的注释。
PATCH_BLOCK = """\
# ==== dsh-resume-sessions：只把「上次关闭前正在运行」的会话接着跑 ====
# 运行期 agent/status 变化实时落盘 ~/.dsh/resume-sessions.json（硬杀也丢不了）。
# 启动后：只挑 status=running（或有 active goal、或关闭前窗口内确实在跑）的会话，
#        逐个串行 resume；日志里还有未关闭的 turn 才 followup 一条继续指令，
#        否则只恢复 live 不烧 token。
#
# 不做什么：idle 的历史会话永远不会被拉起来（force 手动补跑也只重试上次那批）。
# 崩溃防护：
#   maxSessions 8      —— 一次最多恢复 8 个（旧默认 2，会让"关之前开了 5 个"只接上 2 个）；
#   maxSessionLogMB 8  —— 单会话日志上限；maxTotalLogMB 64 —— 本轮累计上限；
#   continueOnlyFirst false —— 每个恢复上来的会话都真的续跑（仍串行，不并发压模型）；
#   anyWorkspace true  —— 跨工作区也恢复：「关之前在跑」跟在哪个工作区无关；
#   runningWindowMin 15—— 关闭前 15 分钟内还在 running 的也算「关的时候在跑」
#                        （退出流程会先把 turn 收尾再 dispose，只看 dispose 那一刻会漏）。
#
# 查看：GET /resume-sessions/state   手动补跑：POST /resume-sessions/restore
- insert:
    - id: resume-sessions
      name: 'dsh-resume-sessions'
      config:
        enabled: true
        mode: 'continue'
        startupDelayMs: 500
        resumeTimeoutMs: 90000
        maxAgeHours: 48
        maxSessions: 8
        runningWindowMin: 15
        resumeGapMs: 2500
        maxSessionLogMB: 8
        maxTotalLogMB: 64
        continueOnlyFirst: false
        anyWorkspace: true
        includeGoalActive: true
        rearmGoals: true
        verbose: true
"""

_PAYLOAD_FILES = ("index.js", "package.json", "dsh.plugin.json", "README.md")


# ── 路径 ────────────────────────────────────────────────────────────────────


def payload_dir() -> Path:
    """内置插件源码目录（源码运行 / PyInstaller 单文件都能定位）。"""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        cand = Path(frozen) / "modules" / "payload" / PLUGIN_NAME
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent / "payload" / PLUGIN_NAME


def resolve_home(config) -> Path:
    home = getattr(config, "deepseek_plugins_dir", "") or ""
    if home:
        return Path(home)
    return Path(os.environ.get("USERPROFILE", "")) / ".dsh"


def resolve_profile_dir(home: Path, profile: str = "desktop") -> Path:
    return Path(home) / "profiles" / profile


def snapshot_path(home: Path) -> Path:
    return Path(home) / STATE_FILE


# ── patch 文本（只动目标条目，其余字节不动） ────────────────────────────────


def _lines(text: str) -> List[str]:
    return text.splitlines()


def find_entry_span(text: str, entry_id: str) -> Optional[Tuple[int, int]]:
    """定位 `- id: <entry_id>` 那一项的行区间 [start, end)。"""
    lines = _lines(text)
    start = None
    indent = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-\s+id:\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
        if m:
            if start is not None:
                return (start, i)
            if m.group(2) == entry_id:
                start = i
                indent = len(m.group(1))
            continue
        if start is not None:
            stripped = line.strip()
            if stripped == "" or stripped.startswith("#"):
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= indent and not stripped.startswith("-"):
                return (start, i)
    if start is None:
        return None
    return (start, len(lines))


def entry_state(text: str) -> str:
    """'missing' | 'enabled' | 'disabled'"""
    span = find_entry_span(text, PLUGIN_ID)
    if span is None:
        return "missing"
    start, end = span
    block = "\n".join(_lines(text)[start:end])
    for line in block.splitlines():
        m = re.match(r"^\s*enabled:\s*(true|false)\s*$", line)
        if m:
            return "enabled" if m.group(1) == "true" else "disabled"
    return "enabled"  # 没写 enabled 时插件默认启用


def set_entry_enabled(text: str, enabled: bool = True) -> str:
    """把目标条目的 enabled 打开；块里没有这个键就补一行（缩进跟随 config 子键）。"""
    span = find_entry_span(text, PLUGIN_ID)
    if span is None:
        return text
    lines = _lines(text)
    start, end = span
    value = "true" if enabled else "false"
    for i in range(start, end):
        if re.match(r"^\s*enabled:\s*(true|false)\s*$", lines[i]):
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = f"{indent}enabled: {value}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    # 找 config: 行，按其子键缩进插入
    for i in range(start, end):
        m = re.match(r"^(\s*)config:\s*$", lines[i])
        if m:
            lines.insert(i + 1, " " * (len(m.group(1)) + 2) + f"enabled: {value}")
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    # 没有 config 段：直接在 name 行后插一段
    for i in range(start, end):
        if re.match(r"^\s*name:", lines[i]):
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i + 1 : i + 1] = [f"{indent}config:", f"{indent}  enabled: {value}"]
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text


def ensure_entry(text: str) -> Tuple[str, str]:
    """保证 patch 里有这条 insert 且启用。返回 (新文本, 动作)。"""
    state = entry_state(text)
    if state == "missing":
        sep = "" if text.endswith("\n") or text == "" else "\n"
        return text + sep + ("\n" if text.strip() else "") + PATCH_BLOCK, "inserted"
    if state == "disabled":
        return set_entry_enabled(text, True), "enabled"
    return text, "unchanged"


#: "多会话一起续跑"的基线阈值。
#:
#: 为什么要在每次启动时纠回：profile 被重置成模板 / 重装 / 从旧备份还原，都会把它们
#: 带回老默认（maxSessions 2 + continueOnlyFirst true），表现就是"关之前开了 5 个会话，
#: 重启只接着跑 1 个" —— 而这种降级是静默发生的，人只会觉得"这功能时好时坏"。
RESUME_BASELINE = {
    "maxSessions": "8",
    "maxTotalLogMB": "64",
    "continueOnlyFirst": "false",
    "maxSessionLogMB": "8",
    "anyWorkspace": "true",
    "staleLiveAsInterrupted": "true",
}


def _config_indent(lines: List[str], start: int, end: int) -> Optional[int]:
    for i in range(start, end):
        m = re.match(r"^(\s*)config:\s*$", lines[i])
        if m:
            return len(m.group(1))
    return None


def ensure_thresholds(text: str, _depth: int = 0) -> Tuple[str, List[str]]:
    """把续跑阈值纠回基线：值不对就改，缺了就补在 config 段末尾。

    只碰这四个键，其余配置（工作区、间隔、超时）一律不动。
    """
    span = find_entry_span(text, PLUGIN_ID)
    if span is None:
        return text, []
    lines = _lines(text)
    start, end = span
    cfg_indent = _config_indent(lines, start, end)
    if cfg_indent is None:
        return text, []

    changed: List[str] = []
    for key, want in RESUME_BASELINE.items():
        for i in range(start, end):
            m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", lines[i])
            if not m:
                continue
            if len(m.group(1)) <= cfg_indent:      # 不是 config 的子键
                continue
            if m.group(2) != key:
                continue
            cur = m.group(3).strip().strip("'\"")
            if cur != want:
                lines[i] = " " * len(m.group(1)) + f"{key}: {want}"
                changed.append(f"{key}: {cur} -> {want}")
            break
        else:
            # 缺这个键：补在 config 段最后一个子键后面，然后重算一遍（位置会变）
            insert_at = None
            for i in range(start, end):
                m = re.match(r"^(\s*)([A-Za-z_][\w-]*)\s*:\s*(.*)$", lines[i])
                if m and len(m.group(1)) > cfg_indent:
                    insert_at = i + 1
            if insert_at is None:
                continue
            lines.insert(insert_at, " " * (cfg_indent + 2) + f"{key}: {want}")
            new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            more_text, more = ensure_thresholds(new_text, _depth + 1)
            return more_text, changed + [f"{key}: +{want}"] + more

    if not changed:
        return text, []
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), changed


# ── 文件与链接 ──────────────────────────────────────────────────────────────


def _backup(path: Path, tag: str = "zf-resume") -> Optional[Path]:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.bak-{tag}-{stamp}")
    try:
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.zf-tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def make_link(link: Path, target: Path) -> bool:
    """建目录链接（Windows 用 junction，不需要管理员）。已存在就复用。"""
    if is_real_link(link):
        return True
    try:
        if link.exists() or link.is_symlink():
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link, ignore_errors=True)
            else:
                link.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, text=True, encoding="mbcs", errors="replace",
            )
            return r.returncode == 0 and link.exists()
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def is_real_link(path: Path) -> bool:
    """是否是符号链接 / junction。

    注意必须用 ``os.lstat``：``Path.stat()`` 会跟随链接，Windows 上 junction 会
    表现成普通目录（attributes 只有 0x10），于是"已建好"被误判成"没建好"，
    每次自愈都白重建一遍。
    """
    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            st = os.lstat(path)
            attrs = getattr(st, "st_file_attributes", 0)
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False
    return False


# ── 插件状态 / 自愈 ─────────────────────────────────────────────────────────


def plugin_status(pdir: Path) -> Dict[str, Any]:
    pdir = Path(pdir)
    patch = pdir / "cordis.patch.yml"
    text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    state = entry_state(text)
    vendor = pdir / "vendor" / PLUGIN_NAME
    index = vendor / "index.js"
    link = pdir / "node_modules" / PLUGIN_NAME
    deps: Dict[str, str] = {}
    pkg = pdir / "package.json"
    if pkg.is_file():
        try:
            deps = json.loads(pkg.read_text(encoding="utf-8")).get("dependencies") or {}
        except (json.JSONDecodeError, OSError):
            deps = {}
    return {
        "patch": state,
        "vendor": index.is_file(),
        "vendor_index_bytes": index.stat().st_size if index.is_file() else 0,
        "dependency": PLUGIN_NAME in deps,
        "node_modules_link": is_real_link(link) if link.exists() else False,
        "node_modules_present": link.exists(),
        "ready": state == "enabled" and index.is_file() and PLUGIN_NAME in deps and link.exists(),
    }


def ensure_plugin(home: Path, profile: str = "desktop", *,
                  host_llm: Optional[Path] = None,
                  force: bool = False) -> Dict[str, Any]:
    """把插件装回 profile：vendor 源码 / 依赖声明 / node_modules 链接 / patch 行。"""
    home = Path(home)
    pdir = resolve_profile_dir(home, profile)
    report: Dict[str, Any] = {"profile": str(pdir), "actions": [], "errors": [], "warnings": []}

    if not (pdir / "package.json").is_file():
        report["errors"].append(f"profile 不存在或缺少 package.json：{pdir}")
        return report

    src = payload_dir()
    if not (src / "index.js").is_file():
        report["errors"].append(f"内置插件源码缺失：{src}")
        return report

    # 1. vendor 源码
    #
    # 判据不能只看"大小不同"：内置 payload 是打包那一刻的快照，本地若先修过
    # vendor（比如 2026-11-12 补的「恢复要挂 agent preset」），大小必然和 payload
    # 不同，只比大小就会用旧 payload 把本地修复覆盖回去——而且是在启动 4 秒后
    # 静默发生，人根本看不见。所以：vendor 比 payload 新就以 vendor 为准。
    vendor = pdir / "vendor" / PLUGIN_NAME
    need_copy = force or not (vendor / "index.js").is_file()
    vendor_newer = False
    if not need_copy:
        try:
            vendor_stat = (vendor / "index.js").stat()
            src_stat = (src / "index.js").stat()
            if vendor_stat.st_size != src_stat.st_size:
                vendor_newer = vendor_stat.st_mtime > src_stat.st_mtime
                need_copy = not vendor_newer
        except OSError:
            need_copy = True
    if need_copy:
        vendor.mkdir(parents=True, exist_ok=True)
        for name in _PAYLOAD_FILES:
            s = src / name
            if s.is_file():
                shutil.copy2(s, vendor / name)
        report["actions"].append("已复制插件源码到 vendor/")
    elif vendor_newer:
        report["warnings"].append(
            "vendor/ 的插件比内置 payload 新，保留 vendor（不覆盖本地修复）；"
            "要固化进 exe 请重新打包"
        )

    # 2. 依赖声明
    pkg_path = pdir / "package.json"
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report["errors"].append(f"package.json 读不了：{exc}")
        return report
    deps = pkg.setdefault("dependencies", {})
    if deps.get(PLUGIN_NAME) != PLUGIN_DEP:
        _backup(pkg_path)
        deps[PLUGIN_NAME] = PLUGIN_DEP
        _atomic_write(pkg_path, json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
        report["actions"].append("package.json 已声明依赖")

    # 3. node_modules 链接
    link = pdir / "node_modules" / PLUGIN_NAME
    if not link.exists() or not is_real_link(link):
        if make_link(link, vendor):
            report["actions"].append("已建立 node_modules 链接")
        else:
            report["warnings"].append("node_modules 链接建立失败（下次 DSH 启动前可手跑 pnpm install）")

    # 4. 插件运行时要用的 host 包链接（失败也能跑，只是消息结构退化成手写）
    llm_src = Path(host_llm) if host_llm else DEFAULT_HOST_LLM
    if llm_src.is_dir():
        llm_link = vendor / "node_modules" / "@deepseek-ai" / "dsh-llm"
        if not llm_link.exists():
            make_link(llm_link, llm_src)

    # 5. cordis.patch.yml
    patch = pdir / "cordis.patch.yml"
    text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    new_text, action = ensure_entry(text)
    if action != "unchanged":
        _backup(patch, "zf-resume")
        _atomic_write(patch, new_text)
        report["actions"].append({"inserted": "patch 已插入插件条目", "enabled": "patch 里已把插件改回 enabled: true"}[action])

    # 阈值纠回基线：maxSessions / continueOnlyFirst 被改回老默认时，
    # 表现就是"多个会话只续跑一个"，而且不会有任何提示 —— 必须每次都看一眼。
    fixed_text, thr_actions = ensure_thresholds(new_text)
    if thr_actions:
        if action == "unchanged":
            _backup(patch, "zf-resume")
        _atomic_write(patch, fixed_text)
        report["actions"].append(
            "续跑阈值已纠回基线：" + "，".join(thr_actions))

    report["status"] = plugin_status(pdir)
    if not report["status"]["ready"] and not report["errors"]:
        report["warnings"].append("安装后仍未就绪，检查上面的 actions/errors")
    return report


# ── 快照与"谁在跑" ──────────────────────────────────────────────────────────


def read_snapshot(home: Path) -> Dict[str, Any]:
    path = snapshot_path(home)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def has_open_turn(home: Path, session_id: str) -> bool:
    """会话日志里是否还有未关闭的 turn（= 崩溃时确实在跑）。"""
    cache = Path(home) / "storages" / "session_projcache" / "sessions" / f"{session_id}.json"
    try:
        doc = json.loads(cache.read_text(encoding="utf-8"))
        val = doc.get("record", {}).get("rows", {}).get("turnBoundary", {}).get("val") or {}
        open_seq = val.get("openTurnStartSeq")
        return isinstance(open_seq, int) and open_seq >= 0
    except (json.JSONDecodeError, OSError, AttributeError):
        return False


def is_blank_session(home: Path, session_id: str) -> bool:
    """从没发过 prompt 的空白会话：没有标题，侧栏只会显示工作区名。"""
    cache = Path(home) / "storages" / "session_projcache" / "sessions" / f"{session_id}.json"
    try:
        doc = json.loads(cache.read_text(encoding="utf-8"))
        rows = doc.get("record", {}).get("rows", {})
        meta = (rows.get("sessionListMetadata") or {}).get("val") or {}
        return isinstance(meta, dict) and meta.get("blank") is True
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return False
    return False


def session_title(home: Path, session_id: str, fallback: str = "") -> str:
    """会话标题：投影 title → 首条 prompt 截断 → fallback。

    快照里的 title 可能是空的（老版本插件只读 header.title），这里补一层，
    免得页签上全是"（无标题）"。
    """
    cache = Path(home) / "storages" / "session_projcache" / "sessions" / f"{session_id}.json"
    try:
        doc = json.loads(cache.read_text(encoding="utf-8"))
        rows = doc.get("record", {}).get("rows", {})
        title = (rows.get("title") or {}).get("val")
        if isinstance(title, str) and title.strip():
            return title.strip()
        outline = (rows.get("turnOutline") or {}).get("val") or {}
        turns = outline.get("turns") if isinstance(outline, dict) else []
        if turns:
            first = (turns[0].get("prompt") or "").strip().replace("\n", " ")
            if first:
                return first[:40] + ("…" if len(first) > 40 else "")
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        pass
    return fallback


def session_log_bytes(home: Path, session_id: str) -> int:
    """会话日志总字节数（决定"恢复它会不会把内存打满"）。"""
    root = Path(home) / "sessions"
    total = 0
    try:
        for workspace in root.iterdir():
            if not workspace.is_dir():
                continue
            folder = workspace / session_id
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _parse_scalar(text: str) -> Any:
    """cordis.patch.yml 里一个标量值的解析（够用即可，不引 yaml 依赖）。"""
    value = str(text).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("", "null", "~"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def read_plugin_config(pdir: Path) -> Dict[str, Any]:
    """profile 里插件的**现役**配置（cordis.patch.yml 的 resume-sessions 条目）。

    为什么必须读现役配置而不是写死默认值：面板上「下次会不会被恢复」完全由这些阈值
    决定（maxAgeHours / runningWindowMin / maxSessions / workspacePath），
    写死一份默认等于面板在替用户猜 —— 实测就因为口径不一致，
    面板说"7 个会恢复"，插件其实只动 2 个。
    """
    pdir = Path(pdir)
    patch = pdir / "cordis.patch.yml"
    text = patch.read_text(encoding="utf-8") if patch.is_file() else ""
    span = find_entry_span(text, PLUGIN_ID)
    if span is None:
        return {}
    start, end = span
    cfg: Dict[str, Any] = {}
    in_config = False
    config_indent = 0
    for line in _lines(text)[start:end]:
        m = re.match(r"^(\s*)config:\s*$", line)
        if m:
            in_config = True
            config_indent = len(m.group(1))
            continue
        if not in_config:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= config_indent:
            break
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            cfg[m.group(1)] = _parse_scalar(m.group(2))
    return cfg


def default_workspace_path(home: Path) -> str:
    """DSH 当前默认工作区（插件按它筛候选）。"""
    ws = Path(home) / "storages" / "workspace.json"
    try:
        doc = json.loads(ws.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    gid = (doc.get("global") or {}).get("defaultWorkspaceId")
    row = ((doc.get("tables") or {}).get("workspaces") or {}).get(gid) or {}
    path = row.get("path")
    return path if isinstance(path, str) else ""


def archived_session_ids(home: Path) -> set:
    """用户归档掉的会话（插件不会恢复它们）。"""
    ws = Path(home) / "storages" / "workspace.json"
    try:
        doc = json.loads(ws.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    ids = (doc.get("global") or {}).get("archivedSessionIds")
    if not isinstance(ids, list):
        return set()
    return {x for x in ids if isinstance(x, str)}


def _same_path(a: str, b: str) -> bool:
    """两个路径是不是同一个（大小写不敏感，Windows 语义）。"""
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (OSError, ValueError):
        return a.lower().rstrip("\\/") == b.lower().rstrip("\\/")


def last_turn_end(entry: Dict[str, Any]) -> int:
    """最后一轮 turn 收尾的时刻（快照 history 里最后一个 idle）。

    'disposed' 不能当 turn 收尾：它的 t 就是关闭时刻，拿它当锚点会让判据恒真。
    """
    hist = entry.get("history") or []
    if not isinstance(hist, list):
        return 0
    for item in reversed(hist):
        if not isinstance(item, dict):
            continue
        t = item.get("t")
        s = item.get("s")
        if not isinstance(t, (int, float)) or isinstance(t, bool):
            continue
        if s == "idle" or (s not in ("running", "disposed")):
            return int(t)
    return 0


def closed_while_running(entry: Dict[str, Any], closed_at: Optional[int] = None,
                         window_min: int = 15) -> bool:
    """关闭那一刻它是不是刚跑过（与插件 closedWhileRunning 同一套语义）。

    锚点取 runningAt（turn 起点）与 turn 收尾时刻里较晚的那个；
    关闭时刻**只认 disposedAt** —— 没被 dispose 过就没有"关闭"这回事，
    那类跑完就 idle 的老会话不该在 48 小时里一直占着候选名额。
    """
    window_ms = max(0, int(window_min)) * 60_000
    if window_ms <= 0:
        return False
    closed = int(closed_at if closed_at is not None else (entry.get("disposedAt") or 0))
    if closed <= 0:
        return False
    last_active = max(int(entry.get("runningAt") or 0), last_turn_end(entry))
    if last_active <= 0:
        return False
    return closed >= last_active and closed - last_active <= window_ms


def pending_sessions(home: Path, include_idle: bool = False,
                     limit: int = 0, cfg: Optional[Dict[str, Any]] = None,
                     workspace: Optional[str] = None,
                     now: Optional[int] = None) -> List[Dict[str, Any]]:
    """会话清单。

    口径**对齐插件的 pick()**（modules/payload/dsh-resume-sessions/index.js）：
      - 关那一刻确实在跑：running / interrupted / active goal / 关闭前窗口内还在跑；
      - 空白会话（从没发过 prompt）不算；
      - 已经恢复过（live / restored）的不会被再抢一次；
      - 归档掉的（archivedSessionIds）不算；
      - 超过 maxAgeHours 保质期的不算；
      - 不在插件当前工作区的不算；
      - 真正会恢复的只有名额内的前 maxSessions 个（按 runningAt 倒序）。

    include_idle=True：连已结束的一起返回，页签用它 —— 否则列表一空，
    人就会以为"它根本没检测到我的会话"，其实只是那个会话当时没在跑。

    now：可注入的时钟（毫秒）。保质期是相对时间，测试必须钉住时钟，
    否则"造出来的 6 月份会话"会被真实时间判成超保质期 —— 测试就不测它想测的东西了。
    """
    home = Path(home)
    cfg = cfg or {}
    max_age_ms = int(cfg.get("maxAgeHours", 48) or 0) * 3_600_000
    window_min = int(cfg.get("runningWindowMin", 15) or 0)
    max_sessions = int(cfg.get("maxSessions", 2) or 0)
    if workspace is None:
        if cfg.get("anyWorkspace"):
            workspace = ""
        else:
            workspace = str(cfg.get("workspacePath") or "") or default_workspace_path(home)
    archived = archived_session_ids(home)
    now = int(now if now is not None else time.time() * 1000)

    snap = read_snapshot(home)
    sessions = snap.get("sessions") or {}
    if not isinstance(sessions, dict):
        return []
    # 上个进程最后一次写盘的时刻（= 它消失的时刻）。崩溃/被杀的会话条目停在 live，
    # 插件那边会按这个锚点把它们重新判成"关闭时在跑"，面板口径必须一致。
    prev_written = 0
    _stamp = str(snap.get("updatedAt") or "").strip()
    for _fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            prev_written = int(datetime.strptime(_stamp, _fmt)
                               .replace(tzinfo=timezone.utc).timestamp() * 1000)
            break
        except Exception:
            prev_written = 0
    stale_live = bool(cfg.get("staleLiveAsInterrupted"))
    rows: List[Dict[str, Any]] = []
    for sid, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        # 三种"关闭前确实在跑"：还在 running / 在 running 时被 dispose（正常退出或崩溃）/
        # goal 仍 active / 关闭前窗口内还在跑（退出流程会先收尾 turn 再 dispose）。
        running = entry.get("status") == "running" or entry.get("running") is True
        interrupted = entry.get("interrupted") is True
        goal_active = entry.get("goalPhase") == "active"
        recent = closed_while_running(entry, window_min=window_min)
        blank = is_blank_session(home, sid)
        candidate = bool((running or interrupted or goal_active or recent) and not blank)
        if entry.get("status") in ("live", "restored"):
            # 本进程里真的活着 → 不抢；但如果是上个进程留下的 live（没 dispose），
            # 按它消失的时刻重新判一次，否则崩溃重启后一个都不会恢复。
            revived = False
            if stale_live and prev_written > 0 and not entry.get("disposedAt"):
                revived = closed_while_running(entry, closed_at=prev_written,
                                               window_min=window_min)
            candidate = bool(revived and not blank)
        if candidate and sid in archived:
            candidate = False
        if candidate and max_age_ms and now - int(entry.get("at") or 0) > max_age_ms:
            candidate = False
        if candidate and workspace and not _same_path(entry.get("cwd") or "", workspace):
            candidate = False
        if not candidate and not include_idle:
            continue
        open_turn = has_open_turn(home, sid)
        rows.append({
            "session_id": sid,
            "title": entry.get("title") or session_title(home, sid),
            "cwd": entry.get("cwd") or "",
            "status": entry.get("status") or "",
            "running": running,
            "interrupted": interrupted,
            "goal_phase": entry.get("goalPhase"),
            "running_at": entry.get("runningAt") or entry.get("at") or 0,
            "at": entry.get("at") or 0,
            "last_error": entry.get("lastError"),
            "open_turn": open_turn,
            "blank": blank,
            "log_mb": round(session_log_bytes(home, sid) / (1024 * 1024), 1),
            "candidate": candidate,
            "will_restore": False,
            "will_continue": False,
        })
    # 插件的排序：runningAt 倒序（没有就退回 at）。
    rows.sort(key=lambda r: (r["running_at"] or r["at"] or 0), reverse=True)
    # 名额：只有排在前 max_sessions 的候选才是"下次真的会被恢复"。
    picked = [r for r in rows if r["candidate"]][:max_sessions] if max_sessions > 0 else []
    picked_ids = {r["session_id"] for r in picked}
    for row in rows:
        row["will_restore"] = row["session_id"] in picked_ids
        row["will_continue"] = bool(
            row["will_restore"] and (row["interrupted"] or row["open_turn"] or row["running"])
        )
    if limit and limit > 0:
        rows = rows[:limit]
    return rows


def snapshot_guard(home: Path) -> Dict[str, Any]:
    snap = read_snapshot(home)
    boot = snap.get("boot") if isinstance(snap.get("boot"), dict) else {}
    guard = snap.get("guard") if isinstance(snap.get("guard"), dict) else {}
    return {
        "crashes": int(guard.get("crashes") or 0),
        "restoring": bool(boot.get("restoring")),
        "pid": boot.get("pid"),
        "updated_at": snap.get("updatedAt"),
        "phase": snap.get("phase") or "",
        "last_restored": snap.get("lastRestored") or None,
        "total": len(snap.get("sessions") or {}) if isinstance(snap.get("sessions"), dict) else 0,
    }


def backup_snapshot(home: Path, tag: str = "zf-resume") -> Optional[Path]:
    return _backup(snapshot_path(home), tag)


def restore_snapshot_backup(home: Path, backup: Path) -> bool:
    target = snapshot_path(home)
    try:
        shutil.copy2(Path(backup), target)
        return True
    except OSError:
        return False


# ── DSH 端点（可选；插件在跑时能直接看/补跑） ──────────────────────────────


_PORT_CACHE: Dict[str, Any] = {"at": 0.0, "ports": []}
# 原来 5 秒。refresh 定时器也是 5 秒，等于每次刷新都要重新起
# powershell.exe + netstat.exe 两趟（各~100ms 且弹控制台窗口）。
# 端口只会在 DSH 重启时变，放 30 秒足够，启动次数降到 1/6。
_PORT_TTL = 30.0

#: DSH 桌面端 host 进程的判别特征（命令行里带这个路径）。
_HOST_MARK = "dsh-desktop-host"


def _system32(name: str) -> str:
    """System32 下的可执行文件绝对路径。

    为什么不能只写裸名字：Windows 的 CreateProcess 只按「程序所在目录 → 当前目录 →
    System32 → Windows 目录 → PATH」找，**PATH 里没有的目录不会被搜**。
    沙箱/服务/计划任务这类环境的 PATH 常常是裁剪过的（DSH 沙箱里就没有 System32），
    裸名字直接 FileNotFoundError。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return os.path.join(root, "System32", name)


def _powershell_exe() -> str:
    """powershell.exe 的绝对路径。

    powershell.exe 不在 System32，而在 System32\\WindowsPowerShell\\v1.0\\，
    那个目录默认不在 PATH 里 —— 裸名字在很多环境下必然找不到。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    cand = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(cand):
        return cand
    import shutil

    return shutil.which("powershell.exe") or "powershell.exe"


def _host_pids() -> set:
    """找 DSH host 的进程号（psutil 优先，不需要外部命令）。"""
    try:
        import psutil
    except Exception:
        return set()
    pids = set()
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if _HOST_MARK in cmd:
                pids.add(int(proc.info["pid"]))
    except Exception:
        return pids
    return pids


def _ports_by_psutil() -> List[int]:
    """用 psutil 直接读「谁在监听哪个回环端口」。零外部命令，最稳的一条路。"""
    try:
        import psutil
    except Exception:
        return []
    pids = _host_pids()
    if not pids:
        return []
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        conns = []
    ports: List[int] = []
    for conn in conns:
        if getattr(conn, "status", "") != "LISTEN":
            continue
        if not getattr(conn, "pid", None) or conn.pid not in pids:
            continue
        laddr = getattr(conn, "laddr", None)
        if not isinstance(laddr, (tuple, list)) or len(laddr) < 2:
            continue
        host = str(laddr[0])
        port = int(laddr[1])
        if host not in ("127.0.0.1", "0.0.0.0", "::", "::1", ""):
            continue
        if port > 0:
            ports.append(port)
    return sorted(set(ports))


def _ports_by_netstat() -> List[int]:
    """老路：PowerShell 取 pid + netstat 取监听端口（绝对路径版）。"""
    if os.name != "nt":
        return []
    # 解码必须用 mbcs（中文 Windows 的 netstat / powershell 输出是 ANSI 代码页）：
    # 原来写 text=True 走 UTF-8，读线程解码失败 → stdout 变成 None →
    # 下一行 .splitlines() 抛 AttributeError，整个"会话续跑"页签永远显示读取失败。
    # errors="replace" 再兜一层：就算某个字符解不出，也只坏一个字符，不毁整份输出。
    try:
        ps = subprocess.run(
            [_powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*" + _HOST_MARK + "*' } "
             "| Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, encoding="mbcs", errors="replace",
            timeout=12,
            creationflags=0x08000000,  # CREATE_NO_WINDOW：别弹"管理员: powershell.exe"黑框
        )
        pids = {p.strip() for p in (ps.stdout or "").splitlines() if p.strip().isdigit()}
        if not pids:
            return []
        net = subprocess.run([_system32("netstat.exe"), "-ano", "-p", "TCP"], capture_output=True, text=True,
                             encoding="mbcs", errors="replace",
                             timeout=12, creationflags=0x08000000)
        ports: List[int] = []
        for line in (net.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            if parts[4] not in pids:
                continue
            addr = parts[1]
            if ":" in addr and addr.split(":")[0] in ("127.0.0.1", "0.0.0.0", "[::]", "::"):
                try:
                    ports.append(int(addr.rsplit(":", 1)[1]))
                except ValueError:
                    continue
        return sorted(set(ports))
    except (OSError, subprocess.SubprocessError):
        return []


def _candidate_ports() -> List[int]:
    """DSH host 可能在监听的所有回环端口（去重、保序）。"""
    ports: List[int] = []
    for group in (_ports_by_psutil(), _ports_by_netstat()):
        for port in group:
            if port not in ports:
                ports.append(port)
    return ports


def discover_ports(force: bool = False) -> List[int]:
    """DSH host 监听的候选端口列表（30 秒缓存）。

    原来只有一个 discover_port()，一旦第一条路失败就直接判定"没找到在跑的 DSH"，
    面板上的「查看状态 / 手动补跑」跟着全废。现在两条路都走，谁先通用谁。
    """
    now = time.time()
    if not force and now - float(_PORT_CACHE["at"] or 0) < _PORT_TTL:
        return list(_PORT_CACHE["ports"] or [])
    ports = _candidate_ports()
    _PORT_CACHE["at"] = now
    _PORT_CACHE["ports"] = ports
    return ports


def discover_port(force: bool = False) -> Optional[int]:
    """DSH host 的端口（第一个候选）。

    注意：这里只保证"host 在这个端口上"，不保证 /resume-sessions/state 有响应；
    端点是否可用由 probe_state() 单独判定。
    """
    ports = discover_ports(force)
    return ports[0] if ports else None


def _http(url: str, method: str = "GET", timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    try:
        import urllib.request

        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 本机回环
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        return data if isinstance(data, dict) else {"raw": body}
    except Exception:  # noqa: BLE001 - 端点不可用不是错误路径
        return None


def probe_state(port: int) -> Optional[Dict[str, Any]]:
    return _http(f"http://127.0.0.1:{port}/resume-sessions/state")


def trigger_restore(port: int, timeout: float = 120.0) -> Optional[Dict[str, Any]]:
    return _http(f"http://127.0.0.1:{port}/resume-sessions/restore", method="POST", timeout=timeout)


# ── 一个给 UI 用的总览 ──────────────────────────────────────────────────────


def overview(config, profile: str = "desktop",
             host_llm: Optional[Path] = None) -> Dict[str, Any]:
    home = resolve_home(config)
    pdir = resolve_profile_dir(home, profile)
    status = plugin_status(pdir) if pdir.is_dir() else {}
    pcfg = read_plugin_config(pdir)
    rows = pending_sessions(home, cfg=pcfg)
    recent = pending_sessions(home, include_idle=True, limit=20, cfg=pcfg)

    # 端口：host 可能同时开好几个监听口，逐个试，谁能答 /resume-sessions/state 谁就是它。
    ports = discover_ports()
    port = None
    live = None
    for candidate in ports:
        state = probe_state(candidate)
        if state is not None:
            port, live = candidate, state
            break
    if port is None and ports:
        port = ports[0]  # 找到了 host，只是端点没答

    # 两套口径：
    #   本地复算 = pending_sessions()，用**现役配置 + 修好的语义**算出来；
    #   端点自报 = 宿主里正在跑的插件自己算的 candidates。
    # 两者一致就没什么可说的；不一致说明宿主里那份插件代码是旧的
    # （插件升级后要等 DSH 重启才换），这时以本地口径为准，并把差异报出来，
    # 而不是默默拿旧代码的结果当权威。
    local_picked = [r["session_id"] for r in rows if r["will_restore"]]
    endpoint_candidates: List[str] = []
    if live:
        endpoint_candidates = [str(x) for x in (live.get("candidates") or [])]
    mismatch = sorted(set(local_picked) ^ set(endpoint_candidates)) if live else []
    return {
        "home": str(home),
        "profile": profile,
        "profile_dir": str(pdir),
        "plugin": status,
        "plugin_config": pcfg,
        "plugin_payload": str(payload_dir()),
        "snapshot_file": str(snapshot_path(home)),
        "guard": snapshot_guard(home),
        "pending": rows,
        "recent": recent,
        "candidates": local_picked,
        "candidate_count": len(local_picked),
        "endpoint_candidates": endpoint_candidates,
        "candidates_mismatch": mismatch,
        "max_sessions": int(pcfg.get("maxSessions", 2) or 0),
        "port": port,
        "ports": ports,
        "endpoint_state": live,
        "endpoint_ok": live is not None,
    }
