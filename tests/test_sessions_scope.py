# -*- coding: utf-8 -*-
"""会话只在完整快照里带（config.sessions_only_in_full）的回归测试

为什么要有这个开关：轻量快照每 10 分钟一份，把 30 MB 会话塞进去会让
每份从 24 MB 涨到 54 MB，48 份就是 1.3 GB；而 10 分钟粒度的恢复点
并不需要会话（会话是"基线"级别数据，30 分钟的完整快照带上就够）。
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_sessopt_'))

from config import ZeroFallConfig              # noqa: E402
from modules.snapshot_manager import SnapshotManager   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_env(tag: str, only_full: bool = True):
    root = Path(tempfile.mkdtemp(prefix='zf_env_' + tag + '_'))
    dsh = root / ".dsh"
    (dsh / "profiles" / "desktop").mkdir(parents=True)
    (dsh / "profiles" / "desktop" / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (dsh / "prompt-inject.md").write_text("# x\n", encoding="utf-8")
    sessions = dsh / "sessions" / "proj" / "session-1"
    sessions.mkdir(parents=True)
    (sessions / "session.v4.jsonl.zstd").write_bytes(b"\x28\xb5\x2f\xfd" + b"s" * 4096)
    cfg = ZeroFallConfig()
    cfg.deepseek_plugins_dir = str(dsh)
    cfg.deepseek_config_dir = str(root / "runtime")
    (root / "runtime").mkdir(exist_ok=True)
    cfg.deepseek_session_dir = str(dsh / "sessions")
    cfg.backup_session_data = True
    cfg.sessions_only_in_full = only_full
    cfg.snapshot_dir = str(root / "snaps")
    cfg.dsh_appdata_dir = ""
    return cfg


print("== 1. 开关语义（单元）==")
cfg = make_env("a")
sm = SnapshotManager(cfg)
check("轻量不带会话", sm._should_include_sessions(False) is False)
check("完整带会话", sm._should_include_sessions(True) is True)
cfg.sessions_only_in_full = False
check("关掉开关后轻量也带", sm._should_include_sessions(False) is True)
cfg.backup_session_data = False
check("总开关关掉后都不带", sm._should_include_sessions(True) is False)

print("== 2. 端到端：轻量快照不含 sessions/ ==")
cfg = make_env("b")
sm = SnapshotManager(cfg)
light = sm.create_snapshot("轻量验证", force_healthy=False, include_runtime=False)
check("轻量快照已生成", light is not None)
with zipfile.ZipFile(light.archive_path) as z:
    names = z.namelist()
check("轻量快照里有 plugins 内容", any(n.startswith("plugins/") for n in names), names[:3])
check("轻量快照里没有 sessions/", not any("sessions/" in n for n in names),
      [n for n in names if "sessions/" in n][:3])
check("has_session_data=False", light.has_session_data is False, light.has_session_data)

print("== 3. 端到端：完整快照含 sessions/ ==")
full = sm.create_snapshot("完整验证", force_healthy=False, include_runtime=True)
check("完整快照已生成", full is not None)
with zipfile.ZipFile(full.archive_path) as z:
    names_full = z.namelist()
check("完整快照里有 sessions/", any("sessions/" in n for n in names_full),
      [n for n in names_full if "sessions/" in n][:3])
check("has_session_data=True", full.has_session_data is True, full.has_session_data)

print("== 4. 轻量比完整小得多（会话没被塞进轻量）==")
check("轻量体积 < 完整体积", light.size_mb < full.size_mb,
      (light.size_mb, full.size_mb))
print("    轻量 {:.1f} MB / 完整 {:.1f} MB".format(light.size_mb, full.size_mb))

print("== 5. 老行为仍可用（sessions_only_in_full=False）==")
cfg2 = make_env("c", only_full=False)
sm2 = SnapshotManager(cfg2)
light2 = sm2.create_snapshot("轻量带会话", force_healthy=False, include_runtime=False)
with zipfile.ZipFile(light2.archive_path) as z:
    names2 = z.namelist()
check("关掉开关后轻量也含 sessions/", any("sessions/" in n for n in names2),
      [n for n in names2 if "sessions/" in n][:2])

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
