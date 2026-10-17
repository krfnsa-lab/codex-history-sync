"""Native compressed archive regressions; synthetic files and SQLite only."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('compressed_fixture', ROOT / 'tests/test_endpoint.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
ep = fixture.ep


class CompressedRollouts(unittest.TestCase):
    args = fixture.EndpointTests.args
    export = fixture.EndpointTests.export

    def setUp(self):
        fixture.EndpointTests.setUp(self)
        try:
            self.zstd = ep.zstd_module()
        except ep.EndpointError:
            self.skipTest('Install backports.zstd on Python <3.14 to test compressed archives')
        db = sqlite3.connect(self.source / 'state_5.sqlite')
        try:
            self.path = Path(db.execute('SELECT rollout_path FROM threads').fetchone()[0])
        finally:
            db.close()
        self.original = self.path.read_bytes()
        self.old_signature = ep.file_signature(self.path)
        self.old_revision = ep.read_revision(self.source, fixture.ID)
        self.packed = self.path.with_name(self.path.name + '.zst')
        self.packed.write_bytes(self.zstd.compress(self.original))
        self.packed_bytes = self.packed.read_bytes()
        self.path.unlink()
        quiet = time.time() - 7200
        os.utime(self.packed, (quiet, quiet))

    def test_compression_preserves_logical_revision_and_native_storage(self):
        current = ep.read_revision(self.source, fixture.ID)
        self.assertEqual(current['revision'], self.old_revision['revision'])
        self.assertEqual(current['transcript_size'], len(self.original))
        self.assertFalse(current['active'])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.packed.read_bytes(), self.packed_bytes)

    def test_export_import_preserves_full_cache_and_projection_offsets(self):
        self.export()
        self.assertEqual((self.package / 'transcript.jsonl').read_bytes(), self.original)
        result = ep.import_chat(self.args(home=str(self.target), expected='absent'))
        self.assertEqual(result['record']['content_revision'], self.old_revision['content_revision'])
        self.assertEqual(result['record']['item_count'], self.old_revision['item_count'])
        self.assertEqual(result['record']['turn_count'], self.old_revision['turn_count'])
        self.assertFalse(self.path.exists())

    def test_truncated_frame_is_never_treated_as_complete_jsonl(self):
        self.packed.write_bytes(self.packed_bytes[:-1])
        with self.assertRaises(ep.EndpointError) as error:
            ep.file_signature(self.path)
        self.assertEqual(error.exception.code, 'compressed_rollout_invalid')

    def test_invalid_frame_is_never_treated_as_missing_or_empty(self):
        self.packed.write_bytes(b'corrupt archive')
        with self.assertRaises(ep.EndpointError) as error:
            ep.file_signature(self.path)
        self.assertEqual(error.exception.code, 'compressed_rollout_invalid')

    def test_concatenated_frames_preserve_every_logical_byte(self):
        extra = b'{"type":"other","payload":{}}\n'
        self.packed.write_bytes(self.packed_bytes + self.zstd.compress(extra))
        result = ep.file_signature(self.path)
        self.assertEqual(result['size'], len(self.original) + len(extra))

    def test_plaintext_takes_priority_when_native_file_is_present(self):
        self.path.write_bytes(self.original + b'new\n')
        self.packed.write_bytes(b'invalid obsolete compressed sibling')
        self.assertEqual(ep.file_signature(self.path)['size'], len(self.original) + 4)

    def test_native_storage_switch_during_read_is_rejected(self):
        with self.assertRaises(ep.EndpointError) as error:
            with ep.read_rollout(self.path) as (reader, _, _):
                reader.read(2)
                self.path.write_bytes(self.original)
        self.assertEqual(error.exception.code, 'source_changed')

    def test_decoder_absence_reports_dependency_not_missing_history(self):
        original_import = builtins.__import__
        def denied(name, *args, **kwargs):
            if name in ('compression', 'backports'):
                raise ImportError('synthetic unavailable decoder')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=denied):
            with self.assertRaises(ep.EndpointError) as error:
                ep.file_signature(self.path)
        self.assertEqual(error.exception.code, 'compressed_decoder_missing')

    def test_environment_preview_reads_compressed_logical_bytes(self):
        self.assertEqual(ep.inspect_workspace_environment(self.path)['environment_lines_changed'], 0)
        self.assertFalse(self.path.exists())

    def test_target_backup_includes_compressed_original_before_replacement(self):
        self.export()
        # Replace source with a different valid package while target holds the
        # original only in compressed form, then verify the private backup.
        self.target = self.source
        other = self.root / 'other-source'
        fixture.fixture(other, with_chat=True, item_text='replacement message')
        self.package = self.root / 'replacement-package'
        old_source = self.source
        self.source = other
        self.export()
        with patch.object(ep, 'backend_status', return_value={'backend_running': False, 'backend_detection_ok': True}):
            result = ep.import_chat(self.args(home=str(old_source), expected=self.old_revision['revision'], allow_existing=True))
        self.assertEqual((Path(result['backup']) / 'transcript.jsonl').read_bytes(), self.original)
        self.assertEqual(self.packed.read_bytes(), self.packed_bytes)

    def test_metadata_archive_move_keeps_compressed_original_and_message_bytes(self):
        other = self.root / 'metadata-source'
        fixture.fixture(other, with_chat=True)
        db = sqlite3.connect(other / 'state_5.sqlite')
        db.execute('UPDATE threads SET archived=1')
        db.commit()
        db.close()
        target = self.source
        self.source = other
        self.export(metadata_only=True)
        with patch.object(ep, 'backend_status', return_value={'backend_running': False, 'backend_detection_ok': True}):
            result = ep.import_chat(self.args(home=str(target), expected=self.old_revision['revision'],
                                             allow_existing=True, metadata_only=True))
        db = sqlite3.connect(target / 'state_5.sqlite')
        try:
            path, archived = db.execute('SELECT rollout_path,archived FROM threads').fetchone()
        finally:
            db.close()
        self.assertTrue(archived)
        self.assertEqual(Path(path).read_bytes(), self.original)
        self.assertEqual(self.packed.read_bytes(), self.packed_bytes)
        self.assertEqual(result['record']['content_revision'], self.old_revision['content_revision'])

    def test_compressed_sibling_symlink_is_not_followed(self):
        outside = self.root / 'outside.zst'
        self.packed.replace(outside)
        try:
            self.packed.symlink_to(outside)
        except OSError:
            self.skipTest('Native symlink permission unavailable')
        with self.assertRaises(ep.EndpointError) as error:
            ep.file_signature(self.path)
        self.assertEqual(error.exception.code, 'rollout_outside_home')


if __name__ == '__main__':
    unittest.main()
