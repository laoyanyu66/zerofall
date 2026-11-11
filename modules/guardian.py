"""
ZeroFall 前置守护启动模块
"""
import os
import sys
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional, Callable, Tuple
import psutil
from logger import ZeroFallLogger
from config import ZeroFallConfig
from modules.environment_checker import EnvironmentChecker, EnvironmentReport
from modules.snapshot_manager import SnapshotManager

class Guardian:
    """前置守护启动器"""
    
    def __init__(self, config: ZeroFallConfig, env_checker: EnvironmentChecker, 
                 snapshot_manager: SnapshotManager):
        self.config = config
        self.env_checker = env_checker
        self.snapshot_manager = snapshot_manager
        self.deepseek_process = None
        self.monitor_thread = None
        self.running = False
        self.on_crash_callback = None
        self.on_exit_callback = None
        self.launch_lock = threading.Lock()
        # 主动终止标记：ZeroFall 自己 kill DeepSeek（重启/恢复/部署）不算崩溃
        self._intentional_kill = False
        # is_deepseek_running 的 TTL 缓存：避免 UI 每 2~5 秒调一次时
        # 都 psutil.process_iter 全遍历（Windows 上单次要 50~200ms，高频调用
        # 累积成明显卡顿，是"窗口未响应"的主要来源之一）
        self._running_cache_ts = 0.0
        self._running_cache_val = False
    
    def set_crash_callback(self, callback: Callable):
        """设置崩溃回调"""
        self.on_crash_callback = callback
    
    def set_exit_callback(self, callback: Callable):
        """设置退出回调"""
        self.on_exit_callback = callback
    
    def pre_launch_check(self) -> Tuple[bool, Optional[EnvironmentReport]]:
        """前置启动校验"""
        ZeroFallLogger.pre_launch_check_started()
        
        # 执行环境校验
        report = self.env_checker.run_full_check()
        
        if report.overall_result.value == "failed":
            ZeroFallLogger.error("环境校验失败，拒绝启动DeepSeek")
            return False, report
        
        return True, report
    
    def launch_deepseek(self, bypass_check: bool = False) -> Tuple[bool, str]:
        """启动DeepSeek（带前置校验）
        
        Args:
            bypass_check: 是否跳过校验（仅在强制启动时使用）
        
        Returns:
            (成功与否, 消息)
        """
        with self.launch_lock:
            # 检查是否已经在运行
            if self.is_deepseek_running():
                return False, "DeepSeek已在运行中"
            
            # 前置校验
            if not bypass_check:
                success, report = self.pre_launch_check()
                if not success:
                    return False, "环境校验失败，请处理异常后再启动"
            
            # 启动DeepSeek进程
            try:
                exe_path = self.config.deepseek_exe_path
                
                if not Path(exe_path).exists():
                    return False, f"DeepSeek可执行文件不存在: {exe_path}"
                
                # 启动进程
                from utils import sanitized_child_env
                # 不把 PyInstaller onefile 的簿记变量漏给 DSH，
                # 否则从 DSH 那条链再启动 ZeroFall 会撞父进程校验失败。
                self.deepseek_process = subprocess.Popen(
                    [exe_path], env=sanitized_child_env())
                self._invalidate_running_cache()

                ZeroFallLogger.deepseek_launched()
                
                # 更新环境基线
                self.env_checker.update_baseline()
                
                # 启动进程监控
                self._start_process_monitor()
                
                return True, "DeepSeek启动成功"
                
            except Exception as e:
                ZeroFallLogger.error(f"启动DeepSeek失败: {e}")
                return False, f"启动失败: {str(e)}"
    
    def _start_process_monitor(self):
        """启动进程监控线程"""
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_process, daemon=True)
        self.monitor_thread.start()
        ZeroFallLogger.debug("进程监控线程已启动")
    
    def _monitor_process(self):
        """监控DeepSeek进程"""
        if not self.deepseek_process:
            return
        
        try:
            # 等待进程结束
            exit_code = self.deepseek_process.wait()
            
            # 判断是否正常退出
            # 主动终止（ZeroFall 自己 kill）不算崩溃，否则每次重启都会污染健康度
            intentional = self._intentional_kill
            self._intentional_kill = False
            crashed = (not intentional) and exit_code != 0 and exit_code is not None
            if intentional:
                ZeroFallLogger.info(
                    "DeepSeek 由 ZeroFall 主动终止（退出码 %s），不计为崩溃" % exit_code
                )
                # 清掉可能残留的崩溃标记
                try:
                    self.env_checker.record_exit(0, False)
                except Exception:
                    pass
                if self.on_exit_callback:
                    self.on_exit_callback(exit_code)
                return
            
            # 记录退出状态
            self.env_checker.record_exit(exit_code, crashed)
            
            if crashed:
                ZeroFallLogger.deepseek_crashed(exit_code)
                
                # 创建崩溃快照（用于排查）
                self.snapshot_manager.create_snapshot(
                    notes=f"崩溃自动快照 (退出码: {exit_code})",
                    force_healthy=False
                )
                
                # 触发崩溃回调
                if self.on_crash_callback:
                    self.on_crash_callback(exit_code)
            else:
                ZeroFallLogger.info(f"DeepSeek正常退出 | 退出码: {exit_code}")
                
                # 创建健康快照
                if self.config.auto_snapshot_before_change:
                    self.snapshot_manager.create_snapshot(
                        notes="退出自动快照",
                        force_healthy=True
                    )
                
                # 触发退出回调
                if self.on_exit_callback:
                    self.on_exit_callback(exit_code)
            
        except Exception as e:
            ZeroFallLogger.error(f"进程监控异常: {e}")
        
        finally:
            self.running = False
            self.deepseek_process = None
            self._invalidate_running_cache()
    
    def is_deepseek_running(self) -> bool:
        """检查DeepSeek是否正在运行（带 2 秒 TTL 缓存，避免高频 psutil 扫描）"""
        now = time.time()
        # 缓存命中：2 秒内的结果直接复用，UI 每 2~5 秒刷新也不会反复全进程遍历
        if now - self._running_cache_ts < 2.0:
            return self._running_cache_val

        result = False
        # 检查本地进程对象（最准、最快）
        if self.deepseek_process and self.deepseek_process.poll() is None:
            result = True
        else:
            # 通过进程名检查（可能用户手动启动了 / ZeroFall 重启后接管）
            try:
                for proc in psutil.process_iter(['name']):
                    try:
                        if proc.info['name'] and 'deepseek' in proc.info['name'].lower():
                            result = True
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:
                result = False

        self._running_cache_ts = now
        self._running_cache_val = result
        return result

    def _invalidate_running_cache(self):
        """主动变更 DeepSeek 生命周期后立即失效缓存（launch/kill/monitor 退出时调用）"""
        self._running_cache_ts = 0.0
    
    def kill_deepseek(self) -> bool:
        """强制终止DeepSeek进程（主动终止，不计为崩溃）"""
        try:
            # 标记为主动终止，_monitor_process 据此不写崩溃状态
            self._intentional_kill = True
            self._invalidate_running_cache()
            if self.deepseek_process:
                self.deepseek_process.kill()
                self.deepseek_process = None
                ZeroFallLogger.warning("DeepSeek进程已被强制终止（主动，不计崩溃）")
                return True
            
            # 通过进程名终止
            killed = False
            for proc in psutil.process_iter(['name']):
                try:
                    if proc.info['name'] and 'deepseek' in proc.info['name'].lower():
                        proc.kill()
                        killed = True
                        ZeroFallLogger.warning(f"终止DeepSeek进程: PID={proc.pid}")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            
            return killed
            
        except Exception as e:
            ZeroFallLogger.error(f"终止DeepSeek失败: {e}")
            return False
    
    def stop_monitoring(self):
        """停止监控"""
        self.running = False
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=2)
            ZeroFallLogger.debug("进程监控线程已停止")
    
    def get_deepseek_pid(self) -> Optional[int]:
        """获取DeepSeek进程PID"""
        if self.deepseek_process and self.deepseek_process.poll() is None:
            return self.deepseek_process.pid
        return None
