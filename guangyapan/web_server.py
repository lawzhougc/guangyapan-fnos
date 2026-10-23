import os
import json
import time
import re
import glob
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from .client import GuangyapanClient
from .downloader import FileDownloader, fix_permissions, format_size, format_speed

APP_VERSION = "1.1.1"
GITHUB_REPO_URL = "https://github.com/lawzhougc/guangyapan-fnos"
_VERSION_CACHE = {"last_check": 0, "data": None}

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

class GuangyapanApp:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.client = GuangyapanClient(config_path)
        self.downloader = FileDownloader(use_aria2=True)
        # task_id -> task dict
        self.downloads = {}
        # Default download dir dynamically resolved
        saved_dir = self.client.config.get("download_dir", "")
        self.download_dir = detect_default_download_dir(saved_dir)
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            fix_permissions(self.download_dir)
        except Exception:
            pass

    def set_download_dir(self, new_dir: str):
        self.download_dir = new_dir.rstrip("/\\")
        self.client.config["download_dir"] = self.download_dir
        self.client._save_config()
        try:
            os.makedirs(self.download_dir, exist_ok=True)
            fix_permissions(self.download_dir)
        except Exception:
            pass

    def start_download_task(self, file_id: str, file_name: str, target_dir: str = None):
        target_dir = target_dir or self.download_dir
        try:
            os.makedirs(target_dir, exist_ok=True)
            fix_permissions(target_dir)
        except Exception:
            pass

        out_path = os.path.join(target_dir, file_name)
        task_id = str(int(time.time() * 1000))

        self.downloads[task_id] = {
            "taskId": task_id,
            "fileId": file_id,
            "fileName": file_name,
            "status": "pending",
            "percent": 0,
            "speed": "0 KB/s",
            "downloaded": "--",
            "total": "--",
            "eta": "--",
            "path": out_path,
            "targetDir": target_dir,
            "startTime": time.time(),
            "finishTime": None,
            "error": None
        }

        def worker():
            try:
                self.downloads[task_id]["status"] = "getting_url"
                dl_info = self.client.get_download_url(file_id)
                signed_url = dl_info.get("signedURL")
                if not signed_url:
                    raise RuntimeError("未能获取到下载直链 (请检查网盘账号或文件是否存在)")

                self.downloads[task_id]["status"] = "downloading"

                def progress(p):
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
                if ok:
                    self.downloads[task_id]["status"] = "completed"
                    self.downloads[task_id]["percent"] = 100
                    self.downloads[task_id]["speed"] = "完成"
                    self.downloads[task_id]["eta"] = "0s"
                    self.downloads[task_id]["finishTime"] = time.time()
                    fix_permissions(out_path)
                else:
                    if self.downloads[task_id]["status"] != "cancelled":
                        self.downloads[task_id]["status"] = "failed"
                        self.downloads[task_id]["error"] = "下载未正常完成"
            except Exception as e:
                self.downloads[task_id]["status"] = "failed"
                self.downloads[task_id]["error"] = str(e)

        threading.Thread(target=worker, daemon=True).start()
        return task_id

    def cancel_download_task(self, task_id: str):
        if task_id in self.downloads:
            self.downloads[task_id]["status"] = "cancelled"
            self.downloader.cancel_task(task_id)

    def delete_download_task(self, task_id: str):
        if task_id in self.downloads:
            self.downloader.cancel_task(task_id)
            del self.downloads[task_id]

    def clear_completed_tasks(self):
        to_del = [tid for tid, t in self.downloads.items() if t["status"] in ("completed", "failed", "cancelled")]
        for tid in to_del:
            del self.downloads[tid]

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
          <div class="subtabs">
            <div class="subtabs-left">
              <button class="subtab-btn active" id="tabDlActiveBtn" onclick="switchTransferTab('active')">正在下载到 NAS (<span id="countActiveDl">0</span>)</button>
              <button class="subtab-btn" id="tabDlDoneBtn" onclick="switchTransferTab('done')">已下载完成 (<span id="countDoneDl">0</span>)</button>
            </div>
            <div>
              <button class="btn-outline btn-sm" onclick="clearCompletedDownloads()">清空已完成记录</button>
            </div>
          </div>
          <div class="task-list-container" id="transfersListContainer">
            <div style="text-align: center; color: var(--text-muted); padding: 40px;">暂无下载任务</div>
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
            <!-- Setting 1: Storage Dir -->
            <div class="setting-card">
              <div class="setting-card-title">📁 NAS 落地下载目录</div>
              <div class="setting-card-desc">指定光鸭网盘文件下载至飞牛 NAS 的本地共享文件夹路径（穿透所有存储卷）。</div>
              <div style="display: flex; gap: 8px; margin-top: 4px;">
                <input type="text" class="form-input" id="settingDownloadDirInput" style="flex: 1;" placeholder="/vol1/1000/下载/光鸭网盘">
                <button class="btn-secondary" onclick="openFolderPicker(onSettingFolderPicked, document.getElementById('settingDownloadDirInput').value)">浏览...</button>
              </div>
              <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px;">
                <label style="display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text-muted); cursor: pointer;">
                  <input type="checkbox" id="setAsDefaultDirCheckbox" checked> 同时设为默认保存路径
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
              <div class="setting-card-title">ℹ️ 关于与更新检查</div>
              <div class="setting-card-desc">
                当前运行版本: <strong id="aboutCurrentVersion" style="color: var(--brand);">v1.1.1</strong> · 由 <strong>chaos</strong> 专为飞牛 fnOS 深度适配打造。
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
          <span>下载文件到 NAS</span>
        </div>
        <button class="close-btn" onclick="closeModal('downloadConfirmModal')">&times;</button>
      </div>
      <div class="modal-body" style="padding: 20px 24px; gap: 16px;">
        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;">下载文件：</div>
          <div style="background: var(--bg-input); border: 1px solid var(--border-subtle); border-radius: 10px; padding: 12px 14px; display: flex; align-items: center; gap: 10px;">
            <span style="font-size: 20px; flex-shrink: 0;">📦</span>
            <div style="font-weight: 500; font-size: 13px; line-height: 1.5; color: var(--text-main); word-break: break-all; flex: 1;" id="dlConfirmFileName">--</div>
          </div>
        </div>

        <div>
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500;">选择 NAS 保存目录：</div>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="dlConfirmPathInput" style="flex: 1; height: 38px; font-size: 13px; font-family: monospace; background: var(--bg-input); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 12px; color: var(--text-main);" readonly onclick="openFolderPicker(onDlConfirmFolderPicked)">
            <button class="btn-secondary" onclick="openFolderPicker(onDlConfirmFolderPicked)" style="height: 38px; padding: 0 16px; font-size: 13px; flex-shrink: 0;">📂 浏览目录</button>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); margin-top: 6px; line-height: 1.5;">
            💡 提示：可自由下载至 NAS 任意存储卷或共享文件夹（如 影视、下载、照片等）。
          </div>
        </div>

        <div style="display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: var(--bg-subtle); border-radius: 8px; border: 1px solid var(--border-subtle);">
          <input type="checkbox" id="setAsDefaultDirCheckbox" style="cursor: pointer; width: 16px; height: 16px; accent-color: var(--brand);">
          <label for="setAsDefaultDirCheckbox" style="font-size: 12.5px; color: var(--text-main); cursor: pointer; user-select: none;">将该目录设为以后的默认下载路径</label>
        </div>
      </div>

      <div class="modal-footer" style="padding: 14px 24px;">
        <button class="btn-secondary" onclick="closeModal('downloadConfirmModal')">取消</button>
        <button class="btn-primary" onclick="confirmStartDownload()">立即下载到 NAS</button>
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

        <div style="margin-top: 16px; width: 100%; display: flex; justify-content: center;">
          <button class="btn-primary" style="width: 100%; justify-content: center; padding: 9px 16px;" onclick="refreshDeviceCode()">🔄 刷新二维码</button>
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
      document.querySelectorAll('.cat-tab-item').forEach(el => {
        el.classList.toggle('active', el.dataset.cat === cat);
      });
      renderFileList();
    }

    function handleSearch(val) {
      searchQuery = (val || '').trim().toLowerCase();
      renderFileList();
    }

    function setViewMode(mode) {
      currentViewMode = mode;
      document.getElementById('viewModeListBtn')?.classList.toggle('active', mode === 'list');
      document.getElementById('viewModeGridBtn')?.classList.toggle('active', mode === 'grid');
      renderFileList();
    }

    function toggleSelectAllTableFiles(checked) {
      document.querySelectorAll('.table-file-cb').forEach(cb => cb.checked = checked);
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
          filtered.forEach(item => {
            const isDir = (item.resType === 2);
            const isVid = isVideoFile(item.fileName);
            const icon = isDir ? "📁" : getFileIcon(item.fileName);
            const card = document.createElement('div');
            card.style.cssText = 'background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; text-align: center; transition: all 0.2s; position: relative;';
            card.onmouseenter = () => card.style.borderColor = 'var(--brand)';
            card.onmouseleave = () => card.style.borderColor = 'var(--border)';
            card.onclick = () => {
              if (isDir) navigateTo(item.fileId, item.fileName);
              else promptStartDownload(item.fileId, item.fileName);
            };
            card.innerHTML = `
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
      filtered.forEach(item => {
        const tr = document.createElement("tr");
        const isDir = (item.resType === 2);
        const icon = isDir ? "📁" : getFileIcon(item.fileName);

        tr.innerHTML = `
          <td><input type="checkbox" class="table-file-cb" data-fileid="${item.fileId}"></td>
          <td>
            <div class="file-name-cell" onclick="${isDir ? `navigateTo('${item.fileId}', '${escapeHtml(item.fileName)}')` : `promptStartDownload('${item.fileId}', '${escapeHtml(item.fileName)}')`}">
              <span class="file-icon">${icon}</span>
              <span class="file-name-text" title="${escapeHtml(item.fileName)}">${escapeHtml(item.fileName)}</span>
            </div>
          </td>
          <td>${isDir ? '--' : formatBytes(item.fileSize)}</td>
          <td>${formatDate(item.utime || item.ctime)}</td>
          <td style="text-align: right; width: 220px; white-space: nowrap;">
            <div class="actions-cell">
              ${!isDir ? `<button class="btn-primary btn-sm" onclick="promptStartDownload('${item.fileId}', '${escapeHtml(item.fileName)}')">⬇️<span class="desktop-text"> 下载到NAS</span><span class="mobile-text"> 下载</span></button>` : ''}
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
      const tbody = document.getElementById("fileTableBody");
      tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 50px;">正在加载目录文件...</td></tr>`;

      try {
        const res = await fetch(`/api/files?parentId=${encodeURIComponent(parentId)}`);
        const json = await res.json();
        if (json.code === 401 || (!isLoggedIn && (!json.list || json.list.length === 0))) {
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
        c.appendChild(span);
        if (idx < breadcrumbStack.length - 1) {
          const sep = document.createElement("span");
          sep.innerText = " / ";
          sep.className = "crumb-sep";
          c.appendChild(sep);
        }
      });
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
        showToast("已成功删除");
        loadFiles();
      } catch (e) {
        alert("删除失败: " + e.message);
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

    // ================= DOWNLOAD CONFIRMATION & NAS FOLDER SELECTION (Issue 2) =================
    function promptStartDownload(fileId, fileName) {
      pendingDownloadFileId = fileId;
      pendingDownloadFileName = fileName;
      document.getElementById("dlConfirmFileName").innerText = fileName;
      document.getElementById("dlConfirmPathInput").value = defaultDownloadDir;
      openModal("downloadConfirmModal");
    }

    function onDlConfirmFolderPicked(selectedPath) {
      document.getElementById("dlConfirmPathInput").value = selectedPath;
    }

    let duplicateContext = null;

    async function confirmStartDownload() {
      const targetDir = document.getElementById("dlConfirmPathInput").value.trim() || defaultDownloadDir;
      const setAsDefault = document.getElementById("setAsDefaultDirCheckbox").checked;

      if (setAsDefault) {
        defaultDownloadDir = targetDir;
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ download_dir: targetDir })
        });
      }

      // Check if file already exists in target NAS directory
      try {
        const checkRes = await fetch(`/api/fs/check-file?targetDir=${encodeURIComponent(targetDir)}&fileName=${encodeURIComponent(pendingDownloadFileName)}`);
        const checkData = await checkRes.json();
        if (checkData.exists) {
          duplicateContext = {
            fileId: pendingDownloadFileId,
            originalFileName: pendingDownloadFileName,
            suggestedFileName: checkData.suggestedName,
            targetDir: targetDir
          };
          document.getElementById("duplicateFileNameDisplay").innerText = pendingDownloadFileName;
          document.getElementById("duplicateSuggestedNameBadge").innerText = `新文件名: ${checkData.suggestedName}`;
          closeModal("downloadConfirmModal");
          openModal("duplicateFileModal");
          return;
        }
      } catch (e) {
        console.warn("Check file existence failed:", e);
      }

      closeModal("downloadConfirmModal");
      executeDownload(pendingDownloadFileId, pendingDownloadFileName, targetDir);
    }

    function resolveDuplicateAndDownload(action) {
      if (!duplicateContext) return;
      const ctx = duplicateContext;
      closeModal("duplicateFileModal");
      if (action === "rename") {
        executeDownload(ctx.fileId, ctx.suggestedFileName, ctx.targetDir);
      } else if (action === "overwrite") {
        executeDownload(ctx.fileId, ctx.originalFileName, ctx.targetDir);
      }
    }

    async function executeDownload(fileId, fileName, targetDir) {
      showToast("正在启动后台下载...");
      try {
        const res = await fetch("/api/download-task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            fileId: fileId,
            fileName: fileName,
            targetDir: targetDir
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

      document.getElementById("countActiveDl").innerText = activeList.length;
      document.getElementById("countDoneDl").innerText = doneList.length;

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
        else if (t.status === "pending") { statusText = "排队中"; }

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
              ${isRunning ? `<button class="btn-danger btn-sm" onclick="cancelTransfer('${t.taskId}')">取消</button>` : `<button class="btn-outline btn-sm" onclick="deleteTransfer('${t.taskId}')">删除记录</button>`}
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

    async function deleteTransfer(taskId) {
      await fetch("/api/transfers/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ taskId })
      });
      loadTransfers();
    }

    async function clearCompletedDownloads() {
      await fetch("/api/transfers/clear-completed", { method: "POST" });
      showToast("已清空已完成记录");
      loadTransfers();
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
      try {
        const res = await fetch("/api/settings");
        const d = await res.json();
        if (d.download_dir) {
          defaultDownloadDir = d.download_dir;
          document.getElementById("settingDownloadDirInput").value = defaultDownloadDir;
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
      if (!path) return;
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ download_dir: path })
        });
        defaultDownloadDir = path;
        showToast("NAS 保存目录设置已保存");
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

    async function openCloudFolderPickerModal() {
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
      if (cloudAddTargetParentId) {
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
      cloudAddTargetParentId = cloudTreeSelectedId;
      cloudAddTargetFolderPath = cloudTreeSelectedPath || "全部文件";
      document.getElementById("cloudAddTargetFolderDisplay").innerText = cloudAddTargetFolderPath;
      closeModal("cloudFolderPickerModal");
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

    // Initial load
    initTheme();
    initUser();
    loadFiles();
    loadSettings();
    loadCloudTasks();

    // Polling timer for active downloads & cloud tasks
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

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
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
                res = self.app.client.list_files(parent_id=parent_id)
                self._send_json(res)
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
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/transfers":
            self._send_json(self.app.downloads)

        elif path == "/api/settings":
            self._send_json({
                "download_dir": self.app.download_dir,
                "use_aria2": self.app.downloader.use_aria2,
                "version": APP_VERSION,
                "github_url": GITHUB_REPO_URL
            })

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
            self._send_json(res)
            return

        elif path == "/api/auth/token-login":
            body = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
            res = self.app.client.login_with_tokens(body.get("access_token", ""), body.get("refresh_token", ""))
            self._send_json(res)
            return

        elif path == "/api/auth/logout":
            self.app.client.logout()
            self._send_json({"code": 0, "msg": "已退出登录"})
            return

        elif path == "/api/settings":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if "download_dir" in body:
                self.app.set_download_dir(body["download_dir"])
            self._send_json({"success": True, "download_dir": self.app.download_dir})

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
            task_id = self.app.start_download_task(body["fileId"], body["fileName"], body.get("targetDir"))
            self._send_json({"taskId": task_id})

        elif path == "/api/transfers/cancel":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self.app.cancel_download_task(body.get("taskId", ""))
            self._send_json({"success": True})

        elif path == "/api/transfers/delete":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            self.app.delete_download_task(body.get("taskId", ""))
            self._send_json({"success": True})

        elif path == "/api/transfers/clear-completed":
            self.app.clear_completed_tasks()
            self._send_json({"success": True})

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
                self._send_json(res)
            except Exception as e:
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
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/delete":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            delete_file = body.get("deleteFile", False)
            try:
                res = self.app.client.delete_cloud_task(task_id, delete_file=delete_file)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/retry":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            try:
                res = self.app.client.retry_cloud_task(task_id)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/cloud-add/pause":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            task_id = body.get("taskId")
            try:
                res = self.app.client.pause_cloud_task(task_id)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/create-folder":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            try:
                res = self.app.client.create_folder(body["name"], body.get("parentId", ""))
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
                self._send_json(res)
            except Exception as e:
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
