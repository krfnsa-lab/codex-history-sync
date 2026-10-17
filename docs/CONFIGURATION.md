# 配置说明

`configure.ps1` 调用 `setup.py` 创建个人配置。配置、运行目录和任务名称由同一份配置驱动；默认配置路径是程序目录下的 `config.json`。

| 字段 | 含义与默认值 |
| --- | --- |
| `windows_home` | Windows 本地 Codex 目录；向导优先读取 `CODEX_HOME`，否则使用用户目录的 `.codex` |
| `wsl_distro` | 需要同步的 WSL 发行版名称，由用户选择 |
| `wsl_user` | 发行版内的 Linux 用户，默认读取该发行版默认用户 |
| `wsl_home` | Linux 本地 Codex 目录，向导读取目标用户的 `CODEX_HOME` 或用户目录 |
| `python_windows` | 已验证的 Windows `python.exe` 绝对路径；示例中的 `auto` 表示探测 |
| `python_wsl` | 目标 WSL 的 Python 路径，探测入口默认 `/usr/bin/python3` |
| `runtime_directory` | 基线、锁、日志和临时包目录；相对路径以配置目录为准 |
| `task_name` | Windows 计划任务名称，默认 `CodexHistorySync-Public` |
| `interval_minutes` | 检查间隔，默认 5 分钟 |
| `quiet_seconds` | 聊天至少静默多少秒，默认 60 |
| `conflict_resolution` | 默认 `manual`，保留冲突；`prefer_longer` 自动选择轮数更多的一端，轮数相同再比较内容条目数 |
| `auto_exit_windows_for_wsl_choices` | 默认 `false`；手动选定 WSL 版本的冲突等待目标后端时，自动正常退出并结束残留 Windows Codex 进程；适合不会同时使用两端的用户，详见[退出行为及草稿限制](CONFLICTS.md#自动退出-windows-后端) |
| `auto_exit_windows_for_wsl_updates` | 默认 `false`；普通单端 WSL → Windows 已有聊天更新可自动退出 Windows 后端；不替冲突选择版本，空闲、兼容性、版本及导入备份保护继续生效 |
| `auto_exit_windows_desktop_host` | 默认 `false`，需单独主动启用；若 ChatGPT 桌面应用是 Codex 后端的父进程，允许同时退出这个桌面应用，避免它立即重启后端。会关闭桌面聊天窗口，草稿须提前保存；只作用于同用户、同会话且父子创建时间核验通过的进程 |
| `windows_history_view_only` | 默认 `false`；Windows 仅查看 WSL 记录时主动开启。WSL → Windows 完整导入保留同一聊天 ID 和全部消息，使用专用空目录及只读环境；查看版本的正文不能反向覆盖原项目 |
| `max_transfers_per_run` | 每轮最多尝试多少条聊天，默认 10 |
| `run_budget_seconds` | 本轮软时间预算，默认 120 秒 |
| `operation_timeout_seconds` | 单次端点调用超时，默认 120 秒 |
| `task_time_limit_minutes` | 计划任务硬时间上限，默认 10 分钟 |

时间与数量字段都须为正整数；布尔值、浮点数、字符串数字不能替代配置中的整数。

## Windows 只查看 WSL 历史

设置 `"windows_history_view_only": true` 后，WSL → Windows 的完整导入在 Windows Codex 数据目录下使用 `.history-sync-view/<聊天ID>` 专用空目录，不使用用户主目录或原 Windows 项目目录作为替代。会话运行环境的 cwd 指向此目录，工作区列表清空，沙箱设为 read-only，审批设为 never；已有 Windows 记录的项目绑定移除。目录链接、重定向和非空目录会使操作停止。

此模式不修改消息正文、工具参数和缓存内容，只转换结构化运行环境并校正索引字节偏移。WSL 原记录不变；目标 `.history-sync-backups/<聊天ID>/<备份目录>/source-package` 保存未转换的完整导出。Windows 查看记录继续沿用原聊天 ID，后续 WSL 更新可补齐，不生成新的可见聊天副本。

Windows 查看版的正文不能反向导入原项目，即使自动数量策略偏向它，也不会因此覆盖 WSL；标题等元数据可按原流程同步。模式不会禁用官方客户端发送框，也不保证阻止用户在客户端主动更改权限；继续操作原 WSL 项目应切回 WSL。原有双方空闲、目标后端停止、精确版本及备份保护保持不变。此开关不会批量重写已经同步且没有更新的聊天，不需要重新初始化基线。

稳定版推荐保持 `conflict_resolution=manual`，在插件的 **Resolve Chat Conflicts** 中明确选择 Windows 或 WSL。先完整备份双方，选择绑定两端精确版本，不生成副本；已保存的手动选择优先于自动计数策略。详见[手动处理冲突](CONFLICTS.md)。

需要自动保留聊天更多的一端时，在个人配置中设置 `"conflict_resolution": "prefer_longer"`。只对已有双方基线的聊天生效，先比较已索引轮数，再比较内容条目数。数值相同、索引缺失或正文不完整时继续保留冲突；不按文件大小或修改时间判断，也不会拼接两份历史。

被替换版本的完整正文、聊天元数据和消息缓存保存在目标数据目录的 `.history-sync-backups/<聊天ID>/<备份目录>`。选择较多的一端可能使较少一端独有的消息退出当前聊天，这些消息保留在备份中。两端须静默，目标 Codex 后端须停止；WSL 较多且桌面端仍打开时，定时任务会等待桌面端后端停止。修改该配置不需要重新初始化基线或重新安装定时任务。

硬上限须满足 `task_time_limit_minutes * 60 >= run_budget_seconds + 3 * operation_timeout_seconds + 60`。这为当前聊天的导出、进程检查、导入和收尾留出余量；更大记录仍可能因单次操作超时而失败，并留下保护日志。配置向导会按所选预算计算默认硬上限。

`windows_home` 和运行目录支持 `%USERPROFILE%` 等 Windows 环境变量与 `~`。`wsl_home` 必须是目标 Linux 系统中的绝对路径，不能用 Windows 挂载路径来共享活动数据库。

## 非交互生成

先启动目标 WSL，然后指定发行版：

```powershell
.\configure.ps1 -NonInteractive -Distro Ubuntu
```

指定其他 Linux 用户或数据目录：

```powershell
.\configure.ps1 -NonInteractive -Distro Ubuntu -WslUser your-linux-user -WslHome /home/your-linux-user/.codex
```

需要批量配置全部参数时，可以直接运行 Python 入口：

```powershell
python setup.py --non-interactive --distro Ubuntu --config profiles/work.json --task-name CodexHistorySync-Work --interval-minutes 15
```

发现多个发行版时，非交互模式要求明确指定。停止的 WSL 不会被配置向导自动启动。

## 解释器与覆盖

PowerShell 入口按显式 `-PythonPath`、有效配置、自动探测的顺序选择 Windows CPython。配置里已经写了具体但失效的路径时会报错，不悄悄切换解释器。安装器从选中的 `python.exe` 推导同目录的 `pythonw.exe`，并将控制器和端点的解释器固定到任务动作中。

控制器支持 `--config`、`--runtime-dir`、`--python-windows` 和 `--task-name`。运行目录覆盖只作用于本轮，长期配置应写入配置文件并重新安装任务。

配置文件已存在时，向导拒绝覆盖。明确使用 `setup.py --force` 可以重新配置，但已有基线时不能更换两端数据目录或运行目录；存在未完成导入时也会拒绝。创建另一组同步配置应使用新配置文件、运行目录和任务名称。
