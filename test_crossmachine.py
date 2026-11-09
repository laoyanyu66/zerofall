# -*- coding: utf-8 -*-
"""测试跨机自适应：模拟配置指向不存在路径时能否自动探测修正"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import ConfigManager, ZeroFallConfig

print('=== 1. 本机探测能力 ===')
cm = ConfigManager(r'D:\zerofall\zerofall_config.json')
detected = cm.auto_detect_paths()
for k, v in detected.items():
    ok = ''
    if v:
        p = Path(v)
        if k == 'deepseek_exe_path':
            ok = '存在' if p.is_file() else '不存在'
        elif k in ('deepseek_plugins_dir', 'deepseek_config_dir'):
            ok = '存在' if p.is_dir() else '不存在'
        else:
            ok = '父目录' + ('存在' if p.parent.exists() else '不存在')
    print('  {:<24} {:<48} {}'.format(k, v or '(未探测到)', ok))

print()
print('=== 2. 模拟"换机器"：配置全部指向不存在的路径 ===')
tmp = Path(tempfile.mkdtemp(prefix='zf_crossmachine_'))
fake_cfg = tmp / 'zerofall_config.json'
fake_cfg.write_text(json.dumps({
    'deepseek_exe_path': r'Z:\NoSuchDrive\DeepSeek Harness.exe',
    'deepseek_plugins_dir': r'C:\Users\__no_such_user__\.dsh',
    'deepseek_config_dir': r'Z:\NoSuchDrive\resources\runtime',
    'snapshot_dir': r'Z:\NoSuchDrive\dsh备份',
    'cloud_sync_dir': r'Z:\NoSuchDrive\BaiduSyncdisk',
    'baidu_pan_enabled': False,
}, ensure_ascii=False, indent=2), encoding='utf-8')

cm2 = ConfigManager(str(fake_cfg))
print('  加载前 deepseek_exe_path =', json.loads(fake_cfg.read_text(encoding='utf-8'))['deepseek_exe_path'])
print('  是否判定需要路径设置:', cm2.needs_path_setup(cm2.load() if False else ZeroFallConfig.from_dict(json.loads(fake_cfg.read_text(encoding='utf-8')))))
print()
cfg2 = cm2.load()
print('  加载后（自动修正）:')
for k in ('deepseek_exe_path', 'deepseek_plugins_dir', 'deepseek_config_dir', 'snapshot_dir', 'cloud_sync_dir'):
    print('    {:<24} = {}'.format(k, getattr(cfg2, k, '')))

print()
print('=== 3. 断言 ===')
ok = True
def chk(cond, desc):
    global ok
    print(('  [OK]  ' if cond else '  [FAIL]') + ' ' + desc)
    ok = ok and cond

chk(bool(cfg2.deepseek_exe_path) and Path(cfg2.deepseek_exe_path).is_file(),
    'DeepSeek 路径已自动探测到真实文件')
chk(Path(cfg2.deepseek_plugins_dir).is_dir(),
    '.dsh 目录已自动探测到真实目录')
chk('Z:' not in cfg2.snapshot_dir,
    '快照目录已从无效的 Z: 盘迁到有效位置: ' + cfg2.snapshot_dir)
chk('__no_such_user__' not in cfg2.deepseek_plugins_dir,
    '插件目录不再是无效用户名路径')

print()
print('最终:', '全部通过' if ok else '存在失败')

# 清理
shutil.rmtree(tmp, ignore_errors=True)
print('临时目录已清理')
