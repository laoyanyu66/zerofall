"""
ZeroFall 崩溃自动上报模块
程序崩溃时生成 crash dump 并保存到本地
"""
import os
import sys
import traceback
import datetime
from pathlib import Path


class CrashReporter:
    """崩溃上报器"""
    
    _installed = False
    
    @classmethod
    def install(cls, log_dir: str = "logs"):
        """安装崩溃捕获钩子"""
        if cls._installed:
            return
        cls._installed = True
        
        # 使用绝对路径（兼容 PyInstaller）
        if getattr(sys, 'frozen', False):
            # PyInstaller 打包后，使用 exe 所在目录
            base_dir = Path(sys.executable).parent
        else:
            # 开发模式，使用当前工作目录
            base_dir = Path.cwd()
        
        cls._crash_dir = base_dir / log_dir / "crashes"
        cls._crash_dir.mkdir(parents=True, exist_ok=True)
        
        # 捕获未处理异常
        sys.excepthook = cls._handle_exception
    
    @classmethod
    def _handle_exception(cls, exc_type, exc_value, exc_traceback):
        """处理未捕获异常"""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        crash_file = cls._crash_dir / f"crash_{timestamp}.log"
        
        try:
            with open(crash_file, 'w', encoding='utf-8') as f:
                f.write(f"ZeroFall Crash Report\n")
                f.write(f"Time: {datetime.datetime.now().isoformat()}\n")
                f.write(f"Python: {sys.version}\n")
                f.write(f"Platform: {sys.platform}\n")
                f.write(f"{'='*60}\n\n")
                
                tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
                f.write(''.join(tb_lines))
            
            # 也写入日志
            try:
                from logger import ZeroFallLogger
                ZeroFallLogger.critical(f"程序崩溃，报告已保存: {crash_file}")
            except:
                pass
        except Exception:
            pass
        
        # 调用原始 excepthook
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
