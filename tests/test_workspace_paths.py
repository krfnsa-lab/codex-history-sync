"""Cross-host restore regressions using synthetic chats and native databases only."""
import argparse
from contextlib import contextmanager
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SPEC = importlib.util.spec_from_file_location("workspace_endpoint_fixture", ROOT / "tests" / "test_endpoint.py")
fixture_module = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixture_module)
ep = fixture_module.ep
fixture = fixture_module.fixture
ID = fixture_module.ID
SYNC_SPEC = importlib.util.spec_from_file_location("workspace_sync", ROOT / "history_sync.py")
sync = importlib.util.module_from_spec(SYNC_SPEC)
SYNC_SPEC.loader.exec_module(sync)


def line(record, newline=b"\n"):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + newline


def boundaries(lines):
    result = [0]
    for data in lines:
        result.append(result[-1] + len(data))
    return result


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def windows_chat():
    # Long paths, Unicode, CRLF and arbitrary user formatting make byte offsets
    # meaningful: replacing a root cannot merely copy the original cache index.
    root = r"C:\Users\example-user\Documents\project"
    extra = r"D:\第二个项目"
    return [
        line({"type": "session_meta", "payload": {"id": ID, "cwd": "\\\\?\\" + root,
              "runtime_workspace_roots": ["\\\\?\\" + root, extra],
              "workspace_roots": [root], "originator": "vscode",
              "unrelated": {"cwd": root}}}),
        line({"type": "turn_context", "payload": {"cwd": root,
              "workspace_roots": [root, extra], "runtime_workspace_roots": [root],
              "sandbox_policy": {"type": "workspace-write", "writable_roots": [root]},
              "approval_policy": "on-request", "unrelated": {"workspace_roots": [root]}}}),
        # Ordinary messages intentionally contain fields and strings that look
        # like environment metadata. Their bytes are conversation content.
        b'{ "type" : "event_msg", "payload" : { "type":"user_message", "message":"C:\\\\Users\\\\example-user\\\\Documents\\\\project", "cwd":"C:\\\\code", "workspace_roots":["C:\\\\code"] } }\r\n',
        line({"type": "response_item", "payload": {"role": "assistant", "type": "message",
              "cwd": root, "content": [{"type": "output_text", "text": "代码中的路径：" + root}]}}, b"\r\n"),
        line({"type": "future_history", "payload": {"cwd": root, "workspace_roots": [root]}}),
        line({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "done"}}),
    ]


class WorkspaceRootMappingTests(unittest.TestCase):
    def test_drive_paths_and_extended_windows_paths_map_to_linux(self):
        for value in (r"C:\Users\example-user\project", "C:/Users/example-user/project", r"\\?\C:\Users\example-user\project"):
            with self.subTest(value=value):
                self.assertEqual(ep.translate_workspace_root(value, "linux"), "/mnt/c/Users/example-user/project")
                self.assertEqual(ep.translate_cwd(value, "linux"), "/mnt/c/Users/example-user/project")

    def test_mounted_drive_maps_back_to_windows(self):
        self.assertEqual(ep.translate_workspace_root("/mnt/d/第二个项目", "windows"), "D:\\第二个项目")
        self.assertEqual(ep.translate_cwd("/mnt/d/第二个项目", "windows"), "D:\\第二个项目")

    def test_native_absolute_roots_are_preserved(self):
        self.assertEqual(ep.translate_workspace_root("/home/example-user/project", "linux"), "/home/example-user/project")
        self.assertEqual(ep.translate_workspace_root(r"D:\project", "windows"), r"D:\project")

    def test_unmappable_roots_are_not_replaced_with_broader_directories(self):
        cases = (("/home/example-user/project", "windows"), ("relative/project", "linux"),
                 (r"C:relative", "linux"), (r"\\server\share\project", "linux"),
                 (r"\\?\UNC\server\share\project", "linux"), ("", "windows"),
                 (None, "linux"), (42, "linux"))
        for value, platform in cases:
            with self.subTest(value=value, platform=platform):
                self.assertIsNone(ep.translate_workspace_root(value, platform))

    def test_parent_traversal_and_filesystem_roots_are_rejected(self):
        for value, platform in (("C:\\project\\..\\", "linux"), ("/mnt/c/project/..", "windows"),
                                ("/", "linux"), ("/mnt/c/", "linux"),
                                ("C:\\", "linux"), ("/mnt/c", "windows")):
            with self.subTest(value=value, platform=platform):
                self.assertIsNone(ep.translate_workspace_root(value, platform))

    def test_windows_aliases_and_invalid_filename_components_are_rejected(self):
        for tail in ("project:stream", "project.", "project ", "NUL", "con.txt", "COM1", "LPT9.log", "bad?name", "bad|name"):
            with self.subTest(tail=tail):
                self.assertIsNone(ep.translate_workspace_root("\\\\?\\C:\\" + tail, "linux"))
                self.assertIsNone(ep.translate_workspace_root("/mnt/c/" + tail, "windows"))
        self.assertIsNone(ep.translate_workspace_root("/mnt/c/project\\nested", "windows"))


class WorkspaceImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-path-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = fixture(self.root / "source")
        self.target = fixture(self.root / "target", with_chat=False, extra_column=True)
        self.package = self.root / "package"
        self.lines = windows_chat()
        self.set_transcript(self.source, self.lines)
        with database(self.source / "state_5.sqlite") as connection:
            connection.execute("UPDATE threads SET cwd=?", (r"C:\Users\example-user\Documents\project",))

    def args(self, home, package, **values):
        defaults = dict(home=str(home), package=str(package), scope="cli,vscode", quiet_seconds=60,
                        id=ID, expected=None, metadata_only=False, allow_existing=False, environment_only=False)
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def set_transcript(self, home, lines, projection_index=None):
        transcript = next((home / "sessions").rglob("*.jsonl"))
        transcript.write_bytes(b"".join(lines))
        quiet = time.time() - 7200
        os.utime(transcript, (quiet, quiet))
        offsets = boundaries(lines)
        projection_index = len(lines) if projection_index is None else projection_index
        with database(home / "thread_history_1.sqlite") as connection:
            connection.execute("UPDATE thread_turns SET rollout_ordinal=1,rollout_byte_offset=?,rollout_end_ordinal=4,rollout_end_byte_offset=?", (offsets[1], offsets[4]))
            connection.execute("UPDATE thread_items SET rollout_ordinal=2,updated_at_ordinal=2")
            connection.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=?", (offsets[projection_index], projection_index))
        return transcript

    def export_from(self, home=None, package=None, platform="windows", metadata_only=False):
        home, package = home or self.source, package or self.package
        revision = ep.read_revision(home, ID)
        with mock.patch.object(ep, "native_platform", return_value=platform):
            ep.export_chat(self.args(home, package, expected=revision["revision"], metadata_only=metadata_only))
        return revision

    def import_to(self, home=None, package=None, platform="linux", expected="absent", allow_existing=False, metadata_only=False, exists=None, environment_only=False):
        home, package = home or self.target, package or self.package
        with mock.patch.object(ep, "native_platform", return_value=platform), \
             mock.patch.object(ep, "workspace_root_exists", side_effect=exists or (lambda value: True)):
            return ep.import_chat(self.args(home, package, expected=expected, allow_existing=allow_existing, metadata_only=metadata_only, environment_only=environment_only))

    def target_lines(self, home=None):
        home = home or self.target
        with database(home / "state_5.sqlite") as connection:
            transcript = Path(connection.execute("SELECT rollout_path FROM threads WHERE id=?", (ID,)).fetchone()[0])
        return transcript.read_bytes().splitlines(keepends=True)

    def test_import_maps_restore_fields_and_preserves_message_bytes(self):
        original_transcript = next((self.source / "sessions").rglob("*.jsonl")).read_bytes()
        self.export_from()
        result = self.import_to()
        imported = self.target_lines()
        session, context = [json.loads(data)["payload"] for data in imported[:2]]
        expected_root = "/mnt/c/Users/example-user/Documents/project"
        self.assertEqual(session["cwd"], expected_root)
        self.assertEqual(session["runtime_workspace_roots"], [expected_root, "/mnt/d/第二个项目"])
        self.assertEqual(session["workspace_roots"], [expected_root])
        self.assertEqual(context["cwd"], expected_root)
        self.assertEqual(context["workspace_roots"], [expected_root, "/mnt/d/第二个项目"])
        self.assertEqual(context["runtime_workspace_roots"], [expected_root])
        self.assertEqual(context["approval_policy"], "on-request")
        self.assertEqual(context["sandbox_policy"]["type"], "workspace-write")
        self.assertEqual(context["sandbox_policy"], json.loads(self.lines[1])["payload"]["sandbox_policy"])
        self.assertEqual(session["unrelated"], json.loads(self.lines[0])["payload"]["unrelated"])
        self.assertEqual(context["unrelated"], json.loads(self.lines[1])["payload"]["unrelated"])
        self.assertEqual(imported[2:], self.lines[2:])
        self.assertEqual(next((self.source / "sessions").rglob("*.jsonl")).read_bytes(), original_transcript)
        self.assertEqual((self.package / "transcript.jsonl").read_bytes(), original_transcript)
        self.assertTrue(result["ok"])

    def test_turn_and_projection_offsets_point_to_transformed_line_boundaries(self):
        for projection_index in (5, 6):
            with self.subTest(projection_index=projection_index):
                package = self.root / ("package-" + str(projection_index))
                home = fixture(self.root / ("target-" + str(projection_index)), with_chat=False, extra_column=True)
                self.set_transcript(self.source, self.lines, projection_index)
                self.export_from(package=package)
                self.import_to(home=home, package=package)
                offsets = boundaries(self.target_lines(home))
                self.assertNotEqual(offsets, boundaries(self.lines))
                with database(home / "thread_history_1.sqlite") as connection:
                    turn = connection.execute("SELECT rollout_ordinal,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset FROM thread_turns").fetchone()
                    projection = connection.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state").fetchone()
                    item = connection.execute("SELECT item_json,rollout_ordinal,updated_at_ordinal FROM thread_items").fetchone()
                with database(self.source / "thread_history_1.sqlite") as connection:
                    original_item = connection.execute("SELECT item_json,rollout_ordinal,updated_at_ordinal FROM thread_items").fetchone()
                self.assertEqual(turn, (1, offsets[1], 4, offsets[4]))
                self.assertEqual(projection, (offsets[projection_index], projection_index))
                self.assertEqual(item, original_item)

    def test_inaccessible_roots_are_removed_without_replacing_with_cwd(self):
        session = json.loads(self.lines[0])
        session["payload"]["runtime_workspace_roots"] += [r"C:\missing", "relative", r"\\server\share", None]
        self.lines[0] = line(session)
        self.set_transcript(self.source, self.lines)
        self.export_from()
        self.import_to(exists=lambda value: value != "/mnt/c/missing")
        roots = json.loads(self.target_lines()[0])["payload"]["runtime_workspace_roots"]
        self.assertEqual(roots, ["/mnt/c/Users/example-user/Documents/project", "/mnt/d/第二个项目"])

    def test_empty_workspace_roots_remain_empty(self):
        session = json.loads(self.lines[0])
        session["payload"]["runtime_workspace_roots"] = []
        self.lines[0] = line(session)
        self.set_transcript(self.source, self.lines)
        self.export_from()
        self.import_to()
        self.assertEqual(json.loads(self.target_lines()[0])["payload"]["runtime_workspace_roots"], [])

    def test_unmappable_or_missing_structured_cwd_is_rejected_without_wider_fallback(self):
        for cwd in ("/home/example-user/project", r"C:\missing", "relative-project"):
            with self.subTest(cwd=cwd):
                session = json.loads(self.lines[0])
                session["payload"]["cwd"] = cwd
                self.lines[0] = line(session)
                self.set_transcript(self.source, self.lines)
                self.package = self.root / ("cwd-package-" + str(len(cwd)))
                self.export_from()
                with self.assertRaises(ep.EndpointError) as error:
                    self.import_to(platform="windows", exists=lambda value: value != "C:\\missing")
                self.assertEqual(error.exception.code, "workspace_cwd_unmappable")
                self.assertIsNone(ep.read_revision(self.target, ID))

    def test_missing_database_cwd_rejects_import_and_restores_staged_transcript(self):
        with database(self.source / "state_5.sqlite") as connection:
            connection.execute("UPDATE threads SET cwd=?", (r"C:\missing",))
        self.export_from()
        with self.assertRaises(ep.EndpointError) as error:
            self.import_to(exists=lambda value: value != "/mnt/c/missing")
        self.assertEqual(error.exception.code, "workspace_cwd_unmappable")
        self.assertIsNone(ep.read_revision(self.target, ID))
        self.assertFalse(list((self.target / "sessions").rglob("*.jsonl")))

    def test_linux_home_roots_are_dropped_when_restoring_on_windows(self):
        native = "/home/example-user/project"
        cwd = "/mnt/c/portable"
        lines = [line({"type": "session_meta", "payload": {"id": ID, "cwd": cwd,
                 "runtime_workspace_roots": [native, "/mnt/c/portable"]}}),
                 line({"type": "turn_context", "payload": {"cwd": cwd,
                 "workspace_roots": [native, "/mnt/c/portable"],
                 "sandbox_policy": {"type": "workspace-write", "writable_roots": [native]}}}), *self.lines[2:]]
        self.set_transcript(self.source, lines)
        with database(self.source / "state_5.sqlite") as connection:
            connection.execute("UPDATE threads SET cwd=?", (cwd,))
        self.export_from(platform="linux")
        self.import_to(platform="windows")
        session, context = [json.loads(data)["payload"] for data in self.target_lines()[:2]]
        self.assertEqual(session["runtime_workspace_roots"], ["C:\\portable"])
        self.assertEqual(context["workspace_roots"], ["C:\\portable"])
        self.assertEqual(context["sandbox_policy"], json.loads(lines[1])["payload"]["sandbox_policy"])
        self.assertEqual(session["cwd"], "C:\\portable")
        self.assertEqual(context["cwd"], "C:\\portable")
        self.assertEqual(self.target_lines()[2:], lines[2:])

    def test_invalid_cache_offset_aborts_before_installing_chat(self):
        self.export_from()
        with database(self.package / "history.sqlite") as connection:
            connection.execute("UPDATE thread_turns SET rollout_byte_offset=1")
        with self.assertRaises(ep.EndpointError):
            self.import_to()
        self.assertIsNone(ep.read_revision(self.target, ID))
        self.assertFalse(list((self.target / "sessions").rglob("*.jsonl")))

    def test_malformed_workspace_roots_fail_before_installing_chat(self):
        session = json.loads(self.lines[0])
        session["payload"]["runtime_workspace_roots"] = "C:\\project"
        self.lines[0] = line(session)
        self.set_transcript(self.source, self.lines)
        self.export_from()
        with self.assertRaises(ep.EndpointError) as error:
            self.import_to()
        self.assertEqual(error.exception.code, "invalid_workspace_roots")
        self.assertIsNone(ep.read_revision(self.target, ID))

    def test_second_import_is_idempotent_on_the_same_host(self):
        self.export_from()
        first = self.import_to()
        first_lines = self.target_lines()
        self.package = self.root / "native-package"
        self.export_from(home=self.target, platform="linux")
        second = self.import_to(expected=first["revision"], allow_existing=True)
        self.assertEqual(self.target_lines(), first_lines)
        self.assertEqual(second["revision"], first["revision"])

    def test_unchanged_native_environment_retains_original_json_formatting(self):
        session = json.loads(self.lines[0])
        session["payload"]["cwd"] = r"C:\Users\example-user\Documents\project"
        session["payload"]["runtime_workspace_roots"][0] = session["payload"]["cwd"]
        self.lines[0] = (json.dumps(session, ensure_ascii=False, indent=None, separators=(", ", ": ")) + "\r\n").encode("utf-8")
        self.set_transcript(self.source, self.lines)
        source_revision = self.export_from()
        result = self.import_to(platform="windows")
        self.assertEqual(self.target_lines(), self.lines)
        self.assertEqual(result["record"]["content_revision"], source_revision["content_revision"])

    def test_failed_existing_repair_restores_transcript_and_original_offsets(self):
        self.export_from()
        imported = self.import_to(platform="windows")
        original_lines = self.target_lines()
        with database(self.target / "thread_history_1.sqlite") as connection:
            original_turn = connection.execute("SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns").fetchone()
        with database(self.target / "state_5.sqlite") as connection:
            connection.execute("CREATE TRIGGER refuse_update BEFORE UPDATE ON threads BEGIN SELECT RAISE(ABORT,'synthetic refusal'); END")
        self.package = self.root / "repair-package"
        self.export_from(home=self.target)
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_to(expected=imported["revision"], allow_existing=True)
        self.assertEqual(self.target_lines(), original_lines)
        self.assertEqual(ep.read_revision(self.target, ID)["revision"], imported["revision"])
        with database(self.target / "thread_history_1.sqlite") as connection:
            restored_turn = connection.execute("SELECT rollout_byte_offset,rollout_end_byte_offset FROM thread_turns").fetchone()
        self.assertEqual(restored_turn, original_turn)

    def test_environment_only_repair_preserves_native_cache_and_all_local_fields(self):
        self.export_from()
        self.import_to(platform="windows")
        with database(self.target / "thread_history_1.sqlite") as connection:
            connection.execute("UPDATE thread_turns SET root_turn_id='native-turn-root'")
            connection.execute("ALTER TABLE thread_items ADD COLUMN root_turn_id TEXT")
            connection.execute("UPDATE thread_items SET root_turn_id='native-item-root'")
            connection.execute("CREATE TRIGGER reject_cache_delete BEFORE DELETE ON thread_turns BEGIN SELECT RAISE(ABORT,'cache delete forbidden'); END")
        with database(self.target / "state_5.sqlite") as connection:
            connection.execute("ALTER TABLE threads ADD COLUMN future_local_state TEXT")
            connection.execute("UPDATE threads SET title='Local title',name='Local name',project_id='local-project',thread_section_id='local-section',is_pinned=1,sandbox_policy='local-policy',approval_mode='local-approval',future_local_state='retained'")

        def snapshot(path, tables, omit=()):
            result = {}
            with database(path) as connection:
                connection.row_factory = sqlite3.Row
                for table in tables:
                    result[table] = [{key: row[key] for key in row.keys() if key not in omit}
                                     for row in connection.execute('SELECT * FROM "' + table + '"')]
            return result

        before_state = snapshot(self.target / "state_5.sqlite", ("threads",), ("cwd",))
        before_cache = snapshot(self.target / "thread_history_1.sqlite", ep.CACHE_TABLES,
                                ("rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset"))
        original_lines = self.target_lines()
        before = ep.read_revision(self.target, ID)
        self.package = self.root / "native-repair"
        self.export_from(home=self.target, platform="linux")
        result = self.import_to(platform="linux", expected=before["revision"], allow_existing=True, environment_only=True)
        self.assertEqual(snapshot(self.target / "state_5.sqlite", ("threads",), ("cwd",)), before_state)
        self.assertEqual(snapshot(self.target / "thread_history_1.sqlite", ep.CACHE_TABLES,
                        ("rollout_byte_offset", "rollout_end_byte_offset", "next_rollout_byte_offset")), before_cache)
        self.assertEqual(self.target_lines()[2:], original_lines[2:])
        offsets = boundaries(self.target_lines())
        with database(self.target / "thread_history_1.sqlite") as connection:
            self.assertEqual(connection.execute("SELECT rollout_byte_offset,rollout_end_byte_offset,root_turn_id FROM thread_turns").fetchone(), (offsets[1], offsets[4], "native-turn-root"))
            self.assertEqual(connection.execute("SELECT root_turn_id FROM thread_items").fetchone()[0], "native-item-root")
            self.assertEqual(connection.execute("SELECT next_rollout_byte_offset FROM thread_history_projection_state").fetchone()[0], offsets[-1])
        self.assertTrue(result["ok"])

    def test_environment_only_cannot_import_a_foreign_transcript_even_with_forged_revision(self):
        self.export_from()
        imported = self.import_to(platform="windows")
        original_lines = self.target_lines()
        self.lines[3] = line({"type": "response_item", "payload": {"role": "assistant", "text": "foreign body"}})
        self.set_transcript(self.source, self.lines)
        self.package = self.root / "foreign-package"
        self.export_from(platform="linux")
        meta_path = self.package / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["source_revision"] = ep.read_revision(self.target, ID)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaises(ep.EndpointError) as error:
            self.import_to(platform="linux", expected=imported["revision"], allow_existing=True, environment_only=True)
        self.assertEqual(error.exception.code, "environment_source_mismatch")
        self.assertEqual(self.target_lines(), original_lines)
        self.assertEqual(ep.read_revision(self.target, ID)["revision"], imported["revision"])

    def test_environment_only_requires_existing_chat(self):
        self.export_from(platform="linux")
        with self.assertRaises(ep.EndpointError) as error:
            self.import_to(environment_only=True)
        self.assertEqual(error.exception.code, "environment_target_missing")

    def test_large_known_message_records_are_copied_byte_for_byte(self):
        for timestamp in (False, True):
            with self.subTest(timestamp=timestamp):
                record = ({"timestamp": "2020-01-01T00:00:00Z"} if timestamp else {})
                record.update({"type": "event_msg", "payload": {"type": "user_message", "message": "A" * (ep.CHUNK_SIZE + 12345) + r"C:\code"}})
                self.lines[2] = line(record, b"\r\n")
                self.set_transcript(self.source, self.lines)
                self.package = self.root / ("large-message-" + str(timestamp))
                home = fixture(self.root / ("large-target-" + str(timestamp)), with_chat=False, extra_column=True)
                self.export_from()
                self.import_to(home=home)
                self.assertEqual(self.target_lines(home)[2], self.lines[2])

    def test_oversized_environment_or_unfamiliar_layout_fails_closed(self):
        for unfamiliar_layout in (False, True):
            with self.subTest(unfamiliar_layout=unfamiliar_layout):
                if unfamiliar_layout:
                    record = {"payload": {"text": "A" * (ep.MAX_ENVIRONMENT_RECORD_BYTES + 1)}, "type": "event_msg"}
                else:
                    record = {"type": "session_meta", "payload": {"cwd": r"C:\code", "unknown": "A" * (ep.MAX_ENVIRONMENT_RECORD_BYTES + 1)}}
                source = self.root / ("oversized-" + str(unfamiliar_layout) + ".jsonl")
                source.write_bytes(line(record))
                destination = source.with_suffix(".rewritten")
                with mock.patch.object(ep, "workspace_root_exists", return_value=True):
                    with self.assertRaises(ep.EndpointError) as error:
                        ep.rewrite_transcript_environment(source, destination, platform="linux")
                self.assertEqual(error.exception.code, "environment_record_too_large")

    def test_import_preview_is_read_only_and_reports_mapped_environment(self):
        self.export_from()
        def files(root):
            return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        before_home, before_package = files(self.target), files(self.package)
        with mock.patch.object(ep, "native_platform", return_value="linux"), \
             mock.patch.object(ep, "workspace_root_exists", return_value=True):
            result = ep.import_preview(self.args(self.target, self.package))
        self.assertTrue(result["compatible"])
        self.assertEqual(result["environment_paths"]["environment_lines_changed"], 2)
        self.assertEqual(files(self.target), before_home)
        self.assertEqual(files(self.package), before_package)

    def test_import_preview_defers_incompatible_cwd_without_creating_backup(self):
        session = json.loads(self.lines[0])
        session["payload"]["cwd"] = "relative-project"
        self.lines[0] = line(session)
        self.set_transcript(self.source, self.lines)
        self.export_from()
        before = {str(path.relative_to(self.target)): path.read_bytes() for path in self.target.rglob("*") if path.is_file()}
        with mock.patch.object(ep, "native_platform", return_value="linux"), \
             mock.patch.object(ep, "workspace_root_exists", return_value=True):
            result = ep.import_preview(self.args(self.target, self.package))
        self.assertFalse(result["compatible"])
        self.assertEqual(result["reason"], "workspace_cwd_unmappable")
        after = {str(path.relative_to(self.target)): path.read_bytes() for path in self.target.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse((self.target / ".history-sync-backups").exists())

    def test_import_preview_does_not_misclassify_corrupt_package_as_deferred(self):
        self.export_from()
        with (self.package / "transcript.jsonl").open("ab") as stream:
            stream.write(b"corrupt\n")
        with self.assertRaises(ep.EndpointError) as error:
            ep.import_preview(self.args(self.target, self.package))
        self.assertEqual(error.exception.code, "package_integrity")

    def test_transformed_native_revision_does_not_create_sync_loop_or_block_next_body(self):
        homes = {"windows": self.source, "wsl": self.target}
        platforms = {"windows": "windows", "wsl": "linux"}
        calls = []

        class NativeEndpoints:
            def inspect(inner, side):
                record = ep.read_revision(homes[side], ID)
                return {"threads": [record] if record else [], "backend_running": False, "skipped": []}

            def backend_status(inner, side):
                return {"backend_running": False, "backend_detection_ok": True}

            def call(inner, side, operation, *options):
                calls.append((side, operation))
                def option(name, default=None):
                    return options[options.index(name) + 1] if name in options else default
                values = self.args(homes[side], option("--package"), expected=option("--expected"),
                                   allow_existing="--allow-existing" in options, metadata_only="--metadata-only" in options)
                with mock.patch.object(ep, "native_platform", return_value=platforms[side]), \
                     mock.patch.object(ep, "workspace_root_exists", return_value=True):
                    return {"export": ep.export_chat, "import": ep.import_chat, "import-preview": ep.import_preview}[operation](values)

        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "ledger.json").write_text(json.dumps({"version": sync.VERSION, "threads": {}}), encoding="utf-8")
        args = argparse.Namespace(initialize=False, dry_run=False, status=False, doctor=False, json=False)
        with mock.patch.object(sync, "wsl_running", return_value=True), \
             mock.patch.object(sync, "Endpoints", return_value=NativeEndpoints()), \
             mock.patch.object(sync, "linux_mount_path", side_effect=lambda value: str(value)):
            first = sync.execute({"wsl_distro": "Synthetic", "quiet_seconds": 60}, args, runtime, mock.Mock())
            self.assertEqual(first["transferred"], 1)
            tracked = json.loads((runtime / "ledger.json").read_text(encoding="utf-8"))["threads"][ID]
            self.assertNotEqual(tracked["windows"]["content_revision"], tracked["wsl"]["content_revision"])
            self.assertFalse(tracked["body_diverged"])
            calls.clear()
            idle = sync.execute({"wsl_distro": "Synthetic", "quiet_seconds": 60}, args, runtime, mock.Mock())
            self.assertEqual(idle["transferred"], 0)
            self.assertEqual(calls, [])
            self.lines[3] = line({"type": "response_item", "payload": {"role": "assistant", "text": "new response"}})
            self.set_transcript(self.source, self.lines)
            with database(self.source / "thread_history_1.sqlite") as connection:
                connection.execute("UPDATE thread_items SET item_json=REPLACE(item_json,'source text','new response')")
            following = sync.execute({"wsl_distro": "Synthetic", "quiet_seconds": 60}, args, runtime, mock.Mock())
            self.assertEqual(following["transferred"], 1)
            self.assertEqual(following["blocked"], [])
            self.assertEqual(calls, [("windows", "export"), ("wsl", "import-preview"), ("wsl", "import")])


class ImportPreflightCoordinatorTests(unittest.TestCase):
    def test_known_incompatible_chat_defers_without_pending_and_next_chat_syncs(self):
        valid_id = "00000000-0000-4000-8000-000000000002"
        def record(tid, revision):
            return {"id": tid, "revision": revision, "content_revision": revision + "-body",
                    "metadata_revision": "metadata", "quiet_seconds": 7200, "active": False,
                    "item_count": 1, "turn_count": 1}
        with tempfile.TemporaryDirectory(prefix="workspace-preflight-controller-") as temporary:
            runtime = Path(temporary)
            (runtime / "ledger.json").write_text(json.dumps({"version": sync.VERSION, "threads": {}}), encoding="utf-8")
            endpoint = mock.MagicMock()
            windows = {"threads": [record(ID, "bad-original"), record(valid_id, "valid-original")], "skipped": [], "backend_running": False}
            wsl = {"threads": [], "skipped": [], "backend_running": False}
            endpoint.inspect.side_effect = lambda side: windows if side == "windows" else wsl
            operations = []
            exported = {}

            def call(side, operation, *options):
                if operation == "export":
                    exported["id"] = options[options.index("--id") + 1]
                tid = exported["id"]
                operations.append((tid, operation))
                if operation == "import-preview":
                    return ({"ok": True, "id": tid, "compatible": False, "reason": "workspace_cwd_unmappable"}
                            if tid == ID else {"ok": True, "id": tid, "compatible": True})
                if operation == "import":
                    result = record(tid, "valid-native")
                    return {"ok": True, "revision": result["revision"], "record": result}
                return {"ok": True}

            endpoint.call.side_effect = call
            args = argparse.Namespace(initialize=False, dry_run=False, status=False, doctor=False, json=False)
            with mock.patch.object(sync, "wsl_running", return_value=True), \
                 mock.patch.object(sync, "Endpoints", return_value=endpoint), \
                 mock.patch.object(sync, "linux_mount_path", side_effect=lambda value: str(value)):
                summary = sync.execute({"wsl_distro": "Synthetic", "quiet_seconds": 60}, args, runtime, mock.Mock())
            self.assertEqual(summary["transferred"], 1)
            self.assertEqual(summary["attempted_transfers"], 2)
            self.assertEqual(summary["deferred"], [{"id": ID, "reason": "workspace_cwd_unmappable"}])
            self.assertEqual(operations, [(ID, "export"), (ID, "import-preview"),
                                         (valid_id, "export"), (valid_id, "import-preview"), (valid_id, "import")])
            ledger = json.loads((runtime / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(set(ledger["threads"]), {valid_id})
            self.assertFalse((runtime / "pending.json").exists())


class RepairControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workspace-repair-controller-")
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        self.windows = self.record("windows-original", "windows-body")
        self.wsl = self.record("wsl-original", "wsl-body")
        self.repaired = self.record("wsl-repaired", "wsl-mapped-body")
        self.ledger = {"version": sync.VERSION, "threads": {
            ID: {"windows": self.windows, "wsl": self.wsl, "body_diverged": True}}}
        self.save_ledger()
        self.args = argparse.Namespace(repair_side="wsl", thread_ids=[], dry_run=False)
        self.config = {"wsl_distro": "Synthetic", "quiet_seconds": 60}
        self.endpoint = mock.MagicMock()
        self.endpoint.inspect.return_value = {"threads": [self.wsl], "backend_running": False, "skipped": []}
        self.endpoint.backend_status.return_value = {"backend_running": False, "backend_detection_ok": True}
        self.receipt = {"ok": True, "id": ID, "revision": self.repaired["revision"],
                        "record": self.repaired, "backup": "synthetic-backup"}
        self.endpoint.call.side_effect = self.call

    def record(self, revision, content):
        return {"id": ID, "revision": revision, "content_revision": content,
                "metadata_revision": "metadata-original", "quiet_seconds": 7200,
                "active": False, "item_count": 1, "turn_count": 1}

    def save_ledger(self):
        (self.runtime / "ledger.json").write_text(json.dumps(self.ledger), encoding="utf-8")

    def saved(self):
        return json.loads((self.runtime / "ledger.json").read_text(encoding="utf-8"))

    def call(self, side, operation, *options):
        if operation == "path-preview":
            return {"ok": True, "id": ID, "needs_repair": True, "database_cwd_changed": False,
                    "environment_paths": {"environment_lines_changed": 2, "workspace_paths_translated": 3,
                    "workspace_roots_removed": 0, "cwd_fallbacks": 0}}
        if operation == "import":
            return copy.deepcopy(self.receipt)
        return {"ok": True}

    def execute(self, running=True):
        with mock.patch.object(sync, "wsl_running", return_value=running), \
             mock.patch.object(sync, "Endpoints", return_value=self.endpoint), \
             mock.patch.object(sync, "linux_mount_path", side_effect=lambda value: str(value)):
            return sync.repair_environment_paths(self.config, self.args, self.runtime, mock.Mock())

    def operations(self):
        return [call.args[1] for call in self.endpoint.call.call_args_list]

    def test_preview_does_not_import_or_change_ledger(self):
        before = (self.runtime / "ledger.json").read_bytes()
        self.args.dry_run = True
        summary = self.execute()
        self.assertEqual(summary["planned_repairs"], 1)
        self.assertEqual(summary["repaired"], 0)
        self.assertEqual(self.operations(), ["path-preview"])
        self.endpoint.backend_status.assert_not_called()
        self.assertEqual((self.runtime / "ledger.json").read_bytes(), before)
        self.assertEqual({path.name for path in self.runtime.iterdir()}, {"ledger.json"})

    def test_running_target_blocks_import(self):
        before = (self.runtime / "ledger.json").read_bytes()
        self.endpoint.backend_status.return_value = {"backend_running": True}
        summary = self.execute()
        self.assertEqual(summary["repaired"], 0)
        self.assertEqual(summary["blocked"], [{"id": ID, "reason": "target_backend_running"}])
        self.assertEqual(self.operations(), ["path-preview"])
        self.assertEqual((self.runtime / "ledger.json").read_bytes(), before)
        self.assertFalse((self.runtime / "pending.json").exists())

    def test_exact_unchanged_replica_updates_its_baseline_and_preserves_pair(self):
        before = copy.deepcopy(self.ledger)
        summary = self.execute()
        saved = self.saved()["threads"][ID]
        self.assertEqual(saved["wsl"], self.repaired)
        self.assertEqual(saved["windows"], before["threads"][ID]["windows"])
        self.assertTrue(saved["body_diverged"])
        self.assertEqual(summary["repaired"], 1)
        self.assertTrue(summary["repairs"][0]["baseline_updated"])
        self.assertEqual(json.loads(Path(summary["ledger_backup"]).read_text(encoding="utf-8")), before)
        self.assertEqual(self.operations(), ["path-preview", "export", "import"])
        import_options = self.endpoint.call.call_args_list[-1].args[2:]
        self.assertIn("--environment-only", import_options)
        self.assertFalse((self.runtime / "pending.json").exists())

    def test_unsynchronized_local_edit_is_not_swallowed_by_baseline_update(self):
        before = (self.runtime / "ledger.json").read_bytes()
        local_edit = self.record("unsynchronized-edit", "unsynchronized-body")
        self.endpoint.inspect.return_value["threads"] = [local_edit]
        summary = self.execute()
        self.assertEqual(summary["repaired"], 1)
        self.assertFalse(summary["repairs"][0]["baseline_updated"])
        self.assertEqual((self.runtime / "ledger.json").read_bytes(), before)
        for call in self.endpoint.call.call_args_list:
            if call.args[1] in ("export", "import"):
                options = call.args[2:]
                self.assertEqual(options[options.index("--expected") + 1], local_edit["revision"])

    def test_pending_journal_blocks_repair_without_endpoint_read(self):
        (self.runtime / "pending.json").write_text('{"operation":"earlier-import"}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unfinished prior import"):
            self.execute()
        self.endpoint.inspect.assert_not_called()
        self.assertEqual(self.operations(), [])

    def test_invalid_import_receipt_preserves_pending_and_original_ledger(self):
        before = (self.runtime / "ledger.json").read_bytes()
        for bad_receipt in ({"record": {}}, {**self.receipt, "revision": "different-receipt"}):
            with self.subTest(receipt=bad_receipt):
                self.receipt = bad_receipt
                with self.assertRaises(RuntimeError):
                    self.execute()
                self.assertTrue((self.runtime / "pending.json").is_file())
                self.assertEqual((self.runtime / "ledger.json").read_bytes(), before)
                (self.runtime / "pending.json").unlink()

    def test_backend_starting_after_export_blocks_import(self):
        before = (self.runtime / "ledger.json").read_bytes()
        self.endpoint.backend_status.side_effect = [{"backend_running": False}, {"backend_running": True}]
        summary = self.execute()
        self.assertEqual(summary["blocked"], [{"id": ID, "reason": "target_backend_started"}])
        self.assertEqual(self.operations(), ["path-preview", "export"])
        self.assertEqual((self.runtime / "ledger.json").read_bytes(), before)
        self.assertFalse((self.runtime / "pending.json").exists())

    def test_stopped_wsl_is_not_started_for_repair(self):
        summary = self.execute(running=False)
        self.assertEqual(summary["status"], "skipped_wsl_stopped")
        self.endpoint.inspect.assert_not_called()
        self.assertEqual(self.operations(), [])


if __name__ == "__main__":
    unittest.main()
