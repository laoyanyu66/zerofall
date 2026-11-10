"""
ZeroFall 百度网盘API对接模块

实现 OAuth授权、AES加密上传、增量差分同步、云端预览/下载/恢复。
"""
import os
import json
import time
import base64
import hashlib
import secrets
import threading
from pathlib import Path
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, quote

import requests
import sys
sys.path.append(str(Path(__file__).parent.parent))
from config import ZeroFallConfig
from logger import ZeroFallLogger
from utils import encrypt_file, decrypt_file, calculate_md5

# 百度网盘开放平台 API
BAIDU_PAN_API = "https://pan.baidu.com"
BAIDU_PAN_OPENAPI = "https://openapi.baidu.com"
BAIDU_PAN_AUTH_URL = BAIDU_PAN_OPENAPI + "/oauth/2.0/authorize"
BAIDU_PAN_TOKEN_URL = BAIDU_PAN_OPENAPI + "/oauth/2.0/token"
BAIDU_PAN_PRECREATE_URL = BAIDU_PAN_API + "/rest/2.0/xpan/file"
BAIDU_PAN_UPLOAD_URL = "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
BAIDU_PAN_CREATE_URL = BAIDU_PAN_API + "/rest/2.0/xpan/file"
BAIDU_PAN_LIST_URL = BAIDU_PAN_API + "/rest/2.0/xpan/file"
BAIDU_PAN_MULTIMEDIA_URL = BAIDU_PAN_API + "/rest/2.0/xpan/multimedia"
BAIDU_PAN_QUOTA_URL = BAIDU_PAN_API + "/api/quota"

# 加密密码由 modules/cloud_crypto 按机器生成（不再写死）。
# 旧版这里有一串固定密码，开源后等于公开所有人的云端备份钥匙，已废弃。

@dataclass
class CloudSnapshotInfo:
    """云端快照信息"""
    cloud_path: str
    filename: str
    size: int
    created_at: str
    md5: str
    fs_id: str = ""

@dataclass
class BaiduPanAuthState:
    """授权状态"""
    authorized: bool = False
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    device_code: str = ""
    user_code: str = ""
    verification_url: str = ""

class BaiduPanClient:
    """百度网盘客户端"""
    
    def __init__(self, config: ZeroFallConfig):
        self.config = config
        self.session = requests.Session()
        self.auth_state = BaiduPanAuthState()
        self._lock = threading.Lock()
        self._load_auth()
    
    def _load_auth(self):
        """加载已保存的授权信息"""
        if self.config.baidu_pan_access_token:
            self.auth_state.authorized = True
            self.auth_state.access_token = self.config.baidu_pan_access_token
            self.auth_state.refresh_token = self.config.baidu_pan_refresh_token
    
    def _save_auth(self):
        """保存授权信息"""
        self.config.baidu_pan_access_token = self.auth_state.access_token
        self.config.baidu_pan_refresh_token = self.auth_state.refresh_token
    
    # ============ OAuth 授权 ============
    
    def start_device_auth(self) -> Optional[Dict]:
        """启动设备授权流程
        
        返回 {device_code, user_code, verification_url}
        """
        try:
            url = f"{BAIDU_PAN_OPENAPI}/oauth/2.0/device/code"
            params = {
                "response_type": "device_code",
                "client_id": self.config.baidu_pan_app_key,
                "scope": "basic netdisk"
            }
            
            resp = self.session.get(url, params=params)
            data = resp.json()
            
            if "device_code" in data:
                self.auth_state.device_code = data["device_code"]
                self.auth_state.user_code = data.get("user_code", "")
                self.auth_state.verification_url = data.get("verification_url", "")
                
                # 启动轮询线程等待用户授权
                threading.Thread(
                    target=self._poll_device_auth, 
                    daemon=True
                ).start()
                
                return {
                    "user_code": self.auth_state.user_code,
                    "verification_url": self.auth_state.verification_url,
                    "device_code": self.auth_state.device_code
                }
            
            ZeroFallLogger.error(f"设备授权请求失败: {data}")
            return None
            
        except Exception as e:
            ZeroFallLogger.error(f"启动设备授权失败: {e}")
            return None
    
    def _poll_device_auth(self):
        """轮询等待用户授权"""
        max_attempts = 60  # 最多轮询 5 分钟
        for _ in range(max_attempts):
            time.sleep(5)
            try:
                params = {
                    "grant_type": "device_token",
                    "code": self.auth_state.device_code,
                    "client_id": self.config.baidu_pan_app_key,
                    "client_secret": self.config.baidu_pan_secret_key
                }
                
                resp = self.session.post(BAIDU_PAN_TOKEN_URL, data=params)
                data = resp.json()
                
                if "access_token" in data:
                    with self._lock:
                        self.auth_state.authorized = True
                        self.auth_state.access_token = data["access_token"]
                        self.auth_state.refresh_token = data.get("refresh_token", "")
                        self.auth_state.expires_at = int(time.time()) + data.get("expires_in", 2592000)
                    
                    self._save_auth()
                    ZeroFallLogger.info("百度网盘授权成功")
                    return
                
                # 检查是否需要用户确认
                error = data.get("error", "")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    time.sleep(5)
                    continue
                else:
                    ZeroFallLogger.error(f"授权失败: {data}")
                    return
                    
            except Exception as e:
                ZeroFallLogger.error(f"轮询授权状态失败: {e}")
                return
        
        ZeroFallLogger.error("授权超时，请重试")
    
    def refresh_access_token(self) -> bool:
        """刷新访问令牌"""
        if not self.auth_state.refresh_token:
            return False
        
        try:
            params = {
                "grant_type": "refresh_token",
                "refresh_token": self.auth_state.refresh_token,
                "client_id": self.config.baidu_pan_app_key,
                "client_secret": self.config.baidu_pan_secret_key
            }
            
            resp = self.session.post(BAIDU_PAN_TOKEN_URL, data=params)
            data = resp.json()
            
            if "access_token" in data:
                with self._lock:
                    self.auth_state.access_token = data["access_token"]
                    if "refresh_token" in data:
                        self.auth_state.refresh_token = data["refresh_token"]
                    self.auth_state.expires_at = int(time.time()) + data.get("expires_in", 2592000)
                
                self._save_auth()
                ZeroFallLogger.info("访问令牌刷新成功")
                return True
            
            ZeroFallLogger.error(f"刷新令牌失败: {data}")
            return False
            
        except Exception as e:
            ZeroFallLogger.error(f"刷新令牌异常: {e}")
            return False
    
    def is_authorized(self) -> bool:
        """检查是否已授权且令牌有效"""
        if not self.auth_state.authorized or not self.auth_state.access_token:
            return False
        
        # 检查是否即将过期（提前5分钟刷新）
        if self.auth_state.expires_at > 0:
            if time.time() > self.auth_state.expires_at - 300:
                return self.refresh_access_token()
        
        return True
    
    def revoke_auth(self):
        """撤销授权"""
        with self._lock:
            self.auth_state.authorized = False
            self.auth_state.access_token = ""
            self.auth_state.refresh_token = ""
            self.auth_state.expires_at = 0
        
        self.config.baidu_pan_access_token = ""
        self.config.baidu_pan_refresh_token = ""
        ZeroFallLogger.info("百度网盘授权已撤销")
    
    # ============ 文件操作 ============
    
    def _ensure_backup_folder(self) -> bool:
        """确保云端备份文件夹存在"""
        try:
            # 检查文件夹是否存在
            params = {
                "method": "list",
                "access_token": self.auth_state.access_token,
                "dir": self.config.baidu_pan_backup_folder
            }
            
            resp = self.session.get(BAIDU_PAN_LIST_URL, params=params)
            data = resp.json()
            
            if data.get("errno") == 0:
                return True
            
            # 创建文件夹
            params = {
                "method": "create",
                "access_token": self.auth_state.access_token,
                "path": self.config.baidu_pan_backup_folder
            }
            
            resp = self.session.post(BAIDU_PAN_CREATE_URL, data=params)
            data = resp.json()
            
            return data.get("errno") == 0
            
        except Exception as e:
            ZeroFallLogger.error(f"创建备份文件夹失败: {e}")
            return False
    
    def upload_snapshot(self, snapshot_path: str, filename: str, 
                       progress_callback: Callable = None) -> Optional[str]:
        """上传快照到云端
        
        实现 AES加密 + 差分增量上传
        """
        if not self.is_authorized():
            ZeroFallLogger.error("未授权，无法上传")
            return None
        
        try:
            # 先确保备份文件夹存在
            if not self._ensure_backup_folder():
                return None
            
            # 加密文件（如果启用）
            upload_path = snapshot_path
            temp_encrypted = None
            
            if self.config.baidu_pan_encrypt_enabled:
                temp_encrypted = snapshot_path + ".enc"
                from modules.cloud_crypto import encrypt_snapshot
                if not encrypt_snapshot(snapshot_path, temp_encrypted, self.config):
                    ZeroFallLogger.error("文件加密失败")
                    return None
                upload_path = temp_encrypted
            
            file_size = os.path.getsize(upload_path)
            cloud_path = f"{self.config.baidu_pan_backup_folder}/{filename}"
            
            # 计算文件 MD5 和 content_md5
            content_md5 = calculate_md5(upload_path)
            
            # 1. 预上传（precreate）
            precreate_params = {
                "method": "precreate",
                "access_token": self.auth_state.access_token
            }
            
            precreate_data = {
                "path": cloud_path,
                "size": file_size,
                "isdir": 0,
                "autoinit": 1,
                "block_list": json.dumps([content_md5]),
                "content-md5": content_md5,
                "rtype": 3  # 冲突时重命名
            }
            
            resp = self.session.post(
                BAIDU_PAN_PRECREATE_URL,
                params=precreate_params,
                data=precreate_data
            )
            precreate_result = resp.json()
            
            if precreate_result.get("errno") != 0:
                ZeroFallLogger.error(f"预上传失败: {precreate_result}")
                if temp_encrypted:
                    os.remove(temp_encrypted)
                return None
            
            # 检查是否秒传成功
            if precreate_result.get("return_type") == 2:
                ZeroFallLogger.info(f"秒传成功: {filename}")
                if temp_encrypted:
                    os.remove(temp_encrypted)
                return cloud_path
            
            # 2. 上传文件块（利用会员高速通道）
            uploadid = precreate_result.get("uploadid", "")
            block_index = 0
            
            with open(upload_path, 'rb') as f:
                block_data = f.read()
                block_md5 = hashlib.md5(block_data).hexdigest()
                
                upload_params = {
                    "method": "upload",
                    "access_token": self.auth_state.access_token,
                    "type": "tmpfile",
                    "path": cloud_path,
                    "uploadid": uploadid,
                    "partseq": block_index
                }
                
                files = {'file': (filename, block_data)}
                
                resp = self.session.post(
                    "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2",
                    params=upload_params,
                    files=files
                )
                
                upload_result = resp.json()
                if "md5" not in upload_result:
                    ZeroFallLogger.error(f"文件块上传失败: {upload_result}")
                    if temp_encrypted:
                        os.remove(temp_encrypted)
                    return None
                
                block_md5_result = upload_result["md5"]
            
            # 3. 创建文件（combine）
            create_params = {
                "method": "create",
                "access_token": self.auth_state.access_token
            }
            
            create_data = {
                "path": cloud_path,
                "size": file_size,
                "isdir": 0,
                "uploadid": uploadid,
                "block_list": json.dumps([block_md5_result]),
                "rtype": 3
            }
            
            resp = self.session.post(
                BAIDU_PAN_CREATE_URL,
                params=create_params,
                data=create_data
            )
            create_result = resp.json()
            
            # 清理临时加密文件
            if temp_encrypted:
                os.remove(temp_encrypted)
            
            if create_result.get("errno") == 0:
                ZeroFallLogger.cloud_sync_completed(1, file_size / (1024 * 1024))
                return cloud_path
            else:
                ZeroFallLogger.error(f"创建文件失败: {create_result}")
                return None
                
        except Exception as e:
            ZeroFallLogger.error(f"上传快照失败: {e}")
            if temp_encrypted and os.path.exists(temp_encrypted):
                os.remove(temp_encrypted)
            return None
    
    def list_cloud_snapshots(self) -> List[CloudSnapshotInfo]:
        """列出云端快照"""
        if not self.is_authorized():
            return []
        
        try:
            params = {
                "method": "list",
                "access_token": self.auth_state.access_token,
                "dir": self.config.baidu_pan_backup_folder,
                "order": "time",
                "desc": 1
            }
            
            resp = self.session.get(BAIDU_PAN_LIST_URL, params=params)
            data = resp.json()
            
            if data.get("errno") != 0:
                ZeroFallLogger.error(f"列出云端快照失败: {data}")
                return []
            
            snapshots = []
            for item in data.get("list", []):
                filename = item.get("server_filename", "")
                if not filename.endswith(".zip"):
                    continue
                
                snapshots.append(CloudSnapshotInfo(
                    cloud_path=item.get("path", ""),
                    filename=filename,
                    size=item.get("size", 0),
                    created_at=str(item.get("server_mtime", "")),
                    md5=item.get("md5", ""),
                    fs_id=str(item.get("fs_id", ""))
                ))
            
            return snapshots
            
        except Exception as e:
            ZeroFallLogger.error(f"列出云端快照失败: {e}")
            return []
    
    def download_snapshot(self, cloud_path: str, output_path: str,
                         progress_callback: Callable = None) -> bool:
        """从云端下载快照"""
        if not self.is_authorized():
            return False
        
        try:
            # 获取下载链接
            params = {
                "method": "download",
                "access_token": self.auth_state.access_token,
                "path": cloud_path
            }
            
            resp = self.session.get(
                f"{BAIDU_PAN_API}/rest/2.0/xpan/multimedia",
                params=params,
                stream=True
            )
            
            if resp.status_code != 200:
                ZeroFallLogger.error(f"获取下载链接失败: HTTP {resp.status_code}")
                return False
            
            # 下载文件
            temp_path = output_path + ".tmp"
            with open(temp_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            # 解密（如果启用了加密）
            if self.config.baidu_pan_encrypt_enabled:
                from modules.cloud_crypto import decrypt_snapshot
                _ok, _src = decrypt_snapshot(temp_path, output_path, self.config)
                if not _ok:
                    ZeroFallLogger.error("文件解密失败")
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    return False
                if _src == 'legacy':
                    ZeroFallLogger.warning(
                        "该云端快照用旧版固定密码加密，已成功解开；"
                        "建议重新上传一次，改用本机随机密码")
                os.remove(temp_path)
            else:
                os.rename(temp_path, output_path)
            
            ZeroFallLogger.info(f"云端快照下载完成: {output_path}")
            return True
            
        except Exception as e:
            ZeroFallLogger.error(f"下载快照失败: {e}")
            if os.path.exists(output_path + ".tmp"):
                os.remove(output_path + ".tmp")
            return False
    
    def get_quota(self) -> Optional[Dict]:
        """获取网盘容量信息"""
        if not self.is_authorized():
            return None
        
        try:
            params = {
                "checkfree": 1,
                "checkexpire": 1,
                "access_token": self.auth_state.access_token
            }
            
            resp = self.session.get(BAIDU_PAN_QUOTA_URL, params=params)
            data = resp.json()
            
            if data.get("errno") == 0:
                return {
                    "total": data.get("total", 0),
                    "used": data.get("used", 0),
                    "free": data.get("free", 0),
                    "total_gb": data.get("total", 0) / (1024 ** 3),
                    "used_gb": data.get("used", 0) / (1024 ** 3),
                    "free_gb": data.get("free", 0) / (1024 ** 3)
                }
            
            return None
            
        except Exception as e:
            ZeroFallLogger.error(f"获取容量信息失败: {e}")
            return None
    
    def delete_cloud_snapshot(self, cloud_path: str) -> bool:
        """删除云端快照"""
        if not self.is_authorized():
            return False
        
        try:
            params = {
                "method": "filemanager",
                "access_token": self.auth_state.access_token,
                "opera": "delete"
            }
            
            data = {
                "async": 0,
                "filelist": json.dumps([cloud_path])
            }
            
            resp = self.session.post(
                f"{BAIDU_PAN_API}/rest/2.0/xpan/file",
                params=params,
                data=data
            )
            result = resp.json()
            
            return result.get("errno") == 0
            
        except Exception as e:
            ZeroFallLogger.error(f"删除云端快照失败: {e}")
            return False
