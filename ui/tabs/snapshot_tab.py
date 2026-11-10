# -*- coding: utf-8 -*-
"""
Snapshot Tab - 快照管理标签页
"""
from datetime import datetime
from typing import Tuple, Dict

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QInputDialog,
    QAbstractItemView, QApplication, QDialog, QLabel, QFileDialog
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent.parent))
from modules.snapshot_manager import RestoreMode
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox


class SnapshotTab(QWidget):
    """快照管理标签页"""
    
    def __init__(self, config, snapshot_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化用户界面"""
        layout = QVBoxLayout(self)
        
        button_layout = QHBoxLayout()
        
        create_btn = QPushButton("创建快照")
        create_btn.clicked.connect(self._on_create_snapshot)
        button_layout.addWidget(create_btn)
        
        full_btn = QPushButton("完整快照（含运行时）")
        full_btn.setToolTip("包含 primary-runtime 与 dsh-runtimes，约 530 MB，适合离线归档")
        full_btn.clicked.connect(self._on_create_full_snapshot)
        button_layout.addWidget(full_btn)
        
        restore_btn = QPushButton("恢复快照")
        restore_btn.clicked.connect(self._on_restore_snapshot)
        button_layout.addWidget(restore_btn)
        
        delete_btn = QPushButton("删除快照")
        delete_btn.clicked.connect(self._on_delete_snapshot)
        button_layout.addWidget(delete_btn)
        
        # 对比快照按钮
        self.compare_btn = QPushButton("对比快照")
        self.compare_btn.setToolTip("选择两个快照进行差异对比")
        self.compare_btn.clicked.connect(self._on_compare_snapshots)
        button_layout.addWidget(self.compare_btn)
        
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_snapshot_list)
        button_layout.addWidget(refresh_btn)
        
        import_enc_btn = QPushButton("从云端加密快照恢复")
        import_enc_btn.setToolTip("选择同步盘里的 .enc 文件，解密后导入并恢复")
        import_enc_btn.clicked.connect(self._on_import_encrypted)
        button_layout.addWidget(import_enc_btn)
        
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        # 磁盘占用 + 可恢复性统计（回答"快照吃了多少 G、哪个能回"）
        self.snap_stats_label = QLabel("")
        self.snap_stats_label.setObjectName('dim')
        self.snap_stats_label.setWordWrap(True)
        self.snap_stats_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.snap_stats_label)

        # 对比提示标签
        self.compare_hint_label = QLabel("")
        self.compare_hint_label.setStyleSheet("color: #ffaa00; font-size: 12px;")
        layout.addWidget(self.compare_hint_label)
        
        self.snapshot_table = QTableWidget()
        self.snapshot_table.setColumnCount(9)
        self.snapshot_table.setHorizontalHeaderLabels([
            "快照ID", "创建时间", "类型", "状态", "插件数", "大小(MB)",
            "MD5", "可恢复", "备注"
        ])
        self.snapshot_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.snapshot_table.setColumnWidth(0, 210)
        self.snapshot_table.setColumnWidth(1, 150)
        self.snapshot_table.setColumnWidth(2, 70)
        self.snapshot_table.setColumnWidth(3, 78)
        self.snapshot_table.setColumnWidth(4, 70)
        self.snapshot_table.setColumnWidth(5, 90)
        self.snapshot_table.setColumnWidth(6, 100)
        self.snapshot_table.setColumnWidth(7, 80)
        self.snapshot_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.snapshot_table.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.snapshot_table)
        
        self.refresh_snapshot_list()
    
    def _on_import_encrypted(self):
        """从云端加密快照（.enc）解密导入并恢复"""
        import tempfile
        from pathlib import Path as _P
        
        fp, _ = QFileDialog.getOpenFileName(
            self, "选择加密快照", "", "加密快照 (*.enc);;所有文件 (*.*)"
        )
        if not fp:
            return
        
        # 找同目录的元数据
        meta_path = _P(str(fp) + ".meta.json")
        
        reply = QMessageBox.question(
            self, "确认导入",
            "将从加密快照恢复环境：\n\n{}\n\n"
            "步骤：解密 → 导入快照库 → 恢复环境\n\n"
            "确定继续？".format(_P(fp).name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        try:
            from utils import decrypt_file
            from logger import ZeroFallLogger
            
            tmp_dir = _P(tempfile.gettempdir())
            dec_path = tmp_dir / (_P(fp).stem + ".zip")
            
            ZeroFallLogger.info("[加密导入] 开始解密: {}".format(_P(fp).name))
            from modules.cloud_crypto import decrypt_snapshot
            ok, _src = decrypt_snapshot(fp, dec_path, getattr(self, 'config', None))
            if not ok:
                QMessageBox.critical(
                    self, "解密失败",
                    "解密失败。可能原因：\n• 文件损坏\n• 不是 ZeroFall 加密的快照"
                )
                return
            
            # 元数据：缺失时现场生成
            if not meta_path.exists():
                import json as _json
                import hashlib
                md5 = hashlib.md5(dec_path.read_bytes()).hexdigest()
                sid = _P(fp).stem.replace(".zip", "")
                meta = {
                    "snapshot_id": sid,
                    "created_at": "00000000_000000",
                    "status": "healthy",
                    "plugin_count": 0,
                    "size_mb": round(dec_path.stat().st_size / (1024 * 1024), 2),
                    "md5_checksum": md5,
                    "plugins": [],
                    "notes": "从云端加密快照导入",
                    "deepseek_version": "",
                    "has_session_data": False,
                    "has_keys": False,
                    "archive_path": ""
                }
                meta_path = tmp_dir / (sid + ".meta.json")
                meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
            
            snapshot = self.snapshot_manager.import_snapshot(str(dec_path), str(meta_path))
            if not snapshot:
                QMessageBox.critical(self, "导入失败", "快照导入失败，请查看日志")
                return
            
            ZeroFallLogger.info("[加密导入] 导入成功: {}".format(snapshot.snapshot_id))
            
            # 立即回退
            restore_reply = QMessageBox.question(
                self, "导入成功",
                "快照已导入：{}\n\n是否立即恢复该快照？\n"
                "（仅恢复插件环境，保留当前会话）".format(snapshot.snapshot_id),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if restore_reply == QMessageBox.StandardButton.Yes:
                from modules.snapshot_manager import RestoreMode
                self._restore_with_progress(
                    snapshot.snapshot_id, RestoreMode.PLUGIN_ONLY,
                    on_done=lambda ok, err: QMessageBox.information(
                        self, "恢复成功", "环境已恢复，请重启 DeepSeek"
                    ) if ok else QMessageBox.critical(
                        self, "恢复失败", "恢复失败\n{}".format(err or "请查看日志")
                    )
                )
            
            self.refresh_snapshot_list()
        except Exception as e:
            QMessageBox.critical(self, "导入异常", str(e))
    
    def _on_create_snapshot(self):
        """创建快照"""
        notes, ok = self._get_input("创建快照", "请输入快照备注（可选）:")
        if ok:
            snapshot = self.snapshot_manager.create_snapshot(notes=notes)
            if snapshot:
                QMessageBox.information(self, "创建成功", "快照创建成功\nID: {}".format(snapshot.snapshot_id))
                self.refresh_snapshot_list()
            else:
                QMessageBox.critical(self, "创建失败", "快照创建失败，请查看日志")
    
    def _on_create_full_snapshot(self):
        """创建完整快照（含运行时，约 530 MB）"""
        reply = QMessageBox.question(
            self, "创建完整快照",
            "完整快照会额外包含：\n\n"
            "• primary-runtime（DSH 程序运行时，约 257 MB）\n"
            "• dsh-runtimes（用户运行时，约 258 MB）\n\n"
            "总计约 530 MB，适合离线归档。\n"
            "日常回退请使用普通快照（15 MB 左右）。\n\n"
            "确定创建？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        QMessageBox.information(self, "开始创建", "正在创建完整快照，可能需要 1-3 分钟，请稍候...")
        QApplication.processEvents()
        
        snapshot = self.snapshot_manager.create_snapshot(
            notes="完整快照（含运行时）",
            force_healthy=True,
            include_runtime=True
        )
        if snapshot:
            QMessageBox.information(
                self, "创建成功",
                "完整快照创建成功\n\nID: {}\n大小: {:.2f} MB".format(
                    snapshot.snapshot_id, snapshot.size_mb
                )
            )
            self.refresh_snapshot_list()
        else:
            QMessageBox.critical(self, "创建失败", "完整快照创建失败，请查看日志")
    
    def _build_restore_preview(self, snapshot) -> str:
        """构造恢复前的差异摘要（快照内容 vs 当前环境）"""
        try:
            import zipfile
            if snapshot is None:
                return ""
            lines = []
            # 快照内含文件数
            try:
                with zipfile.ZipFile(snapshot.archive_path) as zf:
                    names = zf.namelist()
                lines.append("快照包含 {} 个文件".format(len(names)))
            except Exception:
                names = []
            
            # 快照里的插件目录
            snap_plugins = set()
            for n in names:
                if n.startswith('plugins/pkgs/') and n.count('/') >= 3:
                    owner = n.split('/')[2]
                    if owner and owner != 'node_modules':
                        snap_plugins.add(owner)
            
            # 当前实际插件目录
            live_plugins = set()
            try:
                base = Path(self.snapshot_manager.config.deepseek_plugins_dir) / 'pkgs'
                if base.exists():
                    live_plugins = {
                        p.name for p in base.iterdir()
                        if p.is_dir() and not p.name.startswith('.') and p.name != 'node_modules'
                    }
            except Exception:
                pass
            
            extra = sorted(live_plugins - snap_plugins)
            missing = sorted(snap_plugins - live_plugins)
            lines.append("快照插件 {} 个 / 当前 {} 个".format(len(snap_plugins), len(live_plugins)))
            if extra:
                lines.append("⚠ 将移走当前多出的 {} 个插件：{}".format(
                    len(extra), ", ".join(extra[:5]) + (" ..." if len(extra) > 5 else "")
                ))
            if missing:
                lines.append("将补回缺失的 {} 个插件：{}".format(
                    len(missing), ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
                ))
            if not extra and not missing:
                lines.append("插件目录与快照一致，只覆盖被修改的文件")
            return "\n".join(lines) + "\n"
        except Exception:
            return ""
    
    def _restore_with_progress(self, snapshot_id: str, mode, on_done=None):
        """后台线程执行恢复 + 模态进度提示（完整快照 20~40 秒，不能阻塞 UI）"""
        import threading
        from PyQt6.QtWidgets import QProgressDialog
        from PyQt6.QtCore import QTimer as _QTimer
        
        dlg = QProgressDialog(
            "正在恢复快照，请稍候（完整快照可能需要 20~40 秒）...",
            "", 0, 0, self
        )
        dlg.setWindowTitle("恢复快照")
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.setMinimumWidth(420)
        dlg.show()
        
        state = {"ok": False, "err": None}
        
        def _work():
            try:
                state["ok"] = self.snapshot_manager.restore_snapshot(snapshot_id, mode)
            except Exception as e:
                state["err"] = str(e)
        
        th = threading.Thread(target=_work, daemon=True, name="ZeroFall-Restore")
        th.start()
        
        def _poll():
            if th.is_alive():
                _QTimer.singleShot(200, _poll)
                return
            try:
                dlg.close()
            except Exception:
                pass
            if on_done:
                try:
                    on_done(state["ok"], state["err"])
                except Exception:
                    pass
        
        _QTimer.singleShot(200, _poll)
    
    def _on_restore_snapshot(self):
        """恢复快照"""
        selected = self.snapshot_table.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个快照")
            return
        
        snapshot_id = self.snapshot_table.item(selected, 0).text()
        # 读取类型列，给用户更明确的提示
        type_text = ""
        try:
            item = self.snapshot_table.item(selected, 2)
            type_text = item.text() if item else ""
        except Exception:
            pass
        
        mode, ok = self._select_restore_mode()
        if not ok:
            return
        
        # 差异预览：告诉用户会动到什么
        try:
            snap = self.snapshot_manager.snapshots.get(snapshot_id)
            summary = self._build_restore_preview(snap)
        except Exception:
            summary = ""
        
        reply = QMessageBox.question(
            self, "确认恢复",
            "即将恢复快照：\n{}\n类型：{}\n\n{}\n"
            "恢复会：\n"
            "• 自动备份当前（损坏的）环境\n"
            "• 移走快照之后新增的插件/配置\n"
            "• 用快照内容覆盖被修改的文件\n"
            "• 运行时/缓存/凭据保持不变\n\n"
            "确定继续？".format(snapshot_id, type_text or "未知", summary),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        def _done(success, err):
            if success:
                QMessageBox.information(
                    self, "恢复成功",
                    "环境已恢复到快照：\n{}\n\n建议重启 DeepSeek 使改动生效。".format(snapshot_id)
                )
            else:
                QMessageBox.critical(
                    self, "恢复失败",
                    "恢复失败{}\n请查看日志：D:\\zerofall\\logs\\".format(
                        ("：" + err) if err else ""
                    )
                )
            self.refresh_snapshot_list()
        
        self._restore_with_progress(snapshot_id, mode, on_done=_done)
    
    def _on_delete_snapshot(self):
        """删除快照"""
        selected = self.snapshot_table.currentRow()
        if selected < 0:
            QMessageBox.warning(self, "未选择", "请先选择一个快照")
            return
        
        snapshot_id = self.snapshot_table.item(selected, 0).text()
        
        reply = QMessageBox.question(
            self, "确认删除",
            "确定要删除快照 {} 吗？\n此操作不可撤销。".format(snapshot_id),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            success = self.snapshot_manager.delete_snapshot(snapshot_id)
            if success:
                QMessageBox.information(self, "删除成功", "快照已删除")
                self.refresh_snapshot_list()
            else:
                QMessageBox.critical(self, "删除失败", "快照删除失败")
    
    def _on_compare_snapshots(self):
        """对比快照 - 需要用户选择两个快照"""
        selected_rows = self.snapshot_table.selectionModel().selectedRows()
        
        if len(selected_rows) < 2:
            QMessageBox.information(
                self, "对比快照",
                "请在表格中按住 Ctrl 选择两个快照后再点击「对比快照」"
            )
            return
        
        if len(selected_rows) > 2:
            QMessageBox.warning(
                self, "对比快照",
                "只能选择两个快照进行对比，请取消多余选择"
            )
            return
        
        row1 = selected_rows[0].row()
        row2 = selected_rows[1].row()
        id1 = self.snapshot_table.item(row1, 0).text()
        id2 = self.snapshot_table.item(row2, 0).text()
        
        self.compare_hint_label.setText(
            "正在对比 {} <-> {} ...".format(id1[:16], id2[:16])
        )
        QApplication.processEvents()
        
        try:
            diff = self.snapshot_manager.compare_snapshots(id1, id2)
        except Exception as e:
            QMessageBox.critical(self, "对比失败", "对比快照出错: {}".format(e))
            self.compare_hint_label.setText("")
            return
        
        self.compare_hint_label.setText("")
        self._show_compare_result(id1, id2, diff)
    
    def _show_compare_result(self, id1, id2, diff):
        """弹窗展示快照对比结果"""
        dlg = QDialog(self)
        dlg.setWindowTitle("快照对比: {} <-> {}".format(id1[:16], id2[:16]))
        dlg.setMinimumSize(700, 500)
        dlg_layout = QVBoxLayout(dlg)
        
        # 如果没有差异数据
        if not diff:
            dlg_layout.addWidget(QLabel("无差异数据（可能快照中无插件目录）"))
            close_btn = QPushButton("关闭")
            close_btn.clicked.connect(dlg.close)
            dlg_layout.addWidget(close_btn)
            dlg.exec()
            return
        
        # 提取插件差异
        plugins_diff = diff.get('plugins', {})
        added = plugins_diff.get('added', [])
        removed = plugins_diff.get('removed', [])
        modified = plugins_diff.get('modified', [])
        
        # 汇总
        summary = QLabel(
            "新增: {} 个文件 | 删除: {} 个文件 | 修改: {} 个文件".format(
                len(added), len(removed), len(modified)
            )
        )
        summary.setStyleSheet("font-size: 14px; font-weight: bold; padding: 6px;")
        dlg_layout.addWidget(summary)
        
        # 差异表格
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["变更类型", "文件路径"])
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        
        rows = []
        for f in added:
            rows.append(("➕ 新增", f))
        for f in removed:
            rows.append(("❌ 删除", f))
        for f in modified:
            rows.append(("🔄 修改", f))
        
        table.setRowCount(len(rows))
        color_map = {"➕ 新增": "#00ff88", "❌ 删除": "#ff4444", "🔄 修改": "#ffaa00"}
        for r, (change_type, filepath) in enumerate(rows):
            type_item = QTableWidgetItem(change_type)
            type_item.setForeground(QColor(color_map.get(change_type, "#ccc")))
            table.setItem(r, 0, type_item)
            table.setItem(r, 1, QTableWidgetItem(filepath))
        
        dlg_layout.addWidget(table)
        
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.close)
        dlg_layout.addWidget(close_btn)
        
        dlg.exec()
    
    def refresh_snapshot_list(self):
        """刷新快照列表（先从磁盘重载索引，外部创建的快照也能立刻看到）"""
        try:
            self.snapshot_manager.reload_index()
        except Exception:
            pass
        snapshots = self.snapshot_manager.get_snapshot_list()
        try:
            restorable = self.snapshot_manager.get_restorable_ids()
        except Exception:
            restorable = {x.snapshot_id for x in snapshots}
        
        self.snapshot_table.setRowCount(len(snapshots))
        
        for row, snapshot in enumerate(snapshots):
            is_full = getattr(snapshot, 'is_full', False)
            
            self.snapshot_table.setItem(row, 0, QTableWidgetItem(snapshot.snapshot_id))
            self.snapshot_table.setItem(row, 1, QTableWidgetItem(
                datetime.strptime(snapshot.created_at, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            ))
            
            # 类型列：轻量 / 完整
            type_item = QTableWidgetItem("完整" if is_full else "轻量")
            type_item.setForeground(QColor("#1f6feb") if is_full else QColor("#4CAF50"))
            type_font = type_item.font()
            type_font.setBold(is_full)
            type_item.setFont(type_font)
            type_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            type_item.setToolTip(
                "完整快照：含 primary-runtime（DSH 程序运行时），约 108 MB，用于深度恢复"
                if is_full else
                "轻量快照：插件 + 配置 + 会话 + runtime 关键文件，约 26 MB"
            )
            self.snapshot_table.setItem(row, 2, type_item)
            
            # 状态列：健康 / 崩溃
            # 崩溃快照的语义：创建那一刻 DeepSeek 没在运行（可能是真崩溃，也可能
            # 只是用户手动关掉了）。这类快照不参与恢复链，也不计入轻量/完整上限
            # 的编排，只留作现场供事后分析 —— 所以在界面上要说清楚"点了也回不去"。
            if snapshot.status.value != "healthy":
                label, color = "崩溃", QColor("red")
                recovery_hint = (
                    "崩溃点快照（创建时 DeepSeek 未运行）\n"
                    "不参与恢复链：轻量快照恢复需要健康基线，\n"
                    "所以这个点目前无法直接回退。\n"
                    "它保留下来供事后分析崩溃现场。")
            else:
                label, color = "健康", QColor("green")
                recovery_hint = "健康快照，可用于恢复"
            status_item = QTableWidgetItem(label)
            status_item.setForeground(color)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status_item.setToolTip(recovery_hint)
            self.snapshot_table.setItem(row, 3, status_item)
            
            self.snapshot_table.setItem(row, 4, QTableWidgetItem(str(snapshot.plugin_count)))
            # 大小列：显示磁盘占用（压缩后），tooltip 补充解压后大小
            compressed_mb = 0.0
            try:
                compressed_mb = Path(snapshot.archive_path).stat().st_size / (1024 * 1024)
            except Exception:
                compressed_mb = snapshot.size_mb
            size_item = QTableWidgetItem("{:.2f}".format(compressed_mb))
            size_item.setToolTip(
                "磁盘占用（压缩后）：{:.2f} MB\n解压后大小：{:.2f} MB".format(
                    compressed_mb, snapshot.size_mb
                )
            )
            self.snapshot_table.setItem(row, 5, size_item)
            self.snapshot_table.setItem(row, 6, QTableWidgetItem(snapshot.md5_checksum[:8] + "..."))

            # 可恢复列：轻量快照需要它之前有完整快照做基线，否则点了也回不去
            can_restore = snapshot.snapshot_id in restorable
            ok_item = QTableWidgetItem("可恢复" if can_restore else "缺基线")
            ok_item.setForeground(QColor("#22d3ee") if can_restore else QColor("#f59e0b"))
            ok_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if not can_restore:
                ok_item.setToolTip(
                    "这个快照早于库里最早的一个完整快照，缺少恢复基线。\n"
                    "轻量快照恢复要先回到它之前的完整快照再叠加，\n"
                    "所以目前无法直接恢复到这个点。")
            self.snapshot_table.setItem(row, 7, ok_item)
            self.snapshot_table.setItem(row, 8, QTableWidgetItem(snapshot.notes))
    
        self._update_stats()

    def _update_stats(self):
        """更新顶部磁盘占用统计

        快照是"看不见的磁盘大户"：不给数字，用户就不知道该不该调间隔和上限。
        """
        try:
            st = self.snapshot_manager.get_disk_stats()
        except Exception as e:
            self.snap_stats_label.setText("统计不可用: {}".format(e))
            return
        try:
            orphans = self.snapshot_manager.find_orphan_archives()
        except Exception:
            orphans = []

        bits = []
        if st['light_count']:
            bits.append("轻量 {} / {} 个 · {:.2f} GB · 跨 {:.1f}h".format(
                st['light_count'], st['light_max'],
                st['light_mb'] / 1024, st['light_cover_h']))
        else:
            bits.append("轻量 0 / {} 个".format(st['light_max']))
        if st['full_count']:
            bits.append("完整 {} / {} 个 · {:.2f} GB · 跨 {:.1f}h".format(
                st['full_count'], st['full_max'],
                st['full_mb'] / 1024, st['full_cover_h']))
        else:
            bits.append("完整 0 / {} 个".format(st['full_max']))

        text = "快照合计 {:.2f} GB　｜　".format(st['total_mb'] / 1024)
        text += "　｜　".join(bits)
        if st['free_gb'] >= 0:
            text += "　｜　盘剩余 {:.0f} GB".format(st['free_gb'])
        if orphans:
            om = 0.0
            for f in orphans:
                try:
                    om += f.stat().st_size / (1024 * 1024)
                except Exception:
                    pass
            text += "\n⚠ {} 个孤儿快照文件（有压缩包但索引无记录，无法恢复），合计 {:.2f} GB".format(
                len(orphans), om / 1024)

        self.snap_stats_label.setText(text)
        self.snap_stats_label.setStyleSheet(
            "font-size: 12px; color: {};".format('#f59e0b' if orphans else '#8899aa'))

    def _get_input(self, title, prompt):
        """获取用户输入"""
        text, ok = QInputDialog.getText(self, title, prompt)
        return text, ok
    
    def _select_restore_mode(self):
        """选择恢复模式"""
        modes = [
            ("仅恢复插件环境（保留当前会话）", RestoreMode.PLUGIN_ONLY),
            ("完整恢复（插件环境+会话数据）", RestoreMode.FULL_RESTORE),
            ("仅恢复会话数据（不改动插件环境）", RestoreMode.SESSION_ONLY)
        ]
        
        items = [m[0] for m in modes]
        item, ok = QInputDialog.getItem(self, "选择恢复模式", "请选择恢复模式:", items, 0, False)
        
        if ok:
            index = items.index(item)
            return modes[index][1], True
        
        return RestoreMode.PLUGIN_ONLY, False
