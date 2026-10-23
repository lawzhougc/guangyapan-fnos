import os
import tarfile
import io
import shutil
import hashlib
import sys
import argparse

BASE_DIR = r"d:\antigravity\guangyapan_fnos"
VERSION = "1.1.0"

def build_package(target_arch="x86_64"):
    is_arm = target_arch.lower() in ("arm", "arm64", "aarch64")
    arch_tag = "arm64" if is_arm else "x86_64"
    platform_val = "arm" if is_arm else "x86"
    arch_val = "arm64 aarch64" if is_arm else "x86_64"

    build_dir = os.path.join(BASE_DIR, f"build_fpk_tmp_{arch_tag}")
    output_fpk = os.path.join(BASE_DIR, f"guangyapan-{VERSION}-{arch_tag}.fpk")

    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)

    print(f"\n==========================================")
    print(f"Building Guangyapan FPK for [{arch_tag.upper()}] ...")
    print(f"Platform: {platform_val}, Arch: {arch_val}")
    print(f"Output: {output_fpk}")
    print(f"==========================================")

    # 1. Prepare files for app.tgz (the installed payload)
    app_payload_dir = os.path.join(build_dir, "app_payload")
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
        official_icon = os.path.join(BASE_DIR, "guangyapan_real_icon_256.png")

    shutil.copy2(official_icon, os.path.join(app_payload_dir, "icon.png"))
    shutil.copy2(official_icon, os.path.join(app_payload_dir, "guangyapan", "icon.png"))

    if os.path.exists(os.path.join(BASE_DIR, "favicon.ico")):
        shutil.copy2(os.path.join(BASE_DIR, "favicon.ico"), os.path.join(app_payload_dir, "favicon.ico"))

    # Copy qrcode.min.js to payload
    if os.path.exists(os.path.join(BASE_DIR, "qrcode.min.js")):
        shutil.copy2(os.path.join(BASE_DIR, "qrcode.min.js"), os.path.join(app_payload_dir, "qrcode.min.js"))
        shutil.copy2(os.path.join(BASE_DIR, "qrcode.min.js"), os.path.join(app_payload_dir, "guangyapan", "qrcode.min.js"))

    # Copy official brand SVGs & empty state illustration
    for asset_name in ["empty.png", "brand_guangya_txt.svg", "brand_logo_text.png"]:
        src_asset = os.path.join(BASE_DIR, "guangyapan", asset_name)
        if os.path.exists(src_asset):
            shutil.copy2(src_asset, os.path.join(app_payload_dir, asset_name))
            shutil.copy2(src_asset, os.path.join(app_payload_dir, "guangyapan", asset_name))

    # Copy cli.py
    if os.path.exists(os.path.join(BASE_DIR, "cli.py")):
        shutil.copy2(os.path.join(BASE_DIR, "cli.py"), os.path.join(app_payload_dir, "cli.py"))

    # Write pristine, strictly unauthenticated config.json (guaranteeing zero leaked tokens)
    clean_config = {
        "access_token": "",
        "refresh_token": "",
        "sub": "",
        "expires_at": 0,
        "download_dir": "",
        "port": 8998,
        "use_aria2": True
    }
    import json
    with open(os.path.join(app_payload_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(clean_config, f, indent=2, ensure_ascii=False)

    # Create main.py in app_payload
    main_py_code = """#!/usr/bin/env python3
import sys
import os
import signal
import glob

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
        if os.path.exists("/var/apps/guangyapan/var/config.json"):
            config_path = "/var/apps/guangyapan/var/config.json"
        else:
            candidates = glob.glob("/vol*/@appdata/guangyapan/config.json")
            if candidates:
                config_path = candidates[0]
    run_server(port=port, config_path=config_path)
"""
    with open(os.path.join(app_payload_dir, "main.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(main_py_code)

    # Copy UI images (all sizes from official icon)
    for size in [16, 32, 48, 64, 128, 256, 512]:
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
            "desc": "光鸭网盘 Web 管理端 (by chaos)",
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
    app_tgz_path = os.path.join(build_dir, "app.tgz")
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
    pkg_root = os.path.join(build_dir, "pkg_root")
    os.makedirs(os.path.join(pkg_root, "config"), exist_ok=True)
    os.makedirs(os.path.join(pkg_root, "cmd"), exist_ok=True)

    # Manifest
    manifest_content = f"""appname="guangyapan"
version="{VERSION}"
desc="光鸭网盘 fnOS 专用版，支持高速上传下载、磁链/BT云离线、NAS本地存储管理"
arch="{arch_val}"
platform="{platform_val}"
maintainer="chaos"
maintainer_url="https://github.com/lawzhougc/guangyapan-fnos"
distributor="chaos"
distributor_url="https://github.com/lawzhougc/guangyapan-fnos"
display_name="光鸭网盘"
desktop_uidir="ui"
desktop_applaunchname="guangyapan.Application"
service_port="8998"
source="thirdparty"
reloadui="yes"
install_type="root"
checkport="false"
micro_app="false"
disable_authorization_path="true"
changelog="{VERSION}: [{arch_tag.upper()} 架构 · chaos 适配版] 1. 1:1 深度复刻光鸭官方现代视觉体系（品牌色、72px 极简左侧导航轨、顶栏品牌标、官方呆萌鸭空状态）；2. 顶部原生分类标签（全部、最近、视频、图片、文档、音频、安装包）及实时搜索过滤；3. 深度打磨全部弹窗与组件细节，统一字体与按钮层级，规范内边距留白，彻底消除字符溢出与视窗抖动；4. 传输任务入口优化至左侧导航轨，并精确对齐首页与上传按钮水平中线；5. 全面适配夜间模式高对比度视觉；6. 兼容局域网 HTTP 非安全环境下的 CDN 极速直链复制"
"""
    with open(os.path.join(pkg_root, "manifest"), "w", encoding="utf-8", newline="\n") as f:
        f.write(manifest_content)

    # config/privilege
    privilege_content = """{
    "defaults": {
        "run-as": "root"
    }
}
"""
    with open(os.path.join(pkg_root, "config", "privilege"), "w", encoding="utf-8", newline="\n") as f:
        f.write(privilege_content)

    # config/resource
    resource_content = "{}\n"
    with open(os.path.join(pkg_root, "config", "resource"), "w", encoding="utf-8", newline="\n") as f:
        f.write(resource_content)

    # cmd/main
    cmd_main = """#!/bin/bash

APP_PORT=8998

# Dynamically resolve TARGET_DIR (app payload/scripts)
if [ -n "$TRIM_APPDEST" ] && [ -d "$TRIM_APPDEST" ]; then
    TARGET_DIR="$TRIM_APPDEST"
elif [ -d "/var/apps/guangyapan/target" ]; then
    TARGET_DIR="/var/apps/guangyapan/target"
else
    TARGET_DIR=$(ls -d /vol*/@appcenter/guangyapan 2>/dev/null | head -n 1)
    [ -z "$TARGET_DIR" ] && TARGET_DIR="/vol1/@appcenter/guangyapan"
fi

# Dynamically resolve DATA_DIR (app runtime data/logs/pid)
if [ -n "$TRIM_PKGVAR" ] && [ -d "$TRIM_PKGVAR" ]; then
    DATA_DIR="$TRIM_PKGVAR"
elif [ -d "/var/apps/guangyapan/var" ]; then
    DATA_DIR="/var/apps/guangyapan/var"
else
    DATA_DIR=$(ls -d /vol*/@appdata/guangyapan 2>/dev/null | head -n 1)
    [ -z "$DATA_DIR" ] && DATA_DIR="/vol1/@appdata/guangyapan"
fi

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
    pkill -9 -f "guangyapan.*main\\.py" 2>/dev/null || true
    pkill -9 -f "@appdata/guangyapan/main\\.py" 2>/dev/null || true
    pkill -9 -f "@appcenter/guangyapan/main\\.py" 2>/dev/null || true
    pkill -u guangyapan -f main.py 2>/dev/null || true
    fuser -k "${APP_PORT}/tcp" 2>/dev/null || true
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
chmod -R 755 /var/apps/guangyapan/cmd /var/apps/guangyapan/ui 2>/dev/null || true
exit 0
""",
        "uninstall_init": """#!/bin/bash
pkill -9 -f "guangyapan.*main\\.py" 2>/dev/null || true
pkill -9 -f "@appdata/guangyapan/main\\.py" 2>/dev/null || true
pkill -9 -f "@appcenter/guangyapan/main\\.py" 2>/dev/null || true
fuser -k 8998/tcp 2>/dev/null || true
exit 0
""",
        "uninstall_callback": "#!/bin/bash\nexit 0\n",
        "upgrade_init": """#!/bin/bash
pkill -9 -f "guangyapan.*main\\.py" 2>/dev/null || true
pkill -9 -f "@appdata/guangyapan/main\\.py" 2>/dev/null || true
pkill -9 -f "@appcenter/guangyapan/main\\.py" 2>/dev/null || true
fuser -k 8998/tcp 2>/dev/null || true
exit 0
""",
        "upgrade_callback": """#!/bin/bash
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

    with open(official_icon, "rb") as f:
        official_icon_bytes = f.read()

    # 3. Create the final .fpk archive using python tarfile
    print(f"Creating FPK package: {output_fpk} ...")
    with tarfile.open(output_fpk, "w:gz") as fpk:
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

    size_bytes = os.path.getsize(output_fpk)
    md5_hash = hashlib.md5(open(output_fpk, "rb").read()).hexdigest()
    print(f"[{arch_tag.upper()}] FPK built successfully! Size: {size_bytes} bytes, MD5: {md5_hash}")

    if is_arm:
        # Also create copies with common arm aliases for maximum compatibility
        for alias in ["aarch64", "arm"]:
            alias_fpk = os.path.join(BASE_DIR, f"guangyapan-{VERSION}-{alias}.fpk")
            shutil.copy2(output_fpk, alias_fpk)
            print(f"Created alias package: {alias_fpk}")

    return output_fpk

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Guangyapan fnOS FPK Builder")
    parser.add_argument("--arch", choices=["x86_64", "arm64", "all"], default="all", help="Target architecture (default: all)")
    args = parser.parse_args()

    if args.arch == "all":
        build_package("x86_64")
        build_package("arm64")
    else:
        build_package(args.arch)
