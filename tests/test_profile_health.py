# -*- coding: utf-8 -*-
"""profile 健康诊断（7 项）的回归测试

每种失败形状都用临时 profile 造一遍，再对真实 profile 跑一次只读核对。
不写真实文件。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_health_'))

from modules import profile_health as ph              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


def level_of(report, cid):
    for c in report["checks"]:
        if c["id"] == cid:
            return c["level"]
    return None


def detail_of(report, cid):
    for c in report["checks"]:
        if c["id"] == cid:
            return c.get("detail", "")
    return ""


def build_profile(root: Path, *, bundles=None, deps=None, patch_entries=None,
                  bom=False, node_modules: dict = None, lock_text=None,
                  bad_json=False) -> Path:
    home = root
    pdir = home / "profiles" / "desktop"
    pdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "name": "dsh-profile-desktop",
        "private": True,
        "dependencies": deps if deps is not None else {
            "dsh-purge-community": "file:./vendor/dsh-purge-community",
        },
        "dsh": {"profile": {"bundles": bundles if bundles is not None else [
            "@deepseek-ai/dsh-base", "dsh-purge-community"]}},
    }
    raw = json.dumps(doc, ensure_ascii=False, indent=2)
    if bad_json:
        raw = raw[:-10] + "  {{{"
    data = raw.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    (pdir / "package.json").write_bytes(data)

    entries = patch_entries if patch_entries is not None else [
        {"id": "permission-presets", "name": "@deepseek-ai/dsh-permission-presets",
         "config": {"defaultPreset": "danger-full-access"}},
        {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {}},
        {"id": "agent-loop", "name": "@deepseek-ai/dsh-agent-loop"},
    ]
    lines = []
    for e in entries:
        keys = []
        if e.get("id"):
            keys.append("id: {}".format(e["id"]))
        if e.get("name"):
            keys.append('name: "{}"'.format(e["name"]))
        if e.get("insert") is not None:
            keys.append("insert:")
            for item in e["insert"]:
                iid = item.get("id") if isinstance(item, dict) else item
                keys.append("  - id: {}".format(iid))
        if e.get("config") is not None:
            keys.append("config: {}")
        # 块的第一个真实键做 `- key:`，其余加两空格缩进；
        # 顶层没有 id 的条目（纯 `- insert:` 新增挂载）也能正确成形。
        for i, k in enumerate(keys):
            lines.append(("- " + k) if i == 0 else ("  " + k))
        lines.append("")
    (pdir / "cordis.patch.yml").write_text("\n".join(lines), encoding="utf-8")

    nm = pdir / "node_modules"
    nm.mkdir(parents=True, exist_ok=True)
    for name, kind in (node_modules or {}).items():
        p = nm / name
        if kind == "dir":
            p.mkdir(parents=True, exist_ok=True)
        elif kind == "broken":
            pass
    if lock_text is not None:
        (pdir / "pnpm-lock.yaml").write_text(lock_text, encoding="utf-8")
    return home


print("== 1. 健康 profile ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h1_')),
                     node_modules={"dsh-purge-community": "dir"},
                     lock_text="dsh-purge-community:\n  specifier: file:./vendor\n")
rep = ph.run_all(home, "desktop", host_base=None)
check("十项检查都在", len(rep["checks"]) == 10, len(rep["checks"]))
check("健康 profile 无 error", rep["summary"]["error"] == 0, rep["summary"])
check("可以重启", rep["can_restart"] is True, rep["verdict"])

print("== 2. manifest 可读性 ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h2_')), bom=True)
check("BOM 被认成 error", level_of(ph.run_all(home, "desktop"), "manifest") == "error")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h3_')), bad_json=True)
rep = ph.run_all(home, "desktop")
check("坏 JSON 被认成 error", level_of(rep, "manifest") == "error")
check("坏 JSON 时不允许重启", rep["can_restart"] is False)

print("== 3. bundle 可解析性 ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h4_')),
                     bundles=["@deepseek-ai/dsh-base", "not-installed-plugin"],
                     deps={"not-installed-plugin": "file:./vendor/x"})
rep = ph.run_all(home, "desktop")
check("声明了但缺失 → error", level_of(rep, "bundles") == "error")
check("内建 @deepseek-ai 不误报",
      "not-installed-plugin" in [c for c in [rep["checks"][1]["detail"]]][0]
      and "@deepseek-ai/dsh-base" not in rep["checks"][1]["detail"], rep["checks"][1]["detail"])

print("== 4. 一致性 ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h5_')),
                     bundles=["@deepseek-ai/dsh-base", "ghost-plugin"],
                     deps={"dsh-purge-community": "file:./vendor/p"})
rep = ph.run_all(home, "desktop")
check("孤儿 bundle → error", level_of(rep, "consistency") == "error")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h6_')),
                     bundles=["@deepseek-ai/dsh-base"],
                     deps={"dsh-purge-community": "file:./vendor/p"})
check("装了没启用 → info", level_of(ph.run_all(home, "desktop"), "consistency") == "info")

print("== 5. 重置形状 ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h7_')),
                     bundles=["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
                     deps={}, patch_entries=[{"id": "permission-presets", "name": "x"}])
rep = ph.run_all(home, "desktop")
check("模板形状 → error", level_of(rep, "reset") == "error")
check("重置形状时不允许重启", rep["can_restart"] is False)

print("== 6. lockfile ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h8_')),
                     deps={"dsh-purge-community": "file:./vendor/p"},
                     node_modules={"dsh-purge-community": "dir"},
                     lock_text="lockfileVersion: 9.0\n")
check("本地依赖不在锁里 → warn", level_of(ph.run_all(home, "desktop"), "lock") == "warn")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h9_')), deps={"a": "1.0"},
                     node_modules={"dsh-purge-community": "dir"})
check("没有锁文件 → info", level_of(ph.run_all(home, "desktop"), "lock") == "info")

print("== 7. 联接 ==")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h10_')),
                     deps={"dsh-purge-community": "file:./vendor/p"},
                     node_modules={"dsh-purge-community": "dir"})
check("实体副本（非链接）→ warn", level_of(ph.run_all(home, "desktop"), "links") == "warn")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h11_')),
                     deps={"dsh-purge-community": "file:./vendor/p"},
                     node_modules={})
check("链接缺失 → error", level_of(ph.run_all(home, "desktop"), "links") == "error")

print("== 8. patch ==")
# insert 是「新建挂载」，子项 id 本来就不在顶层 —— 真实 profile 里的
# dsh-resume-sessions 就是这种形状，不能被报成悬空引用。
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h12_')),
                     patch_entries=[{"insert": [{"id": "brand-new-plugin"}]}],
                     node_modules={"dsh-purge-community": "dir"})
check("顶层无 id 的 insert（新增挂载）→ ok",
      level_of(ph.run_all(home, "desktop"), "patch") == "ok")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h12b_')),
                     patch_entries=[{"id": "group-a", "insert": [{"id": "taken"}]},
                                    {"id": "taken"}],
                     node_modules={"dsh-purge-community": "dir"})
check("insert 新建 id 与顶层撞车 → warn",
      level_of(ph.run_all(home, "desktop"), "patch") == "warn")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h13_')),
                     patch_entries=[{"id": "group-a", "insert": [{"id": "x"}, {"id": "x"}]}],
                     node_modules={"dsh-purge-community": "dir"})
check("块内重复 → warn", level_of(ph.run_all(home, "desktop"), "patch") == "warn")
home = build_profile(Path(tempfile.mkdtemp(prefix='zf_h13b_')),
                     patch_entries=[{"id": "group-a", "insert": [{"id": "taken"}, {"id": "taken"}]},
                                    {"id": "taken"}],
                     node_modules={"dsh-purge-community": "dir"})
rep = ph.run_all(home, "desktop")
detail = detail_of(rep, "patch")
# 两条规则不能互相掩盖：同时存在时要一次报全，
# 否则用户修完撞车再跑一次才发现块内还有重复。
check("撞车 + 块内重复 一次报全",
      level_of(rep, "patch") == "warn" and "撞车" in detail and "块内重复" in detail,
      detail)

print("== 9. 真实 profile 只读核对 ==")
real_home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
if (real_home / "profiles" / "desktop" / "package.json").is_file():
    rep = ph.run_all(real_home, "desktop")
    print("\n".join("    " + l for l in ph.format_lines(rep)))
    check("真实 profile 能跑完十项", len(rep["checks"]) == 10, len(rep["checks"]))
    check("真实 profile 的 bundle 全部可解析",
          level_of(rep, "bundles") == "ok", rep["checks"][1]["detail"])
    # 真实 profile 里 pnpm 把部分 vendor 依赖落成了实体副本（内容与源一致）。
    # 那是 info 不是问题：只跑起来完全不需要链接，丢的只是"改源码即时生效"。
    check("真实 profile 的联接正常",
          level_of(rep, "links") in ("ok", "info"), rep["checks"][5]["detail"])
else:
    check("真实 profile 检查（跳过）", True)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
