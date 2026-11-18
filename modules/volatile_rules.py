# -*- coding: utf-8 -*-
"""易变运行时文件的单一真相源

为什么单独抽一个文件（2026-11-16 自检）：

1. 变更检测的指纹（`environment_checker.quick_fingerprint`）和快照复制
   （`snapshot_manager.copy_for_snapshot`）原本各维护一套排除表。
   指纹早就排掉了浏览器 profile、blue/、storages/memory/，
   快照却照样把 `douyin-browser/cli-profile` 打进包里 —— 两套规则各排各的，
   越用越分叉。

2. 更要命的是三个"每分钟心跳"文件没进任何排除表：
   `nightly/relay-status.json`、`guardian/state/last-boot.json`、
   `dsh-windows-mcp.versions.json`。它们每写一次就让指纹变一次，
   于是"内容没变就跳过静默快照"永远命中不了 ——
   2026-11-15 实测：430 次"有改动"判定 / 143 份快照 / 约 4.6 GB 写入，
   其实插件根本没动过。

这里只描述事实（哪些文件天然会变），怎么用由调用方决定：
  * FINGERPRINT_*：连"环境有没有被改"都不该算进去（心跳、浏览器运行时）
  * SNAPSHOT_*：仍属于环境，但不值得占包体（浏览器转储、统计库、轮转日志）

约定：所有模式都是小写、fnmatch 语义、按文件名（basename）匹配；
REL_PREFIXES 是相对 `.dsh` 根的 POSIX 路径前缀。
"""

# ── 心跳类：由插件自己每分钟重写，永远不该触发"环境被改" ──
HEARTBEAT_FILE_NAMES = (
    'relay-status.json',              # nightly/中继状态
    'last-boot.json',                 # guardian/state 启动时间戳
    'dsh-windows-mcp.versions.json',  # windows-mcp 版本探测结果
    # 2026-11-18 实测：媒体/QQ 的会话清单是 2 字节的 `{}`，DSH 每次启动重写一遍；
    # 内容永远一样，只有 mtime 换新 —— 不排掉就是"每开一次 DSH 一次假变更"。
    'chat-sessions.json',
)

# 指纹层面额外排除：浏览器运行时转储与统计库（只看名字）
FINGERPRINT_EXTRA_FILE_PATTERNS = HEARTBEAT_FILE_NAMES + (
    '*.pma',                 # Chromium BrowserMetrics/Crashpad 预分配转储
    'load_statistics.db*',
    'topTraffic_*',          # SmartScreen 远程信誉数据
    'crashpadmetrics*',
    # 天工账本的上一版（~/.dsh/tiangong-state.json.prev）：状态每存一次就重写，
    # 主文件被 `*state.json` 排掉了，带 .prev 后缀的这个漏在指纹里，
    # 2026-11-18 实测 09:55 / 10:12 两次差异条目第一条都是它。
    '*state.json.prev',
)

# 指纹层面额外排除：整棵子树（相对 .dsh 根）
FINGERPRINT_EXTRA_REL_PREFIXES = (
    'nightly/relay-logs/',
    'douyin-browser/cli-profile/',   # Chromium profile：每开一次窗就动
    # 2026-11-17 实测：守卫的 state 目录是"记账本"，不是插件环境。
    # 里面 live-port.json / session-scan-cache.json 每跑一次 `guardian doctor` 就刷新，
    # shadow.jsonl / flip.jsonl / boots.jsonl 每跑一次就开始追一行 ——
    # 而 ZeroFall 把整个 `.dsh` 当插件目录盯着，于是"验一次守卫 = 拍一份 48MB 快照"。
    # 当天日志证据：21:40 与 21:43 两次差异条目只有
    # `plugins|guardian/state/live-port.json`，两次各产一份快照。
    # 只影响指纹/MD5 判定，快照内容照样包含它们（真回退时这些账本也有用）。
    'guardian/state/',
    # 2026-11-18 改名后账本搬到 tianheng/state/，旧规则没跟着搬：
    # shadow.jsonl 每几秒追一行、boots.jsonl 每次启动写一行、baseline/inventory
    # 随自检刷新 —— 当日日志里 09:55、10:12 两次"变更点快照"的差异条目
    # 都含 `plugins|tianheng/state/*`，等于每次自检都白拍一份 15MB。
    'tianheng/state/',
)

# ── 快照层面：不值得进包的运行时垃圾（内容恢复出来也没用） ──
SNAPSHOT_JUNK_FILE_PATTERNS = (
    '*.pma',
    'load_statistics.db',
    'load_statistics.db-wal',
    'load_statistics.db-shm',
    'crashpadmetrics*',
    'topTraffic_*',
    '*.log.[0-9]',           # 轮转日志 relay.log.1 —— 后缀不是 .log，老规则漏掉
    '*.log.[0-9][0-9]',
    'log.txt',               # memory-tiers 的运行日志（扩展名 .txt，老规则也漏）
    'log.txt.[0-9]',
    'log.txt.[0-9][0-9]',
)

# 目录名（任意层级命中即跳过）
SNAPSHOT_JUNK_DIR_NAMES = (
    'relay-logs',
    'smartScreen'.lower(),   # 大小写不一，统一按小写比较
    'browsermetrics',
    'crashpad',
)

# 相对路径前缀（命中即跳过整棵子树）
SNAPSHOT_JUNK_REL_PREFIXES = (
    'nightly/relay-logs/',
)

# ── 浏览器 profile 内部：缓存/历史这些恢复出来也没用的东西 ──
#
# 为什么单独一组（2026-11-16 二次自检）：P3 当时把 `douyin-browser/cli-profile/`
# 只加进了**指纹**排除表，快照排除表里没有 —— 实测包内仍有 3.51 MB / 195 个文件
# 的 Chromium profile。整棵排除又不行：里面的 Cookies / Login Data / Local Storage
# 是那台浏览器的登录态，同机恢复时还有用。
#
# 折中：只排"缓存 + 历史 + 预览图"，并且**只在 `*-profile` 目录内生效**，
# 免得别的插件碰巧也叫 History / Web Data 被误伤。
SNAPSHOT_BROWSER_JUNK_DIR_NAMES = (
    'shadercache',
    'grshadercache',
    'cachestorage',          # Default/Service Worker/CacheStorage
    'shared dictionary',
    'nurturing',             # 该浏览器插件的运营数据
    'webstorage',
    'workspaces',
    'collections',
    'sessions_encrypted',
)

SNAPSHOT_BROWSER_JUNK_FILE_PATTERNS = (
    'history',
    'history-journal',
    'web data',
    'web data-journal',
    'dips',
    'dips-wal',
    'dips-shm',
    'top sites',
    'top sites-journal',
    'affiliation database',
    'network action predictor',
    'logout data',
    'edge profile picture.png',   # 1 MB，且已压缩 ⇒ 占包体最大单项
)


def _norm(name: str) -> str:
    return (name or '').strip().lower()


def is_fingerprint_ignorable_file(name: str) -> bool:
    """指纹比对时是否忽略这个文件名"""
    import fnmatch
    low = _norm(name)
    return any(fnmatch.fnmatch(low, p) for p in FINGERPRINT_EXTRA_FILE_PATTERNS)


def is_snapshot_junk_file(name: str) -> bool:
    """快照复制时是否跳过这个文件名"""
    import fnmatch
    low = _norm(name)
    return any(fnmatch.fnmatch(low, p) for p in SNAPSHOT_JUNK_FILE_PATTERNS)


def is_snapshot_junk_dir(name: str) -> bool:
    return _norm(name) in SNAPSHOT_JUNK_DIR_NAMES


def matches_rel_prefix(rel: str, prefixes) -> bool:
    """相对路径（POSIX，小写）是否命中前缀列表"""
    r = (rel or '').replace('\\', '/').lstrip('./').lower()
    return any(r.startswith(p) for p in prefixes)


def is_fingerprint_ignorable_rel(rel: str) -> bool:
    return matches_rel_prefix(rel, FINGERPRINT_EXTRA_REL_PREFIXES)


def is_snapshot_junk_rel(rel: str) -> bool:
    return matches_rel_prefix(rel, SNAPSHOT_JUNK_REL_PREFIXES)


def _is_browser_profile_segment(seg: str) -> bool:
    low = _norm(seg)
    return low.endswith('-profile') or low == 'cli-profile'


def is_snapshot_junk_in_browser_profile(rel: str, is_dir: bool = False) -> bool:
    """rel 是否处在浏览器 profile 内、且属于"恢复也没用"的那类

    只看 rel 的父级若有 `*-profile` 段才生效：`history` / `web data` 这种名字
    太普通，脱离 profile 语境去排会误伤别的插件。
    """
    import fnmatch
    parts = [p for p in (rel or '').replace('\\', '/').strip('/').split('/') if p]
    if len(parts) < 2:
        return False
    if not any(_is_browser_profile_segment(p) for p in parts[:-1]):
        return False
    name = _norm(parts[-1])
    if is_dir:
        return name in SNAPSHOT_BROWSER_JUNK_DIR_NAMES
    return any(fnmatch.fnmatch(name, p) for p in SNAPSHOT_BROWSER_JUNK_FILE_PATTERNS)
