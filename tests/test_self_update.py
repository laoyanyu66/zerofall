# -*- coding: utf-8 -*-
"""本地更新通道（standalone 自更新）

验收点：
  A. 只认比当前 exe 新的包（旧的/同期的都跳过）
  B. 校验不通过的包跳过（sha256 对不上）
  C. 多个包挑最新的那个
  D. 应用过（记账）的包不再重复返回
  E. 没有 exe 的源码包不用于换版
"""
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.updater import UpdatePackage  # noqa: E402
from modules.self_update import (  # noqa: E402
    find_new_package, load_state, mark_applied,
)

PASS = 0
FAIL = 0


def chk(ok, name, detail=''):
    global PASS, FAIL
    if ok:
        PASS += 1
        print('  [OK]   {}'.format(name))
    else:
        FAIL += 1
        print('  [FAIL] {}  {}'.format(name, detail))


def retag(zip_path, created_at):
    """改写包内 manifest 的 created_at（测试用，模拟不同构建时间）"""
    with zipfile.ZipFile(zip_path) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    m = json.loads(items['manifest.json'].decode('utf-8'))
    m['created_at'] = created_at
    items['manifest.json'] = json.dumps(m, ensure_ascii=False).encode('utf-8')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for n, d in items.items():
            zf.writestr(n, d)


def make_pkg(out_dir, name, exe_bytes, created_at=None, version='20260101000000'):
    p = out_dir / name
    UpdatePackage.build(p, exe_path=None, version=version)
    # 塞个假 exe 进去（build 只有在真 exe 存在时才写 exe 段，这里手动补）
    with zipfile.ZipFile(p) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    m = json.loads(items['manifest.json'].decode('utf-8'))
    import hashlib
    m['exe'] = {'name': 'ZeroFall.exe', 'size': len(exe_bytes),
                'sha256': hashlib.sha256(exe_bytes).hexdigest()}
    if created_at:
        m['created_at'] = created_at
    items['manifest.json'] = json.dumps(m, ensure_ascii=False).encode('utf-8')
    items['ZeroFall.exe'] = exe_bytes
    p.unlink()
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as zf:
        for n, d in items.items():
            zf.writestr(n, d)
    return p


def main():
    root = Path(tempfile.mkdtemp(prefix='zf_su_'))
    upd = root / 'updates'
    upd.mkdir()

    # 当前 exe：mtime 定在 2026-11-14 01:00
    cur = root / 'ZeroFall.exe'
    cur.write_bytes(b'OLD-EXE' * 100)
    now = time.mktime(time.strptime('2026-11-14 01:00:00', '%Y-%m-%d %H:%M:%S'))
    os.utime(cur, (now, now))

    print('A. 旧包跳过 / 新包命中')
    old = make_pkg(upd, 'old.zip', b'NEW-EXE-A' * 100,
                   created_at='2026-11-13 22:00:00')
    chk(find_new_package(upd, cur) is None, '比当前 exe 旧的包不返回')
    old.unlink()

    new = make_pkg(upd, 'new.zip', b'NEW-EXE-B' * 100,
                   created_at='2026-11-14 01:30:00')
    hit = find_new_package(upd, cur)
    chk(hit is not None and hit[0].name == 'new.zip', '比当前 exe 新的包命中',
        hit[0].name if hit else None)

    print('B. 坏包跳过')
    bad = upd / 'bad.zip'
    shutil.copy2(new, bad)
    with zipfile.ZipFile(bad) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    items['ZeroFall.exe'] = b'TAMPERED' * 100
    with zipfile.ZipFile(bad, 'w', zipfile.ZIP_DEFLATED) as zf:
        for n, d in items.items():
            zf.writestr(n, d)
    hit = find_new_package(upd, cur)
    chk(hit is not None and hit[0].name == 'new.zip', '校验不过的包被跳过',
        hit[0].name if hit else None)
    bad.unlink()

    print('C. 多个包挑最新')
    newer = make_pkg(upd, 'newer.zip', b'NEW-EXE-C' * 100,
                     created_at='2026-11-14 02:00:00')
    hit = find_new_package(upd, cur)
    chk(hit is not None and hit[0].name == 'newer.zip', '挑 created_at 最新的',
        hit[0].name if hit else None)

    print('D. 记账后不再重复返回')
    mark_applied(upd, newer, {'version': '20261114020000'})
    st = load_state(upd)
    chk(bool(st.get('applied')), 'applied 里记下了 sha256')
    hit = find_new_package(upd, cur)
    chk(hit is not None and hit[0].name == 'new.zip', '装过的包被排除',
        hit[0].name if hit else None)

    print('E. 没有 exe 的源码包不用于换版')
    srcpkg = upd / 'source-only.zip'
    UpdatePackage.build(srcpkg, exe_path=None, version='20270101000000')
    retag(srcpkg, '2027-01-01 00:00:00')
    hit = find_new_package(upd, cur)
    chk(hit is not None and hit[0].name == 'new.zip', '纯源码包不当作换版包',
        hit[0].name if hit else None)

    print('F. 目录不存在 / 空目录')
    chk(find_new_package(root / 'nope', cur) is None, '目录不存在返回 None')

    shutil.rmtree(root, ignore_errors=True)
    print('\n结果：{} 通过 / {} 失败'.format(PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
