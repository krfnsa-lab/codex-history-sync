#!/usr/bin/env python3
"""Build an offline merged copy from two quiet native export packages.

Never opens live Codex databases or replaces an existing conversation. The
result must be tested with the installed native backend before explicit import.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid

import history_endpoint as endpoint


class MergeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise MergeError(code, message)


def sha_file(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(512 * 1024), b''):
            value.update(data)
    return value.hexdigest()


def records(path):
    with path.open('rb') as stream:
        while True:
            start = stream.tell()
            line = stream.readline(16 * 1024 * 1024 + 1)
            if not line:
                break
            require(len(line) <= 16 * 1024 * 1024 and line.endswith(b'\n'),
                    'invalid_transcript', 'Transcript has an incomplete or oversized record')
            record = json.loads(line)
            require(isinstance(record, dict) and isinstance(record.get('payload'), dict),
                    'invalid_transcript', 'Transcript record has an unsupported shape')
            yield start, stream.tell(), record


class Package:
    def __init__(self, path):
        self.path = Path(path).resolve()
        require(self.path.is_dir() and not Path(path).is_symlink(), 'package_path', 'Use a local export directory')
        for name in ('meta.json', 'history.sqlite', 'transcript.jsonl'):
            require((self.path / name).is_file() and not (self.path / name).is_symlink(),
                    'package_path', 'Export file is missing or linked')
        self.hashes = {name: sha_file(self.path / name) for name in ('meta.json', 'history.sqlite', 'transcript.jsonl')}
        self.meta = json.loads((self.path / 'meta.json').read_text(encoding='utf-8'))
        rev = self.meta.get('source_revision', {})
        require(rev.get('active') is False and rev.get('transcript_complete') is True,
                'source_busy', 'Both exports must be complete and quiet')
        endpoint.check_package(self.meta, self.path, False)
        self.id = self.meta['thread']['id']
        require(self.meta['thread'].get('history_mode') == 'paginated', 'history_mode', 'Only paginated exports are supported')
        self.db = endpoint.readonly(self.path / 'history.sqlite')
        try:
            names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            require(names == set(endpoint.CACHE_TABLES), 'cache_schema', 'Export has missing or unknown cache tables')
            for table in endpoint.CACHE_TABLES:
                allowed = set(endpoint.CACHE_HASH_FIELDS[table]) | {'thread_id'}
                if table in ('thread_items', 'thread_realtime_items'):
                    allowed.add('item_json')
                if table == 'thread_turns':
                    allowed.add('root_turn_id')
                required = set(endpoint.CACHE_HASH_FIELDS[table]) | {'thread_id'}
                if table in ('thread_items', 'thread_realtime_items'):
                    required.add('item_json')
                info = endpoint.table_info(self.db, table)
                require(required <= set(endpoint.columns(self.db, table)) <= allowed,
                        'cache_schema', 'Missing or unsupported cache fields')
                keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
                expected_keys = {'thread_turns': ['thread_id', 'turn_id'], 'thread_items': ['thread_id', 'turn_id', 'item_id'],
                                 'thread_realtime_items': ['thread_id', 'item_id'], 'thread_history_projection_state': ['thread_id']}
                require(keys == expected_keys[table], 'cache_schema', 'Unsupported cache primary key')
            counts = {t: self.db.execute('SELECT COUNT(*) FROM ' + endpoint.quote(t)).fetchone()[0] for t in endpoint.CACHE_TABLES}
            require(counts == self.meta.get('cache_counts') and counts['thread_turns'] == rev.get('turn_count')
                    and counts['thread_items'] + counts['thread_realtime_items'] == rev.get('item_count'),
                    'cache_counts', 'Declared cache counts do not match the saved export')
            self.turns = [dict(r) for r in self.db.execute('SELECT * FROM thread_turns ORDER BY rollout_ordinal')]
            require(self.turns and all(r['status'] in ('completed', 'interrupted', 'failed')
                    or (r['status'] == 'inProgress' and i + 1 < len(self.turns)
                        and rev.get('in_progress_recent') is False)
                    for i, r in enumerate(self.turns)),
                    'source_busy', 'Export contains an unfinished or unknown turn')
            require(self.db.execute('SELECT COUNT(*) FROM thread_realtime_items').fetchone()[0] == 0,
                    'realtime_history', 'Realtime history requires a separate adapter')
            require(not self.db.execute('SELECT 1 FROM thread_items i LEFT JOIN thread_turns t ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id WHERE t.turn_id IS NULL LIMIT 1').fetchone(),
                    'cache_reference', 'An item references a missing turn')
            self.offsets = {}
            self.ends = {}
            self.response_hashes = {}
            self.calls = []
            boundaries = {}
            index = 0
            for number, (start, end, record) in enumerate(records(self.path / 'transcript.jsonl')):
                require(type(record.get('ordinal', number)) is int and record.get('ordinal', number) == number,
                        'record_order', 'Ordinals must be contiguous integers')
                self.offsets[number], self.ends[number] = start, end
                if number == 0:
                    require(record.get('type') == 'session_meta' and record['payload'].get('id') == self.id,
                            'session_identity', 'Session metadata does not match the package')
                    self.session_id = record['payload'].get('session_id')
                if record.get('type') == 'response_item' and record['payload'].get('type') in ('function_call', 'custom_tool_call'):
                    call_id = record['payload'].get('call_id')
                    require(isinstance(call_id, str) and call_id, 'tool_reference', 'Tool call identity is missing')
                    self.calls.append((number, call_id))
                while index + 1 < len(self.turns) and number >= self.turns[index + 1]['rollout_ordinal']:
                    index += 1
                turn = self.turns[index]
                if number == turn['rollout_ordinal'] or number == turn.get('rollout_end_ordinal'):
                    boundaries[number] = (record.get('type'), record['payload'].get('type'), record['payload'].get('turn_id'))
                end_ordinal = turn.get('rollout_end_ordinal')
                if number >= turn['rollout_ordinal'] and (end_ordinal is None or number <= end_ordinal) and record.get('type') in ('response_item', 'compacted'):
                    h = self.response_hashes.setdefault(turn['turn_id'], hashlib.sha256())
                    h.update(endpoint.canonical({'type': record['type'], 'payload': record['payload']}))
            self.line_count = len(self.offsets)
            projection = self.db.execute('SELECT * FROM thread_history_projection_state').fetchone()
            require(projection and projection['next_rollout_byte_offset'] == (self.path / 'transcript.jsonl').stat().st_size
                    and projection['next_rollout_ordinal'] == self.line_count,
                    'projection_incomplete', 'Only fully projected exports can be merged')
            for index, turn in enumerate(self.turns):
                require(self.offsets.get(turn['rollout_ordinal']) == turn['rollout_byte_offset'],
                        'cache_offset', 'Turn start offset does not match its record')
                require(boundaries.get(turn['rollout_ordinal']) == ('event_msg', 'task_started', turn['turn_id']),
                        'cache_reference', 'Turn start record has a different identity')
                if turn.get('rollout_end_ordinal') is not None:
                    require(self.ends.get(turn['rollout_end_ordinal']) == turn['rollout_end_byte_offset'],
                            'cache_offset', 'Turn end offset does not match its record')
                    require(boundaries.get(turn['rollout_end_ordinal']) in (('event_msg', 'task_complete', turn['turn_id']), ('event_msg', 'turn_aborted', turn['turn_id'])),
                            'cache_reference', 'Turn end record has a different identity')
                next_start = self.turns[index + 1]['rollout_ordinal'] if index + 1 < len(self.turns) else self.line_count
                require(turn['rollout_ordinal'] < next_start and (turn.get('rollout_end_ordinal') is None
                        or turn['rollout_ordinal'] <= turn['rollout_end_ordinal'] < next_start),
                        'turn_overlap', 'Turn ranges overlap')
                require(not self.db.execute('SELECT 1 FROM thread_items WHERE turn_id=? AND (rollout_ordinal<? OR rollout_ordinal>=?) LIMIT 1',
                                            (turn['turn_id'], turn['rollout_ordinal'], next_start)).fetchone(),
                        'cache_reference', 'An item points outside its own turn')
                for ref in ('first_user_item_id', 'final_agent_item_id'):
                    if turn.get(ref):
                        require(self.db.execute('SELECT 1 FROM thread_items WHERE turn_id=? AND item_id=?', (turn['turn_id'], turn[ref])).fetchone(),
                                'cache_reference', 'Turn references a missing item')
            for row in self.db.execute('SELECT rollout_ordinal,updated_at_ordinal,item_json,item_id,item_type FROM thread_items'):
                require(row[0] in self.offsets and (row[1] is None or row[1] in self.offsets),
                        'cache_ordinal', 'Item points outside the transcript')
                item = json.loads(row[2])
                require(isinstance(item, dict) and item.get('id') == row[3] and item.get('type') == row[4],
                        'cache_json', 'Cached item identity or type does not match its row')
        except BaseException:
            self.db.close()
            raise

    def fingerprint(self, turn):
        ignored = {'thread_id', 'root_turn_id', 'rollout_ordinal', 'rollout_end_ordinal', 'rollout_byte_offset', 'rollout_end_byte_offset'}
        h = hashlib.sha256(endpoint.canonical({k: v for k, v in turn.items() if k not in ignored}))
        for row in self.db.execute('SELECT * FROM thread_items WHERE turn_id=? ORDER BY rollout_ordinal,item_id', (turn['turn_id'],)):
            h.update(endpoint.canonical({k: row[k] for k in row.keys() if k not in ignored | {'updated_at_ordinal'}}))
        h.update(self.response_hashes.get(turn['turn_id'], hashlib.sha256()).digest())
        return h.hexdigest()

    def unchanged(self):
        return self.hashes == {name: sha_file(self.path / name) for name in self.hashes}


class Writer:
    def __init__(self, path, thread_id):
        self.stream = path.open('wb')
        self.id = thread_id
        self.ordinal = 0
        self.map = {}
        self.removed = {'windows': 0, 'wsl': 0}

    def put(self, record, side=None, old=None):
        start, ordinal = self.stream.tell(), self.ordinal
        record['ordinal'] = ordinal
        self.stream.write(endpoint.canonical(record) + b'\n')
        self.ordinal += 1
        if side is not None:
            self.map[(side, old)] = (ordinal, start, self.stream.tell())
        return ordinal, start, self.stream.tell()

    def copy(self, package, side, first, last=None, drop_compactions=False):
        for number, (_, _, record) in enumerate(records(package.path / 'transcript.jsonl')):
            if number < first:
                continue
            if last is not None and number > last:
                break
            if drop_compactions and record['type'] == 'compacted':
                self.removed[side] += 1
                continue
            # Change only structural identity fields. Message text, tool arguments,
            # tool results and call IDs are kept exactly as exported.
            payload = record['payload']
            if record['type'] == 'session_meta':
                payload['id'] = self.id
                if 'session_id' in payload:
                    payload['session_id'] = self.id
                payload.pop('forked_from_id', None)
            if record['type'] in ('event_msg', 'token_usage_record'):
                if payload.get('thread_id') == package.id:
                    payload['thread_id'] = self.id
                if payload.get('session_id') == package.session_id and 'session_id' in payload:
                    payload['session_id'] = self.id
            self.put(record, side, number)

    def marker(self, side):
        turn_id, item_id = str(uuid.uuid4()), str(uuid.uuid4())
        label = 'Windows' if side == 'windows' else 'WSL'
        text = '【合并副本来源标记 / ' + label + '】以下是此端从共同历史分支后新增的完整记录。此标记由合并工具生成，不是新的用户请求；两端分支分别保留原顺序，不代表先后因果关系。'
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        start = self.put({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn_id, 'root_turn_id': turn_id}})
        response = self.put({'timestamp': stamp, 'type': 'response_item', 'payload': {'type': 'message', 'id': item_id, 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})
        event = {'type': 'UserMessage', 'id': item_id, 'client_id': item_id, 'content': [{'type': 'Text', 'text': text, 'text_elements': []}]}
        completed = self.put({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.id, 'turn_id': turn_id, 'item': event}})
        end = self.put({'timestamp': stamp, 'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn_id, 'last_agent_message': None}})
        now = int(time.time() * 1000)
        item = {'type': 'userMessage', 'id': item_id, 'clientId': item_id, 'content': [{'type': 'text', 'text': text, 'text_elements': []}]}
        return ({'thread_id': self.id, 'turn_id': turn_id, 'rollout_ordinal': start[0], 'status': 'completed', 'started_at': now,
                 'completed_at': now, 'duration_ms': 0, 'error_json': None, 'first_user_item_id': item_id, 'final_agent_item_id': None,
                 'rollout_byte_offset': start[1], 'rollout_end_ordinal': end[0], 'rollout_end_byte_offset': end[2]},
                {'thread_id': self.id, 'turn_id': turn_id, 'item_id': item_id, 'rollout_ordinal': response[0], 'created_at_ms': now,
                 'item_json': json.dumps(item, ensure_ascii=False), 'item_type': 'userMessage', 'updated_at_ordinal': completed[0],
                 'started_at_ms': now, 'completed_at_ms': now})


def insert(db, table, value):
    names = endpoint.columns(db, table)
    db.execute('INSERT INTO ' + endpoint.quote(table) + '(' + ','.join(endpoint.quote(k) for k in names) + ') VALUES (' + ','.join('?' for _ in names) + ')', [value.get(k) for k in names])


def build(windows, wsl, stage, title):
    require(windows.id == wsl.id, 'different_chats', 'Both packages must describe the same chat')
    for table in endpoint.CACHE_TABLES:
        shapes = [{r[1]: (r[2].upper(), r[5]) for r in endpoint.table_info(p.db, table) if r[1] != 'root_turn_id'} for p in (windows, wsl)]
        require(shapes[0] == shapes[1], 'cache_schema', 'Export cache schemas differ')
    common = 0
    for a, b in zip(windows.turns, wsl.turns):
        if a['turn_id'] != b['turn_id']:
            break
        require(windows.fingerprint(a) == wsl.fingerprint(b), 'common_turn_changed', 'A shared turn was edited; preserve both for review')
        common += 1
    require(common > 0, 'no_common_history', 'No verified common history was found')
    require(not ({r['turn_id'] for r in windows.turns[common:]} & {r['turn_id'] for r in wsl.turns[common:]}),
            'reconverged_history', 'Divergent tails share turn IDs; manual review is required')
    branches = {'windows': len(windows.turns) - common, 'wsl': len(wsl.turns) - common}
    # A completed shared boundary is necessary to keep appended context outside
    # the last common turn. Interrupted turns within the prefix are retained.
    require(windows.turns[common - 1].get('rollout_end_ordinal') is not None, 'unfinished_boundary', 'The common boundary must be a finished turn')
    call_ids = [cid for ordinal, cid in windows.calls if ordinal <= windows.turns[common - 1]['rollout_end_ordinal']]
    for source in (windows, wsl):
        cutoff = source.turns[common - 1]['rollout_end_ordinal']
        call_ids.extend(cid for ordinal, cid in source.calls if ordinal > cutoff)
    require(len(call_ids) == len(set(call_ids)), 'tool_reference', 'Merged tool call identities would be ambiguous')
    package = stage / 'merged'
    package.mkdir()
    new_id = str(uuid.uuid4())
    writer = Writer(package / 'transcript.jsonl', new_id)
    selections = []
    markers = []
    try:
        cutoff = windows.turns[common - 1]['rollout_end_ordinal']
        writer.copy(windows, 'windows', 0, cutoff)
        selections.append(('windows', windows, windows.turns[:common]))
        for side, source in (('windows', windows), ('wsl', wsl)):
            if branches[side]:
                markers.append(writer.marker(side))
                writer.copy(source, side, source.turns[common - 1]['rollout_end_ordinal'] + 1, drop_compactions=True)
                selections.append((side, source, source.turns[common:]))
    finally:
        writer.stream.close()
    with closing(sqlite3.connect(package / 'history.sqlite')) as db, db:
        for table in endpoint.CACHE_TABLES:
            info = endpoint.table_info(windows.db, table)
            names = [r for r in info if r[1] != 'root_turn_id']
            defs = [endpoint.quote(r[1]) + ' ' + r[2] for r in names]
            keys = [r[1] for r in sorted(names, key=lambda r: r[5]) if r[5]]
            require(keys and 'thread_id' in keys, 'cache_schema', 'Cache primary key must scope IDs to a thread')
            defs.append('PRIMARY KEY (' + ','.join(endpoint.quote(k) for k in keys) + ')')
            db.execute('CREATE TABLE ' + endpoint.quote(table) + '(' + ','.join(defs) + ')')
        for side, source, turns in selections:
            for turn in turns:
                rows = [('thread_turns', turn)] + [('thread_items', dict(r)) for r in source.db.execute('SELECT * FROM thread_items WHERE turn_id=?', (turn['turn_id'],))]
                for table, original in rows:
                    value = dict(original)
                    value['thread_id'] = new_id
                    for key in ('rollout_ordinal', 'rollout_end_ordinal', 'updated_at_ordinal'):
                        if value.get(key) is not None:
                            require((side, value[key]) in writer.map, 'removed_cache_record', 'A cached item refers to a removed context record')
                            value[key] = writer.map[(side, value[key])][0]
                    if table == 'thread_turns':
                        value['rollout_byte_offset'] = writer.map[(side, original['rollout_ordinal'])][1]
                        if original.get('rollout_end_ordinal') is not None:
                            value['rollout_end_byte_offset'] = writer.map[(side, original['rollout_end_ordinal'])][2]
                    insert(db, table, value)
        for turn, item in markers:
            insert(db, 'thread_turns', turn)
            insert(db, 'thread_items', item)
        insert(db, 'thread_history_projection_state', {'thread_id': new_id, 'next_rollout_byte_offset': (package / 'transcript.jsonl').stat().st_size, 'next_rollout_ordinal': writer.ordinal})
        counts = {t: db.execute('SELECT COUNT(*) FROM ' + endpoint.quote(t)).fetchone()[0] for t in endpoint.CACHE_TABLES}
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'cache_integrity', 'Merged cache failed validation')
    now = int(time.time())
    thread = dict(windows.meta['thread'])
    thread.update(id=new_id, name=title, title=title, preview=title, created_at=now, updated_at=now, created_at_ms=now * 1000,
                  updated_at_ms=now * 1000, recency_at=now, recency_at_ms=now * 1000, archived=0, archived_at=None)
    meta = {'format_version': 1, 'thread': thread, 'source_platform': windows.meta['source_platform'], 'metadata_only': False,
            'relative_rollout_path': time.strftime('sessions/%Y/%m/%d/rollout-%Y-%m-%dT%H-%M-%S-', time.gmtime()) + new_id + '.jsonl',
            'transcript': {'sha256': sha_file(package / 'transcript.jsonl'), 'size': (package / 'transcript.jsonl').stat().st_size},
            'cache_counts': counts, 'source_revision': {'id': new_id, 'active': False, 'in_progress_recent': False,
                'transcript_complete': True, 'turn_count': counts['thread_turns'], 'item_count': counts['thread_items'],
                'transcript_size': (package / 'transcript.jsonl').stat().st_size, 'history_mode': 'paginated'}}
    endpoint.write_json(package / 'meta.json', meta)
    endpoint.check_package(meta, package, False)
    # Complete immutable source packages are part of the result, including raw
    # compaction records and tool data omitted from the readable Markdown.
    for side, source in (('windows', windows), ('wsl', wsl)):
        target = stage / 'sources' / side
        target.mkdir(parents=True)
        for name in source.hashes:
            shutil.copyfile(source.path / name, target / name)
        require(source.unchanged() and source.hashes == {n: sha_file(target / n) for n in source.hashes},
                'source_changed', 'Export changed while the copy was being prepared')
    report = {'format_version': 1, 'original_thread_id': windows.id, 'new_thread_id': new_id, 'common_turns': common,
              'source_turns': {'windows': len(windows.turns), 'wsl': len(wsl.turns)}, 'branch_turns': branches,
              'merged_turns': counts['thread_turns'], 'merged_items': counts['thread_items'], 'marker_turns': len(markers),
              'branch_compactions_omitted_from_copy': writer.removed, 'source_hashes': {'windows': windows.hashes, 'wsl': wsl.hashes},
              'native_validation': 'required', 'originals_preserved': True}
    endpoint.write_json(stage / 'report.json', report)
    with closing(endpoint.readonly(package / 'history.sqlite')) as db, (stage / 'merged-history.md').open('w', encoding='utf-8', newline='\n') as output:
        output.write('# ' + title + '\n\n共同历史保留一次；分支带有来源标记，各自顺序保持。原始消息、图片和工具记录另有完整备份。\n\n')
        for row in db.execute('SELECT i.item_type,i.item_json FROM thread_items i JOIN thread_turns t ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id ORDER BY t.rollout_ordinal,i.rollout_ordinal,i.item_id'):
            item = json.loads(row['item_json'])
            if row['item_type'] == 'agentMessage':
                output.write('### 助手\n\n' + item.get('text', '') + '\n\n')
            elif row['item_type'] == 'userMessage':
                parts = [part.get('text', '【图片或附件见完整原始备份】') for part in item.get('content', [])]
                output.write('### 用户 / 历史标记\n\n' + '\n\n'.join(parts) + '\n\n')
    return report


def merge_packages(windows, wsl, output, title='Merged history'):
    output = Path(output).absolute()
    require(not output.exists() and not output.is_symlink(), 'output_exists', 'Choose a new output directory')
    require(all(not output.resolve().is_relative_to(Path(p).resolve()) for p in (windows, wsl)), 'output_inside_source', 'Output must be outside source packages')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.history-merge-', dir=output.parent))
    owned_stage = stage.resolve()
    intended_parent = output.parent.resolve()
    packages = []
    try:
        for path in (windows, wsl):
            packages.append(Package(path))
        report = build(*packages, stage, title)
        require(not output.exists(), 'output_exists', 'Output appeared during generation')
        stage.rename(output)
        return report
    except endpoint.EndpointError as error:
        raise MergeError(error.code, str(error)) from error
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise MergeError('invalid_package', 'Export contains malformed or unsupported data') from error
    finally:
        for package in packages:
            package.db.close()
        if stage.exists():
            require(not stage.is_symlink() and stage.resolve() == owned_stage
                    and owned_stage.parent == intended_parent and owned_stage.name.startswith('.history-merge-'),
                    'cleanup_path', 'Temporary output moved; preserve it for review')
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows-package', required=True)
    parser.add_argument('--wsl-package', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--title', default='Merged history')
    args = parser.parse_args()
    try:
        result = merge_packages(args.windows_package, args.wsl_package, args.output, args.title)
        print(json.dumps({'ok': True, **result}, ensure_ascii=True))
        return 0
    except (MergeError, OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({'ok': False, 'code': getattr(error, 'code', 'merge_failed'), 'message': str(error)}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
