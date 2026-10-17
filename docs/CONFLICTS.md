# 手动处理冲突

两端都续聊后，点击状态栏检查，再选择“处理聊天冲突”；也可以按 F1 运行 **Codex History: Resolve Chat Conflicts**。

选择聊天后，选择“以 Windows 为准”或“以 WSL 为准”，核对轮数并确认。最终两端显示选定版本的同一条聊天，不生成新副本；选定的一端不重写，只更新另一端。另一端独有消息将从当前聊天中被替换，因此确认前应检查内容。

工具首先完整导出双方正文、缓存和元数据到配置运行目录的 `conflict-choices/<choice-id>/windows` 与 `wsl`。选择及两端精确版本记录在 `conflict-choices.json`；基线、备份和凭据不会发布到 GitHub。备份不自动删除。

保存选择不代表已经同步。两条记录都须静默至少 60 秒；目标系统的 Codex 后端须停止，导入时再次核对目标版本。Windows → WSL 可通过状态栏检查，在保存文件和未发送草稿后确认暂停、同步及重载。WSL → Windows 默认须自行关闭桌面端 Codex 后端，由已有定时任务处理；也可主动启用以下自动退出设置。

原历史的工作目录还必须能映射为目标系统中存在的目录。Windows 盘符与 WSL 的 `/mnt/盘符/` 可相互转换；WSL 独有的 Linux 用户目录不能自动映射到 Windows 时会暂缓，不擅自改成用户主目录或扩大工作区权限。这种情况仅停止后端并不足以完成同步，应先检查工作目录兼容性。

等待期间任何一端又有新消息或其他版本变化，旧选择失效，必须重新查看并确认。不会把新消息自动归入旧选择。成功导入并收到提交回执后才更新基线、标记选择已完成。两端环境路径可能不同，消息和工具内容保持选定版本。

## 自动退出 Windows 后端

普通单端 WSL → Windows 更新无需手动选端。需要自动退出 Windows 后端时，可设置 `"auto_exit_windows_for_wsl_updates": true`。同步器仅使用原生产计划中单边更新且目标已存在的记录；先预检查正文和工作目录，再重读双方精确版本及 Windows 活动状态。真正覆盖前仍备份目标并复核后端；退出时写回新消息会形成冲突，不能自动覆盖。只读检查、初始化、双边冲突和新聊天不会因此关闭 Windows。预检查受本轮时间预算限制。

如果后端由 `ChatGPT.exe` 托管，桌面应用可能在子进程退出后立即重新启动它。默认会报告 `windows_exit_desktop_required`，不反复结束子进程。只有单独主动启用 `auto_exit_windows_desktop_host=true` 才能同时退出已核验的桌面父进程：先请求正常关闭，随后先结束残留父进程再结束后端，避免重启循环。这会关闭整个 Windows ChatGPT 桌面聊天窗口，无法保证未发送草稿保留；不会因此退出 VS Code 或 WSL。父进程须与已核验子进程同用户、同会话，且创建时间不能晚于子进程，避免父 PID 重用造成误杀。

如果只使用一端，可在配置 `config.json` 中设置：

```json
"auto_exit_windows_for_wsl_choices": true
```

下一轮基础定时任务会读取设置，无需重装任务或重载 WSL。两个开关默认均为 `false`；`auto_exit_windows_for_wsl_choices` 只对手动选定 WSL、双方精确版本不变且已静默的待处理冲突生效，普通单端更新使用上面的 `auto_exit_windows_for_wsl_updates`。两者都不替“保留较多轮数”策略自动关闭进程。预览、诊断、初始化和插件的只读检查均不退出程序。

同步器先校验双方完整备份和目标工作目录，重新检查版本及 Windows 已知聊天活动，再请求 Windows Codex 正常关闭；等待 5 秒后，结束仍存在且已核验的当前用户、当前登录会话内 Codex 进程（包括 CLI、插件后端及工作进程）。使用持有的进程句柄，避免 PID 重用造成误杀；不结束 VS Code、不停止 WSL、不关闭其他程序，不自动重新打开桌面端。

启用意味着允许退出 Windows Codex：请保存 Windows 的未发送草稿。无法读取桌面端输入框，不能保证草稿保留；结束残留后端也可能中断未被聊天索引识别的后台工作。因此此选项只适合不会同时使用两端的场景。发现已知 Windows 聊天仍在处理或尚未静默时继续等待。同步器自身由 Windows Codex 托管、无法核验身份或权限不足时不会强行处理。

退出后重新读取两端原生记录，任何版本变化仍须重新确认；后端重新启动时仍暂缓，不绕过原生导入保护。退出成功不等于同步成功，实际结果与具体等待原因记录在 `runtime/last-run.json` 的 `summary.windows_exit` 及 `status.ps1`。进程退出阶段等待最多约 10 秒，没有常驻服务。

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
