"""Prove a native target appended only local settings events, without writing it.

This is deliberately narrower than prefix merging. The caller must separately
check source changes, ledger divergence, backend ownership, and pause safety.
"""
import argparse
import base64
import hashlib
import importlib.util
import json
import math
import sqlite3
from pathlib import Path

PROJECTION = 'thread_history_projection_state'
CURSORS = ('next_rollout_byte_offset', 'next_rollout_ordinal')
MAX_SUFFIX_BYTES = 16 * 1024 * 1024
MAX_SETTINGS_LINE = 1024 * 1024
MAX_SETTINGS_COUNT = 4096


class ProofRejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ProofRejected(reason)


def integer(value):
    return type(value) is int and value >= 0


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'settings_suffix_invalid_json')
        result[key] = value
    return result


def invalid_constant(value):
    raise ProofRejected('settings_suffix_invalid_json')


def verify_transcript(stream, baseline, digest):
    """Hash the exact native baseline prefix and whitelist the complete suffix."""
    size = baseline.get('transcript_size')
    require(integer(size) and size > 0, 'invalid_baseline_size')
    require(baseline.get('transcript_complete') is True, 'baseline_incomplete')
    prefix = hashlib.sha256()
    remaining, last, prefix_lines = size, None, 0
    while remaining:
        chunk = stream.read(min(remaining, 512 * 1024))
        require(bool(chunk), 'baseline_prefix_missing')
        prefix.update(chunk)
        prefix_lines += chunk.count(b'\n')
        last = chunk[-1]
        remaining -= len(chunk)
    require(last == 10, 'baseline_boundary_incomplete')
    require(digest({'sha256': prefix.hexdigest(), 'size': size}) == baseline.get('transcript_revision'),
            'baseline_prefix_changed')
    count = suffix_bytes = 0
    while True:
        line = stream.readline(MAX_SETTINGS_LINE + 1)
        if not line:
            break
        suffix_bytes += len(line)
        require(len(line) <= MAX_SETTINGS_LINE and suffix_bytes <= MAX_SUFFIX_BYTES,
                'settings_suffix_too_large')
        require(line.endswith(b'\n'), 'settings_suffix_incomplete')
        try:
            record = json.loads(line, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (ValueError, UnicodeError):
            raise ProofRejected('settings_suffix_invalid_json') from None
        require(isinstance(record, dict) and record.get('type') == 'event_msg'
                and isinstance(record.get('payload'), dict)
                and record['payload'].get('type') == 'thread_settings_applied',
                'target_contains_other_events')
        count += 1
        require(count <= MAX_SETTINGS_COUNT, 'settings_suffix_too_large')
    require(count > 0, 'no_local_settings_append')
    return count, suffix_bytes, prefix_lines


def verify_record(record, baseline, quiet_seconds):
    require(isinstance(baseline, dict) and isinstance(record, dict), 'invalid_record')
    require(record.get('id') == baseline.get('id'), 'baseline_thread_mismatch')
    require(record.get('metadata_revision') == baseline.get('metadata_revision')
            and isinstance(record.get('metadata_revision'), str), 'target_metadata_changed')
    for field in ('source', 'archived', 'history_mode', 'item_count', 'turn_count'):
        require(field in baseline and record.get(field) == baseline[field], 'target_' + field + '_changed')
    require(record.get('active') is False and record.get('in_progress_recent') is False, 'target_busy')
    quiet = record.get('quiet_seconds')
    require(isinstance(quiet, (int, float)) and not isinstance(quiet, bool)
            and math.isfinite(quiet) and quiet >= quiet_seconds, 'target_not_quiet')
    require(record.get('transcript_complete') is True, 'target_incomplete')
    old, new = baseline.get('cache'), record.get('cache')
    require(isinstance(old, dict) and isinstance(new, dict) and old.keys() == new.keys(), 'cache_schema_changed')
    require(PROJECTION in old, 'projection_missing')
    for table in old:
        require(isinstance(old[table], dict) and isinstance(new[table], dict), 'invalid_cache_summary')
        if table != PROJECTION:
            require(new[table] == old[table], 'target_cache_changed')
    ignored = {'rows_sha256', 'max_next_rollout_byte_offset', 'max_next_rollout_ordinal'}
    require(old[PROJECTION].keys() == new[PROJECTION].keys(), 'projection_summary_changed')
    require({k: v for k, v in old[PROJECTION].items() if k not in ignored}
            == {k: v for k, v in new[PROJECTION].items() if k not in ignored}, 'projection_summary_changed')


def verify_projection(ep, connection, thread_id, record, baseline, settings_count, suffix_bytes, prefix_lines):
    require(ep.table_exists(connection, PROJECTION), 'projection_missing')
    require(set(ep.columns(connection, PROJECTION)) == {'thread_id', *CURSORS}, 'projection_schema_changed')
    require(tuple(ep.CACHE_HASH_FIELDS.get(PROJECTION, ())) == CURSORS, 'projection_hash_schema_changed')
    rows = list(connection.execute('SELECT * FROM ' + ep.quote(PROJECTION) + ' WHERE thread_id=?', (thread_id,)))
    old, new = baseline['cache'][PROJECTION], record['cache'][PROJECTION]
    require(len(rows) == 1 and old.get('row_count') == 1 and new.get('row_count') == 1, 'projection_count_changed')
    before = {name: old.get('max_' + name) for name in CURSORS}
    require(all(integer(v) for v in before.values()), 'invalid_baseline_cursor')
    encoded = ep.canonical(before)
    reconstructed = hashlib.sha256(str(len(encoded)).encode('ascii') + b':' + encoded).hexdigest()
    require(reconstructed == old.get('rows_sha256'), 'baseline_projection_unproven')
    row = dict(rows[0])
    require(before[CURSORS[0]] == baseline['transcript_size'], 'baseline_projection_not_at_end')
    # The imported cache may store the next ordinal; native re-projection stores
    # the last processed ordinal. Prove both against physical JSONL line counts,
    # never accept an arbitrary changed cursor merely because its hash changed.
    require(before[CURSORS[1]] in (prefix_lines - 1, prefix_lines), 'baseline_projection_not_at_end')
    require(row[CURSORS[0]] == before[CURSORS[0]] + suffix_bytes
            and row[CURSORS[1]] in (prefix_lines + settings_count - 1, prefix_lines + settings_count)
            and row[CURSORS[0]] == record['transcript_size'], 'projection_not_only_settings')
    for name in CURSORS:
        require(new.get('max_' + name) == row[name], 'projection_snapshot_mismatch')


def verify_native(ep, home, thread_id, baseline, quiet_seconds=60):
    state = history = None
    try:
        require(isinstance(baseline, dict) and baseline.get('id') == thread_id, 'baseline_thread_mismatch')
        before = ep.read_revision(home, thread_id, quiet_seconds)
        require(before is not None, 'target_missing')
        state = ep.readonly(home / 'state_5.sqlite')
        history = ep.readonly(home / 'thread_history_1.sqlite')
        state.execute('BEGIN')
        history.execute('BEGIN')
        row = ep.thread_row(state, thread_id, ep.INSPECT_FIELDS)
        require(row is not None, 'target_missing')
        record = ep.revision_from_connections(home, row, history, quiet_seconds)
        verify_record(record, baseline, quiet_seconds)
        require(before['revision'] == record['revision'], 'target_changed_during_probe')
        rollout = ep.safe_rollout(home, row)
        stat_before = rollout.stat()
        with rollout.open('rb') as stream:
            count, size, prefix_lines = verify_transcript(stream, baseline, ep.digest)
        stat_after = rollout.stat()
        stat_fields = ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_ino')
        require(all(getattr(stat_before, f) == getattr(stat_after, f) for f in stat_fields),
                'target_changed_during_probe')
        require(record['transcript_size'] == baseline['transcript_size'] + size, 'target_snapshot_mismatch')
        verify_projection(ep, history, thread_id, record, baseline, count, size, prefix_lines)
        state.close(); history.close(); state = history = None
        after = ep.read_revision(home, thread_id, quiet_seconds)
        require(after is not None and after['revision'] == record['revision'], 'target_changed_during_probe')
        verify_record(after, baseline, quiet_seconds)
        return {'verified': True, 'reason': 'local_settings_only', 'record': after, 'settings_count': count}
    except ProofRejected as error:
        return {'verified': False, 'reason': str(error), 'record': None, 'settings_count': 0}
    except ep.EndpointError as error:
        return {'verified': False, 'reason': error.code, 'record': None, 'settings_count': 0}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {'verified': False, 'reason': 'native_probe_failed', 'record': None, 'settings_count': 0}
    finally:
        if state is not None: state.close()
        if history is not None: history.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--baseline-base64', required=True)
    parser.add_argument('--quiet-seconds', type=int, default=60)
    args = parser.parse_args()
    try:
        require(args.quiet_seconds >= 0, 'invalid_quiet_seconds')
        require(len(args.baseline_base64) <= 65536, 'invalid_baseline')
        baseline = json.loads(base64.b64decode(args.baseline_base64, validate=True))
        spec = importlib.util.spec_from_file_location('local_settings_native_endpoint', args.endpoint)
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)
        try:
            home = ep.native_home(args.home)
            result = verify_native(ep, home, args.id, baseline, args.quiet_seconds)
        except ep.EndpointError as error:
            result = {'verified': False, 'reason': error.code, 'record': None, 'settings_count': 0}
    except (ValueError, TypeError, OSError, AttributeError):
        result = {'verified': False, 'reason': 'invalid_probe_input', 'record': None, 'settings_count': 0}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
