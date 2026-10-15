"""Lossless offline merge tests using synthetic exported packages only."""
import hashlib
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "history_merge.py"
spec = importlib.util.spec_from_file_location("history_merge", MODULE)
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)
THREAD = "00000000-0000-4000-8000-000000000001"


@contextmanager
def closed_db(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def package(root, turns, platform="windows", root_turn_id=False):
    """Create format 1 exports, with real native ordinal/byte conventions."""
    root.mkdir(parents=True)
    records = [{"timestamp": "2020-01-01T00:00:00Z", "type": "session_meta",
                "payload": {"id": THREAD, "cwd": "/home/user/project", "source": "vscode"}}]
    turn_rows, item_rows = [], []
    offsets = [0]

    def append(record):
        ordinal = len(records)
        records.append(record)
        return ordinal

    for number, (turn_id, text) in enumerate(turns):
        started = append({"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}})
        row_items = []
        for role, kind, content in (("user", "userMessage", text),
                                    ("assistant", "agentMessage", "Answer: " + text)):
            item_id = turn_id + "-" + role
            response = append({"type": "response_item", "payload": {"type": "message", "role": role,
                              "content": [{"type": "input_text" if role == "user" else "output_text", "text": content}]}})
            item = {"id": item_id, "type": kind}
            if role == "user":
                item["content"] = [{"type": "text", "text": content}]
            else:
                item["text"] = content
            event_item = {**item, "type": "UserMessage" if role == "user" else "AgentMessage"}
            if role == "user":
                event_item["content"] = [{"type": "Text", "text": content, "text_elements": []}]
            completed = append({"type": "event_msg", "payload": {"type": "item_completed",
                               "thread_id": THREAD, "turn_id": turn_id, "item": event_item}})
            row_items.append((THREAD, turn_id, item_id, response, 100000 + number,
                              json.dumps(item, ensure_ascii=False), kind, completed, 100000 + number, 100100 + number))
        ended = append({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}})
        turn_rows.append([THREAD, turn_id, started, "completed", None, 100, 200, 100,
                          turn_id + "-user", turn_id + "-assistant", started, ended, ended])
        item_rows.extend(row_items)
    chunks = [(json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8") for record in records]
    for chunk in chunks:
        offsets.append(offsets[-1] + len(chunk))
    for row in turn_rows:
        row[10] = offsets[row[10]]
        row[12] = offsets[row[12] + 1]
        if root_turn_id:
            row.append("root-only-local")
    body = b"".join(chunks)
    (root / "transcript.jsonl").write_bytes(body)
    with closed_db(root / "history.sqlite") as db:
        db.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,error_json TEXT,started_at INTEGER,completed_at INTEGER,duration_ms INTEGER,first_user_item_id TEXT,final_agent_item_id TEXT,rollout_byte_offset INTEGER,rollout_end_ordinal INTEGER,rollout_end_byte_offset INTEGER," + ("root_turn_id TEXT," if root_turn_id else "") + "PRIMARY KEY(thread_id,turn_id))")
        db.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,item_json TEXT,item_type TEXT,updated_at_ordinal INTEGER,started_at_ms INTEGER,completed_at_ms INTEGER,PRIMARY KEY(thread_id,turn_id,item_id))")
        db.execute("CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,item_type TEXT,item_json TEXT,PRIMARY KEY(thread_id,item_id))")
        db.execute("CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER,next_rollout_ordinal INTEGER)")
        db.executemany("INSERT INTO thread_turns VALUES(" + ",".join("?" for _ in turn_rows[0]) + ")", turn_rows)
        db.executemany("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?,?)", item_rows)
        db.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (THREAD, len(body), len(records)))
    meta = {"format_version": 1, "thread": {"id": THREAD, "source": "vscode", "title": "Synthetic learning",
            "name": "Synthetic learning", "cwd": "/home/user/project", "history_mode": "paginated", "created_at": 100,
            "updated_at": 200, "model_provider": "openai"}, "source_platform": platform,
            "relative_rollout_path": "sessions/2020/01/01/rollout-2020-01-01-" + THREAD + ".jsonl",
            "metadata_only": False, "source_revision": {"id": THREAD, "active": False,
            "transcript_complete": True, "transcript_size": len(body), "item_count": len(item_rows),
            "turn_count": len(turn_rows), "revision": "synthetic", "history_mode": "paginated"},
            "transcript": {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()},
            "cache_counts": {"thread_turns": len(turn_rows), "thread_items": len(item_rows),
            "thread_realtime_items": 0, "thread_history_projection_state": 1}}
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return root


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="history-merge-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        common = [("turn-common", "Shared question")]
        self.windows = package(self.root / "windows", common + [("turn-windows", "Windows addition")])
        self.wsl = package(self.root / "wsl", common + [("turn-wsl", "Matplotlib learning")], "linux", True)
        self.output = self.root / "result"

    def run_merge(self):
        return merge.merge_packages(self.windows, self.wsl, self.output, "Combined learning")

    def change_meta(self, root, mutate):
        path = root / "meta.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        path.write_text(json.dumps(value), encoding="utf-8")

    def reject(self):
        before = hashes(self.windows), hashes(self.wsl)
        with self.assertRaises(merge.MergeError):
            self.run_merge()
        self.assertEqual(before, (hashes(self.windows), hashes(self.wsl)))
        self.assertFalse((self.output / "merged" / "meta.json").exists())

    def test_fork_preserves_originals_and_both_additions(self):
        before = hashes(self.windows), hashes(self.wsl)
        report = self.run_merge()
        self.assertEqual(before, (hashes(self.windows), hashes(self.wsl)))
        self.assertEqual(before[0], hashes(self.output / "sources" / "windows"))
        self.assertEqual(before[1], hashes(self.output / "sources" / "wsl"))
        self.assertNotEqual(report["new_thread_id"], THREAD)
        self.assertEqual(report["common_turns"], 1)
        self.assertEqual(report["merged_turns"], 5)  # Shared, two labels, two tails.
        self.assertEqual(report["source_turns"], {"windows": 2, "wsl": 2})
        self.assertEqual(report["branch_turns"], {"windows": 1, "wsl": 1})
        with closed_db(self.output / "merged" / "history.sqlite") as db:
            rows = db.execute("SELECT turn_id FROM thread_turns ORDER BY rollout_ordinal").fetchall()
            original = [r[0] for r in rows if r[0] in ("turn-common", "turn-windows", "turn-wsl")]
            self.assertEqual(original, ["turn-common", "turn-windows", "turn-wsl"])
            for turn, expected in (("turn-common", "Shared question"), ("turn-windows", "Windows addition"), ("turn-wsl", "Matplotlib learning")):
                data = db.execute("SELECT item_json FROM thread_items WHERE turn_id=? AND item_type='userMessage'", (turn,)).fetchone()
                self.assertEqual(json.loads(data[0])["content"][0]["text"], expected)
        text = "\n".join(p.read_text(encoding="utf-8") for p in self.output.glob("*.md"))
        self.assertIn("Matplotlib learning", text)
        self.assertIn("Windows addition", text)

    def test_merged_ordinals_offsets_and_projection_are_consistent(self):
        report = self.run_merge()
        path = self.output / "merged" / "transcript.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        records = [json.loads(line) for line in lines]
        self.assertEqual(records[0]["payload"]["id"], report["new_thread_id"])
        with closed_db(self.output / "merged" / "history.sqlite") as db:
            for row in db.execute("SELECT thread_id,turn_id,rollout_ordinal,rollout_byte_offset,rollout_end_ordinal,rollout_end_byte_offset FROM thread_turns"):
                thread, turn, start, offset, end, end_offset = row
                self.assertEqual(thread, report["new_thread_id"])
                self.assertEqual(offset, offsets[start])
                self.assertEqual(end_offset, offsets[end + 1])
                self.assertEqual(records[start]["payload"]["turn_id"], turn)
                self.assertEqual(records[end]["payload"]["turn_id"], turn)
            self.assertEqual(db.execute("SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state").fetchone(), (path.stat().st_size, len(lines)))
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM thread_items i LEFT JOIN thread_turns t ON i.thread_id=t.thread_id AND i.turn_id=t.turn_id WHERE t.turn_id IS NULL").fetchone()[0], 0)

    def test_existing_output_is_not_clobbered(self):
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("original")
        self.reject()
        self.assertEqual(sentinel.read_text(), "original")

    def test_identical_histories_do_not_duplicate_turns(self):
        self.wsl = package(self.root / "identical", [("turn-common", "Shared question"),
                           ("turn-windows", "Windows addition")], "linux", True)
        report = self.run_merge()
        self.assertEqual(report["common_turns"], 2)
        self.assertEqual(report["branch_turns"], {"windows": 0, "wsl": 0})
        self.assertEqual(report["merged_turns"], 2)
        with closed_db(self.output / "merged" / "history.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM thread_items").fetchone()[0], 4)

    def test_contained_history_retains_only_one_shared_copy(self):
        self.wsl = package(self.root / "contained", [("turn-common", "Shared question")], "linux", True)
        report = self.run_merge()
        self.assertEqual(report["common_turns"], 1)
        self.assertEqual(report["branch_turns"], {"windows": 1, "wsl": 0})
        self.assertEqual(report["merged_turns"], 3)

    def test_tail_order_is_preserved_with_multiple_turns(self):
        self.windows = package(self.root / "multiwin", [("turn-common", "Shared question"),
                               ("turn-windows", "Windows addition"), ("turn-windows-two", "Second Windows addition")])
        self.wsl = package(self.root / "multilin", [("turn-common", "Shared question"),
                           ("turn-wsl", "Matplotlib learning"), ("turn-wsl-two", "Second plot")], "linux", True)
        self.run_merge()
        with closed_db(self.output / "merged" / "history.sqlite") as db:
            order = [row[0] for row in db.execute("SELECT turn_id FROM thread_turns ORDER BY rollout_ordinal")
                     if row[0].startswith("turn-")]
        self.assertEqual(order, ["turn-common", "turn-windows", "turn-windows-two", "turn-wsl", "turn-wsl-two"])

    def test_reconverged_turn_after_fork_is_rejected(self):
        self.windows = package(self.root / "rejoinedwin", [("turn-common", "Shared question"),
                               ("turn-windows", "Windows addition"), ("turn-rejoined", "Shared later question")])
        self.wsl = package(self.root / "rejoinedlin", [("turn-common", "Shared question"),
                           ("turn-wsl", "Matplotlib learning"), ("turn-rejoined", "Shared later question")], "linux", True)
        self.reject()

    def test_output_inside_original_package_is_rejected(self):
        self.output = self.windows / "derived"
        self.reject()

    def test_active_source_is_rejected(self):
        self.change_meta(self.windows, lambda meta: meta["source_revision"].update(active=True))
        self.reject()

    def test_incomplete_source_is_rejected(self):
        self.change_meta(self.wsl, lambda meta: meta["source_revision"].update(transcript_complete=False))
        self.reject()

    def test_transcript_digest_mismatch_is_rejected(self):
        with (self.wsl / "transcript.jsonl").open("ab") as stream:
            stream.write(b'{"type":"unknown"}\n')
        self.reject()

    def test_wrong_thread_is_rejected(self):
        self.change_meta(self.wsl, lambda meta: meta["thread"].update(id="00000000-0000-4000-8000-000000000002"))
        self.reject()

    def test_unknown_schema_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("ALTER TABLE thread_items ADD COLUMN unsupported_semantics TEXT")
        self.reject()

    def test_unknown_table_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("CREATE TABLE unexpected_payload(data TEXT)")
        self.reject()

    def test_modified_common_item_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET item_json=REPLACE(item_json,'Shared question','Different question') WHERE turn_id='turn-common'")
        self.reject()

    def test_modified_common_turn_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_turns SET error_json='new error' WHERE turn_id='turn-common'")
        self.reject()

    def test_inprogress_turn_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_turns SET status='inProgress' WHERE turn_id='turn-wsl'")
        self.reject()

    def test_item_without_turn_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET turn_id='missing-turn' WHERE turn_id='turn-wsl'")
        self.reject()

    def test_first_user_reference_missing_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_turns SET first_user_item_id='missing-item' WHERE turn_id='turn-wsl'")
        self.reject()

    def test_foreign_cache_thread_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET thread_id='foreign' WHERE turn_id='turn-wsl'")
        self.reject()

    def test_projection_before_eof_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=0")
        self.reject()

    def test_invalid_turn_offset_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_turns SET rollout_byte_offset=3 WHERE turn_id='turn-wsl'")
        self.reject()

    def test_realtime_items_are_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("INSERT INTO thread_realtime_items VALUES(?,?,?,?,?,?)", (THREAD, "live", 1, 100, "realtime", "{}"))
        self.change_meta(self.wsl, lambda meta: meta["cache_counts"].update(thread_realtime_items=1))
        self.reject()

    def test_declared_cache_count_mismatch_is_rejected(self):
        self.change_meta(self.wsl, lambda meta: meta["cache_counts"].update(thread_items=400))
        self.reject()

    def test_declared_source_count_mismatch_is_rejected(self):
        self.change_meta(self.wsl, lambda meta: meta["source_revision"].update(turn_count=400))
        self.reject()

    def test_item_ordinal_in_another_turn_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET rollout_ordinal=2 WHERE turn_id='turn-wsl'")
        self.reject()

    def test_item_update_ordinal_in_another_turn_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET updated_at_ordinal=3 WHERE turn_id='turn-wsl'")
        self.reject()

    def test_start_record_with_wrong_turn_identity_is_rejected(self):
        path = self.wsl / "transcript.jsonl"
        old = path.read_bytes()
        body = old.replace(b'"task_started", "turn_id": "turn-wsl"', b'"task_started", "turn_id": "turn-xxx"')
        self.assertNotEqual(old, body)
        self.assertEqual(len(old), len(body))
        path.write_bytes(body)
        self.change_meta(self.wsl, lambda meta: meta["transcript"].update(sha256=hashlib.sha256(body).hexdigest()))
        self.reject()

    def test_malformed_cached_json_is_rejected_with_merge_error(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_items SET item_json='{invalid json' WHERE turn_id='turn-wsl'")
        self.reject()

    def test_root_turn_column_on_wrong_table_is_rejected(self):
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("ALTER TABLE thread_items ADD COLUMN root_turn_id TEXT")
        self.reject()

    def test_end_record_with_wrong_turn_identity_is_rejected(self):
        path = self.wsl / "transcript.jsonl"
        old = path.read_bytes()
        body = old.replace(b'"task_complete", "turn_id": "turn-wsl"', b'"task_complete", "turn_id": "turn-xxx"')
        self.assertNotEqual(old, body)
        self.assertEqual(len(old), len(body))
        path.write_bytes(body)
        self.change_meta(self.wsl, lambda meta: meta["transcript"].update(sha256=hashlib.sha256(body).hexdigest()))
        self.reject()

    def test_branch_compaction_stays_in_original_copy_only(self):
        path = self.wsl / "transcript.jsonl"
        body = path.read_bytes() + (json.dumps({"type": "compacted", "payload": {"message": "Synthetic branch summary"}}) + "\n").encode()
        path.write_bytes(body)
        def refresh(meta):
            meta["transcript"].update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
            meta["source_revision"]["transcript_size"] = len(body)
        self.change_meta(self.wsl, refresh)
        with closed_db(self.wsl / "history.sqlite") as db:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?,next_rollout_ordinal=?", (len(body), len(body.splitlines())))
        report = self.run_merge()
        self.assertEqual(report["branch_compactions_omitted_from_copy"], {"windows": 0, "wsl": 1})
        self.assertIn(b'Synthetic branch summary', (self.output / "sources" / "wsl" / "transcript.jsonl").read_bytes())
        self.assertNotIn(b'Synthetic branch summary', (self.output / "merged" / "transcript.jsonl").read_bytes())
        self.assertIn(b'Matplotlib learning', (self.output / "merged" / "transcript.jsonl").read_bytes())

    def test_source_changed_during_snapshot_copy_aborts_without_result(self):
        original_copy = merge.shutil.copyfile
        changed = False
        def copy_then_change(source, target, *args, **kwargs):
            nonlocal changed
            result = original_copy(source, target, *args, **kwargs)
            if not changed and Path(source) == self.windows / "meta.json":
                changed = True
                with Path(source).open("ab") as stream:
                    stream.write(b" ")
            return result
        with mock.patch.object(merge.shutil, "copyfile", side_effect=copy_then_change):
            with self.assertRaises(merge.MergeError) as error:
                self.run_merge()
        self.assertEqual(error.exception.code, "source_changed")
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".history-merge-*")), [])

    def test_output_created_during_generation_is_preserved(self):
        original_copy = merge.shutil.copyfile
        def copy_then_create(source, target, *args, **kwargs):
            result = original_copy(source, target, *args, **kwargs)
            if not self.output.exists():
                self.output.mkdir()
                (self.output / "keep.txt").write_text("external creation")
            return result
        with mock.patch.object(merge.shutil, "copyfile", side_effect=copy_then_create):
            with self.assertRaises(merge.MergeError) as error:
                self.run_merge()
        self.assertEqual(error.exception.code, "output_exists")
        self.assertEqual((self.output / "keep.txt").read_text(), "external creation")
        self.assertEqual(list(self.root.glob(".history-merge-*")), [])


if __name__ == "__main__":
    unittest.main()
