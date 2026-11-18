# -*- coding: utf-8 -*-
"""renderer 硬崩现场采集。

为什么单独做一个模块
--------------------
2026-11-18 12:38 那类崩溃只有一行：

    Desktop renderer exited: crashed
    (no error-level renderer console output was captured)

没有任何 JS 堆栈 —— 也就是说"崩在哪儿"永远查不到，能查的只有"崩的那一刻机器
长什么样"。而这恰恰是最容易被冲掉的信息：下一份 crash-*.log 一落盘，上一份的
上下文就没了。

于是这里只做一件事：在分类为 runtime_error 的当下，把现场固定成一份目录，
留给自己事后比对（GPU 驱动版本换了？显存爆了？几个插件同时往 renderer 塞东西？）。

采集内容
--------
  crash.log            最新那份 crash-*.log 原样拷贝（防被覆盖）
  scene.json           进程树 / 内存 / GPU / 启动参数 / 崩溃分类
  超过 keep 份自动删最旧（默认 10 份，约几十 KB 一份，不占地方）

设计口径：**采集永远不许失败到影响主流程** —— 每段都单独 try，
拿不到就记 "skipped/error"，不让"采现场"变成新的崩溃源。
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

INTERESTING = ("DeepSeek Harness", "dsh", "ZeroFall", "electron", "node")


# ── 采集项 ──────────────────────────────────────────────────────────────────


def _data_root() -> Path:
    raw = os.environ.get("ZF_DATA_DIR") or ""
    if raw:
        return Path(raw)
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ZeroFall"


def _latest_crash_log(appdata: str) -> Optional[Path]:
    if not appdata:
        return None
    logs = Path(appdata) / "logs"
    if not logs.is_dir():
        return None
    cands = sorted(logs.glob("crash-*.log"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def _process_tree() -> Dict[str, Any]:
    if psutil is None:
        return {"error": "psutil 不可用"}
    rows: List[Dict[str, Any]] = []
    try:
        for p in psutil.process_iter(["pid", "name", "create_time"]):
            name = (p.info.get("name") or "")
            if not any(k.lower() in name.lower() for k in INTERESTING):
                continue
            row = {"pid": p.info.get("pid"), "name": name}
            try:
                row["rss_mb"] = round(p.memory_info().rss / 1048576, 1)
            except Exception:
                row["rss_mb"] = None
            try:
                row["cpu_pct"] = p.cpu_percent(interval=None)
            except Exception:
                row["cpu_pct"] = None
            try:
                born = p.info.get("create_time")
                if born:
                    row["uptime_s"] = int(datetime.now().timestamp() - born)
            except Exception:
                pass
            rows.append(row)
    except Exception as exc:                       # noqa: BLE001
        return {"error": str(exc)}
    rows.sort(key=lambda r: (r.get("rss_mb") or 0), reverse=True)
    return {"count": len(rows), "processes": rows[:12]}


def _memory() -> Dict[str, Any]:
    if psutil is None:
        return {"error": "psutil 不可用"}
    try:
        vm = psutil.virtual_memory()
        return {"total_gb": round(vm.total / 1073741824, 1),
                "available_gb": round(vm.available / 1073741824, 1),
                "used_pct": vm.percent}
    except Exception as exc:                       # noqa: BLE001
        return {"error": str(exc)}


def _gpu() -> Dict[str, Any]:
    """有 nvidia-smi 就采，没有就明确记 skipped（别留一个空字典让人猜）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return {"status": "skipped", "reason": (out.stderr or "").strip()[:160]}
        rows = []
        for line in (out.stdout or "").strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 5:
                rows.append({"name": parts[0], "driver": parts[1],
                             "mem_used_mb": parts[2], "mem_total_mb": parts[3],
                             "util_pct": parts[4]})
        return {"status": "ok", "gpus": rows}
    except FileNotFoundError:
        return {"status": "skipped", "reason": "没有 nvidia-smi（非 N 卡或未装驱动）"}
    except Exception as exc:                       # noqa: BLE001
        return {"status": "error", "reason": str(exc)[:160]}


def _gpu_flags_in_log(text: str) -> List[str]:
    """从 crash log 里挑出和 GPU/渲染相关的行，省得再去翻原文。"""
    keys = ("gpu", "renderer", "crashpad", "gles", "angle", "swiftshader", "vulkan")
    return [ln.strip() for ln in text.splitlines()
            if any(k in ln.lower() for k in keys)][:20]


# ── 入口 ────────────────────────────────────────────────────────────────────


def collect(appdata: str, root: Optional[Path] = None, keep: int = 10,
            category: str = "runtime_error", reason: str = "") -> Dict[str, Any]:
    """采一份现场，返回 {"dir": str, "scene": str, "ok": bool}。"""
    root = Path(root) if root else _data_root() / "logs" / "renderer_crash_dump"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = root / stamp
    scene: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "category": category,
        "reason": reason,
        "appdata": appdata,
        "python": sys.version.split()[0],
    }

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "dir": "", "scene": "", "error": str(exc)}

    src = _latest_crash_log(appdata)
    copied = ""
    if src is not None:
        try:
            copied = str(out / "crash.log")
            shutil.copyfile(src, copied)
            scene["crash_log_source"] = str(src)
            scene["crash_log_mtime"] = datetime.fromtimestamp(
                src.stat().st_mtime).isoformat(timespec="seconds")
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
                scene["gpu_related_lines"] = _gpu_flags_in_log(text)
            except OSError:
                scene["gpu_related_lines"] = []
        except OSError as exc:
            scene["crash_log_error"] = str(exc)

    scene["memory"] = _memory()
    scene["gpu"] = _gpu()
    scene["process_tree"] = _process_tree()

    scene_path = ""
    try:
        scene_path = str(out / "scene.json")
        Path(scene_path).write_text(
            json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "dir": str(out), "scene": "", "error": str(exc)}

    _prune(root, keep)
    return {"ok": True, "dir": str(out), "scene": scene_path, "crash_log": copied}


def _prune(root: Path, keep: int) -> None:
    try:
        dirs = sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: p.name)
    except OSError:
        return
    for old in dirs[:max(0, len(dirs) - max(1, int(keep)))]:
        try:
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass


# ── 自测 ────────────────────────────────────────────────────────────────────


def _self_test() -> int:
    import tempfile

    ok_all = True
    with tempfile.TemporaryDirectory(prefix="crash-dump-") as tmp:
        appdata = Path(tmp) / "dsh-desktop"
        (appdata / "logs").mkdir(parents=True)
        (appdata / "logs" / "crash-2026-11-18T04-38-24-665Z-renderer.log").write_text(
            "Desktop renderer exited: crashed\nGPU process crashed: gpu_process_gone\n",
            encoding="utf-8")

        def say(label: str, cond: bool, extra: str = "") -> None:
            nonlocal ok_all
            ok_all = ok_all and cond
            print("  {}  {}{}".format("ok " if cond else "FAIL", label,
                                      ("  " + extra) if extra else ""))

        res = collect(str(appdata), root=Path(tmp) / "dump")
        say("采集成功", res["ok"], res["dir"][-19:])
        scene = json.loads(Path(res["scene"]).read_text(encoding="utf-8"))
        say("crash.log 已拷贝", Path(res["crash_log"]).exists())
        say("分类记下来了", scene.get("category") == "runtime_error")
        say("GPU 相关行被抽出来", len(scene.get("gpu_related_lines") or []) >= 1,
            str(len(scene.get("gpu_related_lines") or [])))
        say("进程/内存/GPU 三段都在",
            all(k in scene for k in ("process_tree", "memory", "gpu")))

        # 轮转：连采 4 份、keep=2，只应剩 2 份
        for _ in range(3):
            collect(str(appdata), root=Path(tmp) / "dump", keep=2)
        left = [p for p in (Path(tmp) / "dump").iterdir() if p.is_dir()]
        say("超过 keep 自动删最旧", len(left) <= 2, "剩 {} 份".format(len(left)))

    print("结果：{}".format("通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(_self_test())
