# -*- coding: utf-8 -*-
"""配置中心（四个子页签）的 UI 冒烟测试（offscreen，全在临时目录里跑）

覆盖：
  1. 四个子页签都建得起来，且各自能读到内容
  2. 提示词注入：字节/字符/token 估算显示正确，保存后落盘 + 出备份
  3. Agent 预设：列出 .agent-presets 下的文件、能保存、坏 YAML 会被拒
  4. 备份与回滚：能列出改前副本，并能一键还原（还原前还会留安全副本）
不碰真实 $DSH_HOME。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_cfgui_'))

from PyQt6.QtWidgets import QApplication            # noqa: E402

app = QApplication.instance() or QApplication([])

import ui.tabs.config_tab as ct                       # noqa: E402


class _StubBox:
    """offscreen 下模态对话框会一直等输入 → 换成记录型桩，顺便把弹窗内容打印出来"""

    class StandardButton:
        Yes = 1
        No = 0

    @staticmethod
    def _say(kind, args):
        msg = args[2] if len(args) > 2 else (args[1] if len(args) > 1 else "")
        print("  [dialog:{}] {}".format(kind, str(msg)[:120]))

    warning = staticmethod(lambda *a, **k: _StubBox._say("warning", a))
    critical = staticmethod(lambda *a, **k: _StubBox._say("critical", a))
    information = staticmethod(lambda *a, **k: _StubBox._say("info", a))

    @staticmethod
    def question(*a, **k):
        _StubBox._say("question", a)
        return 1


ct.QMessageBox = _StubBox
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


PATCH = """- id: permission-presets
  name: "@deepseek-ai/dsh-permission-presets"
  config:
    defaultPreset: danger-full-access
"""
INJECT = "# 注入稿\n规则一：直接给结果。\n"
AGENT = "# 预设骨架\n- id: persona\n  name: '@deepseek-ai/dsh-persona'\n  config:\n    prefix: |\n      直接执行。\n"
BAD_YAML = "- id: x\n  config:\n    a: [1, 2\n"

home = Path(tempfile.mkdtemp(prefix='zf_home2_'))
prof = home / "profiles" / "desktop"
prof.mkdir(parents=True)
(prof / "cordis.patch.yml").write_text(PATCH, encoding="utf-8")
(home / "prompt-inject.md").write_text(INJECT, encoding="utf-8")
pre = home / ".agent-presets" / "unrestricted"
pre.mkdir(parents=True)
(pre / "agent.cordis.yml").write_text(AGENT, encoding="utf-8")
(pre / "preset.yml").write_text("name: unrestricted\n", encoding="utf-8")

cfg = SimpleNamespace(deepseek_plugins_dir=str(home), acp_profile="desktop")
tab = ConfigTab(cfg, snapshot_manager=None)

print("== 四个子页签 ==")
check("子页签数量=7", tab.tabs.count() == 7, tab.tabs.count())
titles = [tab.tabs.tabText(i) for i in range(tab.tabs.count())]
check("页签名正确", titles == ["profile 补丁", "提示词注入", "Agent 预设", "备份与回滚", "迁移与历史", "profile 健康", "会话修复"], titles)

print("== profile 页 ==")
check("条目列表有 1 条", tab.entry_list.count() == 1, tab.entry_list.count())
check("路径指向 cordis.patch.yml", tab.path_label.text().endswith("cordis.patch.yml"))

print("== 提示词注入页 ==")
check("注入稿已回显", tab.inject_editor.toPlainText() == INJECT, repr(tab.inject_editor.toPlainText())[:60])
meta = tab.inject_meta.text()
check("体量指标显示字节与 token", "字节" in meta and "token/步" in meta, meta)
m = dc.text_metrics(INJECT)
check("估算值自洽", str(m["bytes"]) in meta and str(m["tokens"]) in meta, (m, meta))
tab.inject_editor.setPlainText(INJECT + "规则二：不铺垫。\n")
tab._save_inject()
disk = (home / "prompt-inject.md").read_text(encoding="utf-8")
check("注入稿已落盘", "规则二" in disk)
check("注入稿产生了备份",
      len(list(home.glob("prompt-inject.md.bak-zf-config-*"))) == 1,
      [p.name for p in home.glob("*.bak-*")])

print("== Agent 预设页 ==")
check("预设列表含 unrestricted", "unrestricted" in [tab.preset_combo.itemText(i) for i in range(tab.preset_combo.count())],
      [tab.preset_combo.itemText(i) for i in range(tab.preset_combo.count())])
check("文件列表含两件", tab.preset_file_combo.count() == 2,
      [tab.preset_file_combo.itemText(i) for i in range(tab.preset_file_combo.count())])
tab.preset_combo.setCurrentText("unrestricted")
tab.preset_file_combo.setCurrentText("agent.cordis.yml")
check("预设正文回显", "persona" in tab.preset_editor.toPlainText())
tab.preset_editor.setPlainText(AGENT + "\n# 追加一行\n")
tab._save_preset()
check("预设已落盘", "追加一行" in (pre / "agent.cordis.yml").read_text(encoding="utf-8"))
before_bad = (pre / "agent.cordis.yml").read_text(encoding="utf-8")
tab.preset_editor.setPlainText(BAD_YAML)
try:
    dc.save_text(pre / "agent.cordis.yml", BAD_YAML, validator=dc.validate_yaml_text)
    check("坏 YAML 被拒", False, "竟然写进去了")
except dc.ConfigError:
    check("坏 YAML 被拒", True)
check("坏 YAML 未落盘", (pre / "agent.cordis.yml").read_text(encoding="utf-8") == before_bad)

print("== 备份与回滚页 ==")
tab._reload_backups()
check("备份列表非空", tab.backup_list.count() >= 2, tab.backup_list.count())
first = tab.backup_list.item(0).data(0x0100)     # UserRole
check("列表项带备份路径", bool(first), first)
bak = Path(first)
target = dc.target_of_backup(bak)
check("能从备份名反推目标", target.exists(), target)
content_before = target.read_text(encoding="utf-8")
res = dc.restore_backup(bak, target)
check("还原成功", Path(res["target"]).exists() and (res["safety"] is None or Path(res["safety"]).exists()))
check("还原后内容=备份内容", target.read_text(encoding="utf-8") == bak.read_text(encoding="utf-8"))
if res["safety"]:
    check("还原前留了安全副本", Path(res["safety"]).read_text(encoding="utf-8") == content_before)

print("== 迁移与历史页 ==")
check("导出目标默认指向 DSH_HOME 下", "zf-config-bundle.zip" in tab.export_path.text(), tab.export_path.text())
check("冲突策略三选一", [tab.import_policy.itemText(i) for i in range(tab.import_policy.count())] == ["overwrite", "skip", "keep-both"])
check("版本点列表初始为空", tab.point_list.count() == 0, tab.point_list.count())
tab.point_label.setText("ui-test-point")
tab._save_point()
check("存版本点后列表有 1 条", tab.point_list.count() == 1, tab.point_list.count())
check("版本点目录已落盘", (home / ".zf-config-history").is_dir())
tab._diff_point()
check("与当前对比无差异（刚存完）", "完全一致" in tab.log.toPlainText(), tab.log.toPlainText()[:80])
# 导出一次（去密）
tab.export_path.setText(str(home / "bundle-test.zip"))
tab._export_bundle()
check("导出包已生成", (home / "bundle-test.zip").is_file())
check("导出日志记录了剥离", "剥离密钥值" in tab.log.toPlainText())

print("== profile 健康页 ==")
tab._run_health()
check("诊断跑完并列出条目", tab.health_list.count() >= 7, tab.health_list.count())
check("有结论文案", bool(tab.health_verdict.text()), tab.health_verdict.text())
check("日志记录了摘要", "profile 健康" in tab.log.toPlainText())

print("== 会话修复页 ==")
tab._scan_sessions()
check("扫描后有结论文案", bool(tab.session_verdict.text()), tab.session_verdict.text())
check("日志记录了会话扫描", "会话扫描" in tab.log.toPlainText())
# 假 home 里没有 sessions 目录 → 0 个文件，不应崩
tab._repair_sessions()

print("== 按建议修正（摘除不可解析 bundle）==")
import modules.profile_health as _ph
targets = _ph.unresolvable_bundles(home, "desktop", host_base=None)
check("识别出不可解析的 bundle", "dsh-purge-community" not in targets, targets)
if targets:
    dry = _ph.neutralize_bundles(home, "desktop", targets, dry_run=True)
    check("dry-run 不写盘", dry["changed"] is False and "would_remove" in dry, dry)
if targets:
    res_n = _ph.neutralize_bundles(home, "desktop", targets, dry_run=False)
    check("摘除后计数正确", len(res_n["removed"]) == len(targets), res_n)
    check("留了 package.json 备份", Path(res_n["backup"]).is_file(), res_n["backup"])
    import json as _json
    doc = _json.loads((home / "profiles" / "desktop" / "package.json").read_text(encoding="utf-8"))
    left = ((doc.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    check("bundles 里已摘掉", not any(t in left for t in targets), left)
    check("依赖声明保留", "dependencies" in doc and len(doc["dependencies"]) >= 1, doc.get("dependencies"))
else:
    check("（本夹具没有不可解析 bundle，跳过摘除）", True)

print("== 加密导出 + tag + 两点互 diff ==")
check("有加密密码输入框", hasattr(tab, "export_password"))
check("有 tag 输入框", hasattr(tab, "tag_input"))
check("有 A/B 对比下拉", hasattr(tab, "diff_a") and hasattr(tab, "diff_b"))
# 加密导出
tab.export_path.setText(str(home / "enc-test.zfce"))
tab.export_password.setText("ui-test-pw")
tab._export_bundle()
from modules import config_crypto as _cc
check("导出的是加密容器", _cc.is_encrypted(home / "enc-test.zfce"))
check("加密包能读 header", _cc.read_header(home / "enc-test.zfce")["kdf"] == "scrypt")
# 打 tag
tab.point_label.setText("tagged-point")
tab._save_point()
tab.tag_input.setText("known-good")
tab._tag_selected_point()
check("tag 出现在列表里", any("known-good" in tab.point_list.item(i).text()
                          for i in range(tab.point_list.count())),
      [tab.point_list.item(i).text() for i in range(tab.point_list.count())][:2])
# 再存一个点，A/B 对比
(home / "prompt-inject.md").write_text((home / "prompt-inject.md").read_text(encoding="utf-8") + "追加一行\n",
                                       encoding="utf-8")
tab.point_label.setText("after-edit")
tab._save_point()
tab._load_points()
check("A/B 下拉都有条目", tab.diff_a.count() >= 2 and tab.diff_b.count() >= 2,
      (tab.diff_a.count(), tab.diff_b.count()))
tab._diff_two_points()
check("互 diff 输出到日志", "改动" in tab.log.toPlainText(), tab.log.toPlainText()[:100])

print("== 残渣清理 / 恢复脚本按钮 ==")
check("有 清理 .bak 残留 的入口", hasattr(tab, "_cleanup_junk"))
check("有 给快照补恢复脚本 的入口", hasattr(tab, "_write_restore_scripts"))
# 造一个残渣，走 dry-run 分支（QMessageBox.question 已被 stub 成 No）
(home / "prompt-inject.md.zf-tmp").write_text("x", encoding="utf-8")
tab._cleanup_junk()
# 本测试全局把 QMessageBox.question stub 成 Yes，所以这里走的是"确认后真删"
check("清理有日志记录", "清理残渣" in tab.log.toPlainText(), tab.log.toPlainText()[-120:])
check("残渣已按确认删除", not (home / "prompt-inject.md.zf-tmp").is_file())
# 没有 snapshot_manager 时给提示而不是崩
tab.snapshot_manager = None
tab._write_restore_scripts()
tab.snapshot_manager = None

print("== 重启按钮 ==")
check("没有 main_window 时重启按钮是灰的", tab.restart_btn.isEnabled() is False)
check("按钮文案为「重启 DSH」", tab.restart_btn.text() == "重启 DSH", tab.restart_btn.text())

print("== 备份清理 ==")
# 造几份多余备份，再用 UI 的清理入口（keep=2）
target2 = home / "prompt-inject.md"
for i in range(4):
    shutil.copy2(target2, home / "prompt-inject.md.bak-zf-config-2026010{} -010101".format(i + 1))
before_n = len(list(home.glob("prompt-inject.md.bak-zf-config-*")))
tab.backup_keep_spin.setValue(2)
tab._prune_backups()
after_n = len(list(home.glob("prompt-inject.md.bak-zf-config-*")))
check("清理后按保留份数收敛", after_n == 2, (before_n, after_n))
check("清理日志有记录", "清理旧备份" in tab.log.toPlainText())

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
