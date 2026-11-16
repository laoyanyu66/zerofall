# -*- coding: utf-8 -*-
"""会话续跑页签 —— 把「崩溃/重启后关闭前在跑的会话」接上

三块信息：
  1. 插件是否在位（vendor / 依赖 / node_modules 链接 / patch 行 + enabled）
     —— 修复动作最容易把这行冲掉，冲掉就静默不恢复；
  2. 快照里「关闭前在跑的」是谁（只认 status=running / goal active）；
  3. DSH 正在跑的话，直接看它自己的 /resume-sessions/state，并可一键补跑。

设计上刻意不做「全量恢复历史会话」：候选语义与插件侧 pick() 一致，idle 的历史会话不进表。
"""
import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit, QVBoxLayout, QWidget,
    QFileDialog,
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from modules import dsh_config as dc          # noqa: E402
from modules import resume_guard as rg       # noqa: E402
from ui.dialogs import ZFMessageBox as QMessageBox  # noqa: E402

_COLUMNS = ("会话", "标题", "状态", "未完成的 turn", "日志 MB", "下次恢复", "最近活动")


class _Worker(QThread):
    """把可能阻塞的动作（HTTP 补跑、探测端口）挪出 UI 线程。"""

    done = pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):  # noqa: D102
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住
            result = {"error": str(exc)}
        self.done.emit(result)


class ResumeTab(QWidget):
    """会话续跑标签页"""

    def __init__(self, config, snapshot_manager, parent=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self._worker = None
        self._overview = {}
        self._last_rows_sig = ""
        self._init_ui()
        self._refresh()
        # 自动刷新：dsh 里的会话随时开始/结束，页签不刷就永远是打开那一刻的旧数据
        # （这就是"我明明在跑，续跑页却没显示"的直接原因）。
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(5000)
        self._auto_timer.timeout.connect(self._refresh_silent)
        self._auto_timer.start()

    # ── UI ──────────────────────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Profile："))
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(160)
        self.profile_box.currentTextChanged.connect(lambda _t: self._refresh())
        top.addWidget(self.profile_box)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh)
        top.addWidget(refresh_btn)

        self.auto_check = QCheckBox("自动刷新（5s）")
        self.auto_check.setChecked(True)
        self.auto_check.setToolTip("dsh 里的会话随时开始/结束；关掉的话，这一页只在你手动点「刷新」时更新")
        self.auto_check.toggled.connect(self._on_auto_toggle)
        top.addWidget(self.auto_check)

        self.refresh_label = QLabel("")
        self.refresh_label.setStyleSheet("color: gray; font-size: 11px;")
        top.addWidget(self.refresh_label)

        heal_btn = QPushButton("自愈插件")
        heal_btn.setToolTip("把 dsh-resume-sessions 装回 profile：vendor 源码 + 依赖声明 + node_modules 链接 + patch 行（enabled: true）")
        heal_btn.clicked.connect(self._on_heal)
        top.addWidget(heal_btn)

        backup_btn = QPushButton("备份快照")
        backup_btn.setToolTip("把 ~/.dsh/resume-sessions.json 复制一份带时间戳的备份，修复动作前建议先点")
        backup_btn.clicked.connect(self._on_backup)
        top.addWidget(backup_btn)

        restore_btn = QPushButton("还原快照…")
        restore_btn.setToolTip("从 .bak-zf-resume-* 备份里选一个还原（修复把快照冲掉时用）")
        restore_btn.clicked.connect(self._on_restore_backup)
        top.addWidget(restore_btn)

        top.addStretch()
        layout.addLayout(top)

        self.status_box = QGroupBox("状态")
        status_layout = QVBoxLayout(self.status_box)
        self.status_label = QLabel("（加载中）")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.status_label)
        layout.addWidget(self.status_box)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 3)

        mid = QHBoxLayout()
        # 这里原来有个"使用已运行的 DSH 端点（自动找端口）"复选框，但它没有任何代码
        # 读它的值 —— 页签永远自动探测本机在跑的 DSH（resume_guard.discover_ports），
        # 勾不勾都一样。留着等于承诺一个不存在的选项，换成一句说明。
        mid.addWidget(QLabel("端点：自动探测本机在跑的 DSH"))

        probe_btn = QPushButton("读端点状态")
        probe_btn.clicked.connect(self._on_probe)
        mid.addWidget(probe_btn)

        trigger_btn = QPushButton("触发补跑")
        trigger_btn.setToolTip("POST /resume-sessions/restore —— 只重试插件认为该恢复的那批（不会全量重开）")
        trigger_btn.clicked.connect(self._on_trigger)
        mid.addWidget(trigger_btn)

        open_btn = QPushButton("打开快照目录")
        open_btn.clicked.connect(self._on_open_dir)
        mid.addWidget(open_btn)

        doubao_btn = QPushButton("交给豆包分析")
        doubao_btn.setToolTip("拉起豆包 → 新对话 → 发送崩溃现场（退出码/日志路径/profile），用来排查「为什么没恢复」")
        doubao_btn.clicked.connect(self._on_doubao)
        mid.addWidget(doubao_btn)

        mid.addStretch()
        layout.addLayout(mid)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        self.log.setPlaceholderText("操作日志")
        layout.addWidget(self.log)

    # ── 数据 ────────────────────────────────────────────────────────────

    def _plugins_dir(self) -> str:
        return getattr(self.config, "deepseek_plugins_dir", "") or ""

    def _fill_profiles(self):
        current = self.profile_box.currentText()
        names = []
        try:
            names = dc.list_profiles(self._plugins_dir())
        except Exception:  # noqa: BLE001
            names = []
        if not names:
            names = ["desktop"]
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItems(names)
        if current in names:
            self.profile_box.setCurrentText(current)
        self.profile_box.blockSignals(False)

    def _profile(self) -> str:
        return self.profile_box.currentText() or "desktop"

    def _on_auto_toggle(self, on: bool):
        if on:
            self._auto_timer.start()
            self._refresh_silent()
        else:
            self._auto_timer.stop()

    def _refresh(self):
        """完整刷新：重建 profile 列表 + 读数据。"""
        self._fill_profiles()
        self._load_overview()

    def _refresh_silent(self):
        """定时刷新：不动 profile 下拉（否则会把用户正在选的项刷掉），只更新数据。"""
        if not self.isVisible():
            return
        if self._worker is not None:
            return
        self._load_overview()

    def _load_overview(self):
        try:
            data = rg.overview(self.config, self._profile())
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"读取失败：{exc}")
            return
        self._overview = data
        self._render_status(data)
        # 列全部会话（含已结束）：只列"要恢复的"会让人以为"没检测到我的会话"
        self._render_rows(data.get("recent") or data.get("pending") or [])
        try:
            import datetime as _dt
            self.refresh_label.setText("已刷新 " + _dt.datetime.now().strftime("%H:%M:%S"))
        except Exception:  # noqa: BLE001
            pass

    def _render_status(self, data):
        st = data.get("plugin") or {}
        guard = data.get("guard") or {}
        last = guard.get("last_restored") if isinstance(guard.get("last_restored"), dict) else None
        patch = {"missing": "缺条目", "disabled": "被禁用", "enabled": "已启用"}.get(st.get("patch"), "未知")
        ready = "就绪" if st.get("ready") else "未就绪"
        icon = "✅" if st.get("ready") else "⚠️"
        port = data.get("port")
        endpoint = "可达" if data.get("endpoint_ok") else ("不可达" if port else "未找到在跑的 DSH")
        last_text = "无"
        if last:
            last_text = "{}（{}）".format(last.get("title") or last.get("sessionId") or "?",
                                          self._fmt_time(last.get("at")))
        cand = data.get("candidate_count", len(data.get("pending") or []))
        quota = data.get("max_sessions", "?")
        ep_cand = data.get("endpoint_candidates")
        mismatch = data.get("candidates_mismatch") or []
        cand_text = f"关闭前在跑 {cand} 个，插件本次最多恢复 {quota} 个"
        if data.get("endpoint_ok") and ep_cand is not None:
            cand_text += f"（插件端点自报 {len(ep_cand)} 个）"
        else:
            cand_text += "（本地复算，未连上端点）"
        if mismatch:
            cand_text += ("｜⚠️ 与端点口径不一致（宿主里跑的插件代码较旧，DSH 重启后生效）："
                          + ", ".join(x[:12] for x in mismatch[:6]))
        lines = [
            f"{icon} 插件 {ready} —— patch: {patch}｜vendor: {'有' if st.get('vendor') else '无'}"
            f"｜依赖声明: {'有' if st.get('dependency') else '无'}"
            f"｜node_modules 链接: {'有' if st.get('node_modules_present') else '无'}",
            f"快照：{data.get('snapshot_file')}（共 {guard.get('total', 0)} 条记录，"
            f"{cand_text}）"
            f"｜崩溃计数 {guard.get('crashes', 0)}"
            f"｜恢复阶段 {guard.get('phase') or '未记录'}｜上次恢复中断: {'是' if guard.get('restoring') else '否'}",
            f"上次恢复的会话：{last_text}",
            f"DSH 端点：{('127.0.0.1:' + str(port)) if port else '未发现'}（{endpoint}）｜插件源码：{data.get('plugin_payload')}",
        ]
        self.status_label.setText("\n".join(lines))

    def _render_rows(self, rows):
        # 签名没变就不重建：5 秒一次的自动刷新不该把正在看的行刷掉/闪一下
        sig = "|".join(sorted(
            "{0}:{1}:{2}:{3}:{4}".format(r.get("session_id"), r.get("status"), r.get("running"),
                                         r.get("open_turn"), r.get("will_restore"))
            for r in rows))
        if sig == self._last_rows_sig:
            return
        self._last_rows_sig = sig
        self.table.setRowCount(0)
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            # 状态列说实话：快照里的 status 可能是 live（恢复过）/disposed，但"正在跑"才是用户关心的
            status_text = row.get("status", "")
            if row.get("blank"):
                status_text = "空白会话"
            elif row.get("running"):
                status_text = "在跑" if status_text in ("", "live", "running") else "{}（在跑）".format(status_text)
            elif status_text in ("disposed", "idle", "live", ""):
                status_text = "已结束"
            vals = (
                row.get("session_id", ""),
                row.get("title") or "（无标题）",
                status_text,
                "是" if row.get("open_turn") else "否",
                row.get("log_mb", ""),
                "是" if row.get("will_restore") else "否",
                self._fmt_time(row.get("running_at")),
            )
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, c, item)
        if not rows:
            self._append_log("当前没有「关闭前在跑」的会话（idle 的历史会话不会出现在这里）")

    @staticmethod
    def _fmt_time(ms) -> str:
        try:
            import datetime as _dt
            return _dt.datetime.fromtimestamp(int(ms) / 1000).strftime("%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return "-"

    def _append_log(self, text: str):
        self.log.append(text)

    # ── 动作 ────────────────────────────────────────────────────────────

    def _on_heal(self):
        try:
            rep = rg.ensure_plugin(
                rg.resolve_home(self.config), self._profile(), host_llm=None, force=False,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "自愈失败", str(exc))
            return
        actions = rep.get("actions") or []
        errors = rep.get("errors") or []
        warns = rep.get("warnings") or []
        if errors:
            QMessageBox.warning(self, "自愈未完成", "\n".join(errors))
        elif actions:
            QMessageBox.information(self, "已自愈", "；".join(str(a) for a in actions))
        else:
            QMessageBox.information(self, "无需自愈", "插件已在位且 enabled: true")
        for a in actions:
            self._append_log(f"自愈：{a}")
        for w in warns:
            self._append_log(f"注意：{w}")
        self._refresh()

    def _on_backup(self):
        try:
            bak = rg.backup_snapshot(rg.resolve_home(self.config))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "备份失败", str(exc))
            return
        if bak:
            self._append_log(f"已备份快照 → {bak}")
            QMessageBox.information(self, "已备份", str(bak))
        else:
            QMessageBox.information(self, "没有可备份的快照", str(rg.snapshot_path(rg.resolve_home(self.config))))

    def _on_restore_backup(self):
        home = rg.resolve_home(self.config)
        start = str(Path(home))
        path, _ = QFileDialog.getOpenFileName(self, "选择 resume-sessions 备份", start, "备份 (*.bak-zf-resume-*)")
        if not path:
            return
        ok = rg.restore_snapshot_backup(home, Path(path))
        if ok:
            self._append_log(f"已还原快照 ← {path}")
            self._refresh()
        else:
            QMessageBox.warning(self, "还原失败", path)

    def _on_probe(self):
        # 强制重扫（端口缓存 30 秒，点按钮时必须拿最新的），并把端点真答没答写清楚。
        ports = rg.discover_ports(force=True)
        answered = []
        for port in ports:
            if rg.probe_state(port) is not None:
                answered.append(port)
        self._append_log(
            "探测端点：候选端口 {}｜有响应 {}".format(
                ports or "无", answered or "无"
            )
        )
        self._refresh()

    def _on_trigger(self):
        port = self._overview.get("port")
        if not port:
            QMessageBox.information(self, "没找到 DSH", "没有发现正在运行的 DSH host，无法触发补跑。\n"
                                                        "插件会在下次 DSH 启动时按快照自动恢复。")
            return
        if self._worker is not None:
            return
        self._append_log(f"触发补跑：POST http://127.0.0.1:{port}/resume-sessions/restore")
        self._worker = _Worker(lambda: rg.trigger_restore(port, timeout=90.0), self)
        self._worker.done.connect(self._on_trigger_done)
        self._worker.start()

    def _on_trigger_done(self, result):
        self._worker = None
        if not result or result.get("error"):
            self._append_log(f"补跑失败：{(result or {}).get('error', '端点无响应')}")
            return
        report = (result or {}).get("report") or {}
        self._append_log(
            "补跑完成：live={} armed={} continued={} failed={} skipped={}".format(
                len(report.get("live") or []), len(report.get("armed") or []),
                len(report.get("continued") or []), len(report.get("failed") or []),
                len(report.get("skipped") or []),
            )
        )
        self._refresh()

    def _on_open_dir(self):
        home = rg.resolve_home(self.config)
        try:
            os.startfile(str(home))  # noqa: S606 - 用户主动打开
        except OSError as exc:
            self._append_log(f"打开目录失败：{exc}")

    def _on_doubao(self):
        """把当前现场（快照 + 崩溃日志路径）交给豆包，让它查"为什么没恢复"。"""
        try:
            from modules import doubao_relay as dr
        except Exception as exc:          # noqa: BLE001
            QMessageBox.warning(self, "豆包接力不可用", str(exc))
            return
        if self._worker is not None:
            return
        appdata = getattr(self.config, "dsh_appdata_dir", "") or ""
        crash_dir = str(Path(appdata) / "logs") if appdata else ""
        crash_log = ""
        try:
            from modules.crash_classifier import read_latest_crash_log
            path, _text = read_latest_crash_log(appdata)
            crash_log = str(path) if path else ""
        except Exception:                 # noqa: BLE001
            crash_log = ""
        exe_hint = getattr(self.config, "doubao_exe", "") or ""
        prompt_tpl = getattr(self.config, "doubao_relay_prompt", "") or ""

        def _run():
            # 先备份快照：豆包分析期间别让后续动作把现场冲掉
            try:
                rg.backup_snapshot(rg.resolve_home(self.config), tag="zf-resume-predoubao")
            except Exception:             # noqa: BLE001
                pass
            return dr.relay(exit_code=None, crash_log=crash_log, crash_dir=crash_dir,
                            profile=self._profile(), prompt_template=prompt_tpl, exe_hint=exe_hint)

        self._append_log("正在把现场交给豆包…")
        self._worker = _Worker(_run, self)
        self._worker.done.connect(self._on_doubao_done)
        self._worker.start()

    def _on_doubao_done(self, result):
        self._worker = None
        if not isinstance(result, dict):
            self._append_log("豆包接力：无响应")
            return
        if result.get("error"):
            self._append_log(f"豆包接力失败：{result['error']}（提示词已尽量留在剪贴板）")
        else:
            self._append_log("豆包接力：" + " → ".join(result.get("steps") or []))
