# -*- coding: utf-8 -*-
"""云同步窗口模式（2026-11-13）

背景：百度网盘常驻内存实测 12 个 BaiduNetdisk* 进程 / 1.99 GB，系统空闲
只剩 1.9 GB（11.9%）。同步盘方案的成本不在"写文件"，而在客户端必须一直
在线才能把文件传上去。窗口模式把写入集中到每天一小段，其余时间客户端可以
完全不跑。

验收点：
  A. 窗口边界（正常窗口 + 跨午夜）
  B. 模式开关：只有 windowed 受窗口限制，always/manual 不受
  C. 每日上限被两条上传路径共用（outbox 里的包也计入已用量）
  D. outbox → 同步盘搬移：限量、已存在跳过、meta 跟随
  E. 窗口外溢出归档落在 outbox，不写同步盘
  F. 静态检查：只剩一条"最新快照"定时器（改前两条同频重复跑）
"""
import ast
import json
import os
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from ui.main_window import ZeroFallMainWindow as MW  # noqa: E402

PASS, FAIL = [], []
ROOT = Path(__file__).resolve().parent.parent


def chk(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  {}".format(extra)))


class Cfg:
    """只放窗口相关字段，其余交给 _cfg_int 的默认值"""
    cloud_sync_enabled = True
    cloud_sync_dir = ""
    cloud_sync_encrypt = True
    cloud_client_mode = 'windowed'
    cloud_window_trigger = 'fixed'      # 老用例按定点语义验；机会模式见 G 段
    cloud_sync_cadence_days = 1
    cloud_window_start = '03:30'
    cloud_window_minutes = 60
    cloud_window_max_packages = 8
    cloud_window_auto_close = True
    cloud_window_settle_minutes = 8
    cloud_window_start_delay_minutes = 5
    cloud_window_idle_minutes = 5
    cloud_window_gap_hours = 20
    cloud_window_force_hours = 36
    cloud_outbox_enabled = True
    cloud_outbox_dir = ""
    cloud_outbox_max = 6
    cloud_upload_max_per_day = 24
    cloud_overflow_enabled = True
    cloud_overflow_light_policy = 'archive'
    cloud_upload_per_cycle = 0
    snapshot_dir = ""
    daily_cloud_consolidate = True


class Snap:
    def __init__(self, sid, created_at, is_full=False):
        self.snapshot_id = sid
        self.created_at = created_at
        self.is_full = is_full
        self.archive_path = str(Path(tempfile.gettempdir()) / (sid + '.zip'))


class SM:
    def __init__(self, overflow, fulls=None):
        self._overflow = overflow
        self._fulls = fulls or []
        self.deleted = []

    def prune_missing_entries(self):
        pass

    def get_overflow_snapshots(self):
        return list(self._overflow), [], []

    def get_full_snapshots(self):
        return list(self._fulls)

    def delete_snapshot(self, sid):
        self.deleted.append(sid)
        return True


class Host:
    """把真实方法绑到一只只带 config 的壳上（不构造 QMainWindow）"""

    _BINDS = (
        '_cloud_client_mode', '_cloud_window_bounds', '_cloud_in_window',
        '_cloud_outbox_path', '_cloud_outbox_count', '_cloud_budget_left',
        '_cloud_uploads_today', '_flush_outbox_to_cloud',
        '_handle_overflow_to_cloud', '_cfg_int',
        '_cloud_window_trigger', '_cloud_should_open_window', '_cloud_has_backlog',
        '_cloud_system_idle_seconds',
        '_cloud_cadence_days', '_cloud_window_gap_seconds',
        '_cloud_outbox_limit', '_cloud_window_pkg_limit', '_compact_outbox',
        '_cloud_window_begin', '_cloud_window_end', '_cloud_window_tick_auto',
    )

    def __init__(self, cfg, sm=None):
        self.config = cfg
        self.snapshot_manager = sm
        self._cloud_last_write_at = 0
        self._archive_busy = False
        self._archive_fail_count = {}
        for _n in self._BINDS:
            setattr(self, _n, types.MethodType(getattr(MW, _n), self))


def mk_pkg(d, name, size=1024):
    p = d / name
    p.write_bytes(b'x' * size)
    # 跟 _sync_one_snapshot 一致：meta 用 zip 基名，不是 .zip.enc.meta.json
    base = name[:-4] if name.endswith('.enc') else name
    (d / (base + '.meta.json')).write_bytes(b'{}')
    return p


def test_window_bounds():
    print("A. 窗口边界")
    cfg = Cfg()
    h = Host(cfg)

    now = datetime(2026, 11, 13, 3, 45)
    s, e, is_open = h._cloud_window_bounds(now)
    chk(is_open, '03:45 在 03:30+60min 窗口内', (s, e, is_open))
    chk(s.strftime('%H:%M') == '03:30' and e.strftime('%H:%M') == '04:30',
        '窗口边界 03:30-04:30', (s.strftime('%H:%M'), e.strftime('%H:%M')))

    now = datetime(2026, 11, 13, 2, 30)
    s, e, is_open = h._cloud_window_bounds(now)
    chk(not is_open, '02:30 不在窗口内')
    chk(s.strftime('%Y%m%d %H:%M') == '20261113 03:30', '窗口外回报下一个窗口',
        s.strftime('%Y%m%d %H:%M'))

    now = datetime(2026, 11, 13, 5, 0)
    chk(not h._cloud_window_bounds(now)[2], '05:00 已过窗口')

    # 跨午夜：23:40 起 60 分钟 → 次日 00:40
    cfg.cloud_window_start = '23:40'
    now = datetime(2026, 11, 14, 0, 10)
    s, e, is_open = h._cloud_window_bounds(now)
    chk(is_open, '跨午夜 00:10 仍在窗口内', (s, e))
    chk(s.strftime('%Y%m%d %H:%M') == '20261113 23:40', '窗口起点算在昨天',
        s.strftime('%Y%m%d %H:%M'))
    now = datetime(2026, 11, 14, 0, 50)
    chk(not h._cloud_window_bounds(now)[2], '跨午夜 00:50 已出窗口')

    # 时间串写坏时兜底成 03:30，不抛异常
    cfg.cloud_window_start = '乱写'
    cfg.cloud_window_minutes = 60
    now = datetime(2026, 11, 13, 3, 40)
    chk(h._cloud_window_bounds(now)[2], '非法时间串回落 03:30', cfg.cloud_window_start)


def test_mode_switch():
    print("B. 模式开关")
    cfg = Cfg()
    cfg.cloud_window_start = '03:30'
    h = Host(cfg)
    outside = datetime(2026, 11, 13, 15, 0)

    cfg.cloud_client_mode = 'windowed'
    chk(not h._cloud_in_window(outside), 'windowed 窗口外拦住了')
    chk(h._cloud_in_window(datetime(2026, 11, 13, 3, 40)), 'windowed 窗口内放行')

    cfg.cloud_client_mode = 'always'
    chk(h._cloud_in_window(outside), 'always 不受窗口限制')
    cfg.cloud_client_mode = 'manual'
    chk(h._cloud_in_window(outside), 'manual 不受窗口限制')
    cfg.cloud_client_mode = 'WeIrD'
    chk(h._cloud_client_mode() == 'windowed', '非法值回落 windowed', h._cloud_client_mode())


def test_budget():
    print("C. 每日上限两条路径共用")
    root = Path(tempfile.mkdtemp(prefix='zf_cw_'))
    syncdir = root / 'sync'
    syncdir.mkdir()
    outbox = root / 'outbox'
    outbox.mkdir()

    cfg = Cfg()
    cfg.cloud_sync_dir = str(syncdir)
    cfg.cloud_outbox_dir = str(outbox)
    cfg.snapshot_dir = str(root)
    h = Host(cfg)

    for i in range(2):
        mk_pkg(syncdir, 'snapshot_20261113_10{:02d}00.zip.enc'.format(i))
    chk(h._cloud_uploads_today() == 2, '同步盘今天 2 个包', h._cloud_uploads_today())

    for i in range(3):
        mk_pkg(outbox, 'snapshot_20261113_11{:02d}00.zip.enc'.format(i))
    cfg.cloud_upload_max_per_day = 24
    chk(h._cloud_budget_left() == 19, '上限 24，已用 2+3=5，剩 19', h._cloud_budget_left())

    cfg.cloud_upload_max_per_day = 0
    chk(h._cloud_budget_left() == -1, '上限 0 = 不限', h._cloud_budget_left())

    cfg.cloud_upload_max_per_day = 5
    chk(h._cloud_budget_left() == 0, '到顶就返回 0（不再传）', h._cloud_budget_left())
    shutil.rmtree(root, ignore_errors=True)


def test_flush_outbox():
    print("D. outbox → 同步盘搬移")
    root = Path(tempfile.mkdtemp(prefix='zf_cw2_'))
    syncdir = root / 'sync'
    syncdir.mkdir()
    outbox = root / 'outbox'
    outbox.mkdir()

    cfg = Cfg()
    cfg.cloud_sync_dir = str(syncdir)
    cfg.cloud_outbox_dir = str(outbox)
    cfg.snapshot_dir = str(root)
    cfg.cloud_window_max_packages = 2
    cfg.cloud_upload_max_per_day = 0
    h = Host(cfg)

    for i in range(4):
        mk_pkg(outbox, 'snapshot_20261113_12{:02d}00.zip.enc'.format(i), size=2048)
    # 一个"已经搬过"的：同步盘里有同名同大小，应当只清 outbox 副本、不占窗口配额
    mk_pkg(syncdir, 'snapshot_20261113_120000.zip.enc', size=2048)

    moved = h._flush_outbox_to_cloud()
    chk(moved == 2, '每窗口上限 2 个（已存在的那个不占配额）', moved)
    chk(len([f for f in syncdir.glob('snapshot_*.zip.enc')]) == 3,
        '同步盘 3 个（原有 1 + 新搬 2）',
        [f.name for f in syncdir.glob('snapshot_*.zip.enc')])
    chk(len([f for f in outbox.glob('snapshot_*.zip.enc')]) == 1,
        'outbox 还剩 1 个', [f.name for f in outbox.glob('snapshot_*.zip.enc')])
    chk((syncdir / 'snapshot_20261113_120100.zip.meta.json').exists(),
        '搬过去的包 meta 一起跟随')
    chk(h._cloud_outbox_count() == 1, 'outbox 计数为 1', h._cloud_outbox_count())
    shutil.rmtree(root, ignore_errors=True)


def test_overflow_to_outbox():
    print("E. 窗口外溢出归档落 outbox")
    root = Path(tempfile.mkdtemp(prefix='zf_cw3_'))
    syncdir = root / 'sync'
    syncdir.mkdir()
    outbox = root / 'outbox'

    cfg = Cfg()
    cfg.cloud_sync_dir = str(syncdir)
    cfg.cloud_outbox_dir = str(outbox)
    cfg.snapshot_dir = str(root)
    # 让"窗口"落在很远的地方，确保当前不在窗口内
    cfg.cloud_window_start = '03:30'
    cfg.cloud_window_minutes = 60
    h_now = datetime.now()
    cfg.cloud_window_start = '{:02d}:{:02d}'.format(
        (h_now.hour + 12) % 24, h_now.minute)      # 12 小时外，必然不在窗口

    light = [Snap('L1', '20261113_100000'), Snap('L2', '20261113_101000')]
    sm = SM(light)
    h = Host(cfg, sm)
    targets = []
    h._archive_snapshot_to_cloud = lambda s, d, e: (targets.append(Path(d)), True)[1]
    h._cloud_uploads_today = lambda: 0

    h._handle_overflow_to_cloud()
    chk(all(t == Path(outbox) for t in targets), '归档目标全部是 outbox',
        [str(t) for t in targets])
    chk(not list(syncdir.glob('snapshot_*')), '同步盘一个包都没写',
        [f.name for f in syncdir.glob('snapshot_*')])
    chk(sorted(sm.deleted) == ['L1', 'L2'], '暂存成功后才删本地', sm.deleted)

    # 暂存区满 → 不再往下塞，快照留本地
    cfg.cloud_outbox_max = 1
    mk_pkg(outbox, 'snapshot_20261113_090000.zip.enc')
    sm2 = SM([Snap('L3', '20261113_110000')])
    h2 = Host(cfg, sm2)
    targets2 = []
    h2._archive_snapshot_to_cloud = lambda s, d, e: (targets2.append(s.snapshot_id), True)[1]
    h2._cloud_uploads_today = lambda: 0
    h2._handle_overflow_to_cloud()
    chk(targets2 == [] and sm2.deleted == [], 'outbox 满则留本地等窗口',
        (targets2, sm2.deleted))
    shutil.rmtree(root, ignore_errors=True)


def test_single_timer():
    print("F. 只剩一条最新快照定时器")
    src = (ROOT / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    assigns = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                if isinstance(t, ast.Attribute) and t.attr in (
                        '_cloud_timer', '_cloud_sync_timer', '_overflow_timer',
                        '_cloud_window_timer'):
                    assigns[t.attr] = assigns.get(t.attr, 0) + 1
    chk(assigns.get('_cloud_timer', 0) == 0, '_cloud_timer 已删除（原来是重复的第二条）',
        assigns)
    chk(assigns.get('_cloud_sync_timer', 0) == 1, '只保留一条 _cloud_sync_timer', assigns)
    chk(assigns.get('_cloud_window_timer', 0) == 1, '窗口看门狗已挂上', assigns)
    chk('def _cloud_window_tick' in src and 'def _maybe_close_baidu_client' in src,
        '窗口钩子函数存在')


def test_auto_window():
    """G. 机会模式：开机时间不固定时怎么决定开不开窗口"""
    print("G. 机会模式开窗判定")
    cfg = Cfg()
    cfg.cloud_window_trigger = 'auto'
    h = Host(cfg)
    now = 1_000_000_000.0        # 用一个正常量级的时间戳，别让 0 值兜底干扰判定
    h._cloud_app_started_ts = now - 10 * 60
    h._cloud_outbox_count = lambda: 3
    h._cloud_uploads_today = lambda: 0
    h._cloud_system_idle_seconds = lambda: 60          # 只空闲 1 分钟
    h._cloud_last_window_end_ts = now - 3600            # 1 小时前刚开过

    chk(not h._cloud_should_open_window(now), '刚开过 + 人还在用 → 不开')

    h._cloud_last_window_end_ts = now - 30 * 3600       # 30 小时前
    chk(not h._cloud_should_open_window(now), '隔 30 小时但人还在用 → 仍不开（未到硬传线）')

    h._cloud_system_idle_seconds = lambda: 6 * 60
    chk(h._cloud_should_open_window(now), '隔够 20 小时 + 空闲 6 分钟 → 开')

    h._cloud_system_idle_seconds = lambda: 0
    h._cloud_last_window_end_ts = now - 40 * 3600
    chk(h._cloud_should_open_window(now), '超过 36 小时没传 → 无视空闲硬开一次')

    h._cloud_last_window_end_ts = now - 10
    h._cloud_app_started_ts = now - 60                  # 启动才 1 分钟
    chk(not h._cloud_should_open_window(now), '启动延时没到 → 不开（别跟开机抢资源）')

    h._cloud_app_started_ts = now - 10 * 60
    h._cloud_outbox_count = lambda: 6                   # 暂存区压满
    h._cloud_uploads_today = lambda: 5
    h._cloud_last_window_end_ts = now - 2 * 3600
    chk(h._cloud_should_open_window(now), '暂存区压满 → 不用等空闲，补一次')

    h._cloud_outbox_count = lambda: 0
    h._cloud_uploads_today = lambda: 3
    h._cloud_last_window_end_ts = now - 30 * 3600
    h._cloud_system_idle_seconds = lambda: 10 * 60
    chk(not h._cloud_should_open_window(now), '今天已经传过、暂存区也空 → 不开')

    # auto 模式下"在不在窗口"由状态位决定，不再看日历
    h2 = Host(Cfg())
    h2.config.cloud_window_trigger = 'auto'
    h2._cloud_window_state = False
    chk(not h2._cloud_in_window(), 'auto：窗口没开 → 拦住写同步盘')
    h2._cloud_window_state = True
    chk(h2._cloud_in_window(), 'auto：窗口开着 → 放行')


def test_cadence():
    """H. 同步频率（每天 / 每 2 天 / 每 7 天）"""
    print("H. 同步频率")
    cfg = Cfg()
    cfg.cloud_window_trigger = 'auto'
    h = Host(cfg)

    chk(h._cloud_cadence_days() == 1, '默认每天一次')
    chk(h._cloud_window_gap_seconds() == 86400, '每天 = 24 小时间隔',
        h._cloud_window_gap_seconds())

    cfg.cloud_sync_cadence_days = 7
    chk(h._cloud_window_gap_seconds() == 7 * 86400, '每 7 天 = 168 小时间隔',
        h._cloud_window_gap_seconds())
    chk(h._cloud_outbox_limit() >= 14, '暂存区跟着放大到 7 天份',
        h._cloud_outbox_limit())
    chk(h._cloud_window_pkg_limit() >= 14, '一个窗口够搬完一周积压',
        h._cloud_window_pkg_limit())

    # 频率 7 天时，隔了 30 小时也不该开窗
    now = 1_000_000_000.0
    h._cloud_app_started_ts = now - 10 * 60
    h._cloud_outbox_count = lambda: 3
    h._cloud_uploads_today = lambda: 0
    h._cloud_system_idle_seconds = lambda: 10 * 60
    h._cloud_last_window_end_ts = now - 30 * 3600
    chk(not h._cloud_should_open_window(now), '频率 7 天时 30 小时不算到点')

    h._cloud_last_window_end_ts = now - 8 * 86400
    chk(h._cloud_should_open_window(now), '过了 8 天 → 开窗')

    # 暂存区按天去重：同一天同类只留最新（只在频率 > 1 天时生效）
    cfg.cloud_sync_cadence_days = 7
    root = Path(tempfile.mkdtemp(prefix='zf_cad_'))
    ob = root / 'ob'
    ob.mkdir()
    cfg.cloud_outbox_dir = str(ob)
    cfg.snapshot_dir = str(root)
    for name, is_full in (
            ('snapshot_20261110_090000.zip.enc', False),
            ('snapshot_20261110_120000.zip.enc', False),
            ('snapshot_20261110_100000.zip.enc', True),
            ('snapshot_20261110_130000.zip.enc', True),
            ('snapshot_20261109_090000.zip.enc', False)):
        mk_pkg(ob, name)
        base = name[:-4]
        (ob / (base + '.meta.json')).write_text(
            json.dumps({'is_full': is_full}), encoding='utf-8')

    removed = h._compact_outbox()
    chk(removed == 2, '同一天 2 轻 2 完整 → 删 2 个（实删 {}）'.format(removed))
    left = sorted(f.name for f in ob.glob('snapshot_*.zip.enc'))
    chk(left == ['snapshot_20261109_090000.zip.enc',
                 'snapshot_20261110_120000.zip.enc',
                 'snapshot_20261110_130000.zip.enc'],
        '留下每天最新的 1 轻 + 1 完整，跨天不互相顶掉', left)
    chk(h._compact_outbox() == 0, '重复去重幂等')
    shutil.rmtree(root, ignore_errors=True)


def test_budget_guard():
    """I. 额度用完时不许白拉起网盘客户端（2026-11-14 实测缺陷）

    现场日志：21:24「暂存区已压满，立即开窗搬走」→「已自动启动百度网盘客户端」
    →「今日上传已达上限，暂存区 7 个包留到明天」。额度早就用完了，窗口却先
    把客户端拉起来，然后一整天不干活 —— 用户看到的就是"还是会自动拉起网盘"。
    """
    print("I. 额度用完不拉起客户端")
    cfg = Cfg()
    cfg.cloud_window_trigger = 'auto'
    h = Host(cfg)
    calls = {'ensure': 0, 'flush': 0, 'sync': 0, 'close': 0}
    h._cloud_window_state = False
    h._cloud_window_started_ts = 0
    h._cloud_last_window_end_ts = 0
    h._cloud_client_launched_by_us = False
    h._cloud_close_attempts = 0
    h._save_window_state = lambda **kw: None
    h._cloud_has_backlog = lambda: True
    h._cloud_outbox_count = lambda: 7
    h._cloud_uploads_today = lambda: 24
    h._cloud_system_idle_seconds = lambda: 10 * 60
    h._ensure_baidu_client = lambda auto_start=True: (
        calls.__setitem__('ensure', calls['ensure'] + 1) or True)
    h._flush_outbox_to_cloud = lambda: (calls.__setitem__('flush', calls['flush'] + 1) or 0)
    h._consolidate_cloud_daily = lambda: None
    h._sync_latest_to_cloud = lambda: (calls.__setitem__('sync', calls['sync'] + 1) or 0)
    h._maybe_close_baidu_client = lambda: calls.__setitem__('close', calls['close'] + 1)

    chk(h._cloud_budget_left() == 0, '传满 24 个后额度为 0', h._cloud_budget_left())

    now = 1_000_000_000.0
    h._cloud_app_started_ts = now - 10 * 60
    h._cloud_last_window_end_ts = now - 40 * 3600      # 超过 force 时限，本该"硬开"
    chk(not h._cloud_should_open_window(now), '额度为 0 时即使超时不硬开窗')

    h._cloud_window_begin('机会满足')
    chk(calls['ensure'] == 0, '开窗流程没有拉起客户端', calls)
    chk(calls['flush'] == 0, '没搬暂存区', calls)
    chk(h._cloud_window_state is False, '窗口状态没被置成开')

    # 额度 = 已传 + 暂存（暂存的包迟早要占带宽，先扣着，免得一天冲过上限）
    h._cloud_uploads_today = lambda: 15
    chk(h._cloud_budget_left() == 2, '已传 15 + 暂存 7 → 还剩 2', h._cloud_budget_left())

    # 窗口已经开着、后来额度用完了 → 立刻收窗并关客户端，不干等到 60 分钟
    h._cloud_window_state = True
    h._cloud_window_started_ts = now - 5 * 60
    h._cloud_uploads_today = lambda: 24
    h._cloud_window_tick_auto()
    chk(h._cloud_window_state is False, '额度用完 → 立刻收窗')
    chk(calls['close'] == 1, '顺手关掉自己拉起的客户端', calls)


def main():
    test_window_bounds()
    test_mode_switch()
    test_budget()
    test_flush_outbox()
    test_overflow_to_outbox()
    test_single_timer()
    test_auto_window()
    test_cadence()
    test_budget_guard()
    print()
    print("结果：{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
