# -*- coding: utf-8 -*-
"""云端快照页

为什么单独一页：
  「云端同步」页里的列表只按文件大小猜类型（>100MB 就算完整），
  而且完全不读 .meta.json —— 既显示不出真实类型，也看不到插件数、
  状态、快照 ID，更发现不了"加密包和元数据对不上"这种实际存在的脏数据。

  这一页以 .meta.json 为准，把云端快照的真实清单摊开：
     时间 / 类型 / 大小 / 插件数 / 状态 / 加密包 / 元数据 / 快照ID
  并把对不上的情况直接标出来（缺加密包=不可恢复，缺元数据=信息不全）。

  支持三件事：解密导入到本地快照库、删除云端快照、清理孤儿元数据。
"""
import json
import time
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QAbstractItemView,
)
# 统一风格对话框：外观与主界面同源，调用形状与 QMessageBox 一致
from ui.dialogs import ZFMessageBox as QMessageBox

# 加密密码按机器生成，见 modules/cloud_crypto（不再写死）


def scan_cloud(sync_dir):
    """扫描云同步目录，返回 {items, stats}

    以 .meta.json 为主索引；同时把只有 .enc 没有 meta、
    以及只有 meta 没有 .enc 的情况都统计出来。

    纯函数、不碰 UI，便于单测。
    """
    out = {'items': [], 'stats': {
        'total': 0, 'recoverable': 0, 'meta_only': 0,
        'light': 0, 'full': 0,
        'disk_mb': 0.0, 'declared_mb': 0.0,
        'orphan_meta': 0, 'missing_meta': 0, 'last_sync': '',
        'dir': str(sync_dir or ''), 'exists': False,
    }}
    d = Path(str(sync_dir or ''))
    out['stats']['exists'] = d.is_dir()
    if not d.is_dir():
        return out

    encs = {}
    for f in d.glob('snapshot_*.zip.enc'):
        try:
            encs[f.name[:-len('.enc')]] = f          # snapshot_x.zip -> path
        except Exception:
            pass
    plains = {}
    for f in d.glob('snapshot_*.zip'):
        try:
            plains[f.name] = f
        except Exception:
            pass

    metas = {}
    for f in d.glob('snapshot_*.meta.json'):
        base = f.name[:-len('.meta.json')]           # snapshot_x.zip
        metas[base] = f

    seen = set()
    for base, meta_path in metas.items():
        doc = {}
        try:
            doc = json.loads(meta_path.read_text(encoding='utf-8')) or {}
        except Exception:
            doc = {}
        enc_path = encs.get(base) or plains.get(base)
        item = {
            'base': base,
            'snapshot_id': doc.get('snapshot_id') or base.replace('.zip', ''),
            'created_at': str(doc.get('created_at') or ''),
            'is_full': bool(doc.get('is_full')),
            'size_mb': float(doc.get('size_mb') or 0.0),
            'plugin_count': int(doc.get('plugin_count') or 0),
            'status': str(doc.get('status') or ''),
            'md5': str(doc.get('md5_checksum') or ''),
            'notes': str(doc.get('notes') or ''),
            'meta_path': meta_path,
            'data_path': enc_path,
            'has_data': enc_path is not None,
            'has_meta': True,
            'actual_mb': (enc_path.stat().st_size / 1048576.0) if enc_path else 0.0,
            'mtime': meta_path.stat().st_mtime,
        }
        out['items'].append(item)
        seen.add(base)

    # 只有 .enc / .zip，没有元数据：信息不全但能解密恢复
    for base, p in list(encs.items()) + list(plains.items()):
        if base in seen:
            continue
        out['items'].append({
            'base': base,
            'snapshot_id': base.replace('.zip', ''),
            'created_at': '',
            'is_full': False,
            'size_mb': 0.0,
            'plugin_count': 0,
            'status': '未知',
            'md5': '',
            'notes': '缺少元数据',
            'meta_path': None,
            'data_path': p,
            'has_data': True,
            'has_meta': False,
            'actual_mb': p.stat().st_size / 1048576.0,
            'mtime': p.stat().st_mtime,
        })

    # 只有 meta、没有数据文件 = 孤儿
    for base, meta_path in metas.items():
        if base not in encs and base not in plains:
            out['stats']['orphan_meta'] += 1

    items = out['items']
    items.sort(key=lambda x: (x['created_at'] or '', x['mtime']), reverse=True)
    st = out['stats']
    st['total'] = len(items)
    newest = 0.0
    for it in items:
        # 只有真正拿到数据文件才算"可恢复"
        if it['has_data']:
            st['recoverable'] += 1
            if it['is_full']:
                st['full'] += 1
            else:
                st['light'] += 1
        else:
            st['meta_only'] += 1
        # 实际占用只算磁盘上真实存在的字节；声明大小单独统计，
        # 否则一堆只剩元数据的孤儿会把"占用"虚报成几十 GB
        st['disk_mb'] += it['actual_mb']
        st['declared_mb'] += (it['size_mb'] or 0.0)
        if not it['has_meta']:
            st['missing_meta'] += 1
        if it['mtime'] > newest:
            newest = it['mtime']
    st['disk_mb'] = round(st['disk_mb'], 1)
    st['declared_mb'] = round(st['declared_mb'], 1)
    if newest > 0:
        st['last_sync'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                        time.localtime(newest))
    return out


class CloudSnapshotsTab(QWidget):
    """云端快照：查看 / 恢复 / 删除 / 清理"""

    def __init__(self, config, snapshot_manager=None, parent=None,
                 main_window=None):
        super().__init__(parent)
        self.config = config
        self.snapshot_manager = snapshot_manager
        self.main_window = main_window
        self._items = []
        self._init_ui()
        self.refresh()

    # ── UI ────────────────────────────────────────────────
    def _init_ui(self):
        from ui import theme as th
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(th.header(
            "云端快照", "云同步目录里的快照清单（以 .meta.json 为准）"))

        info = QGroupBox("云端快照总览")
        il = QVBoxLayout()
        self.path_label = QLabel("目录: -")
        self.path_label.setWordWrap(True)
        il.addWidget(self.path_label)
        self.stats_label = QLabel("尚未扫描")
        self.stats_label.setWordWrap(True)
        self.stats_label.setStyleSheet("font-size: 12px;")
        il.addWidget(self.stats_label)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        self.open_btn = QPushButton("打开目录")
        self.open_btn.clicked.connect(self._on_open_dir)
        row.addWidget(self.open_btn)
        self.import_btn = QPushButton("解密并导入本地")
        self.import_btn.clicked.connect(self._on_import_selected)
        row.addWidget(self.import_btn)
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.clicked.connect(self._on_delete_selected)
        row.addWidget(self.delete_btn)
        self.cleanup_btn = QPushButton("清理孤儿元数据")
        self.cleanup_btn.clicked.connect(self._on_cleanup_orphans)
        row.addWidget(self.cleanup_btn)
        self.prune_btn = QPushButton("按保留策略清理")
        self.prune_btn.clicked.connect(self._on_prune_by_policy)
        self.prune_btn.setToolTip(
            "按设置里的「云端保留数量」（轻量/完整）删除超出的旧快照")
        row.addWidget(self.prune_btn)
        row.addStretch(1)
        il.addLayout(row)
        info.setLayout(il)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "时间", "类型", "大小(MB)", "插件", "状态",
            "加密包", "元数据", "快照 ID",
        ])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for _c in (1, 2, 3, 4, 5, 6):
            hh.setSectionResizeMode(_c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.hint = QLabel(
            "说明：以 .meta.json 为准列出云端快照。"
            "「加密包缺失」= 只剩元数据、无法恢复；"
            "「元数据缺失」= 能解密但缺少插件/状态信息。")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self.hint)

    # ── 数据 ──────────────────────────────────────────────
    @property
    def sync_dir(self) -> str:
        return getattr(self.config, 'cloud_sync_dir', '') or ''

    def refresh(self):
        try:
            res = scan_cloud(self.sync_dir)
        except Exception as e:
            self.stats_label.setText("扫描失败: {}".format(e))
            return
        self._items = res['items']
        st = res['stats']
        self.path_label.setText("目录: {}{}".format(
            st['dir'] or '(未配置)',
            '' if st['exists'] else '  ← 不存在或未挂载'))

        if not st['exists']:
            self.stats_label.setText("云同步目录不可用，无法列出云端快照")
            self.table.setRowCount(0)
            return

        txt = ("可恢复 {} 个（轻量 {} / 完整 {}）｜实际占用 {:.2f} GB"
               .format(st['recoverable'], st['light'], st['full'],
                       st['disk_mb'] / 1024.0))
        if st['meta_only']:
            txt += "｜仅剩元数据 {} 个（不可恢复）".format(st['meta_only'])
        if st['missing_meta']:
            txt += "｜缺元数据 {} 个".format(st['missing_meta'])
        if st['last_sync']:
            txt += "｜最后同步 {}".format(st['last_sync'])
        if st['meta_only'] and st['declared_mb'] > st['disk_mb'] * 1.5:
            txt += "\n注意：清单声明的总量为 {:.2f} GB，但磁盘实际只占 {:.2f} GB"\
                   " —— 差额来自只剩元数据的记录。".format(
                       st['declared_mb'] / 1024.0, st['disk_mb'] / 1024.0)
        self.stats_label.setText(txt)
        self._fill_table()

    def _fill_table(self):
        t = self.table
        t.setRowCount(0)
        for it in self._items:
            row = t.rowCount()
            t.insertRow(row)

            when = _fmt_created(it['created_at'], it['mtime'])
            t.setItem(row, 0, QTableWidgetItem(when))

            kind = "完整" if it['is_full'] else "轻量"
            k_item = QTableWidgetItem(kind)
            k_item.setForeground(QColor("#1f6feb") if it['is_full']
                                 else QColor("#2e7d32"))
            t.setItem(row, 1, k_item)

            size = it['actual_mb'] or it['size_mb']
            t.setItem(row, 2, QTableWidgetItem("{:.2f}".format(size)))
            t.setItem(row, 3, QTableWidgetItem(str(it['plugin_count'] or '-')))
            t.setItem(row, 4, QTableWidgetItem(it['status'] or '-'))

            has_data = QTableWidgetItem("有" if it['has_data'] else "缺失")
            if not it['has_data']:
                has_data.setForeground(QColor("#c62828"))
            t.setItem(row, 5, has_data)

            has_meta = QTableWidgetItem("有" if it['has_meta'] else "缺失")
            if not it['has_meta']:
                has_meta.setForeground(QColor("#ef6c00"))
            t.setItem(row, 6, has_meta)

            t.setItem(row, 7, QTableWidgetItem(it['snapshot_id']))

    def _selected_items(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._items[r] for r in rows if 0 <= r < len(self._items)]

    # ── 操作 ──────────────────────────────────────────────
    def _on_open_dir(self):
        d = self.sync_dir
        if not d or not Path(d).is_dir():
            QMessageBox.warning(self, "提示", "云同步目录不存在：\n{}".format(d))
            return
        try:
            import subprocess
            subprocess.Popen(['explorer', str(Path(d))])
        except Exception as e:
            QMessageBox.warning(self, "无法打开", str(e))

    def _on_import_selected(self):
        items = [i for i in self._selected_items() if i['has_data']]
        if not items:
            QMessageBox.information(
                self, "提示", "请先选中一个含加密包的快照（「加密包」列显示为「有」）。")
            return
        it = items[0]
        if self.snapshot_manager is None:
            QMessageBox.warning(self, "不可用", "快照管理器未初始化，无法导入。")
            return
        reply = QMessageBox.question(
            self, "确认导入",
            "将把云端快照解密并导入本地快照库：\n\n{}\n时间: {}\n大小: {:.2f} MB\n\n"
            "继续？".format(it['snapshot_id'], it['created_at'] or '-',
                            it['actual_mb'] or it['size_mb']),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._do_import(it)

    def _do_import(self, it):
        import tempfile
        from logger import ZeroFallLogger
        try:
            from utils import decrypt_file
            tmp = Path(tempfile.gettempdir())
            src = Path(it['data_path'])
            if src.name.endswith('.enc'):
                dec = tmp / (src.stem + "_cloud.zip")
                ZeroFallLogger.info("[云端快照] 解密 {}".format(src.name))
                from modules.cloud_crypto import decrypt_snapshot
            _ok, _src = decrypt_snapshot(src, dec, getattr(self, 'config', None))
            if not _ok:
                    QMessageBox.critical(
                        self, "解密失败",
                        "解密失败：文件可能损坏，或不是 ZeroFall 加密的快照。")
                    return
            else:
                dec = src

            meta_path = it['meta_path']
            if meta_path is None:
                import hashlib
                md5 = hashlib.md5(dec.read_bytes()).hexdigest()
                mp = tmp / (it['snapshot_id'] + ".meta.json")
                mp.write_text(json.dumps({
                    "snapshot_id": it['snapshot_id'],
                    "created_at": it['created_at'] or "00000000_000000",
                    "status": "healthy",
                    "plugin_count": 0,
                    "size_mb": round(dec.stat().st_size / 1048576.0, 2),
                    "md5_checksum": md5,
                    "plugins": [],
                    "notes": "从云端快照页导入",
                    "deepseek_version": "",
                    "has_session_data": False,
                    "has_keys": False,
                    "archive_path": "",
                }, ensure_ascii=False, indent=2), encoding='utf-8')
                meta_path = mp

            snap = self.snapshot_manager.import_snapshot(str(dec), str(meta_path))
            if not snap:
                QMessageBox.critical(self, "导入失败", "快照导入失败，详见日志。")
                return
            ZeroFallLogger.info("[云端快照] 已导入 {} 到本地快照库".format(
                it['snapshot_id']))
            QMessageBox.information(
                self, "导入成功",
                "已导入到本地快照库：\n{}\n\n可在「快照管理」页查看并恢复。".format(
                    it['snapshot_id']))
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "导入异常", str(e))

    def _on_delete_selected(self):
        items = self._selected_items()
        if not items:
            QMessageBox.information(self, "提示", "请先选中要删除的云端快照。")
            return
        reply = QMessageBox.question(
            self, "确认删除",
            "将从云同步目录删除 {} 个快照（加密包 + 元数据）：\n\n{}\n\n"
            "删除后无法恢复。继续？".format(
                len(items), '\n'.join(i['snapshot_id'] for i in items[:8])),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        n = 0
        for it in items:
            for p in (it['data_path'], it['meta_path']):
                if p is None:
                    continue
                try:
                    Path(p).unlink()
                    n += 1
                except Exception:
                    pass
        try:
            from logger import ZeroFallLogger
            ZeroFallLogger.info("[云端快照] 删除 {} 个文件".format(n))
        except Exception:
            pass
        self.refresh()

    def _on_prune_by_policy(self):
        """按"历史保留天数"清理云端过期快照

        原来这里按"保留 N 轻 + N 完整"清理，但那两个字段已经从配置里删掉了
        —— 云端现在的容量控制是"上传抽样 + 次日收敛 + 保留期"，只剩天数
        这一个闸门。所以这个按钮也统一到天数语义，避免两套规则打架。
        """
        import datetime as _dt

        try:
            keep_days = int(getattr(self.config, 'cloud_history_keep_days', 14) or 0)
        except Exception:
            keep_days = 14
        if keep_days <= 0:
            QMessageBox.information(
                self, "未设置保留期",
                "当前「历史保留天数」为 0（不限制），没有可清理的依据。\n\n"
                "请先在设置里填一个天数（例如 14），云端才会自动/手动清理过期快照。")
            return

        cutoff = (_dt.date.today() - _dt.timedelta(days=keep_days)).strftime('%Y%m%d')
        recoverable = [i for i in self._items if i['has_data']]
        doomed = [i for i in recoverable
                  if str(i.get('created_at', ''))[:8].isdigit()
                  and str(i.get('created_at', ''))[:8] < cutoff]

        if not doomed:
            QMessageBox.information(
                self, "无需清理",
                "没有早于 {} 的快照（保留期 {} 天）。\n"
                "当前云端共 {} 个可恢复快照。".format(cutoff, keep_days, len(recoverable)))
            return

        free_mb = sum((i.get('actual_mb') or 0.0) for i in doomed)
        reply = QMessageBox.question(
            self, "确认清理过期快照",
            "保留期：最近 {} 天（cutoff {}）\n"
            "当前云端：{} 个可恢复快照\n\n"
            "将删除 {} 个过期快照，释放约 {:.2f} GB：\n{}\n\n"
            "删除后无法恢复，继续？".format(
                keep_days, cutoff, len(recoverable),
                len(doomed), free_mb / 1024.0,
                '\n'.join(i['snapshot_id'] for i in doomed[:10])
                + ('\n…' if len(doomed) > 10 else '')),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        n = 0
        for it in doomed:
            for p in (it['data_path'], it['meta_path']):
                if p is None:
                    continue
                try:
                    Path(p).unlink()
                    n += 1
                except Exception:
                    pass
        QMessageBox.information(
            self, "清理完成",
            "已删除 {} 个文件，释放约 {:.2f} GB。".format(n, free_mb / 1024.0))
        self.refresh()

    def _on_cleanup_orphans(self):
        d = Path(self.sync_dir or '')
        if not d.is_dir():
            QMessageBox.warning(self, "提示", "云同步目录不存在")
            return
        orphans = []
        for meta in d.glob('snapshot_*.meta.json'):
            base = meta.name[:-len('.meta.json')]
            if not (d / base).exists() and not (d / (base + '.enc')).exists():
                orphans.append(meta)
        if not orphans:
            QMessageBox.information(self, "无需清理", "没有发现孤儿元数据。")
            return
        reply = QMessageBox.question(
            self, "确认清理",
            "发现 {} 个孤儿元数据（对应的加密包已不存在）。\n"
            "删除它们不会影响任何可用快照。继续？".format(len(orphans)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        n = 0
        for f in orphans:
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
        try:
            from logger import ZeroFallLogger
            ZeroFallLogger.info("[云端快照] 清理孤儿元数据 {} 个".format(n))
        except Exception:
            pass
        QMessageBox.information(self, "清理完成", "已删除 {} 个孤儿元数据。".format(n))
        self.refresh()


def _fmt_created(created_at: str, mtime) -> str:
    """把 20261109_153919 这类紧凑时间戳显示成 2026-11-09 15:39:19"""
    s = str(created_at or '')
    if len(s) >= 15 and s[8] == '_':
        d, t = s[:8], s[9:15]
        if d.isdigit() and t.isdigit():
            return '{}-{}-{} {}:{}:{}'.format(
                d[:4], d[4:6], d[6:8], t[:2], t[2:4], t[4:6])
    return _fmt_mtime(mtime)


def _fmt_mtime(ts) -> str:
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(ts)))
    except Exception:
        return '-'
