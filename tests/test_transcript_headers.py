"""Native header variations and large records must preserve conversation bytes."""
import hashlib
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_endpoint import ep


class TranscriptHeaders(unittest.TestCase):
    def rewrite(self, data, limit=1024, chunk=256):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.jsonl'
            target = Path(folder) / 'target.jsonl'
            source.write_bytes(data)
            with patch.object(ep, 'MAX_ENVIRONMENT_RECORD_BYTES', limit), \
                    patch.object(ep, 'CHUNK_SIZE', chunk), \
                    patch.object(ep, 'workspace_root_exists', return_value=True):
                result = ep.rewrite_transcript_environment(source, target, platform='linux')
            self.assertEqual(source.read_bytes(), data)
            return result, target.read_bytes()

    def test_native_ordinal_compacted_record_above_real_environment_limit(self):
        record = {'timestamp': '2026-10-16T00:00:00Z', 'ordinal': 1257,
                  'type': 'compacted', 'payload': {'message': 'x' * (ep.MAX_ENVIRONMENT_RECORD_BYTES + 1234),
                      'replacement_history': [{'type': 'message', 'cwd': r'C:\example'}]}}
        data = json.dumps(record, separators=(',', ':')).encode() + b'\r\n'
        result, copied = self.rewrite(data, ep.MAX_ENVIRONMENT_RECORD_BYTES, ep.CHUNK_SIZE)
        self.assertEqual(copied, data)
        self.assertEqual(result['environment_lines_changed'], 0)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['offset_map'], [])

    def test_header_metadata_order_and_nested_types_do_not_change_message_bytes(self):
        values = {'timestamp': '2026-10-16T00:00:00Z', 'ordinal': 123,
                  'type': 'response_item', 'future': {'type': 'turn_context', 'note': '\"type\":\"session_meta\"'}}
        payload = {'type': 'message', 'text': '字' * 1500, 'cwd': r'C:\keep-message-path'}
        for order in itertools.permutations(values):
            with self.subTest(order=order):
                record = {key: values[key] for key in order}
                record['payload'] = payload
                data = json.dumps(record, ensure_ascii=False).encode() + b'\r\n'
                _, copied = self.rewrite(data, chunk=512)
                self.assertEqual(copied, data)

    def test_escaped_top_level_type_is_environment_and_remains_bounded(self):
        data = b'{"timestamp":"now","ordinal":4,"type":"turn_\\u0063ontext","payload":{"cwd":"C:\\\\code","extra":"' + b'x' * 3000 + b'"}}\n'
        with self.assertRaises(ep.EndpointError) as error:
            self.rewrite(data)
        self.assertEqual(error.exception.code, 'environment_record_too_large')

    def test_unrecognized_or_ambiguous_headers_cannot_use_nested_message_type(self):
        cases = [
            {'payload': {'type': 'message', 'text': 'x' * 3000}, 'type': 'event_msg'},
            {'metadata': {'type': 'event_msg'}, 'payload': {'text': 'x' * 3000}},
            {'ordinal': 1, 'type': None, 'payload': {'text': 'x' * 3000}},
            {'ordinal': 1, 'type': '', 'payload': {'text': 'x' * 3000}},
        ]
        for record in cases:
            with self.subTest(keys=list(record)):
                data = json.dumps(record).encode() + b'\n'
                with self.assertRaises(ep.EndpointError) as error:
                    self.rewrite(data)
                self.assertEqual(error.exception.code, 'transcript_header_unsupported')
        self.assertIsNone(ep.transcript_header_kind(b'{"type":"event_msg","type":"turn_context","payload":'))

    def test_extra_metadata_does_not_disable_environment_path_mapping(self):
        environment = {'ordinal': 3, 'timestamp': 'now', 'type': 'turn_context',
                       'payload': {'cwd': r'C:\example', 'padding': 'a' * 500}}
        result, copied = self.rewrite(json.dumps(environment).encode() + b'\n', limit=4096)
        self.assertEqual(json.loads(copied)['payload']['cwd'], '/mnt/c/example')
        self.assertEqual(result['environment_lines_changed'], 1)
        self.assertEqual(json.loads(copied)['ordinal'], 3)

    def test_large_message_preserves_bounded_reads_and_incomplete_tail_is_rejected(self):
        class BoundedReader(io.BytesIO):
            def readline(self, size=-1):
                self.assert_read(size)
                return super().readline(size)
            @staticmethod
            def assert_read(size):
                if not 0 < size <= 256:
                    raise AssertionError('unbounded transcript read')
        data = b'{"timestamp":"now","ordinal":1,"type":"compacted","payload":{"text":"' + b'x' * 20000 + b'"}}\n'
        with patch.object(ep, 'CHUNK_SIZE', 256), patch.object(ep, 'MAX_ENVIRONMENT_RECORD_BYTES', 1024):
            chunks = list(ep.transcript_environment_chunks(BoundedReader(data), 'linux'))
            self.assertEqual(b''.join(rewritten for _, rewritten, _ in chunks), data)
            self.assertLessEqual(max(len(original) for original, _, _ in chunks), 256)
            with self.assertRaises(ep.EndpointError) as error:
                list(ep.transcript_environment_chunks(BoundedReader(data[:-1]), 'linux'))
            self.assertEqual(error.exception.code, 'incomplete_transcript')


if __name__ == '__main__':
    unittest.main()
