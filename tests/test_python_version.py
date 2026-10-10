"""The public endpoint requires streamed SQLite blob support from Python 3.11."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("public_endpoint_version", ROOT / "history_endpoint.py")
endpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(endpoint)


class PythonVersionTests(unittest.TestCase):
    def test_old_python_is_rejected_before_argument_or_database_access(self):
        output = io.StringIO()
        with mock.patch.object(endpoint.sys, "version_info", (3, 10)), mock.patch.object(endpoint.sys, "stdout", output), mock.patch.object(endpoint, "build_parser") as parser:
            result = endpoint.main()
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "python_version_unsupported")
        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
