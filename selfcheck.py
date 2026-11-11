# -*- coding: utf-8 -*-
"""项目自检：扫已经被踩过的那几类坑

不是通用 linter，只查这个项目真实出过问题的地方：

1. `or <数字>` 吞掉合法的 0
   出过：plugin_drift_settle_seconds=0 被 `0 or 20` 变成 20

2. `self._xxx(` 调用了不存在的方法
   出过 3 次：先写调用、后加定义时，用"字符串是否已存在"判断插入，
   结果定义没进去，运行时 AttributeError（主窗口直接起不来）

3. 类体被复制成两份
   出过 1 次：编辑脚本用 lines[:None] 把整个类复制了一遍，
   后面的副本覆盖前面的定义，新加的方法"存在但不可见"

4. 配置字段定义了却没人读
   出过 8 个：界面上没开关、代码里没引用，纯摆设

5. 重复的顶层定义
"""
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent
SKIP_DIRS = {'build', 'dist', '__pycache__', '_stage_standalone',
             '_stage_master', '_stage_agent', 'tools', 'docs', '.git'}
# 备份目录也跳过（文件名带 _backup_ 前缀的那些）

PASS, WARN, FAIL = [], [], []


def py_files():
    for p in ROOT.rglob('*.py'):
        if any(part in SKIP_DIRS or part.startswith('_backup_') for part in p.parts):
            continue
        if p.name.startswith('test_') or p.name == 'selfcheck.py':
            continue
        yield p


def check_or_zero():
    """`or <数字>` 会把合法的 0 吞掉"""
    print('\n[1] 检查 `or <数字>` 吞零值')
    pat = re.compile(r'getattr\([^)]*\)\s*or\s*(\d+)')
    hits = []
    for p in py_files():
        for i, line in enumerate(p.read_text(encoding='utf-8').splitlines(), 1):
            # 跳过整行注释、以及文档字符串里用反引号包裹的示例代码
            # （否则像 "`int(getattr(cfg, 'x', 20) or 20)` 这种说明文字会被误报）
            stripped = line.strip()
            if stripped.startswith('#') or '`' in line:
                continue
            if 'noqa: or-num' in line:
                continue
            if pat.search(line) and '#' not in line.split('or')[0]:
                hits.append((p.relative_to(ROOT), i, line.strip()[:96]))
    if hits:
        for h in hits:
            WARN.append('or-num {}:{}'.format(h[0], h[1]))
            print('  [WARN] {}:{}  {}'.format(*h))
        print('  提示：若该配置允许 0，应改成显式 int() + 默认值判断')
    else:
        PASS.append('or-num')
        print('  [PASS] 没有可疑写法')


def collect_class_methods(tree):
    """收集每个类里定义的方法名"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out[node.name] = {n.name for n in node.body
                              if isinstance(n, (ast.FunctionDef,
                                                ast.AsyncFunctionDef))}
    return out


def check_called_methods():
    """self._xxx() 是否真有定义"""
    print('\n[2] 检查调用了不存在的方法')
    known_qt = {
        # QDialog / QWidget 继承来的
        'accept', 'reject', 'done', 'exec', 'setWindowFlags', 'windowFlags',
        'setWindowIcon', 'mousePressEvent', 'mouseMoveEvent', 'mouseReleaseEvent',
        'paintEvent', 'resizeEvent', 'showEvent', 'hideEvent', 'closeEvent',
        'keyPressEvent', 'eventFilter', 'sizeHint', 'minimumSizeHint',
        'setGeometry', 'update', 'repaint', 'raise_', 'activateWindow',
        'saveState', 'restoreState', 'saveGeometry', 'restoreGeometry',
        # 几何/尺寸/可见性（QWidget）
        'width', 'height', 'size', 'rect', 'pos', 'x', 'y',
        'setFixedHeight', 'setFixedWidth', 'setFixedSize', 'setMinimumWidth',
        'setMinimumHeight', 'setMaximumWidth', 'setMaximumHeight',
        'minimumWidth', 'minimumHeight', 'maximumWidth', 'maximumHeight',
        'isVisible', 'hide', 'showNormal', 'showMaximized', 'showMinimized',
        'isMaximized', 'isMinimized', 'setWindowState', 'windowState',
        'layout', 'setContentsMargins', 'contentsRect', 'mapToGlobal',
        'mapFromGlobal', 'childAt', 'parentWidget', 'topLevelWidget',
        # QThread
        'msleep', 'sleep', 'usleep', 'requestInterruption', 'isInterrupted',
        # QObject
        'setModal', 'isModal', 'adjustSize', 'sizeGripEnabled',
        'deleteLater', 'blockSignals', 'signalsBlocked', 'disconnect',
        'sender', 'parent', 'installEventFilter', 'removeEventFilter', 'property',
        'setProperty', 'dumpObjectInfo', 'inherits',
        'setWindowOpacity', 'setAttribute', 'setFocus', 'hasFocus',
        # BaseHTTPRequestHandler 继承来的
        'send_response', 'send_header', 'end_headers', 'send_error',
        'log_message', 'log_request', 'send_response_only',
        # Thread / QThread 继承来的
        'run', 'start', 'join', 'is_alive', 'wait', 'quit', 'terminate',
        'menuBar', 'statusBar', 'centralWidget', 'setCentralWidget',
        'findChildren', 'window', 'geometry', 'frameGeometry', 'move',
        'resize', 'show', 'close', 'grab', 'winId', 'setWindowTitle',
        'setMinimumSize', 'setStyleSheet', 'setObjectName', 'setVisible',
        'setEnabled', 'setText', 'text', 'setChecked', 'isChecked',
        'setCurrentIndex', 'currentIndex', 'addWidget', 'setLayout',
    }
    all_bad = []
    for p in py_files():
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        methods = collect_class_methods(tree)
        defined = set()
        for m in methods.values():
            defined |= m
        # 模块级函数也算（有些用 self 传参的辅助）
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                defined.add(node.name)
        # 属性赋值也可能像方法（如 self._added = False）
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                defined.add(node.attr)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == 'self'):
                name = fn.attr
                if name in defined or name in known_qt:
                    continue
                # 双下划线 / 单下划线开头但属于惯例的跳过
                if name.startswith('__') and name.endswith('__'):
                    continue
                all_bad.append((p.relative_to(ROOT), node.lineno, name))
    if all_bad:
        for f, ln, name in all_bad[:15]:
            FAIL.append('missing-method {}:{} {}'.format(f, ln, name))
            print('  [FAIL] {}:{}  self.{}() 无定义'.format(f, ln, name))
    else:
        PASS.append('missing-method')
        print('  [PASS] 所有 self.xxx() 都有对应属性或方法')


def check_duplicated_class_body():
    """同一个类在同一个文件里被定义两次（类体复制的典型症状）"""
    print('\n[3] 检查类体被复制')
    bad = []
    for p in py_files():
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
        dup = [n for n, c in Counter(names).items() if c > 1]
        if dup:
            bad.append((p.relative_to(ROOT), dup))
    if bad:
        for f, dup in bad:
            FAIL.append('dup-class {}'.format(f))
            print('  [FAIL] {} 重复定义: {}'.format(f, dup))
    else:
        PASS.append('dup-class')
        print('  [PASS] 没有重复的类定义')


def check_dup_functions():
    """同一文件里同名函数定义两次"""
    print('\n[4] 检查同名函数重复定义')
    bad = []
    for p in py_files():
        try:
            tree = ast.parse(p.read_text(encoding='utf-8'))
        except Exception:
            continue
        # 只看顶层函数和类内方法（按 (类名, 函数名) 计）
        names = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                names.append(('', node.name))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        names.append((node.name, sub.name))
        c = Counter(names)
        dup = [k for k, v in c.items() if v > 1]
        if dup:
            bad.append((p.relative_to(ROOT), dup))
    if bad:
        for f, dup in bad:
            WARN.append('dup-func {}'.format(f))
            print('  [WARN] {}: {}'.format(f, dup[:4]))
    else:
        PASS.append('dup-func')
        print('  [PASS] 没有重复函数')


def check_unused_config_fields():
    """config 里定义了但没人读的字段"""
    print('\n[5] 检查配置字段是否真被使用')
    cfg_src = (ROOT / 'config.py').read_text(encoding='utf-8')
    tree = ast.parse(cfg_src)
    fields = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'ZeroFallConfig':
            for sub in node.body:
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    fields.append(sub.target.id)
            break

    # 收集所有非 config.py 文件里出现过的字段名
    blob = []
    for p in py_files():
        if p.name == 'config.py':
            continue
        blob.append(p.read_text(encoding='utf-8'))
    used_blob = '\n'.join(blob)

    def _referenced(fname):
        if re.search(r'\b' + re.escape(fname) + r'\b', used_blob):
            return True
        # 常见写法：配置 log_max_size_mb，代码里用局部变量 max_size_mb
        for pref in ('log_', 'cloud_', 'http_', 'ftp_', 'relay_'):
            if fname.startswith(pref):
                short = fname[len(pref):]
                if re.search(r'\b' + re.escape(short) + r'\b', used_blob):
                    return True
        return False

    unused = [f for f in fields if not _referenced(f)]
    if unused:
        for f in unused:
            WARN.append('unused-cfg {}'.format(f))
            print('  [WARN] 未被读取: {}'.format(f))
    else:
        PASS.append('unused-cfg')
        print('  [PASS] 所有 {} 个字段都有被读取'.format(len(fields)))


def check_config_duplication():
    """配置双份漂移：dist 与源码树各有一份 zerofall_config.json

    现役的是 dist 那份（程序同级）。源码树那份只在"从源码跑"时生效，
    两份不一致时，调试读到的值会和实际运行的不同 —— 这种坑很难现场看出来。
    """
    print("[9] 检查配置文件重复")
    dist = ROOT / "dist" / "zerofall_config.json"
    src = ROOT / "zerofall_config.json"
    if not (dist.is_file() and src.is_file()):
        print("  [SKIP] 只有一份配置")
        return
    try:
        a = json.loads(dist.read_text(encoding="utf-8"))
        b = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        WARN.append("配置无法解析: {}".format(e))
        print("  [WARN] 配置解析失败: {}".format(e))
        return
    diff = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    if diff:
        WARN.append("配置双份不一致: {} 项 ({})".format(len(diff), ", ".join(sorted(diff)[:6])))
        print("  [WARN] dist 与源码树配置有 {} 项不同（以 dist 为现役）：{}".format(
            len(diff), ", ".join(sorted(diff)[:6])))
        print("  提示：从源码调试时会读到源码树那份，容易误判；建议保持一致")
    else:
        print("  [PASS] 两份配置一致")


def check_syntax_all():
    print('\n[6] 全项目语法')
    bad = []
    for p in py_files():
        try:
            ast.parse(p.read_text(encoding='utf-8'))
        except SyntaxError as e:
            bad.append((p.relative_to(ROOT), e.lineno, e.msg))
    # 测试文件也过一遍
    for p in ROOT.glob('test_*.py'):
        try:
            ast.parse(p.read_text(encoding='utf-8'))
        except SyntaxError as e:
            bad.append((p.relative_to(ROOT), e.lineno, e.msg))
    if bad:
        for f, ln, msg in bad:
            FAIL.append('syntax {}:{}'.format(f, ln))
            print('  [FAIL] {}:{} {}'.format(f, ln, msg))
    else:
        n = len(list(py_files())) + len(list(ROOT.glob('test_*.py')))
        PASS.append('syntax')
        print('  [PASS] {} 个文件全部通过'.format(n))


def check_bare_except_pass():
    """except: pass 会吞掉真错误（之前 0 值 bug 就被吞了一半）"""
    print('\n[7] 检查吞异常的 except')
    hits = []
    for p in py_files():
        lines = p.read_text(encoding='utf-8').splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s in ('except:', 'except Exception:'):
                # 看下两行是否只有 pass
                nxt = [l.strip() for l in lines[i+1:i+3] if l.strip()]
                if nxt and nxt[0] == 'pass':
                    hits.append((p.relative_to(ROOT), i + 1))
    if hits:
        print('  [INFO] 共 {} 处 except→pass（多数是合理的兜底）'.format(len(hits)))
        for f, ln in hits[:6]:
            print('     {}:{}'.format(f, ln))
        PASS.append('except-pass-informational')
    else:
        PASS.append('except-pass')
        print('  [PASS] 没有')


def check_version_consistency():
    """版本号散落在 5 个地方，任何一处漏改都会让界面/UA/包名对不上

    这不是假想问题：1.3.0 升到 1.5.0 时，界面上显示的 QLabel 和发出去的
    User-Agent 就各漏了一次，靠人工比对才发现。
    """
    print('\n[8] 检查版本号一致性')
    import re
    # (相对路径, 正则) —— 捕获组 1 只取纯版本号，如 1.5.0
    spots = [
        ('modules/__init__.py',      r'__version__\s*=\s*["\']([\d.]+)["\']'),
        ('build_release.py',         r"VERSION\s*=\s*['\"]v([\d.]+)['\"]"),
        ('main.py',                  r'setApplicationVersion\(\s*["\']([\d.]+)["\']'),
        ('ui/main_window.py',        r'QLabel\(\s*["\']v([\d.]+)["\']'),
        ('modules/net_transport.py', r'ZeroFall-Agent/([\d.]+)'),
    ]
    found, missing = {}, []
    for rel, pat in spots:
        p = ROOT / rel
        if not p.exists():
            missing.append(rel)
            continue
        m = re.search(pat, p.read_text(encoding='utf-8'))
        if m:
            found[rel] = m.group(1)
        else:
            missing.append(rel)

    if not found:
        WARN.append('version-none')
        print('  [WARN] 一处版本号都没匹配到（正则可能需要跟着代码改）')
        return

    versions = sorted(set(found.values()))
    if len(versions) == 1 and not missing:
        PASS.append('version')
        print('  [PASS] {} 处全部一致: v{}'.format(len(found), versions[0]))
        return

    for rel, v in sorted(found.items()):
        WARN.append('version {}={}'.format(rel, v))
        print('  [WARN] {:<28} v{}'.format(rel, v))
    for rel in missing:
        WARN.append('version-missing {}'.format(rel))
        print('  [WARN] {:<28} 未匹配到版本号'.format(rel))
    if len(versions) > 1:
        print('  提示：统一成 v{}（modules/__init__.py 的 __version__ 是权威值）'.format(
            found.get('modules/__init__.py', versions[-1])))


if __name__ == '__main__':
    print('=' * 70)
    print('ZeroFall 项目自检')
    print('=' * 70)
    check_syntax_all()
    check_or_zero()
    check_called_methods()
    check_duplicated_class_body()
    check_dup_functions()
    check_unused_config_fields()
    check_bare_except_pass()
    check_version_consistency()
    check_config_duplication()
    print()
    print('=' * 70)
    print('通过 {} / 提示 {} / 失败 {}'.format(len(PASS), len(WARN), len(FAIL)))
    if FAIL:
        print('\n需要修的问题:')
        for f in FAIL:
            print('  ✗', f)
    if WARN:
        print('\n值得看一眼:')
        for w in WARN:
            print('  !', w)
    print('=' * 70)
    sys.exit(0 if not FAIL else 1)
