"""Exercise native rollout aliases using isolated files and SQLite fixtures."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from test_endpoint import ID, ep, fixture


class RolloutPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="history-rollout-path-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = fixture(self.root / "home")
        self.transcript = next((self.home / "sessions").rglob("*.jsonl"))

    def args(self, **values):
        arguments = dict(home=str(self.home), scope="cli,vscode", quiet_seconds=60,
                         id=ID, package=str(self.root / "package"), expected=None,
                         metadata_only=False, allow_existing=False)
        arguments.update(values)
        return argparse.Namespace(**arguments)

    def set_rollout(self, value):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as connection:
            connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (value, ID))
            connection.commit()

    def assert_rejected(self):
        with self.assertRaises(ep.EndpointError):
            ep.read_revision(self.home, ID)
        with mock.patch.object(ep, "backend_status", return_value={}):
            inspected = ep.inspect_home(self.args())
        self.assertEqual(inspected["threads"], [])
        self.assertEqual([entry["id"] for entry in inspected["skipped"]], [ID])
        with self.assertRaises(ep.EndpointError):
            ep.export_chat(self.args())
        self.assertFalse((self.root / "package" / "transcript.jsonl").exists())

    @unittest.skipUnless(os.name == "nt", "Extended drive paths require native Windows")
    def test_extended_drive_rollout_read_inspect_and_export(self):
        original = ep.read_revision(self.home, ID)
        expected_bytes = self.transcript.read_bytes()
        self.set_rollout("\\\\?\\" + str(self.transcript))
        current = ep.read_revision(self.home, ID)
        self.assertEqual(current["revision"], original["revision"])
        self.assertEqual(current["item_count"], 1)
        with mock.patch.object(ep, "backend_status", return_value={}):
            inspected = ep.inspect_home(self.args())
        self.assertEqual(inspected["skipped"], [])
        self.assertEqual([entry["revision"] for entry in inspected["threads"]], [original["revision"]])
        exported = ep.export_chat(self.args(expected=original["revision"]))
        self.assertEqual(exported["revision"], original["revision"])
        package = self.root / "package"
        self.assertEqual((package / "transcript.jsonl").read_bytes(), expected_bytes)
        metadata = json.loads((package / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["relative_rollout_path"], self.transcript.relative_to(self.home).as_posix())

    @unittest.skipUnless(os.name == "nt", "Extended drive paths require native Windows")
    def test_extended_home_and_archived_rollout_aliases_agree(self):
        archived = self.home / "archived_sessions" / self.transcript.name
        archived.parent.mkdir()
        self.transcript.replace(archived)
        self.set_rollout(str(archived))
        original = ep.read_revision(self.home, ID)
        self.set_rollout("\\\\?\\" + str(archived))
        extended_home = "\\\\?\\" + str(self.home)
        native = ep.native_home(extended_home)
        self.assertEqual(native, ep.native_home(str(self.home)))
        self.assertEqual(ep.read_revision(native, ID)["revision"], original["revision"])
        with mock.patch.object(ep, "backend_status", return_value={}):
            inspected = ep.inspect_home(self.args(home=extended_home))
        self.assertEqual(inspected["skipped"], [])
        self.assertEqual(len(inspected["threads"]), 1)
        ep.export_chat(self.args(home=extended_home, expected=original["revision"]))
        metadata = json.loads((self.root / "package" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["relative_rollout_path"], archived.relative_to(self.home).as_posix())

    def test_existing_rollout_outside_home_is_never_exported(self):
        outside = self.root / "outside" / "sessions" / self.transcript.name
        outside.parent.mkdir(parents=True)
        outside.write_bytes(self.transcript.read_bytes())
        variants = [str(outside)]
        if os.name == "nt":
            variants.append("\\\\?\\" + str(outside))
        for value in variants:
            with self.subTest(value=value):
                self.set_rollout(value)
                self.assert_rejected()

    @unittest.skipUnless(os.name == "nt", "Windows network and device namespaces")
    def test_unc_and_device_rollouts_and_homes_are_rejected(self):
        values = (r"\\example-user\share\sessions\rollout.jsonl",
                  r"\\?\UNC\example-user\share\sessions\rollout.jsonl",
                  r"\\.\C:\sessions\rollout.jsonl",
                  r"\\?\GLOBALROOT\Device\HarddiskVolume1\sessions\rollout.jsonl")
        for value in values:
            with self.subTest(value=value):
                self.set_rollout(value)
                self.assert_rejected()
                with self.assertRaises(ep.EndpointError):
                    ep.native_home(value)

    def test_symlink_to_external_rollout_is_rejected(self):
        outside = self.root / "outside.jsonl"
        outside.write_bytes(self.transcript.read_bytes())
        link = self.transcript.with_name("linked-rollout.jsonl")
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Native filesystem does not permit creating symbolic links")
        variants = [str(link)]
        if os.name == "nt":
            variants.append("\\\\?\\" + str(link))
        for value in variants:
            with self.subTest(value=value):
                self.set_rollout(value)
                self.assert_rejected()

    @unittest.skipUnless(os.name == "nt", "Windows extended path component semantics")
    def test_extended_drive_components_do_not_change_filename_semantics(self):
        ambiguous = (str(self.transcript) + " ", str(self.transcript) + ".",
                     str(self.transcript.parent) + "\\..\\" + self.transcript.name,
                     str(self.transcript) + ":stream", str(self.transcript.parent / "CON"))
        for value in ambiguous:
            with self.subTest(value=value):
                self.set_rollout("\\\\?\\" + value)
                self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
