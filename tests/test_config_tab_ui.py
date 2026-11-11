# -*- coding: utf-8 -*-
"""配置编辑标签页的 UI 冒烟测试（offscreen，不写真实 profile）

验证三件事：
  1. 标签页能建起来、能列出条目、能回显 config 原文
  2. 「预览差异」只读，不动文件
  3. 「保存到 profile」走的是 dsh_config 的校验+备份+原子写
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
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_uitab_'))

from PyQt6.QtWidgets import QApplication            # noqa: E402

app = QApplication.instance() or QApplication([])

from ui.tabs.config_tab import ConfigTab             # noqa: E402
from modules import dsh_config as dc                 # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


SAMPLE = """- id: permission-presets
  name: "@deepseek-ai/dsh-permission-presets"
  config:
    defaultPreset: danger-full-access

- id: tool-bash
  disabled: !!js process.platform === 'win32'

- id: tool-pwsh
  name: '@deepseek-ai/dsh-tool-pwsh'
  config:
    fetch: true
"""

# 造一个假 $DSH_HOME：profiles/desktop/cordis.patch.yml
home = Path(tempfile.mkdtemp(prefix='zf_home_'))
prof = home / "profiles" / "desktop"
prof.mkdir(parents=True)
target = prof / "cordis.patch.yml"
target.write_text(SAMPLE, encoding="utf-8")

cfg = SimpleNamespace(deepseek_plugins_dir=str(home), acp_profile="desktop")
tab = ConfigTab(cfg, snapshot_manager=None)

print("== 建页与列条目 ==")
check("页签建起来了", tab is not None)
check("profile 下拉框选中 desktop", tab.profile_combo.currentText() == "desktop",
      tab.profile_combo.currentText())
check("路径正确", tab.path_label.text().endswith("cordis.patch.yml"), tab.path_label.text())
check("列出 3 个条目", tab.entry_list.count() == 3, tab.entry_list.count())
labels = [tab.entry_list.item(i).text() for i in range(tab.entry_list.count())]
check("覆盖/禁用标记正确", any("[覆盖]" in l for l in labels) and any("[禁用]" in l for l in labels),
      labels)

print("== 回显 ==")
tab.entry_list.setCurrentRow(2)          # tool-pwsh
check("选中 tool-pwsh", tab._current_id() == "tool-pwsh", tab._current_id())
check("回显 config 原文", tab.editor.toPlainText().strip() == "fetch: true",
      repr(tab.editor.toPlainText()))
tab.entry_list.setCurrentRow(1)          # tool-bash（无 config）
check("无 config 的条目回显为空", tab.editor.toPlainText().strip() == "",
      repr(tab.editor.toPlainText()))
check("禁用条目的按钮文案为「启用条目」", tab.disable_btn.text() == "启用条目", tab.disable_btn.text())

print("== 预览差异（只读） ==")
tab.entry_list.setCurrentRow(2)
tab.editor.setPlainText("fetch: false\nsearchTimeoutMs: 60000\n")
tab._preview_diff()
log = tab.log.toPlainText()
check("差异里有 fetch 变更", "fetch: false" in log and "fetch: true" in log, log[:200])
check("预览不动磁盘", target.read_text(encoding="utf-8") == SAMPLE)
check("预览不改内存正文", tab._text == SAMPLE)

print("== 保存 ==")
tab._save()
disk = target.read_text(encoding="utf-8")
check("磁盘已更新", dc.get_entry_config(disk, "tool-pwsh").get("searchTimeoutMs") == 60000,
      dc.get_entry_config(disk, "tool-pwsh"))
check("仍在合法结构内", dc.validate(disk)[0], dc.validate(disk)[1])
check("其它条目原样", "disabled: !!js process.platform === 'win32'" in disk)
check("生成了备份文件", len(list(prof.glob("cordis.patch.yml.bak-*"))) == 1,
      [p.name for p in prof.glob("*.bak-*")])

print("== 移除覆盖 ==")
tab.entry_list.setCurrentRow(2)
tab._remove_override()
disk2 = target.read_text(encoding="utf-8")
check("tool-pwsh 覆盖已移除", not dc.has_override(disk2, "tool-pwsh"))
check("页面列表同步刷新", tab.entry_list.count() == 2, tab.entry_list.count())

print("== 坏片段被拦 ==")
tab.entry_list.setCurrentRow(0)          # permission-presets
n_before = target.read_text(encoding="utf-8")
try:
    import modules.dsh_config as dc2
    dc2.set_entry_config_text(n_before, "permission-presets", "- 这是列表不是映射\n")
    check("列表片段被 cfg 层拒绝", False, "竟然通过了")
except Exception:
    check("列表片段被 cfg 层拒绝", True)
check("坏片段未写盘", target.read_text(encoding="utf-8") == n_before)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
