# -*- coding: utf-8 -*-
"""cordis.patch.yml 写入的互斥 + 写后自校验。

为什么要有这个模块
------------------
11-17 那三次 host fatal（`YAMLException: null byte is not allowed in input`）
指向同一个源头：**写入被打断**。当时链路上有两个写者 ——
  · DSH 宿主自己每次启动重写这份 overlay；
  · ZeroFall 的 resume_guard 改 maxSessions 基线；
再加上 paste 脚本/编辑器手工改，三方并发时谁都没加锁。
`os.replace` 保证了"文件要么旧要么新"，但保证不了"新内容本身是完整的"：
临时文件写到一半被强杀，留下的是 0 字节 / 半截 / NUL 前缀，replace 照样成功。

本模块给 ZeroFall 自己的写路径补两件事：
  1. 跨进程互斥（Windows 用 msvcrt.locking，标准库，不引 pywin32）；
  2. 写完立刻读回比对 + 结构校验，不合格就回滚到写前内容并抛错。

它管不到 DSH 宿主的写入（那是另一个进程、另一套代码），
那条路靠 modules/patch_guard.py 在启动前兜底 —— 两者是接力关系，不是替代。
"""
import os
import sys
import time
import contextlib
from pathlib import Path
from typing import IO, Iterator, Optional


# ── 跨进程互斥 ──────────────────────────────────────────────────────────────

_IS_WIN = sys.platform.startswith("win")


@contextlib.contextmanager
def file_lock(target: Path, timeout: float = 5.0, poll: float = 0.05) -> Iterator[None]:
    """对 `target` 的旁路锁文件加独占锁；拿不到就等到 timeout。

    锁文件是 `<target>.flock`（不是目标本身）：
    锁目标文件会让 DSH 读的时候踩到 sharing violation，得不偿失。

    Windows 用 `msvcrt.locking(fd, LK_NBLCK, 1)`，锁 1 个字节；
    拿不到会抛 OSError，按 poll 间隔重试。非 Windows 退回 fcntl；
    两者都没有（理论上不会发生）就退化成进程内无锁 —— 宁可降级也不阻断写入。
    """
    lock_path = Path(str(target) + ".flock")
    fd: Optional[int] = None
    acquired = False
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                if _IS_WIN:
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(
                        "等不到 {} 的写锁（{:.1f}s），放弃写入以免写坏".format(
                            target.name, timeout))
                time.sleep(poll)
        yield
    finally:
        if fd is not None:
            if acquired:
                try:
                    if _IS_WIN:
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)


# ── 写后自校验 ──────────────────────────────────────────────────────────────


class WriteVerificationError(RuntimeError):
    """写下去的内容和读回来的不一致，或结构校验没过。"""


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def atomic_write_bytes(path: Path, data: bytes, validator=None,
                      timeout: float = 5.0) -> None:
    """带锁 + 带校验的原子写。

    流程：pre-image → 写临时文件 → 逐字节回读比对 → os.replace →
            再回读目标校验 → 不合格就把 pre-image 顶回去并抛 WriteVerificationError。

    `validator: (bytes) -> (bool, reason)` 只做结构判断（例如 patch_guard.inspect_bytes），
    不传就只做"字节一致"这一项。
    """
    path = Path(path)
    pre = _read_bytes(path)
    tmp = path.with_name("{}.zf-tmp-{}".format(path.name, os.getpid()))

    with file_lock(path, timeout=timeout):
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            written = _read_bytes(tmp)
            if written != data:
                raise WriteVerificationError(
                    "临时文件回读不一致（写入 {} 字节，读回 {} 字节）".format(
                        len(data), len(written or b"")))
            if validator is not None:
                ok, reason = validator(written)
                if not ok:
                    raise WriteVerificationError("写前结构校验没过：{}".format(reason))
            os.replace(tmp, path)
        except BaseException:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

        # 落地后再验一次：有些"半截文件"是 replace 之后被别的写者截断的
        after = _read_bytes(path)
        bad = None
        if after != data:
            bad = "落盘内容与写入不一致（写入 {} 字节，读回 {} 字节）".format(
                len(data), len(after or b""))
        elif validator is not None:
            ok, reason = validator(after)
            if not ok:
                bad = "落盘内容结构校验没过：{}".format(reason)
        if bad:
            if pre is not None:
                with open(tmp, "wb") as fh:
                    fh.write(pre)
                os.replace(tmp, path)
                raise WriteVerificationError("{}；已回滚到写前内容".format(bad))
            raise WriteVerificationError(bad)


def atomic_write_text(path: Path, text: str, validator=None,
                      timeout: float = 5.0) -> None:
    """同上，但固定 LF —— patch.yml 是 LF 文件，被改成 CRLF 会让
    tools/switch.ps1 的行尾断言全部失配（11-17 踩过）。"""
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes(path, text.encode("utf-8"), validator=validator, timeout=timeout)


# ── 自测 ────────────────────────────────────────────────────────────────────


def _self_test() -> int:
    import tempfile
    import threading

    ok_all = True
    with tempfile.TemporaryDirectory(prefix="patch-io-") as tmp:
        target = Path(tmp) / "cordis.patch.yml"
        target.write_bytes(b"- id: a\n")

        def say(label: str, cond: bool, extra: str = "") -> None:
            nonlocal ok_all
            ok_all = ok_all and cond
            print("  {}  {}{}".format("ok " if cond else "FAIL", label,
                                      ("  " + extra) if extra else ""))

        # 1) 正常写：内容落地、LF 保持
        atomic_write_text(target, "- id: b\nconfig:\n  x: 1")
        body = target.read_bytes()
        say("正常写入", body == b"- id: b\nconfig:\n  x: 1\n", repr(body[-12:]))
        say("没有残留临时文件", not list(Path(tmp).glob("*.zf-tmp-*")))

        # 2) 结构校验拦坏内容：抛错 + 回滚到写前
        bad_val = lambda raw: (b"\x00" not in raw and len(raw) > 16, "含 NUL 或过短")
        try:
            atomic_write_bytes(target, b"\x00- id: c\n", validator=bad_val)
            say("坏内容被拦下", False)
        except WriteVerificationError as e:
            say("坏内容被拦下", True, str(e).split("；")[0])
        say("回滚到写前内容", target.read_bytes() == b"- id: b\nconfig:\n  x: 1\n",
            repr(target.read_bytes()))

        # 3) 跨进程锁：子进程持锁时本进程必须等
        lock_target = Path(tmp) / "locked.yml"
        lock_target.write_bytes(b"- id: x\n")
        held = {"done": False}

        def _hold():
            with file_lock(lock_target, timeout=1.0):
                held["done"] = True
                time.sleep(0.8)

        t = threading.Thread(target=_hold)
        t.start()
        time.sleep(0.2)
        t0 = time.time()
        # 同进程内 msvcrt 是"按 fd"加锁，换 fd 同样会被挡
        try:
            with file_lock(lock_target, timeout=0.4):
                waited_ok = (time.time() - t0) > 0.15
                say("第二个写者被挡/或等到超时", waited_ok,
                    "等待 {:.2f}s".format(time.time() - t0))
        except TimeoutError:
            say("第二个写者被挡/或等到超时", True,
                "等待 {:.2f}s 后超时".format(time.time() - t0))
        t.join()

    print("结果：{}".format("通过" if ok_all else "有失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(_self_test())
