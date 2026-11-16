# -*- coding: utf-8 -*-
"""会话续跑守护（modules/resume_guard.py）的回归测试

重点：ZeroFall 只做「让插件还在 + 让关闭前在跑的会话被看见」，所以测试要盯住两件事
  A. 自愈是**幂等**的：装一次、再装一次不能重复插条目、不能反复改文件
  B. 候选语义和插件侧一致：只认 running / active goal，idle 的历史会话不能进来
     （就是用户明确否掉的"一重启把所有会话都拉起来"）

真实 ~/.dsh 一律不写：全部在 tempfile 造的 home 里跑。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("ZF_DATA_DIR", tempfile.mkdtemp(prefix="zf_resume_"))

from modules import resume_guard as rg  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def make_profile(root: Path, bundles=True):
    pdir = root / "profiles" / "desktop"
    pdir.mkdir(parents=True)
    pkg = {
        "name": "dsh-profile-desktop",
        "private": True,
        "dependencies": {},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}},
    }
    (pdir / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")
    (pdir / "cordis.patch.yml").write_text("# user layer\n[]\n", encoding="utf-8")
    return pdir


def main():
    tmp = Path(tempfile.mkdtemp(prefix="zf_resume_home_"))
    pdir = make_profile(tmp)

    print("A. 首次安装")
    st = rg.plugin_status(pdir)
    check("初始 patch 缺条目", st["patch"] == "missing", st["patch"])
    check("初始未就绪", st["ready"] is False)

    rep = rg.ensure_plugin(tmp, "desktop", host_llm=None)
    check("install 无错误", not rep["errors"], rep["errors"])
    st = rg.plugin_status(pdir)
    check("vendor index.js 已落盘", st["vendor"], st["vendor_index_bytes"])
    check("依赖已声明", st["dependency"])
    check("node_modules 链接已建", st["node_modules_present"])
    check("patch 条目已插入", st["patch"] == "enabled", st["patch"])
    check("整体就绪", st["ready"], st)

    patch_text = (pdir / "cordis.patch.yml").read_text(encoding="utf-8")
    check("插入的是 insert 列表项", "- id: resume-sessions" in patch_text)
    check("默认 maxSessions 为 8（多会话一起续跑）", "maxSessions: 8" in patch_text, patch_text[:0])
    check("默认不只让第一个续跑", "continueOnlyFirst: false" in patch_text)
    check("默认日志上限 8MB", "maxSessionLogMB: 8" in patch_text)

    print("B. 幂等")
    rep2 = rg.ensure_plugin(tmp, "desktop", host_llm=None)
    check("二次安装无动作", rep2["actions"] == [], rep2["actions"])
    check("条目只出现一次", patch_text.count("- id: resume-sessions") == 1)

    print("C. 被禁用后自愈")
    disabled = patch_text.replace("enabled: true", "enabled: false")
    (pdir / "cordis.patch.yml").write_text(disabled, encoding="utf-8")
    check("读到 disabled", rg.plugin_status(pdir)["patch"] == "disabled")
    rep3 = rg.ensure_plugin(tmp, "desktop", host_llm=None)
    check("再次自愈成功", rg.plugin_status(pdir)["patch"] == "enabled", rep3["actions"])
    check("其它配置没被改", "maxSessions: 8" in (pdir / "cordis.patch.yml").read_text(encoding="utf-8"))

    print("D. 候选语义：只认 running / active goal")
    now = 1_794_000_000_000
    snapshot = {
        "version": 2,
        "sessions": {
            "session-run": {"status": "running", "cwd": "C:/ws", "title": "在跑", "at": now, "runningAt": now},
            "session-goal": {"status": "idle", "cwd": "C:/ws", "title": "有 goal", "at": now - 1000,
                             "goalPhase": "active"},
            "session-disposed-run": {"status": "disposed", "cwd": "C:/ws", "title": "关的时候还在跑",
                                      "at": now - 2000, "runningAt": now - 3000, "interrupted": True},
            "session-idle": {"status": "idle", "cwd": "C:/ws", "title": "历史", "at": now - 5000},
            "session-failed": {"status": "failed", "cwd": "C:/ws", "title": "失败", "at": now - 9000},
        },
        "guard": {"crashes": 0},
        "boot": {"pid": 123, "restoring": False},
    }
    (tmp / rg.STATE_FILE).write_text(json.dumps(snapshot), encoding="utf-8")

    proj = tmp / "storages" / "session_projcache" / "sessions"
    proj.mkdir(parents=True)
    (proj / "session-run.json").write_text(json.dumps(
        {"record": {"rows": {"turnBoundary": {"val": {"openTurnStartSeq": 59}}}}}), encoding="utf-8")
    (proj / "session-idle.json").write_text(json.dumps(
        {"record": {"rows": {"turnBoundary": {"val": {"openTurnStartSeq": None}}}}}), encoding="utf-8")

    rows = rg.pending_sessions(tmp, now=now)
    ids = [r["session_id"] for r in rows]
    check("只返回 running / interrupted / active goal",
          sorted(ids) == ["session-disposed-run", "session-goal", "session-run"], ids)
    check("idle 历史会话被排除", "session-idle" not in ids)
    check("failed 不被当成候选", "session-failed" not in ids)
    disp = next(r for r in rows if r["session_id"] == "session-disposed-run")
    check("disposed 但当时在跑的会话仍算候选", disp["interrupted"] is True)
    run_row = next(r for r in rows if r["session_id"] == "session-run")
    check("能读到未关闭的 turn", run_row["open_turn"] is True)

    guard = rg.snapshot_guard(tmp)
    check("guard 读到 crashes", guard["crashes"] == 0)
    check("guard 读到 restoring", guard["restoring"] is False)
    check("guard 统计会话数", guard["total"] == 5, guard["total"])

    print("E. 快照备份")
    bak = rg.backup_snapshot(tmp)
    check("备份文件存在", bak is not None and Path(bak).is_file())
    (tmp / rg.STATE_FILE).write_text("{}", encoding="utf-8")
    check("还原成功", rg.restore_snapshot_backup(tmp, bak) and rg.read_snapshot(tmp).get("version") == 2)

    print("F. 健壮性")
    check("坏快照当空处理", rg.read_snapshot(tmp / "nope") == {})
    (tmp / rg.STATE_FILE).write_text("{not json", encoding="utf-8")
    check("畸形快照不抛", rg.read_snapshot(tmp) == {})
    check("pdir 不存在时状态为空", rg.plugin_status(tmp / "profiles" / "none")["patch"] == "missing")
    bogus = rg.ensure_plugin(tmp, "nope", host_llm=None)
    check("坏 profile 报错而不是崩溃", bool(bogus["errors"]), bogus)

    print("G. 阈值被改回老默认时自愈（多会话只续跑一个的根因）")
    downgraded = (pdir / "cordis.patch.yml").read_text(encoding="utf-8")
    downgraded = downgraded.replace("maxSessions: 8", "maxSessions: 2")
    downgraded = downgraded.replace("continueOnlyFirst: false", "continueOnlyFirst: true")
    (pdir / "cordis.patch.yml").write_text(downgraded, encoding="utf-8")
    rep_g = rg.ensure_plugin(tmp, "desktop", host_llm=None)
    txt = (pdir / "cordis.patch.yml").read_text(encoding="utf-8")
    check("maxSessions 被纠回 8", "maxSessions: 8" in txt, [l for l in txt.splitlines() if "maxSessions" in l])
    check("continueOnlyFirst 被纠回 false", "continueOnlyFirst: false" in txt)
    check("自愈动作有记录", any("阈值" in str(a) for a in rep_g["actions"]), rep_g["actions"])
    rep_g2 = rg.ensure_plugin(tmp, "desktop", host_llm=None)
    check("纠回后幂等", not any("阈值" in str(a) for a in rep_g2["actions"]), rep_g2["actions"])

    print("I. 名额：maxSessions=8 时，关前 5 个在跑的全部都要恢复")
    now2 = 1_794_000_100_000
    five = {
        "version": 2,
        "sessions": {
            f"sess-{i}": {"status": "running", "cwd": "C:/ws", "title": f"在跑{i}",
                          "at": now2, "runningAt": now2 - i * 1000}
            for i in range(5)
        },
        "guard": {"crashes": 0},
        "boot": {"pid": 1, "restoring": False},
    }
    (tmp / rg.STATE_FILE).write_text(json.dumps(five), encoding="utf-8")
    big = rg.pending_sessions(tmp, cfg={"maxSessions": 8, "workspacePath": ""}, now=now2)
    check("5 个候选全部在名额内", len([r for r in big if r["will_restore"]]) == 5,
          [(r["session_id"], r["will_restore"]) for r in big])
    small = rg.pending_sessions(tmp, cfg={"maxSessions": 2, "workspacePath": ""}, now=now2)
    check("老默认 2 只给 2 个名额（复现原症状）",
          len([r for r in small if r["will_restore"]]) == 2,
          [(r["session_id"], r["will_restore"]) for r in small])
    check("候选本身不受名额影响（面板仍能看到 5 个）", len(small) == 5, len(small))


    print("H. 缺键时补全基线")
    txt2, acts = rg.ensure_thresholds(
        "root:\n- insert:\n    - id: resume-sessions\n      name: 'x'\n"
        "      config:\n        enabled: true\n        mode: 'continue'\n")
    check("补了 maxSessions", "maxSessions: 8" in txt2, txt2)
    check("补了 continueOnlyFirst", "continueOnlyFirst: false" in txt2, txt2)
    check("补键有动作记录", len(acts) >= 2, acts)
    other = "root:\n- id: other\n  config:\n    x: 1\n"
    t3, a3 = rg.ensure_thresholds(other)
    check("没有目标条目时原样返回", t3 == other and a3 == [], (t3, a3))

    print("J. 跨工作区：另一个工作区里正在跑的会话也要接上")
    (tmp / "storages").mkdir(exist_ok=True)
    (tmp / "storages" / "workspace.json").write_text(json.dumps({
        "global": {"defaultWorkspaceId": "w1"},
        "tables": {"workspaces": {"w1": {"path": "C:/ws/a", "name": "A"}}},
    }), encoding="utf-8")
    snap_j = {
        "version": 2,
        "sessions": {
            "ws-a": {"status": "running", "cwd": "C:/ws/a", "title": "A",
                     "at": now2, "runningAt": now2},
            "ws-b": {"status": "running", "cwd": "C:/ws/b", "title": "B",
                     "at": now2, "runningAt": now2 - 1000},
        },
        "guard": {"crashes": 0},
        "boot": {"pid": 1, "restoring": False},
    }
    (tmp / rg.STATE_FILE).write_text(json.dumps(snap_j), encoding="utf-8")
    old_rows = rg.pending_sessions(tmp, cfg={"maxSessions": 8}, now=now2)
    check("老行为：另一个工作区的被过滤掉",
          [r["session_id"] for r in old_rows if r["will_restore"]] == ["ws-a"],
          [(r["session_id"], r["will_restore"]) for r in old_rows])
    new_rows = rg.pending_sessions(tmp, cfg={"maxSessions": 8, "anyWorkspace": True}, now=now2)
    check("anyWorkspace=true：两个都恢复",
          sorted(r["session_id"] for r in new_rows if r["will_restore"]) == ["ws-a", "ws-b"],
          [(r["session_id"], r["will_restore"]) for r in new_rows])

    print("K. 崩溃后残留的 live 条目：按「上个进程消失的时刻」重新判定")
    from datetime import datetime, timedelta, timezone
    written = datetime.now(timezone.utc) - timedelta(minutes=2)
    iso = written.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    w_ms = int(written.timestamp() * 1000)
    snap_k = {
        "version": 2,
        "updatedAt": iso,
        "sessions": {
            "stale-live-recent": {"status": "live", "cwd": "C:/ws", "title": "刚还在跑",
                                   "at": w_ms - 60_000, "runningAt": w_ms - 60_000},
            "stale-live-old": {"status": "live", "cwd": "C:/ws", "title": "三小时前就闲置了",
                                "at": w_ms - 3 * 3600_000, "runningAt": w_ms - 3 * 3600_000},
        },
        "guard": {"crashes": 0},
        "boot": {"pid": 9, "restoring": False},
    }
    (tmp / rg.STATE_FILE).write_text(json.dumps(snap_k), encoding="utf-8")
    rows_k = rg.pending_sessions(tmp, include_idle=True, cfg={
        "maxSessions": 8, "anyWorkspace": True,
        "staleLiveAsInterrupted": True}, now=w_ms)
    ids_k = {r["session_id"]: r["candidate"] for r in rows_k}
    check("崩溃时刚在跑的 live 会话算候选", ids_k.get("stale-live-recent") is True, ids_k)
    check("早就闲置的 live 会话不算候选", ids_k.get("stale-live-old") is False, ids_k)
    rows_off = rg.pending_sessions(tmp, include_idle=True, cfg={
        "maxSessions": 8, "anyWorkspace": True,
        "staleLiveAsInterrupted": False}, now=w_ms)
    check("关掉开关就退回老行为（都不算）",
          all(not r["candidate"] for r in rows_off), [(r["session_id"], r["candidate"]) for r in rows_off])

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

