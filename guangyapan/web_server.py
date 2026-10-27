import os
import json
import time
import re
import glob
import socket
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from .client import GuangyapanClient
from .downloader import FileDownloader, fix_permissions, format_size, format_speed

APP_VERSION = "1.2.2"
GITHUB_REPO_URL = "https://github.com/lawzhougc/guangyapan-fnos"
_VERSION_CACHE = {"last_check": 0, "data": None}

STRM_VIDEO_EXTS = {
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "ts", "m2ts",
    "m4v", "rmvb", "rm", "iso", "mpg", "mpeg", "vob", "3gp", "f4v"
}
STRM_SUB_EXTS = {"srt", "ass", "ssa", "vtt", "sub", "idx", "sup", "smi"}
STRM_META_EXTS = {"nfo", "jpg", "jpeg", "png", "tbn"}

def detect_local_ip():
    """Auto-detect the primary LAN IPv4 address of the NAS host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("223.5.5.5", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"

def sanitize_fs_name(name: str) -> str:
    """Sanitize a cloud file/folder name so it is safe on Linux/Windows filesystems."""
    if not name:
        return "unnamed"
    cleaned = re.sub(r'[/\\:\x00]', '_', name).strip()
    return cleaned or "unnamed"

def get_fnos_storage_volumes():
    """Discover all mounted storage volumes on fnOS (e.g. /vol1, /vol2, /vol01)."""
    volumes = []
    # 1. Check /proc/mounts
    try:
        if os.path.exists("/proc/mounts"):
            with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mp = parts[1]
                        if re.match(r"^/vol[0-9]+$", mp) and mp not in volumes:
                            volumes.append(mp)
    except Exception:
        pass

    # 2. Filesystem glob /vol*
    for v in range(0, 33):
        for cand in [f"/vol{v}", f"/vol{v:02d}"]:
            if os.path.isdir(cand) and cand not in volumes:
                volumes.append(cand)

    def _vol_key(x):
        m = re.search(r"\d+", x)
        return int(m.group(0)) if m else 0

    volumes.sort(key=_vol_key)
    return volumes

def get_fnos_user_shares():
    """Discover all user shared folders across all storage volumes."""
    shares = []
    vols = get_fnos_storage_volumes()
    for v in vols:
        v_name = os.path.basename(v)
        try:
            with os.scandir(v) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=True) and entry.name.isdigit() and int(entry.name) >= 1000:
                        uid_dir = entry.path
                        try:
                            with os.scandir(uid_dir) as uit:
                                for s_entry in uit:
                                    if s_entry.is_dir(follow_symlinks=True) and not s_entry.name.startswith((".", "@")):
                                        shares.append({
                                            "name": f"{s_entry.name}  [{v_name}]",
                                            "path": s_entry.path.replace("\\", "/"),
                                            "is_share": True
                                        })
                        except Exception:
                            pass
        except Exception:
            pass
    shares.sort(key=lambda x: x["name"].lower())
    return shares

def detect_default_download_dir(preferred=""):
    """Dynamically determine the best default download directory on fnOS."""
    if preferred and os.path.isdir(preferred):
        return preferred.replace("\\", "/")

    # Check detected user directories across volumes
    vols = get_fnos_storage_volumes()
    for v in vols:
        try:
            with os.scandir(v) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=True) and entry.name.isdigit() and int(entry.name) >= 1000:
                        candidate = os.path.join(entry.path, "光鸭网盘").replace("\\", "/")
                        return candidate
        except Exception:
            pass

    if vols:
        return os.path.join(vols[0], "光鸭网盘").replace("\\", "/")

    home = os.path.expanduser("~")
    return os.path.join(home, "Downloads", "光鸭网盘").replace("\\", "/")

class AppLogger:
    """Centralized in-memory and persistent on-disk structured logger."""
    def __init__(self, log_dir=None, max_in_memory=2000, max_file_bytes=5 * 1024 * 1024):
        self.lock = threading.Lock()
        self.max_in_memory = max_in_memory
        self.max_file_bytes = max_file_bytes
        self.log_dir = log_dir
        self.log_file = None
        self._seq = 0
        self.logs = []
        if log_dir:
            self.setup_log_file(log_dir)

    def setup_log_file(self, target_dir):
        if not target_dir:
            return
        try:
            os.makedirs(target_dir, exist_ok=True)
            self.log_dir = target_dir
            self.log_file = os.path.join(target_dir, "app.log").replace("\\", "/")
        except Exception as e:
            print(f"Error setting up log file: {e}")

    def log(self, level: str, category: str, message: str, details: str = None):
        now = time.time()
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        lvl = (level or "INFO").upper()
        cat = (category or "system").lower()
        msg = str(message or "").strip()
        det = str(details).strip() if details else ""

        with self.lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "time": time_str,
                "ts": now,
                "level": lvl,
                "category": cat,
                "message": msg,
                "details": det
            }
            self.logs.append(item)
            if len(self.logs) > self.max_in_memory:
                del self.logs[:len(self.logs) - self.max_in_memory]

        try:
            print(f"[{time_str}] [{lvl:<7}] [{cat:<8}] {msg}")
        except Exception:
            try:
                enc = sys.stdout.encoding or "utf-8"
                safe_msg = msg.encode(enc, errors="replace").decode(enc)
                print(f"[{time_str}] [{lvl:<7}] [{cat:<8}] {safe_msg}")
            except Exception:
                pass

        if self.log_file:
            try:
                line = f"[{time_str}] [{lvl:<7}] [{cat:<8}] {msg}\n"
                if det:
                    line += f"    详情: {det}\n"
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(line)
                if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > self.max_file_bytes:
                    bak = self.log_file + ".1"
                    if os.path.exists(bak):
                        try:
                            os.remove(bak)
                        except Exception:
                            pass
                    os.rename(self.log_file, bak)
            except Exception:
                pass

    def get_logs(self, category=None, level=None, keyword=None, limit=300, since_id=0):
        with self.lock:
            all_logs = list(self.logs)
            max_id = self._seq

        cat_counts = {
            "all": len(all_logs),
            "download": sum(1 for l in all_logs if l["category"] == "download"),
            "cloud": sum(1 for l in all_logs if l["category"] == "cloud"),
            "strm": sum(1 for l in all_logs if l["category"] == "strm"),
            "system": sum(1 for l in all_logs if l["category"] == "system")
        }

        filtered = all_logs
        if since_id and int(since_id) > 0:
            s_id = int(since_id)
            filtered = [l for l in filtered if l["id"] > s_id]

        if category and category.lower() != "all":
            c = category.lower()
            filtered = [l for l in filtered if l["category"] == c]

        if level and level.upper() != "ALL":
            l_val = level.upper()
            filtered = [l for l in filtered if l["level"] == l_val]

        if keyword:
            kw = keyword.lower().strip()
            filtered = [
                l for l in filtered
                if kw in l["message"].lower() or kw in l.get("details", "").lower()
            ]

        total_filtered = len(filtered)
        if limit and int(limit) > 0:
            lim = int(limit)
            filtered = filtered[-lim:]

        return {
            "logs": filtered,
            "total": total_filtered,
            "max_id": max_id,
            "categories": cat_counts
        }

    def clear(self):
        with self.lock:
            self.logs.clear()
        if self.log_file and os.path.exists(self.log_file):
            try:
                with open(self.log_file, "w", encoding="utf-8") as f:
                    f.truncate(0)
            except Exception:
                pass
        self.log("INFO", "system", "🧹 用户清空了系统运行日志")
        return True

    def export_text(self):
        with self.lock:
            items = list(self.logs)
        lines = [
            "# 光鸭网盘 fnOS 原生客户端系统运行日志",
            f"# 导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"# 记录条数: {len(items)}",
            "-" * 60,
            ""
        ]
        for item in items:
            l = f"[{item['time']}] [{item['level']:<7}] [{item['category']:<8}] {item['message']}"
            if item.get("details"):
                l += f"\n    详情: {item['details']}"
            lines.append(l)
        return "\n".join(lines)

class GuangyapanApp:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.client = GuangyapanClient(config_path)
        self.downloader = FileDownloader(use_aria2=True)
        # Initialize structured logger
        cfg_dir = os.path.dirname(os.path.abspath(config_path))
        self.logger = AppLogger(log_dir=cfg_dir)
        self.logger.log("INFO", "system", f"🚀 光鸭网盘客户端核心引擎初始化 (版本: {APP_VERSION})")
        # task_id -> task dict
        self.downloads = {}
        self.download_queue_lock = threading.Lock()
        self._task_seq = 0
        saved_concurrency = self.client.config.get("max_concurrent_downloads", 3)
        try:
            self.max_concurrent_downloads = max(1, min(16, int(saved_concurrency or 3)))
        except Exception:
            self.max_concurrent_downloads = 3

        saved_page_size = self.client.config.get("file_page_size", 20)
        try:
            self.file_page_size = max(0, min(5000, int(saved_page_size if saved_page_size is not None else 20)))
        except Exception:
            self.file_page_size = 20

        # Default download dir dynamically resolved
        saved_dir = self.client.config.get("download_dir", "")
        self.download_dir = detect_default_download_dir(saved_dir)
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            fix_permissions(self.download_dir)
        except Exception:
            pass

        # ================= STRM Media Library & 302 Gateway State =================
        saved_strm = self.client.config.get("strm_config", {}) or {}
        default_strm_dir = os.path.join(self.download_dir, "STRM影视库").replace("\\", "/")
        default_base_url = f"http://{detect_local_ip()}:8998"
        self.strm_config = {
            "base_url": (saved_strm.get("base_url") or default_base_url).rstrip("/"),
            "output_dir": (saved_strm.get("output_dir") or default_strm_dir).replace("\\", "/"),
            "cloud_parent_id": saved_strm.get("cloud_parent_id", ""),
            "cloud_folder_path": saved_strm.get("cloud_folder_path") or "全部文件",
            "download_subtitles": bool(saved_strm.get("download_subtitles", True)),
            "download_metadata": bool(saved_strm.get("download_metadata", False)),
            "overwrite": bool(saved_strm.get("overwrite", True)),
            "clean_orphans": bool(saved_strm.get("clean_orphans", False)),
            "auto_sync_offline": bool(saved_strm.get("auto_sync_offline", True)),
            "schedule_interval_min": int(saved_strm.get("schedule_interval_min", 0) or 0),
            "use_proxy": bool(saved_strm.get("use_proxy", False)),
        }
        try:
            os.makedirs(self.strm_config["output_dir"], exist_ok=True)
            fix_permissions(self.strm_config["output_dir"])
        except Exception:
            pass

        self.strm_cache_lock = threading.Lock()
        self.strm_url_cache = {}  # file_id -> {"url": signed_url, "expires_at": ts, "cached_at": ts, "hits": 0}
        self.strm_total_hits = 0

        self.strm_sync_lock = threading.Lock()
        self.strm_cancel_flag = False
        self.strm_status = {
            "running": False,
            "taskId": "",
            "status": "idle",  # idle | running | completed | failed | cancelled
            "reason": "",
            "currentDir": "",
            "currentFile": "",
            "scannedDirs": 0,
            "scannedFiles": 0,
            "createdStrm": 0,
            "updatedStrm": 0,
            "skippedStrm": 0,
            "downloadedSubs": 0,
            "cleanedFiles": 0,
            "failedCount": 0,
            "startTime": None,
            "finishTime": None,
            "error": None,
            "recentFiles": [],
            "logs": []
        }
        self._seen_completed_offline_ids = None  # None until first baseline scan
        self._last_scheduled_sync_ts = time.time()
        threading.Thread(target=self._strm_background_scheduler, daemon=True).start()

    def log(self, level: str, category: str, message: str, details: str = None):
        if hasattr(self, "logger") and self.logger:
            self.logger.log(level, category, message, details)
        else:
            print(f"[{level}] [{category}] {message}")

    def set_download_dir(self, new_dir: str):
        self.download_dir = new_dir.rstrip("/\\")
        self.client.config["download_dir"] = self.download_dir
        self.client._save_config()
        self.log("INFO", "system", f"📁 NAS 本地下载目录已更新为: {self.download_dir}")
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            fix_permissions(self.download_dir)
        except Exception:
            pass

    # ================= STRM Gateway & Sync Engine Methods =================
    def save_strm_config(self, new_cfg: dict) -> dict:
        if "base_url" in new_cfg and new_cfg["base_url"]:
            self.strm_config["base_url"] = str(new_cfg["base_url"]).strip().rstrip("/")
        if "output_dir" in new_cfg and new_cfg["output_dir"]:
            self.strm_config["output_dir"] = str(new_cfg["output_dir"]).strip().rstrip("/\\").replace("\\", "/")
            try:
                os.makedirs(self.strm_config["output_dir"], exist_ok=True)
                fix_permissions(self.strm_config["output_dir"])
            except Exception:
                pass
        if "cloud_parent_id" in new_cfg:
            self.strm_config["cloud_parent_id"] = str(new_cfg["cloud_parent_id"] or "")
        if "cloud_folder_path" in new_cfg:
            self.strm_config["cloud_folder_path"] = str(new_cfg["cloud_folder_path"] or "全部文件")
        for bool_key in ("download_subtitles", "download_metadata", "overwrite", "clean_orphans", "auto_sync_offline", "use_proxy"):
            if bool_key in new_cfg:
                self.strm_config[bool_key] = bool(new_cfg[bool_key])
        if "schedule_interval_min" in new_cfg:
            try:
                self.strm_config["schedule_interval_min"] = max(0, int(new_cfg["schedule_interval_min"]))
            except Exception:
                pass

        self.client.config["strm_config"] = self.strm_config
        self.client._save_config()
        return self.strm_config

    def get_strm_redirect_url(self, file_id: str) -> str:
        """Resolve and cache Guangyapan CDN signedURL for fast 302 streaming."""
        now = time.time()
        with self.strm_cache_lock:
            entry = self.strm_url_cache.get(file_id)
            if entry and now < entry["expires_at"] and entry.get("url"):
                entry["hits"] = entry.get("hits", 0) + 1
                self.strm_total_hits += 1
                return entry["url"]

        dl_info = self.client.get_download_url(file_id)
        signed_url = dl_info.get("signedURL")
        if not signed_url:
            raise RuntimeError("未能获取到光鸭网盘播放直链 (请检查账号登录状态或文件是否已删除)")

        duration = int(dl_info.get("urlDuration") or 1800)
        ttl = max(60, min(duration - 180, 1800))
        with self.strm_cache_lock:
            if len(self.strm_url_cache) > 600:
                expired_keys = [k for k, v in self.strm_url_cache.items() if now >= v["expires_at"]]
                for k in expired_keys:
                    self.strm_url_cache.pop(k, None)
            self.strm_url_cache[file_id] = {
                "url": signed_url,
                "expires_at": now + ttl,
                "cached_at": now,
                "hits": 1
            }
        return signed_url

    def clear_strm_cache(self):
        with self.strm_cache_lock:
            count = len(self.strm_url_cache)
            self.strm_url_cache.clear()
            return count

    def get_strm_status_payload(self) -> dict:
        now = time.time()
        with self.strm_cache_lock:
            valid_cache = sum(1 for v in self.strm_url_cache.values() if now < v["expires_at"])
        return {
            "config": self.strm_config,
            "detected_ip": detect_local_ip(),
            "cache_count": valid_cache,
            "total_hits": self.strm_total_hits,
            "status": self.strm_status
        }

    def cancel_strm_sync(self):
        self.strm_cancel_flag = True
        if self.strm_status.get("running"):
            self.strm_status["status"] = "cancelled"

    def _add_strm_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        logs = self.strm_status.get("logs", [])
        logs.append(line)
        if len(logs) > 120:
            del logs[:len(logs) - 120]
        lvl = "INFO"
        if "❌" in msg or "失败" in msg or "错误" in msg:
            lvl = "ERROR"
        elif "⚠️" in msg:
            lvl = "WARN"
        elif "✅" in msg or "🎉" in msg or "🚀" in msg:
            lvl = "SUCCESS"
        self.log(lvl, "strm", msg)

    def start_strm_sync_task(
        self,
        cloud_parent_id: str = None,
        cloud_folder_path: str = None,
        output_dir: str = None,
        single_file: dict = None,
        keep_root_folder: bool = False,
        download_subtitles: bool = None,
        reason: str = "手动触发"
    ) -> dict:
        with self.strm_sync_lock:
            if self.strm_status.get("running"):
                return {"started": False, "error": "当前已有 STRM 同步任务正在运行中", "status": self.strm_status}
            self.strm_cancel_flag = False
            task_id = str(int(time.time() * 1000))
            self.strm_status.update({
                "running": True,
                "taskId": task_id,
                "status": "running",
                "reason": reason,
                "currentDir": "",
                "currentFile": "",
                "scannedDirs": 0,
                "scannedFiles": 0,
                "createdStrm": 0,
                "updatedStrm": 0,
                "skippedStrm": 0,
                "downloadedSubs": 0,
                "cleanedFiles": 0,
                "failedCount": 0,
                "startTime": time.time(),
                "finishTime": None,
                "error": None,
                "logs": []
            })

        target_parent_id = self.strm_config["cloud_parent_id"] if cloud_parent_id is None else cloud_parent_id
        target_cloud_path = self.strm_config["cloud_folder_path"] if cloud_folder_path is None else cloud_folder_path
        base_out_dir = (output_dir or self.strm_config["output_dir"]).rstrip("/\\").replace("\\", "/")
        dl_subs = self.strm_config["download_subtitles"] if download_subtitles is None else bool(download_subtitles)
        dl_meta = self.strm_config.get("download_metadata", False)
        do_overwrite = self.strm_config.get("overwrite", True)
        do_clean = self.strm_config.get("clean_orphans", False) and (single_file is None) and (not keep_root_folder)
        base_url = (self.strm_config.get("base_url") or f"http://{detect_local_ip()}:8998").rstrip("/")
        use_proxy = self.strm_config.get("use_proxy", False)

        def worker():
            expected_strm_set = set()
            try:
                os.makedirs(base_out_dir, exist_ok=True)
                fix_permissions(base_out_dir)
                self._add_strm_log(f"🚀 开始 STRM 任务 ({reason}) -> 目标: {base_out_dir}")

                def write_strm_file(fid: str, fname: str, local_dir: str):
                    safe_name = sanitize_fs_name(fname)
                    stem, _ = os.path.splitext(safe_name)
                    strm_name = f"{stem}.strm"
                    strm_path = os.path.join(local_dir, strm_name).replace("\\", "/")
                    expected_strm_set.add(os.path.normcase(os.path.abspath(strm_path)))

                    enc_fid = urllib.parse.quote(str(fid), safe="")
                    enc_name = urllib.parse.quote(fname, safe="")
                    proxy_q = "?proxy=1" if use_proxy else ""
                    stream_url = f"{base_url}/strm/{enc_fid}/{enc_name}{proxy_q}"

                    os.makedirs(local_dir, exist_ok=True)
                    fix_permissions(local_dir)

                    action_taken = "created"
                    if os.path.exists(strm_path):
                        try:
                            with open(strm_path, "r", encoding="utf-8", errors="ignore") as rf:
                                old_content = rf.read().strip()
                        except Exception:
                            old_content = ""

                        if old_content == stream_url:
                            self.strm_status["skippedStrm"] += 1
                            action_taken = "skipped"
                        elif do_overwrite:
                            with open(strm_path, "w", encoding="utf-8") as wf:
                                wf.write(stream_url + "\n")
                            fix_permissions(strm_path)
                            self.strm_status["updatedStrm"] += 1
                            self._add_strm_log(f"🔄 更新: {strm_name}")
                            action_taken = "updated"
                        else:
                            self.strm_status["skippedStrm"] += 1
                            action_taken = "skipped"
                    else:
                        with open(strm_path, "w", encoding="utf-8") as wf:
                            wf.write(stream_url + "\n")
                        fix_permissions(strm_path)
                        self.strm_status["createdStrm"] += 1
                        self._add_strm_log(f"✅ 生成: {strm_name}")
                        action_taken = "created"

                    recent = self.strm_status.get("recentFiles", [])
                    # Remove existing entry with same strmPath if any
                    recent = [r for r in recent if r.get("strmPath") != strm_path]
                    recent.insert(0, {
                        "fileId": fid,
                        "fileName": fname,
                        "strmName": strm_name,
                        "strmPath": strm_path,
                        "streamUrl": stream_url,
                        "action": action_taken,
                        "time": int(time.time())
                    })
                    self.strm_status["recentFiles"] = recent[:60]

                def download_aux(fid: str, fname: str, local_dir: str):
                    safe_name = sanitize_fs_name(fname)
                    aux_path = os.path.join(local_dir, safe_name).replace("\\", "/")
                    if os.path.exists(aux_path) and os.path.getsize(aux_path) > 0:
                        return
                    try:
                        os.makedirs(local_dir, exist_ok=True)
                        fix_permissions(local_dir)
                        dl_info = self.client.get_download_url(fid)
                        s_url = dl_info.get("signedURL")
                        if not s_url:
                            return
                        req = urllib.request.Request(s_url, headers={"User-Agent": self.client.USER_AGENT})
                        with urllib.request.urlopen(req, timeout=15) as resp, open(aux_path, "wb") as out_f:
                            out_f.write(resp.read())
                        fix_permissions(aux_path)
                        self.strm_status["downloadedSubs"] += 1
                        self._add_strm_log(f"📝 同步字幕/元数据: {safe_name}")
                    except Exception as ex:
                        self._add_strm_log(f"⚠️ 附属文件下载跳过 ({safe_name}): {ex}")

                def scan_folder(pid: str, rel_display: str, current_local_dir: str):
                    if self.strm_cancel_flag:
                        return
                    self.strm_status["scannedDirs"] += 1
                    self.strm_status["currentDir"] = rel_display
                    self._add_strm_log(f"📂 扫描目录: {rel_display}")

                    items = self.client.list_all_files(parent_id=pid)
                    for item in items:
                        if self.strm_cancel_flag:
                            return
                        fname = (item.get("fileName") or "").strip()
                        fid = item.get("fileId") or ""
                        res_type = item.get("resType")
                        if not fname or not fid:
                            continue

                        if res_type == 2:
                            if fname == "保险箱":
                                continue
                            next_rel = f"{rel_display}/{fname}" if rel_display else fname
                            next_local = os.path.join(current_local_dir, sanitize_fs_name(fname)).replace("\\", "/")
                            scan_folder(fid, next_rel, next_local)
                        else:
                            self.strm_status["scannedFiles"] += 1
                            self.strm_status["currentFile"] = fname
                            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                            fsize = int(item.get("fileSize") or 0)
                            try:
                                if ext in STRM_VIDEO_EXTS:
                                    write_strm_file(fid, fname, current_local_dir)
                                elif dl_subs and ext in STRM_SUB_EXTS and fsize <= 30 * 1024 * 1024:
                                    download_aux(fid, fname, current_local_dir)
                                elif dl_meta and ext in STRM_META_EXTS and fsize <= 20 * 1024 * 1024:
                                    download_aux(fid, fname, current_local_dir)
                            except Exception as item_err:
                                self.strm_status["failedCount"] += 1
                                self._add_strm_log(f"❌ 处理失败 ({fname}): {item_err}")

                if single_file:
                    sf_id = single_file.get("fileId", "")
                    sf_name = single_file.get("fileName", "")
                    self.strm_status["scannedFiles"] = 1
                    self.strm_status["currentFile"] = sf_name
                    write_strm_file(sf_id, sf_name, base_out_dir)
                else:
                    start_local_dir = base_out_dir
                    if keep_root_folder and target_cloud_path and target_cloud_path != "全部文件":
                        root_folder_name = target_cloud_path.split("/")[-1].strip()
                        if root_folder_name and root_folder_name != "全部文件":
                            start_local_dir = os.path.join(base_out_dir, sanitize_fs_name(root_folder_name)).replace("\\", "/")
                    scan_folder(target_parent_id, target_cloud_path or "全部文件", start_local_dir)

                    if do_clean and not self.strm_cancel_flag and os.path.exists(start_local_dir):
                        for root_d, _, files in os.walk(start_local_dir):
                            for f in files:
                                if f.lower().endswith(".strm"):
                                    full_p = os.path.join(root_d, f)
                                    norm_p = os.path.normcase(os.path.abspath(full_p))
                                    if norm_p not in expected_strm_set:
                                        try:
                                            os.remove(full_p)
                                            self.strm_status["cleanedFiles"] += 1
                                            self._add_strm_log(f"🗑️ 清理失效 STRM: {f}")
                                        except Exception:
                                            pass

                if self.strm_cancel_flag:
                    self.strm_status["status"] = "cancelled"
                    self._add_strm_log("⏹️ 同步任务已手动停止")
                else:
                    self.strm_status["status"] = "completed"
                    self._add_strm_log(
                        f"🎉 同步完成！新增 {self.strm_status['createdStrm']} 个，更新 {self.strm_status['updatedStrm']} 个，"
                        f"跳过 {self.strm_status['skippedStrm']} 个，字幕 {self.strm_status['downloadedSubs']} 个"
                    )
            except Exception as e:
                self.strm_status["status"] = "failed"
                self.strm_status["error"] = str(e)
                self._add_strm_log(f"❌ 同步异常中断: {e}")
            finally:
                self.strm_status["running"] = False
                self.strm_status["finishTime"] = time.time()
                self._last_scheduled_sync_ts = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True, "taskId": task_id, "status": self.strm_status}

    def check_offline_tasks_for_strm(self, task_list: list):
        """Detect newly finished cloud offline tasks and auto-trigger incremental STRM sync."""
        if not isinstance(task_list, list):
            return
        completed_ids = set()
        newly_done_names = []
        for t in task_list:
            tid = str(t.get("taskId") or "")
            if not tid:
                continue
            if t.get("status") == 2 or bool(t.get("fileId")):
                completed_ids.add(tid)
                if self._seen_completed_offline_ids is not None and tid not in self._seen_completed_offline_ids:
                    newly_done_names.append(t.get("fileName") or tid)

        if self._seen_completed_offline_ids is None:
            self._seen_completed_offline_ids = completed_ids
            return

        self._seen_completed_offline_ids.update(completed_ids)
        if newly_done_names and self.strm_config.get("auto_sync_offline", True):
            if not self.strm_status.get("running") and self.client.access_token:
                sample_name = newly_done_names[0]
                self.start_strm_sync_task(reason=f"离线下载完成联动 ({sample_name})")

    def _strm_background_scheduler(self):
        """Background daemon checking scheduled STRM sync intervals."""
        while True:
            time.sleep(30)
            try:
                interval_min = int(self.strm_config.get("schedule_interval_min") or 0)
                if interval_min > 0 and self.client.access_token and not self.strm_status.get("running"):
                    if (time.time() - self._last_scheduled_sync_ts) >= interval_min * 60:
                        self._last_scheduled_sync_ts = time.time()
                        self.start_strm_sync_task(reason=f"定时自动同步 (每 {interval_min} 分钟)")
            except Exception:
                pass

    def set_max_concurrent_downloads(self, limit: int) -> int:
        try:
            val = max(1, min(16, int(limit)))
        except Exception:
            val = 3
        old_val = self.max_concurrent_downloads
        self.max_concurrent_downloads = val
        self.client.config["max_concurrent_downloads"] = val
        self.client._save_config()
        self._schedule_downloads()
        if old_val != val:
            self.log("INFO", "system", f"⚡ 最大同时下载任务数调整为: {self.max_concurrent_downloads}")
        return self.max_concurrent_downloads

    def set_file_page_size(self, page_size: int) -> int:
        try:
            val = max(0, min(5000, int(page_size)))
        except Exception:
            val = 20
        old_val = self.file_page_size
        self.file_page_size = val
        self.client.config["file_page_size"] = val
        self.client._save_config()
        if old_val != val:
            self.log("INFO", "system", f"📄 文件列表每页显示数量调整为: {self.file_page_size}")
        return self.file_page_size

    def _run_download_worker(self, task_id: str):
        task = self.downloads.get(task_id)
        if not task:
            self._schedule_downloads()
            return

        file_id = task["fileId"]
        out_path = task["path"]
        target_dir = task["targetDir"]

        try:
            try:
                os.makedirs(target_dir, exist_ok=True)
                fix_permissions(target_dir)
            except Exception:
                pass

            if task.get("status") in ("cancelled", "pending"):
                return

            task["status"] = "getting_url"
            task["speed"] = "获取直链中..."
            self.log("INFO", "download", f"⬇️ 开始下载任务: {task.get('fileName', '')}", f"任务ID: {task_id} | 路径: {out_path}")
            dl_info = self.client.get_download_url(file_id)
            signed_url = dl_info.get("signedURL")
            if not signed_url:
                raise RuntimeError("未能获取到下载直链 (请检查网盘账号或文件是否存在)")

            if task.get("status") in ("cancelled", "pending"):
                return

            task["status"] = "downloading"
            task["speed"] = "连接中..."

            def progress(p):
                if task_id not in self.downloads:
                    return
                if "percent" in p:
                    self.downloads[task_id]["percent"] = p["percent"]
                if "speed" in p:
                    self.downloads[task_id]["speed"] = p["speed"]
                if "downloaded" in p:
                    self.downloads[task_id]["downloaded"] = p["downloaded"]
                if "total" in p:
                    self.downloads[task_id]["total"] = p["total"]
                if "eta" in p:
                    self.downloads[task_id]["eta"] = p["eta"]
                elif "raw" in p:
                    self.downloads[task_id]["raw"] = p["raw"]

            ok = self.downloader.download(signed_url, out_path, progress, task_id=task_id)
            if task_id not in self.downloads:
                return
            curr_status = self.downloads[task_id].get("status")
            if curr_status == "pending":
                # Re-queued due to concurrency limit reduction; keep partial progress
                self.downloads[task_id]["speed"] = "排队等待中"
                self.downloads[task_id]["eta"] = "--"
                return
            if curr_status == "cancelled":
                return

            if ok:
                self.downloads[task_id]["status"] = "completed"
                self.downloads[task_id]["percent"] = 100
                self.downloads[task_id]["speed"] = "完成"
                self.downloads[task_id]["eta"] = "0s"
                self.downloads[task_id]["finishTime"] = time.time()
                self.downloads[task_id]["error"] = None
                fix_permissions(out_path)
                self.log("SUCCESS", "download", f"✅ 下载完成: {task.get('fileName', '')}", f"保存路径: {out_path}")
            else:
                self.downloads[task_id]["status"] = "failed"
                self.downloads[task_id]["error"] = "下载未正常完成"
                self.log("ERROR", "download", f"❌ 下载未完成: {task.get('fileName', '')}", f"任务ID: {task_id}")
        except Exception as e:
            if task_id in self.downloads and self.downloads[task_id].get("status") not in ("cancelled", "pending"):
                self.downloads[task_id]["status"] = "failed"
                self.downloads[task_id]["error"] = str(e)
                self.log("ERROR", "download", f"❌ 下载发生异常: {task.get('fileName', '')}", f"错误: {e}")
        finally:
            self._schedule_downloads()

    def _schedule_downloads(self):
        to_start = []
        to_pause = []
        with self.download_queue_lock:
            active_tasks = [
                t for t in self.downloads.values()
                if t.get("status") in ("getting_url", "downloading")
            ]
            active_tasks.sort(key=lambda x: x.get("seq", 0))

            # If concurrency limit was lowered below active count, pause newest active tasks back to pending
            if len(active_tasks) > self.max_concurrent_downloads:
                excess = active_tasks[self.max_concurrent_downloads:]
                for t in excess:
                    t["status"] = "pending"
                    t["speed"] = "排队等待中"
                    t["eta"] = "--"
                    to_pause.append(t["taskId"])
            elif len(active_tasks) < self.max_concurrent_downloads:
                slots = self.max_concurrent_downloads - len(active_tasks)
                pending_tasks = [
                    t for t in self.downloads.values()
                    if t.get("status") == "pending"
                ]
                pending_tasks.sort(key=lambda x: x.get("seq", 0))
                for t in pending_tasks[:slots]:
                    t["status"] = "getting_url"
                    t["speed"] = "准备启动..."
                    t["error"] = None
                    to_start.append(t["taskId"])

        for tid in to_pause:
            self.downloader.cancel_task(tid)

        for tid in to_start:
            threading.Thread(target=self._run_download_worker, args=(tid,), daemon=True).start()

    def _resolve_conflict_filename(self, target_dir: str, file_name: str, conflict_policy: str = "overwrite") -> str:
        safe_name = sanitize_fs_name(file_name)
        full_path = os.path.join(target_dir, safe_name)
        aria2_ctrl = full_path + ".aria2"
        # Only treat as existing complete file if file exists and no .aria2 resume file is present
        if os.path.exists(full_path) and not os.path.exists(aria2_ctrl):
            if conflict_policy == "skip":
                return ""
            elif conflict_policy == "rename":
                base, ext = os.path.splitext(safe_name)
                counter = 1
                while True:
                    cand = f"{base} ({counter}){ext}"
                    if not os.path.exists(os.path.join(target_dir, cand)):
                        return cand
                    counter += 1
        return safe_name

    def _enqueue_single_download(self, file_id: str, file_name: str, target_dir: str, file_size: int = 0) -> str:
        target_dir = (target_dir or self.download_dir).rstrip("/\\").replace("\\", "/")
        out_path = os.path.join(target_dir, file_name).replace("\\", "/")
        with self.download_queue_lock:
            self._task_seq += 1
            seq = self._task_seq
            task_id = f"{int(time.time() * 1000)}_{seq}"
            self.downloads[task_id] = {
                "taskId": task_id,
                "seq": seq,
                "fileId": file_id,
                "fileName": file_name,
                "status": "pending",
                "percent": 0,
                "speed": "排队等待中",
                "downloaded": "--",
                "total": format_size(int(file_size)) if file_size else "--",
                "eta": "--",
                "path": out_path,
                "targetDir": target_dir,
                "startTime": time.time(),
                "finishTime": None,
                "error": None
            }
        return task_id

    def start_download_task(self, file_id: str, file_name: str, target_dir: str = None, file_size: int = 0):
        task_id = self._enqueue_single_download(file_id, file_name, target_dir or self.download_dir, file_size=file_size)
        self.log("INFO", "download", f"📥 添加下载任务: {file_name}", f"文件ID: {file_id} | 大小: {format_size(file_size) if file_size else '未知'}")
        self._schedule_downloads()
        return task_id

    def start_batch_download_tasks(self, items: list, target_dir: str = None, conflict_policy: str = "overwrite") -> dict:
        base_target_dir = (target_dir or self.download_dir).rstrip("/\\").replace("\\", "/")
        try:
            os.makedirs(base_target_dir, exist_ok=True)
            fix_permissions(base_target_dir)
        except Exception:
            pass

        queued_ids = []
        skipped_count = 0
        folder_queue = []

        for item in (items or []):
            fid = str(item.get("fileId") or "").strip()
            fname = str(item.get("fileName") or "").strip()
            res_type = int(item.get("resType") or 1)
            fsize = int(item.get("fileSize") or 0)
            if not fid or not fname:
                continue

            if res_type == 2:
                if fname == "保险箱":
                    continue
                sub_dir = os.path.join(base_target_dir, sanitize_fs_name(fname)).replace("\\", "/")
                folder_queue.append((fid, sub_dir))
            else:
                resolved_name = self._resolve_conflict_filename(base_target_dir, fname, conflict_policy)
                if not resolved_name:
                    skipped_count += 1
                    continue
                tid = self._enqueue_single_download(fid, resolved_name, base_target_dir, file_size=fsize)
                queued_ids.append(tid)

        # Process up to 5 folders synchronously so the user immediately sees queued tasks,
        # and if more nested folders remain, continue expanding in background
        sync_folders_processed = 0
        while folder_queue and sync_folders_processed < 5:
            curr_fid, curr_local_dir = folder_queue.pop(0)
            sync_folders_processed += 1
            try:
                children = self.client.list_all_files(parent_id=curr_fid)
                for child in children:
                    cfid = str(child.get("fileId") or "").strip()
                    cfname = str(child.get("fileName") or "").strip()
                    ctype = int(child.get("resType") or 1)
                    csize = int(child.get("fileSize") or 0)
                    if not cfid or not cfname:
                        continue
                    if ctype == 2:
                        if cfname == "保险箱":
                            continue
                        next_local = os.path.join(curr_local_dir, sanitize_fs_name(cfname)).replace("\\", "/")
                        folder_queue.append((cfid, next_local))
                    else:
                        resolved_name = self._resolve_conflict_filename(curr_local_dir, cfname, conflict_policy)
                        if not resolved_name:
                            skipped_count += 1
                            continue
                        tid = self._enqueue_single_download(cfid, resolved_name, curr_local_dir, file_size=csize)
                        queued_ids.append(tid)
            except Exception as ex:
                print("Error scanning folder for batch download:", ex)

        self._schedule_downloads()
        self.log("INFO", "download", f"📦 批量提交下载任务: 已加入排队 {len(queued_ids)} 个文件", f"跳过已存在: {skipped_count} | 目标目录: {base_target_dir}")

        if folder_queue:
            remaining = list(folder_queue)
            def bg_expand():
                q = remaining
                while q:
                    curr_fid, curr_local_dir = q.pop(0)
                    try:
                        children = self.client.list_all_files(parent_id=curr_fid)
                        added_any = False
                        for child in children:
                            cfid = str(child.get("fileId") or "").strip()
                            cfname = str(child.get("fileName") or "").strip()
                            ctype = int(child.get("resType") or 1)
                            csize = int(child.get("fileSize") or 0)
                            if not cfid or not cfname:
                                continue
                            if ctype == 2:
                                if cfname == "保险箱":
                                    continue
                                next_local = os.path.join(curr_local_dir, sanitize_fs_name(cfname)).replace("\\", "/")
                                q.append((cfid, next_local))
                            else:
                                resolved_name = self._resolve_conflict_filename(curr_local_dir, cfname, conflict_policy)
                                if not resolved_name:
                                    continue
                                self._enqueue_single_download(cfid, resolved_name, curr_local_dir, file_size=csize)
                                added_any = True
                        if added_any:
                            self._schedule_downloads()
                    except Exception:
                        pass
            threading.Thread(target=bg_expand, daemon=True).start()

        return {
            "success": True,
            "queuedCount": len(queued_ids),
            "skippedCount": skipped_count,
            "taskIds": queued_ids,
            "expandingFolders": len(folder_queue) > 0
        }

    def retry_download_task(self, task_id: str) -> bool:
        fn = ""
        with self.download_queue_lock:
            t = self.downloads.get(task_id)
            if not t:
                return False
            self._task_seq += 1
            t["seq"] = self._task_seq
            t["status"] = "pending"
            t["speed"] = "排队等待中"
            t["eta"] = "--"
            t["error"] = None
            t["finishTime"] = None
            fn = t.get("fileName", "")
        self.log("INFO", "download", f"🔄 重试下载任务: {fn} (ID: {task_id})")
        self._schedule_downloads()
        return True

    def cancel_download_task(self, task_id: str):
        if task_id in self.downloads:
            fn = self.downloads[task_id].get("fileName", "")
            self.downloads[task_id]["status"] = "cancelled"
            self.downloads[task_id]["speed"] = "已取消"
            self.downloads[task_id]["eta"] = "--"
            self.downloader.cancel_task(task_id)
            self.log("WARN", "download", f"⏹️ 取消下载任务: {fn} (ID: {task_id})")
            self._schedule_downloads()

    def cancel_all_downloads(self) -> int:
        to_kill = []
        with self.download_queue_lock:
            for tid, t in self.downloads.items():
                if t.get("status") in ("pending", "getting_url", "downloading"):
                    t["status"] = "cancelled"
                    t["speed"] = "已取消"
                    t["eta"] = "--"
                    to_kill.append(tid)
        for tid in to_kill:
            self.downloader.cancel_task(tid)
        self.log("WARN", "download", f"⏹️ 取消全部下载任务: 共终止 {len(to_kill)} 个任务")
        return len(to_kill)

    def _remove_local_task_files(self, file_path: str) -> bool:
        if not file_path:
            return False
        removed = False
        for p in (file_path, file_path + ".aria2"):
            try:
                if os.path.isfile(p) or os.path.islink(p):
                    os.remove(p)
                    removed = True
            except Exception as e:
                print(f"Failed to remove local file {p}: {e}")
        return removed

    def delete_download_task(self, task_id: str, delete_file: bool = False) -> dict:
        task = None
        was_running = False
        if task_id in self.downloads:
            was_running = self.downloads[task_id].get("status") in ("downloading", "getting_url")
            self.downloader.cancel_task(task_id)
            with self.download_queue_lock:
                task = self.downloads.pop(task_id, None)
            self._schedule_downloads()
        file_deleted = False
        if task and delete_file:
            if was_running:
                time.sleep(0.2)
            file_deleted = self._remove_local_task_files(task.get("path", ""))
        self.log("INFO", "download", f"🗑️ 移除下载任务: {task.get('fileName', '') if task else task_id}" + (" (已同步删除本地文件)" if file_deleted else ""))
        return {"deleted": bool(task), "fileDeleted": file_deleted}

    def clear_completed_tasks(self, delete_file: bool = False) -> dict:
        removed_tasks = []
        with self.download_queue_lock:
            to_del = [tid for tid, t in self.downloads.items() if t["status"] in ("completed", "failed", "cancelled")]
            for tid in to_del:
                t = self.downloads.pop(tid, None)
                if t:
                    removed_tasks.append(t)
        files_deleted = 0
        if delete_file:
            for t in removed_tasks:
                if self._remove_local_task_files(t.get("path", "")):
                    files_deleted += 1
        self.log("INFO", "download", f"🧹 清理已结束下载任务: 共 {len(removed_tasks)} 项" + (f" (删除本地文件 {files_deleted} 个)" if files_deleted else ""))
        return {"cleared": len(removed_tasks), "filesDeleted": files_deleted}

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>光鸭网盘 · 飞牛私有云 (fnOS) 官方适配版</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico?v=1.1.1">
  <link rel="icon" type="image/png" sizes="32x32" href="/icon.png?v=1.1.1">
  <link rel="apple-touch-icon" href="/icon.png?v=1.1.1">
  <script src="/qrcode.min.js"></script>
  <style>
    :root {
      /* Official Guangyapan Brand Colors */
      --brand: #ff6800;
      --brand-hover: #f06200;
      --brand-active: #ea4e00;
      --brand-alpha-3: rgba(255, 104, 0, 0.03);
      --brand-alpha-8: rgba(255, 104, 0, 0.08);
      --brand-alpha-15: rgba(255, 104, 0, 0.15);
      --brand-alpha-25: rgba(255, 104, 0, 0.25);
      --vip-gold: #f59e0b;
      --vip-gradient: linear-gradient(135deg, #ffd043 0%, #ff6800 100%);

      /* Light Theme (Official Default) */
      --bg-body: #f6f6f6;
      --bg-header: #ffffff;
      --bg-rail: #ffffff;
      --bg-card: #ffffff;
      --bg-subtle: #f9f9f9;
      --bg-hover: #f5f7fc;
      --bg-active: #fff6ec;
      --bg-input: #f3f3f3;
      --border: #e5e6eb;
      --border-subtle: #f0f0f2;
      --border-focus: #ff6800;

      --text-main: #222222;
      --text-regular: #444444;
      --text-secondary: #666666;
      --text-muted: #888888;
      --text-placeholder: #999999;
      --text-white: #ffffff;

      --btn-secondary-bg: #f3f3f3;
      --btn-secondary-hover: #e8e8e8;

      --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.04);
      --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.06);
      --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.10);
      --shadow-brand: 0 4px 12px rgba(255, 104, 0, 0.25);

      --radius-xs: 4px;
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --radius-full: 9999px;

      /* Variable Aliases */
      --primary: var(--brand);
      --text: var(--text-main);
      --text-dim: var(--text-muted);
    }

    body[data-theme="dark"] {
      --bg-body: #141414;
      --bg-header: #1c1c1c;
      --bg-rail: #1c1c1c;
      --bg-card: #202020;
      --bg-subtle: #252525;
      --bg-hover: #2a2a2a;
      --bg-active: #2a2018;
      --bg-input: #2a2a2a;
      --border: #333333;
      --border-subtle: #292929;
      --border-focus: #ff6800;

      --text-main: #f3f4f6;
      --text-regular: #e5e7eb;
      --text-secondary: #9ca3af;
      --text-muted: #6b7280;
      --text-placeholder: #4b5563;
      --text-white: #ffffff;

      --btn-secondary-bg: #2a2a2a;
      --btn-secondary-hover: #333333;

      --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.3);
      --shadow-md: 0 4px 16px rgba(0, 0, 0, 0.4);
      --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.5);
      --shadow-brand: 0 4px 12px rgba(255, 104, 0, 0.3);

      /* Variable Aliases */
      --primary: var(--brand);
      --text: var(--text-main);
      --text-dim: var(--text-muted);
    }

    *,
    *::before,
    *::after,
    html,
    body,
    input,
    textarea,
    button,
    select,
    table,
    th,
    td,
    div,
    span,
    p,
    a,
    label {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif !important;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background: var(--bg-body);
      color: var(--text-main);
      height: 100vh;
      overflow: hidden;
      display: flex;
    }

    /* Overall Layout */
    #app-container {
      display: flex;
      width: 100vw;
      height: 100vh;
      overflow: hidden;
    }

    /* Left Navigation Rail (72px) */
    #nav-rail {
      width: 72px;
      background: var(--bg-rail);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 0 0 16px 0;
      user-select: none;
      z-index: 100;
      flex-shrink: 0;
      transition: background 0.2s, border-color 0.2s;
    }

    .rail-top {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 56px;
    }

    .rail-brand-logo {
      width: 38px;
      height: 38px;
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.2s;
    }
    .rail-brand-logo:hover {
      transform: scale(1.06);
    }

    .rail-menu {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      width: 100%;
      padding: 0 8px;
      margin-top: 21px;
      flex: 1;
    }

    .rail-btn {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      width: 56px;
      height: 52px;
      border-radius: var(--radius-md);
      cursor: pointer;
      color: var(--text-secondary);
      transition: all 0.18s ease;
      position: relative;
      text-decoration: none;
    }

    .rail-btn:hover {
      background: var(--bg-hover);
      color: var(--brand);
    }

    .rail-btn.active {
      background: var(--brand-alpha-8);
      color: var(--brand);
      font-weight: 600;
    }

    .rail-btn-icon {
      width: 22px;
      height: 22px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 3px;
    }

    .rail-btn-label {
      font-size: 11px;
      line-height: 1;
      letter-spacing: -0.2px;
    }

    .rail-badge {
      position: absolute;
      top: 5px;
      right: 7px;
      background: var(--brand);
      color: #fff;
      font-size: 10px;
      font-weight: 700;
      padding: 1px 5px;
      border-radius: 10px;
      min-width: 16px;
      height: 16px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 1px 3px rgba(255, 104, 0, 0.4);
    }

    .rail-badge.dot {
      width: 8px;
      height: 8px;
      min-width: 8px;
      padding: 0;
      border-radius: 50%;
      top: 6px;
      right: 12px;
    }

    .rail-bottom {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      width: 100%;
      margin-top: auto;
    }

    .rail-theme-btn {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: var(--bg-input);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
      padding: 0;
      overflow: hidden;
      white-space: nowrap;
      user-select: none;
      transition: all 0.2s;
    }
    .rail-theme-btn:hover {
      color: var(--brand);
      border-color: var(--brand);
      transform: rotate(20deg);
    }

    /* Right Main Wrapper */
    #main-wrapper {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
      height: 100vh;
      background: var(--bg-body);
      overflow: hidden;
      transition: background 0.2s;
    }

    /* Top Header (56px) */
    #top-header {
      height: 56px;
      background: var(--bg-header);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      z-index: 90;
      flex-shrink: 0;
      transition: background 0.2s, border-color 0.2s;
    }

    .header-left {
      display: flex;
      align-items: center;
      gap: 14px;
      flex-shrink: 0;
    }

    .brand-full-logo {
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      text-decoration: none;
      color: var(--text-main);
      user-select: none;
    }

    .brand-duck-icon {
      width: 30px;
      height: 30px;
      border-radius: 7px;
      object-fit: cover;
    }

    .brand-txt-svg {
      height: 22px;
      width: auto;
      color: var(--text-main);
    }
    body[data-theme="dark"] .brand-txt-svg {
      color: var(--text-main);
    }

    .fnos-badge {
      font-size: 11px;
      font-weight: 600;
      background: rgba(59, 130, 246, 0.1);
      color: #3b82f6;
      border: 1px solid rgba(59, 130, 246, 0.25);
      padding: 1px 7px;
      border-radius: 4px;
      margin-left: 2px;
    }

    /* Category Sub-navigation Tabs (kj) */
    .header-center {
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 0 16px;
      min-width: 0;
    }

    .cat-tabs {
      display: flex;
      align-items: center;
      gap: 4px;
      background: var(--bg-input);
      padding: 3px 4px;
      border-radius: var(--radius-md);
      overflow-x: auto;
      scrollbar-width: none;
    }
    .cat-tabs::-webkit-scrollbar { display: none; }

    .cat-tab-item {
      padding: 5px 14px;
      border-radius: var(--radius-sm);
      font-size: 13px;
      font-weight: 500;
      color: var(--text-secondary);
      cursor: pointer;
      transition: all 0.18s;
      white-space: nowrap;
      user-select: none;
    }

    .cat-tab-item:hover {
      color: var(--text-main);
    }

    .cat-tab-item.active {
      background: var(--bg-card);
      color: var(--brand);
      font-weight: 600;
      box-shadow: var(--shadow-sm);
    }

    /* Header Right Capsule Tools */
    .header-right {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
    }

    .storage-capsule {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-input);
      padding: 6px 12px;
      border-radius: var(--radius-full);
      font-size: 12px;
      color: var(--text-secondary);
      border: 1px solid var(--border-subtle);
    }

    .storage-bar-wrap {
      width: 50px;
      height: 4px;
      background: rgba(150, 150, 150, 0.2);
      border-radius: 2px;
      overflow: hidden;
    }

    .storage-bar-fill {
      width: 0%;
      height: 100%;
      background: var(--vip-gradient);
      border-radius: 2px;
      transition: width 0.3s;
    }

    .user-profile-capsule {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-input);
      padding: 4px 10px 4px 5px;
      border-radius: var(--radius-full);
      cursor: pointer;
      border: 1px solid var(--border-subtle);
      position: relative;
      transition: all 0.2s;
    }
    .user-profile-capsule:hover {
      border-color: var(--brand-alpha-25);
    }

    .user-avatar-circle {
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: var(--brand-alpha-15);
      color: var(--brand);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      border: 1.5px solid #ffd043;
    }

    .user-name-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-main);
      max-width: 90px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .vip-tag {
      font-size: 10px;
      font-weight: 700;
      background: var(--vip-gradient);
      color: #ffffff;
      padding: 1px 6px;
      border-radius: 10px;
      line-height: 1.2;
    }

    .user-dropdown-menu {
      position: absolute;
      top: 110%;
      right: 0;
      width: 220px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      box-shadow: var(--shadow-lg);
      padding: 8px;
      display: none;
      flex-direction: column;
      gap: 4px;
      z-index: 1000;
    }
    .user-profile-capsule:hover .user-dropdown-menu {
      display: flex;
    }

    .user-dropdown-item {
      padding: 8px 12px;
      border-radius: var(--radius-sm);
      font-size: 13px;
      color: var(--text-regular);
      display: flex;
      align-items: center;
      gap: 8px;
      transition: background 0.15s;
    }
    .user-dropdown-item:hover {
      background: var(--bg-hover);
      color: var(--brand);
    }
    .user-dropdown-item.danger:hover {
      background: #fee2e2;
      color: #ef4444;
    }

    /* Views Container */
    #view-container {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
      padding: 16px 20px 20px 20px;
    }

    .content-view {
      display: none;
      flex: 1;
      flex-direction: column;
      min-height: 0;
      overflow: hidden;
    }
    .content-view.active {
      display: flex;
    }

    /* Card Panels */
    .official-card {
      background: var(--bg-card);
      border-radius: var(--radius-lg);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-sm);
      display: flex;
      flex-direction: column;
      flex: 1;
      min-height: 0;
      overflow: hidden;
    }

    /* File Explorer Toolbar */
    .file-toolbar {
      padding: 12px 18px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-shrink: 0;
    }

    .toolbar-left, .toolbar-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    /* Button Styles */
    button {
      font-family: inherit;
      white-space: nowrap;
      user-select: none;
      box-sizing: border-box;
    }

    .btn-primary, .btn-guangya-orange {
      background: var(--brand);
      color: #ffffff !important;
      border: 1px solid transparent;
      border-radius: var(--radius-sm);
      height: 36px;
      padding: 0 16px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      box-shadow: var(--shadow-brand);
      transition: all 0.18s;
      white-space: nowrap;
    }
    .btn-primary:hover, .btn-guangya-orange:hover {
      background: var(--brand-hover);
      transform: translateY(-1px);
    }
    .btn-primary:active, .btn-guangya-orange:active {
      background: var(--brand-active);
      transform: translateY(0);
    }

    .btn-secondary {
      background: var(--btn-secondary-bg);
      color: var(--text-main) !important;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      height: 36px;
      padding: 0 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: all 0.18s;
      white-space: nowrap;
    }
    .btn-secondary:hover {
      background: var(--btn-secondary-hover);
      color: var(--brand) !important;
      border-color: var(--brand-alpha-25);
    }

    .btn-outline {
      background: transparent;
      color: var(--text-regular) !important;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      height: 36px;
      padding: 0 12px;
      font-size: 13px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: all 0.18s;
      white-space: nowrap;
    }
    .btn-outline:hover {
      background: var(--bg-hover);
      color: var(--brand) !important;
      border-color: var(--brand-alpha-25);
    }

    .btn-icon {
      width: 34px;
      height: 34px;
      padding: 0;
      border-radius: var(--radius-sm);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-secondary);
      cursor: pointer;
      transition: all 0.18s;
    }
    .btn-icon:hover {
      background: var(--bg-hover);
      color: var(--text-main);
    }
    .btn-icon.active {
      background: var(--bg-input);
      color: var(--brand);
      border-color: var(--border);
    }

    .btn-sm {
      height: 28px !important;
      padding: 0 10px !important;
      font-size: 12px !important;
      border-radius: 6px !important;
      line-height: 26px !important;
      white-space: nowrap !important;
    }

    .btn-danger {
      background: #fee2e2;
      color: #ef4444 !important;
      border: 1px solid rgba(239, 68, 68, 0.2);
      border-radius: var(--radius-sm);
      height: 36px;
      padding: 0 14px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: all 0.18s;
      white-space: nowrap;
    }
    .btn-danger:hover {
      background: #ef4444;
      color: #ffffff !important;
    }

    /* Search Box */
    .search-box {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-input);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-full);
      padding: 0 14px;
      height: 36px;
      width: 200px;
      transition: all 0.2s;
    }
    .search-box:focus-within {
      width: 240px;
      border-color: var(--brand);
      background: var(--bg-card);
      box-shadow: 0 0 0 3px var(--brand-alpha-15);
    }
    .search-box input {
      border: none;
      background: transparent;
      color: var(--text-main);
      font-size: 13px;
      outline: none;
      width: 100%;
    }

    /* Breadcrumbs Bar */
    .file-breadcrumbs {
      padding: 9px 18px;
      background: var(--bg-subtle);
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      color: var(--text-secondary);
      flex-shrink: 0;
      overflow-x: auto;
    }

    .crumb-item {
      cursor: pointer;
      transition: color 0.15s;
      white-space: nowrap;
    }
    .crumb-item:hover {
      color: var(--brand);
    }
    .crumb-item.current {
      color: var(--text-main);
      font-weight: 600;
      cursor: default;
    }
    .crumb-sep {
      color: var(--text-muted);
      font-size: 12px;
      user-select: none;
    }

    /* Table Container */
    .table-container {
      flex: 1;
      overflow-y: auto;
      min-height: 0;
    }

    /* File Pagination Bar */
    .file-pagination-bar {
      flex-shrink: 0;
      border-top: 1px solid var(--border-subtle);
      padding: 10px 18px;
      background: var(--bg-card);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      font-size: 12.5px;
      color: var(--text-secondary);
      user-select: none;
    }

    .page-btn {
      min-width: 30px;
      height: 30px;
      padding: 0 8px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--bg-input);
      color: var(--text-secondary);
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s;
    }
    .page-btn:hover:not(:disabled) {
      border-color: var(--brand);
      color: var(--brand);
      background: var(--bg-hover);
    }
    .page-btn.active {
      background: var(--brand);
      border-color: var(--brand);
      color: #fff;
      font-weight: 600;
    }
    .page-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      text-align: left;
    }

    thead th {
      position: sticky;
      top: 0;
      background: var(--bg-card);
      color: var(--text-muted);
      font-weight: 500;
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-subtle);
      user-select: none;
      z-index: 2;
    }

    tbody tr {
      border-bottom: 1px solid var(--border-subtle);
      transition: background 0.15s;
    }
    tbody tr:hover {
      background: var(--bg-hover);
    }

    tbody td {
      padding: 12px 16px;
      color: var(--text-regular);
      vertical-align: middle;
    }

    .file-name-cell {
      display: flex;
      align-items: center;
      gap: 12px;
      cursor: pointer;
      min-width: 0;
    }

    .file-icon {
      font-size: 20px;
      width: 24px;
      text-align: center;
      flex-shrink: 0;
    }

    .file-name-text {
      font-weight: 500;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      transition: color 0.15s;
    }
    .file-name-cell:hover .file-name-text {
      color: var(--brand);
    }

    .actions-cell {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      opacity: 0.85;
      white-space: nowrap;
      flex-wrap: nowrap;
      min-width: 170px;
      transition: opacity 0.15s;
    }
    tbody tr:hover .actions-cell {
      opacity: 1;
    }

    /* Empty State */
    .empty-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 60px 20px;
      color: var(--text-secondary);
      user-select: none;
    }

    .empty-duck-img {
      width: 140px;
      height: 140px;
      object-fit: contain;
      margin-bottom: 14px;
      opacity: 0.9;
    }

    .empty-title {
      font-size: 15px;
      font-weight: 600;
      color: var(--text-main);
      margin-bottom: 6px;
    }

    .empty-sub {
      font-size: 13px;
      color: var(--text-muted);
    }

    /* Subtabs inside Views */
    .subtabs {
      padding: 14px 18px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      background: var(--bg-card);
    }

    .subtabs-left {
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .subtab-btn {
      padding: 6px 14px;
      border-radius: var(--radius-sm);
      font-size: 13px;
      font-weight: 500;
      background: var(--bg-input);
      color: var(--text-secondary);
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.18s;
    }
    .subtab-btn:hover {
      color: var(--text-main);
    }
    .subtab-btn.active {
      background: var(--brand-alpha-8);
      color: var(--brand);
      border-color: var(--brand-alpha-15);
      font-weight: 600;
    }

    /* Task Cards / Lists */
    .task-list-container {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }

    .task-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      transition: all 0.18s;
    }
    .task-card:hover {
      box-shadow: var(--shadow-sm);
      border-color: var(--brand-alpha-25);
    }

    .task-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .task-title {
      font-size: 14px;
      font-weight: 600;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .task-meta {
      display: flex;
      align-items: center;
      gap: 14px;
      font-size: 12px;
      color: var(--text-muted);
    }

    .progress-bar-wrap {
      width: 100%;
      height: 6px;
      background: var(--bg-input);
      border-radius: 3px;
      overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%;
      background: var(--vip-gradient);
      border-radius: 3px;
      transition: width 0.2s;
    }

    /* Settings Page Grid */
    .settings-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 16px;
      padding: 18px;
      overflow-y: auto;
    }

    .setting-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      box-shadow: var(--shadow-sm);
    }

    .setting-card-title {
      font-size: 15px;
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .setting-card-desc {
      font-size: 13px;
      color: var(--text-secondary);
      line-height: 1.5;
    }

    /* Modal Backdrop & Popup */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.55);
      backdrop-filter: blur(4px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }

    .modal {
      background: var(--bg-card);
      border-radius: var(--radius-lg);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-lg);
      display: flex;
      flex-direction: column;
      max-height: 90vh;
      overflow: hidden;
      animation: modalPop 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }

    @keyframes modalPop {
      0% { transform: scale(0.96); opacity: 0; }
      100% { transform: scale(1); opacity: 1; }
    }

    .modal-header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }

    .modal-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .modal-body {
      padding: 20px 24px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
      font-size: 13px;
      color: var(--text-regular);
      box-sizing: border-box;
    }

    .modal-footer {
      padding: 14px 24px;
      border-top: 1px solid var(--border-subtle);
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      background: var(--bg-subtle);
      flex-shrink: 0;
    }

    /* Form Controls */
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .form-label {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-secondary);
    }
    .form-input {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: 8px 12px;
      color: var(--text-main);
      font-size: 13px;
      outline: none;
      transition: all 0.2s;
    }
    .form-input:focus {
      border-color: var(--brand);
      background: var(--bg-card);
      box-shadow: 0 0 0 3px var(--brand-alpha-15);
    }

    /* Form Inputs & Textarea */
    input[type="text"],
    input[type="password"],
    input[type="search"],
    textarea,
    select {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text-main);
      padding: 8px 12px;
      font-size: 13px;
      outline: none;
      box-sizing: border-box;
      transition: all 0.2s;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--brand);
      background: var(--bg-card);
      box-shadow: 0 0 0 3px var(--brand-alpha-15);
    }

    /* Modal Close Button */
    .close-btn {
      background: transparent;
      border: none;
      font-size: 20px;
      color: var(--text-muted);
      cursor: pointer;
      line-height: 1;
      padding: 4px;
      border-radius: 4px;
      transition: all 0.15s;
    }
    .close-btn:hover {
      color: var(--text-main);
      background: var(--bg-hover);
    }

    /* Dropzone for torrent & upload */
    .dropzone {
      border: 2px dashed var(--border);
      border-radius: var(--radius-md);
      background: var(--bg-input);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px 16px;
      cursor: pointer;
      text-align: center;
      transition: all 0.2s;
    }
    .dropzone:hover {
      border-color: var(--brand);
      background: var(--bg-hover);
    }

    /* Cloud Add Step 2 File Row */
    .cloud-file-row {
      display: grid;
      grid-template-columns: 1fr 65px 85px;
      align-items: center;
      padding: 9px 12px;
      border-bottom: 1px solid var(--border-subtle);
      cursor: pointer;
      transition: background 0.15s;
      font-size: 13px;
    }
    .cloud-file-row:hover {
      background: var(--bg-hover);
    }

    /* Cloud Destination Folder Pill */
    .cloud-dest-pill {
      display: flex;
      align-items: center;
      gap: 8px;
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 6px 12px;
      font-size: 13px;
      color: var(--text-main);
      cursor: pointer;
      max-width: 380px;
      transition: all 0.18s;
    }
    .cloud-dest-pill:hover {
      border-color: var(--brand);
      background: var(--bg-hover);
    }

    /* Cloud Drive Destination Folder Tree */
    .cloud-tree-container {
      min-height: 220px;
      max-height: 320px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--bg-card);
      padding: 6px;
    }
    .cloud-tree-node {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 13px;
      color: var(--text-regular);
      transition: background 0.15s;
      user-select: none;
    }
    .cloud-tree-node:hover {
      background: var(--bg-hover);
      color: var(--text-main);
    }
    .cloud-tree-node.selected {
      background: var(--brand-alpha-8);
      color: var(--brand);
      font-weight: 600;
    }
    .cloud-tree-arrow {
      width: 14px;
      font-size: 10px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }
    .cloud-tree-icon {
      font-size: 15px;
      flex-shrink: 0;
    }
    .cloud-tree-name {
      flex: 1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .cloud-tree-check {
      color: var(--brand);
      font-weight: 700;
      font-size: 13px;
      margin-left: auto;
    }
    .cloud-tree-tag-default {
      font-size: 10px;
      background: var(--brand-alpha-15);
      color: var(--brand);
      border: 1px solid var(--brand-alpha-25);
      border-radius: 4px;
      padding: 1px 5px;
      font-weight: 600;
    }

    /* NAS Folder Picker List */
    .fs-browser-list {
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      background: var(--bg-card);
      overflow-y: auto;
      padding: 4px;
    }
    .fs-folder-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 7px 10px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 13px;
      color: var(--text-regular);
      transition: background 0.15s;
      margin-bottom: 2px;
    }
    .fs-folder-item:hover {
      background: var(--bg-hover);
      color: var(--text-main);
    }
    .fs-folder-item.selected {
      background: var(--brand-alpha-8);
      color: var(--brand);
      border: 1px solid var(--brand-alpha-25);
      font-weight: 600;
    }
    .fs-folder-left {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Toast */
    #toast {
      position: fixed;
      bottom: 28px;
      left: 50%;
      transform: translateX(-50%) translateY(20px);
      background: rgba(30, 30, 30, 0.95);
      color: #ffffff;
      padding: 10px 20px;
      border-radius: var(--radius-full);
      font-size: 13px;
      font-weight: 500;
      box-shadow: var(--shadow-lg);
      z-index: 2000;
      opacity: 0;
      pointer-events: none;
      transition: all 0.22s cubic-bezier(0.16, 1, 0.3, 1);
    }
    #toast.show {
      transform: translateX(-50%) translateY(0);
      opacity: 1;
    }

    /* Mobile Responsive Adaptation */
    @media (max-width: 768px) {
      #app-container {
        flex-direction: column;
      }
      #nav-rail {
        order: 2;
        width: 100vw;
        height: 56px;
        flex-direction: row;
        border-right: none;
        border-top: 1px solid var(--border);
        padding: 0 12px;
      }
      .rail-top, .rail-bottom {
        display: none;
      }
      .rail-menu {
        flex-direction: row;
        justify-content: space-around;
        width: 100%;
        padding: 0;
      }
      .rail-btn {
        width: 50px;
        height: 48px;
        margin-bottom: 0;
      }
      #main-wrapper {
        order: 1;
        height: calc(100vh - 56px);
      }
      #top-header {
        padding: 0 14px;
      }
      .header-center {
        display: none;
      }
      .storage-capsule {
        display: none;
      }
      #view-container {
        padding: 10px;
      }
      .file-toolbar {
        flex-direction: column;
        align-items: stretch;
      }
      .toolbar-left, .toolbar-right {
        width: 100%;
        justify-content: space-between;
      }
      .search-box {
        width: 100% !important;
      }
    }

    /* Log Console Styles */
    .log-console-wrap {
      flex: 1;
      overflow-y: auto;
      min-height: 0;
      background: var(--bg-subtle);
      padding: 12px 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace !important;
      font-size: 12.5px;
      line-height: 1.6;
    }
    .log-console-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .log-entry {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 6px 10px;
      border-radius: var(--radius-xs);
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      transition: background 0.15s;
      word-break: break-all;
    }
    .log-entry:hover {
      background: var(--bg-hover);
    }
    .log-entry-time {
      color: var(--text-muted);
      flex-shrink: 0;
      font-size: 11.5px;
      user-select: none;
    }
    .log-entry-cat {
      display: inline-flex;
      align-items: center;
      padding: 1px 6px;
      border-radius: var(--radius-xs);
      font-size: 11px;
      font-weight: 600;
      flex-shrink: 0;
      user-select: none;
    }
    .log-cat-download { background: rgba(59, 130, 246, 0.12); color: #2563eb; }
    .log-cat-cloud { background: rgba(168, 85, 247, 0.12); color: #9333ea; }
    .log-cat-strm { background: rgba(249, 115, 22, 0.12); color: #ea580c; }
    .log-cat-system { background: rgba(107, 114, 128, 0.12); color: #4b5563; }
    body[data-theme="dark"] .log-cat-download { background: rgba(59, 130, 246, 0.25); color: #60a5fa; }
    body[data-theme="dark"] .log-cat-cloud { background: rgba(168, 85, 247, 0.25); color: #c084fc; }
    body[data-theme="dark"] .log-cat-strm { background: rgba(249, 115, 22, 0.25); color: #fb923c; }
    body[data-theme="dark"] .log-cat-system { background: rgba(107, 114, 128, 0.25); color: #9ca3af; }

    .log-entry-level {
      display: inline-flex;
      align-items: center;
      padding: 1px 6px;
      border-radius: var(--radius-xs);
      font-size: 11px;
      font-weight: 700;
      flex-shrink: 0;
      user-select: none;
    }
    .log-level-info { background: rgba(14, 165, 233, 0.12); color: #0284c7; }
    .log-level-success { background: rgba(34, 197, 94, 0.12); color: #16a34a; }
    .log-level-warn { background: rgba(234, 179, 8, 0.15); color: #ca8a04; }
    .log-level-error { background: rgba(239, 68, 68, 0.12); color: #dc2626; }
    body[data-theme="dark"] .log-level-info { background: rgba(14, 165, 233, 0.25); color: #38bdf8; }
    body[data-theme="dark"] .log-level-success { background: rgba(34, 197, 94, 0.25); color: #4ade80; }
    body[data-theme="dark"] .log-level-warn { background: rgba(234, 179, 8, 0.25); color: #facc15; }
    body[data-theme="dark"] .log-level-error { background: rgba(239, 68, 68, 0.25); color: #f87171; }

    .log-entry-msg {
      flex: 1;
      color: var(--text-main);
    }
    .log-entry-details {
      margin-top: 4px;
      padding: 4px 8px;
      background: var(--bg-input);
      border-radius: var(--radius-xs);
      font-size: 11.5px;
      color: var(--text-secondary);
      border-left: 2px solid var(--brand);
    }
    .badge-pill {
      display: inline-block;
      margin-left: 4px;
      padding: 0 6px;
      font-size: 10.5px;
      font-weight: 600;
      border-radius: var(--radius-full);
      background: var(--bg-input);
      color: var(--text-muted);
    }
    .subtab-btn.active .badge-pill {
      background: var(--brand);
      color: #fff;
    }
  </style>
</head>
<body data-theme="light">

<div id="app-container">

  <!-- Left Navigation Rail (72px official dock) -->
  <aside id="nav-rail">
    <div class="rail-top">
      <div class="rail-brand-logo" onclick="switchView('files')" title="光鸭网盘">
        <svg width="34" height="34" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <g clip-path="url(#duck_clip)">
            <path d="M0 7.125C0 3.18997 3.18997 0 7.125 0H24.875C28.81 0 32 3.18997 32 7.125V24.875C32 28.81 28.81 32 24.875 32H7.125C3.18997 32 0 28.81 0 24.875V7.125Z" fill="#FF6800"/>
            <path d="M0 7.125C0 3.18997 3.18997 0 7.125 0H24.875C28.81 0 32 3.18997 32 7.125V24.875C32 28.81 28.81 32 24.875 32H7.125C3.18997 32 0 28.81 0 24.875V7.125Z" fill="url(#duck_radial)" fill-opacity="0.7"/>
            <path d="M17.1816 12.4555C18.0894 12.4555 18.8253 13.1893 18.8253 14.0944C18.8253 14.9995 18.0894 15.7333 17.1816 15.7333C16.2738 15.7333 15.5378 14.9995 15.5378 14.0944C15.5378 13.1893 16.2737 12.4555 17.1816 12.4555Z" fill="white"/>
            <path fill-rule="evenodd" clip-rule="evenodd" d="M16.7571 6.62427C20.7472 6.62427 24.1578 9.09836 25.53 12.5919C25.5793 12.7175 25.6997 12.8022 25.835 12.8022H28.6225C28.7458 12.8022 28.8457 12.9018 28.8457 13.0248C28.8457 13.2536 28.821 13.3611 28.7741 13.576C28.7713 13.5886 28.7685 13.6013 28.7655 13.6138C28.7626 13.6264 28.7596 13.639 28.7566 13.6515C28.7505 13.6765 28.744 13.7015 28.7373 13.7263C28.7306 13.7511 28.7235 13.7758 28.7162 13.8003C28.6979 13.8617 28.6777 13.9223 28.6558 13.982C28.6514 13.994 28.647 14.0059 28.6424 14.0177C28.5836 14.1722 28.5127 14.3207 28.4311 14.4623C28.4248 14.4732 28.4185 14.484 28.4121 14.4948C28.3928 14.5272 28.3731 14.5593 28.3527 14.5909C28.3324 14.6226 28.3114 14.6538 28.29 14.6847C28.267 14.7178 28.2433 14.7503 28.2191 14.7824C28.194 14.8158 28.1681 14.8486 28.1417 14.8808C28.1324 14.8922 28.123 14.9036 28.1135 14.9149C28.0791 14.9557 28.0437 14.9957 28.0073 15.0347C27.9732 15.0712 27.9382 15.1069 27.9024 15.1418C27.8713 15.1721 27.8395 15.2017 27.8072 15.2306C27.7692 15.2646 27.7304 15.2977 27.6908 15.3299C27.6435 15.3683 27.5951 15.4053 27.5456 15.4409C27.3973 15.5477 27.2392 15.6418 27.073 15.7217C27.0616 15.7272 27.0502 15.7326 27.0387 15.7379C26.9615 15.7739 26.8825 15.8067 26.802 15.8364C26.7362 15.8606 26.6694 15.8828 26.6017 15.9027C26.5552 15.9163 26.5082 15.929 26.4608 15.9406C26.4106 15.9528 26.3598 15.9638 26.3087 15.9736C26.2646 15.982 26.2201 15.9894 26.1753 15.996C26.1139 16.005 25.8088 16.0149 25.715 16.0149H22.5902L22.5902 16.0157H23.074C22.6448 16.0355 22.5863 16.4031 22.5659 16.5906L22.5658 16.5893C22.2682 21.5081 18.1731 25.4055 13.165 25.4055C8.07136 25.4055 3.92212 21.3738 3.75213 16.3364C3.74616 16.1593 3.89079 16.0149 4.06849 16.0149H6.83436C7.14554 15.9917 7.31024 15.8661 7.35741 15.3951V15.4213C7.66457 10.5116 11.7555 6.62429 16.7571 6.62427ZM16.7571 10.2183C13.6543 10.2183 11.1192 12.6418 10.9522 15.694C10.9425 15.8709 10.7994 16.0149 10.6217 16.0149H7.68982C7.43331 16.0335 7.37241 16.2308 7.36201 16.3701C7.54605 19.4061 10.0738 21.8115 13.165 21.8115C16.2678 21.8115 18.8029 19.388 18.9699 16.3358C18.9796 16.1588 19.1227 16.0149 19.3004 16.0149H22.1534C22.4713 15.9561 22.5506 15.6831 22.5486 15.5052C22.2897 12.5425 19.7956 10.2183 16.7571 10.2183Z" fill="white"/>
          </g>
          <defs>
            <radialGradient id="duck_radial" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(26.8438 0.593749) rotate(127.312) scale(24.282)">
              <stop stop-color="#FFC30E"/>
              <stop offset="1" stop-color="#FFA70E" stop-opacity="0"/>
            </radialGradient>
            <clipPath id="duck_clip">
              <rect width="32" height="32" rx="7" fill="white"/>
            </clipPath>
          </defs>
        </svg>
      </div>
    </div>

    <!-- Middle Menu Items (4 Core Buttons) -->
    <div class="rail-menu">
      <div class="rail-btn active" id="navFiles" onclick="switchView('files')" title="全部文件">
        <div class="rail-btn-icon">
          <!-- Official TabbarHome SVG -->
          <svg width="20" height="20" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M6.782 1.196a3.75 3.75 0 0 1 4.436 0l4.5 3.3A3.75 3.75 0 0 1 17.25 7.52v5.98a3.75 3.75 0 0 1-3.75 3.75h-9A3.75 3.75 0 0 1 .75 13.5V7.52c0-1.194.57-2.318 1.532-3.024zM9 9.25a.75.75 0 0 0-.75.75v3a.75.75 0 0 0 1.5 0v-3A.75.75 0 0 0 9 9.25"/>
          </svg>
        </div>
        <span class="rail-btn-label">首页</span>
      </div>

      <div class="rail-btn" id="navOffline" onclick="switchView('offline')" title="云添加 / 离线任务">
        <div class="rail-btn-icon">
          <!-- Official TabbarAddcloud SVG -->
          <svg width="20" height="20" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M8.95 1.25a5.75 5.75 0 0 1 5.64 4.64 5.75 5.75 0 0 1-2.64 10.86h-6A5.75 5.75 0 0 1 .2 11 5.75 5.75 0 0 1 3.31 5.89 5.75 5.75 0 0 1 8.95 1.25m1.332 6.874a1.75 1.75 0 0 0-2.564 0L6.45 9.489a.75.75 0 0 0 1.1 1.02l.7-.754V12.5a.75.75 0 0 0 1.5 0V9.755l.7.755a.75.75 0 0 0 1.1-1.02z"/>
          </svg>
        </div>
        <span class="rail-btn-label">云添加</span>
        <span class="rail-badge" id="cloudTaskBadge" style="display: none;">0</span>
      </div>

      <div class="rail-btn" id="navTransfers" onclick="switchView('transfers')" title="传输与下载任务">
        <div class="rail-btn-icon">
          <!-- Official Download / Transfer SVG -->
          <svg width="20" height="20" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path fill="currentColor" d="M2.5 13.5a.75.75 0 0 0 0 1.5h13a.75.75 0 0 0 0-1.5zM9 2.25a.75.75 0 0 1 .75.75v6.44l2.22-2.22a.75.75 0 1 1 1.06 1.06l-3.5 3.5a.75.75 0 0 1-1.06 0l-3.5-3.5a.75.75 0 1 1 1.06-1.06l2.22 2.22V3a.75.75 0 0 1 .75-.75"/>
          </svg>
        </div>
        <span class="rail-btn-label">传输</span>
        <span class="rail-badge" id="activeDlBadge" style="display: none;">0</span>
      </div>

      <div class="rail-btn" id="navStrm" onclick="switchView('strm')" title="STRM 影视库与 302 播放网关">
        <div class="rail-btn-icon">
          <!-- Official Clapperboard / Stream SVG -->
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="2" y="3" width="20" height="18" rx="3"/>
            <line x1="2" y1="8" x2="22" y2="8"/>
            <line x1="7" y1="3" x2="5" y2="8"/>
            <line x1="13" y1="3" x2="11" y2="8"/>
            <line x1="19" y1="3" x2="17" y2="8"/>
            <polygon points="10 11.5 16 14.5 10 17.5 10 11.5" fill="currentColor"/>
          </svg>
        </div>
        <span class="rail-btn-label">STRM</span>
        <span class="rail-badge dot" id="strmRunningBadge" style="display: none;"></span>
      </div>

      <div class="rail-btn" id="navLogs" onclick="switchView('logs')" title="系统运行日志">
        <div class="rail-btn-icon">
          <!-- Document / Terminal Logs SVG -->
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
        </div>
        <span class="rail-btn-label">日志</span>
        <span class="rail-badge dot" id="logErrorBadge" style="display: none; background: #ef4444;"></span>
      </div>

      <div class="rail-btn" id="navTrash" onclick="switchView('trash')" title="回收站">
        <div class="rail-btn-icon">
          <!-- Official TabbarRecycle SVG -->
          <svg width="20" height="20" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M16.5 3.25a.75.75 0 0 1 0 1.5h-.765l-1.164 9.439a3.76 3.76 0 0 1-3.733 3.311H7.133a3.76 3.76 0 0 1-3.735-3.322L2.264 4.75H1.5a.75.75 0 0 1 0-1.5zM7.241 9.613a.6.6 0 0 0-.833.16l-.686 1.01a1.1 1.1 0 0 0 .92 1.717l1.445-.014a.601.601 0 0 0-.012-1.2l-1.254.012.5"/>
          </svg>
        </div>
        <span class="rail-btn-label">回收站</span>
      </div>

      <div class="rail-btn" id="navSettings" onclick="switchView('settings')" title="设置">
        <div class="rail-btn-icon">
          <!-- Official TabbarSetting SVG -->
          <svg width="20" height="20" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M10.527 1.25a4.75 4.75 0 0 1 4.07 2.302l1.805 3a4.75 4.75 0 0 1 0 4.896l-1.804 3a4.75 4.75 0 0 1-4.07 2.302H7.472a4.75 4.75 0 0 1-4.07-2.302l-1.805-3a4.75 4.75 0 0 1 0-4.896l1.804-3a4.75 4.75 0 0 1 4.07-2.302zM9 6.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 0 0 0-5.5"/>
          </svg>
        </div>
        <span class="rail-btn-label">设置</span>
        <span class="rail-badge dot" id="settingsUpdateBadge" style="display: none;"></span>
      </div>
    </div>

    <!-- Bottom Rail Items -->
    <div class="rail-bottom">
      <button class="rail-theme-btn" id="themeToggleBtn" onclick="toggleTheme()" title="切换亮色/暗色模式">🌙</button>
    </div>
  </aside>

  <!-- Right Main View -->
  <div id="main-wrapper">

    <!-- Top Header (56px) -->
    <header id="top-header">
      <div class="header-left">
        <div class="brand-full-logo" onclick="switchView('files')">
          <svg width="99" height="24" viewBox="0 0 99 24" fill="none" class="brand-txt-svg" xmlns="http://www.w3.org/2000/svg">
            <g clip-path="url(#clip0_1034_332)">
              <g clip-path="url(#clip1_1034_332)">
                <path d="M-0.0168457 5.34375C-0.0168457 2.39248 2.37563 0 5.3269 0H18.6394C21.5907 0 23.9832 2.39248 23.9832 5.34375V18.6562C23.9832 21.6075 21.5907 24 18.6394 24H5.3269C2.37563 24 -0.0168457 21.6075 -0.0168457 18.6562V5.34375Z" fill="#FF6800"/>
                <path d="M-0.0168457 5.34375C-0.0168457 2.39248 2.37563 0 5.3269 0H18.6394C21.5907 0 23.9832 2.39248 23.9832 5.34375V18.6562C23.9832 21.6075 21.5907 24 18.6394 24H5.3269C2.37563 24 -0.0168457 21.6075 -0.0168457 18.6562V5.34375Z" fill="url(#paint0_radial_1034_332)" fill-opacity="0.7"/>
                <path d="M12.8693 9.34167C13.5502 9.34167 14.1021 9.89201 14.1021 10.5709C14.1021 11.2497 13.5502 11.8 12.8693 11.8C12.1885 11.8 11.6365 11.2497 11.6365 10.5709C11.6365 9.892 12.1885 9.34167 12.8693 9.34167Z" fill="white"/>
                <path fill-rule="evenodd" clip-rule="evenodd" d="M12.551 4.96826C15.5436 4.96826 18.1015 6.82383 19.1307 9.44397C19.1677 9.53817 19.258 9.6017 19.3594 9.6017H21.45C21.5425 9.6017 21.6174 9.67644 21.6174 9.76864C21.6174 9.9403 21.5989 10.0209 21.5637 10.182C21.5616 10.1915 21.5595 10.201 21.5573 10.2104C21.5551 10.2199 21.5529 10.2293 21.5506 10.2387C21.546 10.2575 21.5412 10.2762 21.5361 10.2948C21.5311 10.3134 21.5258 10.3319 21.5203 10.3503C21.5066 10.3963 21.4915 10.4418 21.475 10.4866C21.4717 10.4955 21.4684 10.5045 21.465 10.5134C21.4208 10.6292 21.3677 10.7406 21.3064 10.8468C21.3017 10.8549 21.297 10.8631 21.2922 10.8712C21.2778 10.8955 21.2629 10.9195 21.2477 10.9433C21.2324 10.967 21.2167 10.9904 21.2006 11.0136C21.1834 11.0384 21.1656 11.0628 21.1475 11.0869C21.1286 11.1119 21.1093 11.1365 21.0894 11.1607C21.0824 11.1692 21.0754 11.1778 21.0683 11.1862C21.0425 11.2168 21.0159 11.2468 20.9886 11.2761C20.963 11.3035 20.9368 11.3303 20.91 11.3564C20.8866 11.3791 20.8628 11.4013 20.8386 11.423C20.8101 11.4485 20.781 11.4734 20.7513 11.4975C20.7158 11.5263 20.6795 11.554 20.6424 11.5808C20.5311 11.6608 20.4126 11.7314 20.2879 11.7913C20.2794 11.7954 20.2708 11.7995 20.2622 11.8035C20.2043 11.8305 20.145 11.8551 20.0846 11.8774C20.0353 11.8955 19.9852 11.9122 19.9344 11.9271C19.8995 11.9373 19.8643 11.9468 19.8288 11.9555C19.7911 11.9646 19.753 11.9729 19.7147 11.9802C19.6816 11.9866 19.6482 11.9921 19.6146 11.9971C19.5686 12.0038 19.3398 12.0112 19.2694 12.0112H16.9258L16.9258 12.0119H17.2887C16.9668 12.0267 16.9229 12.3024 16.9076 12.443L16.9075 12.442C16.6843 16.1311 13.613 19.0542 9.85688 19.0542C6.03668 19.0542 2.92474 16.0304 2.79725 12.2524C2.79277 12.1195 2.90124 12.0112 3.03452 12.0112H5.10892C5.34231 11.9938 5.46583 11.8997 5.50121 11.5464V11.566C5.73158 7.88374 8.7998 4.96828 12.551 4.96826ZM12.551 7.66375C10.2239 7.66377 8.32256 9.48144 8.19728 11.7705C8.19002 11.9033 8.08268 12.0112 7.94941 12.0112H5.75052C5.55814 12.0252 5.51246 12.1732 5.50466 12.2776C5.64269 14.5546 7.53848 16.3587 9.85688 16.3587C12.184 16.3587 14.0853 14.541 14.2106 12.2519C14.2179 12.1192 14.3252 12.0112 14.4585 12.0112H16.5982C16.8366 11.9671 16.8961 11.7624 16.8946 11.6289C16.7005 9.40691 14.8299 7.66375 12.551 7.66375Z" fill="white"/>
              </g>
              <path d="M61.5828 4.8746H63.6372V9.98332C63.6372 11.2456 62.6143 12.2688 61.3525 12.2688H59.8741L59.6054 10.3867H61.3525C61.5751 10.3867 61.7557 10.2061 61.7557 9.98332V6.75674H58.799V13.1427H64.4435V16.9742C64.4435 18.7562 62.9995 20.2008 61.2181 20.2008H60.0086V18.3187H61.2181C61.9603 18.3187 62.562 17.7167 62.562 16.9742V15.0249H58.2615C57.5192 15.0249 56.9175 14.423 56.9175 13.6805V4.8746H59.1638L59.471 3.79907H61.8901L61.5828 4.8746Z" fill="currentColor"/>
              <path fill-rule="evenodd" clip-rule="evenodd" d="M56.1112 13.3443C56.1112 14.6066 55.0883 15.6299 53.8265 15.6299H53.4905V20.1336H51.7434V15.6299H49.1227V4.47126H56.1112V13.3443ZM51.0043 13.7477H51.7434V10.9917H51.0043V13.7477ZM53.4905 13.7477H53.8265C54.0492 13.7477 54.2297 13.5671 54.2297 13.3443V10.9917H53.4905V13.7477ZM51.0043 9.24388H51.7434V6.35347H51.0043V9.24388ZM53.4905 9.24388H54.2297V6.35347H53.4905V9.24388Z" fill="currentColor"/>
              <path d="M40.7232 10.2522H47.1068V12.4033H43.5454V15.6955C43.5454 16.8463 44.4781 17.7793 45.6285 17.7793H47.846L47.3122 20.0648H45.6285C43.2163 20.0648 41.2608 18.1086 41.2607 15.6955V12.4033H37.4236L37.3509 13.3559C37.1176 16.4135 35.1215 19.0577 32.2526 20.1288L31.4539 17.9874C33.4917 17.2266 34.9076 15.3482 35.0729 13.1819L35.1322 12.4033H31.786V10.2522H38.4385V3.86632H40.7232V10.2522Z" fill="currentColor"/>
              <path d="M80.8395 11.5966H72.1407L68.9798 17.0734C68.7988 17.3871 69.0251 17.7792 69.3871 17.7793H77.643C78.0051 17.7793 78.2313 17.3872 78.0503 17.0735L76.5383 14.4535L78.5169 13.3107L80.029 15.9307C81.0893 17.7681 79.7638 20.0647 77.643 20.0648H69.3871C67.2663 20.0647 65.9408 17.7679 67.0012 15.9306L69.5026 11.5966H65.9219V9.44555H80.8395V11.5966Z" fill="currentColor"/>
              <path fill-rule="evenodd" clip-rule="evenodd" d="M96.494 17.9124H98.445V20.0624H81.5588V17.9124H83.1061V14.2131H96.494V17.9124ZM85.2589 17.9124H86.739V16.3631H85.2589V17.9124ZM88.8918 17.9124H90.7756V16.3631H88.8918V17.9124ZM92.9284 17.9124H94.3412V16.3631H92.9284V17.9124Z" fill="currentColor"/>
              <path d="M61.8229 17.512H55.5736V15.6299H61.8229V17.512Z" fill="currentColor"/>
              <path fill-rule="evenodd" clip-rule="evenodd" d="M90.4873 5.2772H96.2922V9.17407H97.6377V11.324H95.9769C95.5955 12.3119 94.8848 13.1593 93.9375 13.7055L93.2647 14.0934L92.1883 12.2315L92.8611 11.8436C93.1036 11.7037 93.3175 11.5274 93.4976 11.324H90.3331L91.1792 13.6084H88.8459L87.9998 11.324H85.4785C85.2208 12.2134 84.7947 13.0919 84.0915 13.9981L82.3899 12.6812C82.7494 12.2179 83.0112 11.776 83.2021 11.324H82.2315V9.17407H83.6662C83.7226 8.38234 83.7116 7.47327 83.7116 6.35218V5.2772H88.0653L88.4882 3.79907H90.9101L90.4873 5.2772ZM85.8676 7.42717C85.8685 8.03041 85.8631 8.60986 85.8274 9.17407H88.7764L88.3536 7.69595H90.7756L91.1984 9.17407H94.1393V7.42717H85.8676Z" fill="currentColor"/>
              <path d="M61.4197 9.84889H59.5382L59.2022 7.56342H61.0837L61.4197 9.84889Z" fill="currentColor"/>
              <path d="M36.6242 9.44555H33.9363L32.3236 4.74018H35.0115L36.6242 9.44555Z" fill="currentColor"/>
              <path d="M45.2253 9.44555H42.5374L44.2846 4.74018H46.9724L45.2253 9.44555Z" fill="currentColor"/>
              <path d="M79.7644 6.89123H66.997V4.74018H79.7644V6.89123Z" fill="currentColor"/>
            </g>
            <defs>
              <radialGradient id="paint0_radial_1034_332" cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(20.116 0.445312) rotate(127.312) scale(18.2115)">
                <stop stop-color="#FFC30E"/>
                <stop offset="1" stop-color="#FFA70E" stop-opacity="0"/>
              </radialGradient>
              <clipPath id="clip0_1034_332">
                <rect width="99" height="24" fill="white"/>
              </clipPath>
              <clipPath id="clip1_1034_332">
                <rect width="24" height="24" fill="white" transform="translate(-0.0168457)"/>
              </clipPath>
            </defs>
          </svg>
          <span class="fnos-badge">fnOS</span>
        </div>
      </div>

      <!-- Center Category Navigation Tabs (kj array) -->
      <div class="header-center">
        <div class="cat-tabs" id="categoryTabsNav">
          <div class="cat-tab-item active" data-cat="all" onclick="switchCategory('all')">全部</div>
          <div class="cat-tab-item" data-cat="recent" onclick="switchCategory('recent')">最近</div>
          <div class="cat-tab-item" data-cat="video" onclick="switchCategory('video')">视频</div>
          <div class="cat-tab-item" data-cat="img" onclick="switchCategory('img')">图片</div>
          <div class="cat-tab-item" data-cat="doc" onclick="switchCategory('doc')">文档</div>
          <div class="cat-tab-item" data-cat="audio" onclick="switchCategory('audio')">音频</div>
          <div class="cat-tab-item" data-cat="installer" onclick="switchCategory('installer')">安装包</div>
        </div>
      </div>

      <!-- Right Profile & Storage Capsule -->
      <div class="header-right">
        <!-- Storage Capsule -->
        <div class="storage-capsule" title="云盘存储空间">
          <svg width="15" height="15" viewBox="0 0 18 18" fill="none" style="color: var(--brand);">
            <path fill="currentColor" d="M8.475 1.538c2.784.016 5.167 1.846 5.977 4.499a5.25 5.25 0 0 1 3.292 5.13q.005.123.006.246a.75.75 0 0 1-.06.294 5.25 5.25 0 0 1-4.79 4.438 1 1 0 0 1-.15.016h-.187a2 2 0 0 1-.127 0H4.75a.8.8 0 0 1-.16-.019A4.748 4.748 0 0 1 2.27 7.528c.24-3.446 2.875-6.01 6.206-5.99m-.01 1.5c-2.575-.015-4.673 2.03-4.714 4.92a.75.75 0 0 1-.375.64 3.248 3.248 0 0 0 1.42 6.057q.018.002.03"/>
          </svg>
          <span id="quotaText">-- / --</span>
          <div class="storage-bar-wrap">
            <div class="storage-bar-fill" id="quotaFillBar"></div>
          </div>
        </div>

        <!-- User Capsule -->
        <div class="user-profile-capsule" id="userCardBox" onclick="handleUserAction()">
          <div class="user-avatar-circle" id="userAvatarText">🦆</div>
          <span class="user-name-label" id="userNameText">连接中...</span>
          <span class="vip-tag" id="userVipText">SVIP</span>

          <!-- User Dropdown Menu -->
          <div class="user-dropdown-menu" onclick="event.stopPropagation()">
            <div class="user-dropdown-item" style="font-weight: 600; border-bottom: 1px solid var(--border-subtle); margin-bottom: 4px;">
              <span>🦆 个人空间</span>
            </div>
            <div class="user-dropdown-item" onclick="openLoginModal()">
              <span>🔄 切换/重新登录</span>
            </div>
            <div class="user-dropdown-item danger" id="userActionBtn" onclick="confirmLogout()">
              <span>🚪 退出登录</span>
            </div>
          </div>
        </div>

        <!-- Refresh Button -->
        <button class="btn-icon" onclick="refreshCurrentView()" title="刷新当前页面">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
          </svg>
        </button>
      </div>
    </header>

    <!-- Main Views Container -->
    <div id="view-container">

      <!-- VIEW 1: FILES EXPLORER -->
      <div class="content-view active" id="view-files">
        <div class="official-card">
          <!-- File Toolbar -->
          <div class="file-toolbar">
            <div class="toolbar-left">
              <button class="btn-primary" onclick="openUploadModal()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 5v14M5 12h14"/>
                </svg>
                <span>上传文件</span>
              </button>
              <button class="btn-secondary" onclick="promptCreateFolder()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                  <line x1="12" y1="11" x2="12" y2="17"/>
                  <line x1="9" y1="14" x2="15" y2="14"/>
                </svg>
                <span>新建文件夹</span>
              </button>
              <button class="btn-secondary" onclick="openCloudAddModal()" style="color: var(--brand); border-color: var(--brand-alpha-25);">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
                </svg>
                <span>云添加 (磁链/BT)</span>
              </button>
              <button class="btn-secondary" onclick="openQuickStrmModalForCurrentDir()" title="将当前网盘目录一键生成为本地 STRM 影视库文件">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--brand);">
                  <rect x="2" y="3" width="20" height="18" rx="3"/>
                  <polygon points="10 9.5 16 12.5 10 15.5 10 9.5" fill="currentColor"/>
                </svg>
                <span>生成 STRM</span>
              </button>
              <button class="btn-secondary" id="batchDownloadBtn" onclick="promptBatchDownload()" title="勾选多个文件或文件夹后一键批量下载到 NAS">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="color: var(--brand);" id="batchDownloadBtnIcon">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="7 10 12 15 17 10"/>
                  <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                <span id="batchDownloadBtnText">批量下载</span>
              </button>
              <button class="btn-danger" id="batchDeleteBtn" onclick="promptBatchDelete()" style="display: none;" title="批量删除所选文件或文件夹">
                <span>🗑️ 批量删除 (<span id="batchDeleteCountText">0</span>)</span>
              </button>
            </div>

            <div class="toolbar-right">
              <!-- Search Box -->
              <div class="search-box">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="color: var(--text-muted);">
                  <circle cx="11" cy="11" r="8"/>
                  <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                <input type="text" id="fileSearchInput" placeholder="搜索我的网盘文件..." oninput="handleSearch(this.value)">
              </div>

              <!-- View Switcher -->
              <button class="btn-icon active" id="viewModeListBtn" onclick="setViewMode('list')" title="列表视图">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/>
                  <line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>
                </svg>
              </button>
              <button class="btn-icon" id="viewModeGridBtn" onclick="setViewMode('grid')" title="缩略图视图">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/>
                  <rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>
                </svg>
              </button>
            </div>
          </div>

          <!-- Breadcrumbs Bar -->
          <div class="file-breadcrumbs" id="breadcrumbs">
            <span class="crumb-item current">全部文件</span>
          </div>

          <!-- File Table -->
          <div class="table-container" id="fileTableContainer">
            <table>
              <thead>
                <tr>
                  <th style="width: 40px;"><input type="checkbox" id="tableSelectAllCb" onchange="toggleSelectAllTableFiles(this.checked)"></th>
                  <th>名称</th>
                  <th style="width: 130px;">大小</th>
                  <th style="width: 170px;">修改时间</th>
                  <th style="width: 260px; text-align: right; padding-right: 20px;">操作</th>
                </tr>
              </thead>
              <tbody id="fileTableBody">
                <tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 50px;">正在加载目录文件...</td></tr>
              </tbody>
            </table>
          </div>

          <!-- Grid Container (when in grid view) -->
          <div id="fileGridContainer" style="display: none; flex: 1; overflow-y: auto; padding: 18px; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));"></div>

          <!-- File Pagination Footer Bar -->
          <div class="file-pagination-bar" id="filePaginationBar">
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
              <span id="filePaginationInfo" style="color: var(--text-secondary); font-weight: 500;">共 0 项</span>
              <div style="display: flex; align-items: center; gap: 6px;">
                <span style="color: var(--text-muted); font-size: 12px;">每页显示:</span>
                <select id="filePageSizeSelect" class="form-input" style="height: 30px; width: auto; min-width: 96px; padding: 0 8px; font-size: 12px; font-weight: 500;" onchange="onFilePageSizeSelectChange(this.value)">
                  <option value="10">10 条/页</option>
                  <option value="20" selected>20 条/页</option>
                  <option value="50">50 条/页</option>
                  <option value="100">100 条/页</option>
                  <option value="200">200 条/页</option>
                  <option value="0">全部显示</option>
                  <option value="custom">自定义...</option>
                </select>
                <div id="filePageSizeCustomWrap" style="display: flex; align-items: center; gap: 4px;" title="可直接输入任意每页显示条数 (1~5000，输入 0 为全部显示)">
                  <input type="number" id="filePageSizeCustomInput" class="form-input" min="0" max="5000" value="20" style="width: 62px; height: 30px; padding: 0 6px; text-align: center; font-size: 12px; font-weight: 600; color: var(--brand);" onchange="setFilePageSize(this.value)">
                  <span style="font-size: 12px; color: var(--text-muted);">条/页</span>
                </div>
              </div>
            </div>

            <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;" id="filePaginationControls">
              <button class="page-btn" id="btnFirstPage" onclick="goToFilePage(1)" title="第一页">⏮ 首页</button>
              <button class="page-btn" id="btnPrevPage" onclick="changeFilePage(-1)" title="上一页">◀ 上一页</button>
              <div id="filePageNumbersContainer" style="display: inline-flex; align-items: center; gap: 4px;"></div>
              <button class="page-btn" id="btnNextPage" onclick="changeFilePage(1)" title="下一页">下一页 ▶</button>
              <button class="page-btn" id="btnLastPage" onclick="goToFilePage(getTotalFilePages())" title="最后一页">末页 ⏭</button>
              <div style="display: inline-flex; align-items: center; gap: 4px; margin-left: 4px; font-size: 12px; color: var(--text-muted);">
                <span>跳至</span>
                <input type="number" id="filePageJumpInput" class="form-input" min="1" value="1" style="width: 52px; height: 30px; padding: 0 4px; text-align: center; font-size: 12px; font-weight: 600;" onchange="goToFilePage(this.value)">
                <span>/ <strong id="fileTotalPagesText" style="color: var(--text-main);">1</strong> 页</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- VIEW 2: OFFLINE / CLOUD TASKS -->
      <div class="content-view" id="view-offline">
        <div class="official-card">
          <div class="subtabs">
            <div class="subtabs-left">
              <button class="subtab-btn active" id="tabOfflineAll" onclick="filterOfflineTasks('all')">全部 (<span id="countOfflineAll">0</span>)</button>
              <button class="subtab-btn" id="tabOfflineRunning" onclick="filterOfflineTasks('running')">进行中 (<span id="countOfflineRunning">0</span>)</button>
              <button class="subtab-btn" id="tabOfflineDone" onclick="filterOfflineTasks('done')">已完成 (<span id="countOfflineDone">0</span>)</button>
              <button class="subtab-btn" id="tabOfflineFailed" onclick="filterOfflineTasks('failed')">失败 (<span id="countOfflineFailed">0</span>)</button>
            </div>
            <div style="display: flex; gap: 10px;">
              <button class="btn-primary btn-sm" onclick="openCloudAddModal()">⚡ 新建云添加</button>
              <button class="btn-outline btn-sm" onclick="loadCloudTasks()">🔄 全部刷新</button>
            </div>
          </div>
          <div class="task-list-container" id="offlineListContainer">
            <div style="text-align: center; color: var(--text-muted); padding: 40px;">正在加载云端离线任务...</div>
          </div>
        </div>
      </div>

      <!-- VIEW 3: TRANSFERS (NAS LOCAL DOWNLOADS) -->
      <div class="content-view" id="view-transfers">
        <div class="official-card">
          <div class="subtabs" style="flex-wrap: wrap; gap: 10px;">
            <div class="subtabs-left">
              <button class="subtab-btn active" id="tabDlActiveBtn" onclick="switchTransferTab('active')">正在下载 / 排队中 (<span id="countActiveDl">0</span>)</button>
              <button class="subtab-btn" id="tabDlDoneBtn" onclick="switchTransferTab('done')">已下载完成 (<span id="countDoneDl">0</span>)</button>
            </div>
            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
              <div style="display: flex; align-items: center; gap: 6px; background: var(--bg-input); padding: 4px 10px; border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); font-size: 12.5px;">
                <span style="color: var(--text-secondary); font-weight: 500;">⚡ 同时下载个数:</span>
                <input type="number" id="transferMaxConcurrentInput" min="1" max="16" value="3"
                  style="width: 54px; height: 26px; text-align: center; font-weight: 700; font-size: 13px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg-card); color: var(--brand); outline: none;"
                  onchange="updateMaxConcurrentSetting(this.value)">
                <span style="color: var(--text-muted); font-size: 11.5px;">个 (1~16)</span>
              </div>
              <button class="btn-danger btn-sm" id="cancelAllDlBtn" onclick="cancelAllTransfers()" style="display: none;">⏹️ 全部取消</button>
              <button class="btn-outline btn-sm" onclick="clearCompletedDownloads()">清空已完成记录</button>
            </div>
          </div>
          <div class="task-list-container" id="transfersListContainer">
            <div style="text-align: center; color: var(--text-muted); padding: 40px;">暂无下载任务</div>
          </div>
        </div>
      </div>

      <!-- VIEW 3.5: STRM MEDIA CENTER -->
      <div class="content-view" id="view-strm">
        <div class="official-card" style="overflow-y: auto; padding: 20px; gap: 18px;">
          <!-- Top Banner -->
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px; padding: 16px 20px; border-radius: var(--radius-md); background: var(--bg-subtle); border: 1px solid var(--border);">
            <div style="display: flex; align-items: center; gap: 14px;">
              <div style="width: 44px; height: 44px; border-radius: 12px; background: var(--vip-gradient); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; box-shadow: var(--shadow-brand);">
                🎬
              </div>
              <div>
                <div style="font-size: 16px; font-weight: 700; color: var(--text-main); display: flex; align-items: center; gap: 8px;">
                  <span>STRM 影视库 &amp; 302 动态串流网关</span>
                  <span class="status-badge status-success" id="strmTopStatusBadge">就绪</span>
                </div>
                <div style="font-size: 12.5px; color: var(--text-muted); margin-top: 4px; line-height: 1.5;">
                  将光鸭网盘海量视频以百字节 <code>.strm</code> 映射至飞牛影视 / Emby / Jellyfin / Infuse，零占用本地硬盘，302 动态直连光鸭 CDN 原画秒播。
                </div>
              </div>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
              <button class="btn-danger" id="strmCancelSyncBtn" onclick="cancelStrmSync()" style="display: none;">⏹️ 停止同步</button>
              <button class="btn-primary" id="strmStartSyncBtn" onclick="triggerFullStrmSync()">⚡ 立即同步 STRM 影视库</button>
            </div>
          </div>

          <!-- Two-Column Grid: Config (Left) + Status/Logs (Right) -->
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 18px; align-items: start;">

            <!-- Left Card: STRM Mapping & Gateway Configuration -->
            <div class="setting-card" style="gap: 14px;">
              <div class="setting-card-title" style="display: flex; justify-content: space-between; align-items: center;">
                <span>⚙️ 映射路径与播放网关配置</span>
                <button class="btn-primary btn-sm" onclick="saveStrmConfig(true)">💾 保存配置</button>
              </div>

              <!-- 1. Cloud Source Folder -->
              <div>
                <label style="font-size: 12.5px; font-weight: 600; color: var(--text-main); display: block; margin-bottom: 6px;">1. 光鸭网盘源目录（扫描范围）：</label>
                <div style="display: flex; gap: 8px;">
                  <input type="text" class="form-input" id="strmCloudPathInput" style="flex: 1; font-family: monospace; cursor: pointer;" readonly value="全部文件" onclick="pickStrmCloudFolder()">
                  <button class="btn-secondary" onclick="pickStrmCloudFolder()" style="flex-shrink: 0;">☁️ 选择网盘目录</button>
                </div>
                <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 4px;">支持映射整个网盘或指定特定影视文件夹（自动保持多级子目录树结构）。</div>
              </div>

              <!-- 2. Local NAS STRM Output Dir -->
              <div>
                <label style="font-size: 12.5px; font-weight: 600; color: var(--text-main); display: block; margin-bottom: 6px;">2. NAS 本地 STRM 存放目录（供飞牛影视/Emby 挂载）：</label>
                <div style="display: flex; gap: 8px;">
                  <input type="text" class="form-input" id="strmOutputDirInput" style="flex: 1; font-family: monospace;" placeholder="/vol1/1000/光鸭网盘/STRM影视库">
                  <button class="btn-secondary" onclick="openFolderPicker(onStrmOutputFolderPicked, document.getElementById('strmOutputDirInput').value)" style="flex-shrink: 0;">📂 浏览 NAS 目录</button>
                </div>
                <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 4px;">在飞牛影视 / Emby / Jellyfin 媒体库中添加该 NAS 目录即可自动刮削海报墙。</div>
              </div>

              <!-- 3. STRM Gateway Base URL -->
              <div>
                <label style="font-size: 12.5px; font-weight: 600; color: var(--text-main); display: block; margin-bottom: 6px;">3. STRM 局域网/外网播放网关地址：</label>
                <div style="display: flex; gap: 8px;">
                  <input type="text" class="form-input" id="strmBaseUrlInput" style="flex: 1; font-family: monospace;" placeholder="http://192.168.31.186:8998">
                  <button class="btn-secondary" onclick="autoFillStrmBaseUrl()" style="flex-shrink: 0;" title="自动填入检测到的 NAS 局域网 IP">🔄 自动获取IP</button>
                </div>
                <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 4px;">请勿填写 127.0.0.1，应使用 NAS 局域网 IP 或反代域名，确保电视/手机/Docker 均可访问。</div>
              </div>

              <!-- 4. Schedule Interval -->
              <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 10px 12px; background: var(--bg-input); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle);">
                <div>
                  <div style="font-size: 13px; font-weight: 600; color: var(--text-main);">⏰ 定时自动增量同步</div>
                  <div style="font-size: 11.5px; color: var(--text-muted);">后台定期扫描网盘新增影视并自动生成 .strm</div>
                </div>
                <select id="strmScheduleSelect" class="form-input" style="width: 150px; height: 34px; padding: 0 8px;">
                  <option value="0">关闭定时同步</option>
                  <option value="60">每 1 小时</option>
                  <option value="180">每 3 小时</option>
                  <option value="360">每 6 小时</option>
                  <option value="720">每 12 小时</option>
                  <option value="1440">每 24 小时</option>
                </select>
              </div>

              <!-- 5. Checkboxes -->
              <div style="display: flex; flex-direction: column; gap: 9px; padding: 12px; background: var(--bg-input); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); font-size: 12.5px; color: var(--text-main);">
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmDlSubsCb" checked style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>自动同步外挂字幕</strong>（下载同目录 <code>.srt / .ass / .vtt / .sub</code> 到本地）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmDlMetaCb" style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>同步 NFO 与海报图</strong>（下载同目录 <code>.nfo / .jpg / .png</code> 元数据）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmAutoOfflineCb" checked style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>离线下载完成自动联动</strong>（云添加任务完成后自动增量生成 STRM）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmOverwriteCb" checked style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>覆盖更新同名 STRM</strong>（当网关地址变更时自动刷新已有 .strm 内容）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmCleanOrphansCb" style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>清理失效残留 STRM</strong>（网盘源文件删除后，同步移除本地对应 .strm）</span>
                </label>
                <label style="display: flex; align-items: center; gap: 8px; cursor: pointer;">
                  <input type="checkbox" id="strmUseProxyCb" style="width: 15px; height: 15px; accent-color: var(--brand);">
                  <span><strong>启用本地流式代理中转</strong>（默认关闭，推荐 302 直连光鸭 CDN 零 CPU 占用）</span>
                </label>
              </div>
            </div>

            <!-- Right Card: Real-time Sync Status, Stats & Recent STRM Files -->
            <div class="setting-card" style="gap: 14px;">
              <div class="setting-card-title" style="display: flex; justify-content: space-between; align-items: center;">
                <span>📊 同步状态与直链缓存监控</span>
                <button class="btn-outline btn-sm" onclick="clearStrmUrlCache()">🧹 清空直链缓存 (<span id="strmCacheCountText">0</span>)</button>
              </div>

              <!-- Stats Grid -->
              <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px;">
                <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 10px; text-align: center;">
                  <div style="font-size: 18px; font-weight: 700; color: var(--brand);" id="statStrmCreated">0</div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">生成/更新 STRM</div>
                </div>
                <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 10px; text-align: center;">
                  <div style="font-size: 18px; font-weight: 700; color: var(--text-main);" id="statStrmSkipped">0</div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">跳过未变更</div>
                </div>
                <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 10px; text-align: center;">
                  <div style="font-size: 18px; font-weight: 700; color: #22c55e;" id="statStrmSubs">0</div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">同步字幕/元数据</div>
                </div>
                <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 10px; text-align: center;">
                  <div style="font-size: 18px; font-weight: 700; color: #3b82f6;" id="statStrmScanned">0</div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">扫描目录数</div>
                </div>
              </div>

              <!-- Current Activity Box -->
              <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 10px 12px; font-size: 12px; line-height: 1.6;">
                <div style="display: flex; justify-content: space-between; color: var(--text-secondary);">
                  <span id="strmCurrentActionText">当前无运行中的同步任务</span>
                  <span id="strmTotalHitsText" style="color: var(--brand); font-weight: 600;">302 缓存命中: 0 次</span>
                </div>
                <div id="strmCurrentFileText" style="color: var(--text-muted); font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;">
                  点击右上角「立即同步 STRM 影视库」或在文件列表中对任意文件夹点击「🎬 STRM」即可生成。
                </div>
              </div>

              <!-- Subtabs: Recent STRM Files vs Execution Logs -->
              <div class="subtabs" style="padding: 0; border-bottom: 1px solid var(--border-subtle); padding-bottom: 8px;">
                <div class="subtabs-left">
                  <button class="subtab-btn active" id="tabStrmRecentBtn" onclick="switchStrmPanelTab('recent')">最近生成的 .strm (<span id="countStrmRecent">0</span>)</button>
                  <button class="subtab-btn" id="tabStrmLogsBtn" onclick="switchStrmPanelTab('logs')">执行日志</button>
                </div>
              </div>

              <!-- Panel 1: Recent STRM Files -->
              <div id="strmRecentListContainer" style="max-height: 290px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;">
                <div style="text-align: center; color: var(--text-muted); padding: 36px 12px; font-size: 12.5px;">
                  尚未生成 STRM 记录，点击「⚡ 立即同步 STRM 影视库」开始构建海报墙音源。
                </div>
              </div>

              <!-- Panel 2: Execution Logs -->
              <div id="strmLogsContainer" style="display: none; max-height: 290px; overflow-y: auto; background: #141414; color: #d1d5db; font-family: monospace; font-size: 11.5px; line-height: 1.6; padding: 10px 12px; border-radius: var(--radius-sm); border: 1px solid var(--border);">
                暂无日志
              </div>
            </div>

          </div>
        </div>
      </div>

      <!-- VIEW: LOGS CENTER -->
      <div class="content-view" id="view-logs">
        <div class="official-card">
          <!-- Subtabs Bar & Toolbar -->
          <div class="subtabs-bar">
            <div class="subtabs-left">
              <button class="subtab-btn active" id="logSubtab-all" onclick="filterLogsByCategory('all')">全部 <span class="badge-pill" id="logCount-all">0</span></button>
              <button class="subtab-btn" id="logSubtab-download" onclick="filterLogsByCategory('download')">⬇️ 下载传输 <span class="badge-pill" id="logCount-download">0</span></button>
              <button class="subtab-btn" id="logSubtab-cloud" onclick="filterLogsByCategory('cloud')">⚡ 云端离线 <span class="badge-pill" id="logCount-cloud">0</span></button>
              <button class="subtab-btn" id="logSubtab-strm" onclick="filterLogsByCategory('strm')">🎬 STRM同步 <span class="badge-pill" id="logCount-strm">0</span></button>
              <button class="subtab-btn" id="logSubtab-system" onclick="filterLogsByCategory('system')">⚙️ 系统服务 <span class="badge-pill" id="logCount-system">0</span></button>
            </div>
            <div class="toolbar-right" style="gap: 8px;">
              <select class="form-select" id="logLevelSelect" onchange="onLogLevelChanged()" style="height: 32px; padding: 0 10px; font-size: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--bg-input); color: var(--text-main);">
                <option value="ALL">全部级别</option>
                <option value="INFO">ℹ️ INFO 提示</option>
                <option value="SUCCESS">✅ SUCCESS 成功</option>
                <option value="WARN">⚠️ WARN 警告</option>
                <option value="ERROR">❌ ERROR 异常</option>
              </select>
              <div class="search-box" style="height: 32px; width: 170px;">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                <input type="text" id="logSearchInput" placeholder="搜索日志..." oninput="onLogSearchInput()">
              </div>
              <label style="display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-secondary); cursor: pointer; user-select: none;">
                <input type="checkbox" id="logAutoScrollCheck" checked onchange="toggleLogAutoScroll()">
                <span>自动滚动</span>
              </label>
              <button class="btn-secondary btn-sm" onclick="loadLogs(true)" title="刷新日志" style="height: 32px; padding: 0 10px; font-size: 12px;">🔄 刷新</button>
              <button class="btn-outline btn-sm" onclick="confirmClearLogs()" title="清空日志" style="height: 32px; padding: 0 10px; font-size: 12px;">🗑️ 清空</button>
              <button class="btn-primary btn-sm" onclick="exportLogs()" title="导出日志为 TXT 文件" style="height: 32px; padding: 0 12px; font-size: 12px;">📥 导出TXT</button>
            </div>
          </div>

          <!-- Log Console Body -->
          <div id="logConsoleContainer" class="log-console-wrap">
            <div id="logConsoleList" class="log-console-list"></div>
            <div id="logEmptyState" style="display: none; padding: 60px 0; text-align: center; color: var(--text-muted);">
              <div style="font-size: 28px; margin-bottom: 8px;">📜</div>
              <div style="font-size: 14px; font-weight: 500;">暂无匹配的运行日志</div>
              <div style="font-size: 12px; margin-top: 4px; color: var(--text-placeholder);">下载、云离线、STRM串流或系统事件触发时将实时在此记录</div>
            </div>
          </div>
        </div>
      </div>

      <!-- VIEW 4: RECYCLE BIN -->
      <div class="content-view" id="view-trash">
        <div class="official-card" style="display: flex; align-items: center; justify-content: center;">
          <div class="empty-container">
            <img src="/empty.png" class="empty-duck-img" alt="回收站">
            <div class="empty-title">回收站就绪</div>
            <div class="empty-sub">官方云端回收站已配置完成，可前往客户端进一步管理</div>
          </div>
        </div>
      </div>

      <!-- VIEW 5: SETTINGS -->
      <div class="content-view" id="view-settings">
        <div class="official-card">
          <div class="settings-grid">
            <!-- Setting 1: Storage Dir & Concurrency -->
            <div class="setting-card">
              <div class="setting-card-title">📁 NAS 落地下载目录与并发控制</div>
              <div class="setting-card-desc">指定光鸭网盘文件下载至飞牛 NAS 的本地共享文件夹路径，并设置最大同时下载个数。</div>
              <div style="display: flex; gap: 8px; margin-top: 4px;">
                <input type="text" class="form-input" id="settingDownloadDirInput" style="flex: 1;" placeholder="/vol1/1000/下载/光鸭网盘">
                <button class="btn-secondary" onclick="openFolderPicker(onSettingFolderPicked, document.getElementById('settingDownloadDirInput').value)">浏览...</button>
              </div>
              <div style="margin-top: 8px; padding: 10px 12px; background: var(--bg-input); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); display: flex; align-items: center; justify-content: space-between; gap: 10px;">
                <div>
                  <div style="font-size: 13px; font-weight: 600; color: var(--text-main);">⚡ 最大同时下载个数限制</div>
                  <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">超出数量的任务自动排队，前序完成后自动接力（可手动输入 1 ~ 16）</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                  <input type="number" class="form-input" id="settingMaxConcurrentInput" min="1" max="16" value="3" style="width: 68px; height: 34px; text-align: center; font-weight: 700; color: var(--brand); padding: 0 6px;">
                  <span style="font-size: 12.5px; color: var(--text-secondary);">个</span>
                </div>
              </div>
              <div style="margin-top: 8px; padding: 10px 12px; background: var(--bg-input); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); display: flex; align-items: center; justify-content: space-between; gap: 10px;">
                <div>
                  <div style="font-size: 13px; font-weight: 600; color: var(--text-main);">📄 文件列表每页显示数量</div>
                  <div style="font-size: 11.5px; color: var(--text-muted); margin-top: 2px;">可设为 10、20、50、100 或任意自定义条数（输入 0 代表单页全部显示）</div>
                </div>
                <div style="display: flex; align-items: center; gap: 6px; flex-shrink: 0;">
                  <input type="number" class="form-input" id="settingFilePageSizeInput" min="0" max="5000" value="20" style="width: 68px; height: 34px; text-align: center; font-weight: 700; color: var(--brand); padding: 0 6px;">
                  <span style="font-size: 12.5px; color: var(--text-secondary);">条/页</span>
                </div>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px;">
                <label style="display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); cursor: pointer;">
                  <input type="checkbox" id="settingSetDefaultCheckbox" checked> 同时设为默认保存路径
                </label>
                <button class="btn-primary btn-sm" onclick="saveDownloadDirSetting()">保存配置</button>
              </div>
            </div>

            <!-- Setting 2: Account Status -->
            <div class="setting-card">
              <div class="setting-card-title">🦆 光鸭网盘账号状态</div>
              <div class="setting-card-desc" id="settingAccountDesc">已安全授权登入，Token 自动无感持久化续期。</div>
              <div style="display: flex; gap: 10px; margin-top: 8px;">
                <button class="btn-primary btn-sm" id="settingLoginBtn" onclick="openLoginModal()" style="display: none;">📱 扫码登录账号</button>
                <button class="btn-danger btn-sm" id="settingLogoutBtn" onclick="confirmLogout()">🚪 退出当前账号</button>
              </div>
            </div>

            <!-- Setting 3: About & Update Check -->
            <div class="setting-card">
              <div class="setting-card-title">ℹ️ 应用介绍与更新检查</div>
              <div class="setting-card-desc">
                当前运行版本: <strong id="aboutCurrentVersion" style="color: var(--brand);">v1.2.2</strong> · 由 <strong>chaos</strong> 专为飞牛 fnOS 深度适配打造。
              </div>
              <div style="background: var(--bg-input); padding: 10px 12px; border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); font-size: 12px; margin-bottom: 4px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; gap: 8px; flex-wrap: wrap;">
                  <span style="font-weight: 600; color: var(--text-main);">🌐 网页独立访问地址 (默认端口: <code style="color: var(--brand); font-weight: 700;">8998</code>):</span>
                  <a id="aboutWebAccessLink" href="http://127.0.0.1:8998" target="_blank" style="color: var(--brand); font-family: monospace; font-weight: 600; text-decoration: underline;">http://&lt;NAS_IP&gt;:8998</a>
                </div>
                <div style="color: var(--text-muted); font-size: 11px; line-height: 1.5;">
                  除在飞牛桌面点击图标以微应用窗口打开外，您也可以在任意局域网浏览器直接输入 <code id="aboutWebAccessCode">http://&lt;NAS_IP&gt;:8998</code> 独立全屏访问。
                </div>
              </div>
              <div style="background: var(--bg-input); padding: 10px 12px; border-radius: var(--radius-sm); border: 1px solid var(--border-subtle); font-size: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                  <span style="font-weight: 600;">GitHub 官方发布源:</span>
                  <span class="fnos-badge" id="updateStatusBadge" style="background: var(--bg-card);">已是最新版</span>
                </div>
                <div id="updateStatusDesc" style="color: var(--text-muted); font-size: 11px;">点击下方按钮可实时检测 GitHub Releases 最新套件更新。</div>
              </div>
              <div style="display: flex; gap: 10px; margin-top: 4px;">
                <button class="btn-secondary btn-sm" id="checkUpdateBtn" onclick="checkForUpdates(true)">🔄 检查最新版本</button>
                <a href="https://github.com/lawzhougc/guangyapan-fnos/releases" target="_blank" class="btn-primary btn-sm" id="downloadUpdateBtn" style="display: none; text-decoration: none;">⬇️ 立即获取最新包</a>
              </div>
            </div>
          </div>
        </div>
      </div>

    </div>
  </div>

</div>

<div class="modal-backdrop" id="downloadConfirmModal">
    <div class="modal" style="width: 620px; max-width: 95vw;">
      <div class="modal-header">
        <div class="modal-title">
          <span style="font-size: 18px;">⬇️</span>
          <span id="dlConfirmModalTitle">下载文件到 NAS</span>
        </div>
        <button class="close-btn" onclick="closeModal('downloadConfirmModal')">&times;</button>
      </div>
      <div class="modal-body" style="padding: 20px 24px; gap: 14px;">
        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;" id="dlConfirmTargetLabel">下载目标：</div>
          <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 10px;">
              <span style="font-size: 20px; flex-shrink: 0;" id="dlConfirmItemIcon">📦</span>
              <div style="font-weight: 600; font-size: 13px; line-height: 1.5; color: var(--text-main); word-break: break-all; flex: 1;" id="dlConfirmFileName">--</div>
            </div>
            <div id="dlConfirmBatchList" style="display: none; max-height: 135px; overflow-y: auto; border-top: 1px solid var(--border-subtle); padding-top: 8px; font-size: 12px; color: var(--text-secondary); flex-direction: column; gap: 4px;"></div>
          </div>
        </div>

        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;">选择 NAS 保存目录：</div>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="dlConfirmPathInput" style="flex: 1; height: 38px; font-size: 13px; font-family: monospace; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 12px; color: var(--text-main);" readonly onclick="openFolderPicker(onDlConfirmFolderPicked)">
            <button class="btn-secondary" onclick="openFolderPicker(onDlConfirmFolderPicked)" style="height: 38px; padding: 0 16px; font-size: 13px; flex-shrink: 0;">📂 浏览目录</button>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px; line-height: 1.5;">
            💡 提示：可自由下载至 NAS 任意存储卷或共享文件夹（选择文件夹下载时将自动保持内部子目录结构）。
          </div>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
          <div id="dlConfirmConflictBox" style="display: none; flex-direction: column; gap: 5px; padding: 10px 12px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border-subtle);">
            <label style="font-size: 12px; color: var(--text-secondary); font-weight: 600;">同名文件处理策略：</label>
            <select id="dlConfirmConflictSelect" class="form-input" style="height: 32px; font-size: 12.5px; padding: 0 8px;">
              <option value="skip">跳过已存在同名文件 (推荐)</option>
              <option value="overwrite">覆盖原文件</option>
              <option value="rename">自动重命名保留两份</option>
            </select>
          </div>

          <div style="display: flex; flex-direction: column; gap: 5px; padding: 10px 12px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border-subtle); grid-column: span 2;" id="dlConfirmConcurrencyBox">
            <div style="display: flex; align-items: center; justify-content: space-between;">
              <div>
                <div style="font-size: 12.5px; color: var(--text-main); font-weight: 600;">⚡ 同时下载任务上限</div>
                <div style="font-size: 11px; color: var(--text-muted);">超出限制的任务将自动排队依次下载</div>
              </div>
              <div style="display: flex; align-items: center; gap: 6px;">
                <input type="number" id="dlConfirmMaxConcurrentInput" min="1" max="16" value="3" class="form-input" style="width: 64px; height: 32px; text-align: center; font-weight: 700; color: var(--brand); padding: 0 6px;">
                <span style="font-size: 12px; color: var(--text-secondary);">个</span>
              </div>
            </div>
          </div>
        </div>

        <div style="display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border-subtle);">
          <input type="checkbox" id="dlConfirmSetDefaultCb" style="cursor: pointer; width: 16px; height: 16px; accent-color: var(--brand);">
          <label for="dlConfirmSetDefaultCb" style="font-size: 12.5px; color: var(--text-main); cursor: pointer; user-select: none;">将该目录设为以后的默认下载路径</label>
        </div>
      </div>

      <div class="modal-footer" style="padding: 14px 24px;">
        <button class="btn-secondary" onclick="closeModal('downloadConfirmModal')">取消</button>
        <button class="btn-primary" id="dlConfirmSubmitBtn" onclick="confirmStartDownload()">立即下载到 NAS</button>
      </div>
    </div>
  </div>

  <!-- Modal: Quick STRM Generator (一键生成目录/单文件 STRM) -->
  <div class="modal-backdrop" id="quickStrmModal">
    <div class="modal" style="width: 620px; max-width: 95vw;">
      <div class="modal-header">
        <div class="modal-title">
          <span style="font-size: 18px;">🎬</span>
          <span>一键生成 STRM 流媒体映射</span>
        </div>
        <button class="close-btn" onclick="closeModal('quickStrmModal')">&times;</button>
      </div>
      <div class="modal-body" style="padding: 20px 24px; gap: 16px;">
        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;">网盘源目标：</div>
          <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 12px 14px; display: flex; align-items: center; gap: 10px;">
            <span style="font-size: 20px; flex-shrink: 0;" id="quickStrmItemIcon">📁</span>
            <div style="font-weight: 600; font-size: 13.5px; line-height: 1.5; color: var(--text-main); word-break: break-all; flex: 1;" id="quickStrmItemName">--</div>
          </div>
        </div>

        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;">NAS 本地 STRM 输出目录：</div>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="quickStrmOutputDirInput" style="flex: 1; height: 38px; font-size: 13px; font-family: monospace; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 12px; color: var(--text-main);">
            <button class="btn-secondary" onclick="openFolderPicker(onQuickStrmFolderPicked, document.getElementById('quickStrmOutputDirInput').value)" style="height: 38px; padding: 0 16px; font-size: 13px; flex-shrink: 0;">📂 浏览目录</button>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px; line-height: 1.5;">
            💡 生成的 <code>.strm</code> 文件可直接被 <b>飞牛影视 / Emby / Jellyfin / Infuse</b> 识别刮削并 302 极速直连播放。
          </div>
        </div>

        <div style="display: flex; flex-direction: column; gap: 8px; padding: 10px 14px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border-subtle);">
          <label id="quickStrmKeepFolderRow" style="display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-main); cursor: pointer;">
            <input type="checkbox" id="quickStrmKeepFolderCb" checked style="width: 15px; height: 15px; accent-color: var(--brand);">
            <span>在输出目录下创建同名子文件夹（保持目录层级整洁）</span>
          </label>
          <label style="display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--text-main); cursor: pointer;">
            <input type="checkbox" id="quickStrmDlSubsCb" checked style="width: 15px; height: 15px; accent-color: var(--brand);">
            <span>同时下载同目录下的外挂字幕文件 (<code>.srt / .ass / .vtt</code>)</span>
          </label>
        </div>
      </div>

      <div class="modal-footer" style="padding: 14px 24px;">
        <button class="btn-secondary" onclick="closeModal('quickStrmModal')">取消</button>
        <button class="btn-primary" onclick="confirmQuickStrmGenerate()">⚡ 立即生成 STRM</button>
      </div>
    </div>
  </div>

  <!-- Modal: NAS Folder Picker (Enhanced with All Shares & Intelligent Navigation) -->
  <div class="modal-backdrop" id="folderPickerModal">
    <div class="modal" style="width: 660px; max-width: 95vw;">
      <div class="modal-header">
        <div class="modal-title">
          <span>📂</span>
          <span>选择 NAS 保存目录</span>
        </div>
        <button class="close-btn" onclick="closeModal('folderPickerModal')">&times;</button>
      </div>
      
      <div class="modal-body" style="padding: 16px 24px; gap: 10px;">
        <!-- Breadcrumb navigation -->
        <div id="folderPickerBreadcrumbs" style="display: flex; align-items: center; gap: 4px; font-size: 12.5px; overflow-x: auto; white-space: nowrap; padding: 2px 0; color: var(--text-muted);">
          <!-- populated by js -->
        </div>

        <!-- Quick Shortcuts -->
        <div style="display: flex; gap: 6px; overflow-x: auto; white-space: nowrap;">
          <button class="btn-secondary btn-sm" onclick="loadFolderPickerList('')">🏠 全部共享文件夹与存储卷</button>
          <button class="btn-secondary btn-sm" onclick="loadFolderPickerList(defaultDownloadDir)">📦 当前默认下载目录</button>
        </div>

        <div style="display: flex; gap: 8px; align-items: center;">
          <input type="text" id="folderPickerCurrentPath" readonly style="flex: 1; font-family: monospace; font-size: 12.5px; height: 34px; padding: 0 10px; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-main);" placeholder="当前选定路径...">
          <button class="btn-secondary btn-sm" onclick="folderPickerGoUp()" style="height: 34px; padding: 0 12px;">⬆️ 上一级</button>
          <button class="btn-primary btn-sm" onclick="folderPickerMkdir()" style="height: 34px; padding: 0 12px;">➕ 新建子文件夹</button>
        </div>

        <div class="fs-browser-list" id="folderPickerList" style="min-height: 220px; max-height: 340px;">
          <!-- populated by js -->
        </div>
      </div>

      <div class="modal-footer" style="padding: 14px 24px; display: flex; justify-content: space-between; align-items: center;">
        <div style="font-size: 12px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 380px;">
          选定: <span id="folderPickerFooterPath" style="color: var(--primary); font-family: monospace;">--</span>
        </div>
        <div style="display: flex; gap: 8px;">
          <button class="btn-secondary" onclick="closeModal('folderPickerModal')">取消</button>
          <button class="btn-primary" onclick="confirmFolderPicker()">确定选择该目录</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal: Cloud Add (Magnet & Torrent with Guangyapan Official Replica Layout) -->
  <div class="modal-backdrop" id="cloudAddModal">
    <div class="modal" style="width: 660px; max-width: 95vw; border-radius: 16px; padding: 22px;">
      <div class="modal-header" style="padding-bottom: 12px; margin-bottom: 12px; border-bottom: 1px solid var(--border-subtle);">
        <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 16px;">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="color: #ff6600;">
            <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>
            <polyline points="12 13 12 9 10 11"/>
          </svg>
          <span id="cloudAddModalTitle">云添加任务解析</span>
        </div>
        <button class="close-btn" onclick="closeModal('cloudAddModal')">&times;</button>
      </div>

      <!-- Step 1: Input Magnet / URL or Torrent -->
      <div id="cloudAddStep1">
        <div class="subtabs" style="margin-bottom: 16px; padding: 0;">
          <div class="subtabs-left" style="width: 100%;">
            <button class="subtab-btn active" id="tabMagnetBtn" onclick="switchCloudTab('magnet')" style="flex: 1; text-align: center;">磁力 / HTTP 链接</button>
            <button class="subtab-btn" id="tabTorrentBtn" onclick="switchCloudTab('torrent')" style="flex: 1; text-align: center;">BT 种子文件 (.torrent)</button>
          </div>
        </div>

        <div id="magnetSection" style="height: 180px; display: flex; flex-direction: column;">
          <p style="font-size: 13px; color: var(--text-secondary); margin-bottom: 8px;">输入 magnet:?xt=urn:btih: 磁力链接或 HTTP 下载直链：</p>
          <textarea id="magnetInput" style="flex: 1; width: 100%; box-sizing: border-box; resize: none; padding: 12px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-input); color: var(--text-main); font-size: 13px; line-height: 1.5; outline: none;" placeholder="magnet:?xt=urn:btih:... 或 http://... / https://..."></textarea>
        </div>

        <div id="torrentSection" style="height: 180px; display: none; flex-direction: column;">
          <div class="dropzone" onclick="document.getElementById('torrentFileInput').click()" style="flex: 1; width: 100%; box-sizing: border-box; border: 2px dashed var(--border); border-radius: 12px; background: var(--bg-input); display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; cursor: pointer; transition: all 0.2s;">
            <span style="font-size: 32px;">📑</span>
            <p id="torrentFileName" style="font-size: 13px; color: var(--text-main); font-weight: 500; margin: 0;">点击选择或拖拽 .torrent 种子文件至此处</p>
            <span style="font-size: 11.5px; color: var(--text-muted);">支持单个 BT 种子解析，快速提交至光鸭云端</span>
            <input type="file" id="torrentFileInput" accept=".torrent" style="display: none;" onchange="handleTorrentSelect(this)">
          </div>
        </div>

        <div class="modal-footer" style="margin-top: 18px; display: flex; justify-content: flex-end; gap: 10px;">
          <button class="btn-secondary" onclick="closeModal('cloudAddModal')">取消</button>
          <button class="btn-primary" id="cloudAddNextBtn" onclick="resolveCloudAdd()">下一步：解析任务 ➔</button>
        </div>
      </div>

      <!-- Step 2: Download File Selection & Destination Window (参考光鸭网页版窗口) -->
      <div id="cloudAddStep2" style="display: none;">
        <!-- Top Torrent Info Card -->
        <div style="border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; display: flex; align-items: center; gap: 12px; margin-bottom: 12px; background: var(--bg-card);">
          <div id="cloudAddTypeBadge" style="width: 44px; height: 44px; border-radius: 9px; background: #22c55e; color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 16px; flex-shrink: 0; box-shadow: 0 2px 6px rgba(34, 197, 94, 0.25);">
            BT
          </div>
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; gap: 6px; margin-bottom: 4px;">
              <input type="text" id="cloudAddTitleInput" style="font-weight: 600; font-size: 13.5px; border: 1px solid transparent; background: transparent; padding: 2px 6px; border-radius: 5px; color: var(--text); width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; outline: none; transition: all 0.15s;" title="点击可直接修改任务名称">
              <button type="button" onclick="focusTitleInput()" style="background: none; border: none; cursor: pointer; color: var(--text-dim); padding: 2px; font-size: 14px; flex-shrink: 0;" title="编辑任务名称">✏️</button>
            </div>
            <div style="font-size: 12px; color: var(--text-muted); display: flex; gap: 16px;">
              <span id="cloudAddFileCountDisplay">共 0 个文件</span>
              <span id="cloudAddTotalSizeDisplay">0 B</span>
            </div>
          </div>
        </div>

        <!-- Filter Bar -->
        <div style="display: flex; justify-content: space-between; align-items: center; padding: 4px 2px 10px 2px; font-size: 13px; color: var(--text);">
          <div style="display: flex; align-items: center; gap: 8px;">
            <input type="checkbox" id="cloudAddSelectAllCb" onchange="toggleSelectAllFiles(this.checked)" style="width: 16px; height: 16px; accent-color: #ff6600; cursor: pointer;">
            <label for="cloudAddSelectAllCb" style="cursor: pointer; font-weight: 500;">全选</label>
            <span id="cloudAddSelectedCountText" style="color: var(--text-muted); font-size: 12.5px; margin-left: 2px;">已选 0 项</span>
          </div>
          <div style="display: flex; align-items: center; gap: 14px;">
            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer; font-size: 13px;">
              <input type="checkbox" id="filterVideoCb" onchange="toggleFilterCategory('video', this.checked)" style="width: 15px; height: 15px; accent-color: #ff6600; cursor: pointer;">
              <span>视频</span>
            </label>
            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer; font-size: 13px;">
              <input type="checkbox" id="filterOtherCb" onchange="toggleFilterCategory('other', this.checked)" style="width: 15px; height: 15px; accent-color: #ff6600; cursor: pointer;">
              <span>其他</span>
            </label>
          </div>
        </div>

        <!-- File Table -->
        <div style="border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--bg-card); margin-bottom: 14px;">
          <div style="display: grid; grid-template-columns: 1fr 65px 85px; padding: 8px 12px; font-size: 12px; color: var(--text-dim); background: var(--bg-subtle); border-bottom: 1px solid var(--border-subtle);">
            <div>文件名</div>
            <div style="text-align: center;">格式</div>
            <div style="text-align: right;">大小</div>
          </div>
          <div id="cloudAddFileListContainer" style="max-height: 250px; overflow-y: auto;">
            <!-- Populated by JS -->
          </div>
        </div>

        <!-- Bottom Bar: Destination Picker & Action Button -->
        <div style="display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap;">
          <div class="cloud-dest-pill" id="cloudAddTargetFolderBtn" onclick="openCloudFolderPickerModal()" title="点击更改保存到网盘的具体文件夹位置">
            <span style="color: var(--text-dim); flex-shrink: 0;">添加到:</span>
            <span id="cloudAddTargetFolderDisplay" style="font-weight: 500; color: var(--text); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">全部文件/来自: 云添加</span>
            <span style="color: var(--text-dim); flex-shrink: 0;">剩余: <span id="cloudAddQuotaDisplay">--</span></span>
            <span style="font-size: 15px; flex-shrink: 0;">📁</span>
          </div>

          <div style="display: flex; align-items: center; gap: 10px; margin-left: auto;">
            <button class="btn-secondary" onclick="backToCloudAddStep1()" style="padding: 9px 14px; font-size: 13px;">⬅️ 返回修改</button>
            <button class="btn-primary" id="confirmCloudAddBtn" onclick="confirmSubmitCloudAdd()">立即添加</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal: Cloud Drive Destination Folder Picker (保存到 - 复刻光鸭网页版树状目录) -->
  <div class="modal-backdrop" id="cloudFolderPickerModal" style="z-index: 1050;">
    <div class="modal" style="width: 560px; max-width: 95vw; border-radius: 14px; padding: 22px;">
      <!-- Header -->
      <div class="modal-header" style="border-bottom: 1px solid var(--border-subtle); padding-bottom: 14px; margin-bottom: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 16px;">
          <span style="font-size: 18px;">☁️</span>
          <span>保存到</span>
        </div>
        <button class="close-btn" onclick="closeModal('cloudFolderPickerModal')">&times;</button>
      </div>

      <!-- Current Location & Restore Default Row -->
      <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; background: var(--bg-subtle); border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px; font-size: 13px;">
          <span style="color: var(--text-dim); flex-shrink: 0;">当前位置：</span>
          <span id="cloudTreeCurrentPath" style="color: var(--text); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;">全部文件/来自: 云添加</span>
          <span id="cloudTreeDefaultBadge" class="cloud-tree-tag-default">默认</span>
        </div>
        <button type="button" class="btn-secondary" onclick="restoreDefaultCloudFolderTree()" style="padding: 7px 14px; font-size: 12.5px; flex-shrink: 0; border-radius: 8px;">恢复默认</button>
      </div>

      <!-- Tree Container -->
      <div id="cloudFolderTreeContainer" class="cloud-tree-container">
        <!-- Rendered by JS -->
      </div>

      <!-- Footer Buttons -->
      <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 16px; border-top: 1px solid var(--border-subtle); padding-top: 14px;">
        <button type="button" class="btn-secondary" onclick="promptCreateFolderInTree()" style="display: flex; align-items: center; gap: 6px; padding: 8px 14px; font-size: 13px; border-radius: 8px;">
          <span style="font-size: 15px;">📁⁺</span>
          <span>新建文件夹</span>
        </button>
        <div style="display: flex; gap: 10px;">
          <button type="button" class="btn-secondary" onclick="closeModal('cloudFolderPickerModal')" style="padding: 8px 20px; font-size: 13px; border-radius: 8px;">取消</button>
          <button type="button" class="btn-primary" onclick="confirmCloudFolderTreeSelection()" style="padding: 8px 24px; font-size: 13px; border-radius: 8px; font-weight: 600;">确定</button>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal: Delete Cloud Offline Task Confirmation (删除离线任务) -->
  <div class="modal-backdrop" id="deleteCloudTaskModal" style="z-index: 1060;">
    <div class="modal" style="width: 440px; max-width: 95vw; border-radius: 14px; padding: 22px;">
      <div class="modal-header" style="border-bottom: 1px solid var(--border-subtle); padding-bottom: 12px; margin-bottom: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 15px; color: #ef4444;">
          <span>🗑️</span>
          <span>删除离线任务</span>
        </div>
        <button class="close-btn" onclick="closeModal('deleteCloudTaskModal')">&times;</button>
      </div>
      <div>
        <div style="font-size: 14px; color: var(--text); margin-bottom: 12px; word-break: break-all;">
          确定要从光鸭网盘中删除离线任务：<br>
          <strong id="deleteCloudTaskName" style="color: var(--text); display: inline-block; margin-top: 6px;">--</strong>
        </div>
        <div style="display: flex; align-items: center; gap: 8px; margin: 16px 0; padding: 10px 12px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border);">
          <input type="checkbox" id="deleteCloudFileCheckbox" style="cursor: pointer; width: 16px; height: 16px;">
          <label for="deleteCloudFileCheckbox" style="font-size: 13px; color: var(--text); cursor: pointer; user-select: none;">
            同时删除网盘已下载的对应文件
          </label>
        </div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; border-top: 1px solid var(--border-subtle); padding-top: 12px;">
        <button class="btn-secondary" onclick="closeModal('deleteCloudTaskModal')">取消</button>
        <button class="btn-danger" id="confirmDeleteCloudTaskBtn" onclick="confirmDeleteCloudTask()" style="padding: 7px 18px; border-radius: 8px; background: #ef4444; color: white; border: none; font-weight: 600; cursor: pointer;">确定删除</button>
      </div>
    </div>
  </div>

  <!-- Modal: Delete Local Transfer Task Confirmation (删除本地下载任务) -->
  <div class="modal-backdrop" id="deleteTransferModal" style="z-index: 1060;">
    <div class="modal" style="width: 460px; max-width: 95vw; border-radius: 14px; padding: 22px;">
      <div class="modal-header" style="border-bottom: 1px solid var(--border-subtle); padding-bottom: 12px; margin-bottom: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 15px; color: #ef4444;">
          <span>🗑️</span>
          <span id="deleteTransferModalTitle">删除下载任务</span>
        </div>
        <button class="close-btn" onclick="closeModal('deleteTransferModal')">&times;</button>
      </div>
      <div>
        <div style="font-size: 14px; color: var(--text); margin-bottom: 8px; word-break: break-all;">
          <span id="deleteTransferPromptLabel">确定要删除以下下载任务吗？</span><br>
          <strong id="deleteTransferTaskName" style="color: var(--text); display: inline-block; margin-top: 6px;">--</strong>
        </div>
        <div id="deleteTransferPathWrap" style="font-size: 12px; color: var(--text-muted); word-break: break-all; margin-bottom: 12px; padding: 6px 10px; background: var(--bg-input); border-radius: 6px; font-family: monospace;">
          NAS 路径: <span id="deleteTransferFilePath">--</span>
        </div>
        <div style="display: flex; align-items: center; gap: 8px; margin: 14px 0; padding: 10px 12px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border);">
          <input type="checkbox" id="deleteTransferFileCheckbox" style="cursor: pointer; width: 16px; height: 16px; accent-color: #ef4444;">
          <label for="deleteTransferFileCheckbox" style="font-size: 13px; color: var(--text); cursor: pointer; user-select: none; font-weight: 500;">
            同时删除 NAS 硬盘上的对应文件（含未完成临时分片）
          </label>
        </div>
      </div>
      <div class="modal-footer" style="display: flex; justify-content: flex-end; gap: 10px; margin-top: 16px; border-top: 1px solid var(--border-subtle); padding-top: 12px;">
        <button class="btn-secondary" onclick="closeModal('deleteTransferModal')">取消</button>
        <button class="btn-danger" id="confirmDeleteTransferBtn" onclick="confirmDeleteTransfer()" style="padding: 7px 18px; border-radius: 8px; background: #ef4444; color: white; border: none; font-weight: 600; cursor: pointer;">确定删除</button>
      </div>
    </div>
  </div>

  <!-- Modal: Duplicate File Warning (NAS 本地文件已存在提示) -->
  <div class="modal-backdrop" id="duplicateFileModal" style="z-index: 1060;">
    <div class="modal" style="width: 480px; max-width: 95vw; border-radius: 14px; padding: 22px;">
      <div class="modal-header" style="border-bottom: 1px solid var(--border-subtle); padding-bottom: 12px; margin-bottom: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 15px; color: #f59e0b;">
          <span>⚠️</span>
          <span>目标文件已存在</span>
        </div>
        <button class="close-btn" onclick="closeModal('duplicateFileModal')">&times;</button>
      </div>
      <div>
        <div style="font-size: 13.5px; color: var(--text); margin-bottom: 10px;">
          目标 NAS 目录中已存在同名文件：
        </div>
        <div id="duplicateFileNameDisplay" style="font-weight: 600; font-size: 14px; word-break: break-all; padding: 10px 12px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border); margin-bottom: 12px; color: var(--text);">
          --
        </div>
        <div style="font-size: 13px; color: var(--text-dim); margin-bottom: 18px;">
          请选择处理方式：
        </div>
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px;">
        <button class="btn-primary" onclick="resolveDuplicateAndDownload('rename')" style="padding: 9px 16px; border-radius: 8px; font-weight: 500; text-align: left; display: flex; justify-content: space-between; align-items: center;">
          <span>📄 保留两份（自动重命名）</span>
          <span id="duplicateSuggestedNameBadge" style="font-size: 11.5px; opacity: 0.85;">--</span>
        </button>
        <button class="btn-secondary" onclick="resolveDuplicateAndDownload('overwrite')" style="padding: 9px 16px; border-radius: 8px; font-weight: 500; text-align: left; display: flex; justify-content: space-between; align-items: center; border-color: #f59e0b; color: var(--text);">
          <span>⚠️ 覆盖原文件</span>
          <span style="font-size: 11.5px; color: var(--text-dim);">原文件将被替换</span>
        </button>
        <button class="btn-secondary" onclick="closeModal('duplicateFileModal')" style="padding: 8px 16px; border-radius: 8px; font-weight: 500; text-align: center; margin-top: 4px;">
          取消下载
        </button>
      </div>
    </div>
  </div>

  <!-- Modal: Upload -->
  <div class="modal-backdrop" id="uploadModal">
    <div class="modal" style="width: 560px; max-width: 95vw;">
      <div class="modal-header">
        <div class="modal-title">
          <span>⬆️</span>
          <span>上传文件到光鸭网盘</span>
        </div>
        <button class="close-btn" onclick="closeModal('uploadModal')">&times;</button>
      </div>
      
      <div class="modal-body" style="padding: 20px 24px; gap: 16px;">
        <p style="font-size: 12.5px; color: var(--text-muted); margin: 0; line-height: 1.5;">支持极速秒传 (基于迅雷哈希算法)，未命中时直传阿里云 OSS</p>
        
        <div class="dropzone" onclick="document.getElementById('uploadFileInput').click()" style="padding: 32px 20px; border: 2px dashed var(--border); border-radius: 12px; background: var(--bg-input); cursor: pointer; text-align: center; transition: all 0.2s;">
          <div style="font-size: 32px; margin-bottom: 8px;">📤</div>
          <p id="uploadFileNameText" style="font-size: 13.5px; font-weight: 500; color: var(--text-main); margin: 0;">点击选择或拖拽文件到此处</p>
          <input type="file" id="uploadFileInput" style="display: none;" onchange="handleUploadSelect(this)">
        </div>

        <div id="uploadProgressSection" style="display: none;">
          <div class="task-details" style="margin-bottom: 6px;">
            <span id="uploadStatusLabel">准备中...</span>
            <span id="uploadPctLabel">0%</span>
          </div>
          <div class="progress-bar-bg">
            <div class="progress-bar-fill" id="uploadProgressBar"></div>
          </div>
        </div>
      </div>

      <div class="modal-footer" style="padding: 14px 24px;">
        <button class="btn-secondary" onclick="closeModal('uploadModal')">关闭</button>
        <button class="btn-primary" id="startUploadBtn" onclick="submitUpload()">开始上传</button>
      </div>
    </div>
  </div>

  
  <!-- Modal: Official Guangyapan Login (QR Code / Token) -->
  <div class="modal-backdrop" id="loginModal" onclick="if(event.target===this)closeLoginModal()">
    <div class="modal" style="width: 460px; max-width: 95vw; overflow-y: auto;">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 8px;">
          <img src="/icon.png?v=1.1.1" style="width: 26px; height: 26px; object-fit: contain; border-radius: 6px;">
          <span>光鸭网盘 · 账号登录</span>
        </div>
        <button class="close-btn" onclick="closeLoginModal()">&times;</button>
      </div>

      <!-- Tab Buttons -->
      <div class="subtabs" style="margin-bottom: 16px;">
        <div class="subtabs-left" style="width: 100%;">
          <button class="subtab-btn active" id="tabLoginQr" onclick="switchLoginTab('qr')" style="flex: 1; text-align: center;">📱 官方扫码登录（推荐）</button>
          <button class="subtab-btn" id="tabLoginToken" onclick="switchLoginTab('token')" style="flex: 1; text-align: center;">🔑 Token 导入</button>
        </div>
      </div>

      <!-- Tab 1: QR Code Login -->
      <div id="loginTabContentQr" style="display: flex; flex-direction: column; align-items: center; padding: 6px 0 10px 0;">
        <div id="qrCodeContainer" style="width: 200px; height: 200px; background: #ffffff; padding: 10px; border-radius: 12px; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 15px rgba(0,0,0,0.18); margin-bottom: 14px; position: relative; border: 1px solid var(--border);">
          <div id="qrCodeCanvas"></div>
          <div id="qrExpiredMask" style="display: none; position: absolute; inset: 0; background: rgba(0,0,0,0.85); border-radius: 12px; flex-direction: column; align-items: center; justify-content: center; color: #fff; cursor: pointer;" onclick="refreshDeviceCode()">
            <div style="font-size: 28px;">🔄</div>
            <div style="font-size: 13.5px; font-weight: 600; margin-top: 6px;">二维码已过期</div>
            <div style="font-size: 11.5px; color: #94a3b8; margin-top: 2px;">点击刷新</div>
          </div>
        </div>

        <div id="loginStatusBadge" class="status-badge status-active" style="margin-bottom: 10px; font-size: 12px;">
          ⌛ 等待扫码...
        </div>

        <div style="font-size: 12.5px; color: var(--text-muted); text-align: center; line-height: 1.6; max-width: 320px;">
          请使用手机 <b>光鸭网盘 APP</b> 扫码<br>
          在手机端点击【确认登录】即可自动完成授权
        </div>

        <div style="margin-top: 16px; display: flex; justify-content: center;">
          <button class="btn-primary" style="width: 160px; justify-content: center; padding: 8px 20px;" onclick="refreshDeviceCode()">🔄 刷新二维码</button>
        </div>
      </div>

      <!-- Tab 2: Token Login -->
      <div id="loginTabContentToken" style="display: none; flex-direction: column; gap: 12px; padding: 6px 0 10px 0;">
        <div style="font-size: 12.5px; color: var(--text-muted); line-height: 1.5;">
          支持填入已有光鸭网盘账号的 <code>Access Token</code> 及 <code>Refresh Token</code>：
        </div>
        <div>
          <label style="font-size: 12px; font-weight: 600; margin-bottom: 4px; display: block;">Access Token (必填)</label>
          <textarea id="inputAccessToken" rows="3" placeholder="eyJhbGciOiJSUzI1NiIs..."></textarea>
        </div>
        <div>
          <label style="font-size: 12px; font-weight: 600; margin-bottom: 4px; display: block;">Refresh Token (可选)</label>
          <input type="text" id="inputRefreshToken" placeholder="gy.4kf...">
        </div>
        <button class="btn-primary" style="margin-top: 8px; justify-content: center;" onclick="submitTokenLogin()">确认登录</button>
      </div>
    </div>
  </div>

  <!-- Toast Component -->
  <div id="toast"></div>

  <script>

    // ================= Category Filtering & Navigation =================
    let currentRawFiles = [];
    let activeCategory = 'all';
    let searchQuery = '';
    let currentViewMode = 'list';

    const VIDEO_EXTS = ['mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm', 'ts', 'm4v', 'rmvb', 'iso'];
    const IMG_EXTS = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'heic'];
    const DOC_EXTS = ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'txt', 'md', 'epub'];
    const AUDIO_EXTS = ['mp3', 'flac', 'wav', 'aac', 'ogg', 'm4a', 'wma', 'ape'];
    const INSTALL_EXTS = ['fpk', 'apk', 'exe', 'msi', 'dmg', 'pkg', 'deb', 'rpm', 'zip', 'rar', '7z', 'tar', 'gz'];

    function isVideoFile(fileName) {
      if (!fileName) return false;
      const ext = fileName.split('.').pop().toLowerCase();
      return VIDEO_EXTS.includes(ext);
    }

    function switchCategory(cat) {
      activeCategory = cat;
      currentFilePage = 1;
      document.querySelectorAll('.cat-tab-item').forEach(el => {
        el.classList.toggle('active', el.dataset.cat === cat);
      });
      renderFileList();
    }

    function handleSearch(val) {
      searchQuery = (val || '').trim().toLowerCase();
      currentFilePage = 1;
      renderFileList();
    }

    function setViewMode(mode) {
      currentViewMode = mode;
      document.getElementById('viewModeListBtn')?.classList.toggle('active', mode === 'list');
      document.getElementById('viewModeGridBtn')?.classList.toggle('active', mode === 'grid');
      renderFileList();
    }

    let selectedFileIds = new Set();
    let currentFilteredFiles = [];
    let currentPageFiles = [];
    let filePageSize = 20;
    let currentFilePage = 1;
    let maxConcurrentDownloads = 3;

    function getTotalFilePages() {
      if (!filePageSize || filePageSize <= 0) return 1;
      return Math.max(1, Math.ceil(currentFilteredFiles.length / filePageSize));
    }

    function syncFilePageSizeInputs(val) {
      let n = parseInt(val, 10);
      if (isNaN(n) || n < 0) n = 20;
      if (n > 5000) n = 5000;
      filePageSize = n;

      const sel = document.getElementById("filePageSizeSelect");
      const customInp = document.getElementById("filePageSizeCustomInput");
      const settingInp = document.getElementById("settingFilePageSizeInput");
      const presets = ["10", "20", "50", "100", "200", "0"];

      if (sel) {
        sel.value = presets.includes(String(n)) ? String(n) : "custom";
      }
      if (customInp) customInp.value = n;
      if (settingInp) settingInp.value = n;
    }

    function onFilePageSizeSelectChange(val) {
      if (val === "custom") {
        const customInp = document.getElementById("filePageSizeCustomInput");
        if (customInp) {
          customInp.focus();
          customInp.select();
        }
        return;
      }
      setFilePageSize(val);
    }

    async function setFilePageSize(val) {
      let n = parseInt(val, 10);
      if (isNaN(n) || n < 0) n = 20;
      if (n > 5000) n = 5000;
      syncFilePageSizeInputs(n);
      currentFilePage = 1;
      renderFileList();
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ file_page_size: n })
        });
        showToast(n === 0 ? "已设为单页全部显示" : `已设置每页显示 ${n} 项`);
      } catch (e) {
        console.warn("Save file_page_size failed:", e);
      }
    }

    function goToFilePage(page) {
      const totalPages = getTotalFilePages();
      let p = parseInt(page, 10);
      if (isNaN(p)) p = 1;
      p = Math.max(1, Math.min(totalPages, p));
      if (p === currentFilePage && document.getElementById("filePageJumpInput")) {
        document.getElementById("filePageJumpInput").value = p;
      }
      currentFilePage = p;
      renderFileList();
      const tc = document.getElementById("fileTableContainer");
      const gc = document.getElementById("fileGridContainer");
      if (tc) tc.scrollTop = 0;
      if (gc) gc.scrollTop = 0;
    }

    function changeFilePage(delta) {
      goToFilePage(currentFilePage + delta);
    }

    function renderFilePaginationBar() {
      const totalItems = currentFilteredFiles.length;
      const totalPages = getTotalFilePages();
      if (currentFilePage > totalPages) currentFilePage = totalPages;
      if (currentFilePage < 1) currentFilePage = 1;

      const infoEl = document.getElementById("filePaginationInfo");
      const totalPagesEl = document.getElementById("fileTotalPagesText");
      const jumpInput = document.getElementById("filePageJumpInput");
      const btnFirst = document.getElementById("btnFirstPage");
      const btnPrev = document.getElementById("btnPrevPage");
      const btnNext = document.getElementById("btnNextPage");
      const btnLast = document.getElementById("btnLastPage");
      const numContainer = document.getElementById("filePageNumbersContainer");

      if (infoEl) {
        if (totalItems === 0) {
          infoEl.innerText = "共 0 项";
        } else if (!filePageSize || filePageSize <= 0) {
          infoEl.innerText = `共 ${totalItems} 项 (全部显示)`;
        } else {
          const startIdx = (currentFilePage - 1) * filePageSize + 1;
          const endIdx = Math.min(totalItems, currentFilePage * filePageSize);
          infoEl.innerText = `显示第 ${startIdx} - ${endIdx} 项 / 共 ${totalItems} 项`;
        }
      }

      if (totalPagesEl) totalPagesEl.innerText = totalPages;
      if (jumpInput) {
        jumpInput.max = totalPages;
        jumpInput.value = currentFilePage;
      }
      if (btnFirst) btnFirst.disabled = (currentFilePage <= 1);
      if (btnPrev) btnPrev.disabled = (currentFilePage <= 1);
      if (btnNext) btnNext.disabled = (currentFilePage >= totalPages);
      if (btnLast) btnLast.disabled = (currentFilePage >= totalPages);

      if (numContainer) {
        numContainer.innerHTML = "";
        const maxButtons = 5;
        let startPage = Math.max(1, currentFilePage - 2);
        let endPage = Math.min(totalPages, startPage + maxButtons - 1);
        if (endPage - startPage + 1 < maxButtons) {
          startPage = Math.max(1, endPage - maxButtons + 1);
        }

        for (let p = startPage; p <= endPage; p++) {
          const btn = document.createElement("button");
          btn.className = "page-btn" + (p === currentFilePage ? " active" : "");
          btn.innerText = p;
          btn.onclick = () => goToFilePage(p);
          numContainer.appendChild(btn);
        }
      }
    }

    function toggleSelectAllTableFiles(checked) {
      const targetList = currentPageFiles.length > 0 ? currentPageFiles : currentFilteredFiles;
      if (checked) {
        targetList.forEach(item => {
          if (item && item.fileId) selectedFileIds.add(item.fileId);
        });
      } else {
        targetList.forEach(item => {
          if (item && item.fileId) selectedFileIds.delete(item.fileId);
        });
      }
      document.querySelectorAll('.table-file-cb').forEach(cb => {
        cb.checked = selectedFileIds.has(cb.dataset.fileid);
      });
      renderFileList();
    }

    function selectAllFilteredFiles() {
      currentFilteredFiles.forEach(item => {
        if (item && item.fileId) selectedFileIds.add(item.fileId);
      });
      renderFileList();
    }

    function onFileCheckboxChange(fileId, checked) {
      if (checked) {
        selectedFileIds.add(fileId);
      } else {
        selectedFileIds.delete(fileId);
      }
      updateBatchSelectionUI();
    }

    function updateBatchSelectionUI() {
      const pageIds = currentPageFiles.map(f => f.fileId).filter(Boolean);
      const allPageSelected = pageIds.length > 0 && pageIds.every(id => selectedFileIds.has(id));
      const selectAllCb = document.getElementById("tableSelectAllCb");
      if (selectAllCb) {
        selectAllCb.checked = allPageSelected;
        selectAllCb.indeterminate = !allPageSelected && pageIds.some(id => selectedFileIds.has(id));
      }

      const selectedCount = selectedFileIds.size;
      const batchDlBtn = document.getElementById("batchDownloadBtn");
      const batchDlText = document.getElementById("batchDownloadBtnText");
      const batchDlIcon = document.getElementById("batchDownloadBtnIcon");
      const batchDelBtn = document.getElementById("batchDeleteBtn");
      const batchDelCount = document.getElementById("batchDeleteCountText");

      if (batchDlBtn && batchDlText) {
        if (selectedCount > 0) {
          batchDlBtn.className = "btn-primary";
          batchDlText.innerText = `批量下载 (${selectedCount})`;
          if (batchDlIcon) batchDlIcon.style.color = "currentColor";
        } else {
          batchDlBtn.className = "btn-secondary";
          batchDlText.innerText = "批量下载";
          if (batchDlIcon) batchDlIcon.style.color = "var(--brand)";
        }
      }

      if (batchDelBtn && batchDelCount) {
        if (selectedCount > 0) {
          batchDelBtn.style.display = "inline-flex";
          batchDelCount.innerText = selectedCount;
        } else {
          batchDelBtn.style.display = "none";
        }
      }

      updateBreadcrumbStats();
    }

    function updateBreadcrumbStats() {
      const statsEl = document.getElementById("fileListStatsText");
      if (!statsEl) return;
      const totalRaw = currentRawFiles.length;
      const visibleCount = currentFilteredFiles.length;
      const pageCount = currentPageFiles.length;
      const selCount = selectedFileIds.size;

      let html = "";
      if (selCount > 0) {
        html += `<span style="color: var(--brand); font-weight: 600;">已选 ${selCount} 项</span>`;
        if (selCount < visibleCount) {
          html += ` <span style="margin: 0 6px; color: var(--border);">|</span> `;
          html += `<a href="javascript:void(0)" onclick="selectAllFilteredFiles()" style="color: var(--brand); text-decoration: none; font-weight: 500;">全选全部 (${visibleCount}项)</a>`;
        }
        html += ` <span style="margin: 0 6px; color: var(--border);">|</span> `;
        html += `<a href="javascript:void(0)" onclick="selectedFileIds.clear(); renderFileList();" style="color: var(--text-secondary); text-decoration: none;">取消选择</a>`;
        html += ` <span style="margin: 0 6px; color: var(--border);">|</span> `;
      } else if (visibleCount > 0) {
        html += `<a href="javascript:void(0)" onclick="toggleSelectAllTableFiles(true)" style="color: var(--brand); text-decoration: none; font-weight: 500;">全选本页 (${pageCount}项)</a>`;
        if (visibleCount > pageCount) {
          html += ` <span style="margin: 0 6px; color: var(--border);">|</span> `;
          html += `<a href="javascript:void(0)" onclick="selectAllFilteredFiles()" style="color: var(--brand); text-decoration: none; font-weight: 500;">全选全部 (${visibleCount}项)</a>`;
        }
        html += ` <span style="margin: 0 6px; color: var(--border);">|</span> `;
      }

      if (visibleCount !== totalRaw) {
        html += `筛选 ${visibleCount} / 共 ${totalRaw} 项`;
      } else {
        html += `共 ${totalRaw} 项`;
      }
      statsEl.innerHTML = html;
    }


    // State
    let currentView = "files";
    let currentParentId = "";
    let breadcrumbStack = [{ id: "", name: "网盘文件" }];
    let activeCloudTab = "magnet";
    let selectedTorrentFile = null;
    let selectedUploadFile = null;
    let currentTransferTab = "active";
    let offlineFilter = "all";
    let allOfflineTasks = [];
    let defaultDownloadDir = "";

    // Download modal state
    let pendingDownloadFileId = "";
    let pendingDownloadFileName = "";
    let pendingDownloadItems = []; // [{fileId, fileName, resType, fileSize}]

    // Folder Picker state
    let folderPickerCallback = null;
    let folderPickerPath = "";
    let folderPickerSelectedPath = "";

    function showToast(msg) {
      const t = document.getElementById("toast");
      t.innerText = msg;
      t.classList.add("show");
      setTimeout(() => t.classList.remove("show"), 2800);
    }

    function formatBytes(bytes) {
      if (bytes === 0) return "0 B";
      if (!bytes || isNaN(bytes)) return "--";
      const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return (bytes / Math.pow(k, i)).toFixed(2) + ' ' + sizes[i];
    }

    function formatDate(ts) {
      if (!ts) return "--";
      const d = new Date(ts * 1000);
      return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
    }

    function switchView(viewName) {
      currentView = viewName;
      document.querySelectorAll(".rail-btn").forEach(el => el.classList.remove("active"));
      document.querySelectorAll(".content-view").forEach(el => el.classList.remove("active"));
      document.getElementById("topTransfersBtn")?.classList.remove("active");

      if (viewName === 'files') {
        document.getElementById("navFiles")?.classList.add("active");
        document.getElementById("view-files")?.classList.add("active");
        updateBreadcrumbs();
      } else if (viewName === 'transfers') {
        document.getElementById("navTransfers")?.classList.add("active");
        document.getElementById("view-transfers")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⬇️ 本地下载与传输任务</span>`;
        loadTransfers();
      } else if (viewName === 'offline') {
        document.getElementById("navOffline")?.classList.add("active");
        document.getElementById("view-offline")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⚡ 云端离线下载 (磁力/BT)</span>`;
        loadCloudTasks();
      } else if (viewName === 'strm') {
        document.getElementById("navStrm")?.classList.add("active");
        document.getElementById("view-strm")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">🎬 STRM 影视库 &amp; 302 动态串流网关</span>`;
        loadStrmView(true);
      } else if (viewName === 'logs') {
        document.getElementById("navLogs")?.classList.add("active");
        document.getElementById("view-logs")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">📜 系统运行日志中心</span>`;
        loadLogs(true);
      } else if (viewName === 'trash') {
        document.getElementById("navTrash")?.classList.add("active");
        document.getElementById("view-trash")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">🗑️ 回收站</span>`;
      } else if (viewName === 'settings') {
        document.getElementById("navSettings")?.classList.add("active");
        document.getElementById("view-settings")?.classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⚙️ 系统设置</span>`;
        loadSettings();
      }
    }

    function refreshCurrentView() {
      if (currentView === 'files') loadFiles(currentParentId);
      else if (currentView === 'transfers') loadTransfers();
      else if (currentView === 'offline') loadCloudTasks();
      else if (currentView === 'strm') loadStrmView(true);
      else if (currentView === 'logs') loadLogs(true);
      else if (currentView === 'trash') switchView('trash');
      else if (currentView === 'settings') loadSettings();
    }

    // ================= VIEW: FILES =================
    
    function renderFileList() {
      const tbody = document.getElementById("fileTableBody");
      const grid = document.getElementById("fileGridContainer");
      const tableWrap = document.getElementById("fileTableContainer");

      let filtered = currentRawFiles.slice();

      // 1. Filter by category
      if (activeCategory === 'video') {
        filtered = filtered.filter(f => f.resType !== 2 && VIDEO_EXTS.includes((f.fileName || '').split('.').pop().toLowerCase()));
      } else if (activeCategory === 'img') {
        filtered = filtered.filter(f => f.resType !== 2 && IMG_EXTS.includes((f.fileName || '').split('.').pop().toLowerCase()));
      } else if (activeCategory === 'doc') {
        filtered = filtered.filter(f => f.resType !== 2 && DOC_EXTS.includes((f.fileName || '').split('.').pop().toLowerCase()));
      } else if (activeCategory === 'audio') {
        filtered = filtered.filter(f => f.resType !== 2 && AUDIO_EXTS.includes((f.fileName || '').split('.').pop().toLowerCase()));
      } else if (activeCategory === 'installer') {
        filtered = filtered.filter(f => f.resType !== 2 && INSTALL_EXTS.includes((f.fileName || '').split('.').pop().toLowerCase()));
      } else if (activeCategory === 'recent') {
        filtered.sort((a, b) => (b.utime || b.ctime || 0) - (a.utime || a.ctime || 0));
      }

      // 2. Filter by search query
      if (searchQuery) {
        filtered = filtered.filter(f => (f.fileName || '').toLowerCase().includes(searchQuery));
      }

      currentFilteredFiles = filtered;
      const totalPages = getTotalFilePages();
      if (currentFilePage > totalPages) currentFilePage = totalPages;
      if (currentFilePage < 1) currentFilePage = 1;

      if (!filePageSize || filePageSize <= 0) {
        currentPageFiles = filtered;
      } else {
        const startIdx = (currentFilePage - 1) * filePageSize;
        currentPageFiles = filtered.slice(startIdx, startIdx + filePageSize);
      }

      renderFilePaginationBar();
      updateBatchSelectionUI();

      if (filtered.length === 0) {
        const emptyHtml = `
          <tr>
            <td colspan="5" style="padding: 0; border: none;">
              <div class="empty-container">
                <img src="/empty.png" class="empty-duck-img" alt="暂无文件">
                <div class="empty-title">暂无相关文件</div>
                <div class="empty-sub">点击左上角「+ 上传」或「云添加」快速存储资源</div>
              </div>
            </td>
          </tr>
        `;
        tbody.innerHTML = emptyHtml;
        if (grid) grid.innerHTML = emptyHtml;
        return;
      }

      if (currentViewMode === 'grid') {
        if (tableWrap) tableWrap.style.display = 'none';
        if (grid) {
          grid.style.display = 'grid';
          grid.innerHTML = '';
          currentPageFiles.forEach(item => {
            const isDir = (item.resType === 2);
            const isChecked = selectedFileIds.has(item.fileId);
            const icon = isDir ? "📁" : getFileIcon(item.fileName);
            const card = document.createElement('div');
            card.style.cssText = `background: ${isChecked ? 'var(--bg-active)' : 'var(--bg-card)'}; border: 1px solid ${isChecked ? 'var(--brand)' : 'var(--border)'}; border-radius: var(--radius-md); padding: 14px; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; text-align: center; transition: all 0.2s; position: relative;`;
            card.onmouseenter = () => card.style.borderColor = 'var(--brand)';
            card.onmouseleave = () => card.style.borderColor = selectedFileIds.has(item.fileId) ? 'var(--brand)' : 'var(--border)';
            card.onclick = () => {
              if (isDir) navigateTo(item.fileId, item.fileName);
              else promptStartDownload(item.fileId, item.fileName, 1, item.fileSize || 0);
            };
            card.innerHTML = `
              <input type="checkbox" class="table-file-cb" data-fileid="${item.fileId}" ${isChecked ? 'checked' : ''}
                style="position: absolute; top: 10px; left: 10px; width: 16px; height: 16px; accent-color: var(--brand); cursor: pointer;"
                onclick="event.stopPropagation();" onchange="onFileCheckboxChange('${item.fileId}', this.checked); renderFileList();">
              <div style="font-size: 38px; margin-bottom: 8px;">${icon}</div>
              <div style="font-size: 12px; font-weight: 500; color: var(--text-main); width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(item.fileName)}">${escapeHtml(item.fileName)}</div>
              <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">${isDir ? '文件夹' : formatBytes(item.fileSize)}</div>
            `;
            grid.appendChild(card);
          });
        }
        return;
      }

      // Default Table View
      if (tableWrap) tableWrap.style.display = 'block';
      if (grid) grid.style.display = 'none';

      tbody.innerHTML = "";
      currentPageFiles.forEach(item => {
        const tr = document.createElement("tr");
        const isDir = (item.resType === 2);
        const isChecked = selectedFileIds.has(item.fileId);
        const icon = isDir ? "📁" : getFileIcon(item.fileName);
        if (isChecked) {
          tr.style.background = "var(--bg-active)";
        }

        tr.innerHTML = `
          <td onclick="if(event.target.tagName!=='INPUT'){const cb=this.querySelector('input');cb.checked=!cb.checked;cb.dispatchEvent(new Event('change'));}" style="cursor: pointer;">
            <input type="checkbox" class="table-file-cb" data-fileid="${item.fileId}" ${isChecked ? 'checked' : ''}
              style="width: 15px; height: 15px; accent-color: var(--brand); cursor: pointer;"
              onchange="onFileCheckboxChange('${item.fileId}', this.checked); this.closest('tr').style.background = this.checked ? 'var(--bg-active)' : '';">
          </td>
          <td>
            <div class="file-name-cell" onclick="${isDir ? `navigateTo('${item.fileId}', '${escapeHtml(item.fileName)}')` : `promptStartDownload('${item.fileId}', '${escapeHtml(item.fileName)}', 1, ${item.fileSize || 0})`}">
              <span class="file-icon">${icon}</span>
              <span class="file-name-text" title="${escapeHtml(item.fileName)}">${escapeHtml(item.fileName)}</span>
            </div>
          </td>
          <td>${isDir ? '--' : formatBytes(item.fileSize)}</td>
          <td>${formatDate(item.utime || item.ctime)}</td>
          <td style="text-align: right; width: 300px; white-space: nowrap;">
            <div class="actions-cell">
              ${isDir ? `<button class="btn-secondary btn-sm" onclick="openQuickStrmModal('${item.fileId}', '${escapeHtml(item.fileName)}', true)" title="将该文件夹递归生成为本地 STRM 影视库">🎬 STRM</button>` : ''}
              ${!isDir && isVideoFile(item.fileName) ? `<button class="btn-secondary btn-sm desktop-only-btn" onclick="openQuickStrmModal('${item.fileId}', '${escapeHtml(item.fileName)}', false)" title="生成该视频的 .strm 流媒体文件">🎬 STRM</button>` : ''}
              <button class="btn-primary btn-sm" onclick="promptStartDownload('${item.fileId}', '${escapeHtml(item.fileName)}', ${isDir ? 2 : 1}, ${item.fileSize || 0})">⬇️<span class="desktop-text"> 下载到NAS</span><span class="mobile-text"> 下载</span></button>
              ${!isDir ? `<button class="btn-secondary btn-sm desktop-only-btn" onclick="copyLink('${item.fileId}')">🔗 直链</button>` : ''}
              <button class="btn-danger btn-sm" onclick="deleteFile('${item.fileId}', '${escapeHtml(item.fileName)}')">🗑️</button>
            </div>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function loadFiles(parentId = currentParentId) {
      currentParentId = parentId;
      currentFilePage = 1;
      selectedFileIds.clear();
      const tbody = document.getElementById("fileTableBody");
      tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 50px;">正在加载目录文件...</td></tr>`;

      try {
        const res = await fetch(`/api/files?parentId=${encodeURIComponent(parentId)}`);
        const json = await res.json();
        if (json.code === 401 || (!isLoggedIn && (!json.list || json.list.length === 0))) {
          currentRawFiles = [];
          currentFilteredFiles = [];
          currentPageFiles = [];
          renderFilePaginationBar();
          updateBatchSelectionUI();
          tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 50px;"><div style="font-size: 15px; font-weight: 600; margin-bottom: 8px;">⚠️ 当前未登录光鸭网盘账号</div><div style="color: var(--text-muted); font-size: 13px; margin-bottom: 16px;">登录后即可查看网盘文件并开始疾速上传下载</div><button class="btn-primary" onclick="openLoginModal()">📱 立即扫码登录</button></td></tr>`;
          return;
        }
        currentRawFiles = json.list || [];
        renderFileList();
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: #f87171; padding: 40px;">加载失败: ${e.message}</td></tr>`;
      }
    }

function escapeHtml(str) {
      if (!str) return "";
      return str.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function getFileIcon(name) {
      if (!name) return "📄";
      const ext = name.split('.').pop().toLowerCase();
      if (['mp4','mkv','avi','mov','rmvb','wmv','flv'].includes(ext)) return "🎬";
      if (['mp3','flac','wav','aac','m4a','ogg'].includes(ext)) return "🎵";
      if (['jpg','jpeg','png','gif','webp','bmp','svg'].includes(ext)) return "🖼️";
      if (['zip','rar','7z','tar','gz','iso'].includes(ext)) return "📦";
      if (['pdf','doc','docx','txt','md','xls','xlsx'].includes(ext)) return "📝";
      if (['torrent'].includes(ext)) return "⚡";
      return "📄";
    }

    function updateBreadcrumbs() {
      const c = document.getElementById("breadcrumbs");
      c.innerHTML = "";
      const leftWrap = document.createElement("div");
      leftWrap.style.cssText = "display: flex; align-items: center; gap: 4px; flex-wrap: wrap;";

      breadcrumbStack.forEach((b, idx) => {
        const span = document.createElement("span");
        span.innerText = b.name;
        if (idx === breadcrumbStack.length - 1) {
          span.className = "crumb-item current";
        } else {
          span.className = "crumb-item";
          span.onclick = () => {
            breadcrumbStack = breadcrumbStack.slice(0, idx + 1);
            updateBreadcrumbs();
            loadFiles(b.id);
          };
        }
        leftWrap.appendChild(span);
        if (idx < breadcrumbStack.length - 1) {
          const sep = document.createElement("span");
          sep.innerText = " / ";
          sep.className = "crumb-sep";
          leftWrap.appendChild(sep);
        }
      });

      const rightStats = document.createElement("span");
      rightStats.id = "fileListStatsText";
      rightStats.style.cssText = "margin-left: auto; font-size: 12px; color: var(--text-muted); font-weight: 500; white-space: nowrap;";
      c.appendChild(leftWrap);
      c.appendChild(rightStats);
      updateBreadcrumbStats();
    }

    function navigateTo(id, name) {
      if (id === "") {
        breadcrumbStack = [{ id: "", name: "网盘文件" }];
      } else {
        breadcrumbStack.push({ id, name });
      }
      updateBreadcrumbs();
      loadFiles(id);
    }

    async function promptCreateFolder() {
      const name = prompt("请输入新文件夹名称：");
      if (!name || !name.trim()) return;
      try {
        const res = await fetch("/api/create-folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim(), parentId: currentParentId })
        });
        const d = await res.json();
        if (d.msg === "success" || d.code === 0) {
          showToast("文件夹创建成功");
          loadFiles();
        } else {
          alert("创建失败: " + (d.msg || JSON.stringify(d)));
        }
      } catch (e) {
        alert("网络错误: " + e.message);
      }
    }

    async function deleteFile(fileId, name) {
      if (!confirm(`确定要删除 "${name}" 吗？`)) return;
      try {
        const res = await fetch("/api/delete-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fileId })
        });
        const d = await res.json();
        if (!res.ok || d.error || (d.code !== undefined && d.code !== 0)) {
          throw new Error(d.error || d.msg || `HTTP ${res.status}`);
        }
        selectedFileIds.delete(fileId);
        showToast("已成功删除");
        loadFiles();
      } catch (e) {
        alert("删除失败: " + e.message);
      }
    }

    async function promptBatchDelete() {
      const ids = Array.from(selectedFileIds);
      if (ids.length === 0) {
        showToast("请先勾选要删除的文件或文件夹");
        return;
      }
      if (!confirm(`确定要批量删除已选中的 ${ids.length} 个项目吗？`)) return;
      try {
        const res = await fetch("/api/delete-file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ fileId: ids })
        });
        const d = await res.json();
        if (!res.ok || d.error || (d.code !== undefined && d.code !== 0)) {
          throw new Error(d.error || d.msg || `HTTP ${res.status}`);
        }
        selectedFileIds.clear();
        showToast(`🗑️ 已成功删除 ${ids.length} 个项目`);
        loadFiles();
      } catch (e) {
        alert("批量删除失败: " + e.message);
      }
    }

    async function copyToClipboard(text) {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (e) {
          console.warn("navigator.clipboard.writeText failed, using execCommand fallback", e);
        }
      }

      try {
        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.position = "fixed";
        textArea.style.top = "0";
        textArea.style.left = "0";
        textArea.style.width = "2em";
        textArea.style.height = "2em";
        textArea.style.padding = "0";
        textArea.style.border = "none";
        textArea.style.outline = "none";
        textArea.style.boxShadow = "none";
        textArea.style.background = "transparent";
        textArea.style.opacity = "0";
        textArea.setAttribute("readonly", "");
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        textArea.setSelectionRange(0, text.length);
        const successful = document.execCommand("copy");
        document.body.removeChild(textArea);
        if (successful) return true;
      } catch (err) {
        console.warn("execCommand copy failed:", err);
      }

      window.prompt("请复制 CDN 极速下载直链 (按 Ctrl+C):", text);
      return true;
    }

    async function copyLink(fileId) {
      try {
        showToast("正在获取直链...");
        const res = await fetch(`/api/download-url?fileId=${encodeURIComponent(fileId)}`);
        const d = await res.json();
        if (d.signedURL) {
          await copyToClipboard(d.signedURL);
          showToast("CDN 极速下载直链已复制！");
        } else {
          alert("获取直链失败: " + (d.error || JSON.stringify(d)));
        }
      } catch (e) {
        alert("错误: " + e.message);
      }
    }

    // ================= DOWNLOAD CONFIRMATION & BATCH / NAS FOLDER SELECTION =================
    function openDownloadModalWithItems(items) {
      pendingDownloadItems = items;
      const isBatchOrFolder = (items.length > 1) || (items.length === 1 && items[0].resType === 2);

      document.getElementById("dlConfirmPathInput").value = defaultDownloadDir;
      document.getElementById("dlConfirmMaxConcurrentInput").value = maxConcurrentDownloads;
      const conflictBox = document.getElementById("dlConfirmConflictBox");
      const concurrencyBox = document.getElementById("dlConfirmConcurrencyBox");
      const batchListEl = document.getElementById("dlConfirmBatchList");
      const titleEl = document.getElementById("dlConfirmModalTitle");
      const iconEl = document.getElementById("dlConfirmItemIcon");
      const nameEl = document.getElementById("dlConfirmFileName");
      const submitBtn = document.getElementById("dlConfirmSubmitBtn");

      if (isBatchOrFolder) {
        conflictBox.style.display = "flex";
        concurrencyBox.style.gridColumn = "span 1";
      } else {
        conflictBox.style.display = "none";
        concurrencyBox.style.gridColumn = "span 2";
      }

      if (items.length === 1) {
        const it = items[0];
        pendingDownloadFileId = it.fileId;
        pendingDownloadFileName = it.fileName;
        if (it.resType === 2) {
          titleEl.innerText = "下载整个文件夹到 NAS";
          iconEl.innerText = "📁";
          nameEl.innerText = `${it.fileName} （文件夹 · 将自动递归下载内部所有文件）`;
          submitBtn.innerText = "立即下载文件夹到 NAS";
        } else {
          titleEl.innerText = "下载文件到 NAS";
          iconEl.innerText = getFileIcon(it.fileName);
          nameEl.innerText = it.fileSize ? `${it.fileName} (${formatBytes(it.fileSize)})` : it.fileName;
          submitBtn.innerText = "立即下载到 NAS";
        }
        batchListEl.style.display = "none";
        batchListEl.innerHTML = "";
      } else {
        const fileCount = items.filter(i => i.resType !== 2).length;
        const dirCount = items.filter(i => i.resType === 2).length;
        const totalBytes = items.reduce((acc, i) => acc + (i.resType !== 2 ? (Number(i.fileSize) || 0) : 0), 0);
        let summaryParts = [];
        if (fileCount > 0) summaryParts.push(`${fileCount} 个文件`);
        if (dirCount > 0) summaryParts.push(`${dirCount} 个文件夹`);
        const sizeSuffix = totalBytes > 0 ? ` · 约 ${formatBytes(totalBytes)}` : "";

        titleEl.innerText = `批量下载到 NAS (共 ${items.length} 项)`;
        iconEl.innerText = "📦";
        nameEl.innerText = `已选择 ${summaryParts.join("、")}${sizeSuffix}`;
        submitBtn.innerText = `⚡ 立即批量下载 (${items.length} 项)`;

        batchListEl.style.display = "flex";
        batchListEl.innerHTML = items.map((it, idx) => {
          const ic = it.resType === 2 ? "📁" : getFileIcon(it.fileName);
          const sz = it.resType === 2 ? "文件夹" : formatBytes(it.fileSize);
          return `<div style="display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 2px 4px;">
            <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;">${idx + 1}. ${ic} ${escapeHtml(it.fileName)}</span>
            <span style="color: var(--text-muted); flex-shrink: 0; font-family: monospace;">${sz}</span>
          </div>`;
        }).join("");
      }

      openModal("downloadConfirmModal");
    }

    function promptStartDownload(fileId, fileName, resType = 1, fileSize = 0) {
      openDownloadModalWithItems([{
        fileId: fileId,
        fileName: fileName,
        resType: resType || 1,
        fileSize: fileSize || 0
      }]);
    }

    function promptBatchDownload() {
      if (selectedFileIds.size === 0) {
        showToast("💡 请先在列表中勾选要下载的文件或文件夹（可点击表头复选框一键全选）");
        return;
      }
      const items = currentRawFiles
        .filter(f => selectedFileIds.has(f.fileId))
        .map(f => ({
          fileId: f.fileId,
          fileName: f.fileName,
          resType: f.resType || 1,
          fileSize: f.fileSize || 0
        }));
      if (items.length === 0) {
        showToast("未找到已勾选的文件，请重新选择");
        return;
      }
      openDownloadModalWithItems(items);
    }

    function onDlConfirmFolderPicked(selectedPath) {
      document.getElementById("dlConfirmPathInput").value = selectedPath;
    }

    let duplicateContext = null;

    async function confirmStartDownload() {
      const targetDir = document.getElementById("dlConfirmPathInput").value.trim() || defaultDownloadDir;
      const setAsDefault = document.getElementById("dlConfirmSetDefaultCb").checked;
      const newConcurrent = parseInt(document.getElementById("dlConfirmMaxConcurrentInput").value, 10) || maxConcurrentDownloads;
      const clampedConcurrent = Math.max(1, Math.min(16, newConcurrent));

      if (setAsDefault || clampedConcurrent !== maxConcurrentDownloads) {
        const payload = { max_concurrent_downloads: clampedConcurrent };
        if (setAsDefault) {
          defaultDownloadDir = targetDir;
          payload.download_dir = targetDir;
        }
        maxConcurrentDownloads = clampedConcurrent;
        syncMaxConcurrentInputs(clampedConcurrent);
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
      }

      // Single regular file: preserve interactive duplicate file modal
      if (pendingDownloadItems.length === 1 && pendingDownloadItems[0].resType !== 2) {
        const single = pendingDownloadItems[0];
        try {
          const checkRes = await fetch(`/api/fs/check-file?targetDir=${encodeURIComponent(targetDir)}&fileName=${encodeURIComponent(single.fileName)}`);
          const checkData = await checkRes.json();
          if (checkData.exists) {
            duplicateContext = {
              fileId: single.fileId,
              originalFileName: single.fileName,
              suggestedFileName: checkData.suggestedName,
              targetDir: targetDir,
              fileSize: single.fileSize || 0
            };
            document.getElementById("duplicateFileNameDisplay").innerText = single.fileName;
            document.getElementById("duplicateSuggestedNameBadge").innerText = `新文件名: ${checkData.suggestedName}`;
            closeModal("downloadConfirmModal");
            openModal("duplicateFileModal");
            return;
          }
        } catch (e) {
          console.warn("Check file existence failed:", e);
        }

        closeModal("downloadConfirmModal");
        executeDownload(single.fileId, single.fileName, targetDir, single.fileSize || 0);
        return;
      }

      // Batch or folder download
      const conflictPolicy = document.getElementById("dlConfirmConflictSelect").value || "skip";
      closeModal("downloadConfirmModal");
      executeBatchDownload(pendingDownloadItems, targetDir, conflictPolicy);
    }

    function resolveDuplicateAndDownload(action) {
      if (!duplicateContext) return;
      const ctx = duplicateContext;
      closeModal("duplicateFileModal");
      if (action === "rename") {
        executeDownload(ctx.fileId, ctx.suggestedFileName, ctx.targetDir, ctx.fileSize || 0);
      } else if (action === "overwrite") {
        executeDownload(ctx.fileId, ctx.originalFileName, ctx.targetDir, ctx.fileSize || 0);
      }
    }

    async function executeDownload(fileId, fileName, targetDir, fileSize = 0) {
      showToast("正在加入下载队列...");
      try {
        const res = await fetch("/api/download-task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            fileId: fileId,
            fileName: fileName,
            targetDir: targetDir,
            fileSize: fileSize
          })
        });
        const d = await res.json();
        if (d.taskId) {
          showToast("已成功添加到本地 NAS 下载队列");
          switchView("transfers");
        }
      } catch (e) {
        alert("启动下载失败: " + e.message);
      }
    }

    async function executeBatchDownload(items, targetDir, conflictPolicy) {
      showToast("⏳ 正在提交批量下载队列...");
      try {
        const res = await fetch("/api/batch-download-task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            items: items,
            targetDir: targetDir,
            conflictPolicy: conflictPolicy
          })
        });
        const d = await res.json();
        if (d.success) {
          selectedFileIds.clear();
          renderFileList();
          const skipMsg = d.skippedCount > 0 ? `（已跳过 ${d.skippedCount} 个同名文件）` : "";
          const expandMsg = d.expandingFolders ? "，深层子目录正在后台持续加入队列" : "";
          showToast(`🚀 已加入 ${d.queuedCount} 个下载任务到队列${skipMsg}${expandMsg}！`);
          switchView("transfers");
        } else {
          alert("批量下载提交失败: " + (d.error || JSON.stringify(d)));
        }
      } catch (e) {
        alert("批量下载失败: " + e.message);
      }
    }

    // ================= NAS FOLDER PICKER =================
    function openFolderPicker(callback, initialPath) {
      folderPickerCallback = callback;
      const target = initialPath || defaultDownloadDir || "/vol1/1000";
      folderPickerSelectedPath = target;
      folderPickerPath = "";
      openModal("folderPickerModal");
      // Load root "" so all NAS shared folders across all volumes are immediately visible
      loadFolderPickerList("");
    }

    function updateFolderPickerBreadcrumbs(path) {
      const el = document.getElementById("folderPickerBreadcrumbs");
      if (!el) return;
      if (!path) {
        el.innerHTML = `<span class="crumb-item current" style="cursor: pointer;" onclick="loadFolderPickerList('')">🏠 全部共享文件夹与存储卷</span>`;
        return;
      }
      let html = `<span class="crumb-link" style="color: var(--primary); cursor: pointer;" onclick="loadFolderPickerList('')">🏠 根目录</span>`;
      const parts = path.split("/").filter(Boolean);
      let accumulated = "";
      for (let i = 0; i < parts.length; i++) {
        accumulated += "/" + parts[i];
        const isLast = (i === parts.length - 1);
        const p = accumulated;
        if (isLast) {
          html += ` <span style="color: var(--text-dim); margin: 0 4px;">/</span> <span class="crumb-item current" style="font-weight: 600; color: var(--text);">${escapeHtml(parts[i])}</span>`;
        } else {
          html += ` <span style="color: var(--text-dim); margin: 0 4px;">/</span> <span class="crumb-link" style="color: var(--primary); cursor: pointer;" onclick="loadFolderPickerList('${escapeHtml(p)}')">${escapeHtml(parts[i])}</span>`;
        }
      }
      el.innerHTML = html;
    }

    function selectFolderPickerItem(path) {
      folderPickerSelectedPath = path;
      document.getElementById("folderPickerCurrentPath").value = path;
      const footerEl = document.getElementById("folderPickerFooterPath");
      if (footerEl) footerEl.innerText = path;
      document.querySelectorAll(".fs-folder-item").forEach(el => {
        if (el.dataset.path === path) {
          el.classList.add("selected");
        } else {
          el.classList.remove("selected");
        }
      });
    }

    async function loadFolderPickerList(path) {
      folderPickerPath = path;
      document.getElementById("folderPickerCurrentPath").value = path || folderPickerSelectedPath || "";
      const footerEl = document.getElementById("folderPickerFooterPath");
      if (footerEl) footerEl.innerText = folderPickerSelectedPath || path || "--";
      updateFolderPickerBreadcrumbs(path);

      const container = document.getElementById("folderPickerList");
      container.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 24px;">加载中...</div>`;

      try {
        const res = await fetch(`/api/fs/browse?path=${encodeURIComponent(path)}`);
        const json = await res.json();
        const folders = json.folders || [];
        folderPickerPath = json.currentPath !== undefined ? json.currentPath : path;

        if (folders.length === 0) {
          const folderName = path.split("/").filter(Boolean).pop() || "此目录";
          folderPickerSelectedPath = path;
          document.getElementById("folderPickerCurrentPath").value = path;
          if (footerEl) footerEl.innerText = path;
          container.innerHTML = `
            <div style="text-align: center; padding: 28px 16px; color: var(--text-muted);">
              <div style="font-size: 32px; margin-bottom: 6px;">📂</div>
              <div style="font-size: 14.5px; font-weight: 600; color: var(--text);">已就绪保存目录：<span style="color: var(--primary);">${escapeHtml(folderName)}</span></div>
              <div style="font-size: 12.5px; margin-top: 6px; color: var(--text-dim); line-height: 1.6;">
                当前文件夹下没有更深层的子文件夹。<br>您可直接点击下方【确定选择该目录】保存到此处，或点击右上角新建子文件夹。
              </div>
              <div style="margin-top: 14px; display: flex; gap: 8px; justify-content: center;">
                <button class="btn-primary btn-sm" style="padding: 6px 16px;" onclick="confirmFolderPicker()">✅ 确定选择该目录</button>
                <button class="btn-secondary btn-sm" style="padding: 6px 16px;" onclick="folderPickerGoUp()">⬆️ 返回上一级</button>
              </div>
            </div>
          `;
          return;
        }

        container.innerHTML = "";
        folders.forEach(f => {
          const div = document.createElement("div");
          const isSelected = (f.path === folderPickerSelectedPath);
          let itemIcon = f.is_volume ? "💽" : "📁";
          let cleanName = (f.name || "").trim();
          if (cleanName.startsWith("💽")) {
            itemIcon = "💽";
            cleanName = cleanName.replace(/^💽\\s*/, "");
          } else if (cleanName.startsWith("🏠")) {
            itemIcon = "🏠";
            cleanName = cleanName.replace(/^🏠\\s*/, "");
          } else if (cleanName.startsWith("📁")) {
            itemIcon = "📁";
            cleanName = cleanName.replace(/^📁\\s*/, "");
          }
          div.className = "fs-folder-item" + (isSelected ? " selected" : "");
          div.dataset.path = f.path;
          div.innerHTML = `
            <div class="fs-folder-left" style="flex: 1; min-width: 0;" onclick="selectFolderPickerItem('${escapeHtml(f.path)}')">
              <span style="font-size: 16px; flex-shrink: 0;">${itemIcon}</span>
              <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: ${f.is_share ? '600' : '400'};" title="${escapeHtml(cleanName)}">${escapeHtml(cleanName)}</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center; flex-shrink: 0;">
              <button class="btn-primary btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="event.stopPropagation(); selectFolderPickerItem('${escapeHtml(f.path)}'); confirmFolderPicker();">选择</button>
              <button class="btn-secondary btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="event.stopPropagation(); loadFolderPickerList('${escapeHtml(f.path)}')">进入 ➔</button>
            </div>
          `;
          div.ondblclick = () => {
            loadFolderPickerList(f.path);
          };
          container.appendChild(div);
        });
      } catch (e) {
        container.innerHTML = `<div style="color: #f87171; padding: 20px;">读取目录失败: ${e.message}</div>`;
      }
    }

    function folderPickerGoUp() {
      if (!folderPickerPath || folderPickerPath === "" || folderPickerPath === "/") {
        return;
      }
      const parts = folderPickerPath.split("/").filter(Boolean);
      if (parts.length <= 1) {
        loadFolderPickerList("");
      } else {
        parts.pop();
        const parent = "/" + parts.join("/");
        loadFolderPickerList(parent);
      }
    }

    async function folderPickerMkdir() {
      const targetDir = folderPickerPath || folderPickerSelectedPath || defaultDownloadDir || "";
      const name = prompt(`在 ${targetDir || '当前位置'} 下新建文件夹名称：`);
      if (!name || !name.trim()) return;
      const newPath = (targetDir.endsWith("/") ? targetDir : targetDir + "/") + name.trim();
      try {
        const res = await fetch("/api/fs/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: newPath })
        });
        const d = await res.json();
        if (d.success) {
          showToast("新建文件夹成功");
          loadFolderPickerList(targetDir);
        } else {
          alert("新建失败: " + (d.error || JSON.stringify(d)));
        }
      } catch (e) {
        alert("网络错误: " + e.message);
      }
    }

    function confirmFolderPicker() {
      const chosen = folderPickerSelectedPath || folderPickerPath || defaultDownloadDir || "";
      if (folderPickerCallback) {
        folderPickerCallback(chosen);
      }
      closeModal("folderPickerModal");
    }

    // ================= VIEW: TRANSFERS (Issue 3 - Speed & Progress) =================
    function switchTransferTab(tab) {
      currentTransferTab = tab;
      if (tab === 'active') {
        document.getElementById("tabDlActiveBtn").classList.add("active");
        document.getElementById("tabDlDoneBtn").classList.remove("active");
      } else {
        document.getElementById("tabDlActiveBtn").classList.remove("active");
        document.getElementById("tabDlDoneBtn").classList.add("active");
      }
      renderTransfers();
    }

    let transferPollingTimer = null;
    let localDownloadsMap = {};

    async function loadTransfers() {
      try {
        const res = await fetch("/api/transfers");
        localDownloadsMap = await res.json();
        renderTransfers();
      } catch (e) {
        console.error("Failed to load transfers:", e);
      }
    }

    function renderTransfers() {
      const c = document.getElementById("transfersListContainer");
      const list = Object.values(localDownloadsMap);

      const activeList = list.filter(t => t.status === "downloading" || t.status === "pending" || t.status === "getting_url");
      const doneList = list.filter(t => t.status === "completed" || t.status === "failed" || t.status === "cancelled");

      // Sort activeList: downloading/getting_url first, then pending in FIFO order (seq)
      activeList.sort((a, b) => {
        const aRunning = (a.status === "downloading" || a.status === "getting_url") ? 0 : 1;
        const bRunning = (b.status === "downloading" || b.status === "getting_url") ? 0 : 1;
        if (aRunning !== bRunning) return aRunning - bRunning;
        return (a.seq || a.startTime || 0) - (b.seq || b.startTime || 0);
      });

      // Sort doneList: newest first
      doneList.sort((a, b) => (b.seq || b.startTime || 0) - (a.seq || a.startTime || 0));

      document.getElementById("countActiveDl").innerText = activeList.length;
      document.getElementById("countDoneDl").innerText = doneList.length;

      const cancelAllBtn = document.getElementById("cancelAllDlBtn");
      if (cancelAllBtn) {
        cancelAllBtn.style.display = (activeList.length > 0) ? "inline-flex" : "none";
      }

      const badge = document.getElementById("activeDlBadge");
      if (badge) {
        if (activeList.length > 0) {
          badge.style.display = "inline-flex";
          badge.innerText = activeList.length;
        } else {
          badge.style.display = "none";
        }
      }

      const displayList = (currentTransferTab === 'active') ? activeList : doneList;

      if (displayList.length === 0) {
        c.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 50px;">暂无${currentTransferTab === 'active' ? '正在进行的下载任务' : '已完成任务'}</div>`;
        return;
      }

      let pendingIdx = 0;
      c.innerHTML = "";
      displayList.forEach(t => {
        const isDone = (t.status === "completed");
        const isFailed = (t.status === "failed");
        const isCancelled = (t.status === "cancelled");
        const isRunning = !isDone && !isFailed && !isCancelled;

        let badgeClass = "status-active";
        let statusText = "下载中";
        if (isDone) { badgeClass = "status-success"; statusText = "已完成"; }
        else if (isFailed) { badgeClass = "status-failed"; statusText = "下载失败"; }
        else if (isCancelled) { badgeClass = "status-failed"; statusText = "已取消"; }
        else if (t.status === "getting_url") { statusText = "获取直链中"; }
        else if (t.status === "pending") {
          pendingIdx++;
          badgeClass = "status-warning";
          statusText = `排队中 (#${pendingIdx})`;
        }

        const div = document.createElement("div");
        div.className = "task-card";

        div.innerHTML = `
          <div class="task-card-header">
            <div class="task-title" title="${escapeHtml(t.fileName)}">
              <span style="margin-right: 6px;">${getFileIcon(t.fileName)}</span>
              <span>${escapeHtml(t.fileName)}</span>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <span class="status-badge ${badgeClass}">${statusText}</span>
              ${(isFailed || isCancelled) ? `<button class="btn-secondary btn-sm" onclick="retryTransfer('${t.taskId}')">🔄 重试</button>` : ''}
              ${isRunning ? `<button class="btn-secondary btn-sm" onclick="cancelTransfer('${t.taskId}')">⏹️ 取消</button>` : ''}
              <button class="btn-outline btn-sm" onclick="deleteTransfer('${t.taskId}')">🗑️ 删除</button>
            </div>
          </div>

          <div class="progress-bar-bg">
            <div class="progress-bar-fill ${isDone ? 'success' : ''}" style="width: ${t.percent || 0}%"></div>
          </div>

          <div class="task-details">
            <div class="task-details-left">
              ${isRunning ? `<span>⚡ 速度: <strong style="color: #60a5fa;">${t.speed || '0 KB/s'}</strong></span>` : ''}
              <span>进度: ${t.downloaded || '--'} / ${t.total || '--'} (${t.percent || 0}%)</span>
              ${isRunning && t.eta && t.eta !== '--' ? `<span>剩余: ${t.eta}</span>` : ''}
              ${isFailed && t.error ? `<span style="color: #f87171;">原因: ${escapeHtml(t.error)}</span>` : ''}
            </div>
            <div>
              <span style="color: var(--text-dim);">保存路径: <code>${escapeHtml(t.path || t.targetDir)}</code></span>
            </div>
          </div>
        `;
        c.appendChild(div);
      });
    }

    async function cancelTransfer(taskId) {
      await fetch("/api/transfers/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taskId })
      });
      showToast("任务已取消");
      loadTransfers();
    }

    async function retryTransfer(taskId) {
      await fetch("/api/transfers/retry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taskId })
      });
      showToast("已重新加入下载队列");
      loadTransfers();
    }

    async function cancelAllTransfers() {
      const res = await fetch("/api/transfers/cancel-all", { method: "POST" });
      const d = await res.json();
      showToast(`已取消全部 ${d.cancelled || 0} 个任务`);
      loadTransfers();
    }

    function syncMaxConcurrentInputs(val) {
      maxConcurrentDownloads = val;
      ["transferMaxConcurrentInput", "settingMaxConcurrentInput", "dlConfirmMaxConcurrentInput"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = val;
      });
    }

    async function updateMaxConcurrentSetting(val) {
      let n = parseInt(val, 10);
      if (isNaN(n)) n = 3;
      if (n < 1) n = 1;
      if (n > 16) n = 16;
      syncMaxConcurrentInputs(n);
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ max_concurrent_downloads: n })
        });
        showToast(`已将同时下载任务上限设为 ${n} 个`);
        loadTransfers();
      } catch (e) {
        alert("保存并发设置失败: " + e.message);
      }
    }

    let pendingDeleteTransferId = null;

    function deleteTransfer(taskId) {
      const t = localDownloadsMap[taskId];
      pendingDeleteTransferId = taskId;
      document.getElementById("deleteTransferModalTitle").innerText = "删除下载任务";
      document.getElementById("deleteTransferPromptLabel").innerText = "确定要删除以下下载任务吗？";
      document.getElementById("deleteTransferTaskName").innerText = t ? t.fileName : taskId;
      const pathWrap = document.getElementById("deleteTransferPathWrap");
      const pathEl = document.getElementById("deleteTransferFilePath");
      if (t && (t.path || t.targetDir)) {
        pathWrap.style.display = "block";
        pathEl.innerText = t.path || t.targetDir;
      } else {
        pathWrap.style.display = "none";
      }
      // Default check: if task is cancelled/failed/running (incomplete) or user wants to clean up, uncheck by default for completed or check? Let's keep unchecked or remember last choice? Wait: user asked "应该在删除时增加一个询问是否删除nas上的文件选项", let's default to checked if incomplete, or false/true clearly selectable! Actually let's check if incomplete (status !== 'completed') so partial files are cleaned, or false when completed so user can check it. Wait—let's check what user said: "删除下载任务后，在nas的硬盘上文件还是存在，没有删除掉，应该在删除时增加一个询问是否删除nas上的文件选项". Let's default checkbox to false (or true if status !== 'completed') and make the checkbox super clear!
      document.getElementById("deleteTransferFileCheckbox").checked = (t && t.status !== "completed");
      openModal("deleteTransferModal");
    }

    function clearCompletedDownloads() {
      const doneList = Object.values(localDownloadsMap).filter(t => t.status === "completed" || t.status === "failed" || t.status === "cancelled");
      if (doneList.length === 0) {
        showToast("当前没有可清空的已完成/历史记录");
        return;
      }
      pendingDeleteTransferId = "__ALL_DONE__";
      document.getElementById("deleteTransferModalTitle").innerText = "清空已完成与历史记录";
      document.getElementById("deleteTransferPromptLabel").innerText = "确定要清空全部已完成、已取消及失败的下载任务吗？";
      document.getElementById("deleteTransferTaskName").innerText = `共 ${doneList.length} 条下载任务记录`;
      document.getElementById("deleteTransferPathWrap").style.display = "none";
      document.getElementById("deleteTransferFileCheckbox").checked = false;
      openModal("deleteTransferModal");
    }

    async function confirmDeleteTransfer() {
      if (!pendingDeleteTransferId) return;
      const deleteFile = document.getElementById("deleteTransferFileCheckbox").checked;
      const btn = document.getElementById("confirmDeleteTransferBtn");
      btn.disabled = true;
      btn.innerText = "正在删除...";
      try {
        if (pendingDeleteTransferId === "__ALL_DONE__") {
          await fetch("/api/transfers/clear-completed", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ deleteFile })
          });
          showToast("已清空历史记录" + (deleteFile ? "并删除对应 NAS 本地文件" : ""));
        } else {
          await fetch("/api/transfers/delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ taskId: pendingDeleteTransferId, deleteFile })
          });
          showToast("已删除下载任务" + (deleteFile ? "及 NAS 本地文件" : ""));
        }
        closeModal("deleteTransferModal");
        loadTransfers();
      } catch (e) {
        alert("删除失败: " + e.message);
      } finally {
        btn.disabled = false;
        btn.innerText = "确定删除";
      }
    }

    // ================= VIEW: OFFLINE TASKS (Issue 4 - Status Logic) =================
    function filterOfflineTasks(filter) {
      offlineFilter = filter;
      document.querySelectorAll("#view-offline .subtab-btn").forEach(b => b.classList.remove("active"));
      if (filter === 'all') document.getElementById("tabOfflineAll").classList.add("active");
      else if (filter === 'running') document.getElementById("tabOfflineRunning").classList.add("active");
      else if (filter === 'done') document.getElementById("tabOfflineDone").classList.add("active");
      else if (filter === 'failed') document.getElementById("tabOfflineFailed").classList.add("active");
      renderOfflineTasks();
    }

    async function loadCloudTasks() {
      try {
        const res = await fetch("/api/cloud-add/tasks");
        const json = await res.json();
        allOfflineTasks = json.list || [];

        // Count categories accurately (status 2 = completed)
        let doneCount = 0;
        let failCount = 0;
        let runCount = 0;

        allOfflineTasks.forEach(t => {
          if (t.status === 2 || !!t.fileId) doneCount++;
          else if (t.status === 3 || !!t.errCode) failCount++;
          else runCount++;
        });

        document.getElementById("countOfflineAll").innerText = allOfflineTasks.length;
        document.getElementById("countOfflineRunning").innerText = runCount;
        document.getElementById("countOfflineDone").innerText = doneCount;
        document.getElementById("countOfflineFailed").innerText = failCount;

        const badge = document.getElementById("cloudTaskBadge");
        const mBadge = document.getElementById("mobileCloudTaskBadge");
        if (runCount > 0) {
          badge.style.display = "inline-block";
          badge.innerText = runCount;
          if (mBadge) { mBadge.style.display = "inline-block"; mBadge.innerText = runCount; }
        } else {
          badge.style.display = "none";
          if (mBadge) mBadge.style.display = "none";
        }

        renderOfflineTasks();
      } catch (e) {
        console.error("Failed to load cloud tasks:", e);
      }
    }

    function renderOfflineTasks() {
      const container = document.getElementById("offlineListContainer");
      let list = allOfflineTasks;

      if (offlineFilter === 'running') {
        list = allOfflineTasks.filter(t => t.status !== 2 && !t.fileId && t.status !== 3 && !t.errCode);
      } else if (offlineFilter === 'done') {
        list = allOfflineTasks.filter(t => t.status === 2 || !!t.fileId);
      } else if (offlineFilter === 'failed') {
        list = allOfflineTasks.filter(t => t.status === 3 || !!t.errCode);
      }

      if (list.length === 0) {
        container.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 50px;">暂无该状态下的离线任务</div>`;
        return;
      }

      container.innerHTML = "";
      list.forEach(t => {
        // Guangyapan status 2 = finished, 4 = paused, 3 = failed, 1 = downloading, 0 = waiting
        const isDone = (t.status === 2 || !!t.fileId);
        const isFailed = (t.status === 3 || !!t.errCode);
        const isPaused = (t.status === 4);
        const isRunning = !isDone && !isFailed && !isPaused;

        let badgeClass = "status-active";
        let badgeText = `下载中 ${t.progress || 0}%`;
        if (isDone) {
          badgeClass = "status-success";
          badgeText = "已完成";
        } else if (isPaused) {
          badgeClass = "status-warning";
          badgeText = `已暂停 ${t.progress || 0}%`;
        } else if (isFailed) {
          badgeClass = "status-failed";
          badgeText = "任务失败";
        }

        const div = document.createElement("div");
        div.className = "task-card";

        div.innerHTML = `
          <div class="task-card-header">
            <div class="task-title" title="${escapeHtml(t.fileName)}">
              <span style="margin-right: 6px;">⚡</span>
              <span>${escapeHtml(t.fileName)}</span>
            </div>
            <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
              <span class="status-badge ${badgeClass}">${badgeText}</span>
              ${isRunning ? `<button class="btn-secondary btn-sm" onclick="pauseOfflineTask('${t.taskId}')">⏸️ 暂停</button>` : ''}
              ${(isFailed || isPaused) ? `<button class="btn-secondary btn-sm" onclick="retryOfflineTask('${t.taskId}')">▶️ 继续</button>` : ''}
              ${isDone && t.fileId ? `<button class="btn-primary btn-sm" onclick="promptStartDownload('${t.fileId}', '${escapeHtml(t.fileName)}')">⬇️<span class="desktop-text"> 下载到NAS</span><span class="mobile-text"> 下载</span></button>` : ''}
              <button class="btn-outline btn-sm" onclick="promptDeleteOfflineTask('${t.taskId}', '${escapeHtml(t.fileName)}')">🗑️ 删除</button>
            </div>
          </div>

          <div class="progress-bar-bg">
            <div class="progress-bar-fill ${isDone ? 'success' : ''}" style="width: ${isDone ? 100 : (t.progress || 0)}%"></div>
          </div>

          <div class="task-details">
            <div class="task-details-left">
              <span>文件大小: ${formatBytes(t.totalSize)}</span>
              <span>创建时间: ${formatDate(t.createTime)}</span>
              ${isFailed && t.errMsg ? `<span style="color: #f87171;">错误: ${escapeHtml(t.errMsg)}</span>` : ''}
            </div>
            <div>
              <span style="color: var(--text-dim); font-size: 11.5px;">任务ID: ${t.taskId}</span>
            </div>
          </div>
        `;
        container.appendChild(div);
      });
    }

    async function pauseOfflineTask(taskId) {
      try {
        const res = await fetch("/api/cloud-add/pause", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ taskId: taskId })
        });
        const d = await res.json();
        if (d.code === 128 || (d.error && d.error.includes("128"))) {
          alert("光鸭网盘提示：该任务当前状态不支持客户端主动暂停");
        } else if (d.error) {
          alert("暂停任务失败: " + d.error);
        } else {
          showToast("已发送暂停请求");
          loadCloudTasks();
        }
      } catch (e) {
        alert("网络异常: " + e.message);
      }
    }

    async function retryOfflineTask(taskId) {
      try {
        const res = await fetch("/api/cloud-add/retry", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ taskId: taskId })
        });
        const d = await res.json();
        if (d.error) {
          alert("继续/重试任务失败: " + d.error);
        } else {
          showToast("已恢复下载任务");
          loadCloudTasks();
        }
      } catch (e) {
        alert("网络异常: " + e.message);
      }
    }

    let pendingDeleteCloudTaskId = null;

    function promptDeleteOfflineTask(taskId, fileName) {
      pendingDeleteCloudTaskId = taskId;
      document.getElementById("deleteCloudTaskName").innerText = fileName || taskId;
      document.getElementById("deleteCloudFileCheckbox").checked = false;
      openModal("deleteCloudTaskModal");
    }

    async function confirmDeleteCloudTask() {
      if (!pendingDeleteCloudTaskId) return;
      const deleteFile = document.getElementById("deleteCloudFileCheckbox").checked;
      const btn = document.getElementById("confirmDeleteCloudTaskBtn");
      btn.disabled = true;
      btn.innerText = "正在删除...";
      try {
        const res = await fetch("/api/cloud-add/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ taskId: pendingDeleteCloudTaskId, deleteFile: deleteFile })
        });
        const d = await res.json();
        if (d.error) {
          alert("删除失败: " + d.error);
        } else {
          showToast("已成功删除离线任务" + (deleteFile ? "及网盘文件" : ""));
          closeModal("deleteCloudTaskModal");
          loadCloudTasks();
        }
      } catch (e) {
        alert("网络异常: " + e.message);
      } finally {
        btn.disabled = false;
        btn.innerText = "确定删除";
      }
    }

    async function deleteOfflineTask(taskId) {
      promptDeleteOfflineTask(taskId, taskId);
    }

    // ================= VIEW: SETTINGS & ABOUT =================
    async function loadSettings() {
      const host = window.location.hostname || "NAS_IP";
      const directUrl = `http://${host}:8998`;
      const linkEl = document.getElementById("aboutWebAccessLink");
      const codeEl = document.getElementById("aboutWebAccessCode");
      if (linkEl) { linkEl.href = directUrl; linkEl.innerText = directUrl; }
      if (codeEl) { codeEl.innerText = directUrl; }
      try {
        const res = await fetch("/api/settings");
        const d = await res.json();
        if (d.download_dir) {
          defaultDownloadDir = d.download_dir;
          document.getElementById("settingDownloadDirInput").value = defaultDownloadDir;
        }
        if (d.max_concurrent_downloads) {
          syncMaxConcurrentInputs(d.max_concurrent_downloads);
        }
        if (d.file_page_size !== undefined && d.file_page_size !== null) {
          syncFilePageSizeInputs(d.file_page_size);
          if (currentRawFiles.length > 0) renderFileList();
        }
        if (d.version) {
          const vEl = document.getElementById("aboutCurrentVersion");
          if (vEl) vEl.innerText = "v" + d.version;
        }
      } catch (e) {
        console.error(e);
      }
      // Silently check for updates in background
      checkForUpdates(false);
    }

    let isCheckingUpdate = false;
    async function checkForUpdates(manual = false) {
      if (isCheckingUpdate) return;
      isCheckingUpdate = true;
      const btn = document.getElementById("checkUpdateBtn");
      const badge = document.getElementById("updateStatusBadge");
      const desc = document.getElementById("updateStatusDesc");
      const dlBtn = document.getElementById("downloadUpdateBtn");
      const navBadge = document.getElementById("settingsUpdateBadge");

      if (btn && manual) {
        btn.innerText = "⏳ 检查中...";
        btn.disabled = true;
      }

      try {
        const res = await fetch(`/api/version/check${manual ? '?force=1' : ''}`);
        const data = await res.json();

        if (data.has_update) {
          if (badge) {
            badge.className = "status-badge status-warning";
            badge.style.background = "rgba(255, 122, 0, 0.2)";
            badge.style.color = "var(--primary)";
            badge.innerText = `发现新版本 v${data.latest_version}`;
          }
          if (desc) {
            desc.innerHTML = `<span style="color: var(--primary); font-weight: 600;">🎉 发现新版本 v${escapeHtml(data.latest_version)} 发布！</span><br><span style="font-size: 12px; color: var(--text-muted);">${escapeHtml(data.release_title || '请前往 GitHub Releases 下载并安装最新 .fpk 套件。')}</span>`;
          }
          if (dlBtn) {
            dlBtn.href = data.release_url || "https://github.com/lawzhougc/guangyapan-fnos/releases";
            dlBtn.style.display = "inline-flex";
          }
          if (navBadge) {
            navBadge.style.display = "inline-block";
          }
          if (manual) {
            showToast(`发现新版本 v${data.latest_version}！`);
          }
        } else {
          if (badge) {
            badge.className = "status-badge status-success";
            badge.style.background = "rgba(34, 197, 94, 0.15)";
            badge.style.color = "#22c55e";
            badge.innerText = "已是最新版本";
          }
          if (desc) {
            if (data.error) {
              desc.innerHTML = `当前版本 <b>v${escapeHtml(data.current_version)}</b>。<br><span style="font-size: 12px; color: var(--text-dim);">${escapeHtml(data.error_hint || '无法直接连接 GitHub API，您可随时访问 GitHub Releases 页面查看最新发布包。')}</span>`;
            } else {
              desc.innerHTML = `当前版本 <b>v${escapeHtml(data.current_version)}</b> 为最新版本，系统运行稳定。`;
            }
          }
          if (dlBtn) {
            dlBtn.style.display = "none";
          }
          if (navBadge) {
            navBadge.style.display = "none";
          }
          if (manual) {
            showToast("当前已是最新版本");
          }
        }
      } catch (e) {
        if (badge) {
          badge.className = "status-badge";
          badge.innerText = "检测受限";
        }
        if (desc) {
          desc.innerHTML = `网络异常或 GitHub 连接受限，可直接访问上方 GitHub 主页查看最新版本。`;
        }
        if (manual) {
          showToast("检查更新超时，请直接访问 GitHub 页面");
        }
      } finally {
        isCheckingUpdate = false;
        if (btn) {
          btn.innerText = "🔍 检查更新";
          btn.disabled = false;
        }
      }
    }

    function onSettingFolderPicked(selectedPath) {
      document.getElementById("settingDownloadDirInput").value = selectedPath;
    }

    async function saveDownloadDirSetting() {
      const path = document.getElementById("settingDownloadDirInput").value.trim();
      const mcEl = document.getElementById("settingMaxConcurrentInput");
      const psEl = document.getElementById("settingFilePageSizeInput");
      let mcVal = mcEl ? parseInt(mcEl.value, 10) : maxConcurrentDownloads;
      if (isNaN(mcVal) || mcVal < 1) mcVal = 1;
      if (mcVal > 16) mcVal = 16;
      let psVal = psEl ? parseInt(psEl.value, 10) : filePageSize;
      if (isNaN(psVal) || psVal < 0) psVal = 20;
      if (psVal > 5000) psVal = 5000;
      if (!path) return;
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            download_dir: path,
            max_concurrent_downloads: mcVal,
            file_page_size: psVal
          })
        });
        defaultDownloadDir = path;
        syncMaxConcurrentInputs(mcVal);
        syncFilePageSizeInputs(psVal);
        renderFileList();
        showToast("NAS 下载目录、并发与分页设置已保存");
      } catch (e) {
        alert("保存失败: " + e.message);
      }
    }

    // ================= CLOUD ADD (MAGNET / TORRENT) & DESTINATION PICKER =================
    let resolvedCloudData = null;
    let cloudAddTargetParentId = "";
    let cloudAddTargetFolderPath = "全部文件";
    let cloudUserQuotaFreeStr = "--";

    let cloudPickerCurrentParentId = "";
    let cloudPickerBreadcrumbs = [{ id: "", name: "全部文件" }];
    let cloudPickerSelectedId = "";
    let cloudPickerSelectedPath = "全部文件";

    async function initCloudAddDestination() {
      // 1. Fetch quota
      try {
        const uRes = await fetch("/api/user");
        const uData = await uRes.json();
        if (uData.assets) {
          const free = (uData.assets.totalSpaceSize || 0) - (uData.assets.usedSpaceSize || 0);
          cloudUserQuotaFreeStr = formatBytes(free);
          const qEl = document.getElementById("cloudAddQuotaDisplay");
          if (qEl) qEl.innerText = cloudUserQuotaFreeStr;
        }
      } catch (e) {}

      // 2. Discover default folder (prefer current folder if navigated inside, else look for "来自：云添加")
      if (currentParentId && currentParentId !== "") {
        cloudAddTargetParentId = currentParentId;
        cloudAddTargetFolderPath = (breadcrumbStack && breadcrumbStack.length > 0) ? breadcrumbStack.map(b => b.name).join("/") : "全部文件";
      } else {
        try {
          const res = await fetch("/api/files?parentId=");
          const data = await res.json();
          const list = data.list || [];
          const cloudFolder = list.find(item => item.resType === 2 && (item.fileName.includes("来自：云添加") || item.fileName.includes("来自: 云添加") || item.fileName === "云添加"));
          if (cloudFolder) {
            cloudAddTargetParentId = cloudFolder.fileId;
            cloudAddTargetFolderPath = "全部文件/" + cloudFolder.fileName;
          } else {
            cloudAddTargetParentId = "";
            cloudAddTargetFolderPath = "全部文件";
          }
        } catch (e) {
          cloudAddTargetParentId = "";
          cloudAddTargetFolderPath = "全部文件";
        }
      }

      const tfEl = document.getElementById("cloudAddTargetFolderDisplay");
      if (tfEl) tfEl.innerText = cloudAddTargetFolderPath;
    }

    function openCloudAddModal() {
      resolvedCloudData = null;
      document.getElementById("magnetInput").value = "";
      selectedTorrentFile = null;
      document.getElementById("torrentFileInput").value = "";
      document.getElementById("torrentFileName").innerText = "点击选择或拖拽 .torrent 种子文件至此处";
      
      // Reset view to Step 1
      document.getElementById("cloudAddStep1").style.display = "block";
      document.getElementById("cloudAddStep2").style.display = "none";
      const nextBtn = document.getElementById("cloudAddNextBtn");
      nextBtn.disabled = false;
      nextBtn.innerText = "下一步：解析任务 ➔";
      
      switchCloudTab('magnet');
      initCloudAddDestination();
      openModal("cloudAddModal");
    }

    function switchCloudTab(tab) {
      activeCloudTab = tab;
      if (tab === 'magnet') {
        document.getElementById("tabMagnetBtn").classList.add("active");
        document.getElementById("tabTorrentBtn").classList.remove("active");
        document.getElementById("magnetSection").style.display = "flex";
        document.getElementById("torrentSection").style.display = "none";
      } else {
        document.getElementById("tabMagnetBtn").classList.remove("active");
        document.getElementById("tabTorrentBtn").classList.add("active");
        document.getElementById("magnetSection").style.display = "none";
        document.getElementById("torrentSection").style.display = "flex";
      }
    }

    function handleTorrentSelect(input) {
      if (input.files && input.files[0]) {
        selectedTorrentFile = input.files[0];
        document.getElementById("torrentFileName").innerText = "已选择: " + selectedTorrentFile.name + ` (${formatBytes(selectedTorrentFile.size)})`;
      }
    }

    async function resolveCloudAdd() {
      const nextBtn = document.getElementById("cloudAddNextBtn");

      if (activeCloudTab === 'magnet') {
        const magnet = document.getElementById("magnetInput").value.trim();
        if (!magnet) {
          alert("请输入磁力链接或下载网址");
          return;
        }
        nextBtn.disabled = true;
        nextBtn.innerText = "⏳ 正在解析资源信息，请稍候...";
        try {
          const res = await fetch("/api/cloud-add/resolve", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: magnet })
          });
          const d = await res.json();
          if (d.success) {
            resolvedCloudData = d;
            showCloudAddStep2(d);
          } else {
            alert("解析失败: " + (d.error || JSON.stringify(d)));
            nextBtn.disabled = false;
            nextBtn.innerText = "下一步：解析任务 ➔";
          }
        } catch (e) {
          alert("网络或解析异常: " + e.message);
          nextBtn.disabled = false;
          nextBtn.innerText = "下一步：解析任务 ➔";
        }
      } else {
        if (!selectedTorrentFile) {
          alert("请先选择 .torrent 种子文件");
          return;
        }
        nextBtn.disabled = true;
        nextBtn.innerText = "⏳ 正在解析种子文件，请稍候...";
        const formData = new FormData();
        formData.append("torrent", selectedTorrentFile);
        try {
          const res = await fetch("/api/cloud-add/resolve", {
            method: "POST",
            body: formData
          });
          const d = await res.json();
          if (d.success) {
            resolvedCloudData = d;
            showCloudAddStep2(d);
          } else {
            alert("解析种子失败: " + (d.error || JSON.stringify(d)));
            nextBtn.disabled = false;
            nextBtn.innerText = "下一步：解析任务 ➔";
          }
        } catch (e) {
          alert("上传或解析异常: " + e.message);
          nextBtn.disabled = false;
          nextBtn.innerText = "下一步：解析任务 ➔";
        }
      }
    }

    function showCloudAddStep2(data) {
      document.getElementById("cloudAddStep1").style.display = "none";
      document.getElementById("cloudAddStep2").style.display = "block";
      
      // Title & Badge
      const titleInput = document.getElementById("cloudAddTitleInput");
      titleInput.value = data.title || "";
      titleInput.style.border = "1px solid transparent";
      titleInput.style.background = "transparent";

      const typeBadge = document.getElementById("cloudAddTypeBadge");
      if (typeBadge) {
        typeBadge.innerText = (data.type === 'url') ? 'URL' : 'BT';
        typeBadge.style.background = (data.type === 'url') ? '#3b82f6' : '#22c55e';
      }

      const files = data.files || [];
      document.getElementById("cloudAddFileCountDisplay").innerText = `共 ${files.length} 个文件`;
      document.getElementById("cloudAddTotalSizeDisplay").innerText = data.formattedTotalSize || formatBytes(data.totalSize || 0);

      document.getElementById("cloudAddTargetFolderDisplay").innerText = cloudAddTargetFolderPath;
      document.getElementById("cloudAddQuotaDisplay").innerText = cloudUserQuotaFreeStr;

      const container = document.getElementById("cloudAddFileListContainer");
      container.innerHTML = "";

      const videoExts = ["mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "ts", "m4v", "iso", "rmvb"];

      if (files.length === 0) {
        container.innerHTML = `
          <div style="padding: 28px 16px; text-align: center; color: var(--text-muted); font-size: 13px;">
            ⚠️ 该资源为单文件或未返回具体子文件列表，将直接整体下载。
          </div>
        `;
      } else {
        files.forEach((f, idx) => {
          const row = document.createElement("div");
          row.className = "cloud-file-row";
          if (idx === files.length - 1) {
            row.style.borderBottom = "none";
          }

          const ext = (f.fileName || "").split(".").pop().toLowerCase();
          const isVideo = videoExts.includes(ext);

          // Icon styling matching official web app
          let iconHtml = "";
          if (isVideo) {
            iconHtml = `<span style="width: 22px; height: 22px; border-radius: 5px; background: #6366f1; color: #fff; display: inline-flex; align-items: center; justify-content: center; font-size: 10px; flex-shrink: 0;">▶</span>`;
          } else if (["txt", "nfo", "md", "srt", "ass", "log", "sub"].includes(ext)) {
            iconHtml = `<span style="width: 22px; height: 22px; border-radius: 5px; background: #93c5fd; color: #1e3a8a; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; flex-shrink: 0;">≡</span>`;
          } else {
            iconHtml = `<span style="width: 22px; height: 22px; border-radius: 5px; background: #e2e8f0; color: #64748b; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; flex-shrink: 0;">?</span>`;
          }

          row.innerHTML = `
            <div style="display: flex; align-items: center; gap: 8px; min-width: 0; overflow: hidden;">
              <input type="checkbox" class="cloud-file-cb" data-index="${f.fileIndex}" data-size="${f.fileSize || 0}" data-ext="${ext}" data-video="${isVideo ? '1' : '0'}" ${f.selected ? 'checked' : ''} style="cursor: pointer; width: 16px; height: 16px; accent-color: #ff6600; flex-shrink: 0;">
              ${iconHtml}
              <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-main); font-size: 13px; min-width: 0;" title="${escapeHtml(f.fileName)}">${escapeHtml(f.fileName)}</span>
            </div>
            <div style="text-align: center; color: var(--text-muted); font-size: 12px; font-family: monospace; white-space: nowrap;">${escapeHtml(ext || '-')}</div>
            <div style="text-align: right; color: var(--text-muted); font-size: 12.5px; font-family: monospace; white-space: nowrap;">${escapeHtml(f.formattedSize || formatBytes(f.fileSize || 0))}</div>
          `;

          row.onclick = (e) => {
            if (e.target.tagName !== 'INPUT') {
              const cb = row.querySelector('.cloud-file-cb');
              cb.checked = !cb.checked;
            }
            updateFilterStateAndSummary();
          };
          const cb = row.querySelector('.cloud-file-cb');
          cb.onchange = () => updateFilterStateAndSummary();

          container.appendChild(row);
        });
      }

      updateFilterStateAndSummary();
    }

    function focusTitleInput() {
      const inp = document.getElementById("cloudAddTitleInput");
      inp.focus();
      inp.select();
      inp.style.border = "1px solid #ff6600";
      inp.style.background = "var(--bg-subtle)";
      inp.onblur = () => {
        inp.style.border = "1px solid transparent";
        inp.style.background = "transparent";
      };
    }

    function toggleSelectAllFiles(checked) {
      const cbs = document.querySelectorAll(".cloud-file-cb");
      cbs.forEach(cb => cb.checked = checked);
      updateFilterStateAndSummary();
    }

    function toggleFilterCategory(cat, checked) {
      if (cat === 'video') {
        document.querySelectorAll('.cloud-file-cb[data-video="1"]').forEach(cb => cb.checked = checked);
      } else {
        document.querySelectorAll('.cloud-file-cb[data-video="0"]').forEach(cb => cb.checked = checked);
      }
      updateFilterStateAndSummary();
    }

    function updateFilterStateAndSummary() {
      const allCbs = Array.from(document.querySelectorAll(".cloud-file-cb"));
      const checkedCbs = allCbs.filter(cb => cb.checked);
      const videoCbs = allCbs.filter(cb => cb.dataset.video === "1");
      const checkedVideoCbs = videoCbs.filter(cb => cb.checked);
      const otherCbs = allCbs.filter(cb => cb.dataset.video === "0");
      const checkedOtherCbs = otherCbs.filter(cb => cb.checked);

      // 1. Select All Checkbox
      const allCb = document.getElementById("cloudAddSelectAllCb");
      if (allCbs.length > 0 && checkedCbs.length === allCbs.length) {
        allCb.checked = true;
        allCb.indeterminate = false;
      } else if (checkedCbs.length > 0) {
        allCb.checked = false;
        allCb.indeterminate = true;
      } else {
        allCb.checked = false;
        allCb.indeterminate = false;
      }

      // 2. Video filter checkbox
      const vCb = document.getElementById("filterVideoCb");
      if (videoCbs.length === 0) {
        vCb.checked = false; vCb.indeterminate = false; vCb.disabled = true;
      } else if (checkedVideoCbs.length === videoCbs.length) {
        vCb.checked = true; vCb.indeterminate = false; vCb.disabled = false;
      } else if (checkedVideoCbs.length > 0) {
        vCb.checked = false; vCb.indeterminate = true; vCb.disabled = false;
      } else if (checkedVideoCbs.length === 0) {
        vCb.checked = false; vCb.indeterminate = false; vCb.disabled = false;
      } else {
        vCb.checked = false; vCb.indeterminate = true; vCb.disabled = false;
      }

      // 3. Other filter checkbox
      const oCb = document.getElementById("filterOtherCb");
      if (otherCbs.length === 0) {
        oCb.checked = false; oCb.indeterminate = false; oCb.disabled = true;
      } else if (checkedOtherCbs.length === otherCbs.length) {
        oCb.checked = true; oCb.indeterminate = false; oCb.disabled = false;
      } else if (checkedOtherCbs.length > 0) {
        oCb.checked = false; oCb.indeterminate = true; oCb.disabled = false;
      } else if (checkedOtherCbs.length === 0) {
        oCb.checked = false; oCb.indeterminate = false; oCb.disabled = false;
      } else {
        oCb.checked = false; oCb.indeterminate = true; oCb.disabled = false;
      }

      // 4. Counter text
      document.getElementById("cloudAddSelectedCountText").innerText = `已选${checkedCbs.length}项`;

      // 5. Submit button
      const confirmBtn = document.getElementById("confirmCloudAddBtn");
      if (allCbs.length > 0 && checkedCbs.length === 0) {
        confirmBtn.disabled = true;
        confirmBtn.innerText = "请至少勾选 1 项";
      } else {
        confirmBtn.disabled = false;
        confirmBtn.innerText = "立即添加";
      }
    }

    function backToCloudAddStep1() {
      document.getElementById("cloudAddStep1").style.display = "block";
      document.getElementById("cloudAddStep2").style.display = "none";
      const nextBtn = document.getElementById("cloudAddNextBtn");
      nextBtn.disabled = false;
      nextBtn.innerText = "下一步：解析任务 ➔";
    }

    // ================= CLOUD DRIVE FOLDER TREE PICKER (保存到 - 复刻光鸭网页版树状结构) =================
    let cloudTreeData = {}; // parentId -> [ { fileId, fileName, resType } ]
    let cloudTreeExpanded = { "": true }; // folderId -> boolean
    let cloudTreeSelectedId = "";
    let cloudTreeSelectedPath = "全部文件";
    let cloudDefaultTargetId = "";
    let cloudDefaultTargetPath = "全部文件/来自: 云添加";
    let cloudFolderParents = {}; // folderId -> { parentId, fileName }

    function getCloudFolderPath(folderId) {
      if (!folderId || folderId === "") return "全部文件";
      const parts = [];
      let curr = folderId;
      while (curr && cloudFolderParents[curr]) {
        parts.unshift(cloudFolderParents[curr].fileName);
        curr = cloudFolderParents[curr].parentId;
      }
      return "全部文件" + (parts.length > 0 ? "/" + parts.join("/") : "");
    }

    async function loadTreeFolderChildren(parentId) {
      if (cloudTreeData[parentId] !== undefined) return cloudTreeData[parentId];
      try {
        const res = await fetch(`/api/files?parentId=${encodeURIComponent(parentId)}`);
        const json = await res.json();
        const folders = (json.list || []).filter(item => item.resType === 2 && item.fileName !== "保险箱");
        folders.forEach(f => {
          cloudFolderParents[f.fileId] = { parentId: parentId, fileName: f.fileName };
        });
        cloudTreeData[parentId] = folders;
        return folders;
      } catch (e) {
        console.error("Failed to load tree folder:", e);
        cloudTreeData[parentId] = [];
        return [];
      }
    }

    let cloudFolderPickerCallback = null;

    async function openCloudFolderPickerModal(customCallback = null, initId = null, initPath = null) {
      cloudFolderPickerCallback = customCallback;
      openModal("cloudFolderPickerModal");
      const container = document.getElementById("cloudFolderTreeContainer");
      container.innerHTML = `<div style="text-align:center; padding:35px; color:var(--text-muted); font-size:13px;">正在读取网盘目录...</div>`;

      // Load root folders
      await loadTreeFolderChildren("");

      // Find default folder (来自：云添加)
      const rootFolders = cloudTreeData[""] || [];
      const cloudFolder = rootFolders.find(f => f.fileName && f.fileName.includes("云添加"));
      if (cloudFolder) {
        cloudDefaultTargetId = cloudFolder.fileId;
        cloudDefaultTargetPath = "全部文件/" + cloudFolder.fileName;
      }

      // Initial selection
      if (initId !== null) {
        cloudTreeSelectedId = initId;
        cloudTreeSelectedPath = initPath || getCloudFolderPath(initId);
      } else if (cloudAddTargetParentId) {
        cloudTreeSelectedId = cloudAddTargetParentId;
        cloudTreeSelectedPath = cloudAddTargetFolderPath || getCloudFolderPath(cloudAddTargetParentId);
      } else if (cloudDefaultTargetId) {
        cloudTreeSelectedId = cloudDefaultTargetId;
        cloudTreeSelectedPath = cloudDefaultTargetPath;
      } else {
        cloudTreeSelectedId = "";
        cloudTreeSelectedPath = "全部文件";
      }

      cloudTreeExpanded[""] = true;
      updateTreeLocationDisplay();
      renderCloudFolderTree();
    }

    function updateTreeLocationDisplay() {
      const pathEl = document.getElementById("cloudTreeCurrentPath");
      const badgeEl = document.getElementById("cloudTreeDefaultBadge");
      if (pathEl) pathEl.innerText = cloudTreeSelectedPath || "全部文件";
      if (badgeEl) {
        const isDefault = (cloudTreeSelectedId === cloudDefaultTargetId) || 
                          (cloudTreeSelectedPath === cloudDefaultTargetPath) ||
                          (cloudTreeSelectedPath.includes("云添加"));
        badgeEl.style.display = isDefault ? "inline-block" : "none";
      }
    }

    function restoreDefaultCloudFolderTree() {
      if (cloudDefaultTargetId) {
        cloudTreeSelectedId = cloudDefaultTargetId;
        cloudTreeSelectedPath = cloudDefaultTargetPath;
      } else {
        cloudTreeSelectedId = "";
        cloudTreeSelectedPath = "全部文件";
      }
      updateTreeLocationDisplay();
      renderCloudFolderTree();
    }

    function renderCloudFolderTree() {
      const container = document.getElementById("cloudFolderTreeContainer");
      if (!container) return;

      let html = "";

      // 1. Root node: 全部文件
      const rootExpanded = !!cloudTreeExpanded[""];
      const rootSelected = (cloudTreeSelectedId === "");
      html += `
        <div class="cloud-tree-node ${rootSelected ? 'selected' : ''}" style="padding-left: 10px;" onclick="selectCloudTreeNode('', '全部文件', event)">
          <div class="cloud-tree-arrow" onclick="event.stopPropagation(); toggleCloudTreeNode('', event)">
            ${rootExpanded ? '▼' : '▶'}
          </div>
          <span class="cloud-tree-icon">📁</span>
          <span class="cloud-tree-name">全部文件</span>
          ${rootSelected ? '<span class="cloud-tree-check">✓</span>' : ''}
        </div>
      `;

      // 2. Sub items
      if (rootExpanded) {
        html += renderTreeChildrenHtml("", 1);
      }

      container.innerHTML = html;
    }

    function renderTreeChildrenHtml(parentId, level) {
      const children = cloudTreeData[parentId] || [];
      if (children.length === 0) return "";

      let html = "";
      const indentPx = 10 + (level * 22);

      children.forEach(f => {
        const isExpanded = !!cloudTreeExpanded[f.fileId];
        const isSelected = (cloudTreeSelectedId === f.fileId);
        const hasLoadedChildren = cloudTreeData[f.fileId] !== undefined;
        const subChildren = cloudTreeData[f.fileId] || [];
        const showArrow = !hasLoadedChildren || subChildren.length > 0;

        html += `
          <div class="cloud-tree-node ${isSelected ? 'selected' : ''}" style="padding-left: ${indentPx}px;" onclick="selectCloudTreeNode('${f.fileId}', '${escapeHtml(f.fileName)}', event)">
            <div class="cloud-tree-arrow" onclick="event.stopPropagation(); toggleCloudTreeNode('${f.fileId}', event)">
              ${showArrow ? (isExpanded ? '▼' : '▶') : '<span style="display:inline-block;width:12px;"></span>'}
            </div>
            <span class="cloud-tree-icon">📁</span>
            <span class="cloud-tree-name" title="${escapeHtml(f.fileName)}">${escapeHtml(f.fileName)}</span>
            ${isSelected ? '<span class="cloud-tree-check">✓</span>' : ''}
          </div>
        `;

        if (isExpanded && hasLoadedChildren && subChildren.length > 0) {
          html += renderTreeChildrenHtml(f.fileId, level + 1);
        }
      });

      return html;
    }

    async function toggleCloudTreeNode(folderId, event) {
      if (event) event.stopPropagation();
      const willExpand = !cloudTreeExpanded[folderId];
      cloudTreeExpanded[folderId] = willExpand;

      if (willExpand && cloudTreeData[folderId] === undefined) {
        renderCloudFolderTree();
        await loadTreeFolderChildren(folderId);
      }
      renderCloudFolderTree();
    }

    async function selectCloudTreeNode(folderId, folderName, event) {
      cloudTreeSelectedId = folderId;
      cloudTreeSelectedPath = getCloudFolderPath(folderId);
      updateTreeLocationDisplay();

      if (folderId && !cloudTreeExpanded[folderId]) {
        cloudTreeExpanded[folderId] = true;
        if (cloudTreeData[folderId] === undefined) {
          renderCloudFolderTree();
          await loadTreeFolderChildren(folderId);
        }
      }
      renderCloudFolderTree();
    }

    async function promptCreateFolderInTree() {
      const targetParentId = cloudTreeSelectedId || "";
      const currentPath = cloudTreeSelectedPath || "全部文件";
      const name = prompt(`在 “${currentPath}” 目录下新建文件夹：`);
      if (!name || !name.trim()) return;

      try {
        const res = await fetch("/api/create-folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim(), parentId: targetParentId })
        });
        const d = await res.json();
        if (d.msg === "success" || d.code === 0) {
          showToast("新建文件夹成功");
          delete cloudTreeData[targetParentId];
          cloudTreeExpanded[targetParentId] = true;
          await loadTreeFolderChildren(targetParentId);
          const children = cloudTreeData[targetParentId] || [];
          const newFolder = children.find(c => c.fileName === name.trim());
          if (newFolder) {
            cloudTreeSelectedId = newFolder.fileId;
            cloudTreeSelectedPath = getCloudFolderPath(newFolder.fileId);
          }
          updateTreeLocationDisplay();
          renderCloudFolderTree();
        } else {
          alert("新建失败: " + (d.msg || JSON.stringify(d)));
        }
      } catch (e) {
        alert("网络异常: " + e.message);
      }
    }

    function confirmCloudFolderTreeSelection() {
      const selId = cloudTreeSelectedId || "";
      const selPath = cloudTreeSelectedPath || "全部文件";
      if (typeof cloudFolderPickerCallback === "function") {
        const cb = cloudFolderPickerCallback;
        cloudFolderPickerCallback = null;
        cb(selId, selPath);
      } else {
        cloudAddTargetParentId = selId;
        cloudAddTargetFolderPath = selPath;
        const el = document.getElementById("cloudAddTargetFolderDisplay");
        if (el) el.innerText = selPath;
      }
      closeModal("cloudFolderPickerModal");
    }

    // ================= STRM MEDIA CENTER & QUICK GENERATOR =================
    let strmState = {
      cloudParentId: "",
      cloudFolderPath: "全部文件",
      outputDir: "",
      baseUrl: "",
      detectedIp: "127.0.0.1",
      panelTab: "recent",
      running: false
    };

    let quickStrmTarget = {
      id: "",
      name: "",
      isDir: true,
      cloudPath: "全部文件"
    };

    function switchStrmPanelTab(tab) {
      strmState.panelTab = tab;
      document.getElementById("tabStrmRecentBtn")?.classList.toggle("active", tab === "recent");
      document.getElementById("tabStrmLogsBtn")?.classList.toggle("active", tab === "logs");
      document.getElementById("strmRecentListContainer").style.display = (tab === "recent") ? "flex" : "none";
      document.getElementById("strmLogsContainer").style.display = (tab === "logs") ? "block" : "none";
    }

    async function loadStrmView(populateInputs = false) {
      try {
        const res = await fetch("/api/strm/config");
        const data = await res.json();
        const cfg = data.config || {};
        strmState.detectedIp = data.detected_ip || "127.0.0.1";
        strmState.outputDir = cfg.output_dir || "";
        strmState.baseUrl = cfg.base_url || `http://${strmState.detectedIp}:8998`;

        if (populateInputs) {
          strmState.cloudParentId = cfg.cloud_parent_id || "";
          strmState.cloudFolderPath = cfg.cloud_folder_path || "全部文件";
          document.getElementById("strmCloudPathInput").value = strmState.cloudFolderPath;
          document.getElementById("strmOutputDirInput").value = strmState.outputDir;
          document.getElementById("strmBaseUrlInput").value = strmState.baseUrl;
          document.getElementById("strmScheduleSelect").value = String(cfg.schedule_interval_min || 0);
          document.getElementById("strmDlSubsCb").checked = cfg.download_subtitles !== false;
          document.getElementById("strmDlMetaCb").checked = !!cfg.download_metadata;
          document.getElementById("strmAutoOfflineCb").checked = cfg.auto_sync_offline !== false;
          document.getElementById("strmOverwriteCb").checked = cfg.overwrite !== false;
          document.getElementById("strmCleanOrphansCb").checked = !!cfg.clean_orphans;
          document.getElementById("strmUseProxyCb").checked = !!cfg.use_proxy;
        }
        renderStrmStatus(data);
      } catch (e) {
        console.error("Failed to load STRM view:", e);
      }
    }

    function renderStrmStatus(data) {
      if (!data) return;
      const st = data.status || {};
      const running = !!st.running;
      strmState.running = running;

      const navBadge = document.getElementById("strmRunningBadge");
      if (navBadge) navBadge.style.display = running ? "inline-block" : "none";

      const topBadge = document.getElementById("strmTopStatusBadge");
      const startBtn = document.getElementById("strmStartSyncBtn");
      const cancelBtn = document.getElementById("strmCancelSyncBtn");

      if (topBadge) {
        if (running) {
          topBadge.className = "status-badge status-active";
          topBadge.innerText = "⏳ 正在同步中...";
        } else if (st.status === "completed") {
          topBadge.className = "status-badge status-success";
          topBadge.innerText = "✅ 同步已完成";
        } else if (st.status === "failed") {
          topBadge.className = "status-badge status-failed";
          topBadge.innerText = "❌ 同步异常";
        } else if (st.status === "cancelled") {
          topBadge.className = "status-badge status-warning";
          topBadge.innerText = "⏹️ 已停止";
        } else {
          topBadge.className = "status-badge status-success";
          topBadge.innerText = "就绪";
        }
      }

      if (startBtn) {
        startBtn.disabled = running;
        startBtn.innerText = running ? "⏳ 同步执行中..." : "⚡ 立即同步 STRM 影视库";
      }
      if (cancelBtn) {
        cancelBtn.style.display = running ? "inline-flex" : "none";
      }

      const createdTotal = (st.createdStrm || 0) + (st.updatedStrm || 0);
      const elCreated = document.getElementById("statStrmCreated");
      if (elCreated) elCreated.innerText = createdTotal;
      const elSkipped = document.getElementById("statStrmSkipped");
      if (elSkipped) elSkipped.innerText = st.skippedStrm || 0;
      const elSubs = document.getElementById("statStrmSubs");
      if (elSubs) elSubs.innerText = st.downloadedSubs || 0;
      const elScanned = document.getElementById("statStrmScanned");
      if (elScanned) elScanned.innerText = st.scannedDirs || 0;

      const cacheEl = document.getElementById("strmCacheCountText");
      if (cacheEl) cacheEl.innerText = data.cache_count || 0;
      const hitsEl = document.getElementById("strmTotalHitsText");
      if (hitsEl) hitsEl.innerText = `302 缓存命中: ${data.total_hits || 0} 次`;

      const actEl = document.getElementById("strmCurrentActionText");
      const fileEl = document.getElementById("strmCurrentFileText");
      if (actEl && fileEl) {
        if (running) {
          actEl.innerText = `正在扫描目录: ${st.currentDir || '全部文件'} (${st.reason || '同步'})`;
          fileEl.innerText = `当前处理: ${st.currentFile || '读取列表中...'}`;
        } else if (st.status === "completed") {
          actEl.innerText = `上次同步完成 (${st.reason || '手动'}) · 共扫描 ${st.scannedFiles || 0} 个文件`;
          fileEl.innerText = `新增 ${st.createdStrm || 0} 个 · 更新 ${st.updatedStrm || 0} 个 · 跳过 ${st.skippedStrm || 0} 个 · 字幕 ${st.downloadedSubs || 0} 个`;
        } else if (st.status === "failed") {
          actEl.innerText = `同步失败`;
          fileEl.innerText = st.error || "请检查网盘登录状态或目录权限";
        }
      }

      // Render Recent Files
      const recentList = st.recentFiles || [];
      const countEl = document.getElementById("countStrmRecent");
      if (countEl) countEl.innerText = recentList.length;

      const rContainer = document.getElementById("strmRecentListContainer");
      if (rContainer) {
        if (recentList.length === 0) {
          rContainer.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 36px 12px; font-size: 12.5px;">尚未生成 STRM 记录，点击「⚡ 立即同步 STRM 影视库」开始构建海报墙音源。</div>`;
        } else {
          rContainer.innerHTML = "";
          recentList.forEach(item => {
            const row = document.createElement("div");
            row.style.cssText = "display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 12px; background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); font-size: 12.5px;";
            row.innerHTML = `
              <div style="min-width: 0; flex: 1;">
                <div style="font-weight: 600; color: var(--text-main); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(item.strmName)}">
                  🎬 ${escapeHtml(item.strmName)}
                </div>
                <div style="font-size: 11px; color: var(--text-muted); font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;" title="${escapeHtml(item.strmPath)}">
                  ${escapeHtml(item.strmPath)}
                </div>
              </div>
              <div style="display: flex; gap: 6px; flex-shrink: 0;">
                <button class="btn-secondary btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="copyStrmUrl('${escapeHtml(item.streamUrl)}')" title="复制 302 播放地址 (可在 PotPlayer/VLC/浏览器测试)">🔗 复制串流</button>
              </div>
            `;
            rContainer.appendChild(row);
          });
        }
      }

      // Render Logs
      const logs = st.logs || [];
      const lContainer = document.getElementById("strmLogsContainer");
      if (lContainer) {
        if (logs.length === 0) {
          lContainer.innerText = "暂无执行日志";
        } else {
          lContainer.innerText = logs.join("\\n");
          lContainer.scrollTop = lContainer.scrollHeight;
        }
      }
    }

    async function copyStrmUrl(url) {
      await copyToClipboard(url);
      showToast("302 动态串流链接已复制！可粘贴至播放器测试");
    }

    function pickStrmCloudFolder() {
      openCloudFolderPickerModal((folderId, folderPath) => {
        strmState.cloudParentId = folderId || "";
        strmState.cloudFolderPath = folderPath || "全部文件";
        document.getElementById("strmCloudPathInput").value = strmState.cloudFolderPath;
      }, strmState.cloudParentId, strmState.cloudFolderPath);
    }

    function onStrmOutputFolderPicked(selectedPath) {
      document.getElementById("strmOutputDirInput").value = selectedPath;
    }

    function autoFillStrmBaseUrl() {
      const hostIp = strmState.detectedIp && strmState.detectedIp !== "127.0.0.1"
        ? strmState.detectedIp
        : (window.location.hostname || "127.0.0.1");
      const url = `http://${hostIp}:8998`;
      document.getElementById("strmBaseUrlInput").value = url;
      showToast(`已填入 NAS 局域网网关地址: ${url}`);
    }

    async function saveStrmConfig(showNotification = true) {
      const payload = {
        cloud_parent_id: strmState.cloudParentId || "",
        cloud_folder_path: document.getElementById("strmCloudPathInput").value.trim() || "全部文件",
        output_dir: document.getElementById("strmOutputDirInput").value.trim(),
        base_url: document.getElementById("strmBaseUrlInput").value.trim(),
        schedule_interval_min: parseInt(document.getElementById("strmScheduleSelect").value || "0", 10),
        download_subtitles: document.getElementById("strmDlSubsCb").checked,
        download_metadata: document.getElementById("strmDlMetaCb").checked,
        auto_sync_offline: document.getElementById("strmAutoOfflineCb").checked,
        overwrite: document.getElementById("strmOverwriteCb").checked,
        clean_orphans: document.getElementById("strmCleanOrphansCb").checked,
        use_proxy: document.getElementById("strmUseProxyCb").checked
      };
      try {
        const res = await fetch("/api/strm/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.config) {
          strmState.outputDir = d.config.output_dir;
          strmState.baseUrl = d.config.base_url;
        }
        if (showNotification) showToast("STRM 映射与播放网关配置已保存");
        return true;
      } catch (e) {
        if (showNotification) alert("保存 STRM 配置失败: " + e.message);
        return false;
      }
    }

    async function triggerFullStrmSync() {
      await saveStrmConfig(false);
      try {
        const res = await fetch("/api/strm/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            cloudParentId: strmState.cloudParentId || "",
            cloudFolderPath: document.getElementById("strmCloudPathInput").value.trim() || "全部文件",
            outputDir: document.getElementById("strmOutputDirInput").value.trim(),
            reason: "全量/增量同步"
          })
        });
        const d = await res.json();
        if (d.error && !d.started) {
          alert(d.error);
        } else {
          showToast("🎬 已启动 STRM 影视库后台同步任务！");
          loadStrmView(false);
        }
      } catch (e) {
        alert("启动 STRM 同步失败: " + e.message);
      }
    }

    async function cancelStrmSync() {
      try {
        await fetch("/api/strm/cancel", { method: "POST" });
        showToast("正在停止 STRM 同步任务...");
        loadStrmView(false);
      } catch (e) {}
    }

    async function clearStrmUrlCache() {
      try {
        const res = await fetch("/api/strm/clear-cache", { method: "POST" });
        const d = await res.json();
        showToast(`已清空 ${d.cleared || 0} 条 302 直链缓存`);
        loadStrmView(false);
      } catch (e) {}
    }

    // Quick STRM Modal from File Explorer
    async function openQuickStrmModal(fileId, fileName, isDir) {
      if (!strmState.outputDir) {
        try {
          const r = await fetch("/api/strm/config");
          const d = await r.json();
          if (d.config && d.config.output_dir) strmState.outputDir = d.config.output_dir;
        } catch (e) {}
      }
      const basePath = (breadcrumbStack && breadcrumbStack.length > 0)
        ? breadcrumbStack.map(b => b.name).join("/")
        : "全部文件";
      const fullCloudPath = fileName ? (basePath + "/" + fileName) : basePath;

      quickStrmTarget = {
        id: fileId,
        name: fileName || "全部文件",
        isDir: isDir,
        cloudPath: isDir ? fullCloudPath : basePath
      };

      document.getElementById("quickStrmItemIcon").innerText = isDir ? "📁" : "🎬";
      document.getElementById("quickStrmItemName").innerText = isDir ? fullCloudPath : fileName;
      document.getElementById("quickStrmOutputDirInput").value = strmState.outputDir || (defaultDownloadDir + "/STRM影视库");
      document.getElementById("quickStrmKeepFolderRow").style.display = (isDir && fileId !== "") ? "flex" : "none";
      document.getElementById("quickStrmKeepFolderCb").checked = (isDir && fileId !== "");
      openModal("quickStrmModal");
    }

    function openQuickStrmModalForCurrentDir() {
      const currCrumb = (breadcrumbStack && breadcrumbStack.length > 0)
        ? breadcrumbStack[breadcrumbStack.length - 1]
        : { id: "", name: "全部文件" };
      openQuickStrmModal(currCrumb.id, currCrumb.id === "" ? "" : currCrumb.name, true);
    }

    function onQuickStrmFolderPicked(selectedPath) {
      document.getElementById("quickStrmOutputDirInput").value = selectedPath;
    }

    async function confirmQuickStrmGenerate() {
      const outDir = document.getElementById("quickStrmOutputDirInput").value.trim();
      if (!outDir) {
        alert("请选择 NAS 本地 STRM 输出目录");
        return;
      }
      const keepFolder = document.getElementById("quickStrmKeepFolderCb").checked;
      const dlSubs = document.getElementById("quickStrmDlSubsCb").checked;

      const payload = {
        outputDir: outDir,
        downloadSubtitles: dlSubs,
        keepRootFolder: quickStrmTarget.isDir && keepFolder && quickStrmTarget.id !== "",
        reason: `快捷生成 (${quickStrmTarget.name})`
      };

      if (quickStrmTarget.isDir) {
        payload.cloudParentId = quickStrmTarget.id;
        payload.cloudFolderPath = quickStrmTarget.cloudPath;
      } else {
        payload.singleFile = {
          fileId: quickStrmTarget.id,
          fileName: quickStrmTarget.name
        };
      }

      try {
        const res = await fetch("/api/strm/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        closeModal("quickStrmModal");
        if (d.error && !d.started) {
          alert(d.error);
        } else {
          showToast(`🎬 已启动 "${quickStrmTarget.name}" 的 STRM 生成任务`);
          switchView("strm");
        }
      } catch (e) {
        alert("请求生成 STRM 失败: " + e.message);
      }
    }

    async function confirmSubmitCloudAdd() {
      if (!resolvedCloudData) return;
      const cbs = Array.from(document.querySelectorAll(".cloud-file-cb"));
      let selectedIndexes = null;
      if (cbs.length > 0) {
        const checked = cbs.filter(cb => cb.checked);
        if (checked.length === 0) {
          alert("请至少勾选一个要下载的文件！");
          return;
        }
        selectedIndexes = checked.map(cb => parseInt(cb.dataset.index, 10));
      }

      const newName = document.getElementById("cloudAddTitleInput").value.trim();
      const confirmBtn = document.getElementById("confirmCloudAddBtn");
      confirmBtn.disabled = true;
      confirmBtn.innerText = "⏳ 正在提交离线任务...";

      try {
        const payload = {
          url: resolvedCloudData.url,
          parentId: cloudAddTargetParentId || "",
          fileIndexes: selectedIndexes,
          newName: newName || undefined
        };

        const res = await fetch("/api/cloud-add/magnet", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (d.data && d.data.taskId) {
          showToast(`离线任务添加成功！保存至: ${cloudAddTargetFolderPath}`);
          closeModal("cloudAddModal");
          switchView("offline");
          loadCloudTasks();
        } else {
          alert("添加离线任务失败: " + (d.msg || JSON.stringify(d)));
          confirmBtn.disabled = false;
          updateFilterStateAndSummary();
        }
      } catch (e) {
        alert("提交失败: " + e.message);
        confirmBtn.disabled = false;
        updateFilterStateAndSummary();
      }
    }

    const submitCloudAdd = resolveCloudAdd;

    // ================= UPLOAD =================
    function openUploadModal() {
      selectedUploadFile = null;
      document.getElementById("uploadFileNameText").innerText = "点击选择或拖拽文件到此处";
      document.getElementById("uploadProgressSection").style.display = "none";
      document.getElementById("startUploadBtn").disabled = false;
      openModal("uploadModal");
    }

    function handleUploadSelect(input) {
      if (input.files && input.files[0]) {
        selectedUploadFile = input.files[0];
        document.getElementById("uploadFileNameText").innerText = `已选择: ${selectedUploadFile.name} (${formatBytes(selectedUploadFile.size)})`;
      }
    }

    async function submitUpload() {
      if (!selectedUploadFile) {
        alert("请先选择要上传的文件");
        return;
      }
      document.getElementById("startUploadBtn").disabled = true;
      document.getElementById("uploadProgressSection").style.display = "block";
      document.getElementById("uploadStatusLabel").innerText = "正在上传文件...";
      document.getElementById("uploadProgressBar").style.width = "0%";
      document.getElementById("uploadPctLabel").innerText = "0%";

      const formData = new FormData();
      formData.append("file", selectedUploadFile);
      formData.append("parentId", currentParentId);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const pct = Math.floor((e.loaded / e.total) * 100);
          document.getElementById("uploadProgressBar").style.width = pct + "%";
          document.getElementById("uploadPctLabel").innerText = pct + "%";
          if (pct === 100) {
            document.getElementById("uploadStatusLabel").innerText = "文件已送达网盘，正在云端校验...";
          }
        }
      };
      xhr.onload = () => {
        if (xhr.status === 200) {
          showToast("文件上传成功！");
          closeModal("uploadModal");
          loadFiles();
        } else {
          alert("上传失败: " + xhr.responseText);
          document.getElementById("startUploadBtn").disabled = false;
        }
      };
      xhr.onerror = () => {
        alert("网络传输错误");
        document.getElementById("startUploadBtn").disabled = false;
      };
      xhr.send(formData);
    }

    // Modal helpers
    function openModal(id) { document.getElementById(id).style.display = "flex"; }
    function closeModal(id) { document.getElementById(id).style.display = "none"; }


    // ================= AUTH / LOGIN / LOGOUT =================
    let currentDeviceCode = "";
    let currentAuthUrl = "";
    let devicePollTimer = null;
    let isLoggedIn = false;

    function openLoginModal() {
      openModal("loginModal");
      switchLoginTab("qr");
      refreshDeviceCode();
    }

    function closeLoginModal() {
      closeModal("loginModal");
      if (devicePollTimer) {
        clearInterval(devicePollTimer);
        devicePollTimer = null;
      }
    }

    function switchLoginTab(tab) {
      if (tab === 'qr') {
        document.getElementById("tabLoginQr").classList.add("active");
        document.getElementById("tabLoginToken").classList.remove("active");
        document.getElementById("loginTabContentQr").style.display = "flex";
        document.getElementById("loginTabContentToken").style.display = "none";
      } else {
        document.getElementById("tabLoginToken").classList.add("active");
        document.getElementById("tabLoginQr").classList.remove("active");
        document.getElementById("loginTabContentToken").style.display = "flex";
        document.getElementById("loginTabContentQr").style.display = "none";
      }
    }

    async function refreshDeviceCode() {
      if (devicePollTimer) {
        clearInterval(devicePollTimer);
        devicePollTimer = null;
      }
      const container = document.getElementById("qrCodeCanvas");
      container.innerHTML = "";
      document.getElementById("qrExpiredMask").style.display = "none";
      const badge = document.getElementById("loginStatusBadge");
      badge.className = "status-badge status-active";
      badge.innerText = "⌛ 正在获取官方授权码...";

      try {
        const res = await fetch("/api/auth/device-code", { method: "POST" });
        const data = await res.json();
        if (data.error || !data.device_code) {
          badge.className = "status-badge status-failed";
          badge.innerText = "❌ 获取授权码失败: " + (data.error || "网络异常");
          return;
        }

        currentDeviceCode = data.device_code;
        currentAuthUrl = data.verification_uri_complete || data.verification_url;


        // Render QR Code using QRCode.js
        if (typeof QRCode !== "undefined") {
          new QRCode(container, {
            text: currentAuthUrl,
            width: 180,
            height: 180,
            colorDark: "#000000",
            colorLight: "#ffffff",
            correctLevel: QRCode.CorrectLevel.M
          });
        } else {
          container.innerHTML = `<div style="text-align: center; color: #111; font-size: 11px; padding: 20px;">二维码渲染组件加载中...</div>`;
        }

        badge.className = "status-badge status-active";
        badge.innerText = "⌛ 等待扫码...";

        const interval = Math.max(2, data.interval || 2);
        devicePollTimer = setInterval(pollLoginStatus, interval * 1000);
      } catch (e) {
        badge.className = "status-badge status-failed";
        badge.innerText = "❌ 获取授权码出错: " + e.message;
      }
    }

    async function pollLoginStatus() {
      if (!currentDeviceCode) return;
      try {
        const res = await fetch("/api/auth/poll-login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device_code: currentDeviceCode })
        });
        const data = await res.json();
        const badge = document.getElementById("loginStatusBadge");

        if (data.status === "success") {
          if (devicePollTimer) clearInterval(devicePollTimer);
          devicePollTimer = null;
          badge.className = "status-badge status-success";
          badge.innerText = "✅ 登录成功！";
          showToast("🎉 登录成功，欢迎使用光鸭网盘！");
          setTimeout(() => {
            closeLoginModal();
            initUser();
            loadFiles();
            loadCloudTasks();
          }, 800);
        } else if (data.status === "expired") {
          if (devicePollTimer) clearInterval(devicePollTimer);
          devicePollTimer = null;
          document.getElementById("qrExpiredMask").style.display = "flex";
          badge.className = "status-badge status-failed";
          badge.innerText = "❌ 二维码已过期";
        } else if (data.status === "denied") {
          if (devicePollTimer) clearInterval(devicePollTimer);
          devicePollTimer = null;
          badge.className = "status-badge status-failed";
          badge.innerText = "❌ 用户取消或拒绝登录";
        }
      } catch (e) {
        console.error("Poll error:", e);
      }
    }



    async function submitTokenLogin() {
      const accessToken = document.getElementById("inputAccessToken").value.trim();
      const refreshToken = document.getElementById("inputRefreshToken").value.trim();
      if (!accessToken) {
        alert("请输入 Access Token");
        return;
      }
      try {
        const res = await fetch("/api/auth/token-login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ access_token: accessToken, refresh_token: refreshToken })
        });
        const data = await res.json();
        if (data.status === "success") {
          showToast("🎉 Token 登录成功！");
          closeLoginModal();
          initUser();
          loadFiles();
          loadCloudTasks();
        } else {
          alert("登录失败: " + (data.error || "Token 无效"));
        }
      } catch (e) {
        alert("登录异常: " + e.message);
      }
    }

    function handleUserAction() {
      if (isLoggedIn) {
        confirmLogout();
      } else {
        openLoginModal();
      }
    }

    async function confirmLogout() {
      if (!confirm("确定要退出当前光鸭网盘账号吗？退出后将无法浏览和下载网盘文件。")) {
        return;
      }
      try {
        const res = await fetch("/api/auth/logout", { method: "POST" });
        if (!res.ok) {
          throw new Error("HTTP " + res.status);
        }
        showToast("已成功退出登录");
        isLoggedIn = false;
      } catch (e) {
        alert("退出失败: " + e.message);
        return;
      }
      try {
        await initUser();
      } catch (_) {}
      try {
        await loadFiles();
      } catch (_) {}
      try {
        openLoginModal();
      } catch (_) {}
    }

    // Theme management
    function initTheme() {
      const saved = localStorage.getItem("gyp_theme") || "dark";
      applyTheme(saved);
    }

    function toggleTheme() {
      const current = document.body.getAttribute("data-theme") || "dark";
      const target = current === "light" ? "dark" : "light";
      applyTheme(target);
    }

    function applyTheme(theme) {
      document.body.setAttribute("data-theme", theme);
      localStorage.setItem("gyp_theme", theme);
      const btn = document.getElementById("themeToggleBtn");
      if (btn) {
        if (theme === "light") {
          btn.innerHTML = "🌙";
          btn.title = "切换为夜间模式";
        } else {
          btn.innerHTML = "☀️";
          btn.title = "切换为日间模式";
        }
      }
      const mBtn = document.getElementById("mobileThemeBtn");
      if (mBtn) mBtn.innerHTML = (theme === "light" ? "🌙" : "☀️");
      const mcBtn = document.getElementById("mobileCardThemeBtn");
      if (mcBtn) mcBtn.innerHTML = (theme === "light" ? "🌙 夜间" : "☀️ 日间");
    }

    // Init User & Quota
    async function initUser() {
      try {
        const res = await fetch("/api/user");
        const data = await res.json();

        isLoggedIn = (data && data.is_logged_in !== false && (data.name || data.nickname));

        const actionBtn = document.getElementById("userActionBtn");
        const mLogoutBtn = document.getElementById("mobileLogoutBtn");
        const mLoginBtn = document.getElementById("mobileLoginBtn");
        const sLoginBtn = document.getElementById("settingLoginBtn");
        const sLogoutBtn = document.getElementById("settingLogoutBtn");
        const sDesc = document.getElementById("settingAccountDesc");

        if (isLoggedIn) {
          const name = data.name || data.nickname;
          document.getElementById("userNameText").innerText = name;
          document.getElementById("userAvatarText").innerText = name.substring(0, 1).toUpperCase();
          if (actionBtn) {
            actionBtn.innerText = "🚪 退出";
            actionBtn.className = "btn-outline btn-sm";
          }

          const mName = document.getElementById("mobileUserName");
          if (mName) mName.innerText = name;
          const mAvatar = document.getElementById("mobileUserAvatar");
          if (mAvatar) mAvatar.innerText = name.substring(0, 1).toUpperCase();
          if (mLogoutBtn) mLogoutBtn.style.display = "inline-flex";
          if (mLoginBtn) mLoginBtn.style.display = "none";
          if (sDesc) sDesc.innerText = `当前已登录账号【${name}】。可随时切换或退出登录。`;
          if (sLogoutBtn) sLogoutBtn.style.display = "inline-flex";

          if (data.assets) {
            const used = data.assets.usedSpaceSize || 0;
            const total = data.assets.totalSpaceSize || 0;
            const usedStr = formatBytes(used);
            const totalStr = formatBytes(total);
            document.getElementById("quotaText").innerText = `${usedStr} / ${totalStr}`;
            const pct = total > 0 ? Math.min(100, Math.max(0.5, (used / total) * 100)) : 0;
            document.getElementById("quotaFillBar").style.width = pct.toFixed(1) + "%";

            const mQuotaText = document.getElementById("mobileQuotaText");
            if (mQuotaText) mQuotaText.innerText = `${usedStr} / ${totalStr}`;
            const mQuotaFill = document.getElementById("mobileQuotaFillBar");
            if (mQuotaFill) mQuotaFill.style.width = pct.toFixed(1) + "%";

            let expireText = "普通用户";
            if (data.assets.vipStatus === 2) {
              expireText = "会员已生效";
              if (data.assets.vipExpireTime) {
                const expDate = new Date(data.assets.vipExpireTime * 1000);
                expireText = `VIP (${expDate.getFullYear()}-${String(expDate.getMonth()+1).padStart(2, '0')}-${String(expDate.getDate()).padStart(2, '0')})`;
              }
            }
            document.getElementById("userVipText").innerText = expireText;
            const mVip = document.getElementById("mobileUserVip");
            if (mVip) mVip.innerText = expireText;
          }
        } else {
          document.getElementById("userNameText").innerText = "未登录";
          document.getElementById("userAvatarText").innerText = "👤";
          document.getElementById("userVipText").innerText = "请登录后使用";
          document.getElementById("quotaText").innerText = "-- / --";
          document.getElementById("quotaFillBar").style.width = "0%";
          if (actionBtn) {
            actionBtn.innerText = "🔑 登录";
            actionBtn.className = "btn-primary btn-sm";
          }

          const mName = document.getElementById("mobileUserName");
          if (mName) mName.innerText = "未登录";
          const mAvatar = document.getElementById("mobileUserAvatar");
          if (mAvatar) mAvatar.innerText = "👤";
          const mVip = document.getElementById("mobileUserVip");
          if (mVip) mVip.innerText = "请登录后使用";
          const mQuotaText = document.getElementById("mobileQuotaText");
          if (mQuotaText) mQuotaText.innerText = "-- / --";
          const mQuotaFill = document.getElementById("mobileQuotaFillBar");
          if (mQuotaFill) mQuotaFill.style.width = "0%";
          if (mLogoutBtn) mLogoutBtn.style.display = "none";
          if (mLoginBtn) mLoginBtn.style.display = "inline-flex";
          if (sDesc) sDesc.innerText = "当前未登录账号。请点击右侧按钮扫码登录。";
          if (sLogoutBtn) sLogoutBtn.style.display = "none";
        }
      } catch (e) {
        console.error(e);
      }
    }

    // ================= VIEW: LOGS =================
    let activeLogCategory = 'all';
    let activeLogLevel = 'ALL';
    let logSearchKeyword = '';
    let logAutoScroll = true;
    let allLoadedLogs = [];
    let isLogsLoading = false;

    function filterLogsByCategory(cat) {
      activeLogCategory = cat;
      document.querySelectorAll("#view-logs .subtab-btn").forEach(b => b.classList.remove("active"));
      document.getElementById(`logSubtab-${cat}`)?.classList.add("active");
      renderLogs();
    }

    function onLogLevelChanged() {
      const sel = document.getElementById("logLevelSelect");
      if (sel) activeLogLevel = sel.value;
      renderLogs();
    }

    function onLogSearchInput() {
      const input = document.getElementById("logSearchInput");
      if (input) logSearchKeyword = input.value.trim().toLowerCase();
      renderLogs();
    }

    function toggleLogAutoScroll() {
      const chk = document.getElementById("logAutoScrollCheck");
      if (chk) logAutoScroll = chk.checked;
    }

    async function loadLogs(forceScroll = false) {
      if (isLogsLoading) return;
      isLogsLoading = true;
      try {
        const res = await fetch("/api/logs?category=all&level=ALL&limit=600");
        const data = await res.json();
        allLoadedLogs = data.logs || [];

        // Update category counts
        const cats = data.categories || {};
        const setBadge = (id, val) => {
          const el = document.getElementById(id);
          if (el) el.innerText = val !== undefined ? val : 0;
        };
        setBadge("logCount-all", cats.all || allLoadedLogs.length);
        setBadge("logCount-download", cats.download || 0);
        setBadge("logCount-cloud", cats.cloud || 0);
        setBadge("logCount-strm", cats.strm || 0);
        setBadge("logCount-system", cats.system || 0);

        renderLogs(forceScroll);
      } catch (e) {
        console.error("Failed to load logs:", e);
      } finally {
        isLogsLoading = false;
      }
    }

    function renderLogs(forceScroll = false) {
      const listEl = document.getElementById("logConsoleList");
      const emptyEl = document.getElementById("logEmptyState");
      const container = document.getElementById("logConsoleContainer");
      if (!listEl || !container) return;

      let filtered = allLoadedLogs;

      // Filter category
      if (activeLogCategory !== 'all') {
        filtered = filtered.filter(l => (l.category || '').toLowerCase() === activeLogCategory.toLowerCase());
      }

      // Filter level
      if (activeLogLevel !== 'ALL') {
        filtered = filtered.filter(l => (l.level || '').toUpperCase() === activeLogLevel);
      }

      // Filter keyword
      if (logSearchKeyword) {
        filtered = filtered.filter(l => {
          const m = (l.message || '').toLowerCase();
          const d = (l.details || '').toLowerCase();
          return m.includes(logSearchKeyword) || d.includes(logSearchKeyword);
        });
      }

      if (filtered.length === 0) {
        listEl.innerHTML = "";
        if (emptyEl) emptyEl.style.display = "block";
        return;
      }

      if (emptyEl) emptyEl.style.display = "none";

      const catLabelMap = {
        download: "下载",
        cloud: "云端",
        strm: "STRM",
        system: "系统"
      };

      const html = filtered.map(item => {
        const catClass = `log-cat-${(item.category || 'system').toLowerCase()}`;
        const catLabel = catLabelMap[(item.category || 'system').toLowerCase()] || item.category;
        const lvlClass = `log-level-${(item.level || 'info').toLowerCase()}`;
        const detailsHtml = item.details ? `<div class="log-entry-details">${escapeHtml(item.details)}</div>` : '';
        return `
          <div class="log-entry" id="log-item-${item.id}">
            <span class="log-entry-time">${item.time}</span>
            <span class="log-entry-cat ${catClass}">${catLabel}</span>
            <span class="log-entry-level ${lvlClass}">${item.level}</span>
            <div class="log-entry-msg">
              <div>${escapeHtml(item.message)}</div>
              ${detailsHtml}
            </div>
          </div>
        `;
      }).join("");

      listEl.innerHTML = html;

      if (forceScroll || logAutoScroll) {
        container.scrollTop = container.scrollHeight;
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    async function confirmClearLogs() {
      if (!confirm("确定要清空当前的系统运行日志吗？")) return;
      try {
        const res = await fetch("/api/logs/clear", { method: "POST" });
        const d = await res.json();
        if (d.success) {
          showToast("已清空系统运行日志");
          loadLogs(true);
        }
      } catch (e) {
        alert("清空日志失败: " + e.message);
      }
    }

    function exportLogs() {
      window.open("/api/logs/export", "_blank");
    }

    // Initial load
    initTheme();
    initUser();
    loadFiles();
    loadSettings();
    loadCloudTasks();
    loadStrmView(true);
    loadLogs(true);

    // Polling timer for active downloads & STRM sync tasks
    setInterval(() => {
      if (currentView === 'transfers') {
        loadTransfers();
      } else {
        // background poll badge
        fetch("/api/transfers").then(r => r.json()).then(tasks => {
          localDownloadsMap = tasks;
          const activeList = Object.values(tasks).filter(t => t.status === "downloading" || t.status === "pending" || t.status === "getting_url");
          const badge = document.getElementById("activeDlBadge");
          const mDlBadge = document.getElementById("mobileActiveDlBadge");
          if (activeList.length > 0) {
            badge.style.display = "inline-block";
            badge.innerText = activeList.length;
            if (mDlBadge) { mDlBadge.style.display = "inline-block"; mDlBadge.innerText = activeList.length; }
          } else {
            badge.style.display = "none";
            if (mDlBadge) mDlBadge.style.display = "none";
          }
        }).catch(()=>{});
      }

      if (currentView === 'strm' || strmState.running) {
        loadStrmView(false);
      }

      if (currentView === 'logs') {
        loadLogs(false);
      }
    }, 1500);

    setInterval(() => {
      if (currentView === 'offline') {
        loadCloudTasks();
      }
    }, 5000);
  
  </script>
</body>
</html>"""



class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app: GuangyapanApp = None

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_str, code=200):
        body = html_str.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _handle_strm_stream(self, path: str, query: dict, is_head: bool = False):
        """Handle STRM playback requests via 302 redirect (default) or HTTP range proxy."""
        file_id = ""
        if path.startswith("/strm/"):
            parts = path.split("/")
            if len(parts) > 2:
                file_id = urllib.parse.unquote(parts[2]).strip()
        if not file_id:
            file_id = (query.get("fileId", [""])[0] or "").strip()

        if not file_id:
            self.send_error(400, "Missing fileId for STRM stream")
            return

        use_proxy = (query.get("proxy", ["0"])[0] in ("1", "true")) or bool(self.app.strm_config.get("use_proxy", False))
        client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "unknown"
        try:
            signed_url = self.app.get_strm_redirect_url(file_id)
            self.app.log("INFO", "strm", f"🎬 STRM 媒体播放请求: FileID={file_id}", f"来源IP: {client_ip} | 模式: {'Proxy代理' if use_proxy else '302重定向'}")
        except Exception as e:
            self.app.log("ERROR", "strm", f"❌ STRM 获取直链失败: FileID={file_id}", f"错误: {e}")
            self.send_error(502, f"Failed to resolve Guangyapan signed URL: {e}")
            return

        if not use_proxy:
            self.send_response(302)
            self.send_header("Location", signed_url)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # Optional proxy streaming mode with Range support
        req_headers = {"User-Agent": self.app.client.USER_AGENT}
        if "Range" in self.headers:
            req_headers["Range"] = self.headers["Range"]
        try:
            req = urllib.request.Request(signed_url, headers=req_headers, method="HEAD" if is_head else "GET")
            with urllib.request.urlopen(req, timeout=15) as up_resp:
                self.send_response(up_resp.status)
                for h_name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                    val = up_resp.headers.get(h_name)
                    if val:
                        self.send_header(h_name, val)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if not is_head:
                    while True:
                        chunk = up_resp.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except urllib.error.HTTPError as he:
            self.send_response(he.code)
            self.end_headers()
        except Exception as e:
            try:
                self.send_error(502, f"STRM Proxy Error: {e}")
            except Exception:
                pass

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
        elif path.startswith("/strm/") or path == "/api/strm/stream":
            self._handle_strm_stream(path, query, is_head=True)
        else:
            self.send_response(200)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_html(HTML_PAGE)
            return

        elif path == "/qrcode.min.js":
            candidates = [
                os.path.join(os.path.dirname(__file__), "qrcode.min.js"),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "qrcode.min.js"),
                "/var/apps/guangyapan/var/qrcode.min.js",
                "/var/apps/guangyapan/target/qrcode.min.js"
            ] + glob.glob("/vol*/@appdata/guangyapan/qrcode.min.js") + glob.glob("/vol*/@appcenter/guangyapan/qrcode.min.js")
            for cand in candidates:
                if os.path.exists(cand):
                    try:
                        with open(cand, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/javascript; charset=utf-8")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        pass
            self.send_error(404, "qrcode.min.js not found")
            return

        elif path in ("/empty.png", "/brand_guangya_txt.svg", "/brand_logo_text.png"):
            filename = os.path.basename(path)
            candidates = [
                os.path.join(os.path.dirname(__file__), filename),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), filename),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "extracted_official", filename),
                f"/var/apps/guangyapan/{filename}",
                f"/var/apps/guangyapan/var/{filename}",
                f"/var/apps/guangyapan/target/{filename}",
            ] + glob.glob(f"/vol*/@appdata/guangyapan/{filename}")
            for cand in candidates:
                if os.path.exists(cand):
                    try:
                        with open(cand, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        ct = "image/svg+xml" if cand.endswith(".svg") else "image/png"
                        self.send_header("Content-Type", ct)
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        pass
            self.send_error(404, f"{filename} Not Found")
            return

        elif path in ("/icon.png", "/icon-256.png"):
            candidates = [
                os.path.join(os.path.dirname(__file__), "icon-256.png"),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon-256.png"),
                os.path.join(os.path.dirname(__file__), "icon.png"),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.png"),
                "/var/apps/guangyapan/ICON_256.PNG",
                "/var/apps/guangyapan/icon.png",
                "/var/apps/guangyapan/var/icon.png"
            ] + glob.glob("/vol*/@appdata/guangyapan/icon.png") + glob.glob("/vol*/@appcenter/guangyapan/ui/images/icon_256.png")
            for cand in candidates:
                if os.path.exists(cand):
                    try:
                        with open(cand, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        pass
            self.send_error(404, "Icon Not Found")
            return

        elif path == "/favicon.ico":
            candidates = [
                os.path.join(os.path.dirname(__file__), "favicon.ico"),
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "favicon.ico"),
                "/var/apps/guangyapan/var/favicon.ico",
                os.path.join(os.path.dirname(__file__), "icon-32.png"),
                os.path.join(os.path.dirname(__file__), "icon.png")
            ] + glob.glob("/vol*/@appdata/guangyapan/favicon.ico")
            for cand in candidates:
                if os.path.exists(cand):
                    try:
                        with open(cand, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/x-icon" if cand.endswith(".ico") else "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except Exception:
                        pass
            self.send_error(404, "Favicon Not Found")
            return

        elif path == "/api/user":
            try:
                user = self.app.client.get_user_profile()
                try:
                    assets_res = self.app.client.get_user_assets()
                    if assets_res and assets_res.get("code") == 0:
                        user["assets"] = assets_res.get("data", {})
                except Exception as ex:
                    print("Error getting user assets:", ex)
                self._send_json(user)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/files":
            parent_id = query.get("parentId", [""])[0]
            try:
                items = self.app.client.list_all_files(parent_id=parent_id)
                self._send_json({"list": items, "total": len(items)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/download-url":
            file_id = query.get("fileId", [""])[0]
            try:
                res = self.app.client.get_download_url(file_id)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/tasks":
            try:
                res = self.app.client.list_cloud_tasks(page=0, page_size=100)
                try:
                    self.app.check_offline_tasks_for_strm(res.get("list") or [])
                except Exception:
                    pass
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith("/strm/") or path == "/api/strm/stream":
            self._handle_strm_stream(path, query, is_head=False)
            return

        elif path in ("/api/strm/config", "/api/strm/status"):
            self._send_json(self.app.get_strm_status_payload())
            return

        elif path == "/api/transfers":
            self._send_json(self.app.downloads)

        elif path == "/api/settings":
            self._send_json({
                "download_dir": self.app.download_dir,
                "max_concurrent_downloads": self.app.max_concurrent_downloads,
                "file_page_size": self.app.file_page_size,
                "use_aria2": self.app.downloader.use_aria2,
                "version": APP_VERSION,
                "github_url": GITHUB_REPO_URL
            })

        elif path == "/api/logs":
            cat = query.get("category", ["all"])[0]
            lvl = query.get("level", ["ALL"])[0]
            kw = query.get("keyword", [""])[0]
            try:
                limit = int(query.get("limit", [300])[0] or 300)
            except Exception:
                limit = 300
            try:
                since_id = int(query.get("since_id", [0])[0] or 0)
            except Exception:
                since_id = 0
            res = self.app.logger.get_logs(category=cat, level=lvl, keyword=kw, limit=limit, since_id=since_id)
            self._send_json(res)

        elif path == "/api/logs/export":
            text = self.app.logger.export_text()
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="guangyapan_logs_{time.strftime("%Y%m%d_%H%M%S")}.txt"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/version/check":
            force_refresh = query.get("force", ["0"])[0] in ("1", "true")
            now = time.time()
            if not force_refresh and _VERSION_CACHE["data"] and (now - _VERSION_CACHE["last_check"] < 300):
                self._send_json(_VERSION_CACHE["data"])
                return

            curr_v = APP_VERSION
            result = {
                "current_version": curr_v,
                "latest_version": curr_v,
                "has_update": False,
                "release_url": f"{GITHUB_REPO_URL}/releases",
                "release_title": "",
                "release_notes": "",
                "published_at": "",
                "error": None
            }
            try:
                req = urllib.request.Request(
                    "https://api.github.com/repos/lawzhougc/guangyapan-fnos/releases/latest",
                    headers={
                        "User-Agent": "Guangyapan-fnOS",
                        "Accept": "application/vnd.github.v3+json"
                    }
                )
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        tag = data.get("tag_name", "").lstrip("v")
                        result["latest_version"] = tag
                        result["release_url"] = data.get("html_url", result["release_url"])
                        result["release_title"] = data.get("name", "")
                        result["release_notes"] = data.get("body", "")
                        result["published_at"] = data.get("published_at", "")

                        def parse_version(v_str):
                            parts = []
                            for p in re.findall(r"\d+", v_str):
                                parts.append(int(p))
                            return tuple(parts)

                        if parse_version(tag) > parse_version(curr_v):
                            result["has_update"] = True
            except Exception as e:
                result["error"] = str(e)
                result["error_hint"] = "无法连接 GitHub API 或仓库为私有，请直接访问 GitHub Releases 页面查看最新发布包"

            _VERSION_CACHE["last_check"] = now
            _VERSION_CACHE["data"] = result
            self._send_json(result)

        elif path == "/api/fs/browse":
            target_path = query.get("path", [""])[0].strip()
            # If path is empty, return all user shared folders and storage volumes dynamically
            if not target_path:
                folders = []
                # 1. Collect all user shared folders across all storage volumes
                shared_folders = get_fnos_user_shares()
                folders.extend(shared_folders)

                # 2. Add raw volume mounts
                vols = get_fnos_storage_volumes()
                for vpath in vols:
                    v_name = os.path.basename(vpath)
                    folders.append({
                        "name": f"💽 存储卷根目录 ({vpath})",
                        "path": vpath,
                        "is_volume": True
                    })

                # Non-fnOS fallback (local test / container)
                if not folders:
                    home = os.path.expanduser("~").replace("\\", "/")
                    folders.append({"name": f"🏠 用户主目录 ({home})", "path": home})
                    if os.path.exists("/"):
                        folders.append({"name": "根目录 (/)", "path": "/"})

                self._send_json({"currentPath": "", "folders": folders})
                return

            if not os.path.exists(target_path):
                self._send_json({"error": "Path does not exist", "folders": []}, 400)
                return

            folders = []
            try:
                with os.scandir(target_path) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=True):
                                if entry.name.startswith((".", "@")):
                                    continue
                                disp_name = entry.name
                                if disp_name.isdigit() and int(disp_name) >= 1000:
                                    disp_name = f"用户空间 ({disp_name})"
                                folders.append({
                                    "name": disp_name,
                                    "path": entry.path.replace("\\", "/")
                                })
                        except Exception:
                            pass
                folders.sort(key=lambda x: x["name"].lower())
                self._send_json({
                    "currentPath": target_path.replace("\\", "/"),
                    "folders": folders
                })
            except PermissionError:
                self._send_json({"error": f"无权限访问该目录: {target_path}", "folders": []}, 403)
            except Exception as e:
                self._send_json({"error": str(e), "folders": []}, 500)

        elif path == "/api/fs/check-file":
            target_dir = query.get("targetDir", [""])[0].strip() or self.app.download_dir
            file_name = query.get("fileName", [""])[0].strip()
            full_path = os.path.join(target_dir, file_name)
            exists = os.path.exists(full_path)
            suggested_name = file_name
            if exists:
                base, ext = os.path.splitext(file_name)
                counter = 1
                while True:
                    candidate = f"{base} ({counter}){ext}"
                    if not os.path.exists(os.path.join(target_dir, candidate)):
                        suggested_name = candidate
                        break
                    counter += 1
            self._send_json({
                "exists": exists,
                "path": full_path,
                "suggestedName": suggested_name
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))

        if path == "/api/auth/device-code":
            res = self.app.client.create_device_authorization()
            self._send_json(res)
            return

        elif path == "/api/auth/poll-login":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            device_code = body.get("device_code", "")
            res = self.app.client.poll_device_token(device_code)
            if res.get("status") == "success":
                self.app.log("SUCCESS", "system", "🔑 用户扫码授权成功登录网盘")
            self._send_json(res)
            return

        elif path == "/api/auth/token-login":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.client.login_with_tokens(body.get("access_token", ""), body.get("refresh_token", ""))
            if res.get("code") == 0:
                self.app.log("SUCCESS", "system", "🔑 用户使用 Token 成功登录网盘")
            else:
                self.app.log("WARN", "system", "❌ 用户 Token 登录失败", f"错误: {res.get('msg')}")
            self._send_json(res)
            return

        elif path == "/api/auth/logout":
            self.app.client.logout()
            self.app.log("INFO", "system", "🚪 用户退出网盘账号登录")
            self._send_json({"code": 0, "msg": "已退出登录"})
            return

        elif path == "/api/logs/clear":
            self.app.logger.clear()
            self._send_json({"success": True})
            return

        elif path == "/api/settings":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if "download_dir" in body and body["download_dir"]:
                self.app.set_download_dir(body["download_dir"])
            if "max_concurrent_downloads" in body:
                self.app.set_max_concurrent_downloads(body["max_concurrent_downloads"])
            if "file_page_size" in body:
                self.app.set_file_page_size(body["file_page_size"])
            self._send_json({
                "success": True,
                "download_dir": self.app.download_dir,
                "max_concurrent_downloads": self.app.max_concurrent_downloads,
                "file_page_size": self.app.file_page_size
            })

        elif path == "/api/strm/config":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            cfg = self.app.save_strm_config(body)
            self._send_json({"success": True, "config": cfg})

        elif path == "/api/strm/sync":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.start_strm_sync_task(
                cloud_parent_id=body.get("cloudParentId"),
                cloud_folder_path=body.get("cloudFolderPath"),
                output_dir=body.get("outputDir"),
                single_file=body.get("singleFile"),
                keep_root_folder=bool(body.get("keepRootFolder", False)),
                download_subtitles=body.get("downloadSubtitles"),
                reason=body.get("reason") or "手动触发"
            )
            self._send_json(res)

        elif path == "/api/strm/cancel":
            self.app.cancel_strm_sync()
            self._send_json({"success": True})

        elif path == "/api/strm/clear-cache":
            cleared = self.app.clear_strm_cache()
            self._send_json({"success": True, "cleared": cleared})

        elif path == "/api/fs/mkdir":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            new_path = body.get("path", "").strip()
            if not new_path:
                self._send_json({"error": "Empty path"}, 400)
                return
            try:
                os.makedirs(new_path, exist_ok=True)
                fix_permissions(new_path)
                self._send_json({"success": True, "path": new_path})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/download-task":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = self.app.start_download_task(
                body["fileId"],
                body["fileName"],
                body.get("targetDir"),
                file_size=int(body.get("fileSize") or 0)
            )
            self._send_json({"taskId": task_id})

        elif path == "/api/batch-download-task":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.start_batch_download_tasks(
                items=body.get("items") or [],
                target_dir=body.get("targetDir"),
                conflict_policy=body.get("conflictPolicy") or "overwrite"
            )
            self._send_json(res)

        elif path == "/api/transfers/cancel":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self.app.cancel_download_task(body.get("taskId", ""))
            self._send_json({"success": True})

        elif path == "/api/transfers/cancel-all":
            count = self.app.cancel_all_downloads()
            self._send_json({"success": True, "cancelled": count})

        elif path == "/api/transfers/retry":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            ok = self.app.retry_download_task(body.get("taskId", ""))
            self._send_json({"success": ok})

        elif path == "/api/transfers/delete":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.delete_download_task(
                body.get("taskId", ""),
                delete_file=bool(body.get("deleteFile", False))
            )
            self._send_json({"success": True, **res})

        elif path == "/api/transfers/clear-completed":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.clear_completed_tasks(
                delete_file=bool(body.get("deleteFile", False))
            )
            self._send_json({"success": True, **res})

        elif path == "/api/cloud-add/resolve":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" in content_type:
                # Torrent file upload
                raw_data = self.rfile.read(content_length)
                boundary = content_type.split("boundary=")[1].encode("utf-8")
                parts = raw_data.split(b"--" + boundary)
                torrent_bytes = None
                filename = "task.torrent"
                for p in parts:
                    if b'name="torrent"' in p:
                        header_end = p.find(b"\r\n\r\n")
                        if header_end != -1:
                            torrent_bytes = p[header_end + 4 : -2]
                            if b'filename="' in p:
                                try:
                                    fn_part = p.split(b'filename="')[1].split(b'"')[0]
                                    filename = fn_part.decode("utf-8", errors="ignore")
                                except Exception:
                                    pass
                if not torrent_bytes:
                    self._send_json({"error": "未找到 .torrent 种子文件"}, 400)
                    return
                try:
                    resolved = self.app.client.resolve_torrent(torrent_bytes, filename=filename)
                    bt_info = resolved.get("btResInfo", {})
                    info_hash = bt_info.get("infoHash", "")
                    magnet_url = f"magnet:?xt=urn:btih:{info_hash}" if info_hash else ""
                    subfiles = bt_info.get("subfiles", [])
                    exclude_indices = set(bt_info.get("excludeIndices", []))

                    files = []
                    for sf in subfiles:
                        idx = sf.get("fileIndex", 0)
                        size = sf.get("fileSize", 0)
                        files.append({
                            "fileIndex": idx,
                            "fileName": sf.get("fileName") or sf.get("path") or f"file_{idx}",
                            "fileSize": size,
                            "formattedSize": format_size(size),
                            "selected": (idx not in exclude_indices) if subfiles else True
                        })

                    title = bt_info.get("fileName") or bt_info.get("title") or filename
                    total_size = bt_info.get("fileSize", sum(f["fileSize"] for f in files))

                    self._send_json({
                        "success": True,
                        "type": "bt",
                        "title": title,
                        "totalSize": total_size,
                        "formattedTotalSize": format_size(total_size),
                        "url": magnet_url,
                        "files": files
                    })
                except Exception as e:
                    self._send_json({"error": f"解析种子失败: {str(e)}"}, 500)
            else:
                # JSON with url (magnet or http/https)
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                url = (body.get("url") or "").strip()
                if not url:
                    self._send_json({"error": "URL 不能为空"}, 400)
                    return
                try:
                    resolved = self.app.client.resolve_res(url)
                    bt_info = resolved.get("btResInfo")
                    url_info = resolved.get("urlResInfo")

                    if bt_info:
                        subfiles = bt_info.get("subfiles", [])
                        exclude_indices = set(bt_info.get("excludeIndices", []))
                        files = []
                        for sf in subfiles:
                            idx = sf.get("fileIndex", 0)
                            size = sf.get("fileSize", 0)
                            files.append({
                                "fileIndex": idx,
                                "fileName": sf.get("fileName") or sf.get("path") or f"file_{idx}",
                                "fileSize": size,
                                "formattedSize": format_size(size),
                                "selected": (idx not in exclude_indices) if subfiles else True
                            })
                        title = bt_info.get("fileName") or bt_info.get("title") or "离线下载任务"
                        total_size = bt_info.get("fileSize", sum(f["fileSize"] for f in files))
                        self._send_json({
                            "success": True,
                            "type": "bt",
                            "title": title,
                            "totalSize": total_size,
                            "formattedTotalSize": format_size(total_size),
                            "url": url,
                            "files": files
                        })
                    elif url_info:
                        title = url_info.get("fileName") or url.split("/")[-1] or "直链下载"
                        file_size = url_info.get("fileSize", 0)
                        self._send_json({
                            "success": True,
                            "type": "url",
                            "title": title,
                            "totalSize": file_size,
                            "formattedTotalSize": format_size(file_size),
                            "url": url,
                            "files": [{
                                "fileIndex": 0,
                                "fileName": title,
                                "fileSize": file_size,
                                "formattedSize": format_size(file_size),
                                "selected": True
                            }]
                        })
                    else:
                        self._send_json({
                            "success": True,
                            "type": "generic",
                            "title": url.split("/")[-1] or "离线下载",
                            "totalSize": 0,
                            "formattedTotalSize": "未知大小",
                            "url": url,
                            "files": []
                        })
                except Exception as e:
                    self._send_json({"error": f"解析资源失败: {str(e)}"}, 500)

        elif path == "/api/cloud-add/magnet":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            magnet = body.get("url")
            parent_id = body.get("parentId", "")
            file_indexes = body.get("fileIndexes")
            new_name = body.get("newName")
            try:
                res = self.app.client.cloud_add_magnet(
                    magnet,
                    parent_id=parent_id,
                    file_indexes=file_indexes,
                    new_name=new_name
                )
                self.app.log("SUCCESS", "cloud", f"⚡ 提交磁力离线任务成功: {new_name or '磁力链接'}", f"链接: {(magnet or '')[:60]}...")
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "cloud", f"❌ 提交磁力离线任务失败: {new_name or '磁力链接'}", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/torrent":
            content_type = self.headers.get("Content-Type", "")
            raw_data = self.rfile.read(content_length)
            boundary = content_type.split("boundary=")[1].encode("utf-8")
            parts = raw_data.split(b"--" + boundary)

            torrent_bytes = None
            filename = "task.torrent"
            parent_id = ""
            file_indexes = None
            new_name = None

            for p in parts:
                if b'name="torrent"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        torrent_bytes = p[header_end + 4 : -2]
                        if b'filename="' in p:
                            try:
                                fn_part = p.split(b'filename="')[1].split(b'"')[0]
                                filename = fn_part.decode("utf-8", errors="ignore")
                            except Exception:
                                pass
                elif b'name="parentId"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        parent_id = p[header_end + 4 : -2].decode("utf-8")
                elif b'name="fileIndexes"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        try:
                            file_indexes = json.loads(p[header_end + 4 : -2].decode("utf-8"))
                        except Exception:
                            pass
                elif b'name="newName"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        new_name = p[header_end + 4 : -2].decode("utf-8").strip()

            if not torrent_bytes:
                self._send_json({"error": "No torrent file found"}, 400)
                return

            try:
                res = self.app.client.cloud_add_torrent(
                    torrent_bytes,
                    filename=filename,
                    parent_id=parent_id,
                    file_indexes=file_indexes,
                    new_name=new_name
                )
                self.app.log("SUCCESS", "cloud", f"⚡ 提交种子离线任务成功: {new_name or filename}")
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "cloud", f"❌ 提交种子离线任务失败: {new_name or filename}", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/delete":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            delete_file = body.get("deleteFile", False)
            try:
                res = self.app.client.delete_cloud_task(task_id, delete_file=delete_file)
                self.app.log("INFO", "cloud", f"🗑️ 移除云端离线任务 (ID: {task_id})" + (" (同步删除网盘文件)" if delete_file else ""))
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "cloud", f"❌ 移除云端离线任务失败 (ID: {task_id})", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/retry":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            try:
                res = self.app.client.retry_cloud_task(task_id)
                self.app.log("INFO", "cloud", f"🔄 重试云端离线任务 (ID: {task_id})")
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "cloud", f"❌ 重试云端离线任务失败 (ID: {task_id})", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/pause":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            try:
                res = self.app.client.pause_cloud_task(task_id)
                self.app.log("INFO", "cloud", f"⏸️ 暂停云端离线任务 (ID: {task_id})")
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "cloud", f"❌ 暂停云端离线任务失败 (ID: {task_id})", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/create-folder":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            try:
                res = self.app.client.create_folder(body["name"], body.get("parentId", ""))
                self.app.log("INFO", "system", f"📁 创建网盘新文件夹: {body.get('name', '')}")
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/delete-file":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            try:
                res = self.app.client.delete_file(body["fileId"])
                if res.get("code") != 0 and res.get("code") is not None:
                    self._send_json({"error": res.get("msg") or f"删除失败 (错误码: {res.get('code')})"}, 400)
                else:
                    self.app.log("INFO", "system", f"🗑️ 网盘文件移入回收站 (ID: {body.get('fileId', '')})")
                    self._send_json({"status": "success", "msg": "删除成功", **res})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/upload":
            content_type = self.headers.get("Content-Type", "")
            raw_data = self.rfile.read(content_length)
            boundary = content_type.split("boundary=")[1].encode("utf-8")
            parts = raw_data.split(b"--" + boundary)

            file_bytes = None
            filename = "upload.tmp"
            parent_id = ""

            for p in parts:
                if b'name="file"' in p:
                    if b'filename="' in p:
                        f_start = p.find(b'filename="') + 10
                        f_end = p.find(b'"', f_start)
                        filename = p[f_start:f_end].decode("utf-8", errors="ignore")
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        file_bytes = p[header_end + 4 : -2]
                elif b'name="parentId"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        parent_id = p[header_end + 4 : -2].decode("utf-8")

            if not file_bytes:
                self._send_json({"error": "No file received"}, 400)
                return

            temp_path = os.path.join(self.app.download_dir, ".upload_tmp_" + str(int(time.time())))
            try:
                os.makedirs(self.app.download_dir, exist_ok=True)
                with open(temp_path, "wb") as f:
                    f.write(file_bytes)
                res = self.app.client.upload_file(temp_path, parent_id=parent_id, custom_name=filename)
                self.app.log("SUCCESS", "system", f"⬆️ 成功上传本地文件至网盘: {filename}")
                self._send_json(res)
            except Exception as e:
                self.app.log("ERROR", "system", f"❌ 上传文件至网盘失败: {filename}", f"错误: {e}")
                self._send_json({"error": str(e)}, 500)
            finally:
                if os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except Exception: pass
        else:
            self.send_response(404)
            self.end_headers()

def run_server(port=8998, config_path="config.json"):
    app = GuangyapanApp(config_path)
    RequestHandler.app = app
    server = ThreadingHTTPServer(("0.0.0.0", port), RequestHandler)
    print("=====================================================")
    print(f"🦆 光鸭网盘 fnOS 客户端已启动!")
    print(f"🌐 Web UI 访问地址: http://0.0.0.0:{port}")
    print(f"📁 默认下载保存路径: {app.download_dir}")
    print("=====================================================")
    server.serve_forever()
