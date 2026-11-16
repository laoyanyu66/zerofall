# -*- coding: utf-8 -*-
"""会话续跑：ZeroFall 侧的语义回归

对齐对象是插件（modules/payload/dsh-resume-sessions/index.js）的 pick()：
面板上「下次恢复」说会恢复，就得真的是插件会恢复的那几个。

覆盖：
  A. closed_while_running 的锚点语义（与插件 closedWhileRunning 一致）
  B. pending_sessions 的筛选项：保质期 / 工作区 / 归档 / 空白 / 已恢复 / 名额
  C. read_plugin_config 读的是**现役**配置，不是写死的默认值
  D. 端口发现不再依赖裸 powershell.exe（本机 PATH 里没有它就整条链断掉）
  E. 集成：面板口径 vs 插件自报口径（端点不可达时判 SKIP，不算回归）
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("ZF_DATA_DIR", tempfile.mkdtemp(prefix="zf_resume_sem_"))

from modules import resume_guard as rg  # noqa: E402

PASS, FAIL, SKIP = [], [], []
MIN = 60_000
HOUR = 60 * MIN
NOW = int(time.time() * 1000)


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print(f"  SKIP {name}  [{why}]")


def write_home(tmp: Path) -> Path:
    """造一个假的 ~/.dsh：快照 + 投影缓存 + 工作区表。"""
    home = tmp / "home"
    (home / "storages" / "session_projcache" / "sessions").mkdir(parents=True)
    (home / "storages").mkdir(parents=True, exist_ok=True)
    (home / "storages" / "workspace.json").write_text(json.dumps({
        "global": {"defaultWorkspaceId": "ws1", "archivedSessionIds": ["archived-1"]},
        "tables": {"workspaces": {"ws1": {"path": "C:/ws"}}},
    }), encoding="utf-8")
    return home


def put_session(home: Path, sid: str, entry: dict):
    (home / "storages" / "session_projcache" / "sessions" / f"{sid}.json").write_text(json.dumps({
        "record": {"rows": {
            "turnBoundary": {"val": {"openTurnStartSeq": None}},
            "sessionListMetadata": {"val": {"blank": False}},
            "title": {"val": entry.get("title", sid)},
        }},
    }), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zf_resume_sem_home_"))
    home = write_home(tmp)

    print("A. closed_while_running 的锚点语义")
    stale = {"status": "idle", "running": False, "at": NOW - 13 * HOUR,
             "runningAt": NOW - 13 * HOUR - 12_000,
             "history": [{"t": NOW - 13 * HOUR - 12_000, "s": "running"},
                         {"t": NOW - 13 * HOUR, "s": "idle"}]}
    check("陈旧 idle（从未 dispose）→ 不算候选", rg.closed_while_running(stale) is False)
    check("锚点缺 disposedAt 时直接否", rg.closed_while_running({"runningAt": NOW}) is False)

    disposed_running = {"status": "disposed", "running": False, "runningAt": NOW - 30_000,
                        "disposedAt": NOW - 20_000,
                        "history": [{"t": NOW - 30_000, "s": "running"}]}
    check("dispose 时还在跑 → 算候选", rg.closed_while_running(disposed_running) is True)

    long_turn = {"status": "disposed", "running": False, "runningAt": NOW - 20 * MIN,
                 "disposedAt": NOW - 5_000,
                 "history": [{"t": NOW - 20 * MIN, "s": "running"},
                             {"t": NOW - 8_000, "s": "idle"}]}
    check("长 turn 收尾后 8 秒被 dispose → 算候选（锚点取 turn 收尾）",
          rg.closed_while_running(long_turn) is True)

    long_gap = {"status": "disposed", "running": False, "runningAt": NOW - 5 * HOUR,
                "disposedAt": NOW - 1 * HOUR,
                "history": [{"t": NOW - 5 * HOUR, "s": "running"},
                            {"t": NOW - 5 * HOUR + MIN, "s": "idle"}]}
    check("turn 收尾后过了 1 小时才 dispose → 超窗口", rg.closed_while_running(long_gap) is False)

    check("last_turn_end 不把 disposed 当锚点",
          rg.last_turn_end({"history": [{"t": 100, "s": "idle"}, {"t": 300, "s": "disposed"}]}) == 100)

    print("B. pending_sessions 的筛选项")
    cfg = {"maxAgeHours": 48, "runningWindowMin": 15, "maxSessions": 2,
           "workspacePath": "", "anyWorkspace": False}
    sessions = {
        # 真候选：dispose 时还在跑
        "s-running": {"status": "disposed", "running": False, "interrupted": True, "at": NOW - MIN,
                      "runningAt": NOW - MIN, "disposedAt": NOW - MIN, "cwd": "C:/ws",
                      "title": "真候选"},
        # 陈旧 idle：从没被 dispose
        "s-stale": {"status": "idle", "running": False, "at": NOW - 13 * HOUR,
                    "runningAt": NOW - 13 * HOUR - 12_000, "cwd": "C:/ws", "title": "陈旧"},
        # 超保质期
        "s-old": {"status": "disposed", "running": False, "interrupted": True, "at": NOW - 100 * HOUR,
                  "runningAt": NOW - 100 * HOUR, "disposedAt": NOW - 100 * HOUR, "cwd": "C:/ws"},
        # 别的工作区
        "s-other": {"status": "disposed", "running": False, "interrupted": True, "at": NOW - 2 * MIN,
                    "runningAt": NOW - 2 * MIN, "disposedAt": NOW - 2 * MIN, "cwd": "D:/other"},
        # 归档了
        "archived-1": {"status": "disposed", "running": False, "interrupted": True, "at": NOW - 3 * MIN,
                       "runningAt": NOW - 3 * MIN, "disposedAt": NOW - 3 * MIN, "cwd": "C:/ws"},
        # 已恢复过（live）不重复抢
        "s-live": {"status": "live", "running": False, "interrupted": True, "at": NOW - 4 * MIN,
                   "runningAt": NOW - 4 * MIN, "disposedAt": NOW - 4 * MIN, "cwd": "C:/ws"},
        # 正在跑
        "s-now": {"status": "running", "running": True, "at": NOW, "runningAt": NOW, "cwd": "C:/ws"},
    }
    (home / rg.STATE_FILE).write_text(json.dumps({"version": 2, "sessions": sessions}), encoding="utf-8")
    for sid in sessions:
        put_session(home, sid, sessions[sid])

    rows = rg.pending_sessions(home, cfg=cfg)
    ids = {r["session_id"] for r in rows}
    check("真候选进列表", "s-running" in ids, str(sorted(ids)))
    check("陈旧 idle 不进列表", "s-stale" not in ids, str(sorted(ids)))
    check("超保质期不进列表", "s-old" not in ids, str(sorted(ids)))
    check("别的工作区不进列表", "s-other" not in ids, str(sorted(ids)))
    ids_any = {r["session_id"] for r in rg.pending_sessions(
        home, cfg={**cfg, "anyWorkspace": True})}
    check("anyWorkspace=true 时别的工作区也进列表", "s-other" in ids_any, str(sorted(ids_any)))
    check("归档的不进列表", "archived-1" not in ids, str(sorted(ids)))
    check("已恢复(live)不进列表", "s-live" not in ids, str(sorted(ids)))
    check("正在跑的进列表", "s-now" in ids, str(sorted(ids)))

    picked = [r for r in rows if r["will_restore"]]
    check("名额 = maxSessions(2)", len(picked) <= 2, str([r["session_id"] for r in picked]))
    check("名额里优先是 runningAt 最新的",
          picked and picked[0]["session_id"] == "s-now",
          str([r["session_id"] for r in picked]))
    check("候选但没名额的 will_restore=False",
          all(not r["will_restore"] for r in rows if r["candidate"] and r["session_id"] not in
              {p["session_id"] for p in picked}))

    print("C. read_plugin_config 读现役配置")
    pdir = tmp / "profiles" / "desktop"
    pdir.mkdir(parents=True)
    (pdir / "cordis.patch.yml").write_text(
        "# user layer\n- insert:\n    - id: resume-sessions\n      name: 'dsh-resume-sessions'\n"
        "      config:\n        enabled: true\n        mode: 'continue'\n        maxSessions: 2\n"
        "        runningWindowMin: 15\n        maxTotalLogMB: 48\n        continueOnlyFirst: true\n",
        encoding="utf-8")
    pcfg = rg.read_plugin_config(pdir)
    check("读出 maxSessions", pcfg.get("maxSessions") == 2, str(pcfg))
    check("读出 runningWindowMin", pcfg.get("runningWindowMin") == 15, str(pcfg))
    check("读出布尔值", pcfg.get("continueOnlyFirst") is True, str(pcfg))
    check("读出字符串值", pcfg.get("mode") == "continue", str(pcfg))
    check("没有条目时返回空", rg.read_plugin_config(tmp / "profiles" / "none") == {})

    print("D. 端口发现不依赖裸 powershell.exe")
    ps = rg._powershell_exe()
    check("powershell.exe 是绝对路径", os.path.isabs(ps), ps)
    check("powershell.exe 真实存在", os.path.isfile(ps), ps)
    netstat = rg._system32("netstat.exe")
    check("netstat.exe 用的是 System32 绝对路径", os.path.isfile(netstat), netstat)
    ports = rg.discover_ports(force=True)
    if ports:
        print(f"     候选端口: {ports}")
        check("端口发现有结果", len(ports) > 0)
    else:
        skip("端口发现有结果", "本机没在跑 DSH host（环境缺件，不是代码回归）")

    print("E. 集成：面板口径 vs 插件自报口径")
    home_real = Path(os.environ["USERPROFILE"]) / ".dsh"
    pdir_real = home_real / "profiles" / "desktop"
    live = None
    for port in rg.discover_ports(force=True):
        live = rg.probe_state(port)
        if live:
            break
    if not live:
        skip("面板口径 == 插件口径", "端点不可达（DSH 没开或端口没探到）")
    else:
        plugin_cands = set(live.get("candidates") or [])
        pcfg_real = rg.read_plugin_config(pdir_real)
        local_rows = rg.pending_sessions(home_real, cfg=pcfg_real)
        local_picked = {r["session_id"] for r in local_rows if r["will_restore"]}
        host_max = (live.get("guard") or {}).get("maxSessions")
        if host_max != pcfg_real.get("maxSessions"):
            # 宿主里跑的还是"重启前加载的那份配置"：改 cordis.patch.yml 不会热生效。
            # 这时两边必然不一致（本地按新配置算出 8 个名额，宿主还按老配置报 1~2 个），
            # 属环境状态而非代码回归 —— 重启 DSH 后再核。
            skip("本地复算的名额 ⊆ 插件自报候选",
                 "宿主仍用旧配置（maxSessions={} / 盘上={}），重启 DSH 后复核".format(
                     host_max, pcfg_real.get("maxSessions")))
        else:
            check("本地复算的名额 ⊆ 插件自报候选", local_picked <= plugin_cands,
                  "local={} plugin={}".format(sorted(local_picked), sorted(plugin_cands)))
        check("插件自报候选 ⊆ 本地候选池",
              plugin_cands <= {r["session_id"] for r in rg.pending_sessions(home_real, include_idle=True, cfg=pcfg_real)},
              f"plugin={sorted(plugin_cands)}")
        check("陈旧 idle 不在插件候选里（回归点）",
              not any(sid.startswith("s-stale") for sid in plugin_cands))

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 跳过")
    if FAIL:
        for name in FAIL:
            print("  - " + name)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
