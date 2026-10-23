# 光鸭网盘 fnOS 飞牛私有云官方适配版套件

<p align="center">
  <img src="ui/images/icon_256.png" width="128" height="128" alt="光鸭网盘 fnOS" style="border-radius: 24px;">
</p>

<p align="center">
  <b>专为飞牛私有云 (fnOS) 打造的光鸭网盘原生套件与 Web 管理端</b><br>
  高速上传下载 · 官方扫码登录 · 磁链/BT云离线 · NAS目录直选 · 昼夜主题自适应 · 移动端专项优化
</p>

---

## 🌟 核心特性

- 🦆 **官方原版视觉体验**：深度还原 Windows 客户端的精致交互与排版风格，采用官方原版橙底立体光鸭 Logo，提供纯正的视觉享受。
- 📱 **官方 OAuth 扫码登录**：完整实现 RFC 8628 Device Authorization Grant 协议，打开即可生成专属授权二维码，手机光鸭 APP 或微信扫码即可安全登入；同时支持 Token 导入与一键登出。
- 📁 **NAS 目录无缝直选**：内置可视化目录层级穿透与选择器，支持直接将网盘文件下载至 `/vol1`、`/vol2` 等指定 NAS 本地文件夹，无需中转。
- ⚡ **云端离线任务管理**：支持磁力链接（`magnet:`）、HTTP 直链及本地 `.torrent` 种子文件批量云端离线，实时同步状态与任务进度。
- 🚀 **极速秒传与文件上传**：基于迅雷哈希算法精确计算文件特征，实现极速秒传；秒传未命中时自动分片直传云端。
- 🌓 **日间 / 夜间双主题自由切换**：提供 Windows 风格日光浅灰与沉浸式夜间深色两种专业配色，支持右上角一键无缝切换并记忆状态。
- 📱 **飞牛手机 APP 响应式优化**：针对飞牛手机客户端内嵌 Web 视图及小屏移动设备深度定制，自适应安全边距与抽屉导航，彻底杜绝元素截断。
- 🛡️ **原生 fnOS 套件规范**：完全遵循 fnOS 第三方套件标准架构，无缝集成桌面快捷图标、应用中心管理（打开、停用、卸载及稳定自启）。

---

## 📂 项目结构

```text
guangyapan_fnos/
├── guangyapan/                 # 核心模块
│   ├── __init__.py
│   ├── client.py               # 光鸭开放接口、OAuth 设备流与网盘 API
│   ├── downloader.py           # 高并发分片下载与 NAS 落地管理
│   ├── hash_utils.py           # 极速秒传哈希算法 (Thunder/Xunlei 算法)
│   ├── web_server.py           # Web 服务、响应式界面与 REST API
│   ├── icon.png                # 官方 256x256 橙色光鸭品牌图标
│   └── qrcode.min.js           # 离线二维码生成引擎
├── cmd/                        # 飞牛套件系统控制脚本
│   ├── main                    # 服务启停、重载与基于 TCP Socket 的健康巡检
│   ├── install_callback        # 安装后用户/权限初始化
│   ├── install_init
│   ├── uninstall_init          # 卸载清理脚本
│   └── ...
├── config/                     # 套件权限与资源配置
│   ├── privilege               # 指定以 package (guangyapan) 权限运行
│   └── resource
├── ui/                         # 桌面端元数据与图标资源
│   ├── config                  # 飞牛桌面快捷启动入口注册配置
│   └── images/                 # 多分辨率高清图标 (16/32/64/128/256/512px)
├── main.py                     # Web 服务启动入口
├── cli.py                      # 独立命令行调试工具
├── config.example.json         # 配置文件范例
├── manifest                    # 套件安装元数据
├── build_fpk.py                # 一键编译打包 .fpk 工具
└── README.md
```

---

## 🚀 快速上手与运行

### 方式一：作为独立服务运行 (本地/Docker/Linux)

1. 克隆代码仓库：
   ```bash
   git clone https://github.com/lawzhougc/guangyapan-fnos.git
   cd guangyapan-fnos
   ```

2. 准备配置文件：
   ```bash
   cp config.example.json config.json
   ```

3. 启动 Web 服务：
   ```bash
   python3 main.py
   # 或使用命令行工具自定义端口与配置
   python3 cli.py server -p 8998
   ```
   启动后即可在浏览器访问 `http://<NAS_IP>:8998`。

---

### 方式二：构建并安装为 fnOS 原生套件 (`.fpk`)

1. **一键打包**：
   在项目根目录下执行打包脚本（支持 `--arch x86_64`、`--arch arm64` 或 `--arch all` 双架构同时生成）：
   ```bash
   # 同时打包 x86_64 与 arm64 架构
   python3 build_fpk.py --arch all

   # 或单独生成指定架构
   python3 build_fpk.py --arch x86_64
   python3 build_fpk.py --arch arm64
   ```
   脚本将自动校验图标资源、写入目标架构清单（`platform = x86` 或 `platform = arm`）、压缩载荷并输出标准的安装包：
   - `guangyapan-1.1.0-x86_64.fpk`（Intel / AMD 处理器）
   - `guangyapan-1.1.0-arm64.fpk`（RK3588 / RK3568 / ARM 芯片）

2. **在飞牛私有云 (fnOS) 中安装**：
   - 打开飞牛 NAS 桌面，进入 **应用中心**。
   - 点击右上角或设置中的 **手动安装**。
   - 选择打包生成的 `guangyapan-1.1.0-x86_64.fpk` 文件并确认。
   - 安装完成后，飞牛桌面与应用中心将自动生成光鸭网盘图标，点击即可直接进入 Web 管理端。

---

## 🛠️ 命令行 (CLI) 常用指令

`cli.py` 提供丰富的命令行操作：

```bash
# 启动 Web 界面
python3 cli.py server -p 8998

# 列出网盘文件
python3 cli.py list

# 下载网盘文件至 NAS 指定路径
python3 cli.py download <FILE_ID> -o /vol1/1000/Movies

# 云添加磁力链接下载任务
python3 cli.py magnet "magnet:?xt=urn:btih:..."

# 云添加本地 BT 种子下载任务
python3 cli.py torrent /path/to/file.torrent

# 查看云离线下载进度
python3 cli.py tasks

# 上传本地文件到网盘 (支持极速秒传)
python3 cli.py upload /path/to/local/file.mp4
```

---

## 📝 更新日志 (Changelog)

### v1.1.0 (2026-10-24)
- 🎨 **1:1 深度复刻光鸭官方现代 UI 体系**：采用官方主色、72px 极简左侧 Dock 导航轨、顶栏品牌标、官方呆萌鸭空状态插画。
- 🏷️ **原生分类标签与实时检索**：顶部原生支持全部分类（全部、最近、视频、图片、文档、音频、安装包）及实时搜索过滤。
- 📐 **界面微距精调与几何对齐**：左侧「首页」按钮中线与右侧「上传文件」按钮中线实现像素级水平对齐。
- 📦 **弹窗细节与视觉规范重构**：全面规范下载确认、文件上传、NAS 保存目录选择等弹窗的边距留白与布局，彻底根除文字挤压溢出与视窗抖动；修复目录选择器重复图标问题。
- ⬇️ **传输任务体验升级**：传输与下载入口移至左侧导航轨（置于云添加与回收站之间），联动动态红点徽标。
- 🌙 **全景深色模式深度适配**：修复夜间模式下顶栏品牌文字的对比度，适配高辨识度矢量展示。
- 🔗 **局域网 CDN 直链兼容**：适配 HTTP 非安全上下文下的“一键复制直链”剪贴板自动降级。

### v1.0.5 (2026-10-23)
- ⚡ **深度复刻光鸭官方云添加解析界面**：支持磁力链及 BT 种子深度解析，自动提取多文件树，智能识别视频与字幕等分类文件，支持批量分类勾选及重命名任务标题。
- 📁 **全新树状网盘目录选择器**：保存位置支持树状多级目录展开与折叠，解决深层跳转及层级回溯，支持一键“恢复默认”及就地“新建网盘文件夹”。
- ⏸️ **离线任务暂停与继续**：离线任务支持主动暂停与继续重试。
- 🗑️ **离线任务真实彻底删除**：重构删除交互与云端 API 传参，提供专属确认弹窗并支持“同时删除网盘已下载的对应文件”。
- ⚠️ **下载到 NAS 本地同名文件冲突检测**：下载时若本地目标路径已存在同名文件，自动弹出防冲突预警，支持“保留两份（智能重命名）”与“覆盖原文件”。
- 🎨 **界面细节与体验调优**：优化亮色/暗色模式对比度与视觉层次，提升日常操作体验。

---

## 👤 作者与维护者

- **开发者 / 适配维护者**: chaos
- **项目仓库**: [https://github.com/lawzhougc/guangyapan-fnos](https://github.com/lawzhougc/guangyapan-fnos)

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。
Copyright (c) 2026 chaos.
