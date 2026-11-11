# -*- coding: utf-8 -*-
"""加密迁移包 + 版本点 tag / 两点互 diff 的回归测试

对应 GitHub 上 xiajiajun516/dsh-config-manager 的加密备份语义，以及
imxzt/dsh-config-manager 的 git 版本管理语义（tag、任意两点互 diff）。
"""
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from modules import config_bundle as cb               # noqa: E402
from modules import config_crypto as cc               # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def make_home(root: Path) -> Path:
    (root / "profiles" / "desktop").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "desktop" / "cordis.patch.yml").write_text(
        "- id: llm-pi-ai\n  config:\n    apiKey: sk-secret-123\n    baseURL: https://x\n",
        encoding="utf-8")
    (root / "prompt-inject.md").write_text("# 注入\n规则一\n", encoding="utf-8")
    return root


print("== 1. 加密容器本身 ==")
blob = cc.encrypt_bytes(b"hello-zf", "pw-123")
check("产物带 magic", blob.startswith(cc.MAGIC))
check("明文不在容器里", b"hello-zf" not in blob)
check("正确密码能解回", cc.decrypt_bytes(blob, "pw-123") == b"hello-zf")
try:
    cc.decrypt_bytes(blob, "wrong")
    check("错密码被拒", False, "竟然解开了")
except cc.CryptoError:
    check("错密码被拒", True)
tampered = bytearray(blob)
tampered[-1] ^= 0xFF
try:
    cc.decrypt_bytes(bytes(tampered), "pw-123")
    check("篡改被 GCM 拦下", False, "竟然解开了")
except cc.CryptoError:
    check("篡改被 GCM 拦下", True)

print("== 2. 加密导出 ==")
home = make_home(Path(tempfile.mkdtemp(prefix='zf_enc_home_')))
outdir = Path(tempfile.mkdtemp(prefix='zf_enc_out_'))
plain = outdir / "plain.zip"
enc = outdir / "enc.zfce"
r_plain = cb.export_bundle(home, plain, profile="desktop", placeholder_secrets=False)
r_enc = cb.export_bundle(home, enc, profile="desktop", password="layer8-move", placeholder_secrets=False)
check("明文包仍是 ZIP", plain.read_bytes()[:2] == b"PK")
check("加密包带 magic", cc.is_encrypted(enc))
check("加密包里看不到明文密钥", b"sk-secret-123" not in enc.read_bytes())
check("加密包大小 > 明文包（有开销）", enc.stat().st_size > plain.stat().st_size)
check("manifest 标记 encrypted", r_enc["manifest"].get("encrypted") is True, r_enc["manifest"].get("encrypted"))

print("== 3. 加密包的检查/预览/导入 ==")
try:
    cb.inspect_bundle(enc)
    check("不给密码读取被拒", False, "竟然读到了")
except ValueError as e:
    check("不给密码读取被拒", "密码" in str(e), str(e))
m = cb.inspect_bundle(enc, "layer8-move")
check("给对密码能读 manifest", len(m["entries"]) == 2, len(m["entries"]))
try:
    cb.inspect_bundle(enc, "bad-pw")
    check("错密码读取被拒", False)
except ValueError:
    check("错密码读取被拒", True)

home2 = make_home(Path(tempfile.mkdtemp(prefix='zf_enc_home2_')))
(home2 / "prompt-inject.md").write_text("# 注入\n规则一\n规则二\n", encoding="utf-8")
rows = cb.preview_import(enc, home2, password="layer8-move")
states = {r["rel"]: r["state"] for r in rows}
check("加密包也能预览 diff", states.get("prompt-inject.md") == "differs", states)
res = cb.apply_import(enc, home2, policy="overwrite", password="layer8-move")
check("加密包能导入", len(res["written"]) == 2, res)
check("导入后注入稿回到包内版本",
      (home2 / "prompt-inject.md").read_text(encoding="utf-8") == "# 注入\n规则一\n")
check("导入后密钥是被剥过的掩码",
      "sk-secret-123" not in (home2 / "profiles/desktop/cordis.patch.yml").read_text(encoding="utf-8"))

print("== 4. 版本点 tag ==")
h = make_home(Path(tempfile.mkdtemp(prefix='zf_tag_home_')))
p1 = cb.save_point(h, label="first", profile="desktop")
cb.tag_point(h, p1["id"], "known-good")
check("tag 写进了索引", any(p.get("tag") == "known-good" for p in cb.list_points(h)),
      [p.get("tag") for p in cb.list_points(h)])
check("按 tag 能解析出 id", cb.resolve_ref(h, "known-good") == p1["id"])
check("按 id 前缀也能解析", cb.resolve_ref(h, p1["id"][:15]) == p1["id"])
try:
    cb.resolve_ref(h, "no-such-thing")
    check("未知引用报错", False)
except ValueError:
    check("未知引用报错", True)

(h / "prompt-inject.md").write_text("# 注入\n规则一\n规则二\n", encoding="utf-8")
p2 = cb.save_point(h, label="second", profile="desktop")
check("第二个点独立存在", p2["id"] != p1["id"])

print("== 5. 两点互 diff ==")
d = cb.diff_points(h, "known-good", p2["id"])
check("返回 a/b id", d["a"] == p1["id"] and d["b"] == p2["id"], (d["a"], d["b"]))
check("识别出 1 处改动", d["summary"]["modified"] == 1, d["summary"])
row = next(r for r in d["rows"] if r["path"] == "prompt-inject.md")
check("diff 里有新增行", "规则二" in row["diff"], row["diff"][:80])
d_same = cb.diff_points(h, "known-good", "known-good")
check("同一点互 diff 全 same", d_same["summary"]["same"] == 2 and d_same["changed"] == 0, d_same["summary"])

print("== 6. 打 tag 的点不被索引截断挤掉 ==")
hh = make_home(Path(tempfile.mkdtemp(prefix='zf_trunc_home_')))
first = cb.save_point(hh, label="milestone", profile="desktop")
cb.tag_point(hh, first["id"], "keep-me")
for i in range(205):
    cb.save_point(hh, label="noise{}".format(i), profile="desktop")
pts = cb.list_points(hh)
check("索引长度受控（≤200）", len(pts) <= 200, len(pts))
check("带 tag 的点仍在", any(p.get("id") == first["id"] for p in pts),
      [p.get("id") for p in pts][:3])
check("tag 仍在", cb.resolve_ref(hh, "keep-me") == first["id"])

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
