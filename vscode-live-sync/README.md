# Codex History Sync 1.0.0

在 VS Code 的 Remote WSL 窗口检查 Windows 聊天更新，提供手动应用及冲突选端入口。基础同步脚本版本为 2.1.0。

## 日常使用

启用后，每 60 秒自动检查。默认 reloadMode=confirm：后台检查不弹窗、不暂停后端、不重载；发现可应用更新时，状态栏提示等待确认。点击右下角的 Codex 状态，确认未发送的聊天草稿已保存后，才完成同步并重载窗口。

两端都续聊时提示冲突。点击检查提示中的“处理聊天冲突”，或按 F1 运行 **Codex History: Resolve Chat Conflicts**，选择聊天，再选择“以 Windows 为准”或“以 WSL 为准”。完整备份双方后保存选择，安全导入后两端统一为选定版本，聊天 ID 不变，不生成副本。另一端独有消息会被选定内容替换，但完整备份仍保留；确认前请核对。

“选择已保存”表示等待安全同步，不是已同步。任何一端在等待期间变化都必须重新选择。Windows → WSL 选择可随后点击状态栏确认应用；WSL → Windows 选择等待桌面后端停止，再由已有定时任务处理。详见[手动处理冲突](../docs/CONFLICTS.md)。

普通同步、冲突选择、设置及恢复操作互斥。后台定时任务共享同步锁；正在回复、运行工具、未保存编辑文件、调试、多个窗口或无法确认进程归属时等待。确认记录数实际传输后才显示“已同步”，没有传输的检查显示等待原因。

可选 reloadMode=automatic 需要主动选择，并通过设置命令或首次手动检查明确接受自动重载。Codex 没有读取未发送草稿的接口，自动模式无法保证这些草稿保留；默认确认模式因此不会无人值守地重载窗口。

## 安装与升级

先完成根目录配置、初始化和同步预览，再从项目根目录运行：

```powershell
.\vscode-live-sync\install.ps1 -Config .\config.json -WhatIf
.\vscode-live-sync\install.ps1 -Config .\config.json
```

安装器构建当前机器的 VSIX、安装两端插件和桥接启动器，不直接启用同步。已有部署可加 -Controller 'C:\your-folder\history_sync.py'。安装器会启动选定 WSL；基础定时任务仍跳过停止的 WSL。

保存文件和聊天草稿，重载一次 Remote WSL 窗口，按 F1 运行 **Codex History: Complete Setup and Enable**。默认选择“启用自动检查”。以后每次打开窗口不需要重复安装或初始化。

升级保留个人配置、完整运行目录和备份，使用新源码重新运行安装器，再重载窗口。已启用的旧用户默认转为确认后应用；自动重载需主动接受。安装器原子替换辅助文件，相同内容的运行中 Windows 启动器不重复替换。

入口：**Check for New Messages** 检查/应用、**Resolve Chat Conflicts** 选择保留版本、**Enable Automatic Sync** 启用检查、**Disable Automatic Sync** 停用检查、**Disable and Restore Original Codex** 恢复原设置。

## 安装位置与资源

Windows 辅助程序在用户目录 .codex-history-sync-live，WindowsApps 中安装启动器。WSL 辅助程序在 .config/codex-history-sync-live、.local/share/codex-history-sync-live、.local/state/cdx-live，启动器在 /usr/local/bin/codex-history-sync-bridge。官方插件文件不被修改。

自动检查使用已有 VS Code 扩展进程；桥接进程随 Codex 后端运行，不添加常驻 Windows 服务。扫描、备份与同步会临时启动 Python；内存受聊天和附件大小影响，不能保证固定小内存。源代码不分发嵌入个人 Python 路径的预编译 EXE/VSIX。

## 异常恢复

失败后保留必要的暂停和未完成导入保护。只有当前窗口唯一健康空闲后端、无暂停文件、Windows 没有正在写入或待恢复的导入时，才恢复自动检查；手动停用会阻止恢复。恢复检查自身不导入或重载。

旧版本的合并任务仍保留两端原始包、验证结果和提交回执，可通过根目录的旧命令行工具显式继续；新版插件不生成新副本，也不自动删除以前的副本。

其他导入异常不要删除 runtime/pending.json 或原备份。检查并完成恢复后，关闭 WSL 窗口运行 recover.ps1 -Config 'C:\your-folder\config.json'，再恢复原设置或重新启用。恢复工具不会绕过活动写入或未完成导入保护。

## 支持边界

插件的已有聊天协调方向为 **Windows → Remote WSL**。WSL → Windows 的已有更新和手动选择仍等待桌面后端停止，由基础任务处理。Windows 本地窗口中的“单独让 Codex 在 WSL 运行”模式不属于此插件支持范围。

原生协议、隔离数据目录、配置/并发/恢复逻辑有测试覆盖；用户的前台聊天重绘和附件视觉渲染不在自动测试证明范围内。首次应用后检查聊天内容。升级后的未知原生格式会停止处理并保留记录。

MIT 社区插件，非 OpenAI 官方同步功能。生成的 Windows 启动器另适用[第三方许可证](THIRD_PARTY_NOTICES.md)。
