# Codex History Live Sync（预览版）

配合现有 Windows↔WSL 同步脚本，让 Remote WSL 的 VS Code 窗口保持打开。发现已有聊天的新内容后，等待 Codex 空闲至少 60 秒，暂停本窗口的后端，完成同步，再自动重载 VS Code 窗口。聊天面板会短暂重新加载。

此版本支持 **Windows → Remote WSL 已有聊天更新**。两端同时编辑同一个聊天时不会自动合并。现有五分钟定时任务继续处理正常同步。

个人同步配置启用 `conflict_resolution=prefer_longer` 后，冲突会自动选择轮数更多的一端，轮数相同再比较内容条目数，替换前完整备份目标旧版本。Windows 较多时，桥接插件可以在空闲后暂停自身后端、同步并重载；WSL 较多时，需等桌面端后端停止，由基础定时任务同步到 Windows。数量相同或无法可靠比较时仍显示冲突。

WSL 插件打开聊天时可能追加本地设置记录。默认策略下，同步桥会验证原正文前缀、消息缓存和元数据没有改变，才允许确认这类本地变化；正式导入前在同步锁内复查并备份基线。出现新的用户消息、工具记录、未知事件或缓存变化时，由配置的冲突策略处理。

状态栏显示实际结果：只有成功传输才显示“已同步”；其余情况显示等待空闲、未保存文件、冲突或暂无待同步内容。点击状态文字或运行 **Codex History: Check for New Messages** 可查看原因。

## 本机安装完成后的启用

1. 在 VS Code 中连接 Ubuntu，打开你的项目。安装后如命令还没有出现，先重载一次窗口。
2. 按 `F1`，搜索并运行 **Codex History: Complete Setup and Enable**。
3. 先发送或保存 Codex 输入框里尚未发送的草稿，然后选择 **启用并重载**。设置命令会保存原设置。

之后每 60 秒检查一次；正在回复、运行后台命令、有未保存文件、正在调试或多个 Codex 窗口时暂缓。官方插件没有提供读取未发送草稿的接口，预览版无法保证聊天草稿在重载中保留。

自动检查使用已有 VS Code 扩展进程，桥接进程仅在 Codex 后端运行时存在，不安装新的常驻 Windows 服务。额外内存用量尚未测量。

同一个 Codex 后端可能启动代码执行子进程。同步桥原生验证其归属后，将它们视为本窗口的一组进程；正在执行任务时仍等待空闲。独立后端、独立终端和无法验证的进程不会被忽略。暂停只清理同步桥创建的原生会话，包含其中单独进程组的工具子进程。

## 在其他机器上安装

需要 Windows x64、Remote WSL Ubuntu、两端官方 Codex 扩展、Windows Python 3.11+（带 pip），以及已配置并初始化的 codex-history-sync。先在 WSL 打开一次 VS Code，以安装 VS Code Server。

```powershell
.\install.ps1 -Config 'C:\your-folder\codex-history-sync\config.json' -WhatIf
.\install.ps1 -Config 'C:\your-folder\codex-history-sync\config.json'
```

安装器按现有配置构建本机 VSIX，安装两端扩展和启动器，但不直接修改 VS Code 设置或启用自动重载。需要执行上面的设置命令。Windows 本地窗口的“让 Codex 单独在 WSL 中运行”模式不在此预览版支持范围内；请使用 Remote WSL 窗口。

配置可以放在仓库之外，解释器的 `auto`、环境变量及相对路径沿用基础脚本的解析规则。默认控制器为此仓库的 `history_sync.py`；需要配合另一套已有部署时，给安装和恢复入口增加 `-Controller 'C:\your-folder\history_sync.py'`。安装过程会启动选定 WSL；基础定时任务仍跳过未运行的 WSL。

本机生成的 EXE/VSIX包含当前 Python 的安装路径，应在使用者机器上重新构建；开源时分发源代码。

安装位置：Windows 用户主目录下的 `.codex-history-sync-live` 和 WindowsApps 启动器；WSL 用户的 `.config/codex-history-sync-live`、`.local/share/codex-history-sync-live`、`.local/state/cdx-live`，以及 `/usr/local/bin/codex-history-sync-bridge`。用户主目录避免 Codex 桌面版的 MSIX LocalAppData 隔离，让 VS Code 和 WSL 能读取同一份检查脚本。官方插件文件不被修改。

从同步桥 0.1.0 升级时，重新运行安装器，然后在 WSL 窗口执行 **Codex History: Complete Setup and Enable**，更新保存的检查脚本路径。安装器识别原有启动器的归属；旧 LocalAppData 文件保留，启动器优先读取新的用户主目录配置。

## 关闭与异常恢复

正常关闭：运行 **Codex History: Disable and Restore Original Codex**，然后重载窗口。

如果同步失败，扩展会停用自动同步并保留必要的暂停保护。不要删除同步器的 `runtime/pending.json` 或聊天备份。先检查、完成或回滚未完成导入。确认完成后，关闭 WSL VS Code 窗口并运行：

```powershell
.\recover.ps1 -Config 'C:\your-folder\codex-history-sync\config.json'
```

恢复工具只清除已停止扩展留下的暂停记录；仍有写入任务、未完成导入或活跃后端时会拒绝恢复。重新打开 WSL 窗口，运行恢复原设置命令，再执行设置命令启用。

## 验证范围

已经验证协调逻辑、原生进程暂停、繁忙保护、失败恢复、Windows 启动器的参数与标准输入输出，以及隔离目录内的真实官方 Codex 后端。尚未通过实际 VS Code 前台面板验证重载后的聊天显示和草稿保留，因此保持预览版标记。

桥接源码采用 MIT 许可证；生成的 Windows 启动器还适用 [第三方许可证](THIRD_PARTY_NOTICES.md)。此组件使用开发用途的 Codex 自定义 CLI 设置；可以恢复原值。它不是 OpenAI 官方同步功能。
