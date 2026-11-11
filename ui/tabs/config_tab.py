# -*- coding: utf-8 -*-
"""配置中心 —— 把 dsh-config-editor 的能力 + DSH 其它配置面搬进 ZeroFall

四个子页签，对应 DSH 真正可编辑的四类配置：

  1. profile 补丁   cordis.patch.yml —— 官方 config-editor 的落点（按 entry 改 config 块）
  2. 提示词注入      prompt-inject.md —— dsh-purge 每步重贴的那份，带 token 估算
  3. Agent 预设      .agent-presets/<name>/*.yml —— 无限制模式/演练台的骨架
  4. 备份与回滚      *.bak-* —— 上面三类文件的改前副本，一键还原

统一安全线：所有写入都走 modules.dsh_config（校验 + 改前备份 + 原子写 + 回读比对），
并且可以联动 ZeroFall 自己的快照（"保存前自动快照"）。
"""
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QPlainTextEdit,
    QListWidget, QListWidgetItem, QComboBox, QCheckBox, QSplitter, QLineEdit,
    QTabWidget, QSpinBox, QGroupBox, QFileDialog, QInputDialog, QLineEdit as _QLineEdit,
)
from PyQt6.QtCore import Qt

from logger import ZeroFallLogger
from modules import dsh_config as dc
from modules import config_bundle as cb
from modules import profile_health as ph
from modules import session_repair as sr
from modules import upgrade_preview as up
from modules import fault_timeline as ftl

from ui.dialogs import ZFMessageBox as QMessageBox


class ConfigTab(QWidget):
    """DSH 配置中心"""

    def __init__(self, config, snapshot_manager=None, main_window=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.main_window = main_window

        # profile 补丁页的状态
        self._text = ""
        self._path = None
        self._entries = []
        # 其它页的状态
        self._inject_text = ""
        self._preset_text = ""
        self._preset_path = None

        self._init_ui()
        self._refresh_profiles()
        self._load()
        self._load_inject()
        self._load_presets()

    # ─────────────────────────────────────────────────────── 骨架

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top = QHBoxLayout()
        self.auto_snapshot_box = QCheckBox("保存前自动快照")
        self.auto_snapshot_box.setChecked(True)
        self.auto_snapshot_box.setToolTip(
            "任何配置写入前都给 ZeroFall 打一份轻量快照：改坏了可从「快照管理」一键回到改之前。"
            "文件级的改前副本另外还有一份（见「备份与回滚」页）。")
        top.addWidget(self.auto_snapshot_box)

        self.home_label = QLineEdit(getattr(self.config, "deepseek_plugins_dir", "") or "")
        self.home_label.setReadOnly(True)
        top.addWidget(QLabel("DSH_HOME"))
        top.addWidget(self.home_label, 1)

        open_home = QPushButton("打开目录")
        open_home.clicked.connect(self._open_home)
        top.addWidget(open_home)

        # 改完 profile/预设要重启 DSH 才生效 —— 这里把"提示"变成"一键动作"
        self.restart_btn = QPushButton("重启 DSH")
        self.restart_btn.setToolTip(
            "结束当前 DSH 进程后重新启动，让刚写入的 profile 补丁 / agent 预设生效。"
            "注入稿不用重启（dsh-purge 每步重读）。")
        guardian = getattr(getattr(self, "main_window", None), "guardian", None)
        self.restart_btn.setEnabled(guardian is not None)
        self.restart_btn.clicked.connect(self._restart_dsh)
        top.addWidget(self.restart_btn)
        root.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_profile_tab(), "profile 补丁")
        self.tabs.addTab(self._build_inject_tab(), "提示词注入")
        self.tabs.addTab(self._build_preset_tab(), "Agent 预设")
        self.tabs.addTab(self._build_backup_tab(), "备份与回滚")
        self.tabs.addTab(self._build_transfer_tab(), "迁移与历史")
        self.tabs.addTab(self._build_health_tab(), "profile 健康")
        self.tabs.addTab(self._build_session_tab(), "会话修复")
        root.addWidget(self.tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        self.log.setPlaceholderText("操作与差异记录")
        root.addWidget(self.log)

    # ─────────────────────────────────────────────────────── 通用

    def _plugins_dir(self):
        return getattr(self.config, "deepseek_plugins_dir", "") or ""

    def _log(self, msg):
        self.log.appendPlainText("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), msg))

    def _open_home(self):
        try:
            import os
            os.startfile(self._plugins_dir())
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _open_file(self, path):
        try:
            import os
            os.startfile(str(path))
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _restart_dsh(self):
        """结束 DSH 再启动：让刚写入的 profile/预设生效

        走 ZeroFall 现成的 guardian（kill_deepseek / launch_deepseek），
        不自己拼命令行——否则会绕开它准备好的环境变量与工作目录。
        """
        guardian = getattr(getattr(self, "main_window", None), "guardian", None)
        if guardian is None:
            QMessageBox.warning(self, "无法重启", "当前没有 guardian 实例（独立测试环境里就是这样）。")
            return
        if QMessageBox.question(
                self, "重启 DSH",
                "会先结束当前 DSH 进程再重新启动。未发送的输入会丢失，继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("重启 DSH 前自动快照")
        try:
            guardian.kill_deepseek()
        except Exception as e:
            self._log("结束 DSH 失败：{}".format(e))
            return
        for _ in range(40):                      # 最多等 10 秒
            alive = False
            try:
                alive = bool(guardian.is_deepseek_running())
            except Exception:
                alive = False
            if not alive:
                break
            time.sleep(0.25)
        try:
            ok, msg = guardian.launch_deepseek()
        except Exception as e:
            self._log("启动 DSH 失败：{}".format(e))
            QMessageBox.critical(self, "启动失败", str(e))
            return
        self._log("重启 DSH：{} {}".format("成功" if ok else "失败", msg or ""))
        if not ok:
            QMessageBox.warning(self, "启动失败", str(msg or ""))

    def _pre_write(self, reason):
        """写盘前：ZeroFall 快照（勾选时）"""
        if not self.auto_snapshot_box.isChecked() or self.snapshot_manager is None:
            return
        try:
            data = self.snapshot_manager.create_snapshot(notes="配置变更前自动快照：{}".format(reason))
            self._log("已自动快照 {}".format(getattr(data, "snapshot_id", "") or "（失败）"))
        except Exception as e:
            self._log("自动快照异常：{}".format(e))

    def _commit_file(self, path, new_text, validator, summary):
        """统一写入：快照 → 校验/备份/原子写 → 刷新备份页"""
        if new_text == path.read_text(encoding="utf-8"):
            self._log("内容无变化，跳过写入")
            return False
        self._pre_write(summary)
        try:
            res = dc.save_text(path, new_text, validator=validator)
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
            self._log("写入失败：{}".format(e))
            return False
        self._log("{}；备份 {}".format(summary, Path(res["backup"]).name if res.get("backup") else "（无）"))
        self._reload_backups()
        return True

    # ─────────────────────────────────────────────────────── 1. profile 补丁

    def _build_profile_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel("Profile"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(140)
        self.profile_combo.currentTextChanged.connect(lambda _t: self._load())
        top.addWidget(self.profile_combo)

        self.path_label = QLineEdit()
        self.path_label.setReadOnly(True)
        top.addWidget(self.path_label, 1)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        open_btn = QPushButton("打开文件")
        open_btn.clicked.connect(lambda: self._path and self._open_file(self._path))
        top.addWidget(open_btn)
        root.addLayout(top)

        hint = QLabel(
            "只改 profile 层的 cordis.patch.yml：按 entry 定位，只替换它的 config 块，"
            "其余字节（注释、!!js 表达式、引号风格）原样保留。改完需重启 DSH 生效。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.entry_list = QListWidget()
        self.entry_list.setMinimumWidth(260)
        self.entry_list.currentRowChanged.connect(self._on_select)
        split.addWidget(self.entry_list)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.entry_label = QLabel("选择一个条目")
        rl.addWidget(self.entry_label)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("config: 的内容（YAML 映射）。留空 = 删除覆盖，回到跟随上层默认。")
        self.editor.setMinimumHeight(220)
        rl.addWidget(self.editor, 1)

        btns = QHBoxLayout()
        save_btn = QPushButton("保存到 profile")
        save_btn.clicked.connect(self._save)
        btns.addWidget(save_btn)
        remove_btn = QPushButton("移除覆盖")
        remove_btn.setToolTip("删掉该条目的 config 块；只剩 id/name 时整条移除，与官方行为一致。")
        remove_btn.clicked.connect(self._remove_override)
        btns.addWidget(remove_btn)
        self.disable_btn = QPushButton("禁用条目")
        self.disable_btn.clicked.connect(self._toggle_disabled)
        btns.addWidget(self.disable_btn)
        diff_btn = QPushButton("预览差异")
        diff_btn.clicked.connect(self._preview_diff)
        btns.addWidget(diff_btn)
        btns.addStretch()
        rl.addLayout(btns)

        split.addWidget(right)
        split.setSizes([280, 640])
        root.addWidget(split, 1)
        return w

    def _refresh_profiles(self):
        names = dc.list_profiles(self._plugins_dir())
        current = getattr(self.config, "acp_profile", "") or "desktop"
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if names:
            self.profile_combo.addItems(names)
            if current in names:
                self.profile_combo.setCurrentText(current)
            elif "desktop" in names:
                self.profile_combo.setCurrentText("desktop")
        self.profile_combo.blockSignals(False)

    def _load(self):
        profile = self.profile_combo.currentText() or "desktop"
        pdir = dc.resolve_profile_dir(self._plugins_dir(), profile)
        if pdir is None:
            self._path, self._text, self._entries = None, "", []
            self.entry_list.clear()
            self.path_label.setText("未找到 profile：{}".format(self._plugins_dir() or "空"))
            return
        self._path = dc.patch_path(pdir)
        try:
            self._text = self._path.read_text(encoding="utf-8")
            self._entries = dc.parse_entries(self._text)
        except Exception as e:
            self._text, self._entries = "", []
            self._log("读取失败：{}".format(e))
        self.path_label.setText(str(self._path))
        self._fill_list()
        self._log("载入 {} 个条目（{} 字节）".format(len(self._entries), len(self._text)))
        self._reload_backups()

    def _fill_list(self):
        self.entry_list.blockSignals(True)
        self.entry_list.clear()
        for row in self._entries:
            eid = str(row.get("id") or "")
            name = str(row.get("name") or "")
            marks = []
            if "insert" in row:
                marks.append("group")
            if "config" in row:
                marks.append("覆盖")
            if row.get("disabled"):
                marks.append("禁用")
            label = eid + ("  —  " + name if name and name != eid else "")
            if marks:
                label += "   [" + " · ".join(marks) + "]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, eid)
            self.entry_list.addItem(item)
        self.entry_list.blockSignals(False)
        if self.entry_list.count():
            self.entry_list.setCurrentRow(0)

    def _current_id(self):
        item = self.entry_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_select(self, _row):
        eid = self._current_id()
        if not eid:
            self.entry_label.setText("选择一个条目")
            self.editor.setPlainText("")
            return
        has_cfg = dc.has_override(self._text, eid)
        disabled = any(r.get("id") == eid and r.get("disabled") for r in self._entries)
        self.entry_label.setText("{}    {}".format(eid, "· 已覆盖" if has_cfg else "· 跟随上层默认"))
        self.disable_btn.setText("启用条目" if disabled else "禁用条目")
        try:
            self.editor.setPlainText(dc.config_block_text(self._text, eid))
        except Exception as e:
            self.editor.setPlainText("")
            self._log("读取 config 失败：{}".format(e))

    def _save(self):
        eid = self._current_id()
        if not eid or self._path is None:
            return
        try:
            new_text = dc.set_entry_config_text(self._text, eid, self.editor.toPlainText())
        except Exception as e:
            QMessageBox.critical(self, "片段无效", str(e))
            return
        if new_text == self._text:
            self._log("内容无变化，跳过写入")
            return
        self._pre_write("profile 补丁：{}".format(eid))
        try:
            res = dc.apply_text(self._path, new_text, auto_backup=True)
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
            self._log("写入失败：{}".format(e))
            return
        self._log("已保存 {} 的 config；备份 {}".format(eid, Path(res["backup"]).name))
        self._text = new_text
        try:
            self._entries = dc.parse_entries(self._text)
        except Exception:
            pass
        self._fill_list()
        self._reload_backups()
        self._log("提示：DSH 需要重启（或热重载）后才按新配置运行")

    def _remove_override(self):
        eid = self._current_id()
        if not eid or self._path is None:
            return
        try:
            new_text = dc.remove_override_text(self._text, eid)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", str(e))
            return
        if new_text != self._text:
            self._pre_write("移除覆盖：{}".format(eid))
            try:
                res = dc.apply_text(self._path, new_text, auto_backup=True)
                self._text = new_text
                self._entries = dc.parse_entries(self._text)
                self._log("已移除 {} 的覆盖；备份 {}".format(eid, Path(res["backup"]).name))
            except Exception as e:
                QMessageBox.critical(self, "写入失败", str(e))
        self._fill_list()
        self._reload_backups()

    def _toggle_disabled(self):
        eid = self._current_id()
        if not eid or self._path is None:
            return
        row = next((r for r in self._entries if r.get("id") == eid), {})
        target = not bool(row.get("disabled"))
        try:
            new_text = dc.set_entry_disabled_text(self._text, eid, target)
        except Exception as e:
            QMessageBox.critical(self, "切换失败", str(e))
            return
        if new_text == self._text:
            return
        self._pre_write("{} {}".format("禁用" if target else "启用", eid))
        try:
            res = dc.apply_text(self._path, new_text, auto_backup=True)
            self._text = new_text
            self._entries = dc.parse_entries(self._text)
            self._log("{} {}；备份 {}".format("已禁用" if target else "已启用", eid,
                                            Path(res["backup"]).name))
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
        self._fill_list()
        self._on_select(self.entry_list.currentRow())
        self._reload_backups()

    def _preview_diff(self):
        eid = self._current_id()
        if not eid or self._path is None:
            return
        try:
            new_text = dc.set_entry_config_text(self._text, eid, self.editor.toPlainText())
        except Exception as e:
            QMessageBox.critical(self, "片段无效", str(e))
            return
        self.log.clear()
        self.log.appendPlainText(dc.unified_diff(self._text, new_text) or "（无差异）")

    # ─────────────────────────────────────────────────────── 2. 提示词注入

    def _build_inject_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        self.inject_path_label = QLineEdit()
        self.inject_path_label.setReadOnly(True)
        top.addWidget(self.inject_path_label, 1)
        reload_btn = QPushButton("重新读取")
        reload_btn.clicked.connect(self._load_inject)
        top.addWidget(reload_btn)
        open_btn = QPushButton("打开文件")
        open_btn.clicked.connect(lambda: self._open_file(dc.inject_path(self._plugins_dir())))
        top.addWidget(open_btn)
        root.addLayout(top)

        self.inject_meta = QLabel("—")
        self.inject_meta.setObjectName("muted")
        root.addWidget(self.inject_meta)

        hint = QLabel(
            "这份文件会被 dsh-purge 在每一步重新贴进系统提示词，所以体量就是每步的固定成本。"
            "删掉重复段落能直接省钱、省上下文。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        self.inject_editor = QPlainTextEdit()
        self.inject_editor.setMinimumHeight(240)
        root.addWidget(self.inject_editor, 1)

        btns = QHBoxLayout()
        save_btn = QPushButton("保存注入稿")
        save_btn.clicked.connect(self._save_inject)
        btns.addWidget(save_btn)
        btns.addStretch()
        root.addLayout(btns)
        return w

    def _load_inject(self):
        p = dc.inject_path(self._plugins_dir())
        self.inject_path_label.setText(str(p))
        try:
            self._inject_text = p.read_text(encoding="utf-8") if p.is_file() else ""
        except Exception as e:
            self._inject_text = ""
            self._log("读取注入稿失败：{}".format(e))
        self.inject_editor.setPlainText(self._inject_text)
        m = dc.text_metrics(self._inject_text)
        self.inject_meta.setText(
            "{} 字节 / {} 字符 / {} 行 ≈ {} token/步（CJK×1 + 非CJK÷3.6）".format(
                m["bytes"], m["chars"], m["lines"], m["tokens"]))
        self._reload_backups()

    def _save_inject(self):
        p = dc.inject_path(self._plugins_dir())
        text = self.inject_editor.toPlainText()
        if not text.strip():
            QMessageBox.warning(self, "内容为空", "注入稿不能为空——空了 dsh-purge 就没有可贴的内容。")
            return
        if text == self._inject_text:
            self._log("注入稿无变化")
            return
        self._pre_write("提示词注入稿")
        try:
            res = dc.save_text(p, text)
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
            return
        before = dc.text_metrics(self._inject_text)
        after = dc.text_metrics(text)
        self._log("注入稿已保存：{} → {} 字节（{} → {} token/步）；备份 {}".format(
            before["bytes"], after["bytes"], before["tokens"], after["tokens"],
            Path(res["backup"]).name))
        self._load_inject()
        self._log("提示：dsh-purge 每步重新读取该文件，多数情况下无需重启 DSH")

    # ─────────────────────────────────────────────────────── 3. Agent 预设

    def _build_preset_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(QLabel("预设"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.currentTextChanged.connect(lambda _t: self._refresh_preset_files())
        top.addWidget(self.preset_combo)
        top.addWidget(QLabel("文件"))
        self.preset_file_combo = QComboBox()
        self.preset_file_combo.setMinimumWidth(180)
        self.preset_file_combo.currentTextChanged.connect(lambda _t: self._load_preset())
        top.addWidget(self.preset_file_combo)
        reload_btn = QPushButton("刷新")
        reload_btn.clicked.connect(self._load_presets)
        top.addWidget(reload_btn)
        open_btn = QPushButton("打开文件")
        open_btn.clicked.connect(lambda: self._preset_path and self._open_file(self._preset_path))
        top.addWidget(open_btn)
        root.addLayout(top)

        self.preset_meta = QLabel("—")
        self.preset_meta.setObjectName("muted")
        root.addWidget(self.preset_meta)

        self.preset_editor = QPlainTextEdit()
        self.preset_editor.setMinimumHeight(240)
        root.addWidget(self.preset_editor, 1)

        btns = QHBoxLayout()
        check_btn = QPushButton("校验 YAML")
        check_btn.clicked.connect(self._check_preset)
        btns.addWidget(check_btn)
        save_btn = QPushButton("保存预设")
        save_btn.clicked.connect(self._save_preset)
        btns.addWidget(save_btn)
        btns.addStretch()
        root.addLayout(btns)
        return w

    def _load_presets(self):
        presets = dc.list_presets(self._plugins_dir())
        current = self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems([p["name"] for p in presets])
        if current in [p["name"] for p in presets]:
            self.preset_combo.setCurrentText(current)
        self.preset_combo.blockSignals(False)
        self._log("发现 {} 个预设：{}".format(len(presets), ", ".join(p["name"] for p in presets) or "无"))
        self._refresh_preset_files()

    def _refresh_preset_files(self):
        name = self.preset_combo.currentText()
        files = []
        for p in dc.list_presets(self._plugins_dir()):
            if p["name"] == name:
                files = p["files"]
                break
        self.preset_file_combo.blockSignals(True)
        self.preset_file_combo.clear()
        self.preset_file_combo.addItems(files)
        self.preset_file_combo.blockSignals(False)
        self._load_preset()
        self._reload_backups()

    def _load_preset(self):
        name = self.preset_combo.currentText()
        fname = self.preset_file_combo.currentText()
        if not name or not fname:
            self._preset_path = None
            self.preset_editor.setPlainText("")
            self.preset_meta.setText("没有可编辑的预设文件")
            return
        self._preset_path = dc.preset_file(self._plugins_dir(), name, fname)
        try:
            self._preset_text = self._preset_path.read_text(encoding="utf-8")
        except Exception as e:
            self._preset_text = ""
            self._log("读取预设失败：{}".format(e))
        self.preset_editor.setPlainText(self._preset_text)
        m = dc.text_metrics(self._preset_text)
        ok, msg = dc.validate_yaml_text(self._preset_text)
        self.preset_meta.setText("{} · {} 字节 / {} 行 · {}".format(
            self._preset_path, m["bytes"], m["lines"], "YAML OK" if ok else ("YAML 错误：" + msg)))

    def _check_preset(self):
        ok, msg = dc.validate_yaml_text(self.preset_editor.toPlainText())
        if ok:
            QMessageBox.information(self, "校验通过", "YAML 语法 OK。")
        else:
            QMessageBox.warning(self, "校验失败", msg)
        self._log("预设校验：{}".format("通过" if ok else msg))

    def _save_preset(self):
        if self._preset_path is None:
            QMessageBox.warning(self, "未选择预设", "先选一个预设文件。")
            return
        text = self.preset_editor.toPlainText()
        if text == self._preset_text:
            self._log("预设无变化")
            return
        self._pre_write("Agent 预设：{}".format(self._preset_path.name))
        try:
            res = dc.save_text(self._preset_path, text, validator=dc.validate_yaml_text)
        except Exception as e:
            QMessageBox.critical(self, "写入失败", str(e))
            return
        self._preset_text = text
        self._log("预设已保存：{}；备份 {}".format(
            self._preset_path, Path(res["backup"]).name if res.get("backup") else "（无）"))
        self._load_preset()
        self._reload_backups()
        self._log("提示：预设改动通常需要重启 DSH 才生效")

    # ─────────────────────────────────────────────────────── 4. 备份与回滚

    def _build_backup_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(self._reload_backups)
        top.addWidget(refresh_btn)
        restore_btn = QPushButton("还原选中")
        restore_btn.clicked.connect(self._restore_selected)
        top.addWidget(restore_btn)
        top.addSpacing(16)
        top.addWidget(QLabel("每个文件保留"))
        self.backup_keep_spin = QSpinBox()
        self.backup_keep_spin.setRange(1, 200)
        self.backup_keep_spin.setValue(10)
        self.backup_keep_spin.setSuffix(" 份")
        top.addWidget(self.backup_keep_spin)
        scripts_btn = QPushButton("给快照补恢复脚本")
        scripts_btn.setToolTip("为已有的每一份快照生成同名「恢复_*.cmd」，换到没装 ZeroFall 的机器也能还原")
        scripts_btn.clicked.connect(self._write_restore_scripts)
        top.addWidget(scripts_btn)
        cleanup_btn = QPushButton("清理 .bak 残留")
        cleanup_btn.setToolTip("清掉 .dsh 下累积的 .bak-* / .zf-tmp / .zf-conflict（先预览再删）")
        cleanup_btn.clicked.connect(self._cleanup_junk)
        top.addWidget(cleanup_btn)
        prune_btn = QPushButton("清理旧备份")
        prune_btn.setToolTip("只删 ZeroFall 自己生成的 *.bak-zf-config-* / *.bak-before-*，你自己命名的 .bak 不动。")
        prune_btn.clicked.connect(self._prune_backups)
        top.addWidget(prune_btn)
        top.addStretch()
        root.addLayout(top)

        hint = QLabel(
            "这些是 ZeroFall 改配置时自动留下的改前副本（*.bak-zf-config-*），"
            "还原前会再给当前文件留一份 *.bak-before-restore-*，所以还原本身也可回退。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        self.backup_list = QListWidget()
        root.addWidget(self.backup_list, 1)
        return w

    def _managed_paths(self):
        paths = []
        if self._path is not None:
            paths.append(self._path)
        paths.append(dc.inject_path(self._plugins_dir()))
        for p in dc.list_presets(self._plugins_dir()):
            for f in p["files"]:
                paths.append(dc.preset_file(self._plugins_dir(), p["name"], f))
        return paths

    def _reload_backups(self):
        if not hasattr(self, "backup_list"):
            return
        rows = dc.list_backups(self._managed_paths())
        self.backup_list.clear()
        self._backup_rows = rows
        for r in rows:
            ts = datetime.fromtimestamp(r["mtime"]).strftime("%m-%d %H:%M:%S")
            item = QListWidgetItem("{time}   {kb:>8.1f} KB   {name}".format(
                time=ts, kb=r["size"] / 1024.0, name=r["name"]))
            item.setData(Qt.ItemDataRole.UserRole, r["path"])
            self.backup_list.addItem(item)
        if rows:
            self.backup_list.setCurrentRow(0)

    def _prune_backups(self):
        """按保留份数裁掉旧备份（备份只增不删会自己变成垃圾）"""
        keep = int(self.backup_keep_spin.value())
        res = dc.prune_backups(self._managed_paths(), keep=keep)
        n, freed = len(res["removed"]), res["freed_bytes"]
        if n:
            self._log("清理旧备份：删除 {} 份，回收 {:.1f} KB（每个文件保留 {} 份）".format(
                n, freed / 1024.0, keep))
            QMessageBox.information(self, "清理完成",
                                   "删除 {} 份旧备份，回收 {:.1f} KB。".format(n, freed / 1024.0))
        else:
            self._log("清理旧备份：没有超过 {} 份的文件，未删除".format(keep))
        self._reload_backups()

    def _restore_selected(self):
        item = self.backup_list.currentItem()
        if item is None:
            QMessageBox.information(self, "没有选中", "先选一条备份。")
            return
        bak = Path(item.data(Qt.ItemDataRole.UserRole))
        target = dc.target_of_backup(bak)
        if QMessageBox.question(
                self, "还原备份",
                "用这份备份覆盖：\n{}\n\n当前内容会先留一份 .bak-before-restore-*。".format(target),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("还原备份：{}".format(bak.name))
        try:
            res = dc.restore_backup(bak, target)
        except Exception as e:
            QMessageBox.critical(self, "还原失败", str(e))
            self._log("还原失败：{}".format(e))
            return
        self._log("已还原 {}（安全副本 {}）".format(res["target"], Path(res["safety"]).name if res["safety"] else "无"))
        self._load()
        self._load_inject()
        self._load_preset()


    # ─────────────────────────────────────────────────────── 5. 迁移与历史
    #
    # 对齐 GitHub 上 dsh-config-manager 的两条主线：
    #   导出（去密 + manifest + sha256）→ 导入（预览 / 冲突策略 / 路径映射）
    #   版本点（存一份 / 看 diff / 整体回滚）

    def _build_transfer_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(8)

        # ── 导出
        g1 = QGroupBox("导出配置包（跨机迁移 / 留档）")
        l1 = QVBoxLayout(g1)
        r1 = QHBoxLayout()
        self.export_path = QLineEdit(str(Path(self._plugins_dir()) / "zf-config-bundle.zip"))
        r1.addWidget(QLabel("目标"))
        r1.addWidget(self.export_path, 1)
        pick = QPushButton("另存为…")
        pick.clicked.connect(self._pick_export_path)
        r1.addWidget(pick)
        l1.addLayout(r1)

        r1b = QHBoxLayout()
        self.export_label = QLineEdit()
        self.export_label.setPlaceholderText("备注（可空），会写进清单")
        r1b.addWidget(QLabel("备注"))
        r1b.addWidget(self.export_label, 1)
        self.export_password = _QLineEdit()
        self.export_password.setEchoMode(_QLineEdit.EchoMode.Password)
        self.export_password.setPlaceholderText("可选：设密码 → 加密导出")
        self.export_password.setToolTip(
            "设了密码就用 scrypt + AES-256-GCM 加密整个包（连 manifest 一起）。"
            "密码不落盘，丢了就解不开。")
        self.export_password.setMaximumWidth(200)
        r1b.addWidget(self.export_password)
        self.export_secrets = QCheckBox("包含密钥（默认不含）")
        self.export_secrets.setToolTip("默认按字段名剥掉 apiKey/token/password 之类的值，包里只留掩码。")
        r1b.addWidget(self.export_secrets)
        export_btn = QPushButton("导出")
        export_btn.clicked.connect(self._export_bundle)
        r1b.addWidget(export_btn)
        l1.addLayout(r1b)
        root.addWidget(g1)

        # ── 导入
        g2 = QGroupBox("导入配置包（先预览再落盘）")
        l2 = QVBoxLayout(g2)
        r2 = QHBoxLayout()
        self.import_path = QLineEdit()
        self.import_path.setPlaceholderText("选择 .zip 配置包")
        r2.addWidget(QLabel("来源"))
        r2.addWidget(self.import_path, 1)
        browse = QPushButton("选择…")
        browse.clicked.connect(self._pick_import_path)
        r2.addWidget(browse)
        preview_btn = QPushButton("预览差异")
        preview_btn.clicked.connect(self._preview_import)
        r2.addWidget(preview_btn)
        l2.addLayout(r2)

        self.import_list = QListWidget()
        self.import_list.setMaximumHeight(150)
        l2.addWidget(self.import_list)

        r2b = QHBoxLayout()
        r2b.addWidget(QLabel("冲突策略"))
        self.import_policy = QComboBox()
        self.import_policy.addItems(["overwrite", "skip", "keep-both"])
        self.import_policy.setToolTip(
            "overwrite=覆盖本机；skip=只补缺失；keep-both=冲突的另存 *.imported，本机文件不动")
        r2b.addWidget(self.import_policy)
        r2b.addWidget(QLabel("路径映射"))
        self.map_src = QLineEdit()
        self.map_src.setPlaceholderText("源前缀，如 /Users/a")
        r2b.addWidget(self.map_src)
        r2b.addWidget(QLabel("→"))
        self.map_dst = QLineEdit()
        self.map_dst.setPlaceholderText("本机前缀，可空")
        r2b.addWidget(self.map_dst)
        apply_btn = QPushButton("应用到本机")
        apply_btn.clicked.connect(self._apply_import)
        r2b.addWidget(apply_btn)
        l2.addLayout(r2b)
        root.addWidget(g2)

        # ── 版本点
        g3 = QGroupBox("配置版本点（存一份 / 看差异 / 整体回滚）")
        l3 = QVBoxLayout(g3)
        r3 = QHBoxLayout()
        self.point_label = QLineEdit()
        self.point_label.setPlaceholderText("版本备注，如 before-upgrade")
        r3.addWidget(QLabel("备注"))
        r3.addWidget(self.point_label, 1)
        save_btn = QPushButton("存一个版本点")
        save_btn.clicked.connect(self._save_point)
        r3.addWidget(save_btn)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load_points)
        r3.addWidget(refresh_btn)
        l3.addLayout(r3)

        self.point_list = QListWidget()
        self.point_list.setMaximumHeight(130)
        l3.addWidget(self.point_list)

        r3b = QHBoxLayout()
        diff_btn = QPushButton("与当前对比")
        diff_btn.clicked.connect(self._diff_point)
        r3b.addWidget(diff_btn)
        restore_btn = QPushButton("回滚到选中版本点")
        restore_btn.clicked.connect(self._restore_point_ui)
        r3b.addWidget(restore_btn)
        r3b.addStretch()
        l3.addLayout(r3b)

        r3c = QHBoxLayout()
        r3c.addWidget(QLabel("打 tag"))
        self.tag_input = QLineEdit()
        self.tag_input.setPlaceholderText("如 known-good（打了 tag 的点不会被索引截断挤掉）")
        r3c.addWidget(self.tag_input, 1)
        tag_btn = QPushButton("打在选中点")
        tag_btn.clicked.connect(self._tag_selected_point)
        r3c.addWidget(tag_btn)
        r3c.addSpacing(16)
        r3c.addWidget(QLabel("A"))
        self.diff_a = QComboBox()
        self.diff_a.setMinimumWidth(150)
        r3c.addWidget(self.diff_a)
        r3c.addWidget(QLabel("B"))
        self.diff_b = QComboBox()
        self.diff_b.setMinimumWidth(150)
        r3c.addWidget(self.diff_b)
        ab_btn = QPushButton("A ↔ B 对比")
        ab_btn.setToolTip("两个历史版本点互 diff（不涉及当前磁盘）：查「中间那段到底改了什么」就靠它")
        ab_btn.clicked.connect(self._diff_two_points)
        r3c.addWidget(ab_btn)
        l3.addLayout(r3c)
        root.addWidget(g3)
        return w

    # ── 导出 / 导入

    def _pick_export_path(self):
        fp, _ = QFileDialog.getSaveFileName(self, "导出配置包",
                                            self.export_path.text() or "zf-config-bundle.zip",
                                            "ZIP (*.zip)")
        if fp:
            self.export_path.setText(fp)

    def _pick_import_path(self):
        fp, _ = QFileDialog.getOpenFileName(self, "选择配置包", "", "ZIP (*.zip)")
        if fp:
            self.import_path.setText(fp)

    def _export_bundle(self):
        dest = Path(self.export_path.text().strip() or "")
        if not dest.name:
            QMessageBox.warning(self, "缺少目标", "先选一个导出路径。")
            return
        self._pre_write("导出配置包")
        try:
            res = cb.export_bundle(Path(self._plugins_dir()), dest,
                                   profile=self.profile_combo.currentText() or "desktop",
                                   include_secrets=self.export_secrets.isChecked(),
                                   label=self.export_label.text().strip(),
                                   password=(self.export_password.text().strip() or None))
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
            self._log("导出失败：{}".format(e))
            return
        self._log("已导出 {} 份配置 → {}（{} KB，剥离密钥值 {} 处{}{}）".format(
            res["entries"], res["dest"], round(res["bytes"] / 1024, 1),
            res["redacted_values"], "，含密钥" if res["secrets_included"] else "",
            "，已加密" if res["manifest"].get("encrypted") else ""))

    def _ask_bundle_password(self, path: Path):
        """加密包要密码；返回密码字符串 / None"""
        if not cb.is_encrypted_bundle(path):
            return ""
        pw, ok = QInputDialog.getText(self, "加密配置包", "这个包是加密的，请输入密码：",
                                      _QLineEdit.EchoMode.Password)
        return pw if ok and pw else None

    def _read_import_rows(self):
        src = self.import_path.text().strip()
        if not src:
            QMessageBox.warning(self, "缺少来源", "先选一个配置包。")
            return None
        self._import_pw = self._ask_bundle_password(Path(src))
        if self._import_pw is None:
            self._log("导入已取消（未提供密码）")
            return None
        path_map = None
        if self.map_src.text().strip():
            path_map = (self.map_src.text().strip(), self.map_dst.text().strip())
        try:
            return cb.preview_import(Path(src), Path(self._plugins_dir()),
                                     path_map=path_map, password=self._import_pw)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            self._log("读取配置包失败：{}".format(e))
            return None

    def _preview_import(self):
        rows = self._read_import_rows()
        if rows is None:
            return
        self._import_rows = rows
        self.import_list.clear()
        for r in rows:
            mark = {"new": "＋ 新增", "same": "＝ 相同", "differs": "≠ 冲突"}.get(r["state"], r["state"])
            item = QListWidgetItem("{}   {}".format(mark, r["rel"]))
            item.setData(Qt.ItemDataRole.UserRole, r["rel"])
            if r["state"] == "differs" and r["diff"]:
                item.setToolTip(r["diff"][:800])
            self.import_list.addItem(item)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        self._log("预览：新增 {} / 相同 {} / 冲突 {}".format(
            counts.get("new", 0), counts.get("same", 0), counts.get("differs", 0)))

    def _apply_import(self):
        src = self.import_path.text().strip()
        if not src:
            QMessageBox.warning(self, "缺少来源", "先选一个配置包。")
            return
        rows = self._read_import_rows()
        if rows is None:
            return
        todo = [r for r in rows if r["state"] != "same"]
        if not todo:
            self._log("没有需要导入的内容（全部相同）")
            return
        policy = self.import_policy.currentText()
        if QMessageBox.question(
                self, "导入配置",
                "将按策略「{}」处理 {} 份配置。\\n导入前会自动打快照与版本点。继续？".format(policy, len(todo)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("导入配置包")
        try:
            cb.save_point(Path(self._plugins_dir()),
                          label="before-import", profile=self.profile_combo.currentText() or "desktop")
        except Exception as e:
            self._log("导入前版本点失败（继续）：{}".format(e))
        path_map = None
        if self.map_src.text().strip():
            path_map = (self.map_src.text().strip(), self.map_dst.text().strip())
        try:
            res = cb.apply_import(Path(src), Path(self._plugins_dir()),
                                  policy=policy, path_map=path_map,
                                  password=getattr(self, "_import_pw", "") or "")
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))
            self._log("导入失败：{}".format(e))
            return
        self._log("导入完成：写入 {} / 跳过 {} / 另存 {} / 错误 {}".format(
            len(res["written"]), len(res["skipped"]), len(res["kept_both"]), len(res["errors"])))
        for e in res["errors"][:5]:
            self._log("  错误：" + e)
        self._load()
        self._load_inject()
        self._load_preset()
        self._load_points()

    # ── 版本点

    def _load_points(self):
        rows = cb.list_points(Path(self._plugins_dir()))
        self.point_list.clear()
        self._points = rows
        for r in rows:
            label = r.get("label") or "（无备注）"
            tag = r.get("tag") or ""
            suffix = "  [tag: {}]".format(tag) if tag else ""
            self.point_list.addItem("{}   {}{}   {} 份".format(
                r["created_at"], label, suffix, r.get("files", 0)))
        if hasattr(self, "diff_a"):
            for combo in (self.diff_a, self.diff_b):
                keep = combo.currentData()
                combo.blockSignals(True)
                combo.clear()
                for r in rows:
                    nm = (r.get("tag") or r["id"])
                    combo.addItem("{}{}".format(nm, "" if r.get("tag") else "  " + r["created_at"]), r["id"])
                combo.blockSignals(False)
                if keep:
                    idx = combo.findData(keep)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
            if self.diff_b.count() > 1 and self.diff_b.currentIndex() == 0:
                self.diff_b.setCurrentIndex(1)
        if rows:
            self.point_list.setCurrentRow(0)

    def _save_point(self):
        try:
            meta = cb.save_point(Path(self._plugins_dir()),
                                 label=self.point_label.text().strip(),
                                 profile=self.profile_combo.currentText() or "desktop")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return
        self._log("已存版本点 {}（{} 份文件）".format(meta["id"], len(meta["entries"])))
        self._load_points()

    def _selected_point_id(self):
        row = self.point_list.currentRow()
        if row < 0 or row >= len(getattr(self, "_points", [])):
            return None
        return self._points[row]["id"]

    def _diff_point(self):
        pid = self._selected_point_id()
        if not pid:
            QMessageBox.information(self, "没有选中", "先选一个版本点。")
            return
        try:
            diff = cb.diff_point(Path(self._plugins_dir()), pid)
        except Exception as e:
            QMessageBox.critical(self, "对比失败", str(e))
            return
        self.log.clear()
        changed = [d for d in diff if d["state"] != "same"]
        if not changed:
            self.log.appendPlainText("（与版本点 {} 完全一致）".format(pid))
            return
        for d in changed:
            self.log.appendPlainText("### {}   {}".format(d["state"], d["path"]))
            if d["diff"]:
                self.log.appendPlainText(d["diff"])

    def _restore_point_ui(self):
        pid = self._selected_point_id()
        if not pid:
            QMessageBox.information(self, "没有选中", "先选一个版本点。")
            return
        if QMessageBox.question(
                self, "回滚到版本点",
                "把受管配置整体回到版本点 {}？\\n当前内容会先留 .bak-zf-config-* 备份。".format(pid),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("回滚到版本点")
        try:
            res = cb.restore_point(Path(self._plugins_dir()), pid)
        except Exception as e:
            QMessageBox.critical(self, "回滚失败", str(e))
            return
        self._log("已回滚到 {}：写入 {} 份，留备份 {} 份".format(
            pid, len(res["written"]), len(res["backups"])))
        self._load()
        self._load_inject()
        self._load_preset()


    # ─────────────────────────────────────────────────────── 6. profile 健康
    #
    # 对齐 imxzt/dsh-config-manager 的「7 项健康诊断 + 启动预检」：
    # 装插件时 pnpm 丢包 → bundles 里还留着名字 → 下次启动硬失败 → DSH 把 profile
    # 重置成模板。这套检查的目的就是在"重启之前"把这种情况喊出来。

    def _build_health_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        run_btn = QPushButton("运行诊断（7 项）")
        run_btn.clicked.connect(self._run_health)
        top.addWidget(run_btn)
        fix_btn = QPushButton("按建议修正")
        fix_btn.setToolTip("把第 2 项里\"声明了但磁盘没有\"的 bundle 从 dsh.profile.bundles 摘掉（依赖声明保留），"
                           "这就是 profile 被重置的直接成因。改前自动存版本点与 .bak。")
        fix_btn.clicked.connect(self._neutralize_bundles)
        top.addWidget(fix_btn)
        up_btn = QPushButton("升级预览")
        up_btn.setToolTip("查 npm 上的最新版本，列出升级后可能掉队的插件（只读，不动文件）")
        up_btn.clicked.connect(self._run_upgrade_preview)
        top.addWidget(up_btn)
        tl_btn = QPushButton("故障时间线")
        tl_btn.setToolTip("上次成功启动以来到底变了什么（崩溃归因基线）")
        tl_btn.clicked.connect(self._show_timeline)
        top.addWidget(tl_btn)
        open_btn = QPushButton("打开 profile 目录")
        open_btn.clicked.connect(lambda: self._open_file(
            Path(self._plugins_dir()) / "profiles" / (self.profile_combo.currentText() or "desktop")))
        top.addWidget(open_btn)
        top.addStretch()
        root.addLayout(top)

        self.health_verdict = QLabel("尚未运行")
        self.health_verdict.setWordWrap(True)
        root.addWidget(self.health_verdict)

        hint = QLabel(
            "这七项都对应真实事故：manifest 带 BOM / 声明的 bundle 磁盘上没有 / "
            "bundles 与依赖不一致 / profile 被重撰 / 锁文件不同步 / file: 联接被换成实体副本 / "
            "patch 里有悬空 insert。有 error 时不要重启 DSH —— 重启会触发 profile 重置。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        self.health_list = QListWidget()
        root.addWidget(self.health_list, 1)
        return w

    def _run_health(self):
        profile = self.profile_combo.currentText() or "desktop"
        try:
            rep = ph.run_all(Path(self._plugins_dir()), profile)
        except Exception as e:
            QMessageBox.critical(self, "诊断失败", str(e))
            return
        self._health_report = rep
        self.health_verdict.setText(rep.get("verdict", ""))
        self.health_list.clear()
        icon = {"ok": "✓", "warn": "!", "error": "✗", "info": "·"}
        for c in rep.get("checks", []):
            text = "{}  {}    {}".format(icon.get(c["level"], "?"), c["title"], c["detail"])
            item = QListWidgetItem(text)
            if c.get("fix"):
                item.setToolTip("建议：" + c["fix"])
            self.health_list.addItem(item)
            if c.get("fix") and c["level"] in ("warn", "error"):
                self.health_list.addItem("      → " + c["fix"])
        s = rep.get("summary", {})
        self._log("profile 健康：错误 {} / 警告 {} / 提示 {} / 通过 {} —— {}".format(
            s.get("error", 0), s.get("warn", 0), s.get("info", 0), s.get("ok", 0),
            rep.get("verdict", "")))


    def _neutralize_bundles(self):
        """摘除不可解析 bundle：事故直接成因的那一刀"""
        home = Path(self._plugins_dir())
        profile = self.profile_combo.currentText() or "desktop"
        try:
            targets = ph.unresolvable_bundles(home, profile)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", str(e))
            return
        if not targets:
            QMessageBox.information(self, "无需处理", "没有\"声明了但磁盘没有\"的 bundle。")
            self._log("按建议修正：没有目标")
            return
        if QMessageBox.question(
                self, "摘除不可解析 bundle",
                "将从 dsh.profile.bundles 摘掉：\n\n{}\n\n依赖声明保留，包修好后可再加回。\n"
                "改前会自动存版本点与 .bak-zf-config-*。继续？".format("、".join(targets)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("摘除不可解析 bundle")
        try:
            cb.save_point(home, label="before-neutralize", profile=profile)
        except Exception as e:
            self._log("版本点保存失败（继续）：{}".format(e))
        try:
            res = ph.neutralize_bundles(home, profile, targets, dry_run=False)
        except Exception as e:
            QMessageBox.critical(self, "修正失败", str(e))
            self._log("摘除失败：{}".format(e))
            return
        self._log("已摘除 {} 个 bundle；备份 {}".format(len(res.get("removed", [])),
                                                   Path(res["backup"]).name if res.get("backup") else "无"))
        self._run_health()
        self._load()

    # ─────────────────────────────────────────────────────── 7. 会话修复
    #
    # 对齐 imxzt/dsh-config-manager 的 F 段：两种损坏可修（单帧布局、seq 重叠），
    # 其余（非法 JSON、帧结构损坏）只报不修。修前必备份、修后三重校验。

    def _build_session_tab(self):
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 6, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        scan_btn = QPushButton("扫描全部会话")
        scan_btn.clicked.connect(self._scan_sessions)
        top.addWidget(scan_btn)
        fix_btn = QPushButton("修复可修项")
        fix_btn.setToolTip("只修两种：整份压成一帧（拆帧）、seq 重复（去重）。非法 JSON/帧损坏只报不修。")
        fix_btn.clicked.connect(self._repair_sessions)
        top.addWidget(fix_btn)
        top.addStretch()
        root.addLayout(top)

        self.session_verdict = QLabel("尚未扫描")
        self.session_verdict.setWordWrap(True)
        root.addWidget(self.session_verdict)

        hint = QLabel(
            "会话文件是 session.v4.jsonl.zstd（一堆 zstd 帧首尾相接）。DSH 的写入器是"
            "「一条记录一帧」，整份压成一帧会让增量追加/恢复对不上；崩溃恢复留下的合成收尾帧"
            "会造成 seq 重复。修复前都会留 .bak-<时间戳>，修完再校验一次（能解、每行合法 JSON、无重复 seq）。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)

        self.session_list = QListWidget()
        root.addWidget(self.session_list, 1)
        return w

    def _scan_sessions(self):
        home = Path(self._plugins_dir())
        try:
            res = sr.scan_all(home, sr.find_node(self._plugins_dir()))
        except Exception as e:
            QMessageBox.critical(self, "扫描失败", str(e))
            return
        self._session_scan = res
        s = res["summary"]
        self.session_verdict.setText("文件 {} 个 · 正常 {} · 单帧 {} · seq 重复 {} · 非法 JSON {} · 帧损坏 {}".format(
            res["total"], s.get("ok", 0), s.get("legacy_single_frame", 0),
            s.get("seq_overlap", 0), s.get("invalid_json", 0), s.get("frame_error", 0)))
        self.session_list.clear()
        for row in res["files"]:
            label = sr.STATE_LABEL.get(row["state"], row["state"])
            self.session_list.addItem("{}    {} 帧 / {} 行    {}".format(
                label, row.get("frames", 0), row.get("lines", 0), row.get("name") or row.get("path")))
        self._log("会话扫描：{} 个文件，可修 {} 个".format(res["total"], res["repairable"]))

    def _repair_sessions(self):
        home = Path(self._plugins_dir())
        node = sr.find_node(self._plugins_dir())
        try:
            dry = sr.repair_all(home, node, apply=False)
        except Exception as e:
            QMessageBox.critical(self, "预检失败", str(e))
            return
        if not dry["would"]:
            QMessageBox.information(self, "无需修复", "没有可修的会话文件（单帧布局 / seq 重复）。")
            self._log("会话修复：无目标")
            return
        if QMessageBox.question(
                self, "修复会话文件",
                "将有 {} 个会话文件被重写（其余只报不修）。\n每个文件修复前会留 .bak-<时间戳>。继续？".format(dry["would"]),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        self._pre_write("修复会话文件")
        try:
            res = sr.repair_all(home, node, apply=True)
        except Exception as e:
            QMessageBox.critical(self, "修复失败", str(e))
            return
        self._log("会话修复：修好 {} / 失败 {}".format(res["repaired"], res["errors"]))
        for r in res["results"]:
            if r.get("action") == "error":
                self._log("  失败：" + str(r.get("message"))[:160])
        self._scan_sessions()


    def _tag_selected_point(self):
        pid = self._selected_point_id()
        if not pid:
            QMessageBox.information(self, "没有选中", "先选一个版本点。")
            return
        tag = self.tag_input.text().strip()
        if not tag:
            QMessageBox.warning(self, "tag 为空", "给个名字，比如 known-good / before-upgrade。")
            return
        try:
            res = cb.tag_point(Path(self._plugins_dir()), pid, tag)
        except Exception as e:
            QMessageBox.critical(self, "打 tag 失败", str(e))
            return
        self._log("已把 {} 标记为「{}」（tag 点不会被索引截断挤掉）".format(res["id"], res["tag"]))
        self._load_points()

    def _diff_two_points(self):
        a = self.diff_a.currentData() if hasattr(self, "diff_a") else None
        b = self.diff_b.currentData() if hasattr(self, "diff_b") else None
        if not a or not b:
            QMessageBox.information(self, "缺少版本点", "至少要有两个版本点才能互比。")
            return
        try:
            res = cb.diff_points(Path(self._plugins_dir()), a, b)
        except Exception as e:
            QMessageBox.critical(self, "对比失败", str(e))
            return
        st = res["summary"]
        self.log.clear()
        self.log.appendPlainText("A {} → B {}：相同 {} / 改动 {} / 新增 {} / 删除 {}".format(
            res["a"], res["b"], st["same"], st["modified"], st["added"], st["removed"]))
        for row in res["rows"]:
            if row["state"] == "same":
                continue
            self.log.appendPlainText("### {}   {}".format(row["state"], row["path"]))
            if row["diff"]:
                self.log.appendPlainText(row["diff"])
        self._log("两点互 diff：{} 处不同".format(res["changed"]))

    def _run_upgrade_preview(self):
        try:
            res = up.preview(Path(self._plugins_dir()),
                             self.profile_combo.currentText() or "desktop",
                             config=self._config if hasattr(self, "_config") else None)
        except Exception as e:
            QMessageBox.critical(self, "升级预览失败", str(e))
            return
        self.log.clear()
        for line in up.format_lines(res):
            self.log.appendPlainText(line)
        s2 = res.get("summary", {})
        self._log("升级预览：{} 个依赖，落后 {} / npm 查不到 {} / 本地源 {}".format(
            s2.get("total", 0), s2.get("behind", 0), s2.get("not_on_npm", 0), s2.get("local", 0)))

    def _show_timeline(self):
        try:
            res = ftl.since_last_ok()
        except Exception as e:
            QMessageBox.critical(self, "读取时间线失败", str(e))
            return
        self.log.clear()
        for line in ftl.format_lines(res):
            self.log.appendPlainText(line)
        self._log("故障时间线：自上次成功启动以来 {} 条".format(res.get("count", 0)))

    def _write_restore_scripts(self):
        """给已有快照批量补独立恢复脚本"""
        sm = getattr(self, "snapshot_manager", None)
        if sm is None:
            QMessageBox.information(self, "不可用", "当前没有快照管理器实例。")
            return
        try:
            stats = sm.write_restore_scripts()
        except Exception as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        self._log("恢复脚本：新生成 {} 个，已存在 {} 个，快照文件缺失 {}".format(
            stats.get("written", 0), stats.get("skipped", 0), stats.get("missing", 0)))

    def _cleanup_junk(self):
        """清理 .bak / .zf-tmp / .zf-conflict 残渣（先预览）"""
        from modules import housekeeping as hk
        home = Path(self._plugins_dir())
        try:
            prev = hk.delete(home, dry_run=True)
        except Exception as e:
            QMessageBox.critical(self, "扫描失败", str(e))
            return
        if not prev["candidates"]:
            QMessageBox.information(self, "很干净", "没找到可清理的备份残渣。")
            return
        detail = "、".join("{} {} 个".format(k, v) for k, v in prev["by_kind"].items())
        if QMessageBox.question(
                self, "清理备份残渣",
                "找到 {} 个文件，共 {:.2f} MB（{}）。\n\n删除后不可恢复；如需保守，先取消，"
                "改成按天数筛选。\n继续删除？".format(prev["candidates"], prev["candidate_mb"], detail),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        res = hk.delete(home, dry_run=False)
        self._log("清理残渣：删除 {} 个，回收 {:.2f} MB{}".format(
            res["removed"], res["freed_mb"],
            "，失败 {} 个".format(len(res["errors"])) if res["errors"] else ""))
