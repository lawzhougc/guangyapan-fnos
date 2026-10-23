import os
import tarfile
import io
import shutil
import hashlib

BASE_DIR = r"d:\antigravity\guangyapan_fnos"
BUILD_DIR = os.path.join(BASE_DIR, "build_fpk_tmp")
OUTPUT_FPK = os.path.join(BASE_DIR, "guangyapan-1.0.3-x86_64.fpk")

if os.path.exists(BUILD_DIR):
    shutil.rmtree(BUILD_DIR)
os.makedirs(BUILD_DIR, exist_ok=True)

# 1. Prepare files for app.tgz (the installed payload)
app_payload_dir = os.path.join(BUILD_DIR, "app_payload")
os.makedirs(os.path.join(app_payload_dir, "ui", "images"), exist_ok=True)
os.makedirs(os.path.join(app_payload_dir, "guangyapan"), exist_ok=True)

# Copy python sources
for py_file in ["client.py", "downloader.py", "hash_utils.py", "web_server.py", "__init__.py"]:
    src = os.path.join(BASE_DIR, "guangyapan", py_file)
    dst = os.path.join(app_payload_dir, "guangyapan", py_file)
    shutil.copy2(src, dst)

# Copy official icon and favicon to payload
official_icon = os.path.join(BASE_DIR, "icon.png")
if not os.path.exists(official_icon):
    official_icon = os.path.join(BASE_DIR, "icon-256.png")

shutil.copy2(official_icon, os.path.join(app_payload_dir, "icon.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "guangyapan", "icon.png"))

if os.path.exists(os.path.join(BASE_DIR, "favicon.ico")):
    shutil.copy2(os.path.join(BASE_DIR, "favicon.ico"), os.path.join(app_payload_dir, "favicon.ico"))

# Copy qrcode.min.js to payload
if os.path.exists(os.path.join(BASE_DIR, "qrcode.min.js")):
    shutil.copy2(os.path.join(BASE_DIR, "qrcode.min.js"), os.path.join(app_payload_dir, "qrcode.min.js"))
    shutil.copy2(os.path.join(BASE_DIR, "qrcode.min.js"), os.path.join(app_payload_dir, "guangyapan", "qrcode.min.js"))

# Copy cli.py and config.json
if os.path.exists(os.path.join(BASE_DIR, "cli.py")):
    shutil.copy2(os.path.join(BASE_DIR, "cli.py"), os.path.join(app_payload_dir, "cli.py"))
if os.path.exists(os.path.join(BASE_DIR, "config.json")):
    shutil.copy2(os.path.join(BASE_DIR, "config.json"), os.path.join(app_payload_dir, "config.json"))

# Create main.py in app_payload
main_py_code = """#!/usr/bin/env python3
import sys
import os
import signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from guangyapan.web_server import run_server

def handle_sig(sig, frame):
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    port = int(os.environ.get("PORT", 8998))
    config_path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(config_path):
        alt = "/vol1/@appdata/guangyapan/config.json"
        if os.path.exists(alt):
            config_path = alt
    run_server(port=port, config_path=config_path)
"""
with open(os.path.join(app_payload_dir, "main.py"), "w", encoding="utf-8", newline="\n") as f:
    f.write(main_py_code)

# Copy UI images (all sizes from official icon)
for size in [16, 32, 64, 128, 256, 512]:
    src = os.path.join(BASE_DIR, f"icon-{size}.png")
    if not os.path.exists(src):
        src = official_icon
    dst_num = os.path.join(app_payload_dir, "ui", "images", f"{size}.png")
    dst_icon = os.path.join(app_payload_dir, "ui", "images", f"icon_{size}.png")
    dst_hyphen = os.path.join(app_payload_dir, "ui", "images", f"icon-{size}.png")
    shutil.copy2(src, dst_num)
    shutil.copy2(src, dst_icon)
    shutil.copy2(src, dst_hyphen)

# Special icon_{0}.png and {0}.png
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "icon_{0}.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "{0}.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "icon-{0}.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "0.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "icon_0.png"))
shutil.copy2(official_icon, os.path.join(app_payload_dir, "ui", "images", "icon.png"))

# UI Config
ui_config = """{
    ".url": {
        "guangyapan.Application": {
            "title": "光鸭网盘",
            "desc": "光鸭网盘 Web 管理界面",
            "icon": "images/icon_{0}.png",
            "type": "url",
            "protocol": "http",
            "port": "8998",
            "url": "/",
            "allUsers": true
        }
    }
}
"""
with open(os.path.join(app_payload_dir, "ui", "config"), "w", encoding="utf-8", newline="\n") as f:
    f.write(ui_config)

# Pack app.tgz
app_tgz_path = os.path.join(BUILD_DIR, "app.tgz")
with tarfile.open(app_tgz_path, "w:gz") as tar:
    for root, dirs, files in os.walk(app_payload_dir):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, app_payload_dir).replace("\\", "/")
            ti = tar.gettarinfo(full_path, arcname=rel_path)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            if f.endswith(".py") or f.endswith(".sh") or f == "main.py":
                ti.mode = 0o755
            else:
                ti.mode = 0o644
            with open(full_path, "rb") as fp:
                tar.addfile(ti, fp)

print("Packed app.tgz successfully, size:", os.path.getsize(app_tgz_path))

# 2. Prepare outer package files
pkg_root = os.path.join(BUILD_DIR, "pkg_root")
os.makedirs(os.path.join(pkg_root, "config"), exist_ok=True)
os.makedirs(os.path.join(pkg_root, "cmd"), exist_ok=True)

# Manifest
manifest_content = """appname="guangyapan"
version="1.0.3"
desc="光鸭网盘 fnOS 专用版，支持高速上传下载、磁链/BT云离线、NAS本地存储管理"
arch="x86_64"
maintainer="guangyapan"
maintainer_url="https://guangyapan.com"
distributor="fnOS"
distributor_url="https://guangyapan.com"
display_name="光鸭网盘"
desktop_uidir="ui"
desktop_applaunchname="guangyapan.Application"
service_port="8998"
source="thirdparty"
reloadui="yes"
install_type="volume"
checkport="false"
micro_app="false"
disable_authorization_path="false"
changelog="1.0.3: 适配光鸭云盘官方视觉与官方应用图标；新增官方扫码登录、Token导入与登出；修复移动端不可用与桌面图标显示，全面优化应用中心状态管理"
"""
with open(os.path.join(pkg_root, "manifest"), "w", encoding="utf-8", newline="\n") as f:
    f.write(manifest_content)

# config/privilege
privilege_content = """{
    "defaults": {
        "run-as": "package"
    }
}
"""
with open(os.path.join(pkg_root, "config", "privilege"), "w", encoding="utf-8", newline="\n") as f:
    f.write(privilege_content)

# config/resource
resource_content = """{}
"""
with open(os.path.join(pkg_root, "config", "resource"), "w", encoding="utf-8", newline="\n") as f:
    f.write(resource_content)

# cmd/main
cmd_main = """#!/bin/bash

APP_PORT=8998
DATA_DIR="/vol1/@appdata/guangyapan"
TARGET_DIR="/vol1/@appcenter/guangyapan"
PID_FILE="${DATA_DIR}/app.pid"
LOG_FILE="${DATA_DIR}/info.log"

check_port_listening() {
    local port=$1
    python3 -c "import socket; s = socket.socket(); s.settimeout(1); exit(s.connect_ex(('127.0.0.1', ${port})))" 2>/dev/null
}

find_pid() {
    cat "$PID_FILE" 2>/dev/null
}

status() {
    if check_port_listening "$APP_PORT"; then
        echo "服务正在运行 (端口: ${APP_PORT})"
        exit 0
    else
        echo "服务未运行"
        exit 3
    fi
}

start_process() {
    if check_port_listening "$APP_PORT"; then
        echo "已有实例在运行 (端口: ${APP_PORT})"
        exit 0
    fi

    mkdir -p "$DATA_DIR"
    
    # Prefer launching from TARGET_DIR or DATA_DIR
    local run_script="${TARGET_DIR}/main.py"
    if [ ! -f "$run_script" ]; then
        run_script="${DATA_DIR}/main.py"
    fi

    cd "$DATA_DIR" || exit 1
    nohup /usr/bin/python3 "$run_script" >> "${LOG_FILE}" 2>&1 &
    local new_pid=$!
    echo "$new_pid" > "$PID_FILE"

    for i in $(seq 1 15); do
        if check_port_listening "$APP_PORT"; then
            echo "服务启动成功 (PID: ${new_pid})"
            exit 0
        fi
        sleep 1
    done

    if check_port_listening "$APP_PORT"; then
        echo "服务启动成功"
        exit 0
    fi

    echo "服务启动失败"
    exit 1
}

stop_process() {
    local pid=$(find_pid)
    if [ -n "$pid" ]; then
        kill -TERM "$pid" 2>/dev/null
        sleep 1
        kill -9 "$pid" 2>/dev/null
    fi
    pkill -u guangyapan -f main.py 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "服务已停止"
    exit 0
}

case "$1" in
    start)
        start_process
        ;;
    stop)
        stop_process
        ;;
    status)
        status
        ;;
    restart)
        stop_process
        sleep 1
        start_process
        ;;
    *)
        echo "用法: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
"""
with open(os.path.join(pkg_root, "cmd", "main"), "w", encoding="utf-8", newline="\n") as f:
    f.write(cmd_main)

# Callbacks
callbacks = {
    "install_init": "#!/bin/bash\nexit 0\n",
    "install_callback": """#!/bin/bash
# Ensure package user & group exist
if ! id guangyapan >/dev/null 2>&1; then
    groupadd guangyapan 2>/dev/null || true
    useradd -g 901 -G guangyapan -s /usr/sbin/nologin -d /home/guangyapan -M guangyapan 2>/dev/null || true
fi
chown -R guangyapan:guangyapan /var/apps/guangyapan /vol1/@appcenter/guangyapan /vol1/@appdata/guangyapan 2>/dev/null || true
chmod -R 755 /var/apps/guangyapan/cmd /var/apps/guangyapan/ui 2>/dev/null || true
exit 0
""",
    "uninstall_init": """#!/bin/bash
pkill -u guangyapan -f main.py 2>/dev/null || true
systemctl stop guangyapan 2>/dev/null || true
exit 0
""",
    "uninstall_callback": "#!/bin/bash\nexit 0\n",
    "upgrade_init": "#!/bin/bash\npkill -u guangyapan -f main.py 2>/dev/null || true\nexit 0\n",
    "upgrade_callback": """#!/bin/bash
chown -R guangyapan:guangyapan /var/apps/guangyapan /vol1/@appcenter/guangyapan /vol1/@appdata/guangyapan 2>/dev/null || true
chmod -R 755 /var/apps/guangyapan/cmd /var/apps/guangyapan/ui 2>/dev/null || true
exit 0
""",
    "config_init": "#!/bin/bash\nexit 0\n",
    "config_callback": "#!/bin/bash\nexit 0\n"
}

for name, content in callbacks.items():
    with open(os.path.join(pkg_root, "cmd", name), "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

# Copy app.tgz into outer pkg_root
shutil.copy2(app_tgz_path, os.path.join(pkg_root, "app.tgz"))

# Read official icon bytes
with open(official_icon, "rb") as f:
    official_icon_bytes = f.read()

# 3. Create the final .fpk archive using python tarfile
# An fpk is a gzip-compressed tar archive containing manifest, config/, cmd/, ICON.PNG, ICON_256.PNG, app.tgz
print(f"Creating FPK package: {OUTPUT_FPK} ...")
with tarfile.open(OUTPUT_FPK, "w:gz") as fpk:
    # Add files from pkg_root
    for root, dirs, files in os.walk(pkg_root):
        for d in dirs:
            full_d = os.path.join(root, d)
            rel_d = os.path.relpath(full_d, pkg_root).replace("\\", "/") + "/"
            ti = fpk.gettarinfo(full_d, arcname=rel_d)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            ti.mode = 0o755
            fpk.addfile(ti)
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, pkg_root).replace("\\", "/")
            ti = fpk.gettarinfo(full_path, arcname=rel_path)
            ti.uid = 0
            ti.gid = 0
            ti.uname = "root"
            ti.gname = "root"
            if rel_path.startswith("cmd/"):
                ti.mode = 0o755
            else:
                ti.mode = 0o644
            with open(full_path, "rb") as fp:
                fpk.addfile(ti, fp)
    
    # Add ICON.PNG, ICON_256.PNG, and icon.png explicitly with exact case
    for icon_name in ["ICON.PNG", "ICON_256.PNG", "icon.png"]:
        ti = tarfile.TarInfo(name=icon_name)
        ti.size = len(official_icon_bytes)
        ti.mode = 0o644
        ti.uid = 0
        ti.gid = 0
        ti.uname = "root"
        ti.gname = "root"
        ti.type = tarfile.REGTYPE
        fpk.addfile(ti, io.BytesIO(official_icon_bytes))

print(f"FPK built successfully! Size: {os.path.getsize(OUTPUT_FPK)} bytes")

# Verify contents of FPK
print("\n=== Contents of generated FPK ===")
with tarfile.open(OUTPUT_FPK, "r:gz") as tar:
    for m in tar.getmembers():
        print(f"  {m.name:<30} {m.size:>8} bytes, mode={oct(m.mode)}")
