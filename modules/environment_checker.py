"""
ZeroFall 环境校验模块
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import ZeroFallConfig
from logger import ZeroFallLogger
from utils import calculate_md5, calculate_directory_md5, get_process_info

# MD5 校验时跳过的目录（运行时/缓存/依赖，体量巨大且不属于"插件环境"）
# 不跳过会导致每次校验扫描 19000+ 文件，几十秒卡死 UI
# 核心配置文件模式：这些文件被改动视为严重异常
# 插件代码（pkgs/*.js 等）变化属正常演化（插件更新/自修改），不判为异常
MD5_CORE_PATTERNS = [
    'cordis.patch.yml',
    'cordis.yml',
    'prompt-inject.md',
    'tier-router-boot.json',
    'workspace.json',
    'preset.yml',
    'agent.cordis.yml',
]


def _is_core_file(path: str) -> bool:
    """判断路径是否属于核心配置（改动将判为异常）"""
    p = path.replace('\\', '/').lower()
    return any(c.lower() in p for c in MD5_CORE_PATTERNS)


# MD5 校验时跳过的目录（运行时/缓存/依赖，体量巨大且不属于"插件环境"）
MD5_EXCLUDE_DIRS = {
    'dsh-runtimes', 'primary-runtime', 'node_modules', 'cache',
    'code cache', 'gpucache', 'blob_storage', 'attachments',
    'backups', '__pycache__', '.git', '.svn', 'dist', 'build',
    'dawnwebgpucache', 'dawngraphitecache', 'grshadercache',
    '.cache', 'vendor',
    # DSH 新增的大目录（工具链/引擎/JDK，不属于插件环境）
    'rea-engines', 'rea-jdk', 'tools', 'skills',
    # 会话数据天然持续变化（每次对话都会改），不适合做"篡改"比对
    'sessions',
}

# MD5 校验时跳过的文件（日志/运行时状态等易变文件，
# 它们本来就会随时变化，若纳入比对会导致永远"不匹配"）
MD5_EXCLUDE_FILES = [
    '*.log', '*.jsonl', '*.tmp', '*.pyc', '*.pyo',
    'applied.json', 'update-status.json',
    '*.db-journal', '*.db-wal',
    '.DS_Store', 'Thumbs.db',
]

class CheckResult(Enum):
    """校验结果枚举"""
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"

@dataclass
class CheckItem:
    """校验项"""
    name: str
    result: CheckResult
    message: str
    details: str = ""

@dataclass
class EnvironmentReport:
    """环境校验报告"""
    overall_result: CheckResult = CheckResult.PASSED
    items: List[CheckItem] = field(default_factory=list)
    last_exit_crash: bool = False
    unbackup_changes: List[str] = field(default_factory=list)
    md5_mismatches: List[str] = field(default_factory=list)
    
    def add_item(self, item: CheckItem):
        self.items.append(item)
        if item.result == CheckResult.FAILED:
            self.overall_result = CheckResult.FAILED
        elif item.result == CheckResult.WARNING and self.overall_result != CheckResult.FAILED:
            self.overall_result = CheckResult.WARNING

class EnvironmentChecker:
    """环境校验器"""
    
    def __init__(self, config: ZeroFallConfig, snapshot_manager=None):
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.state_file = Path(config.snapshot_dir) / ".environment_state.json"
        self._state = self._load_state()
    
    def _load_state(self) -> Dict:
        """加载环境状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {
            'last_exit_code': 0,
            'last_exit_crash': False,
            # 崩溃事件序号：每崩一次 +1；crash_reported_seq 记录"已上报到第几个"。
            # 有了这两个数，同一个崩溃只会被上报一次，重启进程也不会重复。
            'crash_seq': 0,
            'crash_reported_seq': 0,
            'baseline_md5': {},
            'last_check_time': ''
        }
    
    def _save_state(self):
        """保存环境状态"""
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            ZeroFallLogger.error(f"保存环境状态失败: {e}")
    
    def quick_fingerprint(self):
        """插件/配置目录的廉价指纹：只用 stat，不读文件内容

        为什么要它：判断"环境有没有被改动"如果用整套 MD5，
        插件目录几百个文件、上百 MB，60 秒轮询一次会明显吃 IO 和 CPU。
        这里只取 (相对路径, 修改时间, 大小) 做哈希 —— 几百次 stat 通常
        几十毫秒，完全可以每分钟跑。

        排除规则和 MD5 校验保持一致：日志、会话、缓存这些天然会变的
        目录/文件不算"改动"，否则一分钟一个快照。

        Returns:
            str: 指纹哈希；目录都不存在时返回空串
        """
        import hashlib as _hl
        import fnmatch

        parts = []
        for key, cfg_field in (('plugins', 'deepseek_plugins_dir'),
                               ('config', 'deepseek_config_dir')):
            raw = getattr(self.config, cfg_field, '') or ''
            root = Path(raw)
            if not raw or not root.exists():
                parts.append('{}:missing'.format(key))
                continue
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    # 就地裁剪：不进入被排除的目录（省掉整棵子树的遍历）
                    dirnames[:] = [d for d in dirnames
                                   if d.lower() not in MD5_EXCLUDE_DIRS]
                    for fn in sorted(filenames):
                        low = fn.lower()
                        if any(fnmatch.fnmatch(low, pat.lower())
                               for pat in MD5_EXCLUDE_FILES):
                            continue
                        fp = Path(dirpath) / fn
                        try:
                            st = fp.stat()
                        except Exception:
                            continue
                        rel = str(fp.relative_to(root)).replace('\\', '/')
                        parts.append('{}|{}|{}|{}'.format(
                            key, rel, int(st.st_mtime), st.st_size))
            except Exception as e:
                parts.append('{}:error:{}'.format(key, e))

        if not parts:
            return ''
        return _hl.md5('\n'.join(parts).encode('utf-8', 'replace')).hexdigest()

    def update_baseline(self):
        """更新环境基线MD5"""
        baseline = {}
        
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        if plugins_dir.exists():
            baseline['plugins'] = calculate_directory_md5(str(plugins_dir), exclude_dirs=MD5_EXCLUDE_DIRS,
                                      exclude_files=MD5_EXCLUDE_FILES)
        
        config_dir = Path(self.config.deepseek_config_dir)
        if config_dir.exists():
            baseline['config'] = calculate_directory_md5(str(config_dir), exclude_dirs=MD5_EXCLUDE_DIRS,
                                      exclude_files=MD5_EXCLUDE_FILES)
        
        self._state['baseline_md5'] = baseline
        self._save_state()
        ZeroFallLogger.info("环境基线MD5已更新")
    
    def check_md5_consistency(self) -> CheckItem:
        """MD5一致性校验"""
        if not self.config.md5_check_enabled:
            return CheckItem("MD5一致性校验", CheckResult.PASSED, "校验已禁用")
        
        baseline = self._state.get('baseline_md5', {})
        if not baseline:
            # 首次使用没有基线属于正常状态，不应报警告
            return CheckItem("MD5一致性校验", CheckResult.PASSED,
                           "首次使用，创建快照后自动建立基线")
        
        modified = []    # 内容变化的文件（正常演化，仅记录）
        missing = []     # 基线里有但现在已经不存在的文件（真正的危险信号）
        added = []       # 新增文件（正常）
        
        # 校验插件目录
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        if plugins_dir.exists() and 'plugins' in baseline:
            current_md5 = calculate_directory_md5(str(plugins_dir), exclude_dirs=MD5_EXCLUDE_DIRS,
                                      exclude_files=MD5_EXCLUDE_FILES)
            baseline_md5 = baseline['plugins']
            
            for file_path, baseline_hash in baseline_md5.items():
                current_hash = current_md5.get(file_path, "")
                if not current_hash:
                    missing.append(f"plugins/{file_path}")
                elif current_hash != baseline_hash:
                    modified.append(f"plugins/{file_path}")
            
            for file_path in current_md5:
                if file_path not in baseline_md5:
                    added.append(f"plugins/{file_path}")
        
        # 校验配置目录
        config_dir = Path(self.config.deepseek_config_dir)
        if config_dir.exists() and 'config' in baseline:
            current_md5 = calculate_directory_md5(str(config_dir), exclude_dirs=MD5_EXCLUDE_DIRS,
                                      exclude_files=MD5_EXCLUDE_FILES)
            baseline_md5 = baseline['config']
            
            for file_path, baseline_hash in baseline_md5.items():
                current_hash = current_md5.get(file_path, "")
                if not current_hash:
                    missing.append(f"config/{file_path}")
                elif current_hash != baseline_hash:
                    modified.append(f"config/{file_path}")
        
        # 判定规则：
        #   核心文件丢失 → FAILED（真的出事了）
        #   其他文件丢失 → WARNING（插件被删，可能需要还原）
        #   内容变化/新增   → PASSED（插件更新、配置演进属正常）
        if missing:
            missing_core = [m for m in missing if _is_core_file(m)]
            if missing_core:
                return CheckItem(
                    "MD5一致性校验",
                    CheckResult.FAILED,
                    "核心文件丢失（{} 项）".format(len(missing_core)),
                    "\n".join(missing_core[:10])
                )
            return CheckItem(
                "MD5一致性校验",
                CheckResult.WARNING,
                "有 {} 个文件被删除".format(len(missing)),
                "\n".join(missing[:10])
            )
        
        if modified or added:
            parts = []
            if modified:
                parts.append("{} 处更新".format(len(modified)))
            if added:
                parts.append("{} 个新增".format(len(added)))
            return CheckItem(
                "MD5一致性校验",
                CheckResult.PASSED,
                "文件有变化（{}，正常演化）".format("，".join(parts)),
                "\n".join((modified + added)[:10])
            )
        
        return CheckItem("MD5一致性校验", CheckResult.PASSED, "所有文件MD5一致")
    
    def check_unbackup_changes(self) -> CheckItem:
        """检测未备份改动"""
        if not self.config.check_unbackup_changes:
            return CheckItem("未备份改动检测", CheckResult.PASSED, "检测已禁用")
        
        if not self.snapshot_manager:
            return CheckItem("未备份改动检测", CheckResult.PASSED, "快照管理器未就绪，跳过")
        
        baseline = self._state.get('baseline_md5', {})
        if not baseline:
            # 首次使用无基线属于正常
            return CheckItem("未备份改动检测", CheckResult.PASSED, "首次使用，暂无基线")
        
        changes = []
        
        # 对比当前插件目录与基线
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        if plugins_dir.exists() and 'plugins' in baseline:
            current_md5 = calculate_directory_md5(str(plugins_dir), exclude_dirs=MD5_EXCLUDE_DIRS,
                                      exclude_files=MD5_EXCLUDE_FILES)
            baseline_md5 = baseline['plugins']
            
            for file_path, baseline_hash in baseline_md5.items():
                if current_md5.get(file_path, "") != baseline_hash:
                    changes.append(f"plugins/{file_path}")
        
        if changes:
            # 内容变化属正常（每 5 分钟自动快照会很快覆盖），仅信息性展示
            # 只有"文件被删除"才需要提醒用户
            deleted = []
            for c in changes:
                rel = c.replace('plugins/', '', 1)
                if not (Path(self.config.deepseek_plugins_dir) / rel).exists():
                    deleted.append(c)
            
            if deleted:
                return CheckItem(
                    "未备份改动检测",
                    CheckResult.WARNING,
                    "有 {} 个文件被删除且未备份".format(len(deleted)),
                    "\n".join(deleted[:10])
                )
            return CheckItem(
                "未备份改动检测",
                CheckResult.PASSED,
                "有 {} 处文件变化（等待下次自动快照）".format(len(changes)),
                "\n".join(changes[:10])
            )
        
        return CheckItem("未备份改动检测", CheckResult.PASSED, "无未备份改动")
    
    def check_last_exit(self) -> CheckItem:
        """检测上次退出状态

        必须区分两种情况，否则会永久误报：
          · 上次真崩了、现在也没起来 —— 真问题，报 FAILED
          · 上次崩了、但现在 DeepSeek 跑得好好的 —— 只是历史记录，
            报 WARNING 即可，并把标记清掉。不然健康度永远红着，
            将来真正的新故障反而淹没在里面看不出来。

        触发本次修复的实例：状态文件残留 last_exit_crash=true、
        退出码 4294967295（0xFFFFFFFF，拿不到退出码时的占位值），
        而 DeepSeek 实际正在运行，于是每次校验都 failed。
        """
        if not self._state.get('last_exit_crash', False):
            return CheckItem("上次退出状态", CheckResult.PASSED, "上次正常退出")

        raw_code = self._state.get('last_exit_code', 'unknown')
        # 0xFFFFFFFF / -1 在 Windows 上表示"退出码未知"，不是确证崩溃
        code_unknown = raw_code in (4294967295, -1, 'unknown', None)
        code_text = "未知（系统未返回有效退出码）" if code_unknown else str(raw_code)

        running = False
        known = False          # 是否真的探到了进程状态
        try:
            exe_name = Path(str(getattr(self.config, 'deepseek_exe_path', ''))).name
            if exe_name:
                known = True
                running = bool(get_process_info(exe_name))
        except Exception:
            known = False

        if not known:
            # 连可执行文件名都拿不到就没法判断当前到底在不在跑。
            # 这种"不确定"不该报成 FAILED —— 那会变成永久红灯，
            # 而真正的问题反而看不出来。按历史记录降级成 WARNING。
            return CheckItem(
                "上次退出状态",
                CheckResult.WARNING,
                "上次退出异常，当前运行状态未知（未配置 DeepSeek 路径）",
                "退出码: {}".format(code_text)
            )

        if running:
            # 现在活着 → 历史标记已过期，清掉避免持续污染健康度
            try:
                self._state['last_exit_crash'] = False
                self._state['last_exit_code'] = 0
                self._save_state()
            except Exception:
                pass
            return CheckItem(
                "上次退出状态",
                CheckResult.WARNING,
                "上次退出异常，但 DeepSeek 当前正在运行",
                "退出码: {}（该标记已重置）".format(code_text)
            )

        return CheckItem(
            "上次退出状态",
            CheckResult.FAILED,
            "上次DeepSeek异常退出/崩溃，且当前未运行",
            "退出码: {}".format(code_text)
        )

    def check_session_integrity(self) -> CheckItem:
        """会话文件完整性校验"""
        if not self.config.session_integrity_check:
            return CheckItem("会话完整性校验", CheckResult.PASSED, "校验已禁用")
        
        session_dir = Path(self.config.deepseek_session_dir)
        if not session_dir.exists():
            return CheckItem("会话完整性校验", CheckResult.PASSED, "会话目录不存在")
        
        # 检查关键会话文件
        issues = []
        session_files = list(session_dir.rglob("*.json")) + list(session_dir.rglob("*.db"))
        
        for sf in session_files:
            try:
                # 检查文件是否可读取
                with open(sf, 'rb') as f:
                    f.read(min(1024, sf.stat().st_size))
            except Exception as e:
                issues.append(f"{sf.name}: {str(e)}")
        
        if issues:
            return CheckItem(
                "会话完整性校验",
                CheckResult.WARNING,
                f"发现 {len(issues)} 个问题文件",
                "\n".join(issues[:10])
            )
        
        return CheckItem("会话完整性校验", CheckResult.PASSED, "会话文件完整")
    
    def check_plugin_directory(self) -> CheckItem:
        """插件目录检查"""
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        
        if not plugins_dir.exists():
            return CheckItem("插件目录检查", CheckResult.WARNING, "插件目录不存在")
        
        plugin_count = len([d for d in plugins_dir.iterdir() if d.is_dir()])
        
        if plugin_count == 0:
            return CheckItem("插件目录检查", CheckResult.WARNING, "插件目录为空")
        
        return CheckItem("插件目录检查", CheckResult.PASSED, f"插件目录正常，共 {plugin_count} 个插件")
    
    def check_deepseek_executable(self) -> CheckItem:
        """DeepSeek可执行文件检查"""
        exe_path = Path(self.config.deepseek_exe_path)
        
        if not exe_path.exists():
            return CheckItem(
                "DeepSeek可执行文件",
                CheckResult.FAILED,
                f"文件不存在: {exe_path}"
            )
        
        if not os.access(str(exe_path), os.X_OK):
            return CheckItem(
                "DeepSeek可执行文件",
                CheckResult.FAILED,
                "文件无执行权限"
            )
        
        return CheckItem("DeepSeek可执行文件", CheckResult.PASSED, "文件正常")
    
    def run_full_check(self, use_cache: bool = False,
                       auto: bool = False) -> EnvironmentReport:
        """执行完整环境校验

        Args:
            use_cache: True 时若 60 秒内已校验过则复用结果
                       （UI 定时刷新必须用 True，否则会反复扫描大目录拖垮程序）
            auto:      True 表示程序自动触发（启动/定时刷新），受
                       「前置启动校验」开关约束；用户主动点「执行校验」
                       必须传 False —— 关掉开关不代表连手动检查都用不了。
        """
        import time as _time

        # 开关只拦自动流程，手动校验始终执行
        if auto and not getattr(self.config, 'pre_launch_check_enabled', True):
            cached = getattr(self, '_last_report', None)
            if cached is not None:
                return cached
            skipped = EnvironmentReport()
            skipped.add_item(CheckItem(
                "前置启动校验", CheckResult.PASSED,
                "已在设置中关闭，本轮跳过（可随时手动执行校验）"))
            return skipped

        # 缓存机制：避免 Dashboard 等定时刷新反复执行重扫描
        if use_cache:
            _last_time = getattr(self, '_last_check_ts', 0)
            _last_report = getattr(self, '_last_report', None)
            if _last_report is not None and (_time.time() - _last_time) < 60:
                return _last_report

        # 只有自动流程才叫"前置启动校验"，手动校验不该刷这条日志
        if auto:
            ZeroFallLogger.pre_launch_check_started()
        
        report = EnvironmentReport()
        report.last_exit_crash = self._state.get('last_exit_crash', False)
        
        # 执行各项检查
        checks = [
            self.check_deepseek_executable(),
            self.check_plugin_directory(),
            self.check_md5_consistency(),
            self.check_unbackup_changes(),
            self.check_last_exit(),
            self.check_session_integrity(),
        ]
        
        for item in checks:
            report.add_item(item)
        
        # 收集详细数据
        for item in report.items:
            if "MD5一致性" in item.name and item.result == CheckResult.FAILED:
                report.md5_mismatches = item.details.split("\n")
            if "未备份" in item.name and item.result == CheckResult.WARNING:
                report.unbackup_changes = item.details.split("\n")
        
        # 更新状态
        from datetime import datetime
        self._state['last_check_time'] = datetime.now().isoformat()
        self._save_state()
        
        # 写入缓存
        self._last_report = report
        self._last_check_ts = _time.time()
        
        status_str = report.overall_result.value
        ZeroFallLogger.pre_launch_check_completed(status_str)
        
        return report
    
    def record_exit(self, exit_code: int, crashed: bool):
        """记录DeepSeek退出状态"""
        self._state['last_exit_code'] = exit_code
        self._state['last_exit_crash'] = crashed
        if crashed:
            # 每次新崩溃领一个序号，供上报端做"只报一次"的去重依据
            try:
                self._state['crash_seq'] = int(self._state.get('crash_seq', 0) or 0) + 1
            except Exception:
                self._state['crash_seq'] = 1
        self._save_state()
        
        if crashed:
            ZeroFallLogger.deepseek_crashed(exit_code)
        else:
            ZeroFallLogger.info(f"DeepSeek正常退出 | 退出码: {exit_code}")

    def pending_crash_report(self):
        """取出一个尚未上报过的崩溃事件

        返回 (exit_code, seq)，没有待上报的则返回 None。

        为什么需要它：check_dsh_crash() 每轮心跳都会调一次，原先只要
        状态里躺着 last_exit_crash 就会上报一次，于是同一个崩溃会被反复
        报到主控，主控每次都去建会话修同一个问题。用序号去重后，
        一个崩溃事件只上报一次，重启进程也不会重复。
        """
        if not self._state.get('last_exit_crash', False):
            return None
        try:
            seq = int(self._state.get('crash_seq', 0) or 0)
            reported = int(self._state.get('crash_reported_seq', 0) or 0)
        except Exception:
            seq, reported = 0, 0

        if seq <= 0:
            # 老版本状态文件没有序号字段：当作一个待上报事件（seq=1），
            # 保证升级后仍会把历史崩溃补报一次，之后靠 reported 挡住。
            seq = 1
            self._state['crash_seq'] = 1

        if seq <= reported:
            return None
        return self._state.get('last_exit_code', 0), seq

    def mark_crash_reported(self, seq):
        """记账：第 seq 号崩溃已经上报成功。

        只在真正发出去之后调用 —— 发失败不记账，下一轮还能重试。
        """
        try:
            self._state['crash_reported_seq'] = int(seq or 0)
            self._save_state()
            return True
        except Exception as e:
            ZeroFallLogger.warning("记录崩溃上报进度失败: {}".format(e))
            return False

