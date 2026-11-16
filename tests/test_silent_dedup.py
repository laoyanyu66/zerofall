# -*- coding: utf-8 -*-
"""静默快照按内容去重（2026-11-12）

静默快照 10 分钟一份，每份 42 MB 目录复制 + 24.5 MB 压缩；实测一天 27 份，
而插件/配置大部分时间根本没动 —— 拍的是同一份内容。这里验证去重逻辑。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ui.main_window import ZeroFallMainWindow as MW  # noqa: E402

PASS, FAIL = [], []


def chk(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + ("" if cond else "  {}".format(extra)))


class Env:
    def __init__(self, fp):
        self.fp = fp

    def quick_fingerprint(self):
        return self.fp


class Cfg:
    def __init__(self, skip=True):
        self.silent_snapshot_skip_unchanged = skip


class H:
    _should_skip_unchanged_snapshot = MW._should_skip_unchanged_snapshot
    _current_content_fingerprint = MW._current_content_fingerprint
    _newest_light_time = MW._newest_light_time

    def __init__(self, fp, skip=True, newest=None, gap_h=6):
        self.config = Cfg(skip)
        self.config.silent_snapshot_max_gap_hours = gap_h
        self.env_checker = Env(fp)
        self._newest = newest

    def _newest_light_time(self):
        return self._newest


def main():
    print("A. 首次（无基准）→ 照打一份，并记下基准")
    h = H('AAAA1111')
    chk(h._should_skip_unchanged_snapshot() is False, '首次不跳过')
    chk(getattr(h, '_last_light_fingerprint', None) == 'AAAA1111', '基准已记录',
        getattr(h, '_last_light_fingerprint', None))

    print("B. 指纹未变 → 跳过")
    chk(h._should_skip_unchanged_snapshot() is True, '同一指纹 → 跳过')

    print("C. 指纹变了 → 不跳过")
    h.env_checker.fp = 'BBBB2222'
    chk(h._should_skip_unchanged_snapshot() is False, '内容变了 → 照打')

    print("D. 开关关掉 → 永远不跳过")
    h2 = H('CCCC3333', skip=False)
    h2._last_light_fingerprint = 'CCCC3333'
    chk(h2._should_skip_unchanged_snapshot() is False, '关掉开关 → 照打')

    print("E. 指纹读不到（空）→ 保守照打")
    h3 = H('')
    h3._last_light_fingerprint = ''
    chk(h3._should_skip_unchanged_snapshot() is False, '空指纹不拦')

    print("F. 指纹读取抛异常 → 不崩、照打")
    class Boom:
        def quick_fingerprint(self):
            raise RuntimeError('插件目录读不到')
    h4 = H('x')
    h4.env_checker = Boom()
    h4._last_light_fingerprint = 'x'
    chk(h4._should_skip_unchanged_snapshot() is False, '异常时照打不崩')

    print("G. 兜底：指纹一直没变，但超过 N 小时没打 → 补一份")
    from datetime import datetime, timedelta
    h5 = H('DDDD4444', newest=datetime.now() - timedelta(hours=7))
    h5._last_light_fingerprint = 'DDDD4444'
    chk(h5._should_skip_unchanged_snapshot() is False, '超 6 小时 → 硬打一份')
    h6 = H('DDDD4444', newest=datetime.now() - timedelta(minutes=20))
    h6._last_light_fingerprint = 'DDDD4444'
    chk(h6._should_skip_unchanged_snapshot() is True, '才 20 分钟 → 仍然跳过')
    h7 = H('DDDD4444', newest=datetime.now() - timedelta(hours=99), gap_h=0)
    h7._last_light_fingerprint = 'DDDD4444'
    chk(h7._should_skip_unchanged_snapshot() is True, 'gap_h=0 关闭兜底 → 照跳')

    print()
    print("结果：{} 通过 / {} 失败".format(len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  - " + n)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
