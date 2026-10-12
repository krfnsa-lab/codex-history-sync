"""Create a native passthrough executable using this Python's pip launcher."""
import argparse
from io import BytesIO
from pathlib import Path
import sys
import zipfile


MAIN = '''import os, subprocess, sys, json
from pathlib import Path
binary = os.environ.get("CODEX_HISTORY_SYNC_REAL_CLI")
if not binary or not os.path.isfile(binary):
    config_path = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexHistorySyncLive" / "launcher.json"
    try:
        binary = json.loads(config_path.read_text(encoding="utf-8"))["binary"]
    except (OSError, ValueError, KeyError):
        binary = None
if not binary or not os.path.isfile(binary):
    candidates = []
    for package in (Path.home() / ".vscode" / "extensions").glob("openai.chatgpt-*/package.json"):
        try:
            meta = json.loads(package.read_text(encoding="utf-8"))
            target = package.parent / "bin/windows-x86_64/codex.exe"
            if meta.get("publisher") == "openai" and meta.get("name") == "chatgpt" and target.is_file():
                candidates.append((package.stat().st_mtime_ns, str(target)))
        except (OSError, ValueError):
            pass
    binary = max(candidates)[1] if candidates else None
if not binary or not os.path.isfile(binary):
    sys.stderr.write("Codex History Sync: native Codex path is unavailable. Restore chatgpt.cliExecutable before disabling the companion.\\n")
    sys.exit(78)
if Path(binary).resolve() == Path(sys.argv[0]).resolve():
    sys.exit(78)
try:
    # Keep the inherited protocol pipes; do not log inputs, credentials or args.
    child = subprocess.Popen([binary, *sys.argv[1:]], stdin=sys.stdin, stdout=sys.stdout,
                             stderr=sys.stderr, close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sys.exit(child.wait())
except OSError:
    sys.stderr.write("Codex History Sync: could not start native Codex.\\n")
    sys.exit(78)
'''


def build(output):
    if sys.platform != 'win32':
        raise RuntimeError('Build with native Windows Python')
    from pip._vendor import distlib
    launcher = Path(distlib.__file__).parent / ('t64.exe' if sys.maxsize > 2**32 else 't32.exe')
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('__main__.py', MAIN)
    interpreter = str(Path(sys.executable).resolve())
    if '\n' in interpreter or '\r' in interpreter:
        raise ValueError('Invalid interpreter path')
    shebang = ('#!"' + interpreter + '"\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(launcher.read_bytes() + shebang + stream.getvalue())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.output)
