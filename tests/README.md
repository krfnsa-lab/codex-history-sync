# Synthetic test suite

From the package directory, run:

```text
python -X utf8 -B -m unittest discover -s tests -p "test_*.py"
```

Tests create synthetic databases and files in temporary directories and clean
them up. Tests never import real conversations, start WSL, or register tasks.
The Windows controller's main-entry tests are skipped on other platforms.

The endpoint tests can also run separately on Linux:

```text
python -X utf8 -B -m unittest tests.test_endpoint tests.test_diagnostics tests.test_history_endpoint_review
```

On Windows, also run the standalone PowerShell suite from the package directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tests\test_powershell.ps1
```

This suite needs Windows PowerShell 5.1 and native Windows CPython 3.11 or newer,
with `pythonw.exe` beside `python.exe`. It discovers Python from `PATH` (including
GitHub Actions `setup-python`) or standard installation folders; pass
`-PythonPath` with your interpreter's absolute path to override discovery.

The suite copies the PowerShell entry points into a random directory under the
system temporary directory and uses dummy Python endpoints. It covers native
`powershell.exe -File` invocation, UTF-8 output, argument forwarding, configuration
validation, task ownership and `-WhatIf`. Task queries, registration and removal
are mocked, so it never reads or modifies real scheduled tasks. Temporary fixtures
are cleaned in `finally`, after checking the recursive cleanup target is the
suite's own directory inside system TEMP.
