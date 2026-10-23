# Guangyapan fnOS 项目核心规则与开发记忆 (AGENTS.md)

## 1. Git 远程推送规则 (用户铁律)
> [!IMPORTANT]
> **未经用户在 NAS 实机测试完成并明确下达指令前，切勿自动执行 `git push` 推送到 GitHub 远程仓库！**
> 
> - **标准发布流程**：
>   1. 本地完成特性开发、测试与 FPK 打包；
>   2. 自动同步更新并推送到用户的 NAS 实机，热重载服务；
>   3. 提示用户已部署至 NAS，由用户在真机环境中亲自测试验证；
>   4. **必须等待用户明确要求“推送到 GitHub”或测试通过后，方可执行 `git push`**。

---

## 2. NAS 环境与服务信息
- **Web 服务端口**: `8998`
- **系统服务**: `guangyapan.service`
- **关键路径**:
  - 运行目录: `/vol1/@appdata/guangyapan/` 与 `/vol1/@appcenter/guangyapan/`
  - FPK 安装包共享目录: `/vol1/1000/`
