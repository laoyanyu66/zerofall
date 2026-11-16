# -*- coding: utf-8 -*-
"""快照页自动刷新测试（offscreen，不碰真实快照库）

背景：静默 / 变更点 / 定时完整快照都是后台线程在打，快照页原来只在手动
点「刷新」或某个前台动作之后才重建列表。于是界面最上面一直停在上次刷新的
时刻，看起来像"程序不存快照了"。

验证：
  1. 定时器默认在跑，周期来自 config.snapshot_auto_refresh_seconds
  2. 索引文件真有变化才重建表格（没变化不重建，省 CPU）
  3. 自动刷新不会把用户选中的行刷掉（按 snapshot id 还原）
  4. 关掉开关就不再自动刷新；force=True 可以强制刷一次
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_autoref_'))

from PyQt6.QtWidgets import QApplication            # noqa: E402

app = QApplication.instance() or QApplication([])

from ui.tabs.snapshot_tab import SnapshotTab        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_snap(sid, full=False, notes=''):
    return SimpleNamespace(
        snapshot_id=sid,
        created_at='20261111_' + sid.rsplit('_', 1)[-1],
        is_full=full,
        status=SimpleNamespace(value='healthy'),
        plugin_count=14,
        size_mb=26.0,
        archive_path=str(Path(tempfile.gettempdir()) / (sid + '.zip')),
        md5_checksum='abcdef0123456789',
        notes=notes,
    )


class FakeManager:
    """只实现快照页用到的方法；索引文件真实落盘，好让 mtime 变"""

    def __init__(self, root: Path):
        self.snapshot_dir = root
        self.index_file = root / 'snapshot_index.json'
        self.snaps = []
        self.reload_calls = 0
        self._touch()

    def _touch(self):
        # 内容长度随数量变化，保证 (mtime_ns, size) 指纹真的会变
        self.index_file.write_text(
            '{"n": %d}' % len(self.snaps), encoding='utf-8')
        # 某些文件系统 mtime 精度是 1s，睡眠保证下一轮能看出差异
        time.sleep(0.01)

    def add(self, sid, full=False):
        self.snaps.insert(0, make_snap(sid, full=full))
        self._touch()

    def reload_index(self):
        self.reload_calls += 1

    def get_snapshot_list(self):
        return list(self.snaps)

    def get_restorable_ids(self):
        return {s.snapshot_id for s in self.snaps}

    def get_disk_stats(self):
        return {'light_count': 1, 'light_max': 48, 'light_mb': 26.0,
                'light_cover_h': 0.5, 'full_count': 0, 'full_max': 12,
                'full_mb': 0.0, 'full_cover_h': 0.0, 'total_mb': 26.0,
                'free_gb': 100.0}

    def find_orphan_archives(self):
        return []


root = Path(tempfile.mkdtemp(prefix='zf_snaproot_'))
mgr = FakeManager(root)
mgr.add('snapshot_20261111_180000')
mgr.add('snapshot_20261111_181000')
mgr.add('snapshot_20261111_182000')          # add 是头插，所以最新在最前

cfg = SimpleNamespace(
    snapshot_auto_refresh=True,
    snapshot_auto_refresh_seconds=5,
)
tab = SnapshotTab(cfg, mgr)
tab.show()          # offscreen 下也要可见，定时回调才会真的干活

print('== 初始状态 ==')
check('表格列出 3 行', tab.snapshot_table.rowCount() == 3,
      tab.snapshot_table.rowCount())
check('自动刷新开关默认勾上', tab.auto_refresh_chk.isChecked())
check('定时器在跑', tab._auto_timer.isActive())
check('周期 = 配置值 5000ms', tab._auto_timer.interval() == 5000,
      tab._auto_timer.interval())
check('显示"已同步 HH:MM:SS"', tab.sync_label.text().startswith('已同步 '),
      tab.sync_label.text())

print('== 索引没变化时不该重建 ==')
before = mgr.reload_calls
tab._auto_tick()
check('无变化跳过重建', mgr.reload_calls == before, mgr.reload_calls)

print('== 后台新增快照后自动出现 ==')
mgr.add('snapshot_20261111_183000')
tab._auto_tick()
check('表格多出一行', tab.snapshot_table.rowCount() == 4,
      tab.snapshot_table.rowCount())
check('新快照在最上面',
      tab.snapshot_table.item(0, 0).text() == 'snapshot_20261111_183000',
      tab.snapshot_table.item(0, 0).text())
check('确实重载了索引', mgr.reload_calls > before)

print('== 自动刷新保住用户选中行 ==')
row = 2
sel_id = tab.snapshot_table.item(row, 0).text()
tab.snapshot_table.selectRow(row)
mgr.add('snapshot_20261111_184000')
tab._auto_tick()
selected = [i.row() for i in tab.snapshot_table.selectionModel().selectedRows()]
selected_ids = [tab.snapshot_table.item(r, 0).text() for r in selected]
check('选中行还在（按 id 定位）', selected_ids == [sel_id],
      '{} vs {}'.format(selected_ids, [sel_id]))

print('== 强制刷新 ==')
before = mgr.reload_calls
tab._auto_tick(force=True)
check('force 时无变化也刷新', mgr.reload_calls == before + 1, mgr.reload_calls)

print('== 关掉开关 ==')
tab.auto_refresh_chk.setChecked(False)
check('定时器已停', not tab._auto_timer.isActive())
check('配置里记为 False', cfg.snapshot_auto_refresh is False,
      cfg.snapshot_auto_refresh)
check('提示文案已更新', '已关闭' in tab.sync_label.text(), tab.sync_label.text())
before = mgr.reload_calls
mgr.add('snapshot_20261111_185000')
tab._auto_tick()
check('关掉后不再自动刷新', mgr.reload_calls == before, mgr.reload_calls)
tab.auto_refresh_chk.setChecked(True)
check('重新打开后立刻补一次',
      tab.snapshot_table.item(0, 0).text() == 'snapshot_20261111_185000',
      tab.snapshot_table.item(0, 0).text())
check('定时器恢复', tab._auto_timer.isActive())

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
