"""
ZeroFall Windows 系统通知模块
使用 ctypes 调用 Windows Toast Notification API
"""
import sys
import ctypes
from pathlib import Path


class Notifier:
    """Windows 系统通知"""
    
    @staticmethod
    def show(title: str, message: str, icon_path: str = ""):
        """显示 Windows 系统通知"""
        if sys.platform != 'win32':
            return False
        
        try:
            # 使用 PowerShell 发送通知（最可靠的方式）
            import subprocess
            script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
            
            $template = @"
            <toast>
                <visual>
                    <binding template="ToastGeneric">
                        <text>{title}</text>
                        <text>{message}</text>
                    </binding>
                </visual>
            </toast>
"@
            
            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("ZeroFall").Show($toast)
            '''
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', script],
                capture_output=True, timeout=5
            )
            return True
        except:
            return False
    
    @staticmethod
    def snapshot_created(snapshot_id: str, size_mb: float):
        """快照创建通知"""
        Notifier.show(
            "快照创建成功",
            f"ID: {snapshot_id}\n大小: {size_mb:.2f} MB"
        )
    
    @staticmethod
    def crash_detected(exit_code: int):
        """崩溃告警通知"""
        Notifier.show(
            "⚠ DeepSeek 异常退出",
            f"退出码: {exit_code}\n已自动创建崩溃快照，建议回退到健康快照。"
        )
    
    @staticmethod
    def guardian_alert(message: str):
        """守护告警通知"""
        Notifier.show("ZeroFall 告警", message)
