# -*- coding: utf-8 -*-
"""ZeroFall 自持密钥库

为什么需要这个东西：
  ZeroFall 的自动修复要靠 ACP 子进程连大模型，认证来源一直只有两处 ——
  `~/.dsh/.credentials.yaml` 或进程环境变量。问题是前者由 DSH 自己管理，
  它随时可能改格式、轮换、清空（实测 2026-11-09 14:26 就被重写过一次，
  `refs:` 段整个消失，`OPENCODE_GO_API_KEY` 不再存在）。

  一旦凭据文件变了，ZeroFall 修不了子控的报错，而且要到跑完一轮 ACP
  （几分钟）才会因认证失败暴露出来 —— 这种"依赖别人可变状态"的设计太脆。

  所以这里做两件事：
    1. 发现可用的 key 就**落一份到自己的库里**（last-known-good）
    2. 凭据文件里找不到时，用自己库里的顶上

  文件权限：Windows 下放在 exe 同级目录，只有本机用户可读；
  值以明文保存（与 DSH 自身 .credentials.yaml 同级别，不引入额外依赖）。
"""
import json
import os
import sys
from pathlib import Path

_FILE_NAME = 'secrets.json'

# 需要长期跟进的 API key 名单（其它记录一律不碰）
KNOWN_KEYS = (
    'OPENCODE_GO_API_KEY',
    'OPENCODE_PERSONAL_API_KEY',
    'DEEPSEEK_API_KEY',
    'OPENAI_API_KEY',
)


def base_dir() -> Path:
    """密钥库所在目录：优先环境变量，其次 exe 同级，最后源码根"""
    v = os.environ.get('ZF_DATA_DIR')
    if v:
        try:
            p = Path(v)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            pass
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def path() -> Path:
    return base_dir() / _FILE_NAME


def _key_file_for(name: str) -> Path:
    """约定：与 exe 同级的 <KEY名>.txt 可直接放密钥

    为什么加这个：填密钥要么开界面点保存，要么改 JSON，对"就想丢个文件
    进去"的场景都不顺手。任何与 exe 同级的 `OPENCODE_GO_API_KEY.txt`
    （纯文本，首行就是 key）都会被自动认作密钥来源。
    """
    return base_dir() / '{}.txt'.format(name)


def _read_key_file(name: str) -> str:
    try:
        p = _key_file_for(name)
        if not p.is_file():
            return ''
        txt = p.read_text(encoding='utf-8', errors='replace').strip()
        # 取第一行非空、非注释内容
        for line in txt.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            return line
    except Exception:
        pass
    return ''


def load() -> dict:
    """读出全部已保存的密钥；文件损坏时返回空字典，绝不抛异常"""
    try:
        p = path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: dict) -> bool:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        os.replace(str(tmp), str(p))
        try:
            os.chmod(str(p), 0o600)      # 尽力收紧（Windows 上语义有限）
        except Exception:
            pass
        return True
    except Exception:
        return False


def get(name: str) -> str:
    v = load().get(name)
    if isinstance(v, str) and v.strip():
        return v.strip()
    # 外部文件兜底（<KEY名>.txt）
    return _read_key_file(name)


def put(name: str, value: str, only_if_newer: bool = False) -> bool:
    """写一条密钥

    only_if_newer=True 时，只有当前为空才写入 —— 用于"从凭据文件发现"的
    自动缓存，避免把用户手工填的更权威的值覆盖掉。
    """
    if not name or not isinstance(value, str) or not value:
        return False
    data = load()
    if only_if_newer and (data.get(name) or '').strip():
        return False
    data[name] = value
    return save(data)


def merge(found: dict) -> int:
    """把发现的一批 key 存进去（不覆盖已有的），返回新增条数"""
    if not found:
        return 0
    data = load()
    n = 0
    for k, v in found.items():
        if k in KNOWN_KEYS and isinstance(v, str) and v and not data.get(k):
            data[k] = v
            n += 1
    if n:
        save(data)
    return n


def clear(name: str = None) -> bool:
    data = load()
    if name:
        data.pop(name, None)
    else:
        data = {}
    return save(data)


def mask(value: str) -> str:
    """日志/界面里显示用的脱敏形式"""
    if not value:
        return '无'
    return '{}***({})'.format(value[:4], len(value))
