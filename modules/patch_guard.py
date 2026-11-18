# -*- coding: utf-8 -*-
"""cordis.patch.yml 启动前体检与自愈。

为什么要有这个模块
------------------
2026-11-17 实机连踩三次，宿主在启动阶段直接 fatal：

    DesktopHostFatalError: dsh: failed to parse overlay
    C:\\Users\\杰瑞\\.dsh\\profiles\\desktop\\cordis.patch.yml:
    YAMLException: null byte is not allowed in input (1:1)

`dsh-app-boot` 的 `parsePatchList` 在 `main()` 里抛错 → host 进程退出 →
ZeroFall 侧只能看到一个 `exit != 0`，看不出是"配置文件里混了一个 NUL"。
DSH 自己每次启动又会重写这份文件，写入被打断（掉电、强杀、并发写）
就会留下半截内容或 NUL 前缀，于是下一次启动必然崩。

本模块做的事只有一件：**在拉起 DSH 之前，把坏文件换成最近的完好备份**。
不负责改配置内容 —— 改内容有 resume_guard / dsh_config 各自的路。

判定"坏"的口径（只查会让宿主 fatal 的形态）
------------------------------------------
1. 文件存在但 0 字节 / 小于 64 字节（截断）；
2. 含 NUL 字节（`\\x00`）—— 宿主 YAML 解析直接报 null byte；
3. UTF-8 解不开；
4. 有 PyYAML 时用 safe_load 过一遍，抛错即坏；
   没有 PyYAML 时做结构判断：去掉注释与空行后必须以 `-` 或 `[` 开头。

备份口径
--------
候选是 `cordis.patch.yml.bak*`，名字里带 corrupt / invalid / broken / truncated
的一律跳过（那些是"已知坏文件"的留档，本身就可能带 NUL）。
候选也要过同一套检查，只有通过检查的才能当"好备份"用。
"""
import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TARGET_NAME = "cordis.patch.yml"
MIN_BYTES = 16
BAD_TAGS = ("corrupt", "invalid", "broken", "truncated")

try:  # PyYAML 是项目依赖，但打包裁掉时不能让整个模块 import 失败
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


# ── 检查 ────────────────────────────────────────────────────────────────────


def inspect_bytes(raw: bytes) -> Tuple[bool, str]:
    """字节层检查：返回 (是否可用, 原因)。原因只在不可用时才有意义。"""
    if raw is None:
        return False, "读不到内容"
    if len(raw) == 0:
        return False, "文件是空的（0 字节）"
    if b"\x00" in raw:
        pos = raw.index(b"\x00")
        return False, "含 NUL 字节（偏移 {}）".format(pos)
    if len(raw) < MIN_BYTES:
        return False, "只有 {} 字节，疑似被截断".format(len(raw))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, "UTF-8 解不开：{}".format(exc)
    if yaml is not None:
        # 用 compose 而不是 safe_load：只验语法，不构造 Python 对象。
        # profile 里合法的补丁会带 `!!js` 之类 DSH 自定义标签，safe_load 会以
        # "could not determine a constructor for the tag" 拒掉 —— 那不是坏文件。
        try:
            node = yaml.compose(text)
        except Exception as exc:  # ScannerError / ParserError / ComposerError
            return False, "YAML 解析失败：{}".format(str(exc).splitlines()[0][:120])
        if node is not None and type(node).__name__ not in ("SequenceNode", "MappingNode"):
            return False, "顶层不是序列/映射（{}）".format(type(node).__name__)
    else:
        body = "\n".join(
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ).strip()
        if not body.startswith(("-", "[")):
            return False, "结构不像补丁序列（开头不是 '-' / '['）"
    return True, "ok"


def inspect_file(path: Path) -> Tuple[bool, str]:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return False, "文件不存在"
    except OSError as exc:
        return False, "读文件失败：{}".format(exc)
    return inspect_bytes(raw)


def is_backup_of(path: Path, target_name: str = TARGET_NAME) -> bool:
    name = Path(path).name.lower()
    return name.startswith(target_name.lower() + ".bak")


def find_good_backups(pdir: Path, target_name: str = TARGET_NAME) -> List[Path]:
    """按"新→旧"返回所有通过检查的备份。"""
    pdir = Path(pdir)
    rows: List[Tuple[float, Path]] = []
    for cand in pdir.glob(target_name + ".bak*"):
        if not cand.is_file() or not is_backup_of(cand, target_name):
            continue
        if any(tag in cand.name.lower() for tag in BAD_TAGS):
            continue
        ok, _ = inspect_file(cand)
        if not ok:
            continue
        rows.append((cand.stat().st_mtime, cand))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [p for _, p in rows]


# ── 修复 ────────────────────────────────────────────────────────────────────


def _atomic_copy(src: Path, dst: Path, verify: bool = False) -> None:
    """按字节原子替换：先写同目录临时文件，再 os.replace。

    不用 shutil.copy2 直接覆盖：覆盖过程中被强杀会留下半份文件，
    正好制造出"下一次启动读到截断 YAML"的现场。

    verify=True 时走 patch_io：带跨进程锁 + 写后回读比对 + 结构校验，
    校验不过自动回滚到写前内容。留档坏文件时**不能**开 verify（内容本来就是坏的）。
    """
    data = Path(src).read_bytes()
    try:
        from modules.patch_io import atomic_write_bytes
    except ImportError:            # 直接以脚本方式跑（python modules/patch_guard.py）
        from patch_io import atomic_write_bytes
    atomic_write_bytes(dst, data, validator=inspect_bytes if verify else None)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_profile(pdir: Path, apply: bool = True) -> Dict[str, Any]:
    """单个 profile 目录的体检 / 修复。"""
    pdir = Path(pdir)
    target = pdir / TARGET_NAME
    report: Dict[str, Any] = {
        "profile": str(pdir),
        "target": str(target),
        "state": "ok",
        "reason": "",
        "backup_used": "",
        "kept_bad": "",
    }
    if not target.exists():
        report["state"] = "missing"
        report["reason"] = "补丁文件不存在（不是本模块的修复范围）"
        return report

    ok, reason = inspect_file(target)
    if ok:
        return report

    report["state"] = "bad"
    report["reason"] = reason
    backups = find_good_backups(pdir)
    if not backups:
        report["state"] = "bad-no-backup"
        return report

    report["backup_used"] = backups[0].name
    if not apply:
        report["state"] = "would-repair"
        return report

    kept = target.with_name("{}.bak-invalid-{}".format(target.name, _stamp()))
    try:
        _atomic_copy(target, kept)                 # 坏文件留档：不校验内容
        report["kept_bad"] = kept.name
    except OSError:
        # 留档失败也照样修 —— 坏文件的价值低于"起不来"
        report["kept_bad"] = ""
    _atomic_copy(backups[0], target, verify=True)  # 顶回去：必须校验
    report["state"] = "repaired"
    return report


def ensure(home: Optional[Path] = None, profile: Optional[str] = None,
           apply: bool = True) -> Dict[str, Any]:
    """入口：给 home（~/.dsh）就把所有 profile 都过一遍。

    返回 {"repaired": [...], "bad": [...], "checks": [..每份的明细..]}
    """
    if home is None:
        home = Path(os.environ.get("USERPROFILE", "") or Path.home()) / ".dsh"
    home = Path(home)
    reports: List[Dict[str, Any]] = []
    if profile:
        reports.append(ensure_profile(home / "profiles" / profile, apply=apply))
    else:
        root = home / "profiles"
        if root.is_dir():
            for pdir in sorted(p for p in root.iterdir() if p.is_dir()):
                if (pdir / TARGET_NAME).exists():
                    reports.append(ensure_profile(pdir, apply=apply))
    out: Dict[str, Any] = {"checks": reports, "repaired": [], "bad": []}
    for row in reports:
        if row["state"] == "repaired":
            out["repaired"].append(row)
        elif row["state"] in ("bad", "bad-no-backup"):
            out["bad"].append(row)
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


def _print(report: Dict[str, Any]) -> None:
    for row in report["checks"]:
        state = row["state"]
        if state == "ok":
            line = "  ok    {}".format(Path(row["profile"]).name)
        elif state == "missing":
            line = "  skip  {}  补丁文件不存在".format(Path(row["profile"]).name)
        elif state in ("bad", "bad-no-backup"):
            line = "  BAD   {}  {}（{}）".format(
                Path(row["profile"]).name, row["reason"],
                "无可用备份，未修" if state == "bad-no-backup" else "干跑，未修")
        elif state == "would-repair":
            line = "  待修  {}  {} ← {}".format(
                Path(row["profile"]).name, row["reason"], row["backup_used"])
        else:
            line = "  已修  {}  {} ← {}（坏文件留档 {}）".format(
                Path(row["profile"]).name, row["reason"],
                row["backup_used"], row["kept_bad"] or "-")
        print(line)


def _self_test() -> int:
    """离线自测：临时目录造四份坏文件 + 一份好备份，验判定与修复。"""
    import tempfile

    ok_all = True
    with tempfile.TemporaryDirectory(prefix="patch-guard-") as tmp:
        pdir = Path(tmp) / "profiles" / "desktop"
        pdir.mkdir(parents=True)

        def case(label: str, raw: bytes, expect_bad: bool) -> None:
            nonlocal ok_all
            p = Path(tmp) / "probe.yml"
            p.write_bytes(raw)
            ok, reason = inspect_file(p)
            hit = (not ok) == expect_bad
            ok_all = ok_all and hit
            print("  {}  {}  {}".format("ok " if hit else "FAIL", label, reason))

        good = b"- id: agent-default-model\n  config:\n    model: x\n"
        case("正常补丁", good, False)
        case("空文件", b"", True)
        case("NUL 前缀", b"\x00" + good, True)
        case("截断 10 字节", good[:10], True)
        case("坏 YAML", b"- id: [unclosed\n", True)
        case("GBK 乱码", b"\xd6\xd0\xce\xc4" * 20, True)

        target = pdir / TARGET_NAME
        target.write_bytes(b"\x00" + good)
        (pdir / (TARGET_NAME + ".bak-corrupt-20200101-000000")).write_bytes(b"\x00" * 100)
        (pdir / (TARGET_NAME + ".bak-good-20200101-000000")).write_bytes(good)

        dry = ensure_profile(pdir, apply=False)
        print("  干跑：{} ← {}".format(dry["state"], dry["backup_used"]))
        ok_all = ok_all and dry["state"] == "would-repair"

        real = ensure_profile(pdir, apply=True)
        after_ok, _ = inspect_file(target)
        print("  真修：{}，修复后可用={}".format(real["state"], after_ok))
        ok_all = ok_all and real["state"] == "repaired" and after_ok
        ok_all = ok_all and (pdir / real["kept_bad"]).exists()

    print("结果：{}".format("通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="cordis.patch.yml 启动前体检")
    parser.add_argument("--check", action="store_true", help="只检查，不修（默认）")
    parser.add_argument("--repair", action="store_true", help="坏文件用最近完好备份顶回")
    parser.add_argument("--home", default="", help="DSH_HOME，默认 ~/.dsh")
    parser.add_argument("--profile", default="", help="只查指定 profile")
    parser.add_argument("--self-test", action="store_true", help="离线自测")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.self_test:
        return _self_test()

    report = ensure(
        home=Path(args.home) if args.home else None,
        profile=args.profile or None,
        apply=bool(args.repair),
    )
    _print(report)
    if not report["checks"]:
        print("  没有找到任何 cordis.patch.yml")
    return 1 if report["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
