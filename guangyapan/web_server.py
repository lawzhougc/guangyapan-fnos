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

APP_VERSION = "1.0.4"
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
                                            "name": f"📁 {s_entry.name}  [{v_name}]",
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
  <title>光鸭网盘 · 飞牛系统 (fnOS) 客户端</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico?v=1.0.4">
  <link rel="icon" type="image/png" sizes="32x32" href="/icon.png?v=1.0.4">
  <link rel="apple-touch-icon" href="/icon.png?v=1.0.4">
  <script src="/qrcode.min.js"></script>
  <style>
    :root {
      --bg-body: #0b0f19;
      --bg-sidebar: #070a12;
      --bg-card: #151e2e;
      --bg-hover: #1e293b;
      --bg-subtle: #111827;
      --bg-input: #0b0f19;
      --bg-footer: #04060a;
      --border: #263349;
      --border-subtle: #1a2436;
      --primary: #f59e0b;
      --primary-hover: #d97706;
      --accent: #3b82f6;
      --accent-hover: #2563eb;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --table-th-bg: #111827;
      --table-hover: #1c273a;
      --progress-bg: #0f172a;
      --brand-icon-bg: #f59e0b20;
      --brand-badge-bg: #3b82f625;
    }
    body[data-theme="light"] {
      --bg-body: #f3f4f6;
      --bg-sidebar: #ffffff;
      --bg-card: #ffffff;
      --bg-hover: #f1f5f9;
      --bg-subtle: #f8fafc;
      --bg-input: #f8fafc;
      --bg-footer: #f8fafc;
      --border: #cbd5e1;
      --border-subtle: #e2e8f0;
      --primary: #f59e0b;
      --primary-hover: #d97706;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --text: #0f172a;
      --text-muted: #475569;
      --text-dim: #64748b;
      --table-th-bg: #f8fafc;
      --table-hover: #f1f5f9;
      --progress-bg: #e2e8f0;
      --brand-icon-bg: #fef3c7;
      --brand-badge-bg: #dbeafe;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; }
    body { background: var(--bg-body); color: var(--text); height: 100vh; overflow: hidden; display: flex; }

    /* Layout */
    #sidebar {
      width: 230px;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      user-select: none;
      flex-shrink: 0;
    }
    .brand-box {
      padding: 18px 20px;
      display: flex;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--border);
    }
    .brand-icon {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      object-fit: contain;
      flex-shrink: 0;
    }
    .brand-name {
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .brand-badge {
      font-size: 11px;
      background: #3b82f625;
      color: #60a5fa;
      border: 1px solid #3b82f640;
      padding: 1px 6px;
      border-radius: 4px;
    }

    /* Nav menu */
    .nav-list {
      flex: 1;
      padding: 16px 12px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      overflow-y: auto;
    }
    .nav-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 500;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .nav-item:hover {
      background: var(--bg-hover);
      color: var(--text);
    }
    .nav-item.active {
      background: #f59e0b18;
      color: var(--primary);
      font-weight: 600;
    }
    .nav-item-left {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .nav-badge {
      background: var(--accent);
      color: #fff;
      font-size: 11px;
      padding: 1px 7px;
      border-radius: 10px;
      font-weight: 600;
    }
    .nav-badge.orange {
      background: var(--primary);
    }

    /* Sidebar Footer / User info */
    .sidebar-footer {
      padding: 16px;
      border-top: 1px solid var(--border);
      background: var(--bg-footer);
    }
    .user-card {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }
    .user-avatar {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      background: var(--bg-hover);
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 16px;
      font-weight: 700;
      color: var(--primary);
    }
    .user-info-text {
      flex: 1;
      overflow: hidden;
    }
    .user-name {
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .user-vip {
      font-size: 11px;
      color: var(--primary);
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .quota-box {
      font-size: 11px;
      color: var(--text-dim);
    }
    .quota-bar {
      height: 4px;
      background: var(--progress-bg);
      border-radius: 2px;
      margin-top: 4px;
      overflow: hidden;
    }
    .quota-fill {
      height: 100%;
      background: var(--primary);
      width: 15%;
    }

    /* Main Area */
    #main-wrapper {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg-body);
    }
    header {
      height: 58px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-card);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      flex-shrink: 0;
    }
    .breadcrumbs {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      color: var(--text-muted);
      overflow-x: auto;
    }
    .crumb-item {
      cursor: pointer;
      color: #60a5fa;
    }
    .crumb-item:hover { text-decoration: underline; }
    .crumb-item.current { color: var(--text); cursor: default; text-decoration: none; font-weight: 600; }
    .crumb-sep { color: var(--text-dim); font-size: 12px; }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    /* Buttons */
    button {
      cursor: pointer;
      border: none;
      border-radius: 7px;
      font-weight: 500;
      font-size: 13px;
      transition: all 0.15s ease;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 14px;
    }
    .btn-primary { background: var(--primary); color: #111; font-weight: 600; }
    .btn-primary:hover { background: var(--primary-hover); }
    .btn-accent { background: var(--accent); color: #fff; }
    .btn-accent:hover { background: var(--accent-hover); }
    .btn-secondary { background: #1e293b; border: 1px solid var(--border); color: #ffffff; }
    .btn-secondary:hover { background: #28374d; color: #ffffff; }
    .btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text-muted); }
    .btn-outline:hover { color: var(--text); border-color: var(--text-muted); background: #ffffff08; }
    .btn-danger { background: #ef444418; color: #f87171; border: 1px solid #ef444435; }
    .btn-danger:hover { background: #ef444430; }
    .btn-sm { padding: 4px 10px; font-size: 12px; border-radius: 5px; }

    /* Light Theme Button Enhancements (Ensuring high readability) */
    body[data-theme="light"] .btn-secondary {
      background: #1e293b;
      border: 1px solid #334155;
      color: #ffffff !important;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
    }
    body[data-theme="light"] .btn-secondary:hover {
      background: #334155;
      color: #ffffff !important;
    }
    body[data-theme="light"] .btn-outline {
      background: #ffffff;
      border-color: #cbd5e1;
      color: #334155;
    }
    body[data-theme="light"] .btn-outline:hover {
      background: #f1f5f9;
      color: #0f172a;
      border-color: #94a3b8;
    }
    body[data-theme="light"] .btn-danger {
      background: #fee2e2;
      color: #dc2626;
      border-color: #fca5a5;
    }
    body[data-theme="light"] .btn-danger:hover {
      background: #fecaca;
      color: #b91c1c;
    }

    /* Content Views */
    .content-view {
      flex: 1;
      padding: 20px 24px;
      overflow-y: auto;
      display: none;
    }
    .content-view.active { display: block; }

    /* Tables */
    .table-container {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }
    table { width: 100%; border-collapse: collapse; text-align: left; }
    th {
      background: var(--table-th-bg);
      padding: 12px 18px;
      font-size: 13px;
      color: var(--text-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 13px 18px;
      font-size: 13.5px;
      border-bottom: 1px solid var(--border);
      vertical-align: middle;
    }
    tr:hover td { background: var(--table-hover); }
    .file-name-cell {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 500;
      cursor: pointer;
    }
    .file-icon { font-size: 20px; flex-shrink: 0; }
    .file-name-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 450px; }
    .actions-cell { display: flex; gap: 8px; }

    /* Sub-tabs inside view */
    .subtabs {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
    }
    .subtabs-left {
      display: flex;
      gap: 8px;
      background: var(--bg-subtle);
      padding: 4px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }
    .subtab-btn {
      background: transparent;
      color: var(--text-muted);
      border: none;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
    }
    .subtab-btn.active {
      background: var(--bg-hover);
      color: var(--text);
      font-weight: 600;
    }

    /* Cards / Lists */
    .task-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px 20px;
      margin-bottom: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .task-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .task-title {
      font-weight: 600;
      font-size: 14px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 650px;
    }
    .task-details {
      display: flex;
      justify-content: space-between;
      font-size: 12.5px;
      color: var(--text-muted);
    }
    .task-details-left {
      display: flex;
      gap: 16px;
      align-items: center;
    }
    .progress-bar-bg {
      width: 100%;
      height: 7px;
      background: var(--progress-bg);
      border-radius: 4px;
      overflow: hidden;
    }
    .progress-bar-fill {
      height: 100%;
      background: linear-gradient(90deg, #3b82f6, #60a5fa);
      width: 0%;
      transition: width 0.3s;
    }
    .progress-bar-fill.success {
      background: #10b981;
    }

    /* Badges */
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 9px;
      border-radius: 5px;
      font-size: 11.5px;
      font-weight: 600;
    }
    .status-success { background: #10b98120; color: #34d399; border: 1px solid #10b98135; }
    .status-active { background: #3b82f620; color: #60a5fa; border: 1px solid #3b82f635; }
    .status-failed { background: #ef444420; color: #f87171; border: 1px solid #ef444435; }

    /* Modals */
    .modal-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(5px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
    }
    .modal {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 600px;
      max-width: 92vw;
      padding: 24px;
      box-shadow: 0 25px 35px -5px rgba(0,0,0,0.6);
      max-height: 85vh;
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
      font-size: 17px;
      font-weight: 700;
    }
    .close-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 22px;
      cursor: pointer;
      line-height: 1;
    }
    .close-btn:hover { color: var(--text); }
    .modal-footer {
      margin-top: 22px;
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }

    textarea, input[type="text"] {
      width: 100%;
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      color: var(--text);
      font-size: 13.5px;
    }
    textarea:focus, input[type="text"]:focus {
      outline: none;
      border-color: var(--primary);
    }
    .dropzone {
      border: 2px dashed var(--border);
      border-radius: 10px;
      padding: 32px;
      text-align: center;
      color: var(--text-muted);
      cursor: pointer;
      transition: 0.2s;
    }
    .dropzone:hover {
      border-color: var(--primary);
      color: var(--text);
      background: #ffffff04;
    }

    /* Folder Selector Component */
    .fs-browser-list {
      background: var(--bg-input);
      border: 1px solid var(--border);
      border-radius: 8px;
      max-height: 320px;
      overflow-y: auto;
      margin-top: 12px;
    }
    .fs-folder-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      border-bottom: 1px solid var(--border-subtle);
      cursor: pointer;
      font-size: 13.5px;
    }
    .fs-folder-item:last-child { border-bottom: none; }
    .fs-folder-item:hover { background: var(--bg-hover); }
    .fs-folder-item.selected { background: #3b82f625; color: #60a5fa; font-weight: 600; }
    .fs-folder-left { display: flex; align-items: center; gap: 8px; }

    /* Settings section */
    .setting-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 20px;
      margin-bottom: 18px;
    }
    .setting-card-title {
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
      font-size: 13.5px;
    }
    .setting-desc {
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 4px;
      line-height: 1.5;
    }

    /* Toast */
    #toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 8px;
      padding: 12px 20px;
      color: #f8fafc;
      box-shadow: 0 10px 25px -3px rgba(0,0,0,0.5);
      transform: translateY(100px);
      opacity: 0;
      transition: all 0.25s ease;
      z-index: 9999;
      font-size: 13.5px;
      font-weight: 500;
      pointer-events: none;
    }
    #toast.show { transform: translateY(0); opacity: 1; }
    body[data-theme="light"] #toast {
      background: #ffffff;
      border: 1px solid #cbd5e1;
      color: #0f172a !important;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
      font-weight: 600;
    }

    /* Mobile Responsive Adaptation */
    .mobile-header-top { display: none; }
    .mobile-bottom-nav { display: none; }
    .mobile-only-usercard { display: none; }
    .mobile-text { display: none; }
    .desktop-text { display: inline; }
    .desktop-only-btn { display: inline-flex; }

    @media (max-width: 768px) {
      body {
        overflow: hidden;
        height: 100vh;
        height: 100dvh;
      }
      #sidebar {
        display: none !important;
      }
      #main-wrapper {
        width: 100% !important;
        height: 100vh !important;
        height: 100dvh !important;
      }
      
      /* Mobile Header */
      header {
        height: auto !important;
        padding: 10px 14px !important;
        flex-direction: column !important;
        align-items: stretch !important;
        gap: 8px !important;
      }
      .mobile-header-top {
        display: flex !important;
        justify-content: space-between !important;
        align-items: center !important;
      }
      .mobile-brand {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 16px;
        font-weight: 700;
        color: var(--text);
      }
      .mobile-brand-img {
        width: 26px;
        height: 26px;
        border-radius: 6px;
        object-fit: contain;
      }
      .mobile-header-tools {
        display: flex;
        align-items: center;
        gap: 6px;
      }
      .btn-icon {
        padding: 6px 10px !important;
        font-size: 13px !important;
      }
      .breadcrumbs {
        font-size: 13px;
        padding: 2px 0;
        white-space: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
      }
      .breadcrumbs::-webkit-scrollbar { display: none; }

      /* Horizontal Action Bar on Mobile */
      .header-actions {
        width: 100%;
        display: flex !important;
        gap: 6px !important;
        overflow-x: auto !important;
        white-space: nowrap !important;
        padding-bottom: 2px !important;
        -webkit-overflow-scrolling: touch;
      }
      .header-actions::-webkit-scrollbar { display: none; }
      .header-actions button {
        padding: 6px 10px !important;
        font-size: 12.5px !important;
        flex-shrink: 0 !important;
        border-radius: 6px !important;
      }

      #themeToggleBtn {
        display: none !important;
      }

      /* Content Area with Bottom Padding for Mobile Nav */
      .content-view {
        padding: 12px 12px calc(68px + env(safe-area-inset-bottom, 0px)) 12px !important;
      }

      /* Mobile Table Adjustments */
      .table-container {
        border-radius: 8px;
        overflow-x: hidden;
      }
      table th:nth-child(3), table td:nth-child(3) {
        display: none !important;
      }
      table th:nth-child(2), table td:nth-child(2) {
        width: 72px !important;
        text-align: right !important;
        font-size: 12px !important;
        padding: 10px 8px !important;
      }
      table th:first-child, table td:first-child {
        padding: 10px 10px !important;
      }
      table th:last-child, table td:last-child {
        width: 90px !important;
        padding: 10px 8px !important;
        text-align: right !important;
      }
      .file-name-text {
        max-width: 160px !important;
        font-size: 13.5px !important;
      }
      .mobile-text { display: inline !important; }
      .desktop-text { display: none !important; }
      .desktop-only-btn { display: none !important; }
      .actions-cell {
        gap: 4px !important;
        justify-content: flex-end !important;
      }
      .actions-cell button {
        padding: 5px 8px !important;
        font-size: 12px !important;
      }

      /* Subtabs */
      .subtabs {
        flex-direction: column !important;
        align-items: stretch !important;
        gap: 8px !important;
      }
      .subtabs-left {
        overflow-x: auto !important;
        white-space: nowrap !important;
        -webkit-overflow-scrolling: touch;
      }
      .subtabs-left::-webkit-scrollbar { display: none; }
      .subtab-btn {
        padding: 6px 10px !important;
        font-size: 12px !important;
      }

      /* Task Cards on Mobile */
      .task-card {
        padding: 12px 14px !important;
      }
      .task-title {
        max-width: 200px !important;
        font-size: 13.5px !important;
      }
      .task-details {
        flex-direction: column !important;
        gap: 6px !important;
      }
      .task-details-left {
        flex-wrap: wrap !important;
        gap: 8px !important;
      }

      /* User profile in settings */
      .mobile-only-usercard {
        display: block !important;
      }
      .mobile-user-header {
        display: flex;
        align-items: center;
        gap: 12px;
      }

      /* Modals on Mobile */
      .modal {
        width: 92% !important;
        max-width: 92% !important;
        margin: 15px auto !important;
        padding: 18px !important;
        max-height: 90vh !important;
      }

      /* Bottom Nav Bar */
      .mobile-bottom-nav {
        display: flex !important;
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        height: calc(56px + env(safe-area-inset-bottom, 0px));
        padding-bottom: env(safe-area-inset-bottom, 0px);
        background: var(--bg-sidebar);
        border-top: 1px solid var(--border);
        z-index: 999;
        justify-content: space-around;
        align-items: center;
        box-shadow: 0 -2px 10px rgba(0,0,0,0.2);
      }
      .mobile-nav-item {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        height: 100%;
        cursor: pointer;
        color: var(--text-muted);
        transition: color 0.15s ease;
        position: relative;
        -webkit-tap-highlight-color: transparent;
      }
      .mobile-nav-item.active {
        color: var(--primary);
      }
      .mobile-nav-icon {
        font-size: 19px;
        position: relative;
        line-height: 1;
        margin-bottom: 2px;
      }
      .mobile-nav-label {
        font-size: 11px;
        font-weight: 500;
      }
      .mobile-badge {
        position: absolute;
        top: -4px;
        right: -10px;
        background: var(--accent);
        color: #fff;
        font-size: 10px;
        font-weight: 700;
        border-radius: 9px;
        padding: 1px 5px;
        min-width: 15px;
        text-align: center;
      }
      .mobile-badge.orange {
        background: var(--primary);
      }
      #toast {
        left: 50% !important;
        right: auto !important;
        transform: translate(-50%, 100px) !important;
        bottom: calc(68px + env(safe-area-inset-bottom, 0px)) !important;
        white-space: nowrap !important;
      }
      #toast.show {
        transform: translate(-50%, 0) !important;
      }
    }

  </style>
</head>
<body>

  <!-- Left Sidebar (Windows/fnOS Style) -->
  <aside id="sidebar">
    <div class="brand-box">
      <img src="/icon.png?v=1.0.4" class="brand-icon" style="width:34px;height:34px;border-radius:8px;object-fit:cover;flex-shrink:0;" alt="光鸭网盘">
      <div>
        <div class="brand-name">光鸭网盘 <span class="brand-badge">fnOS</span></div>
        <div style="font-size: 11px; color: var(--text-dim); margin-top: 2px;">飞牛 NAS 客户端 · by chaos</div>
      </div>
    </div>

    <div class="nav-list">
      <div class="nav-item active" id="navFiles" onclick="switchView('files')">
        <div class="nav-item-left">
          <span>📁</span>
          <span>网盘文件</span>
        </div>
      </div>

      <div class="nav-item" id="navTransfers" onclick="switchView('transfers')">
        <div class="nav-item-left">
          <span>⬇️</span>
          <span>传输列表</span>
        </div>
        <span class="nav-badge" id="activeDlBadge" style="display: none;">0</span>
      </div>

      <div class="nav-item" id="navOffline" onclick="switchView('offline')">
        <div class="nav-item-left">
          <span>⚡</span>
          <span>离线下载</span>
        </div>
        <span class="nav-badge orange" id="cloudTaskBadge" style="display: none;">0</span>
      </div>

      <div class="nav-item" id="navSettings" onclick="switchView('settings')">
        <div class="nav-item-left">
          <span>⚙️</span>
          <span>系统设置</span>
        </div>
        <span class="nav-badge orange" id="settingsUpdateBadge" style="display: none;">NEW</span>
      </div>
    </div>

    <div class="sidebar-footer">
      <div class="user-card" id="userCardBox">
        <div class="user-avatar" id="userAvatarText">🦆</div>
        <div class="user-info-text">
          <div class="user-name" id="userNameText">连接中...</div>
          <div class="user-vip" id="userVipText">会员已生效</div>
        </div>
        <button class="btn-outline btn-sm" id="userActionBtn" onclick="handleUserAction()" style="padding: 2px 7px; font-size: 11px;">🚪 退出</button>
      </div>
      <div class="quota-box">
        <div style="display: flex; justify-content: space-between;">
          <span>存储空间</span>
          <span id="quotaText">-- / --</span>
        </div>
        <div class="quota-bar"><div class="quota-fill" id="quotaFillBar"></div></div>
      </div>
    </div>
  </aside>

  <!-- Main View Area -->
  <div id="main-wrapper">
    <header>
      <div class="mobile-header-top">
        <div class="mobile-brand">
          <img src="/icon.png?v=1.0.4" class="mobile-brand-img" style="width:26px;height:26px;border-radius:6px;object-fit:cover;flex-shrink:0;" alt="光鸭网盘">
          <span>光鸭网盘 <span class="brand-badge" style="font-size:10px;">by chaos</span></span>
        </div>
        <div class="mobile-header-tools">
          <button class="btn-secondary btn-icon" id="mobileThemeBtn" onclick="toggleTheme()" title="切换日夜模式">🌙</button>
          <button class="btn-outline btn-icon" onclick="refreshCurrentView()" title="刷新">🔄</button>
        </div>
      </div>
      <div class="breadcrumbs" id="breadcrumbs">
        <span class="crumb-item current">网盘文件</span>
      </div>
      <div class="header-actions">
        <button class="btn-secondary" id="themeToggleBtn" onclick="toggleTheme()" title="切换日夜模式">🌙 夜间</button>
        <button class="btn-primary" onclick="openCloudAddModal()">⚡ 离线添加 (磁链/BT)</button>
        <button class="btn-accent" onclick="openUploadModal()">⬆️ 上传文件</button>
        <button class="btn-secondary" onclick="promptCreateFolder()">📁 新建文件夹</button>
        <button class="btn-outline" onclick="refreshCurrentView()">🔄 刷新</button>
      </div>
    </header>

    <!-- VIEW 1: FILES -->
    <div class="content-view active" id="view-files">
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th style="width: 140px;">大小</th>
              <th style="width: 180px;">修改时间</th>
              <th style="width: 220px; text-align: right; padding-right: 24px;">操作</th>
            </tr>
          </thead>
          <tbody id="fileTableBody">
            <tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 50px;">正在加载网盘文件...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- VIEW 2: TRANSFERS -->
    <div class="content-view" id="view-transfers">
      <div class="subtabs">
        <div class="subtabs-left">
          <button class="subtab-btn active" id="tabDlActiveBtn" onclick="switchTransferTab('active')">正在下载 (<span id="countActiveDl">0</span>)</button>
          <button class="subtab-btn" id="tabDlDoneBtn" onclick="switchTransferTab('done')">已完成 (<span id="countDoneDl">0</span>)</button>
        </div>
        <div>
          <button class="btn-outline btn-sm" onclick="clearCompletedDownloads()">清空已完成记录</button>
        </div>
      </div>

      <div id="transfersListContainer">
        <!-- populated by js -->
      </div>
    </div>

    <!-- VIEW 3: OFFLINE (Cloud tasks) -->
    <div class="content-view" id="view-offline">
      <div class="subtabs">
        <div class="subtabs-left">
          <button class="subtab-btn active" id="tabOfflineAll" onclick="filterOfflineTasks('all')">全部 (<span id="countOfflineAll">0</span>)</button>
          <button class="subtab-btn" id="tabOfflineRunning" onclick="filterOfflineTasks('running')">进行中 (<span id="countOfflineRunning">0</span>)</button>
          <button class="subtab-btn" id="tabOfflineDone" onclick="filterOfflineTasks('done')">已完成 (<span id="countOfflineDone">0</span>)</button>
          <button class="subtab-btn" id="tabOfflineFailed" onclick="filterOfflineTasks('failed')">失败 (<span id="countOfflineFailed">0</span>)</button>
        </div>
        <div>
          <button class="btn-primary btn-sm" onclick="openCloudAddModal()">➕ 新建离线下载</button>
          <button class="btn-outline btn-sm" onclick="loadCloudTasks()">🔄 刷新列表</button>
        </div>
      </div>

      <div id="offlineListContainer">
        <!-- populated by js -->
      </div>
    </div>

    <!-- VIEW 4: SETTINGS -->
    <div class="content-view" id="view-settings">
      <!-- Mobile Only User Info Card -->
      <div class="setting-card mobile-only-usercard">
        <div class="mobile-user-header">
          <div class="user-avatar" id="mobileUserAvatar">👤</div>
          <div style="flex: 1;">
            <div style="font-size: 16px; font-weight: 700;" id="mobileUserName">连接中...</div>
            <div style="font-size: 12px; color: var(--primary); margin-top: 3px;" id="mobileUserVip">会员已生效</div>
          </div>
          <button class="btn-danger btn-sm" id="mobileLogoutBtn" onclick="confirmLogout()">🚪 退出登录</button>
          <button class="btn-primary btn-sm" id="mobileLoginBtn" onclick="openLoginModal()" style="display: none;">🔑 立即登录</button>
        </div>
        <div class="quota-box" style="margin-top: 14px; padding: 12px; background: var(--bg-hover); border-radius: 8px;">
          <div style="display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 6px;">
            <span style="color: var(--text-muted);">存储空间</span>
            <span id="mobileQuotaText" style="font-weight: 600;">-- / --</span>
          </div>
          <div class="quota-bar" style="height: 6px; background: var(--border);"><div class="quota-fill" id="mobileQuotaFillBar" style="height: 100%;"></div></div>
        </div>
      </div>

      <!-- Account Management Card (Both PC and Mobile) -->
      <div class="setting-card">
        <div class="setting-card-title">👤 账号登录与授权管理</div>
        <div class="setting-row">
          <div>
            <div style="font-weight: 600;">当前光鸭网盘账号状态</div>
            <div class="setting-desc" id="settingAccountDesc">已配置登录状态。如需切换账号或重新登录，请使用右侧功能。</div>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="btn-primary btn-sm" id="settingLoginBtn" onclick="openLoginModal()">📱 扫码登录 / 切换账号</button>
            <button class="btn-danger btn-sm" id="settingLogoutBtn" onclick="confirmLogout()">🚪 退出登录</button>
          </div>
        </div>
      </div>

      <div class="setting-card">
        <div class="setting-card-title">📁 NAS 存储与默认下载目录</div>
        <div class="setting-row">
          <div>
            <div style="font-weight: 600;">默认下载保存路径</div>
            <div class="setting-desc">网盘文件直接下载保存到该 NAS 目录。系统将自动配置目录权限（<code>777</code> 完整读写权限），手机端与飞牛文件管理器可直接访问。</div>
          </div>
        </div>
        <div style="display: flex; gap: 10px; margin-top: 10px;">
          <input type="text" id="settingDownloadDirInput" readonly style="flex: 1; cursor: pointer;" onclick="openFolderPicker(onSettingFolderPicked)">
          <button class="btn-accent" onclick="openFolderPicker(onSettingFolderPicked)">📂 浏览 NAS 目录</button>
          <button class="btn-primary" onclick="saveDownloadDirSetting()">保存</button>
        </div>
      </div>

      <div class="setting-card">
        <div class="setting-card-title">⚡ 下载加速引擎</div>
        <div class="setting-row">
          <div>
            <div style="font-weight: 600;">aria2c 多线程并发加速</div>
            <div class="setting-desc">已检测并集成 NAS 宿主机 <code>/usr/bin/aria2c</code>，支持高达 16 线程单文件分片并发疾速下载。如环境异常则自动无缝降级为 Python 流式下载。</div>
          </div>
          <div>
            <span class="status-badge status-success">已启用 (16 线程)</span>
          </div>
        </div>
      </div>

      <div class="setting-card">
        <div class="setting-card-title">🛡️ 权限与服务状态</div>
        <div class="setting-row">
          <div>
            <div style="font-weight: 600;">系统后台服务 (guangyapan.service)</div>
            <div class="setting-desc">以 root 身份驻留并管理 NAS 磁盘挂载点，端口 <code>8998</code>，开机自启动。</div>
          </div>
          <div>
            <span class="status-badge status-success">运行中</span>
          </div>
        </div>
        <div class="setting-row" style="margin-top: 14px;">
          <div>
            <div style="font-weight: 600;">fnOS 桌面快捷方式</div>
            <div class="setting-desc">已注册至飞牛应用中心及桌面入口。</div>
          </div>
          <div>
            <span class="status-badge status-success">已配置</span>
          </div>
        </div>
      </div>

      <div class="setting-card">
        <div class="setting-card-title">📖 关于</div>

        <!-- 基础套件信息 -->
        <div class="setting-row">
          <div>
            <div style="font-weight: 600; font-size: 14.5px;">光鸭网盘 fnOS 原生适配版</div>
            <div class="setting-desc" style="margin-top: 4px;">
              当前版本：<span id="aboutCurrentVersion" style="font-weight: 600; color: var(--text);">v1.0.4</span> · 开发者 / 维护者：<b>chaos</b>
            </div>
          </div>
          <div>
            <span class="status-badge" style="background: rgba(255, 122, 0, 0.15); color: var(--accent); font-weight: 600;">chaos 适配版</span>
          </div>
        </div>

        <!-- GitHub 开源仓库地址 -->
        <div class="setting-row" style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="flex: 1; min-width: 0;">
            <div style="font-weight: 600;">GitHub 开源主页</div>
            <div class="setting-desc" style="margin-top: 4px;">
              <a href="https://github.com/lawzhougc/guangyapan-fnos" target="_blank" rel="noopener noreferrer" style="color: var(--primary); text-decoration: underline; word-break: break-all;">
                https://github.com/lawzhougc/guangyapan-fnos ↗
              </a>
            </div>
          </div>
          <div style="flex-shrink: 0; margin-left: 12px;">
            <a href="https://github.com/lawzhougc/guangyapan-fnos" target="_blank" rel="noopener noreferrer" class="btn-outline btn-sm" style="display: inline-flex; align-items: center; gap: 4px; text-decoration: none;">
              ⭐ 前往仓库
            </a>
          </div>
        </div>

        <!-- 新版本更新提示 -->
        <div class="setting-row" style="margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="flex: 1; min-width: 0;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-weight: 600;">新版本更新提示</span>
              <span id="updateStatusBadge" class="status-badge" style="background: var(--bg-hover); color: var(--text-muted); font-size: 11px;">未检查</span>
            </div>
            <div class="setting-desc" id="updateStatusDesc" style="margin-top: 4px; line-height: 1.5;">
              点击右侧按键检查是否有新版本发布。
            </div>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-shrink: 0; margin-left: 12px;">
            <button class="btn-secondary btn-sm" id="checkUpdateBtn" onclick="checkForUpdates(true)">🔍 检查更新</button>
            <a id="downloadUpdateBtn" href="https://github.com/lawzhougc/guangyapan-fnos/releases" target="_blank" rel="noopener noreferrer" class="btn-primary btn-sm" style="display: none; text-decoration: none;">⬇️ 前往下载</a>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Modal: Download Target Confirmation (Issue 2) -->
  <div class="modal-backdrop" id="downloadConfirmModal">
    <div class="modal" style="width: 540px;">
      <div class="modal-header">
        <span>⬇️ 下载文件到 NAS</span>
        <button class="close-btn" onclick="closeModal('downloadConfirmModal')">&times;</button>
      </div>
      <div>
        <div style="margin-bottom: 14px;">
          <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 4px;">下载文件：</div>
          <div style="font-weight: 600; font-size: 15px; word-break: break-all;" id="dlConfirmFileName">--</div>
        </div>

        <div style="margin-bottom: 16px;">
          <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 6px;">选择 NAS 保存目录：</div>
          <div style="display: flex; gap: 8px;">
            <input type="text" id="dlConfirmPathInput" style="flex: 1;" readonly onclick="openFolderPicker(onDlConfirmFolderPicked)">
            <button class="btn-secondary" onclick="openFolderPicker(onDlConfirmFolderPicked)">📂 浏览目录</button>
          </div>
          <div style="font-size: 12px; color: var(--text-dim); margin-top: 6px;">
            提示：可下载至 NAS 任意存储卷或共享文件夹（如 照片、电影、临时文件等）。
          </div>
        </div>

        <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
          <input type="checkbox" id="setAsDefaultDirCheckbox">
          <label for="setAsDefaultDirCheckbox" style="font-size: 13px; color: var(--text-muted); cursor: pointer;">将该目录设为以后的默认下载路径</label>
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-secondary" onclick="closeModal('downloadConfirmModal')">取消</button>
        <button class="btn-primary" onclick="confirmStartDownload()">立即下载到 NAS</button>
      </div>
    </div>
  </div>

  <!-- Modal: NAS Folder Picker (Enhanced with All Shares & Intelligent Navigation) -->
  <div class="modal-backdrop" id="folderPickerModal">
    <div class="modal" style="width: 650px; max-width: 95vw;">
      <div class="modal-header">
        <span>📂 选择 NAS 保存目录</span>
        <button class="close-btn" onclick="closeModal('folderPickerModal')">&times;</button>
      </div>
      
      <!-- Breadcrumb navigation -->
      <div id="folderPickerBreadcrumbs" style="display: flex; align-items: center; gap: 4px; font-size: 12.5px; margin-bottom: 8px; overflow-x: auto; white-space: nowrap; padding: 4px 0; color: var(--text-muted);">
        <!-- populated by js -->
      </div>

      <!-- Quick Shortcuts -->
      <div style="display: flex; gap: 6px; margin-bottom: 10px; overflow-x: auto; white-space: nowrap;">
        <button class="btn-outline btn-sm" onclick="loadFolderPickerList('')">🏠 全部共享文件夹与存储卷</button>
        <button class="btn-outline btn-sm" onclick="loadFolderPickerList(defaultDownloadDir)">📦 当前默认下载目录</button>
      </div>

      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
        <input type="text" id="folderPickerCurrentPath" readonly style="flex: 1; font-family: monospace; font-size: 12.5px;" placeholder="当前选定路径...">
        <button class="btn-outline btn-sm" onclick="folderPickerGoUp()">⬆️ 上一级</button>
        <button class="btn-secondary btn-sm" onclick="folderPickerMkdir()">➕ 新建子文件夹</button>
      </div>

      <div class="fs-browser-list" id="folderPickerList" style="min-height: 200px; max-height: 340px;">
        <!-- populated by js -->
      </div>

      <div class="modal-footer" style="display: flex; justify-content: space-between; align-items: center;">
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

  <!-- Modal: Cloud Add (Magnet & Torrent) -->
  <div class="modal-backdrop" id="cloudAddModal">
    <div class="modal">
      <div class="modal-header">
        <span>⚡ 云离线下载 (磁力 / BT)</span>
        <button class="close-btn" onclick="closeModal('cloudAddModal')">&times;</button>
      </div>
      <div class="subtabs" style="margin-bottom: 14px;">
        <div class="subtabs-left">
          <button class="subtab-btn active" id="tabMagnetBtn" onclick="switchCloudTab('magnet')">磁力 / HTTP 链接</button>
          <button class="subtab-btn" id="tabTorrentBtn" onclick="switchCloudTab('torrent')">BT 种子文件 (.torrent)</button>
        </div>
      </div>

      <div id="magnetSection">
        <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 8px;">输入 magnet:?xt=urn:btih: 磁力链接或下载网址：</p>
        <textarea id="magnetInput" rows="5" placeholder="magnet:?xt=urn:btih:..."></textarea>
      </div>

      <div id="torrentSection" style="display: none;">
        <div class="dropzone" onclick="document.getElementById('torrentFileInput').click()">
          <p id="torrentFileName">点击选择或拖拽 .torrent 种子文件至此处</p>
          <input type="file" id="torrentFileInput" accept=".torrent" style="display: none;" onchange="handleTorrentSelect(this)">
        </div>
      </div>

      <div class="modal-footer">
        <button class="btn-secondary" onclick="closeModal('cloudAddModal')">取消</button>
        <button class="btn-primary" onclick="submitCloudAdd()">添加任务</button>
      </div>
    </div>
  </div>

  <!-- Modal: Upload -->
  <div class="modal-backdrop" id="uploadModal">
    <div class="modal">
      <div class="modal-header">
        <span>⬆️ 上传文件到光鸭网盘</span>
        <button class="close-btn" onclick="closeModal('uploadModal')">&times;</button>
      </div>
      <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">支持极速秒传 (基于迅雷哈希算法)，未命中时直传阿里云 OSS</p>
      
      <div class="dropzone" onclick="document.getElementById('uploadFileInput').click()">
        <p id="uploadFileNameText">点击选择或拖拽文件到此处</p>
        <input type="file" id="uploadFileInput" style="display: none;" onchange="handleUploadSelect(this)">
      </div>

      <div id="uploadProgressSection" style="display: none; margin-top: 16px;">
        <div class="task-details" style="margin-bottom: 6px;">
          <span id="uploadStatusLabel">准备中...</span>
          <span id="uploadPctLabel">0%</span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" id="uploadProgressBar"></div>
        </div>
      </div>

      <div class="modal-footer">
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
          <img src="/icon.png?v=1.0.4" style="width: 26px; height: 26px; object-fit: contain; border-radius: 6px;">
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

  <div id="toast"></div>

  <script>
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
      document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
      document.querySelectorAll(".content-view").forEach(el => el.classList.remove("active"));

      if (viewName === 'files') {
        document.getElementById("navFiles").classList.add("active");
        document.getElementById("view-files").classList.add("active");
        updateBreadcrumbs();
      } else if (viewName === 'transfers') {
        document.getElementById("navTransfers").classList.add("active");
        document.getElementById("view-transfers").classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⬇️ 本地下载与传输任务</span>`;
        loadTransfers();
      } else if (viewName === 'offline') {
        document.getElementById("navOffline").classList.add("active");
        document.getElementById("view-offline").classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⚡ 云端离线下载 (磁力/BT)</span>`;
        loadCloudTasks();
      } else if (viewName === 'settings') {
        document.getElementById("navSettings").classList.add("active");
        document.getElementById("view-settings").classList.add("active");
        document.getElementById("breadcrumbs").innerHTML = `<span class="crumb-item current">⚙️ 系统设置</span>`;
        loadSettings();
      }
    }

    function refreshCurrentView() {
      if (currentView === 'files') loadFiles(currentParentId);
      else if (currentView === 'transfers') loadTransfers();
      else if (currentView === 'offline') loadCloudTasks();
      else if (currentView === 'settings') loadSettings();
    }

    // ================= VIEW: FILES =================
    async function loadFiles(parentId = currentParentId) {
      currentParentId = parentId;
      const tbody = document.getElementById("fileTableBody");
      tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 50px;">正在加载目录文件...</td></tr>`;

      try {
        const res = await fetch(`/api/files?parentId=${encodeURIComponent(parentId)}`);
        const json = await res.json();
        if (json.code === 401 || (!isLoggedIn && (!json.list || json.list.length === 0))) {
          tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; padding: 50px;"><div style="font-size: 15px; font-weight: 600; margin-bottom: 8px;">⚠️ 当前未登录光鸭网盘账号</div><div style="color: var(--text-muted); font-size: 13px; margin-bottom: 16px;">登录后即可查看网盘文件并开始疾速上传下载</div><button class="btn-primary" onclick="openLoginModal()">📱 立即扫码登录</button></td></tr>`;
          return;
        }
        const list = json.list || [];

        if (list.length === 0) {
          tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-muted); padding: 50px;">当前文件夹为空</td></tr>`;
          return;
        }

        tbody.innerHTML = "";
        list.forEach(item => {
          const tr = document.createElement("tr");
          const isDir = (item.resType === 2);
          const icon = isDir ? "📁" : getFileIcon(item.fileName);

          tr.innerHTML = `
            <td>
              <div class="file-name-cell" onclick="${isDir ? `navigateTo('${item.fileId}', '${escapeHtml(item.fileName)}')` : ''}">
                <span class="file-icon">${icon}</span>
                <span class="file-name-text" title="${escapeHtml(item.fileName)}">${escapeHtml(item.fileName)}</span>
              </div>
            </td>
            <td>${isDir ? '--' : formatBytes(item.fileSize)}</td>
            <td>${formatDate(item.utime || item.ctime)}</td>
            <td>
              <div class="actions-cell" style="justify-content: flex-end;">
                ${!isDir ? `<button class="btn-primary btn-sm" onclick="promptStartDownload('${item.fileId}', '${escapeHtml(item.fileName)}')">⬇️<span class="desktop-text"> 下载到NAS</span><span class="mobile-text"> 下载</span></button>` : ''}
                ${!isDir ? `<button class="btn-outline btn-sm desktop-only-btn" onclick="copyLink('${item.fileId}')">🔗 直链</button>` : ''}
                <button class="btn-danger btn-sm" onclick="deleteFile('${item.fileId}', '${escapeHtml(item.fileName)}')">🗑️</button>
              </div>
            </td>
          `;
          tbody.appendChild(tr);
        });
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: #f87171; padding: 40px;">加载失败: ${e.message}</td></tr>`;
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

    async function copyLink(fileId) {
      try {
        const res = await fetch(`/api/download-url?fileId=${encodeURIComponent(fileId)}`);
        const d = await res.json();
        if (d.signedURL) {
          await navigator.clipboard.writeText(d.signedURL);
          showToast("CDN 极速下载直链已复制！");
        } else {
          alert("获取直链失败: " + JSON.stringify(d));
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

      closeModal("downloadConfirmModal");
      showToast("正在启动后台下载...");

      try {
        const res = await fetch("/api/download-task", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            fileId: pendingDownloadFileId,
            fileName: pendingDownloadFileName,
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
                <button class="btn-outline btn-sm" style="padding: 6px 16px;" onclick="folderPickerGoUp()">⬆️ 返回上一级</button>
              </div>
            </div>
          `;
          return;
        }

        container.innerHTML = "";
        folders.forEach(f => {
          const div = document.createElement("div");
          const isSelected = (f.path === folderPickerSelectedPath);
          div.className = "fs-folder-item" + (isSelected ? " selected" : "");
          div.dataset.path = f.path;
          div.innerHTML = `
            <div class="fs-folder-left" style="flex: 1; min-width: 0;" onclick="selectFolderPickerItem('${escapeHtml(f.path)}')">
              <span style="font-size: 16px; flex-shrink: 0;">📁</span>
              <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: ${f.is_share ? '600' : '400'};" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
            </div>
            <div style="display: flex; gap: 6px; align-items: center; flex-shrink: 0;">
              <button class="btn-primary btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="event.stopPropagation(); selectFolderPickerItem('${escapeHtml(f.path)}'); confirmFolderPicker();">选择</button>
              <button class="btn-outline btn-sm" style="padding: 3px 8px; font-size: 11.5px;" onclick="event.stopPropagation(); loadFolderPickerList('${escapeHtml(f.path)}')">进入 ➔</button>
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
      if (activeList.length > 0) {
        badge.style.display = "inline-block";
        badge.innerText = activeList.length;
      } else {
        badge.style.display = "none";
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
        // Guangyapan status 2 = finished
        const isDone = (t.status === 2 || !!t.fileId);
        const isFailed = (t.status === 3 || !!t.errCode);
        const isRunning = !isDone && !isFailed;

        let badgeClass = "status-active";
        let badgeText = `下载中 ${t.progress || 0}%`;
        if (isDone) {
          badgeClass = "status-success";
          badgeText = "已完成";
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
            <div style="display: flex; gap: 8px; align-items: center;">
              <span class="status-badge ${badgeClass}">${badgeText}</span>
              ${isDone && t.fileId ? `<button class="btn-primary btn-sm" onclick="promptStartDownload('${t.fileId}', '${escapeHtml(t.fileName)}')">⬇️<span class="desktop-text"> 下载到NAS</span><span class="mobile-text"> 下载</span></button>` : ''}
              <button class="btn-outline btn-sm" onclick="deleteOfflineTask('${t.taskId}')">🗑️ 删除</button>
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

    async function deleteOfflineTask(taskId) {
      if (!confirm("确定要删除该离线任务记录吗？")) return;
      try {
        await fetch("/api/cloud-add/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ taskId })
        });
        showToast("离线任务已删除");
        loadCloudTasks();
      } catch (e) {
        alert("删除失败: " + e.message);
      }
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

    // ================= CLOUD ADD (MAGNET / TORRENT) =================
    function openCloudAddModal() {
      document.getElementById("magnetInput").value = "";
      selectedTorrentFile = null;
      document.getElementById("torrentFileName").innerText = "点击选择或拖拽 .torrent 种子文件至此处";
      openModal("cloudAddModal");
    }

    function switchCloudTab(tab) {
      activeCloudTab = tab;
      if (tab === 'magnet') {
        document.getElementById("tabMagnetBtn").classList.add("active");
        document.getElementById("tabTorrentBtn").classList.remove("active");
        document.getElementById("magnetSection").style.display = "block";
        document.getElementById("torrentSection").style.display = "none";
      } else {
        document.getElementById("tabMagnetBtn").classList.remove("active");
        document.getElementById("tabTorrentBtn").classList.add("active");
        document.getElementById("magnetSection").style.display = "none";
        document.getElementById("torrentSection").style.display = "block";
      }
    }

    function handleTorrentSelect(input) {
      if (input.files && input.files[0]) {
        selectedTorrentFile = input.files[0];
        document.getElementById("torrentFileName").innerText = "已选择: " + selectedTorrentFile.name;
      }
    }

    async function submitCloudAdd() {
      if (activeCloudTab === 'magnet') {
        const magnet = document.getElementById("magnetInput").value.trim();
        if (!magnet) {
          alert("请输入磁力链接或 URL");
          return;
        }
        try {
          const res = await fetch("/api/cloud-add/magnet", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: magnet, parentId: currentParentId })
          });
          const d = await res.json();
          if (d.data && d.data.taskId) {
            showToast("云下载任务已添加！");
            closeModal("cloudAddModal");
            switchView("offline");
          } else {
            alert("添加失败: " + (d.msg || JSON.stringify(d)));
          }
        } catch (e) {
          alert("错误: " + e.message);
        }
      } else {
        if (!selectedTorrentFile) {
          alert("请选择 .torrent 种子文件");
          return;
        }
        const formData = new FormData();
        formData.append("torrent", selectedTorrentFile);
        formData.append("parentId", currentParentId);
        try {
          const res = await fetch("/api/cloud-add/torrent", {
            method: "POST",
            body: formData
          });
          const d = await res.json();
          if (d.data && d.data.taskId) {
            showToast("BT 离线任务已添加！");
            closeModal("cloudAddModal");
            switchView("offline");
          } else {
            alert("添加失败: " + (d.msg || JSON.stringify(d)));
          }
        } catch (e) {
          alert("上传失败: " + e.message);
        }
      }
    }

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
          btn.innerHTML = "🌙 夜间";
          btn.title = "当前为日间模式，点击切换为夜间模式";
        } else {
          btn.innerHTML = "☀️ 日间";
          btn.title = "当前为夜间模式，点击切换为日间模式";
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
</html>
"""

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

        elif path == "/api/cloud-add/magnet":
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            magnet = body.get("url")
            parent_id = body.get("parentId", "")
            try:
                res = self.app.client.cloud_add_magnet(magnet, parent_id=parent_id)
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

            for p in parts:
                if b'name="torrent"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        torrent_bytes = p[header_end + 4 : -2]
                elif b'name="parentId"' in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end != -1:
                        parent_id = p[header_end + 4 : -2].decode("utf-8")

            if not torrent_bytes:
                self._send_json({"error": "No torrent file found"}, 400)
                return

            try:
                res = self.app.client.cloud_add_torrent(torrent_bytes, filename=filename, parent_id=parent_id)
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
