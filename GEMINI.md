# Guangyapan fnOS 项目核心规则与开发记忆 (GEMINI.md)

## 1. 自动同步至 NAS 与 Git 远程推送规则 (用户铁律)
> [!IMPORTANT]
> 1. **默认完成修改立即部署 NAS**：在本地完成任何代码特性、Bug 修复或优化后，**必须立即自动执行 `python deploy_to_nas.py` 同步最新代码与 FPK 到用户的 NAS 实机并重启 `guangyapan.service` 服务**，确保用户随时能直接在真机环境中测试验证！
> 2. **未经用户明确要求严禁 push**：未经用户在 NAS 实机测试完成并明确下达指令前，切勿自动执行 `git push` 推送到 GitHub 远程仓库！
> 
> - **标准开发流程**：
>   1. 本地完成特性开发、测试与 FPK 打包；
>   2. 立即运行 `python deploy_to_nas.py` 自动推送至 NAS 并热重载服务；
>   3. 提示用户已部署至 NAS，由用户在真机环境中亲自测试验证；
>   4. **必须等待用户明确要求“推送到 GitHub”或测试通过后，方可执行 `git push`**。

---

## 2. NAS 环境与服务信息
- **Web 服务端口**: `8998`
- **系统服务**: `guangyapan.service`
- **关键路径**:
  - 运行目录: `/vol1/@appdata/guangyapan/` 与 `/vol1/@appcenter/guangyapan/`
  - FPK 安装包共享目录: `/vol1/1000/`
