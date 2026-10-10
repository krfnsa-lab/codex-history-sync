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
| `max_transfers_per_run` | 每轮最多尝试多少条聊天，默认 10 |
| `run_budget_seconds` | 本轮软时间预算，默认 120 秒 |
| `operation_timeout_seconds` | 单次端点调用超时，默认 120 秒 |
| `task_time_limit_minutes` | 计划任务硬时间上限，默认 10 分钟 |

时间与数量字段都须为正整数；布尔值、浮点数、字符串数字不能替代配置中的整数。

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
