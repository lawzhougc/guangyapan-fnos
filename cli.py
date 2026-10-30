import sys
import argparse
import os
import json
from guangyapan.client import GuangyapanClient
from guangyapan.downloader import FileDownloader
from guangyapan.web_server import run_server

def main():
    parser = argparse.ArgumentParser(description="光鸭网盘 (Guangyapan) 飞牛系统 fnOS 命令行客户端")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # server
    p_server = subparsers.add_parser("server", help="启动 Web UI 服务")
    p_server.add_argument("-p", "--port", type=int, default=8998, help="监听端口 (默认 8998)")
    p_server.add_argument("-c", "--config", default="config.json", help="配置文件路径")

    # list
    p_list = subparsers.add_parser("list", help="查看网盘文件列表")
    p_list.add_argument("parent_id", nargs="?", default="", help="父文件夹 ID (留空为根目录)")

    # download
    p_dl = subparsers.add_parser("download", help="下载网盘文件到本地")
    p_dl.add_argument("file_id", help="文件 ID")
    p_dl.add_argument("-o", "--output", help="本地保存路径或目录")

    # magnet
    p_mag = subparsers.add_parser("magnet", help="云添加磁力链接任务")
    p_mag.add_argument("url", help="magnet:?xt=urn:btih: 磁力链接或 HTTP 下载链接")
    p_mag.add_argument("-d", "--dir", default="", help="保存到网盘文件夹 ID")
    p_mag.add_argument("-n", "--name", help="重命名任务名称")

    # torrent
    p_tor = subparsers.add_parser("torrent", help="云添加 BT 种子文件任务")
    p_tor.add_argument("torrent_path", help="本地 .torrent 文件路径")
    p_tor.add_argument("-d", "--dir", default="", help="保存到网盘文件夹 ID")

    # tasks
    p_tasks = subparsers.add_parser("tasks", help="查看云离线下载任务列表与进度")

    # upload
    p_up = subparsers.add_parser("upload", help="上传本地文件到光鸭网盘 (支持极速秒传)")
    p_up.add_argument("file_path", help="要上传的本地文件路径")
    p_up.add_argument("-d", "--dir", default="", help="网盘目标文件夹 ID")
    p_up.add_argument("-n", "--name", help="网盘中显示的文件名")

    # mkdir
    p_mkdir = subparsers.add_parser("mkdir", help="在光鸭网盘中新建文件夹")
    p_mkdir.add_argument("name", help="新文件夹名称")
    p_mkdir.add_argument("-d", "--dir", default="", help="父文件夹 ID (留空表示根目录)")

    args = parser.parse_args()

    if not args.command or args.command == "server":
        port = getattr(args, "port", 8998)
        config = getattr(args, "config", "config.json")
        run_server(port=port, config_path=config)
        return

    client = GuangyapanClient(getattr(args, "config", "config.json"))

    if args.command == "list":
        res = client.list_files(parent_id=args.parent_id)
        files = res.get("list", [])
        print(f"\n📂 文件总数: {len(files)}")
        print(f"{'ID':<22} | {'类型':<4} | {'大小':<10} | {'文件名'}")
        print("-" * 75)
        for f in files:
            is_dir = (f.get("resType") == 2)
            t_str = "目录" if is_dir else "文件"
            sz_str = "--" if is_dir else f"{f.get('fileSize', 0)/(1024*1024):.1f}MB"
            print(f"{f.get('fileId'):<22} | {t_str:<4} | {sz_str:<10} | {f.get('fileName')}")

    elif args.command == "download":
        info = client.get_download_url(args.file_id)
        signed_url = info.get("signedURL")
        if not signed_url:
            print("❌ 获取下载直链失败:", info)
            return
        
        out = args.output
        if not out:
            out = f"./download_{args.file_id}"
        elif os.path.isdir(out):
            out = os.path.join(out, f"download_{args.file_id}")

        print(f"⬇️ 开始下载: {out}")
        downloader = FileDownloader(use_aria2=True)
        ok = downloader.download(signed_url, out, lambda p: print("进度:", p))
        if ok:
            print(f"✅ 下载完成: {out}")
        else:
            print(f"❌ 下载失败")

    elif args.command == "magnet":
        res = client.cloud_add_magnet(args.url, parent_id=args.dir, new_name=args.name)
        if res.get("data") and res["data"].get("taskId"):
            print(f"✅ 磁链任务已创建! TaskID: {res['data']['taskId']}")
        else:
            print(f"❌ 任务创建失败: {res}")

    elif args.command == "torrent":
        with open(args.torrent_path, "rb") as f:
            t_bytes = f.read()
        res = client.cloud_add_torrent(t_bytes, filename=os.path.basename(args.torrent_path), parent_id=args.dir)
        if res.get("data") and res["data"].get("taskId"):
            print(f"✅ BT种子任务已创建! TaskID: {res['data']['taskId']}")
        else:
            print(f"❌ BT任务创建失败: {res}")

    elif args.command == "tasks":
        res = client.list_cloud_tasks()
        tasks = res.get("list", [])
        print(f"\n📋 离线任务总数: {res.get('total', len(tasks))}")
        print(f"{'任务ID':<22} | {'进度':<6} | {'状态':<6} | {'任务名称'}")
        print("-" * 75)
        for t in tasks:
            status = "已完成" if (t.get("status") == 2 and t.get("progress") == 100) else f"进行中"
            if t.get("status") == 3: status = "失败"
            print(f"{t.get('taskId'):<22} | {str(t.get('progress', 0))+'%':<6} | {status:<6} | {t.get('fileName')}")

    elif args.command == "upload":
        print(f"⬆️ 开始上传: {args.file_path}")
        def p_cb(p):
            stage = p.get("stage")
            pct = p.get("percent", 0)
            if stage == "hash_ready":
                print(f"[1/3] 哈希计算完成 (GCID/CID)")
            elif stage == "uploading":
                print(f"[2/3] 上传中: {pct}% ({p.get('uploaded',0)}/{p.get('total',0)} bytes)")
            elif stage == "verifying":
                print(f"[3/3] 云端校验中...")
            elif stage == "completed":
                print(f"✅ 秒传命中! 100%")

        res = client.upload_file(args.file_path, parent_id=args.dir, custom_name=args.name, progress_callback=p_cb)
        print("✅ 上传成功! 文件信息:", res)

    elif args.command == "mkdir":
        res = client.create_folder(args.name, parent_id=args.dir)
        code = res.get("code")
        if code == 0 or res.get("msg") == "success":
            print(f"✅ 文件夹创建成功: {args.name}")
            if "data" in res and "fileId" in res["data"]:
                print(f"📁 文件夹 ID: {res['data']['fileId']}")
        else:
            print(f"❌ 文件夹创建失败: {res.get('msg', '未知错误')} (代码: {code})")

if __name__ == "__main__":
    main()
