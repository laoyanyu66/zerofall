"""
ZeroFall 工具函数模块
"""
import hashlib
import os
import shutil
import json
import zipfile
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

def encrypt_file(input_path: str, output_path: str, password: str) -> bool:
    """加密文件"""
    try:
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
    """解密文件"""
    try:
        with open(input_path, 'rb') as f:
            salt = f.read(16)
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
    """获取目录大小（MB）"""
    total_size = 0
    dir_path = Path(dir_path)
    
    if not dir_path.exists():
        return 0.0
    
    for file_path in dir_path.rglob("*"):
        if file_path.is_file():
            total_size += file_path.stat().st_size
    
    return total_size / (1024 * 1024)

def copy_directory(src: str, dst: str, ignore_patterns: List[str] = None) -> bool:
    """复制目录"""
    try:
        if ignore_patterns:
            ignore = shutil.ignore_patterns(*ignore_patterns)
        else:
            ignore = None
        
        shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
        return True
    except Exception as e:
        return False

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
    """
    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(source_dir)
                    method = (zipfile.ZIP_STORED if _is_already_compressed(file_path)
                              else zipfile.ZIP_DEFLATED)
                    zipf.write(file_path, arcname, compress_type=method)
        return True
    except Exception as e:
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
