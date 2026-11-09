# -*- coding: utf-8 -*-
"""
Environment Tab - 环境校验标签页
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTextEdit,
    QLabel, QMessageBox
)
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class EnvTab(QWidget):
    """环境校验标签页"""
    
    def __init__(self, config, env_checker, parent=None):
        super().__init__(parent)
        self.config = config
        self.env_checker = env_checker
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化用户界面"""
        layout = QVBoxLayout(self)
        
        button_layout = QHBoxLayout()
        
        check_btn = QPushButton("执行完整校验")
        check_btn.clicked.connect(self._on_run_environment_check)
        button_layout.addWidget(check_btn)
        
        update_baseline_btn = QPushButton("更新环境基线")
        update_baseline_btn.clicked.connect(self._on_update_baseline)
        button_layout.addWidget(update_baseline_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        self.env_result_text = QTextEdit()
        self.env_result_text.setReadOnly(True)
        layout.addWidget(self.env_result_text)
    
    def _on_run_environment_check(self):
        """执行环境校验"""
        self.env_result_text.clear()
        self.env_result_text.append("正在执行环境校验...\n")
        
        report = self.env_checker.run_full_check()
        
        self.env_result_text.append(f"校验结果: {report.overall_result.value.upper()}\n")
        self.env_result_text.append("=" * 60 + "\n")
        
        for item in report.items:
            status_symbol = {
                "passed": "✓",
                "warning": "⚠",
                "failed": "✗"
            }.get(item.result.value, "?")
            
            self.env_result_text.append(f"[{status_symbol}] {item.name}")
            self.env_result_text.append(f"    状态: {item.result.value}")
            self.env_result_text.append(f"    信息: {item.message}")
            if item.details:
                self.env_result_text.append(f"    详情:\n{item.details}")
            self.env_result_text.append("")
        
        return report.overall_result
    
    def _on_update_baseline(self):
        """更新环境基线"""
        self.env_checker.update_baseline()
        QMessageBox.information(self, "更新成功", "环境基线已更新")
    
    def get_health_label_widget(self):
        """提供给外部更新健康状态的便捷方法（返回结果文本控件）"""
        return self.env_result_text
