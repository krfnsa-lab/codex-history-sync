#!/usr/bin/env python3
"""Native, bounded-memory Codex history transport endpoint.

Run this endpoint separately on Windows and WSL. Never open another platform's
live SQLite database. Only explicit import mutates user history. Existing-chat
imports additionally require --allow-existing, which the controller may pass
only after verifying that the destination Codex backend is stopped.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

FORMAT_VERSION = 1
CACHE_TABLES = ("thread_turns", "thread_items", "thread_realtime_items", "thread_history_projection_state")
PORTABLE_FIELDS = (
    "id", "created_at", "updated_at", "source", "model_provider", "cwd", "title",
    "tokens_used", "has_user_event", "archived", "archived_at", "cli_version",
    "model", "reasoning_effort", "created_at_ms", "updated_at_ms", "thread_source",
    "preview", "recency_at", "recency_at_ms", "history_mode", "name", "originator",
)
LOCAL_FIELDS = {
    "sandbox_policy", "approval_mode", "memory_mode", "agent_path", "is_pinned",
    "thread_section_id", "section_position", "section_entered_at_ms", "project_id",
    "daybreak_enabled", "creator_user_id", "creator_account_id",
}
METADATA_FIELDS = ("title", "name", "archived", "archived_at")
INSPECT_FIELDS = ("id", "rollout_path", "source", "title", "name", "archived", "history_mode")
CHUNK_SIZE = 512 * 1024
CACHE_HASH_FIELDS = {
    "thread_turns": ("turn_id", "rollout_ordinal", "status", "error_json", "started_at", "completed_at", "duration_ms", "first_user_item_id", "final_agent_item_id", "rollout_byte_offset", "rollout_end_ordinal", "rollout_end_byte_offset"),
    "thread_items": ("turn_id", "item_id", "rollout_ordinal", "created_at_ms", "item_type", "updated_at_ordinal", "started_at_ms", "completed_at_ms"),
    "thread_realtime_items": ("item_id", "rollout_ordinal", "created_at_ms", "item_type"),
    "thread_history_projection_state": ("next_rollout_byte_offset", "next_rollout_ordinal"),
}


class EndpointError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise EndpointError(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def quote(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        fail("invalid_identifier", "Unexpected SQLite identifier")
    return '"' + name + '"'


def native_home(value):
    raw = str(value)
    if os.name == "nt" and raw.startswith("\\\\"):
        fail("non_native_home", "Windows endpoint must use a native Windows home, not a WSL UNC path")
    path = Path(raw).expanduser().resolve()
    if os.name == "nt" and str(path).startswith("\\\\"):
        fail("non_native_home", "Windows endpoint must use a native Windows home, not a resolved network path")
    if os.name != "nt" and re.match(r"^/mnt/[A-Za-z](?:/|$)", str(path)):
        fail("non_native_home", "WSL endpoint must use its native Linux home, not a Windows mount")
    if not path.is_dir():
        fail("home_missing", "Codex home directory does not exist")
    return path


def readonly(path):
    if not path.is_file():
        fail("database_missing", "Initialize the native Codex runtime before using this endpoint")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def table_info(connection, table, database="main"):
    return list(connection.execute(f"PRAGMA {quote(database)}.table_info({quote(table)})"))


def columns(connection, table, database="main"):
    return [row[1] for row in table_info(connection, table, database)]


def table_exists(connection, table, database="main"):
    return connection.execute(
        f"SELECT 1 FROM {quote(database)}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def scoped_sources(value):
    parts = {part.strip() for part in value.split(",") if part.strip()}
    if "interactive" in parts:
        parts.remove("interactive")
        parts.update(("cli", "vscode"))
    if not parts or parts - {"cli", "vscode"}:
        fail("scope_invalid", "Supported scope values are cli, vscode, or interactive")
    return parts


def is_native_codex_process(name, platform):
    """Use executable names only; never collect or disclose command lines.

    This deliberately also treats a Codex desktop process as busy. The coarse
    native-OS guard covers app-server and interactive CLI children without
    needing permissions to inspect their arguments or identify a Codex home.
    """
    pattern = r"codex(?:[-_][a-z0-9_.-]+)?\.exe" if platform == "windows" else r"codex(?:[-_][a-z0-9_.-]+)?"
    return re.fullmatch(pattern, name, re.IGNORECASE) is not None


def windows_process_names():
    """Read a native process snapshot without launching a helper process."""
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ("Process32FirstW", "Process32NextW"):
        method = getattr(kernel, name)
        method.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        method.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("Native process snapshot failed")
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID != os.getpid():
                yield entry.szExeFile
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise OSError("Native process enumeration failed")
    finally:
        kernel.CloseHandle(handle)


def linux_process_names(proc_root=Path("/proc")):
    """Only read comm names; inaccessible process metadata fails closed."""
    if not proc_root.is_dir():
        raise OSError("Native process directory is unavailable")
    for directory in proc_root.iterdir():
        if not directory.name.isdecimal() or directory.name == str(os.getpid()):
            continue
        try:
            # Linux task names are limited to 15 bytes, so this read is bounded.
            with (directory / "comm").open("rb") as stream:
                yield stream.read(256).decode("utf-8", errors="strict").strip()
        except (FileNotFoundError, ProcessLookupError):
            continue  # The process exited after enumeration.


def backend_status():
    platform = "windows" if os.name == "nt" else "linux"
    try:
        names = windows_process_names() if platform == "windows" else linux_process_names()
        count = sum(is_native_codex_process(name, platform) for name in names)
    except (OSError, ValueError, AttributeError, UnicodeError):
        return {"backend_running": True, "backend_detection_ok": False,
                "backend_detection_code": "process_scan_failed", "backend_count": None,
                "backend_kinds": {}, "backend_guard_scope": "native_os_codex_processes"}
    return {"backend_running": count > 0, "backend_detection_ok": True,
            "backend_detection_code": "running" if count else "stopped", "backend_count": count,
            "backend_kinds": {"native_codex": count}, "backend_guard_scope": "native_os_codex_processes"}


def backend_status_command(args):
    native_home(args.home)
    return {"ok": True, "platform": "windows" if os.name == "nt" else "linux", **backend_status()}


def thread_row(connection, thread_id, selected=None):
    selection = "*"
    if selected is not None:
        available = set(columns(connection, "threads"))
        selection = ",".join(quote(name) if name in available else "NULL AS " + quote(name) for name in selected)
    row = connection.execute("SELECT " + selection + " FROM threads WHERE id=?", (thread_id,)).fetchone()
    return dict(row) if row is not None else None


def safe_rollout(home, row):
    value = row.get("rollout_path")
    if not isinstance(value, str) or not value:
        fail("rollout_missing", "Chat has no rollout path")
    path = Path(value).resolve()
    try:
        relative = path.relative_to(home)
    except ValueError:
        fail("rollout_outside_home", "Chat rollout must reside inside the native Codex home")
    if not relative.parts or relative.parts[0] not in ("sessions", "archived_sessions"):
        fail("rollout_outside_sessions", "Chat rollout must reside in sessions or archived_sessions")
    return path


def file_signature(path):
    if not path.is_file():
        return {"size": None, "sha256": None, "complete": False, "mtime_ns": None, "message_hint": False}
    before = path.stat()
    hasher = hashlib.sha256()
    last = b""
    line_prefix = b""
    message_hint = False
    def inspect_prefix(prefix):
        return bool(
            re.search(rb'"type"\s*:\s*"response_item"', prefix)
            and re.search(rb'"role"\s*:\s*"(?:user|assistant)"', prefix)
        ) or bool(
            re.search(rb'"type"\s*:\s*"event_msg"', prefix)
            and re.search(rb'"type"\s*:\s*"(?:UserMessage|AgentMessage|user_message|agent_message)"', prefix)
        )
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            last = chunk[-1:]
            if not message_hint:
                parts = chunk.split(b"\n")
                for index, part in enumerate(parts):
                    line_prefix += part[:max(0, 4096 - len(line_prefix))]
                    if index < len(parts) - 1:
                        message_hint = inspect_prefix(line_prefix)
                        line_prefix = b""
                        if message_hint:
                            break
    complete = not before.st_size or last == b"\n"
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        fail("source_changed", "Rollout changed while its revision was being read")
    return {"size": before.st_size, "sha256": hasher.hexdigest(), "complete": complete,
            "mtime_ns": before.st_mtime_ns, "message_hint": message_hint}


def cache_rows_digest(connection, table, thread_id, database="main"):
    hasher = hashlib.sha256()
    if connection is None or not table_exists(connection, table, database):
        return hasher.hexdigest()
    names = set(columns(connection, table, database))
    fields = CACHE_HASH_FIELDS[table]
    unknown = names - set(fields) - {"thread_id", "item_json", "root_turn_id"}
    if unknown:
        nonnull = " OR ".join(quote(name) + " IS NOT NULL" for name in sorted(unknown))
        if connection.execute(
            f"SELECT 1 FROM {quote(database)}.{quote(table)} WHERE thread_id=? AND ({nonnull}) LIMIT 1", (thread_id,)
        ).fetchone():
            fail("schema_unsupported", "Native cache contains populated fields unsupported by this endpoint version")
    selected = [quote(name) if name in names else "NULL AS " + quote(name) for name in fields]
    has_json = "item_json" in names
    use_blob = has_json and hasattr(connection, "blobopen")
    if use_blob:
        selected.append("rowid AS __rowid")
    elif has_json:
        selected.append("item_json AS __item_json")
    keys = [entry[1] for entry in sorted(table_info(connection, table, database), key=lambda entry: entry[5]) if entry[5]]
    order = ",".join(quote(key) for key in keys) or "rowid"
    for record in connection.execute(
        f"SELECT {','.join(selected)} FROM {quote(database)}.{quote(table)} WHERE thread_id=? ORDER BY {order}", (thread_id,)
    ):
        item = {name: record[name] for name in fields}
        if has_json:
            payload_hash = hashlib.sha256()
            payload_size = 0
            if use_blob:
                with connection.blobopen(table, "item_json", record["__rowid"], readonly=True, name=database) as blob:
                    while True:
                        data = blob.read(CHUNK_SIZE)
                        if not data:
                            break
                        payload_hash.update(data)
                        payload_size += len(data)
            else:
                data = record["__item_json"].encode("utf-8")
                payload_hash.update(data)
                payload_size = len(data)
            item["item_json_sha256"] = payload_hash.hexdigest()
            item["item_json_bytes"] = payload_size
        encoded = canonical(item)
        hasher.update(str(len(encoded)).encode("ascii") + b":" + encoded)
    return hasher.hexdigest()


def cache_summary(connection, thread_id, database="main"):
    result = {}
    numeric_columns = (
        "rollout_ordinal", "updated_at_ordinal", "created_at_ms", "started_at", "completed_at",
        "started_at_ms", "completed_at_ms", "next_rollout_byte_offset", "next_rollout_ordinal",
        "rollout_byte_offset", "rollout_end_byte_offset", "rollout_end_ordinal",
    )
    for table in CACHE_TABLES:
        # Normalize absent tables/nullable version additions to the same empty
        # signature instead of treating a platform's schema as history data.
        empty = {"row_count": 0, "json_bytes": 0, "in_progress": 0, "rows_sha256": hashlib.sha256().hexdigest()}
        empty.update({"max_" + name: None for name in numeric_columns})
        result[table] = empty
        if connection is None or not table_exists(connection, table, database):
            continue
        names = set(columns(connection, table, database))
        expressions = ["COUNT(*) AS row_count"]
        for name in numeric_columns:
            if name in names:
                expressions.append(f"MAX({quote(name)}) AS {quote('max_' + name)}")
        if "item_json" in names:
            expressions.append("COALESCE(SUM(LENGTH(CAST(item_json AS BLOB))),0) AS json_bytes")
        if "status" in names:
            expressions.append("COALESCE(SUM(CASE WHEN status='inProgress' THEN 1 ELSE 0 END),0) AS in_progress")
        record = connection.execute(
            f"SELECT {','.join(expressions)} FROM {quote(database)}.{quote(table)} WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        result[table].update(dict(record))
        result[table]["rows_sha256"] = cache_rows_digest(connection, table, thread_id, database)
    return result


def epoch_seconds(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number / 1000 if number > 100_000_000_000 else number


def revision_from_connections(home, row, history, quiet_seconds=60, database="main"):
    path = safe_rollout(home, row)
    file = file_signature(path)
    summary = cache_summary(history, row["id"], database)
    metadata = {key: row.get(key) for key in ("title", "name", "archived")}
    metadata["archived"] = bool(metadata["archived"])
    transcript_revision = digest({"sha256": file["sha256"], "size": file["size"]})
    content_revision = digest({"transcript_revision": transcript_revision, "cache": summary})
    metadata_revision = digest(metadata)
    now = time.time()
    quiet = None if file["mtime_ns"] is None else max(0.0, now - file["mtime_ns"] / 1_000_000_000)
    in_progress_recent = False
    if history is not None and table_exists(history, "thread_turns", database):
        names = set(columns(history, "thread_turns", database))
        timing = "started_at" if "started_at" in names else "NULL"
        for turn in history.execute(
            f"SELECT {timing} FROM {quote(database)}.thread_turns WHERE thread_id=? AND status='inProgress'",
            (row["id"],),
        ):
            started = epoch_seconds(turn[0])
            if (started is not None and now - started < 3600) or (started is None and quiet is not None and quiet < 3600):
                in_progress_recent = True
                break
    active = in_progress_recent or (quiet is not None and quiet < quiet_seconds)
    item_count = summary.get("thread_items", {}).get("row_count", 0) + summary.get("thread_realtime_items", {}).get("row_count", 0)
    turn_count = summary.get("thread_turns", {}).get("row_count", 0)
    return {
        "id": row["id"], "source": row.get("source"), "archived": bool(row.get("archived")),
        "revision": digest({"metadata": metadata_revision, "content": content_revision}),
        "metadata_revision": metadata_revision, "content_revision": content_revision,
        "transcript_revision": transcript_revision, "transcript_size": file["size"],
        "transcript_complete": file["complete"], "active": active,
        "transcript_message_hint": file["message_hint"], "item_count": item_count, "turn_count": turn_count,
        "in_progress_recent": in_progress_recent, "quiet_seconds": quiet,
        "cache": summary, "history_mode": row.get("history_mode") or "legacy",
    }


def read_revision(home, thread_id, quiet_seconds=60):
    state = readonly(home / "state_5.sqlite")
    history_path = home / "thread_history_1.sqlite"
    history = readonly(history_path) if history_path.is_file() else None
    try:
        state.execute("BEGIN")
        if history is not None:
            history.execute("BEGIN")
        row = thread_row(state, thread_id, INSPECT_FIELDS)
        if row is None:
            return None
        return revision_from_connections(home, row, history, quiet_seconds)
    finally:
        state.close()
        if history is not None:
            history.close()


def inspect_home(args):
    home = native_home(args.home)
    sources = scoped_sources(args.scope)
    state = readonly(home / "state_5.sqlite")
    history_path = home / "thread_history_1.sqlite"
    history = readonly(history_path) if history_path.is_file() else None
    try:
        state.execute("BEGIN")
        if history is not None:
            history.execute("BEGIN")
        results = []
        failures = []
        available = set(columns(state, "threads"))
        selection = ",".join(quote(name) if name in available else "NULL AS " + quote(name) for name in INSPECT_FIELDS)
        query = "SELECT " + selection + " FROM threads WHERE source IN (" + ",".join("?" for _ in sources) + ") ORDER BY id"
        for record in state.execute(query, tuple(sorted(sources))):
            row = dict(record)
            try:
                results.append(revision_from_connections(home, row, history, args.quiet_seconds))
            except EndpointError as error:
                failures.append({"id": row["id"], "code": error.code})
        schema = {table: columns(history, table) for table in CACHE_TABLES if history is not None and table_exists(history, table)}
        return {"ok": True, "format_version": FORMAT_VERSION, "platform": "windows" if os.name == "nt" else "linux",
                "threads": results, "skipped": failures, "cache_schema": schema,
                "transcript_hash_mode": "full-streaming-sha256-v1", **backend_status()}
    finally:
        state.close()
        if history is not None:
            history.close()


def journal_summary(home, limit=10000):
    """Summarize endpoint receipts, without returning paths or their metadata."""
    counts = {"pending": 0, "committed": 0, "rolled_back": 0, "invalid": 0, "unknown": 0}
    total = 0
    truncated = False
    root = home / ".history-sync-backups"
    if root.is_symlink() or (root.exists() and not root.resolve().is_relative_to(home)):
        counts["invalid"] += 1
        return {"counts": counts, "examined": 0, "truncated": False}
    if root.is_dir():
        for path in root.glob("*/*/journal.json"):
            if total >= limit:
                truncated = True
                break
            total += 1
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    counts["invalid"] += 1
                    continue
                with path.open("rb") as stream:
                    data = stream.read(64 * 1024 + 1)
                if len(data) > 64 * 1024:
                    counts["invalid"] += 1
                    continue
                value = json.loads(data)
                status = value.get("status") if isinstance(value, dict) else None
                counts[status if status in counts and status != "invalid" else "unknown"] += 1
            except (OSError, ValueError, TypeError):
                counts["invalid"] += 1
    return {"counts": counts, "examined": total, "truncated": truncated}


def database_diagnostic(path, tables, quick_check=False):
    result = {"exists": path.is_file(), "readable": False, "schema": {}, "row_counts": {},
              "unsupported": [], "quick_check": "not_requested"}
    if not result["exists"]:
        return result
    try:
        result["size_bytes"] = path.stat().st_size
        connection = readonly(path)
        try:
            connection.execute("BEGIN")
            for table in tables:
                if table_exists(connection, table):
                    result["schema"][table] = columns(connection, table)
                    result["row_counts"][table] = connection.execute("SELECT COUNT(*) FROM " + quote(table)).fetchone()[0]
                else:
                    result["unsupported"].append({"table": table, "code": "table_missing"})
            if table_exists(connection, "_sqlx_migrations"):
                result["migration_versions"] = [row[0] for row in connection.execute("SELECT version FROM _sqlx_migrations ORDER BY version")]
            for table in CACHE_TABLES:
                names = set(result["schema"].get(table, ()))
                if not names:
                    continue
                required = {"thread_id"}
                if table == "thread_turns":
                    required.update(("turn_id", "status"))
                elif table in ("thread_items", "thread_realtime_items"):
                    required.update(("item_id", "item_json"))
                    if table == "thread_items":
                        required.add("turn_id")
                else:
                    required.update(("next_rollout_byte_offset", "next_rollout_ordinal"))
                if required - names:
                    result["unsupported"].append({"table": table, "code": "columns_missing"})
                unknown = names - set(CACHE_HASH_FIELDS[table]) - {"thread_id", "item_json", "root_turn_id"}
                if unknown:
                    nonnull = " OR ".join(quote(name) + " IS NOT NULL" for name in sorted(unknown))
                    if connection.execute("SELECT 1 FROM " + quote(table) + " WHERE " + nonnull + " LIMIT 1").fetchone():
                        result["unsupported"].append({"table": table, "code": "populated_unknown_columns"})
            if "threads" in tables and not {"id", "rollout_path", "source"}.issubset(result["schema"].get("threads", ())):
                result["unsupported"].append({"table": "threads", "code": "columns_missing"})
            if quick_check:
                # Connection-local cache limit; no WAL checkpoint or journal changes.
                connection.execute("PRAGMA cache_size=-8192")
                result["quick_check"] = "ok" if all(row[0] == "ok" for row in connection.execute("PRAGMA quick_check")) else "failed"
            result["readable"] = True
        finally:
            connection.close()
    except (OSError, sqlite3.Error, EndpointError, ValueError):
        result["error_code"] = "database_read_failed"
    return result


def diagnostic_record_counts(home, sources, state_info, history_info):
    result = {"thread_count": 0, "history_mode_counts": {}, "rollout_missing": 0,
              "rollout_outside_home": 0, "rollout_incomplete": 0, "rollout_unreadable": 0,
              "paginated_threads_without_cached_items": 0,
              "cache_row_counts": history_info.get("row_counts", {})}
    if not state_info["readable"] or state_info["unsupported"]:
        return result
    state = readonly(home / "state_5.sqlite")
    history = None
    if history_info["readable"] and not history_info["unsupported"]:
        history = readonly(home / "thread_history_1.sqlite")
    try:
        state.execute("BEGIN")
        if history is not None:
            history.execute("BEGIN")
        names = set(state_info["schema"]["threads"])
        mode = "history_mode" if "history_mode" in names else "'legacy' AS history_mode"
        query = "SELECT id,rollout_path," + mode + " FROM threads WHERE source IN (" + ",".join("?" for _ in sources) + ")"
        for row in state.execute(query, tuple(sorted(sources))):
            result["thread_count"] += 1
            history_mode = row["history_mode"] or "legacy"
            result["history_mode_counts"][history_mode] = result["history_mode_counts"].get(history_mode, 0) + 1
            try:
                path = safe_rollout(home, dict(row))
                if not path.is_file():
                    result["rollout_missing"] += 1
                elif path.stat().st_size:
                    with path.open("rb") as stream:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            result["rollout_incomplete"] += 1
            except EndpointError as error:
                key = "rollout_missing" if error.code == "rollout_missing" else "rollout_outside_home"
                result[key] += 1
            except OSError:
                result["rollout_unreadable"] += 1
            if history_mode == "paginated":
                populated = False
                if history is not None:
                    populated = any(history.execute(
                        "SELECT 1 FROM " + quote(table) + " WHERE thread_id=? LIMIT 1", (row["id"],)
                    ).fetchone() for table in ("thread_items", "thread_realtime_items"))
                if not populated:
                    result["paginated_threads_without_cached_items"] += 1
    finally:
        state.close()
        if history is not None:
            history.close()
    return result


def doctor_home(args):
    """Metadata-only diagnostics; no transcript hashing or credential reads."""
    checks = []
    def check(code, status, message):
        checks.append({"code": code, "status": status, "message": message})

    backend = backend_status()
    if not backend["backend_detection_ok"]:
        check("backend_detection_failed", "error", "无法可靠检查 Codex 进程，已有聊天更新将暂缓。")
    elif backend["backend_running"]:
        check("backend_running", "warning", "检测到本机 Codex 进程，已有聊天更新将暂缓。")
    else:
        check("backend_stopped", "ok", "未检测到本机 Codex 进程。")
    try:
        home = native_home(args.home)
    except EndpointError as error:
        messages = {"home_missing": "Codex 数据目录不存在。", "non_native_home": "请使用当前系统本地的 Codex 数据目录。"}
        check(error.code, "error", messages.get(error.code, "无法检查 Codex 数据目录。"))
        return {"ok": True, "healthy": False, "checks": checks, "databases": {}, "record_counts": {}, **backend}
    check("native_home", "ok", "数据目录属于当前系统，诊断只读取会话索引和文件状态。")
    sources = scoped_sources(args.scope)
    databases = {
        "state": database_diagnostic(home / "state_5.sqlite", ("threads",), args.quick_check),
        "history": database_diagnostic(home / "thread_history_1.sqlite", CACHE_TABLES, args.quick_check),
    }
    for kind, info in databases.items():
        label = "会话索引" if kind == "state" else "分页正文索引"
        if not info["exists"]:
            check(kind + "_database_missing", "error" if kind == "state" else "warning", label + "数据库不存在，请先打开该系统的 Codex。")
        elif not info["readable"]:
            check(kind + "_database_unreadable", "error", label + "数据库无法读取。")
        elif info["unsupported"]:
            check(kind + "_schema_unsupported", "error", label + "数据库格式不受当前同步脚本支持。")
        elif info["quick_check"] == "failed":
            check(kind + "_database_integrity", "error", label + "数据库完整性检查失败。")
        else:
            check(kind + "_database", "ok", label + "数据库可以读取，格式检查通过。")
    other_versions = sorted(int(path.stem.split("_")[1]) for path in home.glob("state_*.sqlite") if re.fullmatch(r"state_\d+", path.stem) and path.stem != "state_5")
    if other_versions:
        databases["state"]["other_versions"] = other_versions
        check("other_state_versions", "warning", "发现其他版本的会话索引；当前端点仅使用 state_5.sqlite。")
    counts = diagnostic_record_counts(home, sources, databases["state"], databases["history"])
    for code, message in (
        ("rollout_missing", "部分会话缺少记录文件，将跳过同步。"),
        ("rollout_outside_home", "部分会话记录不在当前数据目录中，将跳过同步。"),
        ("rollout_incomplete", "部分会话记录尚未写完，将等待下次检查。"),
        ("rollout_unreadable", "部分会话记录无法读取，将跳过同步。"),
        ("paginated_threads_without_cached_items", "部分分页会话没有正文索引；可能为空会话或旧版记录，需要单独检查。"),
    ):
        if counts[code]:
            check(code, "warning", message)
    if set(counts["history_mode_counts"]) - {"legacy", "paginated"}:
        check("history_mode_unsupported", "error", "发现不受当前同步脚本支持的会话正文格式。")
    journals = journal_summary(home)
    if journals["counts"]["pending"]:
        check("pending_recovery", "error", "发现未完成的同步恢复记录，请先检查备份。")
    if journals["counts"]["invalid"] or journals["counts"]["unknown"] or journals["truncated"]:
        check("journal_review_required", "warning", "部分同步恢复记录无法确认状态，需要人工检查。")
    if not any(journals["counts"].values()):
        check("journals_clear", "ok", "未发现未完成的同步恢复记录。")
    return {"ok": True, "healthy": not any(entry["status"] == "error" for entry in checks),
            "checks": checks, "databases": databases, "record_counts": counts,
            "journals": journals, "platform": "windows" if os.name == "nt" else "linux", **backend}


def write_json(path, value):
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def copy_file_checked(source, destination):
    before = source.stat()
    hasher = hashlib.sha256()
    last = b""
    total = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while True:
            chunk = reader.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            writer.write(chunk)
            total += len(chunk)
            last = chunk[-1:]
        writer.flush()
        os.fsync(writer.fileno())
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
        fail("source_changed", "Rollout changed during export")
    if total and last != b"\n":
        fail("incomplete_transcript", "Export requires a complete final JSONL line")
    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    return {"sha256": hasher.hexdigest(), "size": total, "mtime_ns": before.st_mtime_ns}


def copy_cache_rows(source, destination, thread_id, source_database="main", normalize=True):
    counts = {}
    if source is None:
        return counts
    for table in CACHE_TABLES:
        info = table_info(source, table, source_database)
        if not info:
            continue
        supported = set(CACHE_HASH_FIELDS[table]) | {"thread_id", "item_json"}
        selected = [entry for entry in info if not normalize or entry[1] in supported]
        names = [entry[1] for entry in selected]
        definitions = [f"{quote(entry[1])} {entry[2]}" for entry in selected]
        keys = [entry[1] for entry in sorted(selected, key=lambda value: value[5]) if entry[5]]
        if keys:
            definitions.append("PRIMARY KEY (" + ",".join(quote(key) for key in keys) + ")")
        destination.execute(f"CREATE TABLE {quote(table)} ({','.join(definitions)})")
        names_sql = ",".join(quote(name) for name in names)
        insert = f"INSERT INTO {quote(table)} ({names_sql}) VALUES ({','.join('?' for _ in names)})"
        count = 0
        for row in source.execute(
            f"SELECT {names_sql} FROM {quote(source_database)}.{quote(table)} WHERE thread_id=?", (thread_id,)
        ):
            destination.execute(insert, tuple(row))
            count += 1
        counts[table] = count
    destination.commit()
    return counts


def export_chat(args):
    home = native_home(args.home)
    sources = scoped_sources(args.scope)
    package = Path(args.package).expanduser().resolve()
    if package.exists() and any(package.iterdir()):
        fail("package_not_empty", "Export package directory must be absent or empty")
    package.mkdir(parents=True, exist_ok=True)
    state = readonly(home / "state_5.sqlite")
    history_path = home / "thread_history_1.sqlite"
    history = readonly(history_path) if history_path.is_file() else None
    try:
        state.execute("BEGIN")
        if history is not None:
            history.execute("BEGIN")
        available = set(columns(state, "threads"))
        export_fields = tuple(name for name in tuple(PORTABLE_FIELDS) + ("rollout_path",) if name in available)
        row = thread_row(state, args.id, export_fields)
        if row is None:
            fail("thread_missing", "Requested source chat is absent")
        if row.get("source") not in sources:
            fail("out_of_scope", "Requested source chat is outside the configured scope")
        revision = revision_from_connections(home, row, history, args.quiet_seconds)
        if args.expected and revision["revision"] != args.expected:
            fail("source_changed", "Source revision no longer matches the inspected revision")
        if revision["active"]:
            fail("source_active", "Source chat is active or has not been quiet long enough")
        if not args.metadata_only and revision["item_count"] == 0 and revision["transcript_message_hint"]:
            fail("unindexed_history", "Source transcript contains messages but has no native cached items; recover it before body sync")
        path = safe_rollout(home, row)
        payload = {key: row[key] for key in PORTABLE_FIELDS if key in row}
        meta = {"format_version": FORMAT_VERSION, "thread": payload, "source_revision": revision,
                "source_platform": "windows" if os.name == "nt" else "linux",
                "relative_rollout_path": path.relative_to(home).as_posix(),
                "metadata_only": bool(args.metadata_only)}
        if not args.metadata_only:
            if not path.is_file():
                fail("rollout_missing", "Source rollout is missing")
            cache = sqlite3.connect(package / "history.sqlite")
            try:
                meta["cache_counts"] = copy_cache_rows(history, cache, row["id"])
                if cache.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    fail("package_integrity", "Exported cache failed SQLite quick_check")
            finally:
                cache.close()
            meta["transcript"] = copy_file_checked(path, package / "transcript.jsonl")
            projected = revision.get("cache", {}).get("thread_history_projection_state", {}).get("max_next_rollout_byte_offset")
            if projected is not None and projected > meta["transcript"]["size"]:
                fail("projection_past_eof", "Source cached projection exceeds the copied transcript")
        current = read_revision(home, row["id"], args.quiet_seconds)
        if current is None or current["revision"] != revision["revision"] or current["active"]:
            fail("source_changed", "Source changed during export; discard this package and retry")
        write_json(package / "meta.json", meta)
        return {"ok": True, "id": row["id"], "revision": revision["revision"],
                "metadata_only": bool(args.metadata_only), "cache_counts": meta.get("cache_counts", {})}
    finally:
        state.close()
        if history is not None:
            history.close()


def translate_cwd(value, platform=None):
    platform = platform or ("windows" if os.name == "nt" else "linux")
    if not isinstance(value, str):
        fail("invalid_cwd", "Source cwd must be a string")
    if platform == "linux":
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
        if match:
            return "/mnt/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/")
        if value.startswith("/"):
            return value
    else:
        match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", value)
        if match:
            return match.group(1).upper() + ":\\" + (match.group(2) or "").replace("/", "\\")
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return value
    # Linux /home paths cannot be translated to a Windows drive safely. Keep a
    # native neutral cwd; original cwd remains in the backed-up/exported metadata.
    return str(Path.home())


def target_rollout(home, meta, existing):
    folder = "archived_sessions" if meta["thread"].get("archived") else "sessions"
    if existing is not None:
        relative = safe_rollout(home, existing).relative_to(home)
        tail = relative.parts[1:]
    else:
        source_relative = Path(meta["relative_rollout_path"])
        if source_relative.is_absolute() or ".." in source_relative.parts or not source_relative.parts:
            fail("package_path", "Package rollout path is unsafe")
        if source_relative.parts[0] not in ("sessions", "archived_sessions"):
            fail("package_path", "Package rollout path is outside sessions")
        tail = source_relative.parts[1:]
    if not tail:
        fail("package_path", "Package rollout path has no filename")
    result = home.joinpath(folder, *tail)
    if result.suffix != ".jsonl" or meta["thread"]["id"] not in result.name:
        fail("package_path", "Package filename does not identify the chat")
    return result


def affinity(sql_type):
    value = sql_type.upper()
    if "INT" in value:
        return "integer"
    if any(part in value for part in ("CHAR", "CLOB", "TEXT")):
        return "text"
    if "BLOB" in value or not value:
        return "blob"
    if any(part in value for part in ("REAL", "FLOA", "DOUB")):
        return "real"
    return "numeric"


def validate_cache_schema(target, package):
    for table in CACHE_TABLES:
        source_info = table_info(package, table)
        if not source_info:
            continue
        target_info = table_info(target, table, "history")
        if not target_info:
            fail("schema_incompatible", "Native target cache is missing a required table")
        source_by_name = {entry[1]: entry for entry in source_info}
        target_by_name = {entry[1]: entry for entry in target_info}
        if set(source_by_name) - set(CACHE_HASH_FIELDS[table]) - {"thread_id", "item_json"}:
            fail("schema_unsupported", "Package cache contains fields unsupported by this endpoint version")
        if set(source_by_name) - set(target_by_name):
            fail("schema_incompatible", "Package contains columns unsupported by the native target")
        for name, entry in target_by_name.items():
            if name not in source_by_name and entry[3] and entry[4] is None:
                fail("schema_incompatible", "Target has an unknown required cache column")
            if name in source_by_name and affinity(entry[2]) != affinity(source_by_name[name][2]):
                fail("schema_incompatible", "Source and target cache column types are incompatible")


def check_package(meta, package, metadata_only):
    if meta.get("format_version") != FORMAT_VERSION:
        fail("package_version", "Unsupported package format")
    row = meta.get("thread", {})
    if not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", row["id"]):
        fail("package_id", "Invalid package chat id")
    if not metadata_only and meta.get("metadata_only"):
        fail("package_metadata_only", "This package contains no transcript or cache")
    if metadata_only:
        return
    transcript = package / "transcript.jsonl"
    declared = meta.get("transcript", {})
    hasher = hashlib.sha256()
    size = 0
    last = b""
    with transcript.open("rb") as stream:
        while True:
            data = stream.read(CHUNK_SIZE)
            if not data:
                break
            size += len(data)
            last = data[-1:]
            hasher.update(data)
    if size != declared.get("size") or hasher.hexdigest() != declared.get("sha256") or (size and last != b"\n"):
        fail("package_integrity", "Package transcript is incomplete or its digest does not match")
    cache = readonly(package / "history.sqlite")
    try:
        if cache.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            fail("package_integrity", "Package cache failed SQLite quick_check")
        for table in CACHE_TABLES:
            if not table_exists(cache, table):
                continue
            if cache.execute(f"SELECT 1 FROM {quote(table)} WHERE thread_id<>? LIMIT 1", (row["id"],)).fetchone():
                fail("package_integrity", "Package cache contains another chat")
        if table_exists(cache, "thread_history_projection_state"):
            offset = cache.execute("SELECT MAX(next_rollout_byte_offset) FROM thread_history_projection_state").fetchone()[0]
            if offset is not None and offset > size:
                fail("projection_past_eof", "Package projection exceeds the transcript length")
    finally:
        cache.close()


def backup_chat(home, target, row, thread_id):
    folder = home / ".history-sync-backups" / thread_id / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True, exist_ok=False)
    backup = sqlite3.connect(folder / "history.sqlite")
    try:
        copy_cache_rows(target, backup, thread_id, source_database="history", normalize=False)
    finally:
        backup.close()
    saved = {"thread": row, "format_version": FORMAT_VERSION}
    if row is not None:
        path = safe_rollout(home, row)
        if path.is_file():
            saved["transcript"] = copy_file_checked(path, folder / "transcript.jsonl")
            saved["rollout_path"] = str(path)
    write_json(folder / "backup.json", saved)
    return folder


def import_chat(args):
    home = native_home(args.home)
    sources = scoped_sources(args.scope)
    package = Path(args.package).expanduser().resolve()
    meta = json.loads((package / "meta.json").read_text(encoding="utf-8"))
    check_package(meta, package, args.metadata_only)
    imported = meta["thread"]
    thread_id = imported["id"]
    if imported.get("source") not in sources:
        fail("out_of_scope", "Package chat is outside the configured scope")
    if not args.expected:
        fail("expected_required", "Import requires --expected revision or --expected absent")
    state_path = home / "state_5.sqlite"
    history_path = home / "thread_history_1.sqlite"
    if not state_path.is_file() or not history_path.is_file():
        fail("database_missing", "Initialize native Codex state and paginated-history databases first")
    pending_root = home / ".history-sync-backups" / thread_id
    if pending_root.exists():
        for journal in pending_root.glob("*/journal.json"):
            if json.loads(journal.read_text(encoding="utf-8")).get("status") == "pending":
                fail("pending_recovery", "A previous import was interrupted; review its per-chat backup before retrying")
    target = sqlite3.connect(state_path, timeout=10)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("ATTACH DATABASE ? AS history", (str(history_path),))
    cache = None
    backup = None
    staged = None
    installed = False
    moved_from = None
    destination = None
    existing = None
    committed = False
    try:
        target.execute("BEGIN IMMEDIATE")
        existing = thread_row(target, thread_id)
        if existing is None:
            if args.expected != "absent":
                fail("target_changed", "Target chat is now absent but an existing revision was expected")
            if args.metadata_only:
                fail("metadata_target_missing", "Metadata-only import requires an existing target chat")
        else:
            if not args.allow_existing:
                fail("existing_not_allowed", "Existing-chat imports require a controller stopped-backend check and --allow-existing")
            if existing.get("source") not in sources:
                fail("out_of_scope", "Existing target chat is outside the configured scope")
            current = revision_from_connections(home, existing, target, args.quiet_seconds, "history")
            if args.expected == "absent" or current["revision"] != args.expected:
                fail("target_changed", "Target revision changed since inspection")
            if current["active"]:
                fail("target_active", "Target chat is active or has not been quiet long enough")
        destination = target_rollout(home, meta, existing)
        old_path = safe_rollout(home, existing) if existing is not None else None
        if destination.exists() and (old_path is None or destination != old_path):
            fail("rollout_collision", "Destination rollout already exists without the expected target record")
        if not args.metadata_only:
            cache = readonly(package / "history.sqlite")
            validate_cache_schema(target, cache)
        backup = backup_chat(home, target, existing, thread_id)
        write_json(backup / "journal.json", {"status": "pending", "id": thread_id, "expected": args.expected,
                   "destination": str(destination), "metadata_only": bool(args.metadata_only)})
        destination.parent.mkdir(parents=True, exist_ok=True)
        if args.metadata_only:
            if old_path is not None and old_path != destination:
                if not old_path.is_file():
                    fail("rollout_missing", "Cannot move a missing target rollout for archive metadata")
                os.replace(old_path, destination)
                moved_from = old_path
                installed = True
        else:
            staged = destination.with_name(destination.name + ".sync-" + uuid.uuid4().hex + ".tmp")
            copy_file_checked(package / "transcript.jsonl", staged)
            if existing is not None:
                # Recheck full bytes under the SQLite write lock before
                # replacement. Caller must also keep the native backend stopped.
                last_check = revision_from_connections(home, existing, target, args.quiet_seconds, "history")
                if last_check["revision"] != args.expected or last_check["active"]:
                    fail("target_changed", "Target changed while its backup was being prepared")
            os.replace(staged, destination)
            staged = None
            installed = True
            if old_path is not None and old_path != destination:
                old_path.unlink()
                moved_from = old_path
            for table in CACHE_TABLES:
                if not table_exists(target, table, "history"):
                    continue
                target.execute(f"DELETE FROM history.{quote(table)} WHERE thread_id=?", (thread_id,))
                if not table_exists(cache, table):
                    continue
                names = columns(cache, table)
                fields = ",".join(quote(name) for name in names)
                sql = f"INSERT INTO history.{quote(table)} ({fields}) VALUES ({','.join('?' for _ in names)})"
                for record in cache.execute(f"SELECT {fields} FROM {quote(table)} WHERE thread_id=?", (thread_id,)):
                    target.execute(sql, tuple(record))
        target_columns = set(columns(target, "threads"))
        if args.metadata_only:
            updates = {key: imported.get(key) for key in METADATA_FIELDS if key in target_columns}
        else:
            updates = {key: value for key, value in imported.items() if key in PORTABLE_FIELDS and key in target_columns and key not in LOCAL_FIELDS}
            updates["cwd"] = translate_cwd(imported.get("cwd", ""))
        updates["rollout_path"] = str(destination)
        if existing is not None:
            updates.pop("id", None)
            target.execute("UPDATE threads SET " + ",".join(quote(key) + "=?" for key in updates) + " WHERE id=?",
                           tuple(updates.values()) + (thread_id,))
        else:
            # Never import machine-specific permissions or tool configuration.
            updates.setdefault("sandbox_policy", '{"type":"read-only"}')
            updates.setdefault("approval_mode", "on-request")
            updates.setdefault("title", "")
            updates.setdefault("cwd", str(Path.home()))
            info = table_info(target, "threads")
            for entry in info:
                if entry[3] and entry[4] is None and entry[1] not in updates:
                    fail("schema_incompatible", "Native target threads table has an unknown required field")
            keys = list(updates)
            target.execute("INSERT INTO threads (" + ",".join(quote(key) for key in keys) + ") VALUES (" +
                           ",".join("?" for _ in keys) + ")", tuple(updates[key] for key in keys))
        # Capture exactly what this transaction writes. Do not checkpoint a
        # newer edit that might happen after commit in another process.
        committed_row = thread_row(target, thread_id)
        current = revision_from_connections(home, committed_row, target, args.quiet_seconds, "history")
        write_json(backup / "journal.json", {"status": "pending", "id": thread_id, "expected": args.expected,
                   "result_revision": current["revision"], "destination": str(destination),
                   "metadata_only": bool(args.metadata_only)})
        target.commit()
        committed = True
        write_json(backup / "journal.json", {"status": "committed", "id": thread_id, "expected": args.expected,
                   "result_revision": current["revision"], "destination": str(destination),
                   "metadata_only": bool(args.metadata_only)})
        return {"ok": True, "id": thread_id, "thread_id": thread_id, "revision": current["revision"],
                "record": current, "thread": current,
                "metadata_only": bool(args.metadata_only), "backup": str(backup)}
    except BaseException:
        # If commit completed but receipt writing failed, leave the durable
        # pending journal for manual recovery. Restoring just the file after a
        # successful DB commit would itself create inconsistent history.
        if committed:
            raise
        target.rollback()
        if staged is not None and staged.exists():
            staged.unlink()
        if installed and destination is not None:
            if args.metadata_only and moved_from is not None and destination.is_file():
                moved_from.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, moved_from)
            elif backup is not None and (backup / "transcript.jsonl").is_file() and existing is not None:
                restore_path = safe_rollout(home, existing)
                restore_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / "transcript.jsonl", restore_path)
                if destination != restore_path and destination.exists():
                    destination.unlink()
            elif destination.exists():
                destination.unlink()
        if backup is not None:
            write_json(backup / "journal.json", {"status": "rolled_back", "id": thread_id, "expected": args.expected})
        raise
    finally:
        if cache is not None:
            cache.close()
        target.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "export", "import", "backend-status", "doctor"):
        command = subparsers.add_parser(name)
        command.add_argument("--home", required=True)
        command.add_argument("--scope", default="cli,vscode")
        command.add_argument("--quiet-seconds", type=int, default=60)
        if name in ("export", "import"):
            command.add_argument("--package", required=True)
            command.add_argument("--expected")
            command.add_argument("--metadata-only", action="store_true")
        if name == "export":
            command.add_argument("--id", required=True)
        if name == "import":
            command.add_argument("--allow-existing", action="store_true")
        if name == "doctor":
            command.add_argument("--quick-check", action="store_true")
    return parser


def main():
    if sys.version_info < (3, 11):
        print(json.dumps({"ok": False, "code": "python_version_unsupported",
                          "message": "Python 3.11 or newer is required for bounded SQLite item reads."}))
        return 2
    args = build_parser().parse_args()
    try:
        if args.quiet_seconds < 0:
            fail("quiet_invalid", "Quiet seconds must be nonnegative")
        operation = {"inspect": inspect_home, "export": export_chat, "import": import_chat,
                     "backend-status": backend_status_command, "doctor": doctor_home}[args.command]
        result = operation(args)
    except EndpointError as error:
        print(json.dumps({"ok": False, "code": error.code, "message": str(error)}, ensure_ascii=True))
        return 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        # Do not echo source content, SQL parameter values, credentials, or
        # arbitrary malformed package data in machine-readable error output.
        print(json.dumps({"ok": False, "code": "endpoint_error", "error_type": type(error).__name__}))
        return 3
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
