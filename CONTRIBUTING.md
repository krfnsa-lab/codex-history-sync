# 参与开发

请先阅读 [README](README.md) 和 [同步保护说明](docs/SAFETY.md)。目前协调器运行在 Windows，端点运行在各自系统；项目只依赖 Python 标准库。

修改同步逻辑时，使用临时目录中的合成 SQLite 和 JSONL 数据验证。不要把实际聊天、账号信息、配置、日志、基线或备份加入提交或问题报告。报告问题时可提供版本、操作系统、经过脱敏的错误摘要及复现步骤。

提交前执行：

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -q
.\tests\test_powershell.ps1
python -B tools/build_release.py --check-only
```

PowerShell 用例只在 Windows 运行。新增数据库适配需有合成数据测试，覆盖缺失正文、并发修改、冲突、失败回滚和导入回执。只有得到有效提交回执并写入基线后，才能解除未完成导入保护。

公开版本号与基线格式版本分别维护。配置向导、控制器和安装器应共享配置语义；保留多个配置的任务与运行目录隔离。兼容性变化请同步更新配置说明和 CHANGELOG。
