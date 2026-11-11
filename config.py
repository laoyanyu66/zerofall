"""
ZeroFall 配置管理模块
"""
import json
import os
import sys
import base64
import hashlib
import ctypes
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, asdict

@dataclass
class ZeroFallConfig:
    """ZeroFall配置类"""
    
    # DeepSeek配置
    deepseek_exe_path: str = ""
    deepseek_plugins_dir: str = ""
    deepseek_config_dir: str = ""
    deepseek_session_dir: str = ""
    dsh_appdata_dir: str = ""   # %APPDATA%\@deepseek-ai\dsh-desktop，认证态所在地
    
    # 快照配置
    snapshot_dir: str = ""                   # 空 = 由 detect_snapshot_dir() 探测
    auto_snapshot_before_change: bool = True  # 文件改动前自动快照
    # 插件/配置变更检测：每 N 秒比对一次目录指纹（只用 stat，很便宜），
    # 检测到改动就自动打一个快照。这样每个回退点都精确对应"某次改动之前"，
    # 比单纯加密定时快照有用得多 —— 毒改动一旦被拍进快照，后面所有快照都带毒。
    plugin_drift_snapshot: bool = True        # 检测到插件/配置变更时自动快照
    plugin_drift_check_seconds: int = 60      # 多久检查一次（秒）
    plugin_drift_settle_seconds: int = 20     # 最后一次变化后静置多久才拍（防抖）
    # 两次变更点快照之间的最短间隔（分钟）。有些文件是"程序在跑就会写"的
    # 运行时状态，指纹会持续变化；靠排除规则挡掉大部分，这条是兜底，
    # 避免排除规则漏了某个文件时快照暴涨。设 0 表示不设冷却。
    plugin_drift_cooldown_minutes: int = 5
    # 本地溢出归档检查周期（分钟）：超限快照先传云端再删本地，是磁盘唯一出口。
    # 原来是启动时 singleShot 一次，跑一上午就堆到 68 轻量 + 15 完整。
    # 注意 cloud_sync_interval_minutes 定义在下面（每小时的同步周期），不要重复定义，
    # dataclass 会静默用后一个覆盖前一个。
    cloud_overflow_check_minutes: int = 10
    auto_silent_snapshot: bool = True         # 后台定时静默快照
    silent_snapshot_interval_minutes: int = 10  # 静默快照间隔（分钟）
    
    # 快照生命周期策略
    max_light_snapshots: int = 48             # 本地最多保留轻量快照数（48 个 × 10 分钟 ≈ 覆盖 8 小时）
    max_full_snapshots: int = 12              # 本地最多保留完整快照数（12 个 × 30 分钟 ≈ 覆盖 6 小时，约 3.5 GB）
    # 崩溃快照保留数。这类快照不参与轻量/完整的恢复链，原来两个上限都管不到，
    # 等于永久占用本地磁盘（实测 3 个 168 MB）。而且只要环境校验不通过、
    # DeepSeek 拒绝启动，那段时间每个静默快照都会因为"创建时进程没在跑"
    # 被判成 crashed —— 不设上限会无限堆积。超出部分归档到云端再删本地。
    max_crashed_snapshots: int = 3
    # 完整快照的触发方式（两种，二选一）
    #   定时模式：full_snapshot_interval_minutes > 0 时按时间触发，
    #             锚点是"当天第一个完整快照的时间"，之后每 N 分钟一个。
    #             这是现在的主用方式 —— 按时间比按计数好理解，也更好预期。
    #   计数模式：full_snapshot_interval_minutes <= 0 时退回老逻辑，
    #             每积累 N 个轻量快照整一个完整快照（N 个 × 5 分钟 ≈ 同样时长）
    full_snapshot_interval_minutes: int = 30  # 完整快照间隔（分钟，0/负数=改用计数模式）
    full_snapshot_every_n_light: int = 6      # 计数模式：每积累 N 个轻量快照时整一个完整快照
    cloud_overflow_enabled: bool = True       # 本地快照溢出时云同步后删除
    daily_cloud_consolidate: bool = True      # 次日开机时对云端做按天收敛
    daily_full_snapshot: bool = True          # 每天第一次启动后自动创建一个完整快照
    
    # ══════════════════════════════════════════════════════════
    #  主控 / 子控 体系
    # ══════════════════════════════════════════════════════════
    # 部署形态：
    #   standalone —— 单体模式（开源默认）。纯本地守护，不连任何控制通道、
    #                 不上报任何数据、起不了主控 HTTP 服务。
    #   master     —— 主控模式。要有源码目录+API Key，能给子控生成修复包。
    #   agent      —— 子控模式。向主控上报错误/崩溃，接收更新包。
    #
    # 为什么要有 standalone：开源分发时，"作者可远程下发可执行代码"是一条
    # 会被安全审查盯上的通道，而且用户自己的修改会被更新包覆盖。
    # 所以默认形态必须是不联网的，主控能力留给自建场景显式开启。
    deployment_mode: str = "standalone"

    role: str = "master"                       # master=主控 / agent=子控
    control_channel_dir: str = ""            # 空 = 未启用（主控/子控共享目录，用户自己设）
    agent_id: str = ""                         # 子控标识（空则自动生成）
    master_source_dir: str = ""                # 主控源码目录（空则自动探测）
    acp_profile: str = "zf-acp"                # 自动修复用的 DSH ACP profile
    report_to_master: bool = True              # 是否向主控上报
    report_zf_error: bool = True               # 是否上报 ZeroFall 自身错误
    report_dsh_crash: bool = True              # 是否上报 DeepSeek 崩溃
    auto_apply_update: bool = True             # 子控收到更新包后自动应用
    auto_fix_enabled: bool = True              # 主控自动生成修复包
    master_self_update: bool = True            # 主控生成更新包后也更新自己
    control_poll_seconds: int = 60             # 主控/子控轮询间隔（秒）

    # ── HTTP 更新投递（官方 App 式：拉更新清单 → 校验 → 下载安装）──
    # 主控自己当更新服务器，子控只填一个地址，不需要第三方账号或共享网盘。
    http_server_enabled: bool = True           # 主控是否启动内建更新服务器
    http_port: int = 8765                      # 监听端口
    http_bind: str = "127.0.0.1"               # 监听地址（默认只听回环；子控在主控同网段才改 0.0.0.0）
    http_token: str = ""                       # 可选共享密钥（子控与主控填一样）
    master_url: str = ""                       # 子控填主控地址，如 http://192.168.1.10:8765
    # master_url 非空时子控优先走 HTTP；留空则退回共享目录方式
    
    # ── WebDAV 中转（跨网络，子控零安装）──
    # 主控在 CGNAT 后面时 HTTP 直连不可达、子控又不能装穿透工具，
    # 这时用双方都能出站访问的 WebDAV 当"中转柜"：
    #   主控 PUT 更新包上去，子控 GET 下来，全程只需出站 HTTPS。
    # 兼容坚果云 / Nextcloud / 群晖 NAS / 各类网盘的 WebDAV 接口。
    relay_enabled: bool = False                # 是否启用中继中转
    relay_kind: str = "webdav"                 # webdav / s3 / ftp
    relay_url: str = ""                        # 如 https://dav.jianguoyun.com/dav
    relay_user: str = ""                       # WebDAV 账号（多为邮箱）
    relay_password: str = ""                   # WebDAV 应用密码
    relay_root: str = "/zerofall-ctl"          # 中转目录（自动创建）
    # ── S3 兼容对象存储（R2 / B2 / MinIO / OSS / COS）──
    s3_endpoint: str = ""                      # 如 https://xxx.r2.cloudflarestorage.com
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "auto"                    # R2 填 auto；OSS/COS 填各自区域
    # ── FTP / FTPS ──
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_user: str = ""
    ftp_password: str = ""
    ftp_tls: bool = False                      # 显式 FTPS 勾上
    # ── 子控专用凭据（可选）──
    # 存储位置必须与主控相同，但账号可以拆开：子控只拿到自己的密钥，
    # 即便被翻看也改不了更新包（防伪造更新）。留空则与主控共用。
    relay_agent_user: str = ""
    relay_agent_password: str = ""
    s3_agent_access_key: str = ""
    s3_agent_secret_key: str = ""
    ftp_agent_user: str = ""
    ftp_agent_password: str = ""
    # 注意：主控和子控要填同一组 relay_* 配置（存储位置），才能看到彼此的文件
    
    # 备份内容配置
    backup_plugins: bool = True               # 备份插件目录
    backup_plugin_config: bool = True         # 备份插件配置
    backup_session_data: bool = True          # 备份会话数据（默认开启：崩溃回滚不带上对话等于没回滚）
    # 会话只在完整快照里带：轻量快照每 10 分钟一份，会话 30 MB 全塞进去会把轻量从 24 MB 抬到 54 MB。
    # 会话是"完整基线"级别的数据（每 30 分钟的完整快照带上即可），精细恢复点不需要它。
    sessions_only_in_full: bool = True
    backup_keys: bool = False                 # 备份密钥配置（默认关闭，隐私安全）
    
    # 百度网盘配置
    baidu_pan_enabled: bool = False
    baidu_pan_app_key: str = ""
    baidu_pan_secret_key: str = ""
    baidu_pan_access_token: str = ""
    baidu_pan_refresh_token: str = ""
    baidu_pan_backup_folder: str = "/ZeroFall_Backups"
    baidu_pan_encrypt_enabled: bool = True    # 加密后上传
    
    # 云端同步（同步盘方案：无需 API 授权，依赖百度网盘客户端自动上传）
    cloud_sync_enabled: bool = True
    cloud_sync_dir: str = ""                 # 空 = 未启用（同步盘目录，用户自己设）
    cloud_sync_interval_minutes: int = 60     # 每小时把最新健康快照同步到同步盘
    # 云端上传抽样：本地快照密（5 分钟/30 分钟一个），没必要全传上去。
    # 轻量：每小时 1 个即可（每 12 个轻量传 1 个）
    # 完整：每 4 个传 1 个（≈ 2 小时一个），关键节点够用且省带宽
    cloud_sync_light_stride: int = 12         # 轻量快照每 N 个上传 1 个
    cloud_sync_full_stride: int = 4           # 完整快照每 N 个上传 1 个
    # 云端历史保留期：超过这么多天的整组快照会被清掉，0 = 不限（会无限长）
    cloud_history_keep_days: int = 14
    # 云端体量上限（GB）：只按天数留会失控 —— 一次事故就能塞进 10 GB 并躺满两周。
    # 超过上限时从最旧的完整快照开始删，直到降到上限以下。0 = 不限。
    cloud_max_total_gb: float = 8.0         # 默认留两周
    cloud_sync_encrypt: bool = True           # 上传前 AES 加密（默认开启，恢复时用「导入加密快照」）
    
    # 前置校验配置
    pre_launch_check_enabled: bool = True     # 前置启动校验开关
    md5_check_enabled: bool = True            # MD5一致性校验
    cloud_consistency_check: bool = False     # 云端一致性校验
    session_integrity_check: bool = True      # 会话文件完整性校验
    check_unbackup_changes: bool = True       # 检测未备份改动
    
    # 环境保护配置
    plugin_dir_readonly_lock: bool = False    # 插件目录只读锁定
    crash_alert_enabled: bool = True          # 崩溃告警
    
    # 程序行为配置
    auto_exit_after_deepseek_close: bool = True  # DeepSeek关闭后ZeroFall自动退出
    # 点右上角 X 时的行为：
    #   ask      —— 每次弹窗让用户选（默认，首次使用最友好）
    #   minimize —— 直接最小化到托盘，继续守护（QQ/微信那种习惯）
    #   exit     —— 直接退出程序
    # 选过一次之后可以勾"记住"，写在这里
    close_action: str = "ask"
    auto_launch_deepseek: bool = True            # 自检全部通过后自动启动DeepSeek
    auto_rollback_on_crash: bool = True          # DeepSeek崩溃后自动回退最新健康快照并重启
    max_auto_rollback: int = 2                   # 连续自动回退多少次后转为人工确认（不再是硬上限）
    # 崩溃后恢复对话框的自动倒计时（秒）。默认 600 = 10 分钟：
    # 崩溃时用户可能不在电脑前，1 分钟太短容易"还没看清就自动恢复了"。
    # 设为 0 表示不限时，必须手动点「立即恢复」。
    crash_restore_countdown: int = 600
    run_as_admin: bool = True                    # 以管理员权限运行
    
    # 日志配置
    log_level: str = "INFO"
    log_max_size_mb: int = 10
    log_backup_count: int = 5
    
    # 界面主题
    theme: str = "dark"
    
    # 窗口状态（hex编码）
    window_geometry: str = ""
    window_state: str = ""
    
    # 开机自启
    auto_start_enabled: bool = True
    
    def __post_init__(self):
        """初始化默认路径

        原来这里用 Path.cwd() —— 从不同目录启动会把快照写进不同地方，
        同一台机器能长出好几个快照库。改用 detect_snapshot_dir()：
        优先找到已有快照目录，否则落在 exe 同级 snapshots。
        """
        if not self.snapshot_dir:
            self.snapshot_dir = self._probe_snapshot_dir()
        # 会话目录：空则跟着 DSH_HOME 走（2026-11-11 的"Path('')=cwd"事故之后，
        # 空值不再意味着"用当前目录"，而是"用 <DSH_HOME>/sessions"）
        if not self.deepseek_session_dir and self.deepseek_plugins_dir:
            cand = Path(self.deepseek_plugins_dir) / "sessions"
            if cand.is_dir():
                self.deepseek_session_dir = str(cand)

    @staticmethod
    def _probe_snapshot_dir() -> str:
        """探测快照目录

        顺序：已有的 <盘>:\\dsh备份 → exe 同级 snapshots

        为什么不复用 ConfigManager.detect_snapshot_dir：那个类在本类之后
        定义，__post_init__ 执行时还引用不到。这里的逻辑要和它保持一致，
        否则"配置丢了 → 找不到老快照库"这种事故会静默发生。
        """
        for drive in ('D:', 'E:', 'C:', 'F:'):
            try:
                root = Path(drive + '\\')
                if not root.exists():
                    continue
                candidate = root / 'dsh备份'
                if candidate.is_dir():
                    return str(candidate)
            except Exception:
                continue
        try:
            base = (Path(sys.executable).parent
                    if getattr(sys, 'frozen', False)
                    else Path(__file__).resolve().parent)
        except Exception:
            base = Path.cwd()
        return str(base / "snapshots")
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ZeroFallConfig':
        """从字典创建配置

        兼容迁移：deployment_mode 是后加的字段。老配置文件里没有它，
        但一定有 role（master/agent），而且那台机器可能真的在跑主控。
        所以不能简单地套用默认值 standalone —— 那会把已经在用的主控
        静默降级成单体模式，用户的主控/子控体系会突然不工作。
        规则：没有 deployment_mode 时，沿用 role 的值。
        """
        data = dict(data or {})
        if not data.get('deployment_mode'):
            legacy_role = str(data.get('role', '') or '').strip().lower()
            if legacy_role in ('master', 'agent'):
                data['deployment_mode'] = legacy_role
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: str = None):
        # 关键：打包后必须用 exe 所在目录的绝对路径，
        # 否则 Start-Process 启动时工作目录不确定，会导致读不到配置
        if config_path is None:
            if getattr(sys, 'frozen', False):
                config_path = str(Path(sys.executable).parent / "zerofall_config.json")
            else:
                config_path = str(Path(__file__).parent / "zerofall_config.json")
        self.config_path = Path(config_path)
        self.config: Optional[ZeroFallConfig] = None
        self._ensure_dirs()
        # 需要加密的敏感字段
        self._sensitive_fields = [
            "baidu_pan_secret_key",
            "baidu_pan_access_token",
            "baidu_pan_refresh_token",
        ]
        self._enc_marker = "ENC::"
    
    def _ensure_dirs(self):
        """确保必要的目录存在"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
    
    # ════════════════════════════════════════════════════════════════
    #  跨机自动探测：换电脑后无需手工填写路径
    # ════════════════════════════════════════════════════════════════
    
    @staticmethod
    def detect_deepseek_exe() -> str:
        """探测 DeepSeek Harness.exe 的常见安装位置"""
        candidates = []
        
        # 1) 程序文件目录 / 本地应用目录
        for env in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA', 'APPDATA'):
            base = os.environ.get(env)
            if not base:
                continue
            candidates.extend([
                Path(base) / 'DeepSeek' / 'DeepSeek Harness.exe',
                Path(base) / 'DeepSeek Harness' / 'DeepSeek Harness.exe',
                Path(base) / 'Programs' / 'DeepSeek' / 'DeepSeek Harness.exe',
                Path(base) / 'Programs' / 'deepseek-harness' / 'DeepSeek Harness.exe',
            ])
        
        # 2) 各盘符的常见位置
        for drive in ('C:', 'D:', 'E:', 'F:'):
            candidates.extend([
                Path(drive + '\\') / 'DeepSeek' / 'DeepSeek Harness.exe',
                Path(drive + '\\') / 'Program Files' / 'DeepSeek' / 'DeepSeek Harness.exe',
            ])
        
        # 3) 开始菜单快捷方式反查
        try:
            for root in (
                Path(os.environ.get('APPDATA', '')) / 'Microsoft/Windows/Start Menu/Programs',
                Path(os.environ.get('PROGRAMDATA', '')) / 'Microsoft/Windows/Start Menu/Programs',
                Path(os.environ.get('USERPROFILE', '')) / 'Desktop',
            ):
                if not root.exists():
                    continue
                for lnk in root.rglob('*DeepSeek*.lnk'):
                    try:
                        import win32com.client
                        shell = win32com.client.Dispatch("WScript.Shell")
                        target = shell.CreateShortCut(str(lnk)).TargetPath
                        if target and target.lower().endswith('.exe'):
                            candidates.append(Path(target))
                    except Exception:
                        pass
        except Exception:
            pass
        
        for p in candidates:
            try:
                if p.is_file() and 'deepseek' in p.name.lower():
                    return str(p)
            except Exception:
                continue
        return ''
    
    @staticmethod
    def detect_plugins_dir(deepseek_exe: str) -> str:
        """探测 .dsh 插件目录（优先用户目录）"""
        home = Path(os.environ.get('USERPROFILE', ''))
        candidates = [home / '.dsh']
        # 部分部署把 .dsh 放在程序目录旁
        if deepseek_exe:
            exe_dir = Path(deepseek_exe).parent
            candidates.extend([
                exe_dir / '.dsh',
                exe_dir / 'resources' / '.dsh',
            ])
        for p in candidates:
            try:
                if p.is_dir():
                    return str(p)
            except Exception:
                continue
        # 都找不到：返回用户目录下的（首次运行会创建）
        return str(home / '.dsh')
    
    @staticmethod
    def detect_runtime_dir(deepseek_exe: str) -> str:
        """探测 DSH runtime 目录（用于备份 office-skills 等关键文件）"""
        if not deepseek_exe:
            return ''
        exe_dir = Path(deepseek_exe).parent
        candidates = [
            exe_dir / 'resources' / 'runtime',
            exe_dir / 'runtime',
            exe_dir.parent / 'resources' / 'runtime',
        ]
        for p in candidates:
            try:
                if p.is_dir():
                    return str(p)
            except Exception:
                continue
        return str(exe_dir / 'resources' / 'runtime')
    
    @staticmethod
    def detect_dsh_appdata_dir() -> str:
        """DSH 在 %APPDATA% 下的数据目录（认证态 Cookies/Local Storage）"""
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            return ""
        return str(Path(appdata) / "@deepseek-ai" / "dsh-desktop")
    
    @staticmethod
    def detect_snapshot_dir() -> str:
        """探测快照目录：优先 D:\\dsh备份，否则回落到 exe 同级 snapshots"""
        for drive in ('D:', 'E:', 'C:'):
            p = Path(drive + '\\') / 'dsh备份'
            try:
                parent = Path(drive + '\\')
                if parent.exists() and parent.is_dir():
                    # 只要求盘符存在即可创建
                    if p.exists():
                        return str(p)
            except Exception:
                continue
        # 默认：D 盘存在就用 D，否则用 exe 同级
        if Path('D:\\').exists():
            return 'D:\\dsh备份'
        base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        return str(base / 'snapshots')
    
    @staticmethod
    def detect_cloud_dir() -> str:
        """探测百度网盘同步盘目录"""
        candidates = []
        for drive in ('E:', 'D:', 'C:', 'F:'):
            candidates.append(Path(drive + '\\') / 'BaiduSyncdisk' / 'zerofall-snapshots')
        for p in candidates:
            try:
                if p.parent.exists():
                    return str(p)
            except Exception:
                continue
        return ''
    
    def auto_detect_paths(self) -> Dict[str, str]:
        """探测本机实际路径，返回建议值（不写入配置）"""
        exe = self.detect_deepseek_exe()
        return {
            'deepseek_exe_path': exe,
            'deepseek_plugins_dir': self.detect_plugins_dir(exe),
            'deepseek_config_dir': self.detect_runtime_dir(exe),
            'dsh_appdata_dir': self.detect_dsh_appdata_dir(),
            'snapshot_dir': self.detect_snapshot_dir(),
            'cloud_sync_dir': self.detect_cloud_dir(),
        }
    
    @staticmethod
    def _drive_available(path: str) -> bool:
        """判断路径所在的盘符/根是否存在（目录本身可以不存在，会自动创建）"""
        try:
            if not path:
                return False
            p = Path(path)
            anchor = p.anchor              # 如 'Z:\\' 或 '\\\\server\\share\\'
            if not anchor:
                return True                # 相对路径：按当前盘处理
            return Path(anchor).exists()
        except Exception:
            return False
    
    def needs_path_setup(self, cfg: 'ZeroFallConfig') -> bool:
        """判断当前配置的路径是否在本机失效（换机器/移动目录后为 True）"""
        try:
            if not cfg.deepseek_exe_path or not Path(cfg.deepseek_exe_path).is_file():
                return True
            if not cfg.deepseek_plugins_dir or not Path(cfg.deepseek_plugins_dir).is_dir():
                return True
            # dsh_appdata_dir 允许目录不存在（首次运行会创建），但为空时要补探测
            if not getattr(cfg, 'dsh_appdata_dir', ''):
                return True
            # 快照/云目录：盘符必须存在，否则 mkdir 会失败
            if not self._drive_available(cfg.snapshot_dir):
                return True
            if getattr(cfg, 'cloud_sync_dir', '') and not self._drive_available(cfg.cloud_sync_dir):
                return True
        except Exception:
            return True
        return False

    
    def _derive_key(self) -> bytes:
        """基于机器 ID (Volume Serial Number) + 固定 salt 派生 XOR 密钥"""
        salt = b"ZeroFall_v1_Salt_2024"
        try:
            kernel32 = ctypes.windll.kernel32
            volume_serial = ctypes.c_ulong()
            max_component_len = ctypes.c_ulong()
            fs_flags = ctypes.c_ulong()
            ret = kernel32.GetVolumeInformationW(
                "C:\\", None, 0,
                ctypes.byref(volume_serial),
                ctypes.byref(max_component_len),
                ctypes.byref(fs_flags),
                None, 0
            )
            if ret:
                machine_id = str(volume_serial.value).encode("utf-8")
            else:
                machine_id = b"fallback_zerofall_machine_id"
        except Exception:
            machine_id = b"fallback_zerofall_machine_id"
        
        key_material = machine_id + salt
        # 派生 32 字节密钥
        return hashlib.sha256(key_material).digest()
    
    def _encrypt_value(self, plain: str) -> str:
        """XOR + base64 加密"""
        if not plain:
            return ""
        key = self._derive_key()
        plain_bytes = plain.encode("utf-8")
        encrypted = bytes([plain_bytes[i] ^ key[i % len(key)] for i in range(len(plain_bytes))])
        return self._enc_marker + base64.b64encode(encrypted).decode("utf-8")
    
    def _decrypt_value(self, cipher: str) -> str:
        """解密（识别 ENC:: 前缀）"""
        if not cipher:
            return ""
        if not cipher.startswith(self._enc_marker):
            # 未加密的旧数据，直接返回
            return cipher
        try:
            key = self._derive_key()
            payload = cipher[len(self._enc_marker):]
            encrypted_bytes = base64.b64decode(payload.encode("utf-8"))
            decrypted = bytes([encrypted_bytes[i] ^ key[i % len(key)] for i in range(len(encrypted_bytes))])
            return decrypted.decode("utf-8")
        except Exception:
            # 解密失败返回空字符串
            return ""
    
    def load(self) -> ZeroFallConfig:
        """加载配置（换机器时自动探测并修正失效路径）"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 对敏感字段进行解密（仅在标记为已加密时）
                is_encrypted = data.get("config_encrypted", False)
                if is_encrypted:
                    for field in self._sensitive_fields:
                        if field in data and data[field]:
                            data[field] = self._decrypt_value(data[field])
                
                self.config = ZeroFallConfig.from_dict(data)
            except Exception as e:
                print(f"加载配置失败，使用默认配置: {e}")
                self.config = ZeroFallConfig()
        else:
            self.config = ZeroFallConfig()
            self.save()
        
        # 跨机自适应：路径在本机不存在时，自动探测并写回
        try:
            if self.needs_path_setup(self.config):
                detected = self.auto_detect_paths()
                changed = []
                for field, value in detected.items():
                    if not value:
                        continue
                    current = getattr(self.config, field, '')
                    # 只在"当前值失效"或"为空"时覆盖，避免误改用户手填的有效路径
                    valid = False
                    try:
                        p = Path(current) if current else None
                        if p is not None:
                            if field == 'deepseek_exe_path':
                                valid = p.is_file()
                            elif field in ('deepseek_plugins_dir', 'deepseek_config_dir'):
                                # 这两个目录必须真的存在：换机器后旧用户名
                                # (C:\Users\<旧用户名>\.dsh) 或旧盘符 (Z:\...\runtime)
                                # 只剩个非空字符串，光判断非空会一直留着坏路径，
                                # 于是探测到的正确值永远写不进去。
                                valid = p.is_dir()
                            elif field == 'dsh_appdata_dir':
                                # appdata 目录首次运行会自动创建，但盘符得可用
                                valid = bool(current) and self._drive_available(current)
                            else:
                                # 快照/云目录：目录可不存在（会自动创建），但盘符必须可用
                                valid = self._drive_available(current)
                    except Exception:
                        valid = False
                    if not valid:
                        setattr(self.config, field, value)
                        changed.append('{} -> {}'.format(field, value))
                if changed:
                    self.save()
                    print("[配置自适应] 检测到路径在本机失效，已自动修正:")
                    for c in changed:
                        print("  ", c)
        except Exception as e:
            print("[配置自适应] 跳过: {}".format(e))
        
        return self.config
    
    def save(self) -> bool:
        """保存配置"""
        if self.config is None:
            return False
        
        try:
            data = self.config.to_dict()
            
            # 对敏感字段进行加密
            for field in self._sensitive_fields:
                if field in data and data[field]:
                    data[field] = self._encrypt_value(data[field])
            
            # 标记配置已加密
            data["config_encrypted"] = True
            
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存配置失败: {e}")
            return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        if self.config is None:
            self.load()
        return getattr(self.config, key, default)
    
    def set(self, key: str, value: Any) -> bool:
        """设置配置项"""
        if self.config is None:
            self.load()
        
        if hasattr(self.config, key):
            setattr(self.config, key, value)
            return self.save()
        return False
    
    def update(self, updates: Dict[str, Any]) -> bool:
        """批量更新配置"""
        if self.config is None:
            self.load()
        
        for key, value in updates.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        
        return self.save()
    
    def export_settings(self, output_path: str) -> bool:
        """导出当前配置到文件"""
        try:
            import json, shutil
            # 复制当前配置文件
            if self.config_path.exists():
                shutil.copy2(str(self.config_path), output_path)
                return True
            return False
        except Exception as e:
            print(f"导出配置失败: {e}")
            return False
    
    def import_settings(self, input_path: str) -> bool:
        """从文件导入配置"""
        try:
            import json, shutil
            if not Path(input_path).exists():
                return False
            # 先备份当前配置
            if self.config_path.exists():
                backup = str(self.config_path) + ".bak"
                shutil.copy2(str(self.config_path), backup)
            # 导入新配置
            shutil.copy2(input_path, str(self.config_path))
            # 重新加载
            self.load()
            return True
        except Exception as e:
            print(f"导入配置失败: {e}")
            return False
