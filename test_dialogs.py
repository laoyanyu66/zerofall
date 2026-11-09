# -*- coding: utf-8 -*-
"""对话框兼容性测试

零风险前提：代码里有 200+ 处 QMessageBox.xxx(...) 调用，只把导入的名字
换成了 ZFMessageBox。所以必须确认两件事：

  1. 调用形状完全兼容 —— information/warning/critical/question/about
     的参数顺序、返回值类型都与 QMessageBox 一致，否则那 200 处会崩。
  2. 枚举可用 —— QMessageBox.StandardButton.Yes 这类写法仍然有效，
     因为大量调用用到了它。

顺带验证 VerifyResultDialog 能吃下真实校验报告，并且严重项/警告项都能渲染。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_dlg_')

from PyQt6.QtWidgets import QApplication                # noqa: E402

app = QApplication([])
app.setStyle('Fusion')

from ui import theme                                    # noqa: E402
app.setStyleSheet(theme.qss())

from ui import dialogs as D                             # noqa: E402
from PyQt6.QtWidgets import QMessageBox                 # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def test_enum_compatible():
    print('\n[1] 枚举与 QMessageBox 一致')
    check('StandardButton 是同一个枚举对象',
          D.ZFMessageBox.StandardButton is QMessageBox.StandardButton)
    check('Yes 可用', D.ZFMessageBox.StandardButton.Yes is not None)
    check('ButtonRole 可用',
          D.ZFMessageBox.ButtonRole is QMessageBox.ButtonRole)


def test_static_signatures_exist():
    print('\n[2] 五个静态方法都在，且可调用')
    for m in ('information', 'warning', 'critical', 'question', 'about'):
        fn = getattr(D.ZFMessageBox, m, None)
        check('有 {}'.format(m), callable(fn))


def test_notice_dialogs_build():
    print('\n[3] 通知类对话框能构建并正确取色')
    for kind in ('info', 'warning', 'error'):
        dlg = D._NoticeDialog(None, '测试标题', '测试正文内容', kind)
        check('{} 构建成功'.format(kind), dlg is not None)
        check('{} 有标题'.format(kind), dlg.title_label.text() == '测试标题')
        dlg.close()

    # 未知 kind 不应崩
    dlg = D._NoticeDialog(None, '兜底', '未知类型', 'nonexistent')
    check('未知类型有兜底', dlg is not None)
    dlg.close()


def test_confirm_dialog_buttons():
    print('\n[4] 询问框按 buttons 组合摆按钮')
    sb = D.StandardButton
    dlg = D._ConfirmDialog(None, '确认', '继续吗？',
                           sb.Yes | sb.No | sb.Cancel, sb.Yes)
    check('默认返回 No（未点击时保守）',
          dlg.result_button() == sb.No, dlg.result_button())

    # 模拟点击 Yes
    dlg._pick(sb.Yes)
    check('点击后返回 Yes', dlg.result_button() == sb.Yes, dlg.result_button())

    dlg2 = D._ConfirmDialog(None, '确认', '继续吗？', sb.Ok | sb.Cancel, sb.Ok)
    dlg2._pick(sb.Cancel)
    check('Ok/Cancel 组合可取 Cancel',
          dlg2.result_button() == sb.Cancel, dlg2.result_button())


def test_verify_dialog_with_real_report():
    print('\n[5] 校验面板能吃下真实报告')
    from config import ZeroFallConfig
    from modules.environment_checker import (
        EnvironmentChecker, EnvironmentReport, CheckItem, CheckResult,
    )
    cfg = ZeroFallConfig()
    cfg.snapshot_dir = tempfile.mkdtemp(prefix='zf_dlg_snap_')
    cfg.deepseek_exe_path = r'D:\DeepSeek\DeepSeek Harness.exe'
    ec = EnvironmentChecker(cfg)
    report = ec.run_full_check()
    vd = D.VerifyResultDialog(None, report)
    check('构建成功（{} 项）'.format(len(report.items)), vd is not None)
    vd.close()

    # 人工构造含失败项的报告，验证三种状态都能渲染
    r2 = EnvironmentReport()
    r2.add_item(CheckItem('正常项', CheckResult.PASSED, '一切正常'))
    r2.add_item(CheckItem('警告项', CheckResult.WARNING, '轻微问题', '细节A'))
    r2.add_item(CheckItem('失败项', CheckResult.FAILED, '严重问题', '细节B'))
    vd2 = D.VerifyResultDialog(None, r2)
    check('含失败项的报告可渲染', vd2 is not None)
    check('结论为 failed', r2.overall_result == CheckResult.FAILED,
          r2.overall_result)
    vd2.close()


def test_scroll_height_logic():
    print('\n[6] 滚动区高度随内容自适应（不裁最后一项）')
    from modules.environment_checker import (
        EnvironmentReport, CheckItem, CheckResult,
    )
    few = EnvironmentReport()
    few.add_item(CheckItem('唯一项', CheckResult.PASSED, 'ok'))
    d_few = D.VerifyResultDialog(None, few)
    d_few.adjustSize()
    d_few.grab()
    h_few = d_few.scroll.height()

    many = EnvironmentReport()
    for i in range(20):
        many.add_item(CheckItem('项目{}'.format(i), CheckResult.PASSED, 'ok'))
    d_many = D.VerifyResultDialog(None, many)
    d_many.adjustSize()
    d_many.grab()
    h_many = d_many.scroll.height()

    check('内容少时高度小', h_few < h_many, (h_few, h_many))
    check('内容多时封顶不无限长', h_many <= 424, h_many)
    d_few.close()
    d_many.close()


def test_all_modules_imported_alias():
    print('\n[7] 各页面已切到统一对话框')
    import importlib
    mods = ['ui.main_window', 'ui.tabs.snapshot_tab', 'ui.tabs.plugin_tab',
            'ui.tabs.cloud_tab', 'ui.tabs.cloud_snapshots_tab',
            'ui.tabs.dashboard_tab', 'ui.tabs.settings_tab',
            'ui.tabs.env_tab', 'ui.crash_dialog']
    for m in mods:
        try:
            mod = importlib.import_module(m)
        except Exception as e:
            check('{} 可导入'.format(m), False, e)
            continue
        alias = getattr(mod, 'QMessageBox', None)
        ok = alias is D.ZFMessageBox
        check('{} 使用统一对话框'.format(m.split('.')[-1]), ok,
              '实得 {}'.format(alias))


if __name__ == '__main__':
    print('=' * 64)
    print('对话框兼容性测试')
    print('=' * 64)
    test_enum_compatible()
    test_static_signatures_exist()
    test_notice_dialogs_build()
    test_confirm_dialog_buttons()
    test_verify_dialog_with_real_report()
    test_scroll_height_logic()
    test_all_modules_imported_alias()
    print()
    print('=' * 64)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 64)
    sys.exit(0 if not FAIL else 1)
