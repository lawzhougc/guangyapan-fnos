import json
import time
import os
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List, Tuple
from .hash_utils import calculate_gcid_cid, calculate_file_md5

class GuangyapanClient:
    API_BASE = "https://api.guangyapan.com"
    ACCOUNT_BASE = "https://account.guangyapan.com"
    CLIENT_ID = "aMe_SVSlkrbQXpUT"
    CLIENT_SECRET = "FNAfp5IFEfCn5MYsIUTewg"
    
    # Common headers
    USER_AGENT = "GuangShanYun/1.0.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.7339.41 Electron/38.0.0 Safari/537.36"

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.access_token = self.config.get("access_token", "")
        self.refresh_token = self.config.get("refresh_token", "")
        self.sub = self.config.get("sub", "")
        self.expires_at = self.config.get("expires_at", 0)

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print("Failed to save config:", e)

    def is_token_expired(self) -> bool:
        if not self.access_token:
            return True
        # If expires_at is in milliseconds
        exp = self.expires_at
        if exp > 10000000000:
            exp = exp / 1000.0
        # Refresh 5 minutes before actual expiry
        return time.time() >= (exp - 300)

    def ensure_valid_token(self):
        if self.is_token_expired() and self.refresh_token:
            self.refresh_access_token()

    def refresh_access_token(self) -> bool:
        url = f"{self.ACCOUNT_BASE}/v1/auth/token"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT
        }
        body = {
            "client_id": self.CLIENT_ID,
            "client_secret": self.CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.access_token = data.get("access_token", "")
                if "refresh_token" in data:
                    self.refresh_token = data.get("refresh_token")
                self.expires_at = time.time() + data.get("expires_in", 7200)
                
                self.config["access_token"] = self.access_token
                self.config["refresh_token"] = self.refresh_token
                self.config["expires_at"] = self.expires_at
                self._save_config()
                return True
        except Exception as e:
            print("Failed to refresh token:", e)
            return False

    def _get_headers(self) -> Dict[str, str]:
        self.ensure_valid_token()
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "av": "1.1.0",
            "vc": "1008",
            "dt": "5"
        }

    def _post(self, path: str, body: Dict[str, Any], retry_auth: bool = True) -> Dict[str, Any]:
        url = f"{self.API_BASE}{path}" if path.startswith("/") else path
        headers = self._get_headers()
        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res_bytes = resp.read().decode("utf-8")
                return json.loads(res_bytes)
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry_auth and self.refresh_token:
                if self.refresh_access_token():
                    return self._post(path, body, retry_auth=False)
            err_text = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {e.code} Error calling {path}: {err_text}")
        except Exception as e:
            raise RuntimeError(f"Error calling {path}: {e}")

    def _get(self, url: str) -> Dict[str, Any]:
        headers = self._get_headers()
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ================= Auth & User Session =================
    def create_device_authorization(self) -> Dict[str, Any]:
        """RFC 8628 OAuth 2.0 Device Authorization Grant (Official QR Login)."""
        url = f"{self.ACCOUNT_BASE}/v1/auth/device/code"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT
        }
        body = {
            "client_id": self.CLIENT_ID
        }
        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                return {"error": err_data.get("error_description") or err_data.get("error") or str(e)}
            except Exception:
                return {"error": f"HTTP {e.code}"}
        except Exception as e:
            return {"error": str(e)}

    def poll_device_token(self, device_code: str) -> Dict[str, Any]:
        """Poll OAuth token for device code."""
        url = f"{self.ACCOUNT_BASE}/v1/auth/token"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT
        }
        body = {
            "client_id": self.CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code
        }
        req = urllib.request.Request(url, headers=headers, data=json.dumps(body).encode("utf-8"))
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.access_token = data.get("access_token", "")
                if "refresh_token" in data:
                    self.refresh_token = data.get("refresh_token")
                self.sub = data.get("sub", "")
                self.expires_at = time.time() + data.get("expires_in", 7200)

                self.config["access_token"] = self.access_token
                self.config["refresh_token"] = self.refresh_token
                self.config["sub"] = self.sub
                self.config["expires_at"] = self.expires_at
                self._save_config()

                profile = {}
                try:
                    profile = self.get_user_profile()
                except Exception:
                    pass
                return {"status": "success", "user": profile}
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
                err_type = err_data.get("error", "")
                if err_type == "authorization_pending":
                    return {"status": "pending"}
                elif err_type == "slow_down":
                    return {"status": "slow_down"}
                elif err_type == "expired_token":
                    return {"status": "expired", "error": "二维码已过期，请点击刷新"}
                elif err_type == "access_denied":
                    return {"status": "denied", "error": "用户已取消或拒绝授权"}
                else:
                    return {"status": "failed", "error": err_data.get("error_description") or err_type}
            except Exception:
                return {"status": "failed", "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    def login_with_tokens(self, access_token: str, refresh_token: str = "") -> Dict[str, Any]:
        """Direct login via Access Token and Refresh Token."""
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.expires_at = time.time() + 7200
        self.config["access_token"] = self.access_token
        self.config["refresh_token"] = self.refresh_token
        self.config["expires_at"] = self.expires_at
        self._save_config()
        try:
            profile = self.get_user_profile()
            return {"status": "success", "user": profile}
        except Exception as e:
            return {"status": "failed", "error": f"Token 验证失败: {e}"}

    def logout(self) -> bool:
        """Sign out and clear session credentials."""
        self.access_token = ""
        self.refresh_token = ""
        self.sub = ""
        self.expires_at = 0
        self.config["access_token"] = ""
        self.config["refresh_token"] = ""
        self.config["sub"] = ""
        self.config["expires_at"] = 0
        self._save_config()
        return True

    # ================= User Information =================
    def get_user_profile(self) -> Dict[str, Any]:
        """Get current user account information."""
        if not self.access_token:
            return {"is_logged_in": False, "name": "未登录", "nickname": "未登录"}
        try:
            res = self._get(f"{self.ACCOUNT_BASE}/v1/user/me")
            if isinstance(res, dict):
                res["is_logged_in"] = True
            return res
        except Exception:
            return {"is_logged_in": False, "name": "未登录", "nickname": "未登录"}

    def get_user_assets(self) -> Dict[str, Any]:
        """Get user storage quota, VIP status, and assets."""
        if not self.access_token:
            return {"code": -1, "msg": "未登录", "data": {}}
        try:
            return self._post("/assets/v1/get_assets", {})
        except Exception:
            return {"code": -1, "msg": "获取资产失败", "data": {}}

    # ================= File & Folder Operations =================
    def list_files(self, parent_id: str = "", page: int = 0, page_size: int = 100, order_by: int = 3, sort_type: int = 1) -> Dict[str, Any]:
        """
        List files in parent_id.
        order_by: 0: NAME, 1: SIZE, 2: CTIME, 3: UTIME
        sort_type: 0: ASC, 1: DESC
        """
        body = {
            "parentId": parent_id or "",
            "page": page,
            "pageSize": page_size,
            "orderBy": order_by,
            "sortType": sort_type
        }
        res = self._post("/userres/v1/file/get_file_list", body)
        return res.get("data", {})

    def list_all_files(self, parent_id: str = "", page_size: int = 100, order_by: int = 3, sort_type: int = 1) -> List[Dict[str, Any]]:
        """
        Fetch all files and folders in parent_id across pages, with deduplication guard.
        """
        all_items: List[Dict[str, Any]] = []
        seen_ids = set()
        page = 0
        max_pages = 100
        while page < max_pages:
            data = self.list_files(parent_id=parent_id, page=page, page_size=page_size, order_by=order_by, sort_type=sort_type)
            items = data.get("list") or []
            if not items:
                break
            new_count = 0
            for item in items:
                fid = item.get("fileId")
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    all_items.append(item)
                    new_count += 1
            total = data.get("total") or data.get("totalCount") or 0
            if new_count == 0 or (total and len(all_items) >= total) or (not total and len(items) < page_size):
                break
            page += 1
        return all_items

    def create_folder(self, name: str, parent_id: str = "") -> Dict[str, Any]:
        folder_name = (name or "").strip()
        if not folder_name:
            return {"code": 400, "msg": "文件夹名称不能为空"}
        body = {
            "dirName": folder_name,
            "name": folder_name,
            "parentId": parent_id or ""
        }
        return self._post("/userres/v1/file/create_dir", body)

    def list_folder_children_map(self, parent_id: str = "") -> Tuple[Dict[str, str], Dict[str, int]]:
        """
        List all children under parent_id, returning:
        (folders_map: {folder_name: file_id}, files_map: {file_name: file_size_in_bytes})
        """
        folders_map: Dict[str, str] = {}
        files_map: Dict[str, int] = {}
        try:
            items = self.list_all_files(parent_id=parent_id)
            for item in items:
                fname = (item.get("name") or item.get("fileName") or "").strip()
                if not fname:
                    continue
                fid = item.get("fileId") or ""
                is_dir = (item.get("resType") == 2 or bool(item.get("isDir")))
                if is_dir:
                    folders_map[fname] = fid
                else:
                    size_val = item.get("size") if item.get("size") is not None else item.get("fileSize", 0)
                    try:
                        files_map[fname] = int(size_val)
                    except (ValueError, TypeError):
                        files_map[fname] = 0
        except Exception as e:
            print(f"[Client] list_folder_children_map failed for parent_id='{parent_id}': {e}")
        return folders_map, files_map

    def get_or_create_folder(self, name: str, parent_id: str = "", existing_folders: Optional[Dict[str, str]] = None) -> str:
        """
        Idempotent directory getter or creator.
        If the folder already exists under parent_id, returns its fileId.
        Otherwise creates it and returns its new fileId.
        """
        folder_name = (name or "").strip()
        if not folder_name:
            return parent_id or ""

        if existing_folders is not None:
            if folder_name in existing_folders:
                return existing_folders[folder_name]
        else:
            # Check existing cloud directory
            subfolders, _ = self.list_folder_children_map(parent_id=parent_id)
            if folder_name in subfolders:
                return subfolders[folder_name]

        # Not found, create it
        res = self.create_folder(folder_name, parent_id=parent_id)
        data = res.get("data")
        new_fid = data.get("fileId") if isinstance(data, dict) else (data if isinstance(data, str) else None)
        if not new_fid or res.get("code") != 0:
            # Re-list to check if already exists or created concurrently
            time.sleep(0.3)
            subfolders_retry, _ = self.list_folder_children_map(parent_id=parent_id)
            if folder_name in subfolders_retry:
                new_fid = subfolders_retry[folder_name]
            else:
                err_msg = res.get("msg") or f"创建文件夹失败 (code: {res.get('code')})"
                raise RuntimeError(err_msg)

        if existing_folders is not None and new_fid:
            existing_folders[folder_name] = new_fid
        return new_fid

    def delete_file(self, file_id: str) -> Dict[str, Any]:
        """
        Delete file(s) or folder(s).
        Guangyapan official endpoint: /userres/v1/file/delete_file with body {"fileIds": [file_id]}.
        Returns an asynchronous task (taskId). We poll /userres/v1/get_task_status until completed.
        """
        file_ids = [file_id] if isinstance(file_id, str) else list(file_id)
        res = self._post("/userres/v1/file/delete_file", {"fileIds": file_ids})
        if res.get("code") != 0 and res.get("code") is not None:
            raise RuntimeError(res.get("msg") or f"删除失败 (错误码: {res.get('code')})")

        task_id = (res.get("data") or {}).get("taskId")
        if task_id:
            import time
            for _ in range(16):
                time.sleep(0.5)
                try:
                    st_res = self._post("/userres/v1/get_task_status", {"taskId": task_id})
                    st_data = st_res.get("data") or {}
                    status = st_data.get("status")
                    if status == 2:
                        break
                    elif status == 3:
                        detail = st_data.get("detail") or {}
                        raise RuntimeError(detail.get("msg") or "删除任务执行失败")
                except RuntimeError:
                    raise
                except Exception as e:
                    print("Error polling delete task status:", e)
                    break

        return res

    def rename_file(self, file_id: str, new_name: str) -> Dict[str, Any]:
        return self._post("/userres/v1/file/rename", {"fileId": file_id, "name": new_name})

    # ================= Recycle Bin (Trash) =================
    def get_trash_list(self, page: int = 0, page_size: int = 100, order_by: int = 12, sort_type: int = 1) -> Dict[str, Any]:
        """
        Get recycle bin (trash) file list.
        Official endpoint: POST /userres/v1/file/get_file_list with dirType=4.
        Returns: { total: int, list: [...] }
        """
        payload = {
            "dirType": 4,
            "page": page,
            "pageSize": page_size,
            "orderBy": order_by,
            "sortType": sort_type
        }
        res = self._post("/userres/v1/file/get_file_list", payload)
        return res.get("data") or {}

    def restore_trash_files(self, file_ids) -> Dict[str, Any]:
        """
        Restore file(s) from recycle bin.
        Official endpoint: POST /userres/v1/file/recycle_file with {"fileIds": [...]}
        """
        ids = [file_ids] if isinstance(file_ids, str) else list(file_ids)
        res = self._post("/userres/v1/file/recycle_file", {"fileIds": ids})
        task_id = (res.get("data") or {}).get("taskId")
        if task_id:
            import time
            for _ in range(16):
                time.sleep(0.5)
                try:
                    st_res = self._post("/userres/v1/get_task_status", {"taskId": task_id})
                    st_data = st_res.get("data") or {}
                    status = st_data.get("status")
                    if status in (2, 3):
                        break
                except Exception:
                    break
        return res

    def delete_trash_files(self, file_ids) -> Dict[str, Any]:
        """
        Permanently delete file(s) from recycle bin.
        Official endpoint: POST /userres/v1/file/delete_file with {"fileIds": [...]}
        """
        return self.delete_file(file_ids)

    def clear_recycle_bin(self) -> Dict[str, Any]:
        """
        Clear / empty entire recycle bin.
        Official endpoint: POST /userres/v1/file/clear_recycle_bin
        """
        res = self._post("/userres/v1/file/clear_recycle_bin", {})
        task_id = (res.get("data") or {}).get("taskId")
        if task_id:
            import time
            for _ in range(20):
                time.sleep(0.5)
                try:
                    st_res = self._post("/userres/v1/get_task_status", {"taskId": task_id})
                    st_data = st_res.get("data") or {}
                    status = st_data.get("status")
                    if status in (2, 3):
                        break
                except Exception:
                    break
        return res

    # ================= Download =================
    def get_download_url(self, file_id: str) -> Dict[str, Any]:
        """
        Get direct CDN signed download URL.
        Returns: { signedURL, urlDuration, requestId }
        """
        res = self._post("/userres/v1/get_res_download_url", {"fileId": file_id})
        return res.get("data", {})

    # ================= Cloud Add (Magnet & BT) =================
    def resolve_res(self, url: str) -> Dict[str, Any]:
        """
        Resolve a resource URL (magnet, http/https, etc.) before creating offline task.
        Returns resolved data with btResInfo or urlResInfo.
        """
        res = self._post("/cloudcollection/v1/resolve_res", {"url": url.strip()})
        return res.get("data", {})

    def cloud_add_magnet(self, magnet_url: str, parent_id: str = "", file_indexes: Optional[List[int]] = None, new_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Add a magnet link task to cloud download with optional file selection and custom name.
        """
        body = {
            "url": magnet_url.strip(),
            "parentId": parent_id or ""
        }
        if file_indexes is not None:
            body["fileIndexes"] = file_indexes
        if new_name:
            body["newName"] = new_name
        res = self._post("/cloudcollection/v1/create_task", body)
        return res

    def resolve_torrent(self, torrent_bytes: bytes, filename: str = "task.torrent") -> Dict[str, Any]:
        """
        Resolve a .torrent file before adding.
        Returns btResInfo with infoHash, file list, title, etc.
        """
        boundary = "----WebKitFormBoundaryGuangyaFnOS" + str(int(time.time()))
        headers = self._get_headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        
        body_parts = []
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"torrent\"; filename=\"{filename}\"\r\nContent-Type: application/x-bittorrent\r\n\r\n".encode("utf-8"))
        body_parts.append(torrent_bytes)
        body_parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        data = b"".join(body_parts)

        url = f"{self.API_BASE}/cloudcollection/v1/resolve_torrent"
        req = urllib.request.Request(url, headers=headers, data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return res.get("data", {})

    def cloud_add_torrent(self, torrent_bytes: bytes, filename: str = "task.torrent", parent_id: str = "", file_indexes: Optional[List[int]] = None, new_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Upload .torrent file, resolve infoHash, and create cloud download task.
        """
        resolved = self.resolve_torrent(torrent_bytes, filename)
        bt_info = resolved.get("btResInfo", {})
        info_hash = bt_info.get("infoHash")
        if not info_hash:
            raise RuntimeError(f"Failed to resolve torrent: {resolved}")
        
        magnet_url = f"magnet:?xt=urn:btih:{info_hash}"
        body = {
            "url": magnet_url,
            "parentId": parent_id or ""
        }
        if file_indexes is not None:
            body["fileIndexes"] = file_indexes
        if new_name:
            body["newName"] = new_name
        elif bt_info.get("title") or bt_info.get("fileName"):
            body["newName"] = bt_info.get("title") or bt_info.get("fileName")

        return self._post("/cloudcollection/v1/create_task", body)

    def list_cloud_tasks(self, page: int = 0, page_size: int = 20, type_filter: int = 0) -> Dict[str, Any]:
        """
        Query cloud collection offline download tasks.
        """
        body = {
            "page": page,
            "pageSize": page_size,
            "type": type_filter
        }
        res = self._post("/cloudcollection/v1/list_task", body)
        return res.get("data", {})

    def delete_cloud_task(self, task_id: str, delete_file: bool = False) -> Dict[str, Any]:
        """
        Delete cloud offline download task with support for deleting cloud file.
        """
        return self._post("/cloudcollection/v2/delete_task", {
            "taskIds": [task_id],
            "deleteFile": bool(delete_file)
        })

    def retry_cloud_task(self, task_id: str) -> Dict[str, Any]:
        """
        Retry / resume a cloud offline download task.
        """
        return self._post("/cloudcollection/v2/retry_task", {
            "taskIds": [task_id]
        })

    def pause_cloud_task(self, task_id: str) -> Dict[str, Any]:
        """
        Attempt to pause a cloud offline download task.
        """
        return self._post("/cloudcollection/v2/pause_task", {
            "taskIds": [task_id]
        })

    # ================= Upload (Instant & Object Storage) =================
    def upload_file(self, file_path: str, parent_id: str = "", custom_name: Optional[str] = None, progress_callback=None) -> Dict[str, Any]:
        """
        Upload file to Guangyapan:
        1. Calculate GCID & CID (Xunlei block SHA-1 algorithm) and MD5
        2. Request upload token (/userres/v2/get_res_center_token with capacity: 30)
        3. Attempt flash upload (秒传)
        4. If not hit, upload directly via Aliyun OSS2 SDK or HTTP PUT
        5. Poll task completion until fileId is returned
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        file_name = custom_name or os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        file_md5 = calculate_file_md5(file_path)
        hashes = calculate_gcid_cid(file_path)

        if progress_callback:
            progress_callback({"stage": "hash_ready", "percent": 0, "file": file_name})

        # 1. Get upload token (capacity 30 = 2|4|8|16)
        token_body = {
            "capacity": 30,
            "name": file_name,
            "parentId": parent_id or "",
            "res": {
                "fileSize": file_size,
                "md5": file_md5
            }
        }
        token_res = self._post("/userres/v2/get_res_center_token", token_body)
        token_data = token_res.get("data", {})
        task_id = token_data.get("taskId")
        if not task_id:
            raise RuntimeError(f"Failed to get upload token: {token_res}")

        # If instant upload code 156 returned directly
        if token_res.get("code") == 156:
            if progress_callback:
                progress_callback({"stage": "completed", "percent": 100, "file": file_name, "instant": True})
            return {"fileId": token_data.get("fileId"), "instant": True}

        # 2. Check Flash Upload (秒传)
        flash_body = {
            "taskId": task_id,
            "gcid": hashes["gcid"],
            "cid": hashes["cid"]
        }
        flash_res = self._post("/userres/v1/check_can_flash_upload", flash_body)
        flash_data = flash_res.get("data", {})
        if flash_data.get("canFlashUpload"):
            if progress_callback:
                progress_callback({"stage": "completed", "percent": 100, "file": file_name, "instant": True})
            # Poll completion
            return self._poll_uploaded_file(task_id, file_size)

        # 3. Direct Upload via OSS (Provider 1 = Aliyun OSS)
        provider = token_data.get("provider")
        if provider == 1:
            try:
                import oss2
                creds = token_data.get("creds", {})
                auth = oss2.StsAuth(creds["accessKeyID"], creds["secretAccessKey"], creds["sessionToken"])
                endpoint = token_data.get("fullEndPoint") or f"https://{token_data['bucketName']}.{token_data['endPoint']}"
                bucket = oss2.Bucket(auth, endpoint, token_data["bucketName"], is_cname=True)

                def oss_progress(consumed_bytes, total_bytes):
                    if progress_callback and total_bytes:
                        pct = int((consumed_bytes / total_bytes) * 100)
                        progress_callback({"stage": "uploading", "percent": pct, "file": file_name, "uploaded": consumed_bytes, "total": total_bytes})

                if file_size > 50 * 1024 * 1024:
                    try:
                        oss2.resumable_upload(bucket, token_data["objectPath"], file_path, progress_callback=oss_progress, num_threads=2)
                    except Exception:
                        with open(file_path, "rb") as f:
                            bucket.put_object(token_data["objectPath"], f, progress_callback=oss_progress)
                else:
                    with open(file_path, "rb") as f:
                        bucket.put_object(token_data["objectPath"], f, progress_callback=oss_progress)

            except ImportError:
                # Fallback error when oss2 is missing
                raise RuntimeError("检测到未安装阿里云 OSS SDK (oss2)。非秒传文件需要物理上传至对象存储，请在 NAS 终端执行: pip install oss2 -i https://pypi.tuna.tsinghua.edu.cn/simple --break-system-packages")
        else:
            raise NotImplementedError(f"Unsupported storage provider: {provider}")

        # 4. Poll completion
        if progress_callback:
            progress_callback({"stage": "verifying", "percent": 99, "file": file_name})

        return self._poll_uploaded_file(task_id, file_size)

    def _poll_uploaded_file(self, task_id: str, uploaded_size: int, max_attempts: int = 30) -> Dict[str, Any]:
        for attempt in range(max_attempts):
            time.sleep(1)
            res = self._post("/userres/v1/file/get_info_by_task_id", {"taskId": task_id})
            data = res.get("data")
            if data and data.get("fileId"):
                return data
            if res.get("code") == 147: # Still processing/validating
                continue
            if res.get("code") and res.get("code") != 0:
                raise RuntimeError(f"Upload verification failed with code {res.get('code')}: {res.get('msg')}")
        raise TimeoutError(f"Timed out polling upload completion for task {task_id}")
