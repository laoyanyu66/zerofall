# -*- coding: utf-8 -*-
"""P0/P1/P2 优化项的回归测试

覆盖：恢复后路径改写、密钥占位符 + 本机 vault 回填、三方合并、
会话代际识别、健康诊断 10 项、故障时间线、升级预览（离线）、保留策略对账。
全部在临时目录里跑，不碰真实配置。
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ZF_DATA = Path(tempfile.mkdtemp(prefix='zf_p01_data_'))
os.environ['ZF_DATA_DIR'] = str(ZF_DATA)

from modules import config_bundle as cb            # noqa: E402
from modules import config_crypto as cc            # noqa: E402
from modules import diagnostics as diag            # noqa: E402
from modules import fault_timeline as ftl          # noqa: E402
from modules import path_rewrite as pr             # noqa: E402
from modules import profile_health as ph           # noqa: E402
from modules import secret_vault as sv             # noqa: E402
from modules import session_repair as sr           # noqa: E402
from modules import upgrade_preview as up          # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


class Cfg:
    """最小配置替身"""
    def __init__(self, **kw):
        self.deepseek_plugins_dir = kw.get('plugins', '')
        self.deepseek_config_dir = kw.get('config', '')
        self.deepseek_session_dir = kw.get('sessions', '')
        self.dsh_appdata_dir = kw.get('appdata', '')
        self.snapshot_dir = kw.get('snapshot_dir', '')
        self.max_light_snapshots = kw.get('max_light', 48)
        self.max_full_snapshots = kw.get('max_full', 12)
        self.silent_snapshot_interval_minutes = 10
        self.full_snapshot_interval_minutes = 30


print("== 1. 恢复后路径改写 ==")
snap = Path(tempfile.mkdtemp(prefix='zf_snap_'))
new_home = Path(tempfile.mkdtemp(prefix='zf_newhome_'))
old_home = r"C:\Users\OldUser\.dsh"
cfg = Cfg(plugins=str(new_home), config=str(new_home / "runtime"))
pr.write_meta(snap, Cfg(plugins=old_home, config=r"D:\DeepSeek\runtime",
                        sessions=old_home + r"\sessions"))
(target := new_home / "storages").mkdir(parents=True)
(target / "workspace.json").write_text(json.dumps({
    "ws": [{"path": old_home + r"\workspace\proj-a"},
           {"path": r"D:\DeepSeek\runtime\cache"}]
}, ensure_ascii=False), encoding="utf-8")
(nh_res := pr.rewrite_after_restore(snap, cfg))
check("读到旧前缀并改写", nh_res["replacements"] >= 2, nh_res)
txt = (target / "workspace.json").read_text(encoding="utf-8")
check("旧 DSH_HOME 已被换掉", old_home not in txt, txt[:120])
escaped = str(new_home).replace("\\", "\\\\")
check("新 DSH_HOME 已写入", (str(new_home) in txt) or (escaped in txt), txt[:120])
check("留了 .bak-zf-rewrite", any(p.name.startswith("workspace.json.bak-zf-rewrite")
                                  for p in target.iterdir()),
      [p.name for p in target.iterdir()])
check("没有 meta 时安全跳过",
      pr.rewrite_after_restore(Path(tempfile.mkdtemp()), cfg).get("skipped") is not None)

print("== 2. 密钥占位符 + vault 回填 ==")
home = Path(tempfile.mkdtemp(prefix='zf_ph_home_'))
(home / "profiles" / "desktop").mkdir(parents=True)
(home / "profiles" / "desktop" / "cordis.patch.yml").write_text(
    "- id: llm\n  config:\n    apiKey: sk-live-abcdef\n    baseURL: https://api\n",
    encoding="utf-8")
(home / "prompt-inject.md").write_text("# 注入\n", encoding="utf-8")
out = Path(tempfile.mkdtemp(prefix='zf_ph_out_')) / "b.zip"
res = cb.export_bundle(home, out, profile="desktop")           # 默认占位符模式
check("manifest 标注占位符", res["manifest"]["placeholder_secrets"] is True)
check("manifest 记了槽位", len(res["manifest"]["secret_slots"]) >= 1,
      res["manifest"]["secret_slots"])
with zipfile.ZipFile(out) as zf:
    body = zf.read([n for n in zf.namelist() if "cordis.patch.yml" in n][0]).decode("utf-8")
check("包里是占位符不是明文", "__ZF_SECRET__:" in body and "sk-live-abcdef" not in body, body[:160])
slots = sv.slots()
check("槽位已进本机 vault", any("apikey" in s for s in slots), slots)
# 新机器：vault 里有（同机）→ 自动回填
home2 = Path(tempfile.mkdtemp(prefix='zf_ph_home2_'))
(home2 / "profiles" / "desktop").mkdir(parents=True)
(home2 / "prompt-inject.md").write_text("# 注入\n", encoding="utf-8")
r2 = cb.apply_import(out, home2, policy="overwrite")
check("导入时回填了密钥", r2["secrets_filled"] >= 1, r2)
back = (home2 / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
check("回填后是原值", "sk-live-abcdef" in back, back[:160])
# 换机器（清空 vault）→ 保留占位符并计数
saved = sv.load()
sv.save({})
home3 = Path(tempfile.mkdtemp(prefix='zf_ph_home3_'))
(home3 / "profiles" / "desktop").mkdir(parents=True)
(home3 / "prompt-inject.md").write_text("# 注入\n", encoding="utf-8")
r3 = cb.apply_import(out, home3, policy="overwrite")
check("无 vault 时计为待补录", r3["secrets_missing"] >= 1, r3)
body3 = (home3 / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
check("无 vault 时保留占位符", "__ZF_SECRET__:" in body3, body3[:160])
sv.save(saved)

print("== 3. 三方合并 merge3 ==")
h = Path(tempfile.mkdtemp(prefix='zf_m3_home_'))
(h / "profiles" / "desktop").mkdir(parents=True)
f = h / "prompt-inject.md"
f.write_text("A\nB\nC\n", encoding="utf-8")
(h / "profiles" / "desktop" / "cordis.patch.yml").write_text("- id: x\n  config: {}\n", encoding="utf-8")
pt = cb.save_point(h, label="base", profile="desktop")
# 本地改一行 → 与 base 不同
f.write_text("A\nB-local\nC\n", encoding="utf-8")
# 包里是另一个版本（B-incoming）
bundle = Path(tempfile.mkdtemp(prefix='zf_m3_out_')) / "b.zip"
tmp_home = Path(tempfile.mkdtemp(prefix='zf_m3_tmp_'))
(tmp_home / "profiles" / "desktop").mkdir(parents=True)
(tmp_home / "prompt-inject.md").write_text("A\nB-incoming\nC\n", encoding="utf-8")
(tmp_home / "profiles" / "desktop" / "cordis.patch.yml").write_text("- id: x\n  config: {}\n", encoding="utf-8")
cb.export_bundle(tmp_home, bundle, profile="desktop")
r = cb.apply_import(bundle, h, policy="merge3", only=["prompt-inject.md"])
check("两边都改 → 冲突另存", any(c.endswith(".zf-conflict") for c in r["conflicts"]), r["conflicts"])
check("冲突时本地未被覆盖", "B-local" in f.read_text(encoding="utf-8"), f.read_text(encoding="utf-8"))
# 本地=base 的情况：应该采用包内版本
f.write_text("A\nB\nC\n", encoding="utf-8")
r2 = cb.apply_import(bundle, h, policy="merge3", only=["prompt-inject.md"])
check("本地未动 → 采用包内版本", "B-incoming" in f.read_text(encoding="utf-8"),
      f.read_text(encoding="utf-8"))

print("== 4. 会话代际识别 ==")
node = sr.find_node()
sdir = Path(tempfile.mkdtemp(prefix='zf_gen_')) / "proj" / "session-old"
sdir.mkdir(parents=True)
v3 = sdir / "session.v3.jsonl.zstd"
v3.write_bytes(b"\x28\xb5\x2f\xfd" + b"\x00" * 8)
st = sr.scan_one(v3, node)
check("非 v4 被标 unsupported_generation", st.get("generation") == 3 and
      st["state"] in ("unsupported_generation",), (st.get("generation"), st["state"]))
res = sr.repair_one(v3, node, apply=True)
check("非 v4 拒修", res["action"] in ("refuse", "none"), res["action"])

print("== 5. 健康诊断 10 项 ==")
hh = Path(tempfile.mkdtemp(prefix='zf_h10_'))
pdir = hh / "profiles" / "desktop"
pdir.mkdir(parents=True)
(pdir / "package.json").write_text(json.dumps({
    "dependencies": {"dsh-x": "file:./vendor/dsh-x"},
    "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "dsh-x"]}},
}), encoding="utf-8")
(pdir / "cordis.patch.yml").write_text("- id: a\n  config: {}\n- id: b\n- id: c\n", encoding="utf-8")
(pdir / "node_modules" / "dsh-x").mkdir(parents=True)
(pdir / "storages").mkdir()
(pdir / "storages" / "workspace.json").write_text(json.dumps({
    "ws": [{"path": r"Z:\definitely\missing\dir"}]}), encoding="utf-8")
rep = ph.run_all(hh, "desktop")
check("十项都在", len(rep["checks"]) == 10, len(rep["checks"]))
ids = [c["id"] for c in rep["checks"]]
check("新增 workspace/storages/credentials",
      all(k in ids for k in ("workspace", "storages", "credentials")), ids)
check("失效路径被发现", any(c["id"] == "workspace" and c["level"] == "warn"
                         for c in rep["checks"]), rep["summary"])

print("== 6. 故障时间线 ==")
ftl.timeline_path().unlink(missing_ok=True)
ftl.mark_boot_ok("abc123", "test")
ftl.record_change("装了插件 A")
ftl.record_crash("启动失败")
res = ftl.since_last_ok()
check("基线存在", res["baseline"] and res["baseline"]["fingerprint"] == "abc123", res["baseline"])
check("自基线以来 2 条", res["count"] == 2, res["count"])
check("有格式化输出", any("crash" in l for l in ftl.format_lines(res)))
check("指纹稳定", ftl.fingerprint([str(ZF_DATA)]) == ftl.fingerprint([str(ZF_DATA)]))

print("== 7. 升级预览（离线模式）==")
pf = Path(tempfile.mkdtemp(prefix='zf_up_'))
(pf / "profiles" / "desktop").mkdir(parents=True)
(pf / "profiles" / "desktop" / "package.json").write_text(json.dumps({
    "dependencies": {"dsh-local": "file:./vendor/dsh-local", "@deepseek-ai/dsh-base": "^0.0.1"},
    "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "dsh-local"]}},
}), encoding="utf-8")
res = up.preview(pf, "desktop", config=None, offline=True)
check("列出两条", len(res["rows"]) == 2, len(res["rows"]))
check("本地源被识别", any(r["state"] == "local-source" for r in res["rows"]), res["rows"])
check("内建被识别", any(r["state"] == "builtin" for r in res["rows"]), res["rows"])
check("有格式化输出", len(up.format_lines(res)) >= 3)

print("== 8. 保留策略对账 ==")
sd = Path(tempfile.mkdtemp(prefix='zf_ret_'))
for i in range(3):
    (sd / "snapshot_a{}.zip".format(i)).write_bytes(b"x" * 1000)
(sd / "snapshot_b_full.zip").write_bytes(b"x" * 50000)
rec = diag.retention_reconcile(Cfg(snapshot_dir=str(sd)), sd)
check("统计到 4 份", rec["disk"]["total"] == 4, rec)
check("分出完整/轻量", rec["disk"]["full"] >= 1 and rec["disk"]["light"] >= 1, rec["disk"])
check("给出稳态预估", rec["steady_state_gb"] >= 0, rec["steady_state_gb"])

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
