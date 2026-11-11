"""
ZeroFall 工具函数模块
"""
import hashlib
import os
import shutil
import json
import zipfile
import fnmatch
import stat
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

def calculate_md5(file_path: str) -> str:
    """计算文件MD5值"""
    md5_hash = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    except Exception as e:
        return ""

def calculate_directory_md5(dir_path: str, exclude_dirs: Optional[set] = None,
                            exclude_files: Optional[list] = None,
                            max_files: int = 20000) -> Dict[str, str]:
    """计算目录下所有文件的MD5值
    
    Args:
        exclude_dirs:  要跳过的目录名集合（运行时/缓存等，避免扫描上万文件）
        exclude_files: 要跳过的文件名模式列表（如 '*.log'），支持通配符
        max_files:     最多计算的文件数上限，防止极端情况卡死
    """
    import fnmatch
    
    md5_dict: Dict[str, str] = {}
    dir_path = Path(dir_path)
    
    if not dir_path.exists():
        return md5_dict
    
    skip_dirs = {d.lower() for d in (exclude_dirs or set())}
    patterns = list(exclude_files or [])
    count = 0
    
    for root, dirs, files in os.walk(dir_path):
        # 就地过滤目录，避免进入运行时/缓存等大目录
        if skip_dirs:
            dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        
        for name in files:
            if count >= max_files:
                return md5_dict
            # 文件级排除（日志、运行时状态等易变文件）
            if patterns and any(fnmatch.fnmatch(name, p) for p in patterns):
                continue
            fp = Path(root) / name
            try:
                rel_path = fp.relative_to(dir_path)
                md5_dict[str(rel_path)] = calculate_md5(str(fp))
                count += 1
            except Exception:
                continue
    
    return md5_dict

def generate_key(password: str, salt: bytes) -> bytes:
    """从密码生成加密密钥"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


# ── 分块加密格式 ──
# Fernet 是整体加密（AES-CBC + HMAC），必须把整个文件读进内存，内部还要
# 再做 CBC 输出、base64 编码（+33%），峰值内存约为文件大小的 5 倍。
# 实测 194 MB 的快照在 4.8 GB 可用内存下直接 MemoryError，意味着
# 大完整快照永远无法加密归档，只能留在本地 —— 磁盘会一直涨。
#
# 这里定义分块格式，超过阈值的文件按 8 MB 一块独立加密：
#   MAGIC(8B) + CHUNK_SIZE(4B LE) + CHUNK_COUNT(4B LE) + 每块[ salt(16B) + len(4B LE) + 密文 ]
# 每块独立 salt/Fernet，内存占用恒定为一块的量级。
#
# 兼容：旧文件没有 MAGIC，首 16 字节直接是 salt，按老路整体解密。
_CHUNK_MAGIC = b'ZFCHUNK1'
_CHUNK_SIZE = 8 * 1024 * 1024        # 单块 8 MB → 峰值内存约 40 MB
_CHUNK_THRESHOLD = 64 * 1024 * 1024  # 超过 64 MB 自动分块


def _encrypt_chunked(input_path: str, output_path: str, password: str) -> bool:
    import struct
    try:
        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            fout.write(_CHUNK_MAGIC)
            fout.write(struct.pack('<I', _CHUNK_SIZE))
            count_pos = fout.tell()
            fout.write(b'\x00\x00\x00\x00')      # 块数占位，最后回填
            n = 0
            while True:
                chunk = fin.read(_CHUNK_SIZE)
                if not chunk:
                    break
                salt = os.urandom(16)
                ct = Fernet(generate_key(password, salt)).encrypt(chunk)
                fout.write(salt)
                fout.write(struct.pack('<I', len(ct)))
                fout.write(ct)
                n += 1
                del chunk, ct                    # 及时释放，别等循环下一轮
            fout.seek(count_pos)
            fout.write(struct.pack('<I', n))
        return True
    except Exception:
        try:
            if os.path.exists(output_path):
                os.unlink(output_path)       # 半成品不能留，否则下次误判"已存在"
        except Exception:
            pass
        return False


def _decrypt_chunked(input_path: str, output_path: str, password: str) -> bool:
    import struct
    try:
        with open(input_path, 'rb') as fin, open(output_path, 'wb') as fout:
            fin.read(8)                          # MAGIC
            fin.read(4)                          # chunk size（当前未使用，保留）
            count = struct.unpack('<I', fin.read(4))[0]
            for _ in range(count):
                salt = fin.read(16)
                ln = struct.unpack('<I', fin.read(4))[0]
                ct = fin.read(ln)
                fout.write(Fernet(generate_key(password, salt)).decrypt(ct))
        return True
    except Exception:
        try:
            if os.path.exists(output_path):
                os.unlink(output_path)
        except Exception:
            pass
        return False


def encrypt_file(input_path: str, output_path: str, password: str,
                 chunked: bool = None) -> bool:
    """加密文件

    chunked=None 时按大小自动选择：超过 64 MB 走分块流式，
    小文件保持原来的整体 Fernet（格式不变，老文件照常可解）。
    """
    try:
        if chunked is None:
            chunked = os.path.getsize(input_path) > _CHUNK_THRESHOLD
        if chunked:
            return _encrypt_chunked(input_path, output_path, password)

        salt = os.urandom(16)
        key = generate_key(password, salt)
        fernet = Fernet(key)
        
        with open(input_path, 'rb') as f:
            data = f.read()
        
        encrypted_data = fernet.encrypt(data)
        
        with open(output_path, 'wb') as f:
            f.write(salt)
            f.write(encrypted_data)
        
        return True
    except Exception as e:
        return False

def decrypt_file(input_path: str, output_path: str, password: str) -> bool:
    """解密文件，自动识别分块格式与旧的整体 Fernet 格式"""
    try:
        # 一次 open 读完，不要中途重新 open —— 重新 open 会把指针复位，
        # 导致 f.read(8) 又读到开头那 8 字节，salt 就重复了。
        with open(input_path, 'rb') as f:
            head = f.read(8)
            if head == _CHUNK_MAGIC:
                f.close()
                return _decrypt_chunked(input_path, output_path, password)
            # 旧格式：开头 16 字节就是 salt，整体一个 Fernet
            salt = head + f.read(8)          # 补足 16 字节
            encrypted_data = f.read()
        
        key = generate_key(password, salt)
        fernet = Fernet(key)
        decrypted_data = fernet.decrypt(encrypted_data)
        
        with open(output_path, 'wb') as f:
            f.write(decrypted_data)
        
        return True
    except Exception as e:
        return False

def get_directory_size(dir_path: str) -> float:
    """获取目录大小（MB）

    逐项统计并跳过读不了的条目：目录里只要有一个重解析点（junction/OneDrive）
    或 ACL 拒绝的目录，rglob 就会整体抛 PermissionError，调用方拿不到大小
    还算好的——快照流程里这一抛等于整次快照报废。
    """
    total_size = 0
    dir_path = Path(dir_path)

    if not dir_path.exists():
        return 0.0

    def _onerror(_err):
        return

    for root, dirs, files in os.walk(dir_path, onerror=_onerror):
        # 重解析点（junction/符号链接）不进，否则会去统计别人家的整个目录
        dirs[:] = [d for d in dirs if not _is_reparse_point(Path(root) / d)]
        for name in files:
            try:
                total_size += (Path(root) / name).stat().st_size
            except OSError:
                continue

    return total_size / (1024 * 1024)


def _is_reparse_point(path: Path) -> bool:
    """判断是否是重解析点（junction / 符号链接 / OneDrive 占位目录）

    为什么必须判：Windows 上 junction 对 os.walk 就是个普通目录，跟进去会
    复制/统计一整棵别人的目录；读不动的时候直接 WinError 5。
    实测快照失败就是栽在 DSH 会话目录里一个 %userprofile%\\OneDrive 上。
    """
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), 'st_file_attributes', 0)
        return bool(attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))
    except OSError:
        # 连 stat 都失败（ACL 拒绝）就当它不可用，跳过
        return True


def copy_directory(src: str, dst: str, ignore_patterns: List[str] = None) -> bool:
    """复制目录（逐项容错）

    不用 shutil.copytree 的原因：copytree 碰到一个读不了的条目就整体抛异常，
    调用方只拿到一份残缺目录 + 一句"失败"，而快照流程里这一抛等于整次快照
    丢掉（实测：[WinError 5] 拒绝访问 ...\\sessions\\%userprofile%\\OneDrive，
    当天的每日完整快照就这么没了）。

    现在的语义：
      * 重解析点（junction/符号链接/OneDrive 占位）整棵跳过，不跟进去
      * 单个文件复制失败（被占用/ACL 拒绝/正在轮转）只跳过它，其余照常
      * 目录无法遍历时跳过该子树
    返回 True 表示"复制流程跑完"，不代表逐字节完整。
    """
    try:
        src_p = Path(src)
        dst_p = Path(dst)
        if not src_p.is_dir():
            return False
        dst_p.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    pats = list(ignore_patterns or [])

    def _ignored(name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in pats)

    def _onerror(_err):
        return

    for root, dirs, files in os.walk(src_p, onerror=_onerror):
        root_p = Path(root)
        rel = root_p.relative_to(src_p)
        target_root = dst_p / rel if str(rel) != '.' else dst_p
        # 剪掉不该跟的目录：忽略规则 + 重解析点
        dirs[:] = [d for d in dirs
                   if not _ignored(d) and not _is_reparse_point(root_p / d)]
        try:
            target_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            dirs[:] = []
            continue
        for name in files:
            if _ignored(name):
                continue
            s = root_p / name
            if _is_reparse_point(s):
                continue
            try:
                shutil.copy2(str(s), str(target_root / name))
            except OSError:
                continue
    return True

# 这些扩展名的内容本身已经是压缩/加密过的（熵很高），再 DEFLATE 几乎不缩小，
# 只是白烧 CPU。实测一个完整快照里 .zstd 占 26.7%、.tgz/.png/.whl/.enc 再 4%，
# 合计约三成数据。对它们直接用 ZIP_STORED（原样存储），快照大小基本不变，
# 创建耗时明显下降。
# 注意 excludes 里不要放 .exe/.dll/.pyd/.node —— 那些 PE/ELF 还有可观压缩空间
# （实测整体压缩率约 45%），压它们是值得的。
_ALREADY_COMPRESSED_EXTS = {
    '.zip', '.gz', '.tgz', '.bz2', '.xz', '.7z', '.zst', '.zstd', '.br', '.rar', '.lz4', '.whl',
    '.enc',                                    # 本项目加密后的快照，等于随机数据
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif',
    '.mp3', '.mp4', '.m4a', '.avi', '.mov', '.mkv', '.webm',
    '.woff', '.woff2', '.otf',
}


def _is_already_compressed(path) -> bool:
    return Path(path).suffix.lower() in _ALREADY_COMPRESSED_EXTS


def create_archive(source_dir: str, output_path: str, compression: str = "zip") -> bool:
    """创建压缩包

    逐文件选压缩方式：已压缩格式原样存储，其余 DEFLATE。

    逐文件也逐个兜异常：打包时文件被删/被占用/ACL 拒绝都只跳过那一个，
    不让一个读不了的残留文件把整次快照打成失败。
    """
    skipped = 0

    def _onerror(_err):
        return

    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir, onerror=_onerror):
                dirs[:] = [d for d in dirs if not _is_reparse_point(Path(root) / d)]
                for file in files:
                    file_path = Path(root) / file
                    try:
                        arcname = file_path.relative_to(source_dir)
                        method = (zipfile.ZIP_STORED if _is_already_compressed(file_path)
                                  else zipfile.ZIP_DEFLATED)
                        zipf.write(file_path, arcname, compress_type=method)
                    except (OSError, ValueError):
                        skipped += 1
                        continue
        return True
    except Exception:
        return False

def extract_archive(archive_path: str, output_dir: str) -> bool:
    """解压压缩包"""
    try:
        with zipfile.ZipFile(archive_path, 'r') as zipf:
            zipf.extractall(output_dir)
        return True
    except Exception as e:
        return False

def compare_directories(dir1: str, dir2: str) -> Dict[str, List[str]]:
    """比较两个目录的差异"""
    result = {
        "added": [],
        "removed": [],
        "modified": []
    }
    
    dir1_path = Path(dir1)
    dir2_path = Path(dir2)
    
    # 获取所有文件的相对路径
    files1 = {str(f.relative_to(dir1_path)) for f in dir1_path.rglob("*") if f.is_file()}
    files2 = {str(f.relative_to(dir2_path)) for f in dir2_path.rglob("*") if f.is_file()}
    
    # 新增文件
    result["added"] = list(files2 - files1)
    
    # 删除文件
    result["removed"] = list(files1 - files2)
    
    # 修改文件
    common_files = files1 & files2
    for file in common_files:
        md5_1 = calculate_md5(str(dir1_path / file))
        md5_2 = calculate_md5(str(dir2_path / file))
        if md5_1 != md5_2:
            result["modified"].append(file)
    
    return result

def generate_snapshot_id() -> str:
    """生成快照ID"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"snapshot_{timestamp}"

def format_timestamp(timestamp: str) -> str:
    """格式化时间戳为可读格式"""
    try:
        dt = datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return timestamp

def is_directory_locked(dir_path: str) -> bool:
    """检查目录是否只读锁定"""
    try:
        test_file = Path(dir_path) / ".zerofall_lock_test"
        with open(test_file, 'w') as f:
            f.write("test")
        test_file.unlink()
        return False
    except PermissionError:
        return True
    except:
        return False

def set_directory_readonly(dir_path: str, readonly: bool) -> bool:
    """设置目录只读属性"""
    try:
        for root, dirs, files in os.walk(dir_path):
            for name in files:
                file_path = Path(root) / name
                if readonly:
                    os.chmod(file_path, 0o444)
                else:
                    os.chmod(file_path, 0o666)
        return True
    except:
        return False

def get_process_info(process_name: str) -> Optional[Dict[str, Any]]:
    """获取进程信息"""
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name', 'exe', 'status']):
            if proc.info['name'] and process_name.lower() in proc.info['name'].lower():
                return {
                    'pid': proc.info['pid'],
                    'name': proc.info['name'],
                    'exe': proc.info['exe'],
                    'status': proc.info['status']
                }
        return None
    except:
        return None

def kill_process(process_name: str) -> bool:
    """终止进程"""
    try:
        import psutil
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and process_name.lower() in proc.info['name'].lower():
                proc.kill()
        return True
    except:
        return False

# ── PyInstaller onefile 环境隔离 ─────────────────────────────────────────────
# 事故复盘（2026-11-11）：
#   ZeroFall 是 PyInstaller onefile 打包，运行中环境里带着
#   _PYI_PARENT_PROCESS_LEVEL / _PYI_ARCHIVE_FILE / _PYI_APPLICATION_HOME_DIR。
#   它启动 DSH 时这些变量被继承下去；再从那条链里启动 ZeroFall，新进程的
#   bootloader 会以为自己是"onefile 子进程"，去找一个早已退出的父 PID，弹：
#     Security validation failure: invalid originating onefile parent process (PID not found)!
#   修法：自己启动时清一遍，且给所有子进程传一份干净 env。

_PYI_ENV_KEYS = (
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_ARCHIVE_FILE",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_SPLASH_IPC",
    "_MEIPASS",
    "_MEIPASS2",
)


def sanitized_child_env() -> Dict[str, str]:
    """给子进程用的环境：去掉 PyInstaller onefile 的簿记变量"""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("_PYI_") or k in _PYI_ENV_KEYS:
            env.pop(k, None)
    return env


def strip_pyinstaller_env() -> list:
    """清掉**当前进程**环境里的 PyInstaller 簿记变量，返回被删掉的键名

    必须在启动任何子进程之前调用：否则 DSH 会继承这些变量，
    再从那条链启动 ZeroFall 就会撞上 "invalid originating onefile parent process"。
    """
    removed = []
    for k in list(os.environ):
        if k.startswith("_PYI_") or k in _PYI_ENV_KEYS:
            os.environ.pop(k, None)
            removed.append(k)
    return removed


def config_int(obj, name: str, default: int = 0) -> int:
    """读配置里的整数，显式区分 None / 空串 / 0

    为什么不用 `int(getattr(cfg, name, d) or 0)`：那样 `0` 和 `None` 混在一起，
    "用户设 0 = 禁用"和"没设过"看起来一样。这里只有 None/空串才回落默认值。
    """
    val = getattr(obj, name, None)
    if val is None or (isinstance(val, str) and not val.strip()):
        return int(default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(default)
