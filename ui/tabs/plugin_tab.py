# -*- coding: utf-8 -*-
"""
ZeroFall 插件管理标签页
"""
import os
import sys
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QCheckBox, QGroupBox, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from logger import ZeroFallLogger
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class PluginTab(QWidget):
    """插件管理标签页"""
    
    # 已知插件的中文功能说明（优先于 package.json 的英文 description）
    PLUGIN_DESC_CN = {
        'dsh-custom-header': '在 fetch 传输层修改出站 LLM 请求，注入客户端身份请求头',
        'dsh-luopan': '罗盘：DeepSeek 主控 + 免费 Worker 调度，含重试/熔断/DAG/额度池/缓存/审计',
        'dsh-opencode-go': 'OpenCode Go 模型提供商 + 设置界面',
        'dsh-opencode-personal': 'OpenCode Personal 模型提供商 + 设置界面',
        'dsh-router-laya': '本地分类模型自动选择思考档（low/high/max），CPU 推理 <1 秒',
        'dsh-sinan': '司南：会话内嵌入式模型调节器（DeepSeek 主控 + 免费模型执行）',
        'dsh-tier-router': '模型 + 思考预算路由治理层（Laya 判定 → 档位 → 模型选择）',
    }
    
    def __init__(self, config, snapshot_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self._init_ui()
        self._refresh_plugins()
    
    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        # 按钮栏
        btn_layout = QHBoxLayout()
        
        refresh_btn = QPushButton("刷新插件列表")
        refresh_btn.clicked.connect(self._refresh_plugins)
        btn_layout.addWidget(refresh_btn)
        
        toggle_btn = QPushButton("启用/禁用选中插件")
        toggle_btn.setToolTip("禁用会把插件移到 pkgs/.zerofall_disabled/，操作前自动创建快照")
        toggle_btn.clicked.connect(self._on_toggle_plugin)
        btn_layout.addWidget(toggle_btn)
        
        backup_btn = QPushButton("备份选中插件")
        backup_btn.setToolTip("把插件目录单独打包到快照库的 plugin_backups/，改造前建议先备份")
        backup_btn.clicked.connect(self._on_backup_plugin)
        btn_layout.addWidget(backup_btn)
        
        lock_btn = QPushButton("切换只读锁定")
        lock_btn.clicked.connect(self._toggle_readonly)
        btn_layout.addWidget(lock_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        # 插件列表
        self.plugin_table = QTableWidget()
        self.plugin_table.setColumnCount(6)
        self.plugin_table.setHorizontalHeaderLabels([
            "插件名称", "版本", "功能说明", "状态", "大小", "路径"
        ])
        hh = self.plugin_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)   # 功能说明占满剩余宽度
        self.plugin_table.setColumnWidth(0, 170)
        self.plugin_table.setColumnWidth(1, 60)
        self.plugin_table.setColumnWidth(3, 70)
        self.plugin_table.setColumnWidth(4, 80)
        self.plugin_table.setColumnWidth(5, 160)
        # 关键：开启自动换行 + 行高随内容自适应，保证功能说明完整可见
        self.plugin_table.setWordWrap(True)
        self.plugin_table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.plugin_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.plugin_table)
        
        # 状态栏
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(self.status_label)
    
    DISABLED_DIR = ".zerofall_disabled"
    
    def _plugins_root(self):
        """插件实际根目录：优先 .dsh/pkgs，否则退回配置目录"""
        base = Path(self.config.deepseek_plugins_dir)
        pkgs = base / "pkgs"
        return pkgs if pkgs.exists() else base
    
    def _refresh_plugins(self):
        """刷新插件列表（含已禁用项）"""
        root = self._plugins_root()
        self.plugin_table.setRowCount(0)
        
        if not root.exists():
            self.status_label.setText("插件目录不存在: {}".format(root))
            return
        
        disabled_root = root / self.DISABLED_DIR
        total_size = 0
        active = 0
        disabled = 0
        
        entries = []
        # 启用中的插件（排除共享依赖 node_modules 等非插件目录）
        NON_PLUGIN = {"node_modules", "package.json", "package-lock.json",
                      "pnpm-lock.yaml", "pnpm-workspace.yaml", "vendor"}
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith('.') and d.name != self.DISABLED_DIR:
                if d.name in NON_PLUGIN:
                    continue
                entries.append((d, True))
        # 已禁用的插件
        if disabled_root.exists():
            for d in sorted(disabled_root.iterdir()):
                if d.is_dir():
                    entries.append((d, False))
        
        for d, enabled in entries:
            version = "unknown"
            desc = ""
            
            # 读取 package.json（name/version/description）
            pkg_file = d / "package.json"
            pkg_desc = ""
            if pkg_file.exists():
                try:
                    with open(pkg_file, 'r', encoding='utf-8') as f:
                        pkg = json.load(f)
                    version = pkg.get('version', 'unknown')
                    pkg_desc = (pkg.get('description') or '').strip()
                except Exception:
                    pass
            
            # 优先用中文映射，其次用 package.json 的 description
            desc = self.PLUGIN_DESC_CN.get(d.name, '') or pkg_desc or '(无说明)'
            
            try:
                size = sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
            except Exception:
                size = 0
            total_size += size
            if enabled:
                active += 1
            else:
                disabled += 1
            
            row = self.plugin_table.rowCount()
            self.plugin_table.insertRow(row)
            
            name_item = QTableWidgetItem(d.name)
            name_item.setData(Qt.ItemDataRole.UserRole, str(d))
            name_item.setData(Qt.ItemDataRole.UserRole + 1, enabled)
            self.plugin_table.setItem(row, 0, name_item)
            self.plugin_table.setItem(row, 1, QTableWidgetItem(version))
            
            # 功能说明：完整显示（不截断），靠自动换行 + 行高自适应保证可见
            desc_item = QTableWidgetItem(desc)
            desc_item.setToolTip(desc)
            desc_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.plugin_table.setItem(row, 2, desc_item)
            
            status_item = QTableWidgetItem("已启用" if enabled else "已禁用")
            status_item.setForeground(QColor("#4CAF50") if enabled else QColor("#888"))
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.plugin_table.setItem(row, 3, status_item)
            self.plugin_table.setItem(row, 4, QTableWidgetItem("{:.1f} KB".format(size / 1024)))
            # 路径列只显示目录名，完整路径放 tooltip，给功能说明腾出宽度
            path_item = QTableWidgetItem(d.name)
            path_item.setToolTip(str(d))
            self.plugin_table.setItem(row, 5, path_item)
        
        self.status_label.setText(
            "共 {} 个插件（启用 {} / 禁用 {}），总大小 {:.1f} MB | 根目录: {}".format(
                len(entries), active, disabled, total_size / (1024 * 1024), root
            )
        )
        
        # 行高重算：确保多行描述完整显示，不被压成一行
        try:
            self.plugin_table.resizeRowsToContents()
        except Exception:
            pass
    
    def _on_backup_plugin(self):
        """把选中插件单独打包备份（改造插件前用）"""
        row = self.plugin_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个插件")
            return
        
        item = self.plugin_table.item(row, 0)
        plugin_dir = Path(item.data(Qt.ItemDataRole.UserRole))
        name = item.text()
        if not plugin_dir.exists():
            QMessageBox.warning(self, "目录不存在", "插件目录已不存在：\n{}".format(plugin_dir))
            return
        
        try:
            from datetime import datetime as _dt
            import zipfile
            
            vault = Path(self.snapshot_manager.config.snapshot_dir)
            backup_dir = vault / "plugin_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            out = backup_dir / "{}_{}.zip".format(name, stamp)
            
            count = 0
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in plugin_dir.rglob('*'):
                    if f.is_file():
                        zf.write(f, f.relative_to(plugin_dir.parent))
                        count += 1
            
            mb = out.stat().st_size / (1024 * 1024)
            ZeroFallLogger.info(
                "[插件备份] {} -> {} ({} 文件, {:.2f} MB)".format(name, out.name, count, mb)
            )
            QMessageBox.information(
                self, "备份成功",
                "插件「{}」已备份\n\n"
                "文件：{}\n"
                "大小：{:.2f} MB\n"
                "文件数：{}\n\n"
                "位置：{}".format(name, out.name, mb, count, backup_dir)
            )
        except Exception as e:
            ZeroFallLogger.error("[插件备份] 失败: {}".format(e))
            QMessageBox.critical(self, "备份失败", str(e))
    
    def _on_toggle_plugin(self):
        """启用/禁用选中插件"""
        row = self.plugin_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个插件")
            return
        
        item = self.plugin_table.item(row, 0)
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        enabled = bool(item.data(Qt.ItemDataRole.UserRole + 1))
        name = item.text()
        
        action = "禁用" if enabled else "启用"
        reply = QMessageBox.question(
            self, "确认{}插件".format(action),
            "将对插件执行{}操作：\n\n{}\n\n"
            "{}后需要重启 DeepSeek 才生效。\n"
            "操作前会自动创建快照，可随时回退。\n\n"
            "确定继续？".format(name, action),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            root = self._plugins_root()
            disabled_root = root / self.DISABLED_DIR
            disabled_root.mkdir(parents=True, exist_ok=True)
            
            # 安全网：操作前自动快照
            try:
                self.snapshot_manager.create_snapshot(
                    notes="{}插件前自动快照: {}".format(action, name),
                    force_healthy=True
                )
                ZeroFallLogger.info("[插件管理] 已创建操作前快照")
            except Exception as e:
                ZeroFallLogger.warning("[插件管理] 操作前快照失败: {}".format(e))
            
            import shutil as _sh
            if enabled:
                target = disabled_root / path.name
                if target.exists():
                    _sh.rmtree(str(target), ignore_errors=True)
                _sh.move(str(path), str(target))
                ZeroFallLogger.info("[插件管理] 已禁用插件: {}".format(name))
            else:
                target = root / path.name
                if target.exists():
                    QMessageBox.warning(self, "冲突", "目标目录已存在同名插件，请先处理")
                    return
                _sh.move(str(path), str(target))
                ZeroFallLogger.info("[插件管理] 已启用插件: {}".format(name))
            
            QMessageBox.information(
                self, "操作成功",
                "插件「{}」已{}。\n\n请重启 DeepSeek 使其生效。".format(name, action)
            )
            self._refresh_plugins()
        except Exception as e:
            ZeroFallLogger.error("[插件管理] 切换插件状态失败: {}".format(e))
            QMessageBox.critical(self, "操作失败", str(e))
    
    def _toggle_readonly(self):
        """切换插件目录只读锁定"""
        plugins_dir = Path(self.config.deepseek_plugins_dir)
        if not plugins_dir.exists():
            QMessageBox.warning(self, "警告", "插件目录不存在")
            return
        
        from utils import set_directory_readonly
        current = self.config.plugin_dir_readonly_lock
        new_state = not current
        if set_directory_readonly(str(plugins_dir), new_state):
            self.config.plugin_dir_readonly_lock = new_state
            self._config_manager_save()
            state_text = "已锁定" if new_state else "已解锁"
            QMessageBox.information(self, "成功", "插件目录{}".format(state_text))
            self._refresh_plugins()
        else:
            QMessageBox.critical(self, "失败", "切换只读锁定失败")
    
    def _config_manager_save(self):
        """保存配置"""
        try:
            from config import ConfigManager
            cm = ConfigManager()
            cm.config = self.config
            cm.save()
        except:
            pass
