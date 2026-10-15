# Codex History Sync 1.0.0

Optional Remote WSL companion for Codex History Sync 2.1.0. Automatic checking is disabled until setup. When enabled, the existing extension host checks every 60 seconds; no extra Windows service is installed.

The default reloadMode=confirm only previews in the background. A ready update is applied after the user clicks the status and confirms that unsent drafts are saved. Automatic reload is opt-in and requires acceptance through VS Code APIs. The official Codex composer exposes no draft-inspection API; the companion does not promise draft preservation in automatic mode.

**Codex History: Resolve Chat Conflicts** lets the user explicitly keep either Windows or WSL. The native Windows helper binds confirmation to both exact revisions and preserves complete private exports before queuing the choice. The normal controller updates the opposite replica under the same chat ID only after its backend stops, with an exact expected revision and native backup. No new copy is created. Unique messages on the replaced side remain in backups rather than the active chat. A later edit invalidates the choice and requires fresh confirmation.

## Installation contract

The installer builds a machine-specific VSIX and installs both platform launchers with the same bare executable name. chatgpt.cliExecutable is application-scoped; a Linux-only absolute path must not be written into this shared setting. The launcher selects the locally installed official CLI, without modifying or redistributing that extension.

The installer places preview_sync.py, process/local-setting probes and conflict-helper dependencies in the Windows helper directory. The existing mergeHelper setting can select another helper; otherwise it is merge_chat.py beside previewHelper. WSL owns its gateway, socket, private instance and launcher files.

## Coordination and recovery

The coordinator requires one healthy idle gateway owned by the current extension-host PID, private pause-file ownership and a stable process snapshot. Unsaved editor files, debugging, active protocol work, pending requests, other windows, invalid state and unfinished imports block application. Setting changes and explicit disable cancel before pause; a paused operation skips import and follows the verified recovery path. Setup, polling, checks, conflict selection and recovery are mutually exclusive.

Only a committed transfer is reported as synced. A zero-transfer round is deferred with backoff. Source activity before export is a normal deferral; unknown failures remain fail-closed. Recovery never removes the controller import journal. Restoring the original CLI respects later user changes and cannot bypass an unresolved pause or import.

## Validation boundary

Tests cover pause admission, ownership, idle state, cancellation, setting races, default confirmation, automatic acceptance, manual side selection and safe diagnostics. Preview and native transfer guards share the same exact-version choice. They do not claim foreground UI redraw or image-attachment visual rendering. Existing-chat updates from WSL to the desktop require the desktop backend to stop.
