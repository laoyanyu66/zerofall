# -*- coding: utf-8 -*-
"""
Cloud Tab - 云端同步标签页（百度网盘同步盘方案）
"""
import os
import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGroupBox, QMessageBox, QGridLayout, QTableWidget,
    QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class CloudTab(QWidget):
    """云端同步标签页 — 同步盘方案"""
    
    def __init__(self, config, baidu_pan=None, parent=None, main_window=None):
        super().__init__(parent)
        self.config = config
        self.baidu_pan = baidu_pan
        self.main_window = main_window
        self._init_ui()
        self._refresh_status()
        
        # 每 10 秒自动刷新状态
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start(10000)
    
    def _init_ui(self):
        """初始化用户界面"""
        layout = QVBoxLayout(self)
        
        # ── 状态组 ──
        status_group = QGroupBox("同步盘同步状态")
        g = QGridLayout()
        
        g.addWidget(QLabel("同步功能:"), 0, 0)
        self.enabled_label = QLabel("检测中...")
        self.enabled_label.setStyleSheet("font-weight: bold;")
        g.addWidget(self.enabled_label, 0, 1)
        
        g.addWidget(QLabel("百度网盘客户端:"), 0, 2)
        self.client_label = QLabel("检测中...")
        self.client_label.setStyleSheet("font-weight: bold;")
        g.addWidget(self.client_label, 0, 3)
        
        g.addWidget(QLabel("同步目录:"), 1, 0)
        self.dir_label = QLabel("")
        self.dir_label.setStyleSheet("color: #888; font-size: 11px;")
        self.dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(self.dir_label, 1, 1, 1, 3)
        
        g.addWidget(QLabel("加密方式:"), 2, 0)
        self.encrypt_label = QLabel("")
        self.encrypt_label.setStyleSheet("color: #888; font-size: 11px;")
        g.addWidget(self.encrypt_label, 2, 1)
        
        g.addWidget(QLabel("同步策略:"), 2, 2)
        self.policy_label = QLabel("")
        self.policy_label.setStyleSheet("color: #888; font-size: 11px;")
        g.addWidget(self.policy_label, 2, 3)
        
        status_group.setLayout(g)
        layout.addWidget(status_group)
        
        # ── 按钮栏 ──
        btn_row = QHBoxLayout()
        
        refresh_btn = QPushButton("刷新状态")
        refresh_btn.clicked.connect(self._refresh_status)
        btn_row.addWidget(refresh_btn)
        
        sync_now_btn = QPushButton("立即同步")
        sync_now_btn.clicked.connect(self._on_sync_now)
        btn_row.addWidget(sync_now_btn)
        
        open_dir_btn = QPushButton("打开同步盘目录")
        open_dir_btn.clicked.connect(self._on_open_dir)
        btn_row.addWidget(open_dir_btn)
        
        start_client_btn = QPushButton("启动百度网盘")
        start_client_btn.clicked.connect(self._on_start_client)
        btn_row.addWidget(start_client_btn)
        
        btn_row.addStretch()
        layout.addLayout(btn_row)
        
        # ── 已同步文件列表 ──
        file_group = QGroupBox("同步盘中的快照")
        flayout = QVBoxLayout()
        
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels([
            "文件名", "大小(MB)", "修改时间", "类型"
        ])
        hh = self.file_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.file_table.setColumnWidth(1, 100)
        self.file_table.setColumnWidth(2, 160)
        self.file_table.setColumnWidth(3, 100)
        self.file_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        flayout.addWidget(self.file_table)
        
        file_group.setLayout(flayout)
        layout.addWidget(file_group)
        
        # ── 说明 ──
        hint = QLabel(
            "工作原理：ZeroFall 把快照加密复制到百度网盘同步盘目录，\n"
            "百度网盘客户端检测到文件变化后自动上传到云端。\n"
            "客户端必须保持运行，否则文件只留在本地不会上传。"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)
    
    # ── 状态检测 ──
    
    def _is_client_running(self) -> bool:
        """检测百度网盘客户端是否在运行"""
        try:
            import psutil
            for p in psutil.process_iter(['name']):
                try:
                    name = (p.info.get('name') or '').lower()
                    if 'baidunetdisk' in name:
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False
    
    def _refresh_status(self):
        """刷新所有状态显示"""
        try:
            enabled = bool(getattr(self.config, 'cloud_sync_enabled', False))
            sync_dir = getattr(self.config, 'cloud_sync_dir', '') or ''
            encrypt = bool(getattr(self.config, 'cloud_sync_encrypt', False))
            interval = getattr(self.config, 'cloud_sync_interval_minutes', 60)
            keep = getattr(self.config, 'cloud_history_keep_days', 14)
            keep_full = 0   # 已改为按天保留，见 cloud_history_keep_days
            
            # 同步功能状态
            if enabled:
                self.enabled_label.setText("已启用")
                self.enabled_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
            else:
                self.enabled_label.setText("未启用（请在设置中开启）")
                self.enabled_label.setStyleSheet("color: #ff9800; font-weight: bold;")
            
            # 客户端状态
            if self._is_client_running():
                self.client_label.setText("运行中 ✅")
                self.client_label.setStyleSheet("color: #4CAF50; font-weight: bold;")
            else:
                self.client_label.setText("未运行 ⚠（不上传）")
                self.client_label.setStyleSheet("color: #f44336; font-weight: bold;")
            
            self.dir_label.setText(sync_dir or "(未配置)")
            self.encrypt_label.setText("AES 加密" if encrypt else "明文")
            self.policy_label.setText(
                "每 {} 分钟 · 轻量保留 {} · 完整保留 {}".format(interval, keep, keep_full)
            )
            
            self._refresh_file_list(sync_dir, encrypt)
        except Exception as e:
            try:
                from logger import ZeroFallLogger
                ZeroFallLogger.error("[云同步页] 刷新状态失败: {}".format(e))
            except Exception:
                pass
    
    def _refresh_file_list(self, sync_dir: str, encrypt: bool):
        """列出同步盘里的快照文件"""
        self.file_table.setRowCount(0)
        if not sync_dir:
            return
        d = Path(sync_dir)
        if not d.exists():
            return
        
        pattern = "*.zip.enc" if encrypt else "*.zip"
        files = sorted(d.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        
        for f in files:
            try:
                st = f.stat()
                size_mb = st.st_size / (1024 * 1024)
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
                kind = "完整" if size_mb > 100 else "轻量"
                
                row = self.file_table.rowCount()
                self.file_table.insertRow(row)
                self.file_table.setItem(row, 0, QTableWidgetItem(f.name))
                self.file_table.setItem(row, 1, QTableWidgetItem("{:.2f}".format(size_mb)))
                self.file_table.setItem(row, 2, QTableWidgetItem(mtime))
                kind_item = QTableWidgetItem(kind)
                kind_item.setForeground(QColor("#1f6feb") if kind == "完整" else QColor("#4CAF50"))
                self.file_table.setItem(row, 3, kind_item)
            except Exception:
                pass
    
    # ── 操作 ──
    
    def _on_sync_now(self):
        """立即触发一次云同步"""
        try:
            if self.main_window and hasattr(self.main_window, '_sync_latest_to_cloud'):
                self.main_window._sync_latest_to_cloud()
                QMessageBox.information(self, "已触发", "已执行同步，请查看日志或稍后刷新状态")
                QTimer.singleShot(1500, self._refresh_status)
            else:
                QMessageBox.warning(self, "不可用", "主窗口未连接，无法触发同步")
        except Exception as e:
            QMessageBox.critical(self, "同步失败", str(e))
    
    def _on_open_dir(self):
        """打开同步盘目录"""
        sync_dir = getattr(self.config, 'cloud_sync_dir', '')
        if not sync_dir:
            QMessageBox.warning(self, "未配置", "同步目录未设置")
            return
        d = Path(sync_dir)
        if not d.exists():
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self, "无法创建", str(e))
                return
        try:
            os.startfile(str(d))
        except Exception as e:
            QMessageBox.critical(self, "打开失败", str(e))
    
    def _on_start_client(self):
        """启动百度网盘客户端"""
        try:
            if self._is_client_running():
                QMessageBox.information(self, "已在运行", "百度网盘客户端已在运行")
                return
            
            if self.main_window and hasattr(self.main_window, '_ensure_baidu_client'):
                ok = self.main_window._ensure_baidu_client(auto_start=True)
                if ok:
                    QMessageBox.information(self, "已启动", "正在启动百度网盘客户端...")
                    QTimer.singleShot(5000, self._refresh_status)
                else:
                    QMessageBox.warning(self, "启动失败", "未找到百度网盘客户端")
            else:
                QMessageBox.warning(self, "不可用", "主窗口未连接")
        except Exception as e:
            QMessageBox.critical(self, "启动异常", str(e))
    
    def update_auth_status(self):
        """兼容旧调用（API 方案已弃用）"""
        self._refresh_status()
