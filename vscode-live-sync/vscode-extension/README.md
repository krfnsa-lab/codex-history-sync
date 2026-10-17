# Codex History Sync 1.1.6

Optional Remote WSL companion for Codex History Sync 2.3.1. Automatic checking is disabled until setup. Once configured, checks use the selected interval in the existing extension host; no extra Windows service is installed.

Version 1.1.6 applies eligible outgoing WSL-to-Windows updates during a manual check. The direction-scoped controller writes only Windows, without WSL pause, acknowledgement or reload; normal target activity, exit, version, compatibility and backup guards still apply. Automatic checking keeps its configured interval. Reload the window once after a frontend upgrade to load the installed code; activation records its loaded version separately.

The controller's opt-in windows_history_view_only profile receives WSL history under the original chat ID in a dedicated empty Windows directory, with a read-only runtime environment. It does not map or operate the WSL project. Full history from a viewing replica cannot overwrite the original project chat; later WSL updates can still arrive. This does not disable the official client's message composer.

Version 1.1.4 checks outgoing full-record compatibility through a temporary exact-version export and the native Windows import preview. An unmappable workspace is reported as a block, rather than an indefinite timer wait. These checks never import records or stop backends, and temporary packages are removed afterward.

Version 1.1.2 handles large ordinary records with ordinal or other complete header metadata without rewriting message bytes. Manual retry bypasses only the time delay; automatic backoff retains allowlisted failure counts, while all activity, confirmation and import guards remain in force.

Version 1.1.1 stops completed background authentication-status polls from resetting the idle timer. Outstanding requests remain busy, and active turns, tools and unknown mutating requests still prevent pausing. A competing synchronization lock now produces a normal deferral rather than a locked-byte read error; genuine file-access failures still propagate.

When the only missing condition is idle time, a manual check shows the verified elapsed idle seconds against the 60-second threshold. Pending work and incomplete state are reported separately; the message does not bypass admission checks.

The default reloadMode=confirm only previews in the background. A ready update is applied after the user clicks the status and confirms that unsent drafts are saved. Automatic reload is opt-in and requires acceptance through VS Code APIs. The official Codex composer exposes no draft-inspection API; the companion does not promise draft preservation in automatic mode.

**Codex History: Resolve Chat Conflicts** lets the user explicitly keep either Windows or WSL. The native Windows helper binds confirmation to both exact revisions and preserves complete private exports before queuing the choice. The normal controller updates the opposite replica under the same chat ID only after its backend stops, with an exact expected revision and native backup. No new copy is created. Unique messages on the replaced side remain in backups rather than the active chat. A later edit invalidates the choice and requires fresh confirmation.

## Live progress

The status bar shows actual phases and elapsed time. Manual checks also open a progress notification. Clicking the status while a check is running opens its current progress and the **Codex History Sync** output channel, rather than starting a duplicate check. **Codex History: Show Sync Progress** opens the same details.

Processed, synchronized and deferred counts are distinct; unknown totals never produce an estimated percentage. A snapshot without updates for 30 seconds is shown as unconfirmed progress, not success. Run, request and operation identifiers prevent old results or unrelated endpoint details from completing a current check. The display reads small local snapshots once per second only while work or bounded observation is active, then stops; it does not add a resident service or repeatedly start Python.

Progress snapshots contain allowlisted stages and counters, never chat titles, message bodies, personal paths, credentials or arbitrary error text. Display or snapshot failures do not change synchronization, import recovery or conflict authorization. Dry runs and initialization do not create progress snapshots.

## Installation contract

The installer builds a machine-specific VSIX and installs both platform launchers with the same bare executable name. chatgpt.cliExecutable is application-scoped; a Linux-only absolute path must not be written into this shared setting. The launcher selects the locally installed official CLI, without modifying or redistributing that extension.

The installer places preview_sync.py, process/local-setting probes and conflict-helper dependencies in the Windows helper directory. The existing mergeHelper setting can select another helper; otherwise it is merge_chat.py beside previewHelper. WSL owns its gateway, socket, private instance and launcher files.

## Coordination and recovery

The coordinator requires one healthy idle gateway owned by the current extension-host PID, private pause-file ownership and a stable process snapshot. Unsaved editor files, debugging, active protocol work, pending requests, other windows, invalid state and unfinished imports block application. Setting changes and explicit disable cancel before pause; a paused operation skips import and follows the verified recovery path. Setup, polling, checks, conflict selection and recovery are mutually exclusive.

Only a committed transfer is reported as synced. A zero-transfer round is deferred with backoff. Source activity before export is a normal deferral; unknown failures remain fail-closed. Recovery never removes the controller import journal. Restoring the original CLI respects later user changes and cannot bypass an unresolved pause or import.

## Validation boundary

Tests cover pause admission, ownership, idle state, cancellation, setting races, default confirmation, automatic acceptance, manual side selection and safe diagnostics. Preview and native transfer guards share the same exact-version choice. They do not claim foreground UI redraw or image-attachment visual rendering. Existing-chat updates from WSL to the desktop require the desktop backend to stop.
