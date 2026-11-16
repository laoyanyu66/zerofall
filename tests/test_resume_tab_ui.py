# -*- coding: utf-8 -*-
"""会话续跑标签页的 UI 冒烟测试（offscreen）

只验证界面这一层：能建、能读状态、表格只显示「关闭前在跑」的会话、自愈按钮真的落盘。
弹窗全部打桩，测试里不弹任何对话框。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("ZF_DATA_DIR", tempfile.mkdtemp(prefix="zf_resume_ui_"))

from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from ui.tabs import resume_tab as rt  # noqa: E402
from modules import resume_guard as rg  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"  {extra}"))


# ── 弹窗打桩 ────────────────────────────────────────────────────────────────
SHOWN = []


class _FakeBox:
    @staticmethod
    def information(parent, title, text):
        SHOWN.append(("info", title, text))

    @staticmethod
    def warning(parent, title, text):
        SHOWN.append(("warn", title, text))


rt.QMessageBox = _FakeBox


def make_home():
    home = Path(tempfile.mkdtemp(prefix="zf_resume_ui_home_"))
    pdir = home / "profiles" / "desktop"
    pdir.mkdir(parents=True)
    (pdir / "package.json").write_text(json.dumps(
        {"name": "dsh-profile-desktop", "private": True, "dependencies": {},
         "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base"]}}}, indent=2), encoding="utf-8")
    (pdir / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    return home, pdir


def main():
    home, pdir = make_home()
    cfg = SimpleNamespace(deepseek_plugins_dir=str(home))

    print("A. 建页签")
    tab = rt.ResumeTab(cfg, None)
    check("页签可实例化", tab is not None)
    check("profile 下拉有 desktop", tab.profile_box.currentText() == "desktop", tab.profile_box.currentText())
    check("初始状态显示未就绪", "未就绪" in tab.status_label.text(), tab.status_label.text())
    check("初始表格为空", tab.table.rowCount() == 0)

    print("B. 只显示关闭前在跑的会话")
    # 用真实时钟造快照：保质期（maxAgeHours）是相对时间，用固定的远古时间戳
    # 会被真实时间判成"超保质期"，测的就不是它想测的那条判据了。
    now = int(time.time() * 1000)
    (home / rg.STATE_FILE).write_text(json.dumps({
        "version": 2,
        "sessions": {
            "session-run": {"status": "running", "title": "在跑", "cwd": "C:/ws", "at": now, "runningAt": now},
            "session-idle": {"status": "idle", "title": "历史", "cwd": "C:/ws", "at": now - 1000},
            "session-failed": {"status": "failed", "title": "失败", "cwd": "C:/ws", "at": now - 2000},
        },
        "guard": {"crashes": 1},
        "boot": {"pid": 1, "restoring": False},
    }), encoding="utf-8")
    tab._refresh()
    # 列全部会话（含已结束）：running 那条在前，idle 那条也会显示但「下次恢复」= 否
    check("表格列全部会话（含已结束）", tab.table.rowCount() >= 2, tab.table.rowCount())
    check("第 1 行是在跑的会话", tab.table.item(0, 2).text() == "在跑", tab.table.item(0, 2).text())
    check("第 1 行下次恢复=是", tab.table.item(0, 5).text() == "是", tab.table.item(0, 5).text())
    check("存在已结束且不恢复的行",
          any(tab.table.item(i, 2).text() == "已结束" and tab.table.item(i, 5).text() == "否"
              for i in range(tab.table.rowCount())))
    check("行内容是 session-run", tab.table.item(0, 0).text() == "session-run", tab.table.item(0, 0).text())
    check("状态行含崩溃计数", "崩溃计数 1" in tab.status_label.text(), tab.status_label.text())

    print("C. 自愈按钮真的落盘")
    SHOWN.clear()
    tab._on_heal()
    st = rg.plugin_status(pdir)
    check("patch 条目已写入", st["patch"] == "enabled", st["patch"])
    check("vendor 已落盘", st["vendor"])
    check("依赖已声明", st["dependency"])
    check("整体就绪", st["ready"], st)
    check("点了自愈后状态变成就绪", "就绪" in tab.status_label.text() or "✅" in tab.status_label.text())
    check("弹了「已自愈」", any("已自愈" in t for _k, t, _x in SHOWN), SHOWN)

    print("D. 幂等：再点一次不该重复写")
    before = (pdir / "cordis.patch.yml").read_text(encoding="utf-8")
    SHOWN.clear()
    tab._on_heal()
    after = (pdir / "cordis.patch.yml").read_text(encoding="utf-8")
    check("patch 字节不变", before == after)
    check("弹了「无需自愈」", any("无需自愈" in t for _k, t, _x in SHOWN), SHOWN)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
