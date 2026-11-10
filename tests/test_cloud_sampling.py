# -*- coding: utf-8 -*-
"""云端上传抽样 + 历史保留期测试

背景：云端原来有两个问题
  1. 完整快照"未同步的全传" —— 本地 30 分钟一个，一天 16 个 × 300 MB ≈ 4.8 GB
  2. 收敛只处理"昨天"，更早的永远留着 —— 每天 0.45 GB，一年 160 GB

方案 B：
  · 轻量每 12 个传 1 个（≈每小时 1 个）
  · 完整每 4 个传 1 个（≈每 2 小时 1 个）
  · 云端历史保留 14 天，超期整组删除

抽样必须**确定性** —— 同一批快照每次选出同一批，否则程序重启就会
反复传不同的快照，云端反而更乱。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 统一以项目根为 cwd
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['ZF_DATA_DIR'] = tempfile.mkdtemp(prefix='zf_cloud_b_')

from PyQt6.QtWidgets import QApplication            # noqa: E402

app = QApplication.instance() or QApplication([])
from config import ZeroFallConfig                     # noqa: E402
import ui.main_window as MW                           # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def snap(created_at, is_full=False):
    return SimpleNamespace(
        created_at=created_at,
        is_full=is_full,
        snapshot_id='snapshot_' + created_at,
        size_mb=120.0,
        archive_path='C:/fake/snapshot_{}.zip'.format(created_at),
    )


def today_str():
    return datetime.now().strftime('%Y%m%d')


def times_today(n, step_min=5):
    """生成今天连续 n 个时间点（从 09:00 起，每 step_min 分钟）"""
    base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    return [(base + timedelta(minutes=step_min * i)).strftime('%Y%m%d_%H%M%S')
            for i in range(n)]


class H:
    def __init__(self, light_stride=12, full_stride=4, keep_days=14):
        c = ZeroFallConfig()
        c.cloud_sync_light_stride = light_stride
        c.cloud_sync_full_stride = full_stride
        c.cloud_history_keep_days = keep_days
        self.config = c
        self.cloud_sync_dir = ''

    _pick_sampled_snapshots = MW.ZeroFallMainWindow._pick_sampled_snapshots
    _prune_cloud_history = MW.ZeroFallMainWindow._prune_cloud_history
    # 注意：这个在原类里是 @staticmethod，直接赋值会变成普通函数，
    # 通过实例调用时会多绑一个 self 导致 TypeError（被清理函数的
    # except 吞掉，表现为"没删任何文件"）。必须包一层 staticmethod。
    _unlink_cloud_with_meta = staticmethod(
        MW.ZeroFallMainWindow._unlink_cloud_with_meta)


def test_light_sampling():
    print('\n[1] 轻量每 12 个取 1 个')
    h = H()
    snaps = [snap(t) for t in times_today(24)]        # 24 个 = 2 小时
    picked = h._pick_sampled_snapshots(snaps, 'cloud_sync_light_stride', 12)
    check('24 个里选出 2 个', len(picked) == 2, len(picked))
    check('选的是第 12 和第 24 个',
          picked[0].created_at == snaps[11].created_at
          and picked[1].created_at == snaps[23].created_at,
          [p.created_at[9:] for p in picked])


def test_full_sampling():
    print('\n[2] 完整每 4 个取 1 个')
    h = H()
    snaps = [snap(t, is_full=True) for t in times_today(16, step_min=30)]  # 16 个
    picked = h._pick_sampled_snapshots(snaps, 'cloud_sync_full_stride', 4)
    check('16 个里选出 4 个', len(picked) == 4, len(picked))


def test_deterministic():
    print('\n[3] 抽样是确定性的（同样输入两次结果一致）')
    h = H()
    snaps = [snap(t) for t in times_today(30)]
    a = [s.created_at for s in h._pick_sampled_snapshots(snaps, 'cloud_sync_light_stride', 12)]
    b = [s.created_at for s in h._pick_sampled_snapshots(snaps, 'cloud_sync_light_stride', 12)]
    check('两次结果相同', a == b, (a, b))
    # 打乱输入顺序也应一致（内部会排序）
    import random
    shuffled = snaps[:]
    random.shuffle(shuffled)
    c = [s.created_at for s in h._pick_sampled_snapshots(shuffled, 'cloud_sync_light_stride', 12)]
    check('打乱输入也选出同一批', a == c, (a, c))


def test_only_today():
    print('\n[4] 只处理当天快照，不往回补传历史')
    h = H()
    old = (datetime.now() - timedelta(days=3)).strftime('%Y%m%d_%H%M%S')
    snaps = [snap(old)] + [snap(t) for t in times_today(12)]
    picked = h._pick_sampled_snapshots(snaps, 'cloud_sync_light_stride', 12)
    check('历史快照不在抽样结果里',
          all(not p.created_at.startswith(old[:8]) for p in picked),
          [p.created_at for p in picked])
    check('当天的仍被选中', len(picked) >= 1, len(picked))


def test_stride_edge_cases():
    print('\n[5] 步长边界')
    h = H()
    one = [snap(times_today(1)[0])]
    check('只有 1 个也会选它',
          len(h._pick_sampled_snapshots(one, 'cloud_sync_light_stride', 12)) == 1)

    h0 = H(light_stride=0)
    many = [snap(t) for t in times_today(5)]
    picked = h0._pick_sampled_snapshots(many, 'cloud_sync_light_stride', 12)
    check('步长 0 按 1 处理（全选）', len(picked) == 5, len(picked))

    h1 = H(light_stride=1)
    picked1 = h1._pick_sampled_snapshots(many, 'cloud_sync_light_stride', 1)
    check('步长 1 = 全选', len(picked1) == 5, len(picked1))


def test_history_prune():
    print('\n[6] 云端历史保留期清理')
    root = Path(tempfile.mkdtemp(prefix='zf_cl_'))
    # 造文件：今天、昨天、20 天前、30 天前
    def mk(days_ago, kind='light'):
        d = (datetime.now() - timedelta(days=days_ago)).strftime('%Y%m%d')
        name = 'snapshot_{}_120000.zip.enc'.format(d)
        p = root / name
        p.write_bytes(b'x' * 100)
        meta = root / (name + '.meta.json')
        meta.write_text('{}', encoding='utf-8')
        return p

    f_today = mk(0)
    f_yest = mk(1)
    f_20 = mk(20)
    f_30 = mk(30)

    h = H(keep_days=14)
    h.config.cloud_sync_dir = str(root)
    removed = h._prune_cloud_history()

    check('删除了 2 个过期文件', removed == 2, removed)
    check('今天的还在', f_today.exists())
    check('昨天的还在', f_yest.exists())
    check('20 天前的已删', not f_20.exists())
    check('30 天前的已删', not f_30.exists())
    check('对应的 meta 也一起删了',
          not (root / (f_20.name + '.meta.json')).exists())


def test_history_disabled():
    print('\n[7] 保留期设 0 = 不清理')
    root = Path(tempfile.mkdtemp(prefix='zf_cl2_'))
    d = (datetime.now() - timedelta(days=99)).strftime('%Y%m%d')
    p = root / 'snapshot_{}_120000.zip.enc'.format(d)
    p.write_bytes(b'x')

    h = H(keep_days=0)
    h.config.cloud_sync_dir = str(root)
    removed = h._prune_cloud_history()
    check('没有删除', removed == 0, removed)
    check('老文件还在', p.exists())


def test_config_defaults():
    print('\n[8] 配置默认值')
    c = ZeroFallConfig()
    check('轻量步长 12', c.cloud_sync_light_stride == 12, c.cloud_sync_light_stride)
    check('完整步长 4', c.cloud_sync_full_stride == 4, c.cloud_sync_full_stride)
    check('历史保留 14 天', c.cloud_history_keep_days == 14, c.cloud_history_keep_days)


def test_prune_wired_after_consolidate():
    print('\n[9] 历史清理已接到次日收敛流程之后')
    import inspect
    src = inspect.getsource(MW.ZeroFallMainWindow._consolidate_cloud_daily)
    check('收敛函数里调用了 _prune_cloud_history',
          '_prune_cloud_history' in src, '未接上')


if __name__ == '__main__':
    print('=' * 66)
    print('云端抽样上传 + 历史保留期测试')
    print('=' * 66)
    test_light_sampling()
    test_full_sampling()
    test_deterministic()
    test_only_today()
    test_stride_edge_cases()
    test_history_prune()
    test_history_disabled()
    test_config_defaults()
    test_prune_wired_after_consolidate()
    print()
    print('=' * 66)
    print('通过 {} / {} 项'.format(len(PASS), len(PASS) + len(FAIL)))
    if FAIL:
        print('失败项:')
        for f in FAIL:
            print('  -', f)
    print('=' * 66)
    sys.exit(0 if not FAIL else 1)
