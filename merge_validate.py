"""Validate a merged copy with a native Linux Codex and a loopback mock provider.

Only database schemas and migration records are cloned from --home. No credentials
are copied. This checks an isolated environment, not a filesystem/network sandbox.
The native CLI must already be installed; this does not download or authenticate.
"""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import history_endpoint as endpoint

FILES = ('meta.json', 'history.sqlite', 'transcript.jsonl')
RPC_TIMEOUT = 40
PAGE_LIMIT = 100
MAX_PAGES = 4096


class ValidationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def package_hashes(package):
    result = {}
    for name in FILES:
        digest = hashlib.sha256()
        with (package / name).open('rb') as stream:
            for part in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(part)
        result[name] = digest.hexdigest()
    return result


def clone_schema(source, destination):
    """Open the original database read-only; copy no chat rows."""
    require(not destination.exists(), 'Isolated database already exists')
    with closing(endpoint.readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.execute('BEGIN')
        schema = src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END").fetchall()
        for kind, name, sql in schema:
            if kind == 'table':
                require('VIRTUAL TABLE' not in sql.upper(), 'Native virtual schema unsupported')
                dst.execute(sql)
        if any(name == '_sqlx_migrations' for _, name, _ in schema):
            count = len(src.execute('PRAGMA table_info(_sqlx_migrations)').fetchall())
            dst.executemany('INSERT INTO _sqlx_migrations VALUES (' + ','.join('?' for _ in range(count)) + ')', src.execute('SELECT * FROM _sqlx_migrations'))
        for kind, name, sql in schema:
            if kind != 'table':
                dst.execute(sql)
        dst.commit()


def texts(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'text' and isinstance(child, str):
                yield child
            else:
                yield from texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from texts(child)


def branch_text(path, common_turns):
    require(type(common_turns) is int and common_turns > 0, 'Invalid common-history boundary')
    started = 0
    result = []
    with path.open(encoding='utf-8') as stream:
        for raw in stream:
            record = json.loads(raw)
            payload = record.get('payload') or {}
            if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                started += 1
            if (started > common_turns and record.get('type') == 'response_item'
                    and payload.get('type') == 'message' and payload.get('role') in ('user', 'assistant')
                    and payload.get('channel') != 'analysis'):
                result.extend(texts(payload.get('content', [])))
    return result


def pages(rpc, method, params, expected_count=None):
    result, cursor, seen = [], None, set()
    limit = MAX_PAGES if expected_count is None else min(MAX_PAGES, expected_count // PAGE_LIMIT + 2)
    for _ in range(limit):
        page = rpc(method, {**params, 'limit': PAGE_LIMIT, 'cursor': cursor})
        require(isinstance(page, dict) and isinstance(page.get('data'), list), 'Unsupported native page response')
        result.extend(page['data'])
        cursor = page.get('nextCursor')
        if cursor is None:
            return result
        require(isinstance(cursor, str) and cursor and cursor not in seen, 'Native pagination did not advance')
        seen.add(cursor)
    raise ValidationError('Native pagination exceeded limit')


def isolated_env(root):
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    environment.update(HOME=str(root), CODEX_HOME=str(root), XDG_CONFIG_HOME=str(root / 'xdg-config'),
                       XDG_DATA_HOME=str(root / 'xdg-data'), XDG_CACHE_HOME=str(root / 'xdg-cache'), TMPDIR=str(root / 'tmp'))
    return environment


def branch_context_check(request_text, expected, marker, other_markers):
    """A duplicate in the common prefix cannot stand in for a branch message."""
    if not expected:
        return {'expected_visible_text_parts': 0, 'missing_text_parts': 0, 'order_preserved': True}
    positions = [index for index, text in enumerate(request_text) if text == marker]
    require(marker is not None and len(positions) == 1, 'Missing or repeated branch marker in model context')
    start = positions[0] + 1
    end = next((index for index in range(start, len(request_text)) if request_text[index] in other_markers), len(request_text))
    region = request_text[start:end]
    expected_counts, actual_counts = Counter(expected), Counter(region)
    cursor = 0
    for text in region:
        if cursor < len(expected) and text == expected[cursor]:
            cursor += 1
    return {'expected_visible_text_parts': len(expected), 'missing_text_parts': sum((expected_counts - actual_counts).values()),
            'order_preserved': cursor == len(expected)}


def stop_session(process):
    # start_new_session gives this child a session distinct from the caller.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        if sig == signal.SIGTERM:
            # Also terminate any descendants remaining after the leader exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            break
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()


def native_checks(root, codex, package, compiler, report):
    captured = queue.Queue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                size = int(self.headers.get('content-length', '0'))
                require(0 < size <= 128 * 1024 * 1024, 'Mock request exceeded limit')
                captured.put(json.loads(self.rfile.read(size)))
            except (ValueError, ValidationError):
                captured.put({'_invalid_request': True})
            body = b'{"error":{"message":"isolated validation complete","type":"invalid_request_error","code":"validation_complete"}}'
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    config = ('model_provider="merge_validation"\nmodel="gpt-6.1-sol"\ncli_auth_credentials_store="file"\n'
              '[analytics]\nenabled=false\n[feedback]\nenabled=false\n[model_providers.merge_validation]\n'
              'name="Local isolated validation"\nbase_url="http://127.0.0.1:' + str(server.server_port) + '/v1"\n'
              'wire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\n')
    (root / 'config.toml').write_text(config, encoding='utf-8')
    (root / 'tmp').mkdir()
    process = None
    messages = queue.Queue()
    request_id = 0
    deadline_all = time.monotonic() + 180
    try:
        version = subprocess.run([str(codex), '--version'], env=isolated_env(root), cwd=root, capture_output=True, text=True, timeout=10)
        require(version.returncode == 0, 'Native CLI version check failed')
        report['cli_version'] = version.stdout.strip()[:200]
        process = subprocess.Popen([str(codex), 'app-server'], cwd=root, env=isolated_env(root), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', start_new_session=True)

        def reader():
            for raw in process.stdout:
                try:
                    messages.put(json.loads(raw))
                except ValueError:
                    pass
            messages.put({'_eof': True})

        def errors():
            # Drain stderr without storing user data or unbounded output.
            for _ in process.stderr:
                pass

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()

        def rpc(method, params):
            nonlocal request_id
            request_id += 1
            process.stdin.write(json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            deadline = min(time.monotonic() + RPC_TIMEOUT, deadline_all)
            while time.monotonic() < deadline:
                try:
                    response = messages.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                require(isinstance(response, dict) and not response.get('_eof'), 'Native server exited')
                if response.get('id') == request_id:
                    require('error' not in response and 'result' in response, 'Native RPC failed: ' + method)
                    return response['result']
            raise ValidationError('Native RPC timed out: ' + method)

        tid = compiler['new_thread_id']
        rpc('initialize', {'clientInfo': {'name': 'merge_copy_validation', 'version': '1.0'}, 'capabilities': {'experimentalApi': True}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        read = rpc('thread/read', {'threadId': tid, 'includeTurns': True})
        turns = pages(rpc, 'thread/turns/list', {'threadId': tid, 'itemsView': 'full', 'sortDirection': 'asc'}, compiler['merged_turns'])
        items = pages(rpc, 'thread/items/list', {'threadId': tid, 'sortDirection': 'asc'}, compiler['merged_items'])
        with closing(endpoint.readonly(package / 'history.sqlite')) as db:
            expected_pairs = {(r[0], r[1]) for r in db.execute('SELECT turn_id,item_id FROM thread_items')}
            expected_turns = {r[0] for r in db.execute('SELECT turn_id FROM thread_turns')}
            source_turns = set()
            for side in ('windows', 'wsl'):
                with closing(endpoint.readonly(package.parent / 'sources' / side / 'history.sqlite')) as source:
                    source_turns.update(row[0] for row in source.execute('SELECT turn_id FROM thread_turns'))
            markers = expected_turns - source_turns
            marker_text = [text for turn, item in db.execute('SELECT turn_id,item_json FROM thread_items')
                           if turn in markers for text in texts(json.loads(item))]
        actual_pairs = {(item['turnId'], item['item']['id']) for item in items}
        item_text = set(texts(items))
        report.update(read_turn_count=len(read['thread']['turns']), listed_turn_count=len(turns), listed_item_count=len(items),
                      unique_turn_ids=len({turn['id'] for turn in turns}), native_item_pair_missing=len(expected_pairs - actual_pairs),
                      native_item_pair_extra=len(actual_pairs - expected_pairs), native_turn_missing=len(expected_turns - {turn['id'] for turn in turns}),
                      marker_turn_count=len(markers), marker_turns_native_present=sum(turn['id'] in markers for turn in turns),
                      marker_text_parts=len(marker_text), marker_text_parts_native_exact=sum(text in item_text for text in marker_text))
        resumed = rpc('thread/resume', {'threadId': tid, 'modelProvider': 'merge_validation', 'model': 'gpt-6.1-sol',
                                        'cwd': str(root), 'sandbox': 'read-only', 'approvalPolicy': 'on-request', 'runtimeWorkspaceRoots': []})
        report['resumed_turn_count'] = len(resumed['thread']['turns'])
        require(resumed.get('modelProvider') == 'merge_validation', 'Native CLI did not select local validation provider')
        rpc('turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': 'ISOLATED MOCK VALIDATION ONLY. Do not run any tools.'}]})
        try:
            request = captured.get(timeout=max(.01, min(RPC_TIMEOUT, deadline_all - time.monotonic())))
        except queue.Empty as error:
            raise ValidationError('No loopback model request captured') from error
        require(isinstance(request, dict) and isinstance(request.get('input'), list), 'Unsupported mock request')
        request_text = list(texts(request['input']))
        captured_text = Counter(request_text)
        branches, total_expected = {}, Counter()
        for side in ('windows', 'wsl'):
            expected = branch_text(package.parent / 'sources' / side / 'transcript.jsonl', compiler['common_turns'])
            total_expected.update(expected)
            label = 'Windows' if side == 'windows' else 'WSL'
            matches = [text for text in marker_text if ' / ' + label + '】' in text]
            marker = matches[0] if len(matches) == 1 else None
            branches[side] = branch_context_check(request_text, expected, marker, set(marker_text) - {marker})
        report.update(model_context_branches=branches, mock_request_input_items=len(request['input']),
                      combined_branch_text_parts_missing=sum((total_expected - captured_text).values()),
                      marker_text_parts_model_exact=sum(text in captured_text for text in marker_text),
                      mock_local_only=True, credentials_copied=False, model_response_generated=False)
    finally:
        if process:
            stop_session(process)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def validate(home, codex, package, report_path):
    require(sys.platform == 'linux', 'Native validation currently supports Linux/WSL only')
    require(not Path(report_path).is_symlink(), 'Report must not be a symbolic link')
    home, codex, package, report_path = (Path(value).resolve() for value in (home, codex, package, report_path))
    require(codex.is_file() and os.access(codex, os.X_OK), 'Native Codex executable is missing')
    require(not report_path.is_relative_to(home) and not report_path.is_relative_to(package.parent), 'Report must be outside native home and merge output')
    compiler = json.loads((package.parent / 'report.json').read_text(encoding='utf-8'))
    require(all(type(compiler.get(key)) is int and compiler[key] >= 0 for key in ('merged_turns', 'merged_items', 'marker_turns')), 'Invalid compiler counts')
    require(type(compiler.get('common_turns')) is int and compiler['common_turns'] > 0, 'Invalid common-history boundary')
    meta = json.loads((package / 'meta.json').read_text(encoding='utf-8'))
    require(meta['thread']['id'] == compiler['new_thread_id'] != compiler['original_thread_id'], 'Merged copy must use a new thread identity')
    report = {'validation_passed': False, 'new_thread_id': compiler['new_thread_id'], 'original_databases_modified': False,
              'isolation': 'Schema clone, isolated HOME/CODEX_HOME/XDG/TMP, explicit loopback provider; not an OS sandbox',
              'package_hashes': package_hashes(package), 'expected_turn_count': compiler['merged_turns'], 'expected_item_count': compiler['merged_items']}
    source_hashes = {side: package_hashes(package.parent / 'sources' / side) for side in ('windows', 'wsl')}
    require(source_hashes == compiler.get('source_hashes'), 'Preserved source packages do not match compiler report')
    try:
        with tempfile.TemporaryDirectory(prefix='codex-merge-validation-', dir='/tmp') as directory:
            root = Path(directory)
            for filename in ('state_5.sqlite', 'thread_history_1.sqlite'):
                clone_schema(home / filename, root / filename)
            imported = endpoint.import_chat(argparse.Namespace(home=str(root), scope='cli,vscode', package=str(package), expected='absent',
                                           quiet_seconds=0, metadata_only=False, allow_existing=False, environment_only=False))
            require(imported.get('ok') is True, 'Isolated native import failed')
            report['import'] = {'ok': True, 'turn_count': imported['record']['turn_count'], 'item_count': imported['record']['item_count']}
            native_checks(root, codex, package, compiler, report)
            require(all(report[key] == compiler['merged_turns'] for key in ('read_turn_count', 'listed_turn_count', 'resumed_turn_count', 'unique_turn_ids')), 'Native turn counts differ')
            require(report['listed_item_count'] == compiler['merged_items'], 'Native item count differs')
            require(report['native_item_pair_missing'] == report['native_item_pair_extra'] == report['native_turn_missing'] == 0, 'Native identities differ')
            require(report['marker_turn_count'] == report['marker_turns_native_present'] == compiler['marker_turns'], 'Native markers differ')
            require(report['marker_text_parts_native_exact'] == report['marker_text_parts_model_exact'] == report['marker_text_parts'], 'Marker contents differ')
            require(report['combined_branch_text_parts_missing'] == 0 and all(branch['missing_text_parts'] == 0 and branch['order_preserved'] for branch in report['model_context_branches'].values()), 'Model branch context missing or reordered')
            require(report['package_hashes'] == package_hashes(package), 'Offline package changed during validation')
            require(source_hashes == {side: package_hashes(package.parent / 'sources' / side) for side in source_hashes}, 'Preserved sources changed during validation')
            report['validation_passed'] = True
        report['owned_temporary_directory_removed'] = not root.exists()
    except Exception as error:
        report['error'] = str(error)[:300] if isinstance(error, ValidationError) else type(error).__name__ + ': validation failed'
        report['owned_temporary_directory_removed'] = 'root' not in locals() or not root.exists()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    endpoint.write_json(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('home', 'codex', 'package', 'report'):
        parser.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    try:
        result = validate(args.home, args.codex, args.package, args.report)
    except (ValidationError, OSError, ValueError, KeyError, sqlite3.Error) as error:
        result = {'validation_passed': False, 'error': str(error) if isinstance(error, ValidationError) else type(error).__name__ + ': validation setup failed'}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['validation_passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
