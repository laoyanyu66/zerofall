# -*- coding: utf-8 -*-
"""会话文件扫描 / 修复 的回归测试

用 Node 造三种坏样本（单帧布局、seq 重复、非法 JSON），验证：
  扫描能分类 → dry-run 只说不做 → apply 真修 + 备份 + 修后校验 → 非法 JSON 拒修
最后对真实 sessions 目录做一次只读扫描（不写）。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ.setdefault('ZF_DATA_DIR', tempfile.mkdtemp(prefix='zf_sess_'))

from modules import session_repair as sr              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=''):
    if cond:
        PASS.append(name)
        print('  [PASS] {}'.format(name))
    else:
        FAIL.append(name)
        print('  [FAIL] {} {}'.format(name, extra))


NODE = sr.find_node()

BUILD_JS = r'''
const fs=require("fs"), zlib=require("zlib");
const out=process.argv[1];
const spec=JSON.parse(process.argv[2]);   // {frames: [[line,...], ...], raw_lines: bool}
const chunks = spec.frames.map(lines => {
  const text = spec.raw ? lines.join("") : lines.join("\n") + "\n";
  return zlib.zstdCompressSync(Buffer.from(text, "utf8"),
    { params: { [zlib.constants.ZSTD_c_checksumFlag]: 1 } });
});
fs.writeFileSync(out, Buffer.concat(chunks));
process.stdout.write(JSON.stringify({frames: chunks.length, bytes: Buffer.concat(chunks).length}));
'''


def build(path: Path, frames, raw=False):
    spec = {"frames": frames, "raw": raw}
    r = subprocess.run([NODE, "-e", BUILD_JS, str(path), json.dumps(spec)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return path


def rec(seq, text):
    return json.dumps({"type": "event", "seq": seq, "text": text}, ensure_ascii=False)


print("== 环境 ==")
check("找到 node", bool(NODE), NODE)
check("帧助手存在", sr.tool_path().is_file(), sr.tool_path())

root = Path(tempfile.mkdtemp(prefix='zf_sessroot_'))
sdir = root / "sessions" / "proj-x" / "session-legacy"
sdir.mkdir(parents=True)

print("== 1. 单帧布局（legacy）==")
legacy = sdir / sr.SESSION_GLOB
build(legacy, [[rec(1, "a"), rec(2, "b"), rec(3, "c")]])
st = sr.scan_one(legacy, NODE)
check("分类为 legacy_single_frame", st["state"] == "legacy_single_frame", st["state"])
check("统计到 3 行 3 帧内", st["lines"] == 3 and st["frames"] == 1, (st["lines"], st["frames"]))
dry = sr.repair_one(legacy, NODE, apply=False)
check("dry-run 只判定不写", dry["action"] == "would_repair" and legacy.stat().st_size > 0, dry["action"])
res = sr.repair_one(legacy, NODE, apply=True)
check("修复成功", res["action"] == "repaired", res.get("message"))
check("留了备份", Path(res["backup"]).is_file(), res.get("backup"))
after = sr.scan_one(legacy, NODE)
check("修完状态正常", after["state"] == "ok", after["state"])
check("拆成了每条一帧", after["frames"] >= after["lines"], (after["frames"], after["lines"]))
check("内容没丢", after["lines"] == 3 and after["valid_json"] == 3, (after["lines"], after["valid_json"]))

print("== 2. seq 重复（合成收尾帧）==")
sdir2 = root / "sessions" / "proj-y" / "session-dup"
sdir2.mkdir(parents=True)
dup = sdir2 / sr.SESSION_GLOB
build(dup, [[rec(1, "a")], [rec(2, "b")], [rec(2, "b-again")], [rec(3, "c")]])
st = sr.scan_one(dup, NODE)
check("分类为 seq_overlap", st["state"] == "seq_overlap", st["state"])
check("抓到 1 个重复 seq", st["dup_seq"] == 1, st["dup_seq"])
res = sr.repair_one(dup, NODE, apply=True)
check("去重修复成功", res["action"] == "repaired", res.get("message"))
check("报告丢掉了 1 条", res.get("dropped_dup_seq") == 1, res.get("dropped_dup_seq"))
after = sr.scan_one(dup, NODE)
check("修完没有重复 seq", after["dup_seq"] == 0, after["dup_seq"])
check("保留 3 条记录", after["lines"] == 3, after["lines"])

print("== 3. 非法 JSON 只报不修 ==")
sdir3 = root / "sessions" / "proj-z" / "session-bad"
sdir3.mkdir(parents=True)
bad = sdir3 / sr.SESSION_GLOB
build(bad, [["{not json}"], [rec(1, "a")]])
st = sr.scan_one(bad, NODE)
check("分类为 invalid_json", st["state"] == "invalid_json", st["state"])
before_bytes = bad.read_bytes()
res = sr.repair_one(bad, NODE, apply=True)
check("拒修", res["action"] == "refuse", res["action"])
check("文件没被动", bad.read_bytes() == before_bytes)

print("== 4. scan_all 汇总 ==")
scan = sr.scan_all(root, NODE)
check("扫到 3 个文件", scan["total"] == 3, scan["total"])
check("修完的两个是 ok", scan["summary"].get("ok", 0) == 2, scan["summary"])
check("剩下一个仍拒修", scan["summary"].get("invalid_json", 0) == 1, scan["summary"])

print("== 5. 真实 sessions 目录（只读） ==")
real = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh")))
if (real / "sessions").is_dir():
    live = sr.scan_all(real, NODE)
    print("    文件 {} 个：{}".format(live["total"], live["summary"]))
    check("真实目录扫描不报错", len(live["errors"]) == 0, live["errors"][:2])
    check("真实文件全部能解帧", all(r["state"] != "frame_error" for r in live["files"]),
          [r["name"] for r in live["files"] if r["state"] == "frame_error"][:3])
else:
    check("真实 sessions 检查（跳过）", True)

print()
print('通过 {} 项 / 失败 {} 项'.format(len(PASS), len(FAIL)))
if FAIL:
    print('失败项: {}'.format(', '.join(FAIL)))
    sys.exit(1)
