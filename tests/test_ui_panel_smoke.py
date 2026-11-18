# -*- coding: utf-8 -*-
"""面板可用性回归：三个真被自检抓到的坑

1. 插件页「启用/禁用」确认框的 format 占位符比实参多一个 →
   点一下就 IndexError，按钮完全用不了（2026-11-16 自检实测）。
2. 同一秒连建两份快照会拿到同一个 snapshot_id → 第二份覆盖第一份，
   索引里只剩一条（一份快照被静默吞掉）。
3. 会话续跑页原有一个"使用已运行的 DSH 端点"复选框，代码从不读它的值，
   属于承诺了不存在的选项 —— 已换成说明文字。
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_uismoke_'))

from PyQt6.QtWidgets import QApplication, QMessageBox      # noqa: E402

app = QApplication.instance() or QApplication([])

from config import ZeroFallConfig                            # noqa: E402
from modules import snapshot_manager as sm                   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


class _NoBox:
    """一律回答 No：既能跑完 format，又不会真去禁用插件"""
    class StandardButton:
        Yes = 1
        No = 0
        Ok = 1024

    @staticmethod
    def question(*a, **k):
        print('  [dialog:question] {}'.format(str(a[2] if len(a) > 2 else a)[:100]))
        return 0

    warning = staticmethod(lambda *a, **k: print('  [dialog:warning] {}'.format(str(a)[:100])))
    critical = staticmethod(lambda *a, **k: print('  [dialog:critical] {}'.format(str(a)[:100])))
    information = staticmethod(lambda *a, **k: print('  [dialog:info] {}'.format(str(a)[:100])))


def make_env(tag):
    root = Path(tempfile.mkdtemp(prefix='zf_uism_' + tag + '_'))
    dsh = root / ".dsh"
    (dsh / "profiles" / "desktop").mkdir(parents=True)
    (dsh / "profiles" / "desktop" / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    plug = dsh / "pkgs" / "demo-plugin"          # .dsh/pkgs 才是插件页真实根目录
    plug.mkdir(parents=True)
    (plug / "package.json").write_text('{"name":"demo-plugin","version":"1.0.0"}', encoding="utf-8")
    (dsh / "sessions" / "proj").mkdir(parents=True)
    (dsh / "sessions" / "proj" / "s.bin").write_bytes(b"x" * 2048)
    cfg = ZeroFallConfig()
    cfg.deepseek_plugins_dir = str(dsh)
    cfg.deepseek_config_dir = str(root / "runtime")
    (root / "runtime").mkdir()
    cfg.deepseek_session_dir = str(dsh / "sessions")
    cfg.snapshot_dir = str(root / "snaps")
    cfg.dsh_appdata_dir = ""
    cfg.backup_plugin_config = False
    return cfg


print('== 1. 插件页启停确认框不再崩 ==')
import ui.tabs.plugin_tab as pl                            # noqa: E402
pl.QMessageBox = _NoBox
cfg = make_env('a')
mgr = sm.SnapshotManager(cfg)
tab = pl.PluginTab(cfg, mgr)
tab._refresh_plugins()
check('插件表有行', tab.plugin_table.rowCount() >= 1, tab.plugin_table.rowCount())
tab.plugin_table.selectRow(0)
err = None
try:
    tab._on_toggle_plugin()
except Exception as e:                                      # noqa: BLE001
    err = '{}: {}'.format(type(e).__name__, e)
check('点"启用/禁用"不抛异常', err is None, err)
check('选了 No 时插件未被改动',
      not (cfg.deepseek_plugins_dir and Path(cfg.deepseek_plugins_dir, 'plugins',
                                            'demo-plugin', '.zf-disabled').exists()))

print('== 2. 同一秒建两份快照不撞 id ==')
cfg2 = make_env('b')
mgr2 = sm.SnapshotManager(cfg2)
a = mgr2.create_snapshot('同秒 A', force_healthy=True)
# 第二份之前先改一个文件：内容完全一致时会被快照去重拦下（那是另一条用例），
# 这里要验的是"同一秒内两份内容不同的快照不能撞 id"。
(Path(cfg2.deepseek_plugins_dir) / 'marker.txt') \
    .write_text('second', encoding='utf-8')
b = mgr2.create_snapshot('同秒 B', force_healthy=True)
check('两份都拿到对象', a is not None and b is not None)
check('id 不同', a and b and a.snapshot_id != b.snapshot_id, (a and a.snapshot_id, b and b.snapshot_id))
zips = sorted(p.name for p in Path(cfg2.snapshot_dir).glob('snapshot_*.zip'))
check('盘上确实两份包', len(zips) == 2, zips)
check('索引里也是两条', len(mgr2.snapshots) == 2, len(mgr2.snapshots))
if a and b:
    check('第二份 id 只往后挪了 1 秒（不改命名规则）',
          b.snapshot_id[:15] == a.snapshot_id[:15], (a.snapshot_id, b.snapshot_id))

print('== 3. 会话续跑页的"假开关"已换成说明文字 ==')
import ui.tabs.resume_tab as rt                             # noqa: E402
rt.QMessageBox = _NoBox
cfg3 = make_env('c')
mgr3 = sm.SnapshotManager(cfg3)
rtab = rt.ResumeTab(cfg3, mgr3)
check('构造成功且不再有 live_check 假开关', not hasattr(rtab, 'live_check'))
labels = [w.text() for w in rtab.findChildren(type(rtab).__mro__[0]) if False] or []
from PyQt6.QtWidgets import QLabel                          # noqa: E402
texts = [w.text() for w in rtab.findChildren(QLabel)]
check('端点说明文字在', any('自动探测' in t for t in texts), texts[:6])

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
