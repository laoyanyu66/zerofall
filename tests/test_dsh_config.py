# -*- coding: utf-8 -*-
"""DSH 配置编辑（融合 config-editor）的回归测试

分成两段：
  A. 用**受控样例**跑全部语义（改 config / 幂等 / 改回原值 / 删覆盖 / disabled 开关 / 落盘）
  B. 用**真实 profile 补丁**只做只读核对（解析 / 校验 / 定位 / !!js 不炸）
     真实文件不写，避免测试碰生产配置。

核心约束：只动目标 entry 的 config 块，其余字节原样；改回原值必须逐字节还原。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_cfg_'))

from modules import dsh_config as dc                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


SAMPLE = """# 样例补丁：注释必须活下来
- id: permission-presets
  name: "@deepseek-ai/dsh-permission-presets"
  config:
    defaultPreset: danger-full-access

- id: tool-bash
  disabled: !!js process.platform === 'win32'

- id: tool-pwsh
  name: '@deepseek-ai/dsh-tool-pwsh'
  config:
    fetch: true
"""

print("== A1 解析 ==")
rows = dc.parse_entries(SAMPLE)
check("解析成 3 条", len(rows) == 3, len(rows))
check("!!js 不影响解析", "tool-bash" in [r.get("id") for r in rows])
check("!!js 被读成表达式文本",
      dc.parse_entries(SAMPLE)[1].get("disabled") == "process.platform === 'win32'",
      dc.parse_entries(SAMPLE)[1].get("disabled"))
check("整篇校验通过", dc.validate(SAMPLE)[0], dc.validate(SAMPLE)[1])

print("== A2 读取 ==")
check("读到 config", dc.get_entry_config(SAMPLE, "tool-pwsh") == {"fetch": True})
check("has_override 正判", dc.has_override(SAMPLE, "tool-pwsh"))
check("has_override 反判", not dc.has_override(SAMPLE, "tool-bash"))

print("== A3 改 config：只动目标块 ==")
frag = "fetch: false\nsearchTimeoutMs: 60000\nnested:\n  a: 1\n  b: [1, 2]\n"
new = dc.set_entry_config_text(SAMPLE, "tool-pwsh", frag)
ok, msg = dc.validate(new)
check("改后仍是合法 YAML", ok, msg)
check("新值生效", dc.get_entry_config(new, "tool-pwsh").get("nested", {}).get("b") == [1, 2],
      dc.get_entry_config(new, "tool-pwsh"))
diff = [l for l in dc.unified_diff(SAMPLE, new).splitlines()
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
check("差异只落在目标块（≤8 行）", len(diff) <= 8, len(diff))
check("另一条目的 !!js 原封不动", "  disabled: !!js process.platform === 'win32'" in new)
check("注释保留", "# 样例补丁：注释必须活下来" in new)

print("== A4 幂等 & 改回原值 ==")
again = dc.set_entry_config_text(new, "tool-pwsh", frag)
check("同片段再写一次结果不变", again == new)
restored = dc.set_entry_config_text(new, "tool-pwsh", "fetch: true\n")
check("改回原值逐字节还原", restored == SAMPLE,
      "\n".join(list(dc.unified_diff(SAMPLE, restored).splitlines())[:6]))

print("== A5 删覆盖 / 空 config ==")
removed = dc.remove_override_text(SAMPLE, "tool-pwsh")
check("config 块已删", not dc.has_override(removed, "tool-pwsh"))
check("只剩 id/name 时整条删掉", "tool-pwsh" not in removed, removed)
check("删除后仍合法", dc.validate(removed)[0])
check("保留的条目没被动", "tool-bash" in removed and "permission-presets" in removed)

print("== A6 disabled 开关 ==")
off = dc.set_entry_disabled_text(SAMPLE, "permission-presets", True)
check("加上 disabled: true", "  disabled: true" in off.splitlines(), off.splitlines()[:5])
on = dc.set_entry_disabled_text(off, "permission-presets", False)
check("去掉后逐字节还原", on == SAMPLE,
      "\n".join(list(dc.unified_diff(SAMPLE, on).splitlines())[:6]))

print("== A7 落盘：备份 / 原子写 / 回读 / 拒绝坏文本 ==")
tmp = Path(tempfile.mkdtemp(prefix='zf_patch_'))
target = tmp / "cordis.patch.yml"
target.write_text(SAMPLE, encoding="utf-8")
res = dc.apply_text(target, new)
check("changed=True", res["changed"] is True)
check("自动备份存在", res["backup"] and Path(res["backup"]).exists(), res["backup"])
check("磁盘内容=新文本", target.read_text(encoding="utf-8") == new)
check("备份内容=原文", Path(res["backup"]).read_text(encoding="utf-8") == SAMPLE)
try:
    dc.apply_text(target, new + "\n  - 这不是映射\n")
    check("非法结构被拒绝", False, "竟然写进去了")
except dc.ConfigError:
    check("非法结构被拒绝", True)
check("拒绝后文件未动", target.read_text(encoding="utf-8") == new)
check("内容相同时不重复写", dc.apply_text(target, new)["changed"] is False)

print("== B 真实 profile：只读核对 ==")
REAL = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))) / "profiles" / "desktop" / "cordis.patch.yml"
if REAL.exists():
    real = REAL.read_text(encoding="utf-8")
    rrows = dc.parse_entries(real)
    check("真实补丁可解析", len(rrows) >= 5, len(rrows))
    check("真实补丁校验通过", dc.validate(real)[0], dc.validate(real)[1])
    check("!!js 条目在真实文件里也在", "!!js" in real)
    check("定位到已知条目 permission-presets", dc.find_entry(real, "permission-presets") is not None)
    check("定位到 llm-pi-ai", dc.find_entry(real, "llm-pi-ai") is not None)
    check("未写真实文件", REAL.read_text(encoding="utf-8") == real)
else:
    print("  [SKIP] 真实 profile 不存在")
    check("真实 profile 检查（跳过）", True)

print("== C profile 定位 ==")
pdir = dc.resolve_profile_dir(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")), "desktop")
check("定位到 desktop profile", pdir is not None and dc.patch_path(pdir).exists(), pdir)
check("profile 列表非空", len(dc.list_profiles(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))) >= 1)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
