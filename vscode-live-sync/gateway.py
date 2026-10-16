#!/usr/bin/env python3
"""Opt-in Linux stdio gateway. Persistent state never contains protocol bodies."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import sys
import time
import uuid

PROTOCOL_VERSION = 1
PAUSED_ERROR = -32098
MAX_PROTOCOL_LINE = 128 * 1024 * 1024
PASSIVE_ITEM_TYPES = {"userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"}
READ_ONLY_METHODS = {
    "thread/list", "thread/read", "thread/loaded/list", "thread/turns/list", "thread/items/list",
    "thread/queue/list", "thread/backgroundTerminals/list", "model/list", "config/read",
    "configRequirements/read", "account/read", "account/rateLimits/read", "account/usage/read",
    "app/list", "app/installed", "skills/list", "hooks/list", "mcpServerStatus/list",
    "project/list", "project/read", "threadSection/list", "permissionProfile/list",
    "experimentalFeature/list", "remoteControl/status/read", "server/diagnostics",
    "getAuthStatus",
}


def private_directory(path: Path) -> Path:
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ValueError("state directory must belong to current user")
    path.chmod(0o700)
    return path


def atomic_state(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def rpc_object(raw: bytes):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def rpc_key(identifier):
    # JSON-RPC permits string and numeric IDs; distinguish them without logging.
    return (type(identifier).__name__, str(identifier))


def object_or_empty(value):
    return value if isinstance(value, dict) else {}


class Gateway:
    def __init__(self, binary: str, native_args: list[str], state_dir: Path, pause_timeout=120.0, shutdown_timeout=5.0):
        self.binary, self.native_args = binary, native_args
        self.state_dir = private_directory(state_dir)
        self.instances = private_directory(self.state_dir / "instances")
        self.instance_id = uuid.uuid4().hex
        self.nonce = secrets.token_hex(32)
        self.socket_path = self.instances / (self.instance_id + ".sock")
        if len(os.fsencode(self.socket_path)) > 103:
            raise ValueError("state directory is too long for a Unix socket")
        self.state_path = self.instances / (self.instance_id + ".json")
        self.pause_timeout, self.shutdown_timeout = pause_timeout, shutdown_timeout
        self.gate = asyncio.Lock()
        self.child = None
        self.server = None
        self.paused = False
        self.starting = True
        self.shutdown_failed = False
        self.child_group_stopped = False
        self.pending = {}  # Method / thread ID / process handle or pagination marker, never bodies.
        self.server_pending = set()
        self.turns = set()
        self.activities = set()
        self.process_handles = {}
        self.last_activity = time.monotonic()
        self.last_state = None
        self.done = asyncio.Event()
        self.io_tasks = []

    def state(self):
        running = self.child is not None and self.child.returncode is None
        return dict(protocol_version=PROTOCOL_VERSION, instance_id=self.instance_id, pid=os.getpid(), parent_pid=os.getppid(),
                    child_pid=self.child.pid if running else None, child_running=running,
                    pending_request_count=sum(len(entries) for entries in self.pending.values()), server_request_count=len(self.server_pending),
                    active_turn_count=len(self.turns), known_activity_count=len(self.activities),
                    idle_seconds=round(max(0.0, time.monotonic() - self.last_activity), 3),
                    busy=bool(self.pending or self.server_pending or self.turns or self.activities or self.starting or self.shutdown_failed),
                    paused=self.paused, starting=self.starting, shutdown_failed=self.shutdown_failed)

    def publish(self):
        state = self.state()
        if state != self.last_state:
            atomic_state(self.state_path, {**state, "socket_path": str(self.socket_path), "nonce": self.nonce})
            self.last_state = state

    @staticmethod
    def output(raw):
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()

    def reject_paused(self, message):
        if message is not None and message.get("method") and "id" in message:
            self.output((json.dumps(dict(id=message["id"], error=dict(code=PAUSED_ERROR,
                         message="History synchronization paused the backend; reload the Codex window."))) + "\n").encode())

    async def terminate_child(self):
        child = self.child
        if child is None or self.child_group_stopped:
            return True
        # The child starts a new session. Code-mode helpers may create their
        # own process groups inside it; stop only groups in that owned session.
        try:
            if not self.owned_group_live(child.pid):
                await child.wait()
                self.child_group_stopped = True
                return True
            self.signal_owned_session(child.pid, signal.SIGTERM)
            deadline = time.monotonic() + self.shutdown_timeout
            while self.owned_group_live(child.pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.025)
            if self.owned_group_live(child.pid):
                self.signal_owned_session(child.pid, signal.SIGKILL)
            await child.wait()
            deadline = time.monotonic() + 1
            while self.owned_group_live(child.pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.025)
            if self.owned_group_live(child.pid):
                self.shutdown_failed = True
                return False
            self.child_group_stopped = True
            return True
        except OSError:
            self.shutdown_failed = True
            return False

    @staticmethod
    def owned_session_groups(session_pid):
        """The kernel session ID remains owned while its descendants survive."""
        groups = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                uid = entry.stat().st_uid
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[3]) != session_pid or fields[0] == "Z":
                    continue
                # Unexpected ownership/session cannot count as a confirmed exit.
                if uid != os.getuid() or int(fields[2]) <= 1:
                    raise PermissionError("owned session validation failed")
                groups.add(int(fields[2]))
            except (FileNotFoundError, ProcessLookupError):
                continue
        return groups

    @classmethod
    def owned_group_live(cls, group_pid):
        return bool(cls.owned_session_groups(group_pid))

    @classmethod
    def signal_owned_session(cls, session_pid, sig):
        for group in cls.owned_session_groups(session_pid):
            # Recheck membership before each signal; another process's group
            # must never be selected merely because its executable is Codex.
            if group in cls.owned_session_groups(session_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, sig)

    async def control(self, reader, writer):
        try:
            peer = writer.get_extra_info("socket")
            if hasattr(socket, "SO_PEERCRED"):
                _pid, uid, _gid = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    return
            raw = await asyncio.wait_for(reader.readline(), 3)
            request = rpc_object(raw)
            authorized = (request is not None and request.get("instance_id") == self.instance_id
                          and isinstance(request.get("nonce"), str)
                          and hmac.compare_digest(request["nonce"], self.nonce))
            if not authorized:
                response = dict(ok=False, error="unauthorized")
            elif request.get("action") == "status":
                async with self.gate:
                    response = dict(ok=True, state=self.state())
            elif request.get("action") == "prepare_pause":
                async with self.gate:
                    minimum_idle = request.get("minimum_idle_seconds", 60)
                    if not (self.state_dir / "pause.json").is_file():
                        response = dict(ok=False, error="global_pause_required", state=self.state())
                    elif self.state()["busy"]:
                        response = dict(ok=False, error="busy", state=self.state())
                    elif (not isinstance(minimum_idle, (int, float)) or isinstance(minimum_idle, bool)
                          or not 0 <= minimum_idle <= 86400
                          or time.monotonic() - self.last_activity < minimum_idle):
                        response = dict(ok=False, error="recent_activity", state=self.state())
                    else:
                        # No future stdin request can enter the child after this point.
                        self.paused = True
                        self.publish()
                        stopped = await self.terminate_child()
                        self.publish()
                        response = (dict(ok=True, paused=True, state=self.state()) if stopped
                                    else dict(ok=False, error="stop_failed", state=self.state()))
            else:
                response = dict(ok=False, error="unknown_action")
            writer.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def receive_native(self, message):
        if message is None:
            return
        before = (set(self.turns), set(self.activities), set(self.server_pending))
        method, params = message.get("method"), object_or_empty(message.get("params"))
        if "id" in message:
            key = rpc_key(message["id"])
            if method:
                self.server_pending.add(key)
            else:
                entries = self.pending.get(key)
                previous = entries.pop(0) if entries else None
                if entries == []:
                    self.pending.pop(key, None)
                if previous and not message.get("error"):
                    sent_method, tid, handle = previous
                    result = object_or_empty(message.get("result"))
                    response_thread = object_or_empty(result.get("thread"))
                    if response_thread.get("status") is not None:
                        # Stored thread reads cannot clear activity already observed
                        # on this live connection. Only lifecycle notifications can.
                        self.observe_thread_status(str(response_thread.get("id", tid)), response_thread["status"], can_clear=False)
                    self.observe_returned_turns(str(response_thread.get("id", tid)), response_thread.get("turns"))
                    if sent_method in {"turn/start", "thread/queue/start", "review/start", "turn/addUserMessage"}:
                        turn = object_or_empty(result.get("turn"))
                        if turn.get("status") in {"inProgress", "running", "active"}:
                            self.turns.add((tid, str(turn.get("id", "unknown"))))
                    if sent_method == "thread/queue/list":
                        # Only a structurally known empty list proves the queue cleared.
                        entries = result.get("data")
                        if handle == "first_page" and isinstance(entries, list) and not entries and result.get("nextCursor", "unknown") is None:
                            self.activities.discard(("queue", tid))
                        else:
                            self.activities.add(("queue", tid))
                    if sent_method.startswith("thread/backgroundTerminals/"):
                        terminals = result.get("data")
                        if (sent_method == "thread/backgroundTerminals/list" and handle == "first_page"
                            and isinstance(terminals, list) and not terminals and result.get("nextCursor", "unknown") is None):
                            self.activities.discard((tid, "backgroundTerminals"))
                        else:
                            self.activities.add((tid, "backgroundTerminals"))
                elif previous and message.get("error") and previous[0] == "process/spawn":
                    handle = previous[2]
                    count = self.process_handles.get(handle, 0) - 1
                    if count <= 0:
                        self.process_handles.pop(handle, None)
                        self.activities.discard(("process", handle))
                    else:
                        self.process_handles[handle] = count
                if previous and previous[0] not in READ_ONLY_METHODS:
                    self.last_activity = time.monotonic()
        if not method:
            if before != (self.turns, self.activities, self.server_pending):
                self.last_activity = time.monotonic()
            return
        notification_thread = object_or_empty(params.get("thread"))
        tid = str(params.get("threadId", notification_thread.get("id", "")))
        turn = object_or_empty(params.get("turn"))
        turn_id = str(turn.get("id", params.get("turnId", "unknown")))
        if method == "turn/started":
            self.turns.add((tid, turn_id))
        elif method in {"turn/completed", "turn/failed", "turn/cancelled"}:
            self.turns = {entry for entry in self.turns if not (entry[0] == tid and (entry[1] == turn_id or turn_id == "unknown"))}
        elif method == "thread/status/changed":
            self.observe_thread_status(tid, params.get("status"))
        elif method == "thread/started" and notification_thread.get("status") is not None:
            self.observe_thread_status(tid, notification_thread["status"])
        elif method in {"item/started", "item/completed"}:
            item = object_or_empty(params.get("item"))
            activity = (tid, str(item.get("id", params.get("itemId", "unknown"))))
            if method == "item/completed":
                self.activities.discard(activity)
                self.observe_background_item(tid, item)
            elif item.get("type") not in PASSIVE_ITEM_TYPES:
                self.activities.add(activity)
        elif method in {"process/exited", "process/completed", "process/exit"}:
            handle = str(params.get("processHandle", params.get("processId", params.get("pid", "unknown"))))
            self.activities.discard(("process", handle))
            self.process_handles.pop(handle, None)
        elif method == "process/outputDelta":
            self.activities.add(("process", str(params.get("processHandle", "unknown"))))
        elif method in {"hook/started", "hook/completed"}:
            run = object_or_empty(params.get("run"))
            hook_id = str(run.get("id", "unknown"))
            activity = ("hook", tid, hook_id)
            if method == "hook/started":
                self.activities.add(activity)
            elif hook_id != "unknown":
                self.activities.discard(activity)
        elif method == "thread/queue/changed":
            self.activities.add(("queue", tid))
        elif method in {"thread/realtime/started", "thread/backgroundTerminals/started"}:
            self.activities.add((tid, method.split("/")[1]))
        elif method in {"thread/realtime/closed", "thread/backgroundTerminals/completed", "thread/backgroundTerminals/terminated"}:
            self.activities.discard((tid, method.split("/")[1]))
        if before != (self.turns, self.activities, self.server_pending):
            self.last_activity = time.monotonic()

    def observe_returned_turns(self, tid, turns):
        if isinstance(turns, list):
            for turn in turns:
                if isinstance(turn, dict):
                    if turn.get("status") in {"inProgress", "running", "active"}:
                        self.turns.add((tid, str(turn.get("id", "unknown"))))
                    if isinstance(turn.get("items"), list):
                        for item in turn["items"]:
                            self.observe_background_item(tid, object_or_empty(item))

    def observe_background_item(self, tid, item):
        if item.get("type") == "commandExecution" and item.get("processId") is not None and item.get("exitCode") is None:
            # An item can finish while its PTY survives as a background terminal.
            # A later complete first-page terminal list is required to prove exit.
            self.activities.add((tid, "backgroundTerminals"))

    def receive_client(self, message):
        if message is None or "id" not in message:
            return
        key = rpc_key(message["id"])
        method = message.get("method")
        if method:
            params = object_or_empty(message.get("params"))
            handle = str(params.get("processHandle", "unknown")) if method == "process/spawn" else ""
            if method in {"thread/queue/list", "thread/backgroundTerminals/list"}:
                handle = "first_page" if params.get("cursor") is None else "page"
            self.pending.setdefault(key, []).append((method, str(params.get("threadId", "")), handle))
            if method == "process/spawn":
                self.process_handles[handle] = self.process_handles.get(handle, 0) + 1
                self.activities.add(("process", handle))
            if method not in READ_ONLY_METHODS:
                self.last_activity = time.monotonic()
        else:
            self.server_pending.discard(key)
            self.last_activity = time.monotonic()

    def observe_thread_status(self, tid, status, can_clear=True):
        kind = status.get("type") if isinstance(status, dict) else status
        if kind in {"active", "running", "inProgress"}:
            self.turns.add((tid, "status"))
        elif kind in {"idle", "notLoaded"}:
            if can_clear:
                self.turns = {entry for entry in self.turns if entry[0] != tid}
                self.activities.discard(("threadStatus", tid))
        else:
            # A future SDK status is not evidence that writes have stopped.
            self.activities.add(("threadStatus", tid))

    async def native_output(self):
        try:
            while raw := await self.child.stdout.readline():
                async with self.gate:
                    self.receive_native(rpc_object(raw))
                    self.publish()
                    self.output(raw)
        finally:
            if not self.paused:
                self.done.set()

    async def native_errors(self):
        while raw := await self.child.stderr.read(65536):
            # Forward without inspecting or persisting stderr.
            sys.stderr.buffer.write(raw)
            sys.stderr.buffer.flush()

    async def input(self):
        reader = asyncio.StreamReader(limit=MAX_PROTOCOL_LINE)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        try:
            while raw := await reader.readline():
                async with self.gate:
                    message = rpc_object(raw)
                    if self.paused or self.child is None or self.child.returncode is not None:
                        self.reject_paused(message)
                        continue
                    self.receive_client(message)
                    self.publish()
                    self.child.stdin.write(raw)
                    await self.child.stdin.drain()
        finally:
            transport.close()
            self.done.set()

    async def run(self):
        old_umask = os.umask(0o077)
        try:
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(signum, self.done.set)
            self.server = await asyncio.start_unix_server(self.control, path=str(self.socket_path), limit=16384)
            self.socket_path.chmod(0o600)
            self.publish()  # Discoverable before launch, including during global pause.
            deadline = time.monotonic() + self.pause_timeout
            while (self.state_dir / "pause.json").exists():
                if self.done.is_set():
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("global synchronization pause did not clear")
                await asyncio.sleep(0.05)
            self.child = await asyncio.create_subprocess_exec(self.binary, *self.native_args,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_PROTOCOL_LINE)
            if (self.state_dir / "pause.json").exists():
                # A pause established across launch must never leave an unreported writer.
                self.paused = True
                await self.terminate_child()
            self.starting = False
            self.publish()
            self.io_tasks = [asyncio.create_task(self.input()), asyncio.create_task(self.native_output()), asyncio.create_task(self.native_errors())]
            for task in self.io_tasks:
                task.add_done_callback(lambda done: self.done.set() if not done.cancelled() and done.exception() else None)
            await self.done.wait()
        finally:
            self.paused = True
            await self.terminate_child()
            for task in self.io_tasks:
                task.cancel()
            await asyncio.gather(*self.io_tasks, return_exceptions=True)
            if self.server:
                self.server.close()
                await self.server.wait_closed()
            for path in (self.state_path, self.socket_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(description="Optional WSL Codex stdio synchronization gateway")
    parser.add_argument("--binary", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--pause-timeout", type=float, default=120)
    parser.add_argument("--shutdown-timeout", type=float, default=5)
    parser.add_argument("native_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    native_args = args.native_args[1:] if args.native_args[:1] == ["--"] else args.native_args
    if "app-server" not in native_args or any(command in native_args for command in ("generate-ts", "generate-json-schema", "daemon", "proxy", "help", "--help", "-h", "--version", "-V")):
        os.execv(args.binary, [args.binary, *native_args])
    if sys.platform != "linux":
        parser.error("the app-server gateway is supported only on Linux/WSL")
    if args.pause_timeout <= 0 or args.shutdown_timeout <= 0:
        parser.error("timeouts must be positive")
    try:
        asyncio.run(Gateway(args.binary, native_args, args.state_dir, args.pause_timeout, args.shutdown_timeout).run())
    except (OSError, ValueError, TimeoutError, KeyboardInterrupt):
        print("Codex history-sync gateway stopped safely; inspect its configuration or reload the window.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
