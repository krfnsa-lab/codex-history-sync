"""Best-effort, private-data-free progress snapshots shared by native helpers.

The coordinator owns the root snapshot under its existing run lock. One native
endpoint owns the detail snapshot at a time. Identifiers correlate those files;
they are diagnostics only and never authorize a history write or recovery.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid


PHASES = frozenset(('starting', 'checking_windows', 'checking_wsl', 'planning',
    'exporting', 'previewing', 'checking_backend', 'backing_up', 'writing',
    'verifying', 'checkpointing', 'completed', 'waiting', 'error', 'scanning',
    'reading', 'repairing', 'committed', 'scan', 'read', 'export', 'backup',
    'write', 'verify'))
STATUSES = frozenset(('running', 'waiting', 'completed', 'error'))
KINDS = frozenset(('sync', 'check', 'repair', 'choice'))
COUNTERS = frozenset(('processed_chats', 'total_chats', 'transferred_chats',
    'deferred_chats', 'current_index', 'processed_records', 'total_records'))
REASONS = frozenset(('wsl_stopped', 'task_failed', 'pending_import', 'batch_limit',
    'time_budget', 'conflict', 'source_busy', 'target_busy', 'source_changed',
    'target_backend_running', 'target_backend_started', 'environment_paths_unavailable',
    'manual_choice_stale', 'controller_running', 'updates_available', 'deferred'))


def _uuid(value):
    if value is None:
        return str(uuid.uuid4())
    parsed = str(uuid.UUID(str(value)))
    if parsed != value:
        raise ValueError('noncanonical progress identity')
    return parsed


class ProgressWriter:
    """Atomic snapshots with allowlisted fields; failures never affect work.

    Callers must serialize root writers with their existing synchronization
    lock. Unique temporary names protect parallel detail writers from corrupt
    JSON; consumers additionally require matching run and operation identities.
    """
    def __init__(self, path, run_id=None, kind='sync', operation_id=None, side=None, request_id=None):
        self.enabled = False
        self.path = None
        self.run_id = self.operation_id = None
        self.kind = kind
        self._last_write = None
        self._terminal = False
        self._started = time.monotonic()
        self._value = {}
        try:
            self.path = Path(path)
            self.run_id, self.operation_id = _uuid(run_id), _uuid(operation_id)
            if kind not in KINDS or side not in (None, 'windows', 'wsl'):
                return
            self._value = {'schema_version': 1, 'owner': 'codex-history-sync',
                'run_id': self.run_id, 'operation_id': self.operation_id,
                'kind': kind, 'status': 'running', 'phase': 'starting',
                'started_at': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid()}
            if request_id is not None:
                self._value['request_id'] = _uuid(request_id)
            if side is not None:
                self._value['side'] = side
            self.enabled = True
        except Exception:
            pass

    def begin_operation(self, phase, side=None):
        try:
            if self._terminal:
                return None
            self.operation_id = str(uuid.uuid4())
            self._value['operation_id'] = self.operation_id
            self._last_write = None
            self.update(phase, side=side)
            return self.operation_id
        except Exception:
            return None

    def update(self, phase, **fields):
        self._publish(phase, fields)

    def finish(self, status, **fields):
        if status not in STATUSES or status == 'running':
            return
        self._publish('completed' if status == 'completed' else status,
                      {**fields, 'status': status}, force=True)

    def _publish(self, phase, fields, force=False):
        temporary = None
        try:
            if not self.enabled or self._terminal or phase not in PHASES:
                return
            changed = self._value['phase'] != phase
            self._value['phase'] = phase
            for name, value in fields.items():
                if name in COUNTERS and type(value) is int and value >= 0:
                    self._value[name] = value
                elif name == 'side' and value in ('windows', 'wsl'):
                    self._value[name] = value
                elif name == 'status' and value in STATUSES:
                    self._value[name] = value
                elif name == 'reason' and value in REASONS:
                    self._value[name] = value
            now = time.monotonic()
            if not force and not changed and self._last_write is not None and now - self._last_write < 1:
                return
            elapsed = now - self._started
            self._value.update(updated_at=datetime.now(timezone.utc).isoformat(),
                               elapsed_seconds=round(max(0.0, elapsed), 3) if math.isfinite(elapsed) else 0)
            # A progress path is supplied only by the owning coordinator. Avoid
            # following a replaced snapshot or publishing into a missing folder.
            if self.path.is_symlink() or not self.path.parent.is_dir():
                return
            temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(self._value, stream, ensure_ascii=False)
                stream.flush()
            # Windows readers and scanners can briefly hold the destination
            # without delete-sharing. Retry only that transient permission
            # failure, for at most three attempts and 30 ms of total waiting.
            # The staged file remains complete throughout; persistent failures
            # still affect diagnostics alone, never the history operation.
            for attempt in range(3):
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.01 * (attempt + 1))
            self._last_write = now
            if force:
                self._terminal = True
        except Exception:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
