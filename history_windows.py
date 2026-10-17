"""Opt-in, bounded shutdown of current-user Windows Codex processes.

No command lines or window titles are read. Open process handles bind actions
to the inspected process identity, including when Windows reuses a PID.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import PureWindowsPath
import re
import time


class DesktopHostRequired(Exception):
    """The desktop app owns and can immediately restart these children."""


class WindowsProcesses:
    def __init__(self, allow_desktop_exit=False):
        self.allow_desktop_exit = allow_desktop_exit
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.security = ctypes.WinDLL('advapi32', use_last_error=True)
        signatures = {
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'ProcessIdToSessionId': ([wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                          ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.security.OpenProcessToken.restype = wintypes.BOOL
        self.security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.security.GetTokenInformation.restype = wintypes.BOOL
        self.security.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.EqualSid.restype = wintypes.BOOL
        current = self.kernel.OpenProcess(0x1000, False, os.getpid())
        if not current:
            raise OSError('Cannot verify current user')
        try:
            self.owner = self.token_user(current)
        finally:
            self.kernel.CloseHandle(current)
        self.session = self.session_id(os.getpid())

    def session_id(self, pid):
        session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
            raise OSError('Cannot verify process session')
        return session.value

    def token_user(self, handle):
        token = wintypes.HANDLE()
        if not self.security.OpenProcessToken(handle, 8, ctypes.byref(token)):
            raise OSError('Cannot verify process user')
        try:
            size = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError('Cannot verify process user')
            buffer = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise OSError('Cannot verify process user')
            return buffer  # Keep the SID pointer's backing buffer alive.
        finally:
            self.kernel.CloseHandle(token)

    def snapshot(self):
        class Entry(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                        ('heap', ctypes.c_size_t), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                        ('parent', wintypes.DWORD), ('priority', wintypes.LONG), ('flags', wintypes.DWORD),
                        ('name', wintypes.WCHAR * 260)]
        for name in ('Process32FirstW', 'Process32NextW'):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = [wintypes.HANDLE, ctypes.POINTER(Entry)], wintypes.BOOL
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise OSError('Cannot enumerate processes')
        rows = []
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            found = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while found:
                rows.append({'pid': entry.pid, 'parent': entry.parent, 'name': entry.name})
                found = self.kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise OSError('Cannot enumerate processes')
            return rows
        finally:
            self.kernel.CloseHandle(handle)

    def open_candidates(self):
        rows = self.snapshot()
        parents = {row['pid']: row['parent'] for row in rows}
        protected, current = set(), os.getpid()
        while current and current not in protected:
            protected.add(current)
            current = parents.get(current, 0)
        opened = []
        def add_verified(row):
            if row['pid'] in protected:
                raise OSError('Synchronizer is hosted by Windows Codex')
            handle = self.kernel.OpenProcess(0x1000 | 0x100000 | 1, False, row['pid'])
            if not handle:
                if ctypes.get_last_error() == 87:  # Exited since snapshot.
                    return
                raise OSError('Cannot open a Codex process')
            opened.append({**row, 'handle': handle})
            if not self.alive(opened[-1]):
                self.release(opened.pop())
                return
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                raise OSError('Cannot verify process executable')
            if PureWindowsPath(path.value).name.casefold() != row['name'].casefold():
                raise OSError('Process identity changed')
            owner = self.token_user(handle)
            if (self.session_id(row['pid']) != self.session or not self.security.EqualSid(
                    ctypes.c_void_p.from_buffer(owner).value, ctypes.c_void_p.from_buffer(self.owner).value)):
                self.release(opened.pop())
                return
            creation, end, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(end),
                                               ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise OSError('Cannot verify process creation time')
            opened[-1]['created_at'] = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        try:
            for row in rows:
                if re.fullmatch(r'codex(?:[-_][a-z0-9_.-]+)?\.exe', row['name'], re.I):
                    add_verified(row)
            by_pid = {row['pid']: row for row in rows}
            hosts = {row['parent'] for row in opened if self.alive(row)
                     if by_pid.get(row['parent'], {}).get('name', '').casefold() == 'chatgpt.exe'}
            if hosts and not self.allow_desktop_exit:
                raise DesktopHostRequired()
            for pid in hosts:
                children = [row for row in opened if row['parent'] == pid]
                add_verified(by_pid[pid])
                parent = next((row for row in opened if row['pid'] == pid), None)
                if parent is not None and any(parent['created_at'] > child['created_at'] for child in children):
                    raise OSError('Desktop parent process identity changed')
            return opened
        except BaseException:
            for row in opened:
                self.release(row)
            raise

    def alive(self, row):
        result = self.kernel.WaitForSingleObject(row['handle'], 0)
        if result not in (0, 258):
            raise OSError('Cannot verify process liveness')
        return result == 258

    def request_close(self, rows):
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL
        identities = {row['pid']: row for row in rows}
        errors = []

        @callback_type
        def visit(window, _):
            try:
                pid = wintypes.DWORD()
                self.user.GetWindowThreadProcessId(window, ctypes.byref(pid))
                row = identities.get(pid.value)
                if row and self.user.IsWindowVisible(window) and self.alive(row):
                    self.user.PostMessageW(window, 0x10, 0, 0)  # WM_CLOSE, no forced UI dismissal.
            except Exception:
                errors.append(True)
            return True
        if not self.user.EnumWindows(visit, 0) or errors:
            raise OSError('Cannot request normal application exit')

    def terminate(self, row):
        if self.alive(row) and not self.kernel.TerminateProcess(row['handle'], 0):
            raise OSError('Cannot stop Codex process')

    def release(self, row):
        self.kernel.CloseHandle(row['handle'])


def stop_windows_codex(*, api=None, clock=time.monotonic, sleep=time.sleep, heartbeat=None, allow_desktop_exit=False):
    """Try normal exit for 5s, then stop only verified processes; never restart.

    The caller must hold the sync lock and validate explicit WSL winner choices
    plus both backups before calling. Native import guards still run afterwards.
    """
    if api is None and os.name != 'nt':
        return {'status': 'failed', 'reason': 'windows_exit_unavailable'}
    rows = []
    forced = 0
    try:
        api = api or WindowsProcesses(allow_desktop_exit=allow_desktop_exit)
        rows = api.open_candidates()
        api.request_close(rows)
        deadline = clock() + 5
        while any(api.alive(row) for row in rows) and clock() < deadline:
            if heartbeat:
                heartbeat()
            sleep(0.25)
        # An explicitly approved desktop host must exit before its remaining
        # children, or it can immediately restart them during synchronization.
        hosts = [row for row in rows if row.get('name', '').casefold() == 'chatgpt.exe']
        for row in hosts:
            if api.alive(row):
                api.terminate(row)
                forced += 1
        pending = [row for row in rows if row not in hosts]
        while pending:
            parents = {row['parent'] for row in pending}
            leaves = [row for row in pending if row['pid'] not in parents] or pending[:1]
            for row in leaves:
                if api.alive(row):
                    api.terminate(row)
                    forced += 1
                pending.remove(row)
        deadline = clock() + 5
        while any(api.alive(row) for row in rows) and clock() < deadline:
            if heartbeat:
                heartbeat()
            sleep(0.25)
        remaining = sum(api.alive(row) for row in rows)
        return {'status': 'stopped' if not remaining else 'waiting',
                'reason': 'windows_exit_incomplete' if remaining else 'windows_exit_completed',
                'process_count': len(rows), 'forced_count': forced}
    except DesktopHostRequired:
        return {'status': 'waiting', 'reason': 'windows_exit_desktop_required', 'forced_count': 0}
    except (OSError, ValueError, AttributeError):
        return {'status': 'failed', 'reason': 'windows_exit_failed', 'forced_count': forced}
    finally:
        if api is not None:
            for row in rows:
                api.release(row)
