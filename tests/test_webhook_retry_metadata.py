"""Source/migration contracts only; real upgrade preservation needs Odoo/PG."""
import ast
import importlib.util
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BASE = "37c4b335edf97f96e81f85c4c21ee4d92ed91211"
MIGRATION = ROOT / "migrations/19.0.26.201/post-migrate.py"


def assignments(raw):
    model = next(n for n in ast.parse(raw).body if isinstance(n, ast.ClassDef)
                 and n.name == "MercadolibreNotification")
    return {n.targets[0].id: n.value for n in model.body if isinstance(n, ast.Assign)}


def baseline(path):
    return subprocess.run(["git", "-C", str(ROOT), "show", BASE + ":" + path],
                          capture_output=True, check=True).stdout


class Cursor:
    def __init__(self, rows, column_exists=True):
        self.rows, self.column_exists, self.calls = rows, column_exists, []
    def execute(self, query, params=None):
        sql = " ".join(query.split())
        self.calls.append((sql, params))
        if sql.startswith("UPDATE"):
            if sql != "UPDATE mercadolibre_notification SET local_attempts = 0 WHERE local_attempts IS NULL":
                raise AssertionError("migration changed non-local metadata")
            for row in self.rows:
                if row["local_attempts"] is None:
                    row["local_attempts"] = 0
    def fetchone(self):
        return (1,) if self.column_exists else None


class RetryMetadata(unittest.TestCase):
    def setUp(self):
        deny = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        deny.start()
        self.addCleanup(deny.stop)
        self.fields = assignments((ROOT / "models/notification.py").read_bytes())
    def options(self, name, constructor):
        declaration = self.fields[name]
        self.assertEqual((declaration.func.value.id, declaration.func.attr), ("fields", constructor))
        return {keyword.arg: ast.literal_eval(keyword.value) for keyword in declaration.keywords}
    def migration(self):
        spec = importlib.util.spec_from_file_location("retry_metadata_migration", MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.migrate

    def test_local_attempts_is_readonly_local_zero_and_not_copied(self):
        options = self.options("local_attempts", "Integer")
        self.assertEqual((options["default"], options["readonly"], options["copy"]), (0, True, False))
        self.assertNotIn("compute", options)

    def test_next_attempt_is_nullable_indexed_readonly_and_not_copied(self):
        options = self.options("next_attempt_at", "Datetime")
        self.assertEqual((options["index"], options["readonly"], options["copy"]), (True, True, False))
        self.assertFalse(options.get("required", False))
        self.assertNotIn("compute", options)
        self.assertNotIn("default", options)

    def test_exact_four_states_and_all_prior_model_declarations_unchanged(self):
        old = assignments(baseline("models/notification.py"))
        self.assertEqual(set(self.fields) - set(old), {"local_attempts", "next_attempt_at"})
        for field, declaration in old.items():
            self.assertEqual(ast.dump(self.fields[field]), ast.dump(declaration), field)
        self.assertEqual([code for code, _ in ast.literal_eval(self.fields["state"].args[0])],
                         ["RECEIVED", "PROCESSING", "FAILED", "SUCCESS"])

    def test_only_manifest_version_changes_and_old_migrations_preserved(self):
        current = ast.literal_eval(ast.parse((ROOT / "__manifest__.py").read_bytes()).body[-1].value)
        old = ast.literal_eval(ast.parse(baseline("__manifest__.py")).body[-1].value)
        self.assertEqual(current.pop("version"), "19.0.26.201")
        self.assertEqual(old.pop("version"), "19.0.26.200")
        self.assertEqual(current, old)
        for version in ("19.0.26.86", "19.0.26.87"):
            path = "migrations/" + version + "/post-migrate.py"
            self.assertEqual((ROOT / path).read_bytes().replace(b"\r\n", b"\n"), baseline(path))
        self.assertTrue(MIGRATION.is_file())

    def test_view_adds_only_readonly_retry_metadata(self):
        tree = ET.parse(ROOT / "views/notifications_view.xml")
        for name in ("local_attempts", "next_attempt_at"):
            nodes = tree.findall(".//field[@name='%s']" % name)
            self.assertEqual(len(nodes), 2)
            self.assertTrue(all(node.get("readonly") == "1" for node in nodes))

    def test_post_migration_null_only_and_repeat_preserves_history(self):
        rows = [{"state": state, "attempts": 19, "sent": "old sent", "received": "old received",
                 "processing_started": "old started", "processing_ended": "old ended",
                 "next_attempt_at": "future due" if count else None, "local_attempts": count}
                for state in ("RECEIVED", "PROCESSING", "FAILED", "SUCCESS") for count in (None, 0, 7)]
        expected = [dict(row, local_attempts=0 if row["local_attempts"] is None else row["local_attempts"]) for row in rows]
        cursor = Cursor(rows)
        migrate = self.migration()
        migrate(cursor, "19.0.26.200")
        self.assertEqual(rows, expected)
        migrate(cursor, "19.0.26.201")
        self.assertEqual(rows, expected)
        self.assertTrue(all("state" not in query and "attempts = 0" not in query.replace("local_attempts = 0", "")
                            for query, _ in cursor.calls))

    def test_fresh_install_and_absent_column_do_not_update_history(self):
        migrate = self.migration()
        cursor = Cursor([])
        migrate(cursor, None)
        self.assertFalse(cursor.calls)
        missing = Cursor([], column_exists=False)
        migrate(missing, "19.0.26.200")
        self.assertEqual(len(missing.calls), 1)
        self.assertTrue(missing.calls[0][0].startswith("SELECT"))


if __name__ == "__main__":
    unittest.main()
