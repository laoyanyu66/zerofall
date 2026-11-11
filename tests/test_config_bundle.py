# -*- coding: utf-8 -*-
"""配置迁移包 + 版本点 的回归测试（全在临时目录，不碰真实 $DSH_HOME）

对齐 GitHub 两个 dsh-config-manager 的核心回路：
  Export → 去密 → manifest → sha256 → ZIP
  Import → 预览（new/same/differs）→ 冲突策略 → 路径映射 → 写前留备份
  Version point → 列表 / 与当前 diff / 整体回滚
"""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_bundle_'))

from modules import config_bundle as cb               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_home(root: Path, with_secrets=True) -> Path:
    (root / "profiles" / "desktop").mkdir(parents=True, exist_ok=True)
    (root / ".agent-presets" / "unrestricted").mkdir(parents=True, exist_ok=True)
    patch = "- id: llm-pi-ai\n  config:\n    providers:\n      opencode-go:\n        apiKey: sk-super-secret-123\n        baseURL: https://x\n"
    if not with_secrets:
        patch = "- id: llm-pi-ai\n  config:\n    providers:\n      opencode-go:\n        baseURL: https://x\n"
    (root / "profiles" / "desktop" / "cordis.patch.yml").write_text(patch, encoding="utf-8")
    (root / "prompt-inject.md").write_text("# 注入\n规则一\n", encoding="utf-8")
    (root / ".agent-presets" / "unrestricted" / "agent.cordis.yml").write_text(
        "# 预设\n- id: persona\n", encoding="utf-8")
    return root


print("== 1. 受管文件收集 ==")
home = make_home(Path(tempfile.mkdtemp(prefix='zf_home_')))
paths = cb.managed_paths(home, "desktop")
rels = sorted(p.relative_to(home).as_posix() for p in paths)
check("收集到三份受管文件", len(paths) == 3, rels)
check("包含注入稿与预设", any("prompt-inject" in r for r in rels) and any("agent-presets" in r for r in rels), rels)

print("== 2. 导出 + 去密 ==")
out = Path(tempfile.mkdtemp(prefix='zf_out_')) / "bundle.zip"
res = cb.export_bundle(home, out, profile="desktop", placeholder_secrets=False)
check("包已生成", out.is_file() and res["entries"] == 3, res["entries"])
check("默认剥掉密钥值", res["redacted_values"] >= 1, res["redacted_values"])
with zipfile.ZipFile(out) as zf:
    names = zf.namelist()
    manifest = json.loads(zf.read(cb.CONFIG_MANIFEST).decode("utf-8"))
    patch = zf.read("files/profiles/desktop/cordis.patch.yml").decode("utf-8")
check("含 manifest 与 files/", cb.CONFIG_MANIFEST in names and any(n.startswith("files/") for n in names))
check("包里看不到明文密钥", "sk-super-secret-123" not in patch, patch[:80])
check("manifest 记了 sha256", all(len(e["sha256"]) == 64 for e in manifest["entries"]))
check("manifest 声明已去密", manifest["secrets_included"] is False)

print("== 3. 显式包含密钥 ==")
out2 = Path(tempfile.mkdtemp(prefix='zf_out2_')) / "bundle_raw.zip"
cb.export_bundle(home, out2, profile="desktop", include_secrets=True)
with zipfile.ZipFile(out2) as zf:
    raw = zf.read("files/profiles/desktop/cordis.patch.yml").decode("utf-8")
check("include_secrets=True 时保留原值", "sk-super-secret-123" in raw)

print("== 4. inspect / 坏包 ==")
m = cb.inspect_bundle(out)
check("inspect 读到 3 条", len(m["entries"]) == 3, len(m["entries"]))
bad = Path(tempfile.mkdtemp(prefix='zf_bad_')) / "bad.zip"
with zipfile.ZipFile(bad, "w") as zf:
    zf.writestr("readme.txt", "not a bundle")
try:
    cb.inspect_bundle(bad)
    check("坏包被拒", False, "竟然通过了")
except ValueError:
    check("坏包被拒", True)

print("== 5. 预览：new / same / differs ==")
home2 = make_home(Path(tempfile.mkdtemp(prefix='zf_home2_')))
rows = cb.preview_import(out, home2)
states = {r["rel"]: r["state"] for r in rows}
check("只有含密钥那份是 differs（其余内容一致）",
      states.get("profiles/desktop/cordis.patch.yml") == "differs"
      and states.get("prompt-inject.md") == "same", states)
# 让 prompt-inject 与包内一致
with zipfile.ZipFile(out) as zf:
    same_text = zf.read("files/prompt-inject.md").decode("utf-8")
(home2 / "prompt-inject.md").write_text(same_text, encoding="utf-8")
rows = cb.preview_import(out, home2)
states = {r["rel"]: r["state"] for r in rows}
check("相同内容识别为 same", states["prompt-inject.md"] == "same", states)
check("不同内容附带 diff", any(r["diff"] for r in rows if r["state"] == "differs"))

# 新目录 → 全 new
home3 = Path(tempfile.mkdtemp(prefix='zf_home3_'))
rows3 = cb.preview_import(out, home3)
check("空目录预览全为 new", {r["state"] for r in rows3} == {"new"}, [r["state"] for r in rows3])

print("== 6. 导入：overwrite / skip / keep-both + 备份 ==")
res_ov = cb.apply_import(out, home2, policy="overwrite")
check("overwrite 只写 differs 的那份（same 跳过）", len(res_ov["written"]) == 1, res_ov)
check("写了备份", len(res_ov["backups"]) >= 1, len(res_ov["backups"]))
check("导入后磁盘值=包内值",
      (home2 / "profiles/desktop/cordis.patch.yml").read_text(encoding="utf-8")
      == zipfile.ZipFile(out).read("files/profiles/desktop/cordis.patch.yml").decode("utf-8"))

home4 = make_home(Path(tempfile.mkdtemp(prefix='zf_home4_')))
res_skip = cb.apply_import(out, home4, policy="skip")
check("skip 不覆盖已存在文件", len(res_skip["written"]) == 0 and len(res_skip["skipped"]) == 3, res_skip)

home5 = make_home(Path(tempfile.mkdtemp(prefix='zf_home5_')))
res_kb = cb.apply_import(out, home5, policy="keep-both")
check("keep-both 只把冲突那份另存", len(res_kb["kept_both"]) == 1, res_kb["kept_both"])
check("原文件没被动", "sk-super-secret-123" in (home5 / "profiles/desktop/cordis.patch.yml").read_text(encoding="utf-8"))

print("== 7. 路径映射 ==")
rows_map = cb.preview_import(out, home2, path_map=("profiles", "profiles/desktop"))
mapped = [r["rel"] for r in rows_map]
check("映射后路径变了", any(r.startswith("profiles/desktop/desktop/") for r in mapped) or mapped != [r["rel"] for r in cb.preview_import(out, home2)], mapped[:3])

print("== 8. 版本点 ==")
p1 = cb.save_point(home, label="before-change")
check("版本点已保存", (Path(home) / cb.HISTORY_DIRNAME / p1["id"] / "meta.json").is_file())
check("列表能读到", any(x["id"] == p1["id"] for x in cb.list_points(home)))
(home / "prompt-inject.md").write_text("# 注入\n规则一\n规则二（新）\n", encoding="utf-8")
diff = cb.diff_point(home, p1["id"])
changed = {d["path"]: d["state"] for d in diff}
check("diff 认出被改的文件", changed.get("prompt-inject.md") == "modified", changed)
check("diff 文本包含新增行", any("规则二" in d["diff"] for d in diff if d["path"] == "prompt-inject.md"))
restored = cb.restore_point(home, p1["id"])
check("回滚写回原内容", "规则二" not in (home / "prompt-inject.md").read_text(encoding="utf-8"))
check("回滚前留了备份", len(restored["backups"]) >= 1, restored["backups"])
final_states = {d["path"]: d["state"] for d in cb.diff_point(home, p1["id"])}
check("回滚后 diff 变 same", final_states.get("prompt-inject.md") == "same", final_states)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
