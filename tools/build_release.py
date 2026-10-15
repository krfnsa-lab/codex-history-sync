#!/usr/bin/env python3
"""Build a checked, source-only release ZIP without personal runtime files.

Only the explicit allowlist is packaged. SHA256SUMS inside the package covers
every source file (the manifest cannot hash itself); the adjacent .zip.sha256
file verifies the complete ZIP bytes. No installed task or Codex home is read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile

ARCHIVE_ROOT = "codex-history-sync"
REQUIRED_FILES = frozenset((
    "history_endpoint.py", "history_sync.py", "history_merge.py", "merge_chat.py", "merge_validate.py", "setup.py", "config.example.json",
    "install.ps1", "uninstall.ps1", "sync-now.ps1", "status.ps1", "doctor.ps1", "repair-paths.ps1",
    "configure.ps1", "Common.ps1", "README.md", "CHANGELOG.md", "LICENSE", ".gitignore",
    "docs/SAFETY.md", "docs/CONFIGURATION.md", ".github/workflows/ci.yml",
    "tools/build_release.py", "tests/test_endpoint.py", "tests/test_diagnostics.py",
    "tests/test_history_endpoint_review.py", "tests/test_history_sync_review.py",
    "tests/test_prefer_longer.py", "tests/test_prefer_longer_backup.py",
    "tests/test_merge.py",
    "tests/test_merge_chat.py", "tests/test_merge_validate.py",
))
OPTIONAL_ROOT_FILES = frozenset(("CONTRIBUTING.md", "SECURITY.md", "VALIDATION.md"))
LIVE_SYNC_FILES = frozenset('vscode-live-sync/' + name for name in (
    'build_vsix.py', 'build_windows_launcher.py', 'gateway.py', 'install.py', 'install.ps1',
    'linux_launcher.py', 'preview_sync.py', 'recover.py', 'recover.ps1', 'README.md', 'LICENSE',
    'THIRD_PARTY_NOTICES.md', 'DISTLIB-LICENSE.txt', 'WINDOWS-LAUNCHER-LICENSE.txt',
    'tests/test_preview.py', 'tests/test_gateway.py', 'tests/test_recovery.py',
    'tests/test_windows_launcher.py', 'tests/native_smoke.py',
    'tests/test_install_config.py',
    'native_local_probe.py', 'tests/test_native_local_probe.py',
    'native_backend_probe.py', 'tests/test_native_backend_probe.py',
    'vscode-extension/package.json', 'vscode-extension/bridge.js', 'vscode-extension/extension.js',
    'vscode-extension/README.md', 'vscode-extension/test/bridge.test.js', 'vscode-extension/test/extension.test.js',
))
REQUIRED_FILES = REQUIRED_FILES | LIVE_SYNC_FILES
IGNORED_DIRECTORIES = frozenset((
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "runtime", "logs", "backups", ".history-sync-backups", "sessions",
    "archived_sessions", "dist", "build",
))
PRIVATE_CONFIG_NAMES = frozenset(("config.json", "auth.json", "config.toml", ".codex-global-state.json"))
TEXT_DOC_SUFFIXES = frozenset((".md", ".txt", ".rst", ".svg"))


class ReleaseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def allowed_file(relative):
    if relative in REQUIRED_FILES or relative in OPTIONAL_ROOT_FILES:
        return True
    if relative == "tests/README.md":
        return True
    parts = Path(relative).parts
    suffix = Path(relative).suffix.lower()
    if len(parts) > 1 and parts[0] == "docs" and suffix in TEXT_DOC_SUFFIXES:
        return True
    if len(parts) > 1 and parts[0] == ".github" and suffix in (".md", ".yml", ".yaml"):
        return True
    if len(parts) == 2 and parts[0] == "tests":
        return (parts[1].startswith("test_") and suffix in (".py", ".ps1")) or parts[1] == "__init__.py"
    return False


def ignored_file(name):
    value = name.lower()
    return (value in PRIVATE_CONFIG_NAMES or value.startswith(".env")
            or value.endswith((".pyc", ".pyo", ".log", ".tmp", ".bak", ".exe", ".vsix", "~"))
            or value in ('installation-result.json', 'verification-result.json')
            or ".sqlite" in value or value in (".ds_store", "thumbs.db", "sha256sums", "sha256sums.txt"))


def is_placeholder_account(value):
    value = value.strip().casefold()
    if re.fullmatch(r"<[^<>/]+>", value) or re.fullmatch(r"%[a-z_][a-z0-9_]*%", value):
        return True
    if re.fullmatch(r"\$(?:env:[a-z_][a-z0-9_]*|[a-z_][a-z0-9_]*|\{[a-z_][a-z0-9_]*\})", value):
        return True
    return value.replace("_", "-") in {
        "user", "username", "your-user", "your-username", "your-name",
        "your-linux-user", "your-windows-user", "example-user",
    }


def check_personal_references(data, project):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        raise ReleaseError("text_encoding", "All release source files must use UTF-8 text")
    normalized = text.replace("\\\\", "\\").replace("\\", "/").casefold()
    # Split the path prefixes so this scanner's own regex source is not mistaken
    # for a concrete user directory. Usernames containing spaces are supported.
    user_directory = (r"(?<![a-z0-9_])(?:[a-z]:/" + "users" + "/|/" + "home"
                      + r"/)(?P<account>[^/\r\n\"'`]+?)(?=/|[\"'`\r\n]|$)")
    for match in re.finditer(user_directory, normalized):
        if not is_placeholder_account(match.group("account")):
            raise ReleaseError("personal_reference", "Release text contains a concrete user home directory")
    forbidden_paths = [project]
    current_home = Path.home().resolve()
    # Explicit documentation placeholders stay portable even on a machine whose
    # actual account happens to have the same example name.
    if not is_placeholder_account(current_home.name):
        forbidden_paths.append(current_home)
    if project.parent.name.casefold() == "outputs":
        forbidden_paths.append(project.parent.parent)
    for path in forbidden_paths:
        value = str(path).replace("\\", "/").casefold()
        if len(value) >= 8 and value in normalized:
            raise ReleaseError("personal_reference", "Release text contains an absolute source workspace path")


def collect_release_files(project):
    project = Path(project).resolve()
    if not project.is_dir():
        raise ReleaseError("project_missing", "Project source directory does not exist")
    # Filename-only check: never read a personal configuration or runtime tree.
    if any(path.name.casefold() in PRIVATE_CONFIG_NAMES for path in project.iterdir()):
        raise ReleaseError("private_config_present", "Build from a clean source directory without personal configuration")
    missing = sorted(relative for relative in REQUIRED_FILES if not (project / relative).is_file())
    if missing:
        raise ReleaseError("required_files_missing", "Missing required release files: " + ", ".join(missing))
    payload = {}
    source_stats = {}
    for directory, directories, filenames in os.walk(project, followlinks=False):
        parent = Path(directory)
        kept = []
        for name in sorted(directories):
            if name in IGNORED_DIRECTORIES or name.startswith("run-"):
                continue
            path = parent / name
            if path.is_symlink() or not path.resolve().is_relative_to(project):
                raise ReleaseError("source_symlink", "Release directories must remain inside the project")
            kept.append(name)
        directories[:] = kept
        for name in sorted(filenames):
            if ignored_file(name):
                continue
            path = parent / name
            relative = path.relative_to(project).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(project):
                raise ReleaseError("source_symlink", "Release files must remain inside the project")
            if not allowed_file(relative):
                raise ReleaseError("file_not_allowlisted", "Source file is outside the release allowlist: " + relative)
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            signature = (before.st_size, before.st_mtime_ns)
            if signature != (after.st_size, after.st_mtime_ns):
                raise ReleaseError("source_changed", "Source changed during packaging")
            check_personal_references(data, project)
            payload[relative] = data
            source_stats[relative] = signature
    if not REQUIRED_FILES.issubset(payload):
        raise ReleaseError("required_files_missing", "Required files were excluded by a safety rule")
    return project, payload, source_stats


def checksum_manifest(payload):
    return "".join(sha256(payload[name]) + "  " + name + "\n" for name in sorted(payload)).encode("utf-8")


def verify_archive(path, payload):
    expected = {ARCHIVE_ROOT + "/" + name: data for name, data in payload.items()}
    expected[ARCHIVE_ROOT + "/SHA256SUMS"] = checksum_manifest(payload)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = archive.namelist()
            if len(entries) != len(set(entries)) or set(entries) != set(expected):
                raise ReleaseError("archive_mismatch", "ZIP entry list does not match the release allowlist")
            if archive.testzip() is not None:
                raise ReleaseError("archive_mismatch", "ZIP CRC verification failed")
            for name, data in expected.items():
                if archive.read(name) != data:
                    raise ReleaseError("archive_mismatch", "ZIP bytes differ from checked source content")
    except zipfile.BadZipFile:
        raise ReleaseError("archive_mismatch", "ZIP verification failed")


def build_release(project, output=None, check_only=False):
    project, payload, source_stats = collect_release_files(project)
    if check_only:
        return {"status": "checked", "file_count": len(payload), "files": sorted(payload)}
    if output is None:
        source = payload["history_sync.py"].decode("utf-8-sig")
        match = re.search(r"^SOFTWARE_VERSION\s*=\s*['\"]([A-Za-z0-9_.-]+)['\"]", source, re.MULTILINE)
        if match is None:
            raise ReleaseError("version_missing", "Controller must declare SOFTWARE_VERSION")
        output = project.parent / "releases" / (ARCHIVE_ROOT + "-" + match.group(1) + ".zip")
    output = Path(output).expanduser().resolve()
    if output.suffix.lower() != ".zip" or output.is_relative_to(project):
        raise ReleaseError("output_invalid", "ZIP output must be a .zip path outside the project directory")
    checksum_path = output.with_name(output.name + ".sha256")
    if output.exists() or checksum_path.exists():
        raise ReleaseError("output_exists", "Release output already exists; choose a new output filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    temporary_checksum = None
    try:
        with tempfile.NamedTemporaryFile(prefix="release-", suffix=".zip", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
        archive_payload = dict(payload)
        archive_payload["SHA256SUMS"] = checksum_manifest(payload)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(archive_payload):
                info = zipfile.ZipInfo(ARCHIVE_ROOT + "/" + name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, archive_payload[name])
        verify_archive(temporary, payload)
        for name, signature in source_stats.items():
            path = project / name
            current = path.stat()
            if signature != (current.st_size, current.st_mtime_ns) or sha256(path.read_bytes()) != sha256(payload[name]):
                raise ReleaseError("source_changed", "Source changed before the release was committed")
        archive_hash = sha256(temporary.read_bytes())
        checksum_bytes = (archive_hash + "  " + output.name + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(prefix="release-", suffix=".sha256", dir=output.parent, delete=False) as stream:
            temporary_checksum = Path(stream.name)
            stream.write(checksum_bytes)
        # Both files have been generated and verified before publishing either.
        # Hard links publish complete files atomically and refuse a concurrent
        # builder's output instead of replacing it. Temporary files share the
        # output filesystem, so cross-device links are never required.
        os.link(temporary, output)
        temporary.unlink()
        temporary = None
        try:
            os.link(temporary_checksum, checksum_path)
            temporary_checksum.unlink()
            temporary_checksum = None
        except OSError:
            output.unlink()  # This build refused pre-existing outputs.
            raise
        if sha256(output.read_bytes()) != archive_hash or checksum_path.read_bytes() != checksum_bytes:
            raise ReleaseError("archive_mismatch", "Published release checksum verification failed")
        return {"status": "built", "file_count": len(payload), "archive": str(output),
                "sha256_file": str(checksum_path), "sha256": archive_hash}
    finally:
        for path in (temporary, temporary_checksum):
            if path is not None:
                path.unlink(missing_ok=True)


def main():
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="ZIP path outside this source project")
    parser.add_argument("--check-only", action="store_true", help="Check source files without generating an archive")
    args = parser.parse_args()
    try:
        result = build_release(Path(__file__).resolve().parents[1], args.output, args.check_only)
    except ReleaseError as error:
        print(json.dumps({"status": "error", "code": error.code, "message": str(error)}))
        return 2
    except OSError:
        print(json.dumps({"status": "error", "code": "filesystem_error"}))
        return 3
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
