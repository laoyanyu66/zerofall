"""
ZeroFall 日志模块
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime


def _is_test_run() -> bool:
    """判断当前进程是不是在跑 tests/ 下的测试脚本

    为什么要单独判：测试会 import 主程序模块并触发日志初始化，而日志目录
    按 cwd 计算，测试又都把 cwd 切到项目根 —— 于是 run 一遍测试就往生产日志
    logs/zerofall_YYYYMMDD.log 里灌进一堆假故障（假退出码 1/2/3/9、
    假 sha256 不匹配、假"无可用快照"）。真出问题时翻当天日志，噪音比线索多。
    测试进程单独落 logs/_tests/，生产日志只剩真事件。
    """
    try:
        argv0 = Path(sys.argv[0]).resolve()
    except Exception:
        return False
    if argv0.stem.lower().startswith('test_'):
        return True
    return len(argv0.parts) >= 2 and argv0.parts[-2].lower() == 'tests'


class ZeroFallLogger:
    """ZeroFall日志管理器"""
    
    _instance = None
    _logger = None
    _file_handler = None
    _console_handler = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, log_dir: str = "logs", log_level: str = "INFO", 
                 max_size_mb: int = 10, backup_count: int = 5):
        if ZeroFallLogger._logger is not None:
            return
        
        # 使用绝对路径：PyInstaller 打包后用 exe 所在目录，否则用 cwd
        if getattr(sys, 'frozen', False):
            base_dir = Path(sys.executable).parent
        else:
            base_dir = Path.cwd()
        # 日志目录优先级：ZF_LOG_DIR 环境变量 > 测试进程单独目录 > logs
        env_dir = os.environ.get('ZF_LOG_DIR', '').strip()
        if env_dir:
            log_dir = env_dir
        elif not getattr(sys, 'frozen', False) and _is_test_run():
            log_dir = 'logs/_tests'
        self.log_dir = (base_dir / log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ZeroFallLogger._log_dir = self.log_dir      # 供 cleanup_old_logs 使用
        
        # 创建日志器（必须用类名赋值，避免创建实例属性遮蔽类属性）
        ZeroFallLogger._logger = logging.getLogger("ZeroFall")
        ZeroFallLogger._logger.setLevel(getattr(logging, log_level.upper()))
        
        # 日志格式
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 文件处理器（轮转）—— 使用绝对路径
        log_file = (self.log_dir / f"zerofall_{datetime.now().strftime('%Y%m%d')}.log").resolve()
        ZeroFallLogger._file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding='utf-8'
        )
        ZeroFallLogger._file_handler.setFormatter(formatter)
        
        # 控制台处理器
        ZeroFallLogger._console_handler = logging.StreamHandler()
        ZeroFallLogger._console_handler.setFormatter(formatter)
        
        # 添加处理器
        ZeroFallLogger._logger.addHandler(ZeroFallLogger._file_handler)
        ZeroFallLogger._logger.addHandler(ZeroFallLogger._console_handler)
    
    @classmethod
    def cleanup_old_logs(cls, log_dir=None, keep_days: int = 5) -> int:
        """删除超过保留天数的日志文件

        为什么需要：日志按天分文件（zerofall_YYYYMMDD.log），但从来没人清理。
        一年下来会积累 300 多个文件、上百 MB —— 而 log_backup_count 这个
        配置项一直存在却从没被读取过。这里把它真正落地。

        keep_days <= 0 表示不清理。
        返回删除的文件数。
        """
        if keep_days is None or int(keep_days) <= 0:
            return 0
        import re as _re
        from datetime import datetime as _dt, timedelta as _td
        root = Path(log_dir) if log_dir else (
            cls._log_dir if getattr(cls, '_log_dir', None) else Path('logs'))
        root = Path(root)
        if not root.exists():
            return 0
        cutoff = _dt.now() - _td(days=int(keep_days))
        removed = 0
        pat = _re.compile(r'zerofall_(\d{8})\.log$')
        try:
            for f in root.iterdir():
                if not f.is_file():
                    continue
                m = pat.search(f.name)
                if not m:
                    continue
                try:
                    day = _dt.strptime(m.group(1), '%Y%m%d')
                except Exception:
                    continue
                if day < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            return removed
        return removed

    @classmethod
    def get_logger(cls) -> logging.Logger:
        """获取日志器实例"""
        if cls._logger is None:
            cls()
        return cls._logger
    
    @classmethod
    def _flush(cls):
        """内部：flush 所有 handler"""
        if cls._file_handler:
            cls._file_handler.flush()
        if cls._console_handler:
            cls._console_handler.flush()
    
    @classmethod
    def flush_all(cls):
        """公开：flush 所有 handler（供外部调用）"""
        cls._flush()
    
    @classmethod
    def info(cls, message: str):
        """记录信息日志"""
        cls.get_logger().info(message)
        cls._flush()
    
    @classmethod
    def warning(cls, message: str):
        """记录警告日志"""
        cls.get_logger().warning(message)
        cls._flush()
    
    @classmethod
    def error(cls, message: str):
        """记录错误日志"""
        cls.get_logger().error(message)
        cls._flush()
    
    @classmethod
    def debug(cls, message: str):
        """记录调试日志"""
        cls.get_logger().debug(message)
        cls._flush()
    
    @classmethod
    def critical(cls, message: str):
        """记录严重错误日志"""
        cls.get_logger().critical(message)
        cls._flush()
    
    @classmethod
    def snapshot_created(cls, snapshot_id: str, size_mb: float, plugin_count: int):
        """记录快照创建"""
        cls.info(f"快照创建成功 | ID: {snapshot_id} | 大小: {size_mb:.2f}MB | 插件数: {plugin_count}")
    
    @classmethod
    def snapshot_restored(cls, snapshot_id: str, mode: str):
        """记录快照恢复"""
        cls.info(f"快照恢复完成 | ID: {snapshot_id} | 模式: {mode}")
    
    @classmethod
    def environment_check_passed(cls):
        """记录环境校验通过"""
        cls.info("环境校验通过，允许启动DeepSeek")
    
    @classmethod
    def environment_check_failed(cls, reason: str):
        """记录环境校验失败"""
        cls.error(f"环境校验失败: {reason}")
    
    @classmethod
    def deepseek_launched(cls):
        """记录DeepSeek启动"""
        cls.info("DeepSeek客户端启动成功")
    
    @classmethod
    def deepseek_crashed(cls, exit_code: int):
        """记录DeepSeek崩溃"""
        cls.critical(f"DeepSeek异常退出 | 退出码: {exit_code}")
    
    @classmethod
    def cloud_sync_completed(cls, file_count: int, size_mb: float):
        """记录云端同步完成"""
        cls.info(f"云端同步完成 | 文件数: {file_count} | 大小: {size_mb:.2f}MB")
    
    @classmethod
    def cloud_sync_failed(cls, error: str):
        """记录云端同步失败"""
        cls.error(f"云端同步失败: {error}")
    
    @classmethod
    def file_changed(cls, file_path: str, change_type: str):
        """记录文件变更"""
        cls.info(f"文件变更检测 | 路径: {file_path} | 类型: {change_type}")
    
    @classmethod
    def pre_launch_check_started(cls):
        """记录前置校验开始"""
        cls.info("前置启动校验开始")
    
    @classmethod
    def pre_launch_check_completed(cls, status: str):
        """记录前置校验完成"""
        cls.info(f"前置启动校验完成 | 状态: {status}")
