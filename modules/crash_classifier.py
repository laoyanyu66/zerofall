# -*- coding: utf-8 -*-
"""
DSH 崩溃日志分类器

ZeroFall 看到 crash-*.log 后，先读内容分类，再选恢复策略：
  - auth_failed   : Web 认证失败 / welcome 握手失败 → 清认证态重登，不要回滚插件
  - config_error  : profile 补丁文件被写坏（overlay 解析失败 / NUL 字节）→ 修文件，不要回滚插件
  - plugin_error  : 插件/Node 模块错误（EPIPE、dsh-rea-plugin、node_modules）→ 回滚插件快照
  - runtime_error : 运行时/渲染进程崩溃 → 提示用户
  - unknown       : 未知 → 走通用快照选择流程
"""
from pathlib import Path
from typing import Optional, Tuple
import re

from logger import ZeroFallLogger


# 关键词 → 分类。顺序敏感：先匹配先命中。
_AUTH_PATTERNS = [
    re.compile(r'web authentication failed', re.I),
    re.compile(r'desktop welcome', re.I),
    re.compile(r'connectDesktopWelcome', re.I),
    re.compile(r'auth(entication)?\s*failed', re.I),
    re.compile(r'sign[- ]?in required', re.I),
]

_PLUGIN_PATTERNS = [
    re.compile(r'dsh-rea-plugin', re.I),
    re.compile(r'node_modules', re.I),
    re.compile(r'cannot resolve profile bundle', re.I),
    re.compile(r'write EPIPE', re.I),
    re.compile(r'error:\s*cannot find module', re.I),
    re.compile(r'Module not found', re.I),
]

_RUNTIME_PATTERNS = [
    re.compile(r'crashpad', re.I),
    re.compile(r'GPU process crashed', re.I),
    re.compile(r'renderer process gone', re.I),
    re.compile(r'Desktop renderer exited', re.I),
    re.compile(r'renderer exited:\s*crashed', re.I),
    re.compile(r'out of memory', re.I),
]

# 配置文件被写坏（overlay / YAML）
#
# 2026-11-17 实机连踩三次，宿主在 main() 里 parsePatchList 抛
# "YAMLException: null byte is not allowed in input"，整个 host 进程退出。
# 这一类**回滚插件快照完全无效**：插件没坏，是 profile 的 cordis.patch.yml
# 被写坏了（并发重写 + 强杀/掉电留下半截或 NUL 前缀）。
# 正确处置是修文件（modules/patch_guard.py），不是回退历史。
_CONFIG_PATTERNS = [
    re.compile(r'failed to parse overlay', re.I),
    re.compile(r'null byte is not allowed', re.I),
    re.compile(r'yaml ?exception', re.I),
    re.compile(r'failed to parse [^\s]*\.ya?ml', re.I),
]

# 宿主启动期就退出了（不是配置坏了、也不是插件报错）
#
# 2026-11-18 07:16 那次：main 进程的 AggregateError 里两条 ——
#   `dsh desktop host stopped: [dsh-purge-community] ...`
#   和 `host did not exit after SIGKILL`
# 也就是宿主在启动阶段自己停了，随后清理阶段连 SIGKILL 都没能把它收掉。
# 这类多半是"上一次换版/换补丁没落地"留下的状态机残留（D:\DeepSeek\resources
# 下的 app.asar.rename-pending / app.asar.purge-handoff.json），重试一次往往能过；
# 连续两次都这样才值得回滚。
_HOST_PATTERNS = [
    re.compile(r'dsh desktop host stopped', re.I),
    re.compile(r'host did not exit after SIGKILL', re.I),
    re.compile(r'desktop backend startup and cleanup failed', re.I),
]


def classify_crash_log(text: str) -> Tuple[str, str]:
    """读 crash log 文本，返回 (category, reason_one_line)。"""
    if not text:
        return ("unknown", "空崩溃日志")

    for pat in _AUTH_PATTERNS:
        m = pat.search(text)
        if m:
            return ("auth_failed", "Web 认证失败：{}".format(m.group(0)))

    for pat in _CONFIG_PATTERNS:
        m = pat.search(text)
        if m:
            return ("config_error", "配置文件损坏：{}".format(m.group(0)))

    for pat in _PLUGIN_PATTERNS:
        m = pat.search(text)
        if m:
            return ("plugin_error", "插件错误：{}".format(m.group(0)))

    for pat in _RUNTIME_PATTERNS:
        m = pat.search(text)
        if m:
            return ("runtime_error", "运行时崩溃：{}".format(m.group(0)))

    for pat in _HOST_PATTERNS:
        m = pat.search(text)
        if m:
            return ("host_startup", "宿主启动期退出：{}".format(m.group(0)))

    return ("unknown", "未识别的崩溃")


def read_latest_crash_log(dsh_appdata_dir: str) -> Tuple[Optional[Path], Optional[str]]:
    """在 dsh_appdata_dir/logs 下找最新的 crash-*.log，返回 (path, text)。"""
    if not dsh_appdata_dir:
        return (None, None)
    logs_dir = Path(dsh_appdata_dir) / "logs"
    if not logs_dir.is_dir():
        return (None, None)
    candidates = sorted(logs_dir.glob("crash-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return (None, None)
    latest = candidates[0]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
        return (latest, text)
    except Exception as e:
        ZeroFallLogger.warning("读取崩溃日志失败 {}: {}".format(latest, e))
        return (latest, None)


def clear_auth_state(dsh_appdata_dir: str) -> list:
    """损坏认证态清理：把 Cookies/Local Storage/Session Storage 改名加 .zerofall_bak 后缀。
    返回重命名的条目列表。失败的保留，让用户手动处理。
    """
    results = []
    root = Path(dsh_appdata_dir)
    if not root.is_dir():
        return results
    targets = [
        root / "Network" / "Cookies",
        root / "Local Storage",
        root / "Session Storage",
    ]
    for t in targets:
        if not t.exists():
            continue
        try:
            new_name = t.name + ".zerofall_bak"
            t.rename(t.parent / new_name)
            results.append(str(t))
            ZeroFallLogger.info("[认证态] 已重命名损坏文件: {} -> {}".format(t.name, new_name))
        except Exception as e:
            ZeroFallLogger.warning("[认证态] 重命名失败 {}: {}".format(t, e))
    return results
