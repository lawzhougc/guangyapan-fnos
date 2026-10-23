# 光鸭网盘 fnOS 原生适配版 · 项目上下文与架构演进文档 (CONTEXT.md)

> **文档生成时间**: 2026-10-23  
> **当前软件版本**: `v1.0.6`  
> **项目仓库**: [https://github.com/lawzhougc/guangyapan-fnos](https://github.com/lawzhougc/guangyapan-fnos)  
> **开发者 / 适配维护者**: `chaos`  
> **适配目标系统**: 飞牛私有云 (fnOS / Linux Debian 底层)  
> **支持硬件架构**: `x86_64` (Intel/AMD)、`arm64` / `aarch64` (RK3588, RK3568 等)  
> **实机验证环境**: `192.168.1.3:8998` (MEPro fnOS 设备, `guangyapan.service`)

---

## 1. 项目定位与核心价值

光鸭网盘是一款新兴的高速网盘与离线下载服务，拥有极高的上下行吞吐带宽与大容量云存储空间。然而官方暂无针对 NAS 系统的原生客户端。

本项目是专为**飞牛私有云 (fnOS)** 量身打造的**原生套件客户端 (FPK)**，具备以下核心能力：
1. **无需 Docker**：纯原生轻量服务，开箱即用，内存常驻占用小于 20MB。
2. **极速下载引擎**：深度集成 `aria2c` 16 线程断点续传（兼备 Python 原生无缝回退），榨干宽带与网盘 CDN 速率。
3. **极速秒传与分片直传**：基于官方迅雷哈希算法秒传，未命中时直传阿里云 OSS。
4. **云端离线下载管理**：深度支持磁力链 (Magnet) 与 BT 种子解析，在 NAS 上即可调度光鸭云端高速离线下载。
5. **全端响应式界面**：完美适配 fnOS 桌面端浏览器与手机端飞牛 App，支持深色/浅色主题。

---

## 2. 系统核心架构与模块职责

```
guangyapan_fnos/
├── build_fpk.py             # fnOS 官方规范 FPK 打包器 (构建 x86_64/arm64 独立安装包)
├── cli.py                   # 命令行交互工具 (CLI 启动、管理、离线、下载、上传)
├── manifest                 # 飞牛应用中心元数据与安装清单
├── ui/                      # 桌面图标资源与配置 (icon.png, icon_256.png)
└── guangyapan/
    ├── __init__.py          # 模块入口
    ├── client.py            # 光鸭网盘云端 API 交互层 (逆向对接与核心业务协议)
    ├── downloader.py        # 下载引擎 (aria2c 进程控制、进度解析、权限修复与 Python 回退)
    ├── web_server.py        # HTTP 路由与内嵌式 SPA 单页 Web 前端 (HTML/CSS/JS 全集成)
    ├── qrcode.min.js        # 登录二维码前端生成组件
    └── ...
```

### 2.1 后端服务层
- **`guangyapan/client.py`**:
  - **身份认证机制**：基于官方 Device Flow 与扫码登录，持久化 Token/Cookie 机制，支持过期自动感知与注销。
  - **文件系统接口**：目录浏览 (`list_files`)、新建文件夹 (`create_folder`)、删除文件/目录 (`delete_file`)、获取 CDN 极速直链 (`get_download_url`)。
  - **云添加引擎**：
    - 资源解析：`/cloudcollection/v1/resolve_res`（解析磁链/URL）与 `/cloudcollection/v1/resolve_torrent`（解析 BT 种子）。
    - 任务创建：`/cloudcollection/v1/create_task`，支持传入 `parentId`（指定网盘目录）、`fileIndexes`（按需筛选文件）、`newName`（重命名）。
    - 任务控制：`/cloudcollection/v2/delete_task`（彻底删除并支持联动清理网盘文件）、`/cloudcollection/v2/retry_task`（重试/继续任务）、`/cloudcollection/v2/pause_task`（暂停任务）。
- **`guangyapan/downloader.py`**:
  - **aria2c 加速模式**：动态拉起 `aria2c` 独立进程，配置 `--continue=true --allow-overwrite=true --auto-file-renaming=false -s16 -x16 -k1M`，实时正则捕获 stdout 提取百分比、瞬时速率、已下载量与 ETA。
  - **Python 回退模式**：若系统未安装 aria2c，使用原生 `urllib.request` 进行 512KB 分块流式拉取。
  - **存储权限治理**：下载完毕后统一执行 `fix_permissions` (0o777 / 0o666)，解决 NAS 多用户/SMB 共享写入后的只读权限冲突。
- **`guangyapan/web_server.py`**:
  - 基于 Python 标准库 `ThreadingHTTPServer`，零外部依赖，极速启动。
  - RESTful API 路由全面覆盖用户状态、网盘文件、本地传输任务、离线任务管理、NAS 目录探测与文件冲突预检 (`/api/fs/check-file`)。

### 2.2 前端用户界面 (SPA)
- **单文件高集成度**：HTML/CSS/JS 全量内嵌在 `web_server.py`，杜绝文件静态路径缺失问题，响应极快。
- **树状网盘目录选择器 (`cloudFolderPickerModal`)**：
  - 多级目录展开/折叠 (`▼` / `▶`)，按需懒加载子目录并本地缓存。
  - 自由层级回溯，彻底解决深层目录无法向上一层的问题。
  - 顶部显示当前位置与 `默认` 橙色标签，支持一键「恢复默认」与就地「新建文件夹」。
- **弹窗防冲突与交互安全**：
  - `duplicateFileModal`：下载到 NAS 发现同名文件时，弹出专属警告框，提供“保留两份（智能重命名）”、“覆盖原文件”、“取消下载”三种处理策略。
  - `deleteCloudTaskModal`：彻底替换原生的系统 alert，显示任务名称并提供是否“同时删除网盘已下载文件”勾选框。
- **飞牛移动端适配**：断点 `@media (max-width: 768px)`，提供顶栏抽屉菜单与移动端快捷操作栏。

---

## 3. 实现进度与版本里程碑

| 版本 | 发布日期 | 核心实现与解决的问题 |
| :--- | :--- | :--- |
| **v1.0.1** | 2026-10-22 | 完成光鸭官方 API 逆向与基本原型跑通；实现极速秒传算法。 |
| **v1.0.2** | 2026-10-22 | 接入 `aria2c` 多线程下载加速；实现 NAS 后台静默下载任务管理。 |
| **v1.0.3** | 2026-10-23 | 适配 fnOS 原生 FPK 架构；彻底移除调试阶段的个人登录硬编码信息，实现开源安全分发；提取正版光鸭图标。 |
| **v1.0.4** | 2026-10-23 | 优化亮色模式色彩对比度；移除二维码多余保存按钮；修复跨存储卷（`/vol1`, `/vol2`）共享目录无法浏览的问题；设置中增加“关于”与 GitHub 检查更新卡片。 |
| **v1.0.5** | 2026-10-23 | 1. 深度复刻官方云添加解析界面（BT/磁链分文件解析、分类过滤、重命名）；<br>2. 官方树状网盘目录选择器（多级展开折叠、自由返回任意层级、一键恢复默认）；<br>3. 离线任务管理支持暂停/继续与彻底删除（联动删网盘文件）；<br>4. 下载到 NAS 本地同名文件防冲突弹窗（覆盖/智能重命名）。 |
| **v1.0.6** | 2026-10-24 | **当前里程碑**：<br>1. **1:1 官方原版视觉体验深度复刻**：完整采用光鸭官方设计规范（72px 极简左侧导航轨、顶栏矢量品牌标组合、官方呆萌鸭空状态插画、全部/最近/视频/图片/文档/音频/安装包分类标签及实时搜索过滤）；<br>2. **在线视频直链流式播放**：支持一键调用 CDN 极速直链在浏览器内免下载直接在线观影（支持倍速、全屏、画中画）；<br>3. **超级会员与容量胶囊**：展示 SVIP 黄金身份标识与实时云盘容量微进度胶囊。 |

---

## 4. 关键技术细节与避坑总结

1. **光鸭离线删除 API 参数规范**：
   - 官方最新 `/cloudcollection/v2/delete_task` 要求入参为 `{"taskIds": [taskId], "deleteFile": bool}`。若传递单值字符串 `{"taskId": taskId}`，后端虽返回 200 但不会触发删除逻辑。
2. **离线任务暂停服务端限制**：
   - 调用 `/cloudcollection/v2/pause_task` 时，云端对部分下载中的活跃任务会返回 HTTP 410（`code: 128, msg: "路径不允许访问"`），表明云端节点暂不支持客户端单向中断连接。前端已对此作了友好提示；重试/继续接口 (`/cloudcollection/v2/retry_task`) 则表现完全正常。
3. **飞牛 NAS 目录与权限穿透**：
   - fnOS 存储卷分属 `/vol1`, `/vol2` 等独立分区。为保证所有共享目录均能被浏览，目录树首层必须读取系统 mount 信息与所有可读共享文件夹根节点。
4. **aria2c 同名文件写入保护**：
   - 必须配置 `--allow-overwrite=true` 与 `--auto-file-renaming=false`，否则已完成的文件重新发起下载时，aria2c 会触发 `errorCode=13 Cannot overwrite existing file` 报错或生成 `.1` 后缀文件。

---

## 5. 待办事项与演进规划 (TODO)

- [ ] **社区宣发**：在飞牛官方论坛 (fnOS 社区) 发帖分享该套件，提供 GitHub Release 直链与安装指引。
- [ ] **批量离线提交**：支持多行磁力链接/文本批量解析与自动提交离线任务。
- [x] **在线视频直链播放**：Web 界面内嵌 HTML5 直链播放器，调用 signedURL 实现免下载在线串流观影（v1.0.6 已完成）。
- [ ] **通知推送集成**：支持下载完成事件 Webhook 推送（微信 Server酱、Bark、Telegram Bot、钉钉群机器人）。
- [ ] **NAS 本地定时同步备份**：支持定时将 NAS 指定的照片/文档目录自动增量秒传备份至光鸭网盘。
