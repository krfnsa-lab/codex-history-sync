# Codex History Sync

在 Windows 与 WSL 的本地 Codex 数据目录之间同步聊天。支持新增聊天、单边续聊、标题和归档状态；两端都修改的聊天会保留双方并报告冲突。

Windows 任务计划程序按配置启动一次检查，处理完成就退出。脚本没有常驻服务，不唤醒电脑；指定 WSL 未运行时跳过。为使同步后的聊天能够在两端打开，脚本同时处理会话索引、分页正文缓存和 JSONL 文件。

这是独立社区脚本，不是 OpenAI 官方同步功能。首版运行范围为 Windows + WSL，使用 `state_5.sqlite` 与 `thread_history_1.sqlite`；遇到无法识别的数据格式会停止该记录的迁移。

## 准备环境

- Windows PowerShell 5.1 或更新版本。
- Windows 原生 CPython 3.11 或更新版本，带同目录的 `python.exe` 和 `pythonw.exe`。
- 所选 WSL 发行版中的 Python 3.11 或更新版本。
- 两端都已使用过 Codex，具有自己的本地数据目录。

只使用 Python 标准库，无需安装 pip 依赖。程序与运行目录须位于 WSL 可以通过 `/mnt/盘符/` 访问的 Windows 本地磁盘。不要把原生 SQLite 数据目录放在另一系统的挂载路径上。

## 首次使用

将本项目解压到准备长期保留的目录，在该目录打开 PowerShell。先自行启动准备使用的 WSL 发行版，再执行：

```powershell
.\configure.ps1
.\doctor.ps1
.\sync-now.ps1 -Initialize
.\sync-now.ps1 -DryRun
```

配置向导会检测可用的 Windows Python，列出 WSL 发行版，读取选中发行版的默认用户和目录，再让你设置检查间隔、数量和时间上限。它仅生成本地 `config.json`，不初始化基线、不写聊天、不安装任务，也不启动已经停止的 WSL。

`-Initialize` 建立两端独立的版本基线；已有基线受到保护，不能重复初始化。`-DryRun` 预览当前待同步记录和跳过原因，不更新基线或聊天。中文摘要只显示去重后的原因，`-Json` 保留完整诊断信息。

预览符合预期后，先手动同步一次，再安装自动任务：

```powershell
.\sync-now.ps1
.\status.ps1
.\install.ps1 -WhatIf
.\install.ps1
```

默认任务名是 `CodexHistorySync-Public`，每 5 分钟检查一次，并在当前用户登录时检查。任务以当前用户普通权限运行，不保存密码。`-WhatIf` 只显示安装计划。所有前台入口支持 `-PythonPath`，配置完成后统一使用同一 Windows 解释器。

已有聊天的更新会等待目标系统内的 Codex 进程关闭；检测为系统范围内的保守保护，包含桌面应用、扩展后台和 CLI。新聊天可以在目标后台运行时导入，源聊天须已静默。导入后，已打开的 VS Code 面板可能需要重新加载窗口或重新打开列表。

## 配置与多个实例

默认使用本目录的 `config.json`。公开的 `config.example.json` 是配置格式示例，示例 Linux 用户需要自行替换。推荐通过配置向导生成实际配置。

所有入口可以指定独立配置文件，例如：

```powershell
.\configure.ps1 -ConfigPath .\profiles\work.json -TaskName CodexHistorySync-Work
.\doctor.ps1 -ConfigPath .\profiles\work.json
.\sync-now.ps1 -ConfigPath .\profiles\work.json -Initialize
.\sync-now.ps1 -ConfigPath .\profiles\work.json -DryRun
.\install.ps1 -ConfigPath .\profiles\work.json -WhatIf
```

每个配置应使用独立任务名称和运行目录；每对 Codex 数据目录只启用一个同步配置。相对运行目录以配置文件所在目录为基准。也可将配置放在项目目录之外，更新源代码时保留个人配置和基线。

配置项和非交互配置方法见 [配置说明](docs/CONFIGURATION.md)。

## 状态、诊断和限制

```powershell
.\status.ps1
.\doctor.ps1
.\sync-now.ps1 -DryRun -Json
```

状态仅显示本地缓存，不扫描聊天或启动 WSL；中文时间按本地时区显示，JSON 时间保留 UTC。诊断仅检查本地数据库结构、数量、正文文件状态和未完成导入日志，不修复或导入。

默认每轮最多尝试 10 条、软预算 120 秒。预算包含初始指纹扫描，只在开始下一条传输前检查；本条传输会完成回执和检查点再退出。初始扫描仍会读取整个记录库的指纹，时间和内存用量取决于数据规模，并不是固定上限。任务另有硬上限，安装器会检查它是否给本条收尾留出时间。

登录凭据、Codex 配置、插件和权限设置不参与同步。双边修改、删除、缺失正文、越界路径、无法识别的数据库字段和未索引旧记录受到保护。导入前会保存目标备份；备份和基线不自动删除。完整边界及中断处理见 [同步保护说明](docs/SAFETY.md)。

## 卸载与升级

```powershell
.\uninstall.ps1 -WhatIf
.\uninstall.ps1
```

卸载只删除本目录和当前配置拥有的计划任务，保留程序、配置、聊天、日志和备份。脚本移动或删除后，原任务中的路径会失效。

升级或移动目录时，先用原目录和原配置卸载任务，并等待当前同步退出；保留配置及完整运行目录，再更新程序、运行诊断和预览、重新安装任务。不要复制另一套部署的基线到新配置；不要同时启用新旧任务处理同一对数据目录。

## 开发与发布

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -q
.\tests\test_powershell.ps1
python -B tools/build_release.py
```

测试只使用合成数据和临时目录；PowerShell 测试用模拟任务验证安装和卸载，不注册真实任务。Windows CI 运行控制器、端点和 PowerShell 测试；Linux CI 检查端点和跨平台纯逻辑，Windows 专属用例会跳过。工作流使用 [checkout](https://github.com/actions/checkout) 和 [setup-python](https://github.com/actions/setup-python) 的官方 Action。

发布工具从干净源码目录的白名单创建带 SHA-256 清单的 ZIP，排除实际配置、运行数据、日志、备份、数据库和凭据。若目录已经生成了个人 `config.json`，请从干净的源码副本构建，工具会拒绝把该部署目录直接当成发布源。上传 GitHub 时应上传本项目源码或生成的 ZIP，而不是个人 Codex 数据目录。源码和版本下载见 [GitHub 仓库](https://github.com/krfnsa-lab/codex-history-sync)。

项目采用 [MIT](LICENSE) 许可证，标准文本见 [Open Source Initiative](https://opensource.org/license/mit)。
