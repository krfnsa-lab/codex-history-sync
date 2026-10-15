# 手动处理冲突

两端都续聊后，点击状态栏检查，再选择“处理聊天冲突”；也可以按 F1 运行 **Codex History: Resolve Chat Conflicts**。

选择聊天后，选择“以 Windows 为准”或“以 WSL 为准”，核对轮数并确认。最终两端显示选定版本的同一条聊天，不生成新副本；选定的一端不重写，只更新另一端。另一端独有消息将从当前聊天中被替换，因此确认前应检查内容。

工具首先完整导出双方正文、缓存和元数据到配置运行目录的 `conflict-choices/<choice-id>/windows` 与 `wsl`。选择及两端精确版本记录在 `conflict-choices.json`；基线、备份和凭据不会发布到 GitHub。备份不自动删除。

保存选择不代表已经同步。两条记录都须静默至少 60 秒；目标系统的 Codex 后端须停止，导入时再次核对目标版本。Windows → WSL 可通过状态栏检查，在保存文件和未发送草稿后确认暂停、同步及重载。WSL → Windows 须关闭桌面端 Codex 后端，由已有定时任务处理；插件不自动关闭桌面应用。

原历史的工作目录还必须能映射为目标系统中存在的目录。Windows 盘符与 WSL 的 `/mnt/盘符/` 可相互转换；WSL 独有的 Linux 用户目录不能自动映射到 Windows 时会暂缓，不擅自改成用户主目录或扩大工作区权限。这种情况仅停止后端并不足以完成同步，应先检查工作目录兼容性。

等待期间任何一端又有新消息或其他版本变化，旧选择失效，必须重新查看并确认。不会把新消息自动归入旧选择。成功导入并收到提交回执后才更新基线、标记选择已完成。两端环境路径可能不同，消息和工具内容保持选定版本。

## 命令行

在 Windows 原生 Python 中使用配置对应的控制器：

```powershell
python .\merge_chat.py --controller .\history_sync.py --config .\config.json --list
python .\merge_chat.py --controller .\history_sync.py --config .\config.json --id <chat-id> --keep-side windows --expected-token <choice-token>
python .\merge_chat.py --controller .\history_sync.py --config .\config.json --id <chat-id> --keep-side wsl --expected-token <choice-token>
.\sync-now.ps1 -DryRun
.\sync-now.ps1
```

`choice-token` 必须取自最近一次 `--list` 返回的对应聊天，它绑定双方版本，不能跳过。手动与定时同步共享运行锁。运行中的同步、未完成的导入、不完整正文、版本变化等都会阻止替换；未知异常保留恢复保护。

导入中断时，不要删除 `runtime/pending.json`、选择记录或原生 `.history-sync-backups`。保留完整运行目录供恢复检查。旧版本已有的合并副本及原聊天不会因升级被删除；旧合并任务的显式恢复入口见 [旧合并工具说明](MERGING.md)。
