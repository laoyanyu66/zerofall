# -*- coding: utf-8 -*-
"""云端容器 / 体量上限 / 残渣清理 / 升级缓存 的回归测试"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ZF_DATA = Path(tempfile.mkdtemp(prefix='zf_cloud_data_'))
os.environ['ZF_DATA_DIR'] = str(ZF_DATA)

from modules import cloud_crypto as cc        # noqa: E402
from modules import housekeeping as hk        # noqa: E402
from modules import upgrade_preview as up     # noqa: E402
from config import ZeroFallConfig             # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


print("== 1. ZFCH1 二进制容器 ==")
tmp = Path(tempfile.mkdtemp(prefix='zf_cc_'))
src = tmp / "snap.zip"
payload = os.urandom(9 * 1024 * 1024)      # 9 MB → 跨 3 块
src.write_bytes(payload)
enc = tmp / "snap.zip.enc"
plain = tmp / "out.zip"
ok = cc.encrypt_snapshot_v2(src, enc, config=None)
check("加密成功", ok is True)
check("容器带 magic", cc.is_v2(enc) is True)
back = cc.decrypt_file_v2(enc, plain, cc.get_cloud_password(None))
check("解密成功", back is True)
check("内容字节级一致", plain.read_bytes() == payload)
ratio = enc.stat().st_size / src.stat().st_size
check("开销 < 1.1（老 Fernet 是 1.33）", ratio < 1.1, round(ratio, 4))
bad = tmp / "bad.zip"
check("错密码解不开", cc.decrypt_file_v2(enc, bad, "not-the-password") is False)
ok2, src_kind = cc.decrypt_snapshot(enc, tmp / "via-wrapper.zip", config=None)
check("decrypt_snapshot 走 v2 分支", ok2 is True and src_kind == "current", (ok2, src_kind))

print("== 2. 老 Fernet 文件仍可解 ==")
from utils import encrypt_file
legacy = tmp / "legacy.zip"
legacy.write_bytes(b"hello-legacy-payload" * 1000)
enc_legacy = tmp / "legacy.zip.enc"
encrypt_file(str(legacy), str(enc_legacy), cc.get_cloud_password(None))
check("老文件不是 v2 格式", cc.is_v2(enc_legacy) is False)
ok3, kind3 = cc.decrypt_snapshot(enc_legacy, tmp / "legacy-out.zip", config=None)
check("老文件仍能解密", ok3 is True, (ok3, kind3))
check("老文件内容正确",
      (tmp / "legacy-out.zip").read_bytes() == legacy.read_bytes())

print("== 3. 云端体量上限配置 ==")
cfg = ZeroFallConfig()
check("有 cloud_max_total_gb 字段", hasattr(cfg, "cloud_max_total_gb"), dir(cfg))
check("默认 8 GB", float(cfg.cloud_max_total_gb) == 8.0, cfg.cloud_max_total_gb)
src_main = (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")
check("有按大小清理的方法", "def _prune_cloud_by_size" in src_main)
check("日期清理后调用体量清理", "_prune_cloud_by_size(sync_dir)" in src_main)

print("== 4. 残渣清理 ==")
home = Path(tempfile.mkdtemp(prefix='zf_hk_'))
(home / "profiles" / "desktop").mkdir(parents=True)
(home / "profiles" / "desktop" / "cordis.patch.yml.bak-before-x").write_text("a", encoding="utf-8")
(home / "prompt-inject.md.zf-tmp").write_text("b", encoding="utf-8")
(home / "notes.md.zf-conflict").write_text("c", encoding="utf-8")
nm = home / "node_modules" / "pkg"
nm.mkdir(parents=True)
(nm / "x.js.bak-20260101").write_text("d", encoding="utf-8")     # 应被跳过
scan = hk.scan(home)
check("扫到 3 个（跳过 node_modules）", scan["count"] == 3, scan["count"])
check("分类统计", set(scan["by_kind"]) >= {"普通备份", "临时文件", "冲突副本"}, scan["by_kind"])
dry = hk.delete(home, dry_run=True)
check("dry-run 不删", dry["candidates"] == 3 and dry["removed"] == 0, dry)
check("dry-run 后文件还在",
      (home / "notes.md.zf-conflict").is_file())
real = hk.delete(home, dry_run=False)
check("真删 3 个", real["removed"] == 3, real)
check("node_modules 里的没动", (nm / "x.js.bak-20260101").is_file())

print("== 4b. 云端格式迁移 ==")
from modules import cloud_migrate as cmig      # noqa: E402
from utils import encrypt_file as _ef          # noqa: E402
mdir = Path(tempfile.mkdtemp(prefix='zf_mig_'))
_pw = cc.get_cloud_password(None)
for i in range(2):
    srcp = mdir / f"s{i}.zip"
    srcp.write_bytes(os.urandom(1_500_000))
    _ef(str(srcp), str(mdir / f"snapshot_20261101_00000{i}.zip.enc"), _pw)
cc.encrypt_file_v2(str(srcp), str(mdir / "snapshot_20261102_000009.zip.enc"), _pw)
dry = cmig.migrate_dir(mdir, None, dry_run=True)
check("试算识别 2 份老格式", dry["would"] == 2, dry["would"])
check("试算不动文件", (mdir / "snapshot_20261101_000000.zip.enc").read_bytes()[:6] != cc.V2_MAGIC)
real = cmig.migrate_dir(mdir, None, dry_run=False)
check("真迁移 2 份", real["migrated"] == 2, real["migrated"])
check("已新格式的被跳过", real["already"] == 1, real["already"])
check("迁移后有节省", real["saved"] > 0, real["saved"])
converted = (mdir / "snapshot_20261101_000000.zip.enc")
check("转换后是新容器", cc.is_v2(converted))
outp = mdir / "check.out"
check("转换后仍可解", cc.decrypt_file_v2(str(converted), str(outp), _pw) is True)
check("没有临时文件残留", not any('.zf-' in p.name for p in mdir.iterdir()),
      [p.name for p in mdir.iterdir()])

print("== 5. 升级预览落盘缓存 ==")
up._disk_put("demo-pkg", {"latest": "1.2.3"})
cached = up._disk_cache()
check("缓存写入可读回", cached.get("demo-pkg", {}).get("value", {}).get("latest") == "1.2.3", cached)
check("缓存文件落在 ZF_DATA_DIR", up._cache_path().parent == ZF_DATA, up._cache_path())

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
