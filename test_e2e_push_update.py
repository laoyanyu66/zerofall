# -*- coding: utf-8 -*-
"""端到端：报错上报 -> 主控自动修复 -> 推更新包 -> 子控自动装上

全程隔离，不动现有安装：
  SRC     = 源码副本（ACP 只改副本，不碰真实源码）
  CHAN    = 临时通道目录
  INSTALL = 模拟子控安装目录（放一份真实 exe，供替换验证）

真实环节（不是打桩）：
  * 真 ACP：拉起 zf-acp profile 的 DSH 会话修 bug（约 30-40s）
  * 真 pyinstaller：重新打包 exe（约 90-120s）
  * 真 HTTP：主控起内建服务器，子控走 HTTP 拉清单/下载
  * 真 sha256 校验 + 真原子替换 + 备份

全程约 3-5 分钟。
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS, FAIL = [], []


def chk(cond, desc, extra=''):
    (PASS if cond else FAIL).append(desc)
    mark = '[OK]  ' if cond else '[FAIL]'
    print('  {} {}'.format(mark, desc) + ('  ' + extra if extra else ''))


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


BUGGY = '''# -*- coding: utf-8 -*-
"""端到端测试用的故意出错模块"""


def average(numbers):
    """求平均值（故意漏掉空列表保护）"""
    return sum(numbers) / len(numbers)
'''



# ── 凭据前置检查 ─────────────────────────────────────────────
# ACP 修复要真的调模型，必须能解析出 OPENCODE_GO_API_KEY。
# 它来自 ZeroFall 自持密钥库，而密钥库位置由 ZF_DATA_DIR 决定：
# 没设这个变量时会指向当前工作目录（源码目录），那里没有 secrets.json，
# 于是整条修复链路失败，表现为"ACP 修复成功"等 10 项静默 FAIL，
# 看起来像代码坏了，其实只是跑测试的环境没带数据目录。
_PROD_DATA_DIR = r'D:\zerofall'
if not os.environ.get('ZF_DATA_DIR') and os.path.isdir(_PROD_DATA_DIR):
    os.environ['ZF_DATA_DIR'] = _PROD_DATA_DIR
    print('[env] 未设 ZF_DATA_DIR，已自动指向生产目录 {}'.format(_PROD_DATA_DIR))
try:
    from modules import secret_store as _ss
    _HAS_CRED = bool(_ss.get('OPENCODE_GO_API_KEY'))
except Exception:
    _HAS_CRED = False
if not _HAS_CRED:
    print('=' * 64)
    print('[跳过] 找不到 OPENCODE_GO_API_KEY，ACP 修复类断言无法验证。')
    print('       请先设置 ZF_DATA_DIR 指向含 secrets.json 的数据目录，例如：')
    print('       set ZF_DATA_DIR=D:\\zerofall')
    print('=' * 64)
    sys.exit(2)


def main():
    total_t0 = time.time()

    # ═══ 0. 沙箱 ═══
    print('=== 0. 准备隔离环境 ===')
    sand = Path(tempfile.mkdtemp(prefix='zf_e2e_'))
    src = sand / 'src'
    chan = sand / 'chan'
    install = sand / 'install'
    src.mkdir()
    install.mkdir()

    # 复制源码（排除产物目录）
    excl = {'build', 'dist', '__pycache__', 'logs', '.git'}
    n = 0
    for p in ROOT.rglob('*'):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in excl for part in rel.parts):
            continue
        dst = src / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        n += 1
    print('  源码副本: {} 个文件'.format(n))

    # 注入 bug
    bug = src / 'modules' / 'e2e_sample.py'
    bug.write_text(BUGGY, encoding='utf-8')
    before_hash = sha256(bug)
    print('  待修文件: modules/e2e_sample.py')
    print('  修复前  : sum([])/len([]) 会 ZeroDivisionError')

    # 模拟子控安装目录：放一份真实 exe
    real_exe = Path(r'D:\zerofall\ZeroFall.exe')
    if not real_exe.exists():
        real_exe = ROOT / 'dist' / 'ZeroFall.exe'
    shutil.copy2(real_exe, install / 'ZeroFall.exe')
    old_exe_hash = sha256(install / 'ZeroFall.exe')
    print('  子控 exe: {:.2f} MB'.format((install / 'ZeroFall.exe').stat().st_size / 1048576))

    port = free_port()
    token = 'e2e-token-{}'.format(int(time.time()))
    print('  HTTP 端口: {} | 令牌: {}'.format(port, token[:12] + '...'))

    # ═══ 1. 主控起来（含 HTTP 服务器）═══
    print()
    print('=== 1. 主控启动 + 内建更新服务器 ===')
    from modules.master import MasterNode
    from modules.control import ControlChannel, AgentNode
    from modules.net_transport import MasterHttpServer

    class MCfg:
        role = 'master'
        control_channel_dir = str(chan)
        master_source_dir = str(src)
        auto_fix_enabled = True
        master_self_update = False        # 不碰真实安装
        acp_profile = 'zf-acp'
        report_to_master = True
        snapshot_dir = str(sand / 'snaps')
        http_server_enabled = True
        http_port = port
        http_token = token

    class SCfg(MCfg):
        role = 'agent'

    mcfg = MCfg()
    master = MasterNode(mcfg)
    srv = MasterHttpServer(str(chan), port=port, token=token)
    chk(srv.start() is True, '内建更新服务器启动 (端口 {})'.format(port))
    time.sleep(0.3)
    chk(master.source_dir == src, '主控识别到源码副本')
    chk((master.source_dir / 'main.py').exists(), '源码副本 main.py 存在')

    # ═══ 2. 子控上报 ═══
    print()
    print('=== 2. 子控通过 HTTP 上报错误 ===')
    acfg = SCfg()
    acfg.master_url = 'http://127.0.0.1:{}'.format(port)
    acfg.agent_id = 'e2e-agent-01'
    acfg.auto_apply_update = True
    agent = AgentNode(acfg)
    # 子控通道现在统一是 HybridChannel（HTTP → 中继 → 共享目录，按可用性降级）。
    # 这条配置只给了 master_url，所以要验证的是"HTTP 腿真的在起作用"，
    # 而不是通道类的名字。
    _ch = agent.channel
    _http_leg = getattr(_ch, '_http', None)
    chk(_http_leg is not None and hasattr(_http_leg, '_probe'),
        '子控采用 HTTP 通道（HybridChannel 的 HTTP 腿）',
        type(_ch).__name__)
    chk(agent.channel.available() is True, '子控能连上主控')

    tb = ('Traceback (most recent call last):\n'
          '  File "modules/e2e_sample.py", line 7, in average\n'
          '    return sum(numbers) / len(numbers)\n'
          'ZeroDivisionError: division by zero\n')
    agent.report('zf_error', {
        'error': 'ZeroDivisionError: division by zero',
        'traceback': tb,
        'file_hint': 'modules/e2e_sample.py',
    })
    time.sleep(0.4)
    pend = ControlChannel(str(chan)).list_reports(only_new=True)
    chk(len(pend) == 1, '主控收到 1 条报告')
    if pend:
        chk(pend[0][1].get('agent_id') == 'e2e-agent-01', 'agent_id 正确')

    # ═══ 3. 主控自动修复（真 ACP）═══
    print()
    print('=== 3. 主控自动修复（真 ACP 驱动 DSH，约 30-40s）===')
    t0 = time.time()
    result = master.tick()
    fix_s = time.time() - t0
    print('  用时: {:.0f}s'.format(fix_s))
    chk(result is not None, '主控处理了报告')
    if not result:
        print('  中断：主控没返回结果')
        return finish(sand, srv)

    fix = result.get('fix') or {}
    print('  修复结果: ok={} stop={}'.format(
        fix.get('ok'), str(fix.get('stop_reason') or '')[:40]))
    chk(fix.get('ok') is True, 'ACP 修复成功')

    # ═══ 4. 源码真的被改了吗 ═══
    print()
    print('=== 4. 验证源码改动 ===')
    after_hash = sha256(bug)
    changed = after_hash != before_hash
    chk(changed, 'bug 文件内容已变化')
    new_text = bug.read_text(encoding='utf-8')
    if changed:
        print('  --- 修复后 ---')
        for line in new_text.strip().splitlines()[:14]:
            print('    ' + line)
        has_guard = ('if not numbers' in new_text or 'len(numbers) == 0' in new_text
                     or 'if len(numbers)' in new_text or 'ValueError' in new_text
                     or 'or 1' in new_text)
        chk(has_guard, '代码里出现了空列表保护')

    # ═══ 5. 更新包与发布 ═══
    print()
    print('=== 5. 打包 + 发布更新包 ===')
    pkg = result.get('package') or {}
    build = result.get('build') or {}
    chk(build.get('ok') is True, 'pyinstaller 重新打包成功')
    chk(bool(pkg.get('path')), '更新包已产出')
    if pkg.get('path'):
        p = Path(pkg['path'])
        chk(p.exists(), '更新包文件存在', '{:.2f} MB'.format(p.stat().st_size / 1048576))
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
        chk('manifest.json' in names, '包内含 manifest.json')
        chk('ZeroFall.exe' in names, '包内含 ZeroFall.exe')
        chk(any(n.startswith('source') for n in names), '包内含 source 源码')
    chk(bool(result.get('published')), '已发布到通道')

    idx_file = chan / 'updates' / 'index.json'
    chk(idx_file.exists(), 'updates/index.json 已生成')
    if idx_file.exists():
        idxd = json.loads(idx_file.read_text(encoding='utf-8'))
        print('  清单: version={} file={} sha256={}...'.format(
            idxd.get('version'), idxd.get('file'), str(idxd.get('sha256'))[:16]))
        chk(bool(idxd.get('version')), '清单含版本号')
        chk(bool(idxd.get('sha256')), '清单含 sha256')

    # ═══ 6. 子控 HTTP 拉取并安装 ═══
    print()
    print('=== 6. 子控通过 HTTP 拉更新并自动安装 ===')
    applied, msg = agent.poll_update(install_dir=install, restart=False)
    print('  poll_update -> applied={} msg={}'.format(applied, msg[:70]))
    chk(applied is True, '子控应用更新成功')

    new_exe_hash = sha256(install / 'ZeroFall.exe')
    chk(new_exe_hash != old_exe_hash, '安装目录的 exe 已被替换')
    bak = list(install.glob('ZeroFall.exe.bak-*'))
    chk(len(bak) >= 1, '旧版已备份', bak[0].name if bak else '')
    if bak:
        chk(sha256(bak[0]) == old_exe_hash, '备份内容 == 更新前原文件')

    # ═══ 7. 回执 ═══
    print()
    print('=== 7. 子控回执 ===')
    applied_files = list((chan / 'applied').glob('*'))
    chk(len(applied_files) >= 1, 'applied/ 里有回执')
    if applied_files:
        print('  回执: ' + applied_files[-1].name)

    # ═══ 8. 版本幂等：再拉一次不该重复装 ═══
    print()
    print('=== 8. 幂等：已是最新则不重复下载 ===')
    applied2, msg2 = agent.poll_update(install_dir=install, restart=False)
    chk(applied2 is False, '第二次不再触发更新')
    print('  说明: ' + msg2[:70])

    return finish(sand, srv, time.time() - total_t0)


def finish(sand, srv, elapsed=None):
    print()
    print('=== 清理 ===')
    try:
        srv.stop()
        print('  服务器已停止')
    except Exception:
        pass
    try:
        shutil.rmtree(sand, ignore_errors=True)
        print('  沙箱已删除: {}'.format(sand))
    except Exception:
        pass

    print()
    print('=' * 62)
    if elapsed:
        print('总耗时: {:.0f}s'.format(elapsed))
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    for f in FAIL:
        print('  - ' + f)
    return 0 if not FAIL else 1


if __name__ == '__main__':
    raise SystemExit(main())
