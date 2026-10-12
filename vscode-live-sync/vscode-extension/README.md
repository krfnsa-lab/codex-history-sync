# Codex History Live Sync — optional preview extension

This companion keeps the VS Code application open. When a newer Windows chat is ready and the WSL Codex backend is idle, it coordinates the ordinary history synchronizer and reloads the VS Code window. The official Codex panel is briefly reloaded; this is not streaming both editors concurrently.

Automatic sync is **disabled by default**. No new background service is installed: the already-running VS Code extension host checks at a configurable interval, default 60 seconds. The existing Windows scheduled task continues to use its own original protections.

## Installation contract

The preview installer must install this same companion in Windows and WSL and package these launchers:

- `bin/windows/codex-history-sync-bridge.exe`: a native Windows passthrough that runs the official bundled CLI selected by `CODEX_HISTORY_SYNC_REAL_CLI`.
- `bin/linux/codex-history-sync-bridge`: a Linux executable that invokes the packaged Python gateway with the current official bundled CLI and the private state directory.

The Linux launcher expects `gateway.py` in this extension's root directory. The package builder must copy the project's gateway source there and preserve executable mode 0755 on the Linux launcher. The preview helper runs in Windows from its configured absolute path; it is not imported into the WSL extension host.

Both must use the same bare executable name. `chatgpt.cliExecutable` is application-scoped, so a Linux-only absolute path must **never** be written into that setting shared with Windows.

The installer must also put the bare launcher command in each operating system's existing `PATH`; activating the companion early does not guarantee it starts before the official extension. The companion additionally prepends its packaged launcher directory when activated. It finds the currently installed official extension through VS Code's extension registry rather than hard-coding its version. It does not edit or redistribute the official extension bundle.

The existing synchronizer must be configured and initialized before enabling this extension. It uses native Windows Python for Windows live database access and the existing native WSL endpoint for WSL live database access.

## Configuration in the WSL window

Configure these companion settings using VS Code settings. Keep the state directory short enough for Unix sockets and private to the current Linux user (directory mode 0700 and instance files 0600).

| Setting | Meaning |
| --- | --- |
| `codexHistoryLiveSync.stateDir` | Absolute Linux state directory also used by the gateway. |
| `codexHistoryLiveSync.windowsPython` | Linux mount path to the native Windows Python executable, such as `/mnt/c/Users/example-user/AppData/Local/Programs/Python/Python312/python.exe`. |
| `codexHistoryLiveSync.windowsController` | Absolute Windows path to the existing `history_sync.py`. |
| `codexHistoryLiveSync.windowsConfig` | Absolute Windows path to the existing synchronizer config. |
| `codexHistoryLiveSync.previewHelper` | Absolute Windows path to the read-only eligibility helper. |
| `codexHistoryLiveSync.intervalSeconds` | Default 60 seconds, minimum 30. |
| `codexHistoryLiveSync.controllerTimeoutSeconds` | Default 600 seconds; at least the synchronizer run budget plus three endpoint timeouts and 60 seconds. |

After installing both platform launchers, open the command palette in the Remote WSL window and run **Codex History: Complete Setup and Enable**. It reads the current Linux user's private `~/.config/codex-history-sync-live/launcher.json`, requires `wrappers_installed: true`, and loads its `setupSettings` object. Its property names are the setting names from the table without the `codexHistoryLiveSync.` prefix. The command saves previous settings and writes the new values through VS Code's settings API, preserving JSONC comments. It shows a one-time draft warning before enabling and reloading the window.

The advanced **Configure Optional Gateway** command records the old global CLI setting and sets the shared bare executable name. Both setup routes refuse to overwrite an existing nonempty custom CLI path. A local Windows window with `chatgpt.runCodexInWindowsSubsystemForLinux` enabled is unsupported by this preview; use a Remote WSL window instead.

## What waits

The extension waits while a debug session is active, a text document is unsaved, Codex has an active turn or unanswered server request, another sync owns the pause lease, or more than one gateway instance is open. It requires at least 60 seconds without native protocol activity. The sole gateway must belong to the current VS Code extension-host PID; an instance from another window or one without a verified parent is left untouched. It does not force-disable backend protection in the original synchronizer. Any unmanaged backend therefore continues to prevent writes. A scheduled sync already in progress or a completed run with zero transfers is reported as waiting, with a five-minute delay preserved across reload to prevent repeated automatic reloads.

The official plugin does not expose an API to inspect its unsent composer draft. Draft preservation across reload must be verified in the end-to-end preview before treating this as production-ready. The companion checks unsaved editor documents; that does not prove an unsent chat draft is protected.

If an error occurs after pausing the backend, the extension stops automatic updates and persists a “needs review” state. A separate recovery check must prove there is no active controller/importer or unfinished import before the companion removes its own pause lease and reloads the window to restore the backend. If that check fails or is unavailable, the pause gate stays in place and the backend is not restarted. It never removes the synchronizer's `pending.json`. Review the failed import before enabling another automatic update.

## Rollback

Use **Codex History: Disable and Restore Original Codex**, then reload the window. The extension restores the saved global CLI value only if the current setting is still the companion's own bare command; it does not overwrite a subsequently chosen custom setting. Setup settings are restored only while still equal to the installed values; subsequent user edits are preserved. A remaining pause gate or unverified import blocks restoration of the raw CLI, so the command cannot bypass recovery protection. If installed in two separate user-data profiles, restore each configured profile. Keep the original synchronizer config, runtime ledger, and import backups intact.

## Tests and boundary

`node --test test/bridge.test.js test/extension.test.js` exercises actual private temporary pause files and mocked protocol/process and VS Code configuration boundaries. Tests cover idle import ordering, active-work blocking, a request arriving during pause admission, multiple windows, new instances, lease ownership, failed-import recovery, retention of the synchronizer journal, explicit setup, original-setting restoration, and preservation of later user settings.

The current test run passed 27 tests using the real WSL VS Code bundled Node runtime. These tests do not prove the official foreground chat redraws or an unsent draft survives after a real VS Code reload; that requires a separately reported end-to-end check with a synthetic history directory and no model turns.
