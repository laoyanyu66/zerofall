"""
ZeroFall 快照管理核心模块
"""
import json
import os
import time
import shutil
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum
import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import ZeroFallConfig
from logger import ZeroFallLogger
from utils import (
    calculate_md5, calculate_directory_md5, get_directory_size,
    copy_directory, create_archive, extract_archive, compare_directories,
    generate_snapshot_id, format_timestamp
)

# 快照排除规则：这些目录/文件不纳入快照
# 目标：只保留"环境本身"（插件/配置/会话/预设），排除可重建的运行时、依赖、缓存、日志
SNAPSHOT_EXCLUDE_PATTERNS = [
    # 版本控制 / 编译产物
    ".git", ".svn", "__pycache__", "*.pyc", "*.pyo",
    # 浏览器/GPU 缓存
    "cache", "Cache", "Code Cache", "GPUCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "GrShaderCache", "blob_storage", ".cache",
    # DSH 运行时（258 MB，可按需重装，不应进每次快照）
    "dsh-runtimes",
    # 附件与历史备份（不属于环境）
    "attachments", "backups",
    # 日志类
    "*.log", "*.jsonl", "*.tmp", "*.db-journal",
    # 凭据（隐私，默认不备份；如需请单独开启 backup_keys）
    ".credentials.yaml", ".credentials.yaml.bak-*",
    # 系统垃圾
    ".DS_Store", "Thumbs.db",
]

# 额外规则：node_modules 一律排除
#   * profiles 下 → pnpm install 秒级重建
#   * pkgs 下     → 插件依赖，同样可重建
#     实测 dsh-tiangong/node_modules 单个已达 275 MB / 6900 文件，
#     纳入快照会把轻量快照从 26 MB 撑到 144 MB（且每 5 分钟重复一次）
# 注：恢复时 node_modules 属于 RESTORE_PROTECTED_NAMES，不会被覆盖，
#     所以排除它只影响"全新机器恢复"，而那本来就需要 pnpm install。
NODE_MODULES_EXCLUDE = True
PROFILES_NODE_MODULES_EXCLUDE = True   # 兼容旧引用

# 文件级排除：后缀
SNAPSHOT_EXCLUDE_EXTS = {".log", ".jsonl", ".tmp", ".pyc", ".pyo"}
# 文件级排除：名称（含前缀匹配）
SNAPSHOT_EXCLUDE_NAMES = {".credentials.yaml", ".DS_Store", "Thumbs.db"}


_SKIP_DIR_PREFIXES = (
    'sessions_backup', 'session-backup', 'session_backup',
    'backup_', 'backups_',
)


def _is_skippable_dir(name: str) -> bool:
    """按前缀判断是否该跳过（这类目录通常带时间戳，无法穷举）

    出过的问题：plugins/sessions_backup_20261109_182302 有 36 MB，
    名字带时间戳所以进不了固定集合，被一路打进快照。
    """
    low = name.lower()
    return any(low.startswith(p) for p in _SKIP_DIR_PREFIXES)


def copy_for_snapshot(src: Path, dst: Path, include_runtime: bool = False) -> dict:
    """按快照规则复制目录，返回统计信息
    
    Args:
        include_runtime: True 时把 primary-runtime / dsh-runtimes 也纳入
                         （用于"完整快照"，体积约 530 MB，适合离线归档）
    """
    stats = {"copied": 0, "skipped_dirs": 0, "skipped_files": 0, "bytes": 0}
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return stats

    # 完整快照只纳入 primary-runtime（DSH 程序侧运行时，约 257 MB，压缩后约 100 MB）；
    # dsh-runtimes（用户侧运行时，258 MB）可自动重装，始终排除，避免完整快照过大。
    global_dirs = {
        ".git", ".svn", "__pycache__", "cache", "Cache", "Code Cache",
        "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache", "GrShaderCache",
        "blob_storage", ".cache", "attachments", "backups",
        ".DS_Store", "Thumbs.db",
        "dsh-runtimes",          # 始终排除：可自动重装
        "rea-engines", "rea-jdk", "tools", "skills",
        "dist", "build",
        # 会话数据：天然每次对话都在变，且体量巨大（实测 plugins/sessions
        # 单个就有 ~59 MB，占了轻量快照 126 MB 里的 94 MB）。
        # 它不属于"插件环境"，回退会话也没意义（对话记录恢复不了上下文）。
        # 用户真想要时走 backup_session_data，那条路会打到独立的 sessions/ 目录。
        "sessions",
        "sessions_backup",       # 前缀匹配见下方 _is_skippable_dir
        "session-backup",
    }
    if not include_runtime:
        global_dirs.add("primary-runtime")   # 轻量快照也排除程序运行时

    for root, dirs, files in os.walk(src):
        root_p = Path(root)
        try:
            rel = root_p.relative_to(src)
        except ValueError:
            continue
        rel_parts = rel.parts
        in_profiles = bool(rel_parts) and rel_parts[0] == "profiles"

        # 过滤子目录
        kept = []
        for d in dirs:
            if d in global_dirs or _is_skippable_dir(d):
                stats["skipped_dirs"] += 1
                continue
            if NODE_MODULES_EXCLUDE and d == "node_modules":
                stats["skipped_dirs"] += 1
                continue
            kept.append(d)
        dirs[:] = kept

        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            if f in SNAPSHOT_EXCLUDE_NAMES or f.startswith(".credentials.yaml.bak"):
                stats["skipped_files"] += 1
                continue
            if Path(f).suffix.lower() in SNAPSHOT_EXCLUDE_EXTS:
                stats["skipped_files"] += 1
                continue
            try:
                shutil.copy2(root_p / f, target_dir / f)
                stats["copied"] += 1
                stats["bytes"] += (root_p / f).stat().st_size
            except Exception:
                stats["skipped_files"] += 1

    return stats

class SnapshotStatus(Enum):
    """快照状态枚举"""
    HEALTHY = "healthy"           # 健康快照
    CRASHED = "crashed"           # 崩溃快照
    INCOMPLETE = "incomplete"     # 不完整快照

class RestoreMode(Enum):
    """恢复模式枚举"""
    PLUGIN_ONLY = "plugin_only"           # 仅恢复插件环境
    FULL_RESTORE = "full_restore"         # 完整恢复（插件+会话）
    SESSION_ONLY = "session_only"         # 仅恢复会话

@dataclass
class PluginInfo:
    """插件信息"""
    name: str
    version: str
    enabled: bool
    path: str
    md5: str

@dataclass
class SnapshotData:
    """快照数据"""
    snapshot_id: str
    created_at: str
    status: SnapshotStatus
    plugin_count: int
    size_mb: float
    md5_checksum: str
    plugins: List[PluginInfo] = field(default_factory=list)
    notes: str = ""
    deepseek_version: str = ""
    has_session_data: bool = False
    has_keys: bool = False
    archive_path: str = ""
    is_full: bool = False       # True = 完整快照（含运行时），不参与自动清理
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        data = asdict(self)
        data['status'] = self.status.value
        data['plugins'] = [asdict(p) for p in self.plugins]
        return data
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SnapshotData':
        """从字典创建"""
        data['status'] = SnapshotStatus(data['status'])
        data['plugins'] = [PluginInfo(**p) for p in data.get('plugins', [])]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

class SnapshotManager:
    """快照管理器"""
    
    def __init__(self, config: ZeroFallConfig):
        self.config = config
        self.snapshot_dir = Path(config.snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.snapshot_dir / "snapshot_index.json"
        self.snapshots: Dict[str, SnapshotData] = {}
        # 本进程删除过的 ID：保存索引时不要把磁盘上的旧条目又合并回来
        self._deleted_ids = set()
        self._load_index()
    
    def _load_index(self):
        """加载快照索引"""
        if self.index_file.exists():
            try:
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.snapshots = {
                    k: SnapshotData.from_dict(v) 
                    for k, v in data.items()
                }
                ZeroFallLogger.info(f"加载快照索引成功，共 {len(self.snapshots)} 个快照")
            except Exception as e:
                ZeroFallLogger.error(f"加载快照索引失败: {e}")
                self.snapshots = {}
    
    def _save_index(self):
        """保存快照索引
        
        先合并磁盘上的其他条目：外部工具 / 另一个进程创建的快照不会被本进程覆盖丢失。
        """
        try:
            # 合并磁盘现有条目（跳过本进程已删除的）
            if self.index_file.exists():
                try:
                    raw = json.loads(self.index_file.read_text(encoding='utf-8'))
                    if isinstance(raw, dict):
                        for key, val in raw.items():
                            if key in self.snapshots or key in self._deleted_ids:
                                continue
                            try:
                                self.snapshots[key] = SnapshotData.from_dict(val)
                            except Exception:
                                pass
                except Exception:
                    pass
            
            data = {k: v.to_dict() for k, v in self.snapshots.items()}
            self.index_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
            )
        except Exception as e:
            ZeroFallLogger.error(f"保存快照索引失败: {e}")
    
    def prune_missing_entries(self, save: bool = True) -> List[str]:
        """清掉 archive_path 已不存在的索引条目

        典型成因：手动删了 zip、云同步误删、或增量恢复后残留。
        不清会让"溢出归档"每轮都重试一个不存在的文件，形成活锁
        （表现为每 5 分钟刷一条"云归档失败，保留本地"）。
        """
        removed = []
        for key, sd in list(self.snapshots.items()):
            try:
                ap = getattr(sd, 'archive_path', '')
                if ap and not Path(ap).exists():
                    removed.append(key)
                    del self.snapshots[key]
                    # 登记为已删除，否则 _save_index 的"从磁盘合并"会把它复活
                    self._deleted_ids.add(key)
            except Exception:
                continue
        if removed:
            ZeroFallLogger.warning(
                "索引清理：移除 {} 个文件已缺失的条目 {}".format(
                    len(removed), ', '.join(removed[:5])))
            if save:
                self._save_index()
        return removed

    def cleanup_staging_dirs(self, min_age_seconds: int = 3600) -> List[str]:
        """清掉快照目录里遗留的打包临时目录

        打包被中途打断（进程被杀/断电）会留下 snapshot_xxx 目录（无 .zip），
        实测单个可达 340 MB。只清理超过 min_age_seconds 的，避免误删进行中的。
        """
        removed = []
        try:
            base = Path(self.snapshot_dir)
            if not base.is_dir():
                return removed
            now = time.time()
            for d in base.iterdir():
                try:
                    if not d.is_dir():
                        continue
                    name = d.name
                    if not (name.startswith('snapshot_')
                            or name.startswith('_zerofall_pruned_')
                            or name.startswith('_update_tmp')):
                        continue
                    if now - d.stat().st_mtime < min_age_seconds:
                        continue
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(name)
                except Exception:
                    continue
            if removed:
                ZeroFallLogger.warning(
                    "临时目录清理：移除 {} 个遗留打包目录 {}".format(
                        len(removed), ', '.join(removed[:5])))
        except Exception as e:
            ZeroFallLogger.warning("临时目录清理失败: {}".format(e))
        return removed

    def cleanup_corrupt_archives(self, min_age_seconds: int = 3600) -> List[str]:
        """清掉写了一半的快照 zip（截断包）

        中断的快照写入会留下"文件头是 PK、但中央目录缺失"的坏包，
        ZipFile 打开即 BadZipFile。这类文件对恢复毫无价值，且白占几十上百 MB。

        安全条件（三条同时满足才删）：
          1. 不在索引里
          2. 修改时间超过 min_age_seconds（避免误删正在写的）
          3. 确实读不出中央目录
        """
        removed = []
        try:
            base = Path(self.snapshot_dir)
            if not base.is_dir():
                return removed
            now = time.time()
            for p in sorted(base.glob("snapshot_*.zip")):
                sid = p.stem
                if sid in self.snapshots:
                    continue
                try:
                    st = p.stat()
                    if now - st.st_mtime < min_age_seconds:
                        continue
                    try:
                        with zipfile.ZipFile(p) as zf:
                            zf.namelist()          # 强制读中央目录
                        continue                    # 能读 → 是完整包，不动
                    except Exception:
                        pass                        # 读不出 → 截断包
                    p.unlink()
                    removed.append(sid)
                except Exception:
                    continue
            if removed:
                ZeroFallLogger.warning(
                    "损坏包清理：移除 {} 个截断快照 {}".format(
                        len(removed), ', '.join(removed[:5])))
        except Exception as e:
            ZeroFallLogger.warning("损坏包清理失败: {}".format(e))
        return removed

    def reconcile_with_disk(self, adopt_orphans: bool = True,
                            min_age_seconds: int = 120) -> dict:
        """让索引与磁盘上的 zip 对齐

        两个方向：
          * 索引有、磁盘无 → 删除条目（prune_missing_entries 已覆盖）
          * 磁盘有、索引无 → 孤儿 zip（索引曾丢失/被截断），按需重新登记

        孤儿 zip 不登记的话会永远占着磁盘、UI 里看不见、也永远不会被归档回收。

        is_full 用"zip 里有没有 primary-runtime"判定：
        轻量快照恒定排除 primary-runtime，完整快照包含它，这是精确判据。
        """
        result = {"pruned": [], "adopted": [], "skipped": []}
        result["pruned"] = self.prune_missing_entries(save=False)

        if not adopt_orphans:
            if result["pruned"]:
                self._save_index()
            return result

        try:
            base = Path(self.snapshot_dir)
            if not base.is_dir():
                if result["pruned"]:
                    self._save_index()
                return result
            now = time.time()
            for zf_path in sorted(base.glob("snapshot_*.zip")):
                sid = zf_path.stem
                if sid in self.snapshots or sid in self._deleted_ids:
                    continue
                try:
                    st = zf_path.stat()
                    if now - st.st_mtime < min_age_seconds:
                        result["skipped"].append(sid)      # 可能正在写
                        continue
                    is_full = False
                    try:
                        with zipfile.ZipFile(zf_path) as zf:
                            for n in zf.namelist():
                                # 归档内的路径形如 `config/primary-runtime/...`，
                                # 不是以 primary-runtime 开头。原判据用 startswith
                                # 永远匹配不到，会把孤儿完整快照误登记成轻量，
                                # 进而被当作轻量按 48 个上限清理掉。
                                if ('primary-runtime' in n.split('/')):
                                    is_full = True
                                    break
                    except Exception:
                        result["skipped"].append(sid)      # 坏包，不登记
                        continue
                    created = sid.replace('snapshot_', '')
                    try:
                        datetime.strptime(created, "%Y%m%d_%H%M%S")
                    except Exception:
                        created = time.strftime('%Y%m%d_%H%M%S',
                                                time.localtime(st.st_mtime))
                    self.snapshots[sid] = SnapshotData(
                        snapshot_id=sid,
                        created_at=created,
                        status=SnapshotStatus.HEALTHY,
                        plugin_count=0,
                        size_mb=round(st.st_size / (1024 * 1024), 2),
                        md5_checksum='',
                        notes='索引重建（磁盘孤儿）',
                        archive_path=str(zf_path),
                        is_full=is_full,
                    )
                    result["adopted"].append(sid)
                except Exception:
                    continue
        except Exception as e:
            ZeroFallLogger.warning("索引对账失败: {}".format(e))

        if result["pruned"] or result["adopted"]:
            ZeroFallLogger.warning(
                "索引对账：移除 {} 个失效条目、登记 {} 个孤儿快照 {}".format(
                    len(result["pruned"]), len(result["adopted"]),
                    ', '.join(result["adopted"][:5])))
            self._save_index()
        return result

    def reload_index(self) -> int:
        """从磁盘重新加载索引（合并新增项，刷新 UI 用）
        
        返回新增的条目数。
        """
        before = len(self.snapshots)
        if not self.index_file.exists():
            return 0
        try:
            raw = json.loads(self.index_file.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return 0
            for key, val in raw.items():
                if key in self.snapshots or key in self._deleted_ids:
                    continue
                try:
                    self.snapshots[key] = SnapshotData.from_dict(val)
                except Exception:
                    pass
            # 清理：磁盘上已不存在的条目（被外部删除）
            for key in list(self.snapshots.keys()):
                if key not in raw and key not in self._deleted_ids:
                    # 只有对应文件也不存在时才移除
                    sd = self.snapshots.get(key)
                    if sd and not Path(getattr(sd, 'archive_path', '')).exists():
                        del self.snapshots[key]
            added = len(self.snapshots) - before
            if added > 0:
                ZeroFallLogger.info(f"索引重载：新增 {added} 个快照，当前共 {len(self.snapshots)} 个")
            return added
        except Exception as e:
            ZeroFallLogger.error(f"重载索引失败: {e}")
            return 0
    
    def _collect_plugin_info(self) -> List[PluginInfo]:
        """收集插件信息"""
        plugins = []
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        
        if not plugins_dir.exists():
            return plugins
        
        for plugin_path in plugins_dir.iterdir():
            if plugin_path.is_dir():
                # 尝试读取插件配置
                config_file = plugin_path / "config.json"
                plugin_name = plugin_path.name
                plugin_version = "unknown"
                plugin_enabled = True
                
                if config_file.exists():
                    try:
                        with open(config_file, 'r', encoding='utf-8') as f:
                            config = json.load(f)
                        plugin_name = config.get('name', plugin_name)
                        plugin_version = config.get('version', 'unknown')
                        plugin_enabled = config.get('enabled', True)
                    except:
                        pass
                
                plugin_md5 = calculate_md5(str(plugin_path / "main.py")) if (plugin_path / "main.py").exists() else ""
                
                plugins.append(PluginInfo(
                    name=plugin_name,
                    version=plugin_version,
                    enabled=plugin_enabled,
                    path=str(plugin_path),
                    md5=plugin_md5
                ))
        
        return plugins
    
    def create_snapshot(self, notes: str = "", force_healthy: bool = False,
                        include_runtime: bool = False) -> Optional[SnapshotData]:
        """创建快照
        
        Args:
            include_runtime: True 时纳入运行时（完整快照，约 530 MB）
        """
        """创建快照"""
        try:
            snapshot_id = generate_snapshot_id()
            snapshot_path = self.snapshot_dir / snapshot_id
            snapshot_path.mkdir(parents=True, exist_ok=True)
            
            ZeroFallLogger.info(f"开始创建快照: {snapshot_id}")
            
            # 确定快照状态
            #
            # 判据不要用"创建时 DeepSeek 在不在跑" —— 那会把大量正常快照
            # 误判成崩溃：用户手动「快速快照」时没开 DeepSeek、环境校验失败
            # 导致它拒绝启动、或者用户就是想关掉它再打快照，都会命中。
            # 实测误判出来的 crashed 既不参与恢复链，还会占满
            # max_crashed_snapshots 的名额。
            #
            # 真正需要"标记这个点有问题"的只有崩溃现场，而那些都是
            # guardian / 崩溃处理逻辑显式创建、并在 notes 里写明"崩溃"的：
            #   guardian.py      notes=f"崩溃自动快照 (退出码: {exit_code})"
            #   main_window.py   notes="崩溃快照 (退出码 {})"
            # 所以这里以 notes 为准，其余一律 HEALTHY（内容本身是完整的）。
            if force_healthy:
                status = SnapshotStatus.HEALTHY
            elif '崩溃' in (notes or ''):
                status = SnapshotStatus.CRASHED
            else:
                status = SnapshotStatus.HEALTHY
            
            # 备份插件目录（排除 node_modules 等大目录，否则单次快照可达上百 MB）
            if self.config.backup_plugins:
                plugins_src = Path(self.config.deepseek_plugins_dir)
                if plugins_src.exists():
                    plugins_dst = snapshot_path / "plugins"
                    stats = copy_for_snapshot(plugins_src, plugins_dst, include_runtime=include_runtime)
                    ZeroFallLogger.debug(
                        "插件目录备份完成 | 复制 {} 文件 {:.2f} MB | 跳过 {} 目录 {} 文件".format(
                            stats["copied"], stats["bytes"] / (1024 * 1024),
                            stats["skipped_dirs"], stats["skipped_files"]
                        )
                    )
            
            # 备份插件配置（含 DSH runtime 关键文件：bin/cli/office-skills/versions.json）
            if self.config.backup_plugin_config:
                config_src = Path(self.config.deepseek_config_dir)
                if config_src.exists():
                    config_dst = snapshot_path / "config"
                    cstats = copy_for_snapshot(config_src, config_dst, include_runtime=include_runtime)
                    ZeroFallLogger.debug(
                        "配置/runtime 备份完成 | 复制 {} 文件 {:.2f} MB | 跳过 {} 目录".format(
                            cstats["copied"], cstats["bytes"] / (1024 * 1024),
                            cstats["skipped_dirs"]
                        )
                    )
            
            # 备份认证态（DSH 登录态：Cookies/Local Storage，崩溃自愈关键）
            try:
                auth_src = Path(getattr(self.config, "dsh_appdata_dir", "") or "")
                if auth_src.is_dir():
                    auth_dst = snapshot_path / "auth"
                    auth_dst.mkdir(parents=True, exist_ok=True)
                    # 关键认证文件（小，直接拷）
                    for fname in ("Local State", "Preferences"):
                        f = auth_src / fname
                        if f.exists():
                            shutil.copy2(str(f), str(auth_dst / fname))
                    # Network/Cookies（Chromium SQLite）
                    # Chromium 运行时以独占句柄持有 Cookies，实测 CreateFileW 带
                    # FILE_SHARE_READ|WRITE|DELETE 任意组合仍返回 error 32，普通进程
                    # 无法读取。这是 Windows 的锁语义，不是缺陷。
                    # 登录态主要存放在 Local Storage（Electron 把 token 放那儿），
                    # 所以跳过 Cookies 几乎无损失；用 DEBUG 记录，避免每次快照刷一条
                    # WARNING 把真正的错误淹没（实测一天 138 条）。
                    skipped_locked = []
                    cookies_dir = auth_src / "Network"
                    if cookies_dir.is_dir():
                        ck = cookies_dir / "Cookies"
                        if ck.exists():
                            (auth_dst / "Network").mkdir(exist_ok=True)
                            try:
                                shutil.copy2(str(ck), str(auth_dst / "Network" / "Cookies"))
                            except OSError as ck_err:
                                if getattr(ck_err, "winerror", None) == 32:
                                    skipped_locked.append("Cookies")
                                else:
                                    raise
                    # Local Storage / Session Storage（leveldb）
                    #
                    # leveldb 会持续轮转：写满的 .log 落成 .ldb、旧的 .ldb 被删、
                    # MANIFEST/CURRENT 被改写。复制过程中这些文件随时可能消失，
                    # 于是 copytree 抛 FileNotFoundError（WinError 2）：
                    #   ERROR 创建快照失败: [WinError 2] 系统找不到指定的文件。:
                    #   '...\\auth\\Local Storage\\leveldb\\000014.ldb'
                    # 这不是缺陷，是 Chromium 存储引擎的正常行为。
                    # 逐个目录单独兜住，让残余文件照样备份，不牵连整次快照。
                    for dname in ("Local Storage", "Session Storage"):
                        d = auth_src / dname
                        if d.is_dir():
                            try:
                                copy_directory(str(d), str(auth_dst / dname))
                            except FileNotFoundError as fnf_err:
                                ZeroFallLogger.debug(
                                    "{} 有文件在复制时被 leveldb 轮转删除，"
                                    "已复制其余部分: {}".format(dname, fnf_err))
                            except OSError as os_err:
                                ZeroFallLogger.debug(
                                    "{} 复制不完整（登录态主体已尽力保留）: {}".format(
                                        dname, os_err))
                    ZeroFallLogger.debug(
                        "认证态备份完成 -> {}{}".format(
                            auth_dst,
                            "（被独占跳过: {}）".format(", ".join(skipped_locked))
                            if skipped_locked else ""
                        )
                    )
            except Exception as e:
                ZeroFallLogger.warning("认证态备份失败（不影响主快照）: {}".format(e))
            
            # 备份会话数据
            if self.config.backup_session_data:
                session_src = Path(self.config.deepseek_session_dir)
                if session_src.exists():
                    session_dst = snapshot_path / "sessions"
                    copy_directory(str(session_src), str(session_dst))
                    ZeroFallLogger.debug("会话数据备份完成")
            
            # 收集插件信息
            plugins = self._collect_plugin_info()
            
            # 计算快照大小
            size_mb = get_directory_size(str(snapshot_path))
            
            # 创建压缩包
            archive_path = str(snapshot_path) + ".zip"
            create_archive(str(snapshot_path), archive_path)
            
            # 计算MD5
            md5_checksum = calculate_md5(archive_path)
            
            # 删除临时目录
            shutil.rmtree(snapshot_path)
            
            # 创建快照数据
            snapshot_data = SnapshotData(
                snapshot_id=snapshot_id,
                created_at=datetime.now().strftime("%Y%m%d_%H%M%S"),
                status=status,
                plugin_count=len(plugins),
                size_mb=size_mb,
                md5_checksum=md5_checksum,
                plugins=plugins,
                notes=notes,
                has_session_data=self.config.backup_session_data,
                has_keys=self.config.backup_keys,
                archive_path=archive_path,
                is_full=include_runtime
            )
            
            # 保存到索引
            self.snapshots[snapshot_id] = snapshot_data
            self._save_index()
            
            ZeroFallLogger.snapshot_created(snapshot_id, size_mb, len(plugins))
            
            # 自动清理老旧快照
            self._cleanup_old_snapshots()
            
            return snapshot_data
            
        except Exception as e:
            ZeroFallLogger.error(f"创建快照失败: {e}")
            return None
    
    # 恢复时不能清理的目录（运行时/缓存/日志/附件，不属于"环境状态"）
    RESTORE_PROTECTED_NAMES = {
        'dsh-runtimes', 'primary-runtime', 'rea-engines', 'rea-jdk',
        'tools', 'skills', 'cache', 'Cache', 'Code Cache', 'GPUCache',
        'logs', 'node_modules', '.cache', 'attachments', 'backups',
        '.DS_Store', 'Thumbs.db',
    }
    
    def _prune_extra_entries(self, snapshot_dir: Path, target_dir: Path) -> List[str]:
        """把 target 中"快照里没有"的顶层条目移到备份区，实现真正的回到快照状态
        
        这是"完美回退"的关键：覆盖式复制只会还原旧文件，
        不会删除快照之后新装的插件/配置，导致回退后仍然崩溃。
        """
        pruned = []
        try:
            if not snapshot_dir.exists() or not target_dir.exists():
                return pruned
            snap_names = {p.name for p in snapshot_dir.iterdir()}
            backup_dir = target_dir.parent / (
                "_zerofall_pruned_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            )
            for item in list(target_dir.iterdir()):
                if item.name in snap_names:
                    continue
                if item.name in self.RESTORE_PROTECTED_NAMES:
                    continue
                try:
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(backup_dir / item.name))
                    pruned.append(item.name)
                except Exception as e:
                    ZeroFallLogger.warning(
                        "清理多余条目失败 {}: {}".format(item.name, e)
                    )
            if pruned:
                ZeroFallLogger.info(
                    "已移走快照后新增的 {} 项（备份于 {}）: {}".format(
                        len(pruned), backup_dir.name, ", ".join(pruned[:8])
                    )
                )
        except Exception as e:
            ZeroFallLogger.error("清理多余条目异常: {}".format(e))
        return pruned
    
    def _restore_dir_clean(self, snapshot_dir: Path, target_dir: Path) -> int:
        """干净恢复：先删除目标中与快照同名的条目，再从快照重新复制
        
        确保插件目录内部"改造时新增的文件"也不会残留，
        例如你在 dsh-luopan 里新建的 bad.js 会被一并清掉。
        """
        removed = 0
        try:
            if not snapshot_dir.exists() or not target_dir.exists():
                return removed
            for item in snapshot_dir.iterdir():
                target_item = target_dir / item.name
                if not target_item.exists():
                    continue
                try:
                    if target_item.is_dir():
                        shutil.rmtree(target_item)
                    else:
                        target_item.unlink()
                    removed += 1
                except Exception as e:
                    ZeroFallLogger.warning(
                        "清理同名条目失败 {}: {}".format(item.name, e)
                    )
            copy_directory(str(snapshot_dir), str(target_dir))
        except Exception as e:
            ZeroFallLogger.error("干净恢复异常: {}".format(e))
        return removed
    
    def restore_snapshot(self, snapshot_id: str, mode: RestoreMode,
                          overlay: bool = False,
                          skip_pre_restore_backup: bool = False) -> bool:
        """恢复快照

        Args:
            overlay: True 时为"叠加模式"——只覆盖复制，不 prune 多余条目、不删同名文件，
                    用于在完整快照之上叠加后续轻量快照，保留完整快照里多出来的文件。
            skip_pre_restore_backup: True 时跳过"恢复前自动备份"（崩溃自愈路径已经单独留了崩溃快照）。
        """
        try:
            if snapshot_id not in self.snapshots:
                ZeroFallLogger.error(f"快照不存在: {snapshot_id}")
                return False
            
            snapshot_data = self.snapshots[snapshot_id]
            archive_path = snapshot_data.archive_path
            
            if not Path(archive_path).exists():
                ZeroFallLogger.error(f"快照文件不存在: {archive_path}")
                return False
            
            # 验证MD5
            current_md5 = calculate_md5(archive_path)
            if current_md5 != snapshot_data.md5_checksum:
                ZeroFallLogger.error(f"快照MD5校验失败，文件可能已损坏")
                return False
            
            # 磁盘空间检查：解压 + 备份当前环境，粗算需要压缩包的 3.5 倍
            try:
                zip_mb = Path(archive_path).stat().st_size / (1024 * 1024)
                need_mb = zip_mb * 3.5
                free_mb = shutil.disk_usage(str(Path(archive_path).parent)).free / (1024 * 1024)
                if free_mb < need_mb:
                    ZeroFallLogger.error(
                        "磁盘空间不足：恢复需要约 {:.0f} MB，剩余仅 {:.0f} MB".format(need_mb, free_mb)
                    )
                    return False
                ZeroFallLogger.info(
                    "磁盘空间检查通过：需要 {:.0f} MB / 剩余 {:.0f} MB".format(need_mb, free_mb)
                )
            except Exception as _e:
                ZeroFallLogger.warning("磁盘空间检查跳过: {}".format(_e))
            
            # 解压快照
            temp_dir = Path(archive_path).parent / f"temp_{snapshot_id}"
            extract_archive(archive_path, str(temp_dir))
            
            # 恢复前备份当前环境（自愈路径已有崩溃快照，可跳过避免重复堆积）
            backup_snapshot = None
            if not skip_pre_restore_backup:
                backup_snapshot = self.create_snapshot("恢复前自动备份", force_healthy=False)
            
            # 根据模式恢复
            if mode == RestoreMode.PLUGIN_ONLY or mode == RestoreMode.FULL_RESTORE:
                plugins_src = temp_dir / "plugins"
                if plugins_src.exists():
                    plugins_dst = Path(self.config.deepseek_plugins_dir)
                    plugins_dst.mkdir(parents=True, exist_ok=True)
                    if overlay:
                        # 叠加模式：只覆盖，不删任何东西（保留完整快照里多出来的运行时/插件）
                        copy_directory(str(plugins_src), str(plugins_dst))
                        ZeroFallLogger.debug("[叠加] 插件目录已覆盖到基线之上")
                    else:
                        # 1) 移走快照之后新增的条目（新装插件等）
                        self._prune_extra_entries(plugins_src, plugins_dst)
                        # 2) 干净恢复：同名条目先删后放，清除改造时的残留文件
                        cleaned = self._restore_dir_clean(plugins_src, plugins_dst)
                        ZeroFallLogger.debug(
                            "插件目录已干净恢复（清理 {} 个同名条目，运行时/缓存已保留）".format(cleaned)
                        )
                
                config_src = temp_dir / "config"
                if config_src.exists():
                    config_dst = Path(self.config.deepseek_config_dir)
                    config_dst.mkdir(parents=True, exist_ok=True)
                    if overlay:
                        copy_directory(str(config_src), str(config_dst))
                        ZeroFallLogger.debug("[叠加] 配置目录已覆盖到基线之上")
                    else:
                        self._prune_extra_entries(config_src, config_dst)
                        self._restore_dir_clean(config_src, config_dst)
                        ZeroFallLogger.debug("运行时关键文件已干净恢复")
            
            # 恢复认证态（Cookies/Local Storage）——每次恢复都覆盖，不区分 overlay
            try:
                auth_src_pkg = temp_dir / "auth"
                auth_dst_root = Path(getattr(self.config, "dsh_appdata_dir", "") or "")
                if auth_src_pkg.is_dir() and auth_dst_root:
                    auth_dst_root.mkdir(parents=True, exist_ok=True)
                    # 备份当前认证态（便于回滚）
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    cur_backup = auth_dst_root.parent / ("_dsh_auth_bak_" + ts)
                    cur_backup.mkdir(parents=True, exist_ok=True)
                    for fname in ("Local State", "Preferences"):
                        f = auth_dst_root / fname
                        if f.exists():
                            shutil.copy2(str(f), str(cur_backup / fname))
                    ZeroFallLogger.info("当前认证态已备份到 {}".format(cur_backup.name))
                    # 覆盖恢复
                    for fname in ("Local State", "Preferences"):
                        f = auth_src_pkg / fname
                        if f.exists():
                            shutil.copy2(str(f), str(auth_dst_root / fname))
                    ck_src = auth_src_pkg / "Network" / "Cookies"
                    if ck_src.exists():
                        (auth_dst_root / "Network").mkdir(exist_ok=True)
                        shutil.copy2(str(ck_src), str(auth_dst_root / "Network" / "Cookies"))
                    for dname in ("Local Storage", "Session Storage"):
                        d = auth_src_pkg / dname
                        if d.is_dir():
                            copy_directory(str(d), str(auth_dst_root / dname))
                    ZeroFallLogger.info("认证态已从快照恢复（Cookies/Local Storage）")
            except Exception as e:
                ZeroFallLogger.warning("认证态恢复失败（不影响插件恢复）: {}".format(e))
            
            if mode == RestoreMode.SESSION_ONLY or mode == RestoreMode.FULL_RESTORE:
                # 恢复会话数据
                if snapshot_data.has_session_data:
                    session_src = temp_dir / "sessions"
                    if session_src.exists():
                        session_dst = Path(self.config.deepseek_session_dir)
                        session_dst.mkdir(parents=True, exist_ok=True)
                        copy_directory(str(session_src), str(session_dst))
                        ZeroFallLogger.debug("会话数据已覆盖恢复")
            
            # 恢复后自动重建 profile 依赖（node_modules 已被排除，需 pnpm install 秒级重建）
            if mode in (RestoreMode.PLUGIN_ONLY, RestoreMode.FULL_RESTORE):
                self._ensure_profile_deps()
            
            # 清理临时目录
            shutil.rmtree(temp_dir)
            
            ZeroFallLogger.snapshot_restored(snapshot_id, mode.value)
            return True
            
        except Exception as e:
            ZeroFallLogger.error(f"恢复快照失败: {e}")
            return False
    
    def _ensure_profile_deps(self) -> bool:
        """恢复后确保 profile 依赖完整
        
        node_modules 未纳入快照（38 MB），但有 pnpm store 缓存时
        `pnpm install --offline` 只需 2-3 秒（硬链接，零下载）。
        """
        try:
            base = Path(self.config.deepseek_plugins_dir)
            profile_dir = base / "profiles" / "desktop"
            pkg = profile_dir / "package.json"
            nm = profile_dir / "node_modules"
            
            if not pkg.exists():
                return True      # 无 profile 依赖，跳过
            if nm.exists():
                return True      # 依赖已存在，无需重建
            
            # 使用 DSH 自带 node + pnpm，避免依赖系统环境
            rt = base / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies"
            node = rt / "node" / "bin" / "node.exe"
            pnpm = rt / "pnpm" / "bin" / "pnpm.cjs"
            
            if not node.exists() or not pnpm.exists():
                ZeroFallLogger.warning(
                    "[恢复] 未找到 DSH 运行时 node/pnpm，无法自动重建依赖"
                )
                return False
            
            ZeroFallLogger.info("[恢复] 正在重建 profile 依赖（pnpm install）...")
            import subprocess
            
            # 优先离线安装（有 store 缓存时 2-3 秒）；失败再尝试在线
            for args in (["--offline", "--ignore-scripts"], ["--ignore-scripts"]):
                try:
                    r = subprocess.run(
                        [str(node), str(pnpm).replace("\\", "/"), "install"] + args,
                        cwd=str(profile_dir),
                        capture_output=True, text=True, timeout=300,
                        encoding="utf-8", errors="replace"
                    )
                    if r.returncode == 0:
                        ZeroFallLogger.info(
                            "[恢复] profile 依赖重建成功（{}）".format(" ".join(args))
                        )
                        return True
                    ZeroFallLogger.warning(
                        "[恢复] pnpm install {} 失败: {}".format(
                            " ".join(args), (r.stderr or r.stdout or "")[-300:]
                        )
                    )
                except Exception as e:
                    ZeroFallLogger.warning("[恢复] pnpm install 异常: {}".format(e))
            
            ZeroFallLogger.error(
                "[恢复] 依赖重建失败，请手动在 profiles/desktop 目录执行 pnpm install"
            )
            return False
        except Exception as e:
            ZeroFallLogger.error("[恢复] 重建依赖异常: {}".format(e))
            return False
    
    def delete_snapshot(self, snapshot_id: str) -> bool:
        """删除快照"""
        try:
            if snapshot_id not in self.snapshots:
                return False
            
            snapshot_data = self.snapshots[snapshot_id]
            
            # 删除压缩包
            if Path(snapshot_data.archive_path).exists():
                os.remove(snapshot_data.archive_path)
            
            # 从索引中删除
            del self.snapshots[snapshot_id]
            self._deleted_ids.add(snapshot_id)
            self._save_index()
            
            ZeroFallLogger.info(f"快照已删除: {snapshot_id}")
            return True
            
        except Exception as e:
            ZeroFallLogger.error(f"删除快照失败: {e}")
            return False
    
    def _cleanup_old_snapshots(self):
        """清理老旧快照
        
        - 健康快照（轻量/完整）不在此删除，由 ZeroFall 生命周期编排
          「溢出 → 云归档 → 删本地」处理，否则会跳过云归档直接丢数据。
        - 崩溃快照：只保留最近 3 个且 7 天内，避免长期占用磁盘。
        """
        try:
            now = time.time()
            crashed = sorted(
                [(sid, d) for sid, d in self.snapshots.items()
                 if d.status == SnapshotStatus.CRASHED],
                key=lambda x: x[1].created_at
            )
            max_crashed = 3
            keep_days = 7
            
            for sid, data in crashed:
                # 超期直接删
                expired = False
                try:
                    ts = datetime.strptime(data.created_at, "%Y%m%d_%H%M%S").timestamp()
                    expired = (now - ts) > keep_days * 86400
                except Exception:
                    pass
                if expired:
                    self.delete_snapshot(sid)
                    ZeroFallLogger.info("清理超期崩溃快照: {}".format(sid))
            
            # 重新统计后按数量截断
            crashed = sorted(
                [(sid, d) for sid, d in self.snapshots.items()
                 if d.status == SnapshotStatus.CRASHED],
                key=lambda x: x[1].created_at
            )
            while len(crashed) > max_crashed:
                old_id, _ = crashed.pop(0)
                self.delete_snapshot(old_id)
                ZeroFallLogger.info("清理多余崩溃快照: {}".format(old_id))
        except Exception as e:
            ZeroFallLogger.error(f"清理快照失败: {e}")
    
    def get_snapshot_list(self) -> List[SnapshotData]:
        """获取快照列表"""
        return sorted(
            self.snapshots.values(),
            key=lambda x: x.created_at,
            reverse=True
        )
    
    def get_healthy_snapshots(self) -> List[SnapshotData]:
        """获取健康快照列表"""
        return [
            s for s in self.snapshots.values()
            if s.status == SnapshotStatus.HEALTHY
        ]
    
    def get_disk_stats(self) -> dict:
        """快照磁盘占用统计（真实文件大小，不是压缩前的目录大小）

        size_mb 字段记的是"压缩前目录有多大"，和磁盘上实际占的空间不是一回事。
        这里统计 zip 本体，用户看到的才是真实占用。
        """
        snap_dir = Path(self.config.snapshot_dir)

        def _real(snaps):
            total = 0.0
            for s in snaps:
                try:
                    total += Path(s.archive_path).stat().st_size / (1024 * 1024)
                except Exception:
                    pass
            return total

        lights = self.get_light_snapshots() or []
        fulls = self.get_full_snapshots() or []
        light_mb, full_mb = _real(lights), _real(fulls)

        # 覆盖时长用**实际时间跨度**算，而不是"个数 × 当前配置间隔"。
        # 原因：老快照可能是别的间隔产生的（比如从 5 分钟改到 10 分钟之后，
        # 库里的旧快照仍是 5 分钟一个），乘当前配置会算出一个不存在的时间。
        def _span_hours(snaps):
            stamps = []
            for s in snaps:
                try:
                    stamps.append(__import__('datetime').datetime.strptime(
                        s.created_at, '%Y%m%d_%H%M%S'))
                except Exception:
                    pass
            if len(stamps) < 2:
                return 0.0
            return (max(stamps) - min(stamps)).total_seconds() / 3600.0

        cover_l = _span_hours(lights)
        cover_f = _span_hours(fulls)

        # 目录里的杂项（索引、meta、临时残留）
        extra = 0.0
        try:
            for f in snap_dir.glob('*'):
                if f.is_file() and f.suffix.lower() not in ('.zip', '.zst', '.7z'):
                    extra += f.stat().st_size / (1024 * 1024)
        except Exception:
            pass

        try:
            free_gb = __import__('shutil').disk_usage(str(snap_dir)).free / (1024 ** 3)
        except Exception:
            free_gb = -1.0

        return {
            'light_count': len(lights), 'light_mb': light_mb, 'light_cover_h': cover_l,
            'full_count': len(fulls), 'full_mb': full_mb, 'full_cover_h': cover_f,
            'total_mb': light_mb + full_mb, 'extra_mb': extra,
            'free_gb': free_gb,
            'light_max': int(getattr(self.config, 'max_light_snapshots', 48) or 0),
            'full_max': int(getattr(self.config, 'max_full_snapshots', 12) or 0),
        }

    def get_restorable_ids(self) -> set:
        """返回当前真正能恢复的快照 ID

        轻量快照不是独立的 —— 恢复时要先回到它之前最近的完整快照，
        再把后续轻量依次叠加。所以如果某个轻量快照早于所有完整快照，
        它就缺基线，点了也恢复不了。
        """
        fulls = [f.created_at for f in (self.get_full_snapshots() or [])]
        out = set()
        for s in (self.get_snapshot_list() or []):
            if getattr(s, 'is_full', False):
                out.add(s.snapshot_id)
            elif any(ft <= s.created_at for ft in fulls):
                out.add(s.snapshot_id)
        return out

    def find_orphan_archives(self) -> list:
        """找出磁盘上有压缩包、但索引里没有记录的快照

        这类文件既不会出现在界面上，也不会被清理逻辑回收（清理遍历索引），
        属于纯占空间。正常为 0；创建快照中途失败时会留下。
        """
        snap_dir = Path(self.config.snapshot_dir)
        if not snap_dir.exists():
            return []
        known = set(self.snapshots.keys())
        orphans = []
        try:
            for f in snap_dir.glob('snapshot_*'):
                if f.suffix.lower() not in ('.zip', '.zst', '.7z'):
                    continue
                if f.stem not in known:
                    orphans.append(f)
        except Exception:
            pass
        return orphans

    def get_light_snapshots(self) -> List[SnapshotData]:
        """获取轻量快照（健康且不含运行时）"""
        return [
            s for s in self.snapshots.values()
            if s.status == SnapshotStatus.HEALTHY and not getattr(s, 'is_full', False)
        ]
    
    def get_full_snapshots(self) -> List[SnapshotData]:
        """获取完整快照（健康且含运行时）"""
        return [
            s for s in self.snapshots.values()
            if s.status == SnapshotStatus.HEALTHY and getattr(s, 'is_full', False)
        ]
    
    def get_overflow_snapshots(self):
        """返回超出本地保留上限的快照 (轻量溢出, 完整溢出, 崩溃溢出)

        每个列表按"最老优先"排序，供云同步归档。

        崩溃快照原先不参与任何溢出判定 —— get_light_snapshots() 和
        get_full_snapshots() 都硬编码了 status == HEALTHY，crashed 既不算
        轻量也不算完整，两个上限都管不到，等于永久占用本地磁盘。
        实测 3 个就吃掉 168 MB。

        更麻烦的是成因：快照状态只看"创建时 DeepSeek 在不在跑"
        （`HEALTHY if deepseek_proc else CRASHED`）。环境校验不通过时
        DeepSeek 会拒绝启动，那段时间每 10 分钟的静默快照全被判成 crashed，
        而且永远不清理 —— 磁盘会无限涨。

        所以给崩溃现场单独一条上限，超出部分同样先归档云端再删本地，
        本地只留最近 max_crashed_snapshots 个供事后分析。
        """
        max_light = int(getattr(self.config, 'max_light_snapshots', 10))
        max_full = int(getattr(self.config, 'max_full_snapshots', 3))
        max_crashed = int(getattr(self.config, 'max_crashed_snapshots', 3))

        lights = sorted(self.get_light_snapshots(), key=lambda s: s.created_at)
        fulls = sorted(self.get_full_snapshots(), key=lambda s: s.created_at)
        crashes = sorted(self.get_crashed_snapshots(), key=lambda s: s.created_at)

        # 上限为 0 表示这一类不保留（但不是"无限"，别用 or 吞掉 0）
        light_overflow = lights[:-max_light] if max_light > 0 and len(lights) > max_light else (
            lights if max_light == 0 else [])
        full_overflow = fulls[:-max_full] if max_full > 0 and len(fulls) > max_full else (
            fulls if max_full == 0 else [])
        crash_overflow = crashes[:-max_crashed] if max_crashed > 0 and len(crashes) > max_crashed else (
            crashes if max_crashed == 0 else [])

        return light_overflow, full_overflow, crash_overflow
    
    @staticmethod
    def snapshot_date(snapshot: 'SnapshotData') -> str:
        """从快照取日期（YYYYMMDD），created_at 格式为 YYYYMMDD_HHMMSS"""
        try:
            return snapshot.created_at.split('_')[0]
        except Exception:
            return ""
    
    def repair_misjudged_crashed(self, save: bool = True) -> List[str]:
        """修正历史误判：notes 里没有"崩溃"字样、却被标成 CRASHED 的快照

        旧判据是 `HEALTHY if deepseek_proc else CRASHED`，只要创建那一刻
        DeepSeek 没在跑就记成崩溃。结果：
          · 用户手动「快速快照」时没开 DeepSeek → 记成崩溃
          · 环境校验失败、DeepSeek 拒绝启动期间的定时静默快照 → 记成崩溃
        这些快照内容完全正常，只是标签错了。留着会占满
        max_crashed_snapshots 的名额，也让界面统计失真。

        只改标签，不动文件内容；真崩溃现场（notes 含"崩溃"）原样保留。
        """
        fixed = []
        for sid, snap in self.snapshots.items():
            if snap.status != SnapshotStatus.CRASHED:
                continue
            if '崩溃' in (getattr(snap, 'notes', '') or ''):
                continue
            snap.status = SnapshotStatus.HEALTHY
            fixed.append(sid)
        if fixed:
            if save:
                self._save_index()
            ZeroFallLogger.info(
                "修正 {} 个误判为崩溃的快照（notes 未标记崩溃）: {}".format(
                    len(fixed), ', '.join(sorted(fixed)[:5])))
        return fixed

    def get_crashed_snapshots(self) -> List[SnapshotData]:
        """获取崩溃快照列表"""
        return [
            s for s in self.snapshots.values()
            if s.status == SnapshotStatus.CRASHED
        ]

    def find_overlay_chain(self, target: SnapshotData) -> List[SnapshotData]:
        """崩溃自愈叠加链：返回 [基线完整快照, ...依次叠加到 target 的轻量快照]

        策略：
          - 若 target 本身是完整快照，链 = [target]
          - 若 target 是轻量快照，找它之前（含同刻）最近的完整快照 F，
            链 = [F, F 之后到 target 之间的所有健康轻量快照（按时间升序），target]
        """
        healthy = sorted(
            [s for s in self.snapshots.values() if s.status == SnapshotStatus.HEALTHY],
            key=lambda x: x.created_at
        )
        if not healthy:
            return []

        # 若 target 是完整快照，直接只恢复它
        if getattr(target, 'is_full', False):
            return [target]

        # target 是轻量：找它之前最近的完整快照
        fulls = [s for s in healthy if getattr(s, 'is_full', False)]
        baseline = None
        for f in fulls:
            if f.created_at <= target.created_at:
                baseline = f
            else:
                break
        if baseline is None:
            # 没有更早的完整快照做基线，就只能直接恢复 target（退化为普通恢复）
            return [target]

        # 收集 baseline 之后、target 及之前的所有健康轻量快照（升序）
        chain = [baseline]
        for s in healthy:
            if s.created_at <= baseline.created_at:
                continue
            if s.created_at > target.created_at:
                break
            chain.append(s)
        return chain
    
    def compare_snapshots(self, snapshot_id1: str, snapshot_id2: str) -> Dict:
        """比较两个快照的差异"""
        try:
            if snapshot_id1 not in self.snapshots or snapshot_id2 not in self.snapshots:
                return {}
            
            snapshot1 = self.snapshots[snapshot_id1]
            snapshot2 = self.snapshots[snapshot_id2]
            
            # 解压两个快照
            temp1 = Path(snapshot1.archive_path).parent / f"temp_compare_{snapshot_id1}"
            temp2 = Path(snapshot2.archive_path).parent / f"temp_compare_{snapshot_id2}"
            
            extract_archive(snapshot1.archive_path, str(temp1))
            extract_archive(snapshot2.archive_path, str(temp2))
            
            # 比较插件目录
            plugins1 = temp1 / "plugins"
            plugins2 = temp2 / "plugins"
            
            diff = {}
            if plugins1.exists() and plugins2.exists():
                diff['plugins'] = compare_directories(str(plugins1), str(plugins2))
            
            # 清理临时目录
            shutil.rmtree(temp1)
            shutil.rmtree(temp2)
            
            return diff
            
        except Exception as e:
            ZeroFallLogger.error(f"比较快照失败: {e}")
            return {}
    
    def export_snapshot(self, snapshot_id: str, output_path: str) -> bool:
        """导出快照"""
        try:
            if snapshot_id not in self.snapshots:
                return False
            
            snapshot_data = self.snapshots[snapshot_id]
            archive_path = snapshot_data.archive_path
            
            if not Path(archive_path).exists():
                return False
            
            # 复制快照文件和元数据
            shutil.copy2(archive_path, output_path)
            
            # 保存元数据
            metadata_path = output_path + ".meta.json"
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(snapshot_data.to_dict(), f, ensure_ascii=False, indent=2)
            
            ZeroFallLogger.info(f"快照导出成功: {output_path}")
            return True
            
        except Exception as e:
            ZeroFallLogger.error(f"导出快照失败: {e}")
            return False
    
    def import_snapshot(self, archive_path: str, metadata_path: str) -> Optional[SnapshotData]:
        """导入快照"""
        try:
            # 读取元数据
            with open(metadata_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            snapshot_data = SnapshotData.from_dict(data)
            
            # 复制快照文件到快照目录
            new_archive_path = str(self.snapshot_dir / Path(archive_path).name)
            shutil.copy2(archive_path, new_archive_path)
            snapshot_data.archive_path = new_archive_path
            
            # 添加到索引
            self.snapshots[snapshot_data.snapshot_id] = snapshot_data
            self._save_index()
            
            ZeroFallLogger.info(f"快照导入成功: {snapshot_data.snapshot_id}")
            return snapshot_data
            
        except Exception as e:
            ZeroFallLogger.error(f"导入快照失败: {e}")
            return None
