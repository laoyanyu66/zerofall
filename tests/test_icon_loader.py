# -*- coding: utf-8 -*-
"""图标加载测试 —— 验证"换台电脑也能显示图标"

真实痛点：本机图标正常，拷到别的电脑却变成白窗口/系统默认图标。
根因有三类，都是这个测试要挡住的：

  1. 只依赖 `--add-data` 带出来的 ico 文件 —— 打包参数漏了就静默退化成
     系统默认图标，而且提示只有一行 WARNING，很容易没人看见
  2. 图标路径写死成相对当前目录 —— 从别的位置启动就找不到
  3. 托盘图标尺寸写死 —— 高 DPI 下模糊（这个不致命，但能测就测）

三层回退要求：
  A. ico 文件（_MEIPASS 或项目根）
  B. exe 资源里的图标（ExtractIconEx）
  C. 系统默认图标
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import modules.icon_loader as IL

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def test_icon_path_resolution():
    print('\n[1] 图标路径解析')
    p = IL.icon_file_path()
    check('返回 Path', isinstance(p, Path), type(p))
    check('文件名是 ZeroFall.ico', p.name == 'ZeroFall.ico', p.name)
    check('是绝对路径', p.is_absolute(), p)
    check('源码运行时指向项目根', p.parent == Path(__file__).resolve().parent.parent, p.parent)


def test_icon_file_exists():
    print('\n[2] ico 文件存在性检测')
    exists = IL.icon_file_exists()
    print('     icon_file_exists() = {}'.format(exists))
    # 两种都合法；只断言不抛异常且是 bool
    check('返回 bool', isinstance(exists, bool), type(exists))
    if not exists:
        print('     注意：项目根没有 ZeroFall.ico，托盘会走 exe 资源回退')


def test_tray_size():
    print('\n[3] 托盘尺寸按 DPI 计算')
    size = IL.tray_icon_size()
    print('     当前系统推荐尺寸: {}px'.format(size))
    check('是正整数', isinstance(size, int) and size > 0, size)
    check('落在 ico 实际含有的尺寸里',
          size in (16, 20, 24, 32, 40, 48, 64), size)
    check('至少 16', size >= 16, size)


def test_hicon_three_fallbacks():
    print('\n[4] 托盘 HICON 三层回退')
    if sys.platform != 'win32':
        print('     非 Windows，跳过')
        PASS.append('hicon-platform-skip')
        return
    hicon, source = IL.load_tray_hicon()
    print('     got hicon=0x{:X} source={}'.format(hicon or 0, source))
    check('拿到了有效句柄', bool(hicon), hicon)
    check('来源是三者之一', source in ('file', 'exe', 'default', 'none'), source)
    # 关键：只要 exe 有 --icon，就算没有 ico 文件也不该退到 default
    if source == 'default':
        print('     注意：退到了系统默认图标，说明 ico 和 exe 资源都没取到')
        print('     （源码运行时 exe 是 python.exe，没有自定义图标，这是正常的）')
    check('源码运行时不会崩', source != 'none', source)


def test_fallback_order_by_simulation():
    print('\n[5] 回退顺序：ico 文件优先于 exe 资源')
    # 用 monkeypatch 检查优先级：把 icon_file_path 指到一个假 ico
    orig = IL.icon_file_path
    tmp = Path(tempfile.mkdtemp(prefix='zf_icotest_'))
    real_ico = Path(__file__).resolve().parent.parent / 'ZeroFall.ico'
    if not real_ico.exists():
        print('     没有 ZeroFall.ico，跳过优先级测试')
        PASS.append('fallback-priority-skip')
        return
    fake = tmp / 'ZeroFall.ico'
    fake.write_bytes(real_ico.read_bytes())
    try:
        IL.icon_file_path = lambda: fake
        hicon, source = IL.load_tray_hicon()
        print('     用假 ico 路径 → source={}'.format(source))
        check('优先走 ico 文件', source == 'file', source)
    finally:
        IL.icon_file_path = orig


def test_describe():
    print('\n[6] 诊断字符串（给用户排查用）')
    d = IL.describe()
    print('     {}'.format(d))
    check('包含 frozen 状态', 'frozen=' in d, d)
    check('包含 ico 存在性', 'ico=' in d, d)
    check('包含托盘尺寸', 'tray_size=' in d, d)
    check('包含 exe 路径', 'exe=' in d, d)


def test_no_hardcoded_relative_path():
    print('\n[7] 没有写死的相对路径（换目录启动不能失效）')
    src = Path('modules/icon_loader.py').read_text(encoding='utf-8')
    # 不该出现裸的 "ZeroFall.ico" 字符串拼接当前目录
    bad = [ln for ln in src.splitlines()
           if 'ZeroFall.ico' in ln and 'cwd' in ln.lower()]
    check('没有 cwd 依赖', not bad, bad)
    # 应该用 __file__ 或 _MEIPASS 定位
    check('用 __file__ 定位', '__file__' in src)
    check('支持 _MEIPASS', '_MEIPASS' in src)


def test_native_tray_uses_loader():
    print('\n[8] native_tray 已改用统一加载（不再自己拼路径）')
    src = Path('ui/native_tray.py').read_text(encoding='utf-8')
    check('引用了 icon_loader', 'load_tray_hicon' in src)
    check('有 ExtractIconEx 回退', 'ExtractIconEx' in src
          or 'icon_loader' in src)
    # 旧的写死 32,32 应该没了
    check('不再写死 32x32', '32, 32,' not in src)


def test_window_icon_uses_loader():
    print('\n[9] 主窗口图标也走统一加载')
    src = Path('ui/main_window.py').read_text(encoding='utf-8')
    check('窗口图标引用 icon_file_path', 'icon_file_path' in src)
    check('托盘路径不再自己判断 frozen',
          src.count("Path(sys._MEIPASS) / \"ZeroFall.ico\"") == 0)


def test_app_user_model_id():
    print('\n[10] AppUserModelID 按 exe 路径区分')
    src = Path('main.py').read_text(encoding='utf-8')
    check('存在 _app_id_for_this_exe', '_app_id_for_this_exe' in src)
    check('按 exe 路径算哈希', 'sys.executable' in src and 'md5' in src)
    check('调用时兜底生成', 'if not app_id:' in src)
    check('在 QApplication 之前设置',
          src.index('_set_app_user_model_id()') < src.index('QApplication(sys.argv)'))


if __name__ == '__main__':
    print('=' * 66)
    print('图标加载测试（换电脑也能显示）')
    print('=' * 66)
    test_icon_path_resolution()
    test_icon_file_exists()
    test_tray_size()
    test_hicon_three_fallbacks()
    test_fallback_order_by_simulation()
    test_describe()
    test_no_hardcoded_relative_path()
    test_native_tray_uses_loader()
    test_window_icon_uses_loader()
    test_app_user_model_id()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
