import os
import shutil
import subprocess
import threading
import urllib.request
import time
import re
from typing import Optional, Callable, Dict, Any

def fix_permissions(path: str):
    """Ensure fnOS user and all apps have full read/write access."""
    try:
        if os.path.exists(path):
            try:
                # If path is inside /vol*/<uid>/..., match the owner uid if numeric
                parts = path.replace("\\", "/").split("/")
                for p in parts:
                    if p.isdigit() and int(p) >= 1000:
                        uid = int(p)
                        os.chown(path, uid, uid)
                        break
                else:
                    os.chown(path, 1000, 1001)
            except Exception:
                pass
            try:
                os.chmod(path, 0o777)
            except Exception:
                pass
    except Exception:
        pass

def format_speed(speed_bytes_sec: float) -> str:
    if speed_bytes_sec <= 0:
        return "0 KB/s"
    if speed_bytes_sec >= 1024 * 1024 * 1024:
        return f"{speed_bytes_sec / (1024**3):.2f} GB/s"
    if speed_bytes_sec >= 1024 * 1024:
        return f"{speed_bytes_sec / (1024**2):.1f} MB/s"
    if speed_bytes_sec >= 1024:
        return f"{speed_bytes_sec / 1024:.1f} KB/s"
    return f"{int(speed_bytes_sec)} B/s"

def format_size(bytes_val: int) -> str:
    if not bytes_val or bytes_val <= 0:
        return "--"
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / (1024**3):.2f} GB"
    if bytes_val >= 1024 * 1024:
        return f"{bytes_val / (1024**2):.1f} MB"
    if bytes_val >= 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val} B"

def clean_aria2_unit(val: str) -> str:
    """Convert aria2 unit like 72MiB to 72 MB"""
    if not val:
        return ""
    val = val.replace("iB", "B").replace("ib", "B")
    return val

class FileDownloader:
    def __init__(self, use_aria2: bool = True):
        self.use_aria2 = use_aria2 and (shutil.which("aria2c") is not None)
        self.active_processes: Dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def cancel_task(self, task_id: str):
        with self.lock:
            p = self.active_processes.pop(task_id, None)
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass

    def download(self, url: str, output_path: str, progress_callback: Optional[Callable] = None, task_id: str = "") -> bool:
        dest_dir = os.path.dirname(output_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)
            fix_permissions(dest_dir)

        if self.use_aria2:
            ok = self._download_aria2(url, output_path, progress_callback, task_id)
        else:
            ok = self._download_python(url, output_path, progress_callback)

        if ok and os.path.exists(output_path):
            fix_permissions(output_path)
        return ok

    def _download_aria2(self, url: str, output_path: str, progress_callback: Optional[Callable], task_id: str = "") -> bool:
        dest_dir = os.path.abspath(os.path.dirname(output_path))
        filename = os.path.basename(output_path)

        cmd = [
            "aria2c",
            "--summary-interval=1",
            "--continue=true",
            "--max-connection-per-server=16",
            "--split=16",
            "--min-split-size=1M",
            "--user-agent=GuangShanYun/1.0.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            f"--dir={dest_dir}",
            f"--out={filename}",
            url
        ]

        pattern = re.compile(r'\[#\w+\s+([0-9.]+[A-Za-z]+)/([0-9.]+[A-Za-z]+)\((\d+)%\).*?DL:([0-9.]+[A-Za-z]+)(?:\s+ETA:([^\s\]]+))?')

        try:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            if task_id:
                with self.lock:
                    self.active_processes[task_id] = process

            for line in process.stdout:
                line_str = line.strip()
                match = pattern.search(line_str)
                if match and progress_callback:
                    dl_str = clean_aria2_unit(match.group(1))
                    tot_str = clean_aria2_unit(match.group(2))
                    pct = int(match.group(3))
                    spd = clean_aria2_unit(match.group(4)) + "/s"
                    eta = match.group(5) or "--"
                    progress_callback({
                        "downloaded": dl_str,
                        "total": tot_str,
                        "percent": pct,
                        "speed": spd,
                        "eta": eta
                    })
                elif progress_callback and "%" in line_str:
                    progress_callback({"raw": line_str})

            process.wait()
            if task_id:
                with self.lock:
                    self.active_processes.pop(task_id, None)

            if process.returncode == 0:
                return True
            else:
                print(f"Aria2 process exited with code {process.returncode}")
                return False
        except Exception as e:
            print("Aria2 download error, falling back to python:", e)
            if task_id:
                with self.lock:
                    self.active_processes.pop(task_id, None)
            return self._download_python(url, output_path, progress_callback)

    def _download_python(self, url: str, output_path: str, progress_callback: Optional[Callable]) -> bool:
        req = urllib.request.Request(url, headers={
            "User-Agent": "GuangShanYun/1.0.0 Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        tmp_path = output_path + ".tmp"
        try:
            with urllib.request.urlopen(req) as resp, open(tmp_path, "wb") as f:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 512 * 1024 # 512KB chunks
                last_time = time.time()
                last_downloaded = 0

                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    elapsed = now - last_time
                    if progress_callback and (elapsed >= 0.8 or downloaded == total):
                        speed_bps = (downloaded - last_downloaded) / elapsed if elapsed > 0 else 0
                        pct = int((downloaded / total) * 100) if total else 0
                        
                        eta_str = "--"
                        if speed_bps > 0 and total > downloaded:
                            rem_secs = int((total - downloaded) / speed_bps)
                            if rem_secs >= 60:
                                eta_str = f"{rem_secs // 60}m{rem_secs % 60}s"
                            else:
                                eta_str = f"{rem_secs}s"

                        progress_callback({
                            "downloaded": format_size(downloaded),
                            "total": format_size(total),
                            "percent": pct,
                            "speed": format_speed(speed_bps),
                            "eta": eta_str
                        })
                        last_time = now
                        last_downloaded = downloaded

            if os.path.exists(output_path):
                os.remove(output_path)
            os.rename(tmp_path, output_path)
            fix_permissions(output_path)
            return True
        except Exception as e:
            if os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except Exception: pass
            raise e
