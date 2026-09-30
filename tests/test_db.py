import os
import sqlite3
import tempfile
import unittest
from token_dashboard.db import SCHEMA, init_db, connect


class InitDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def test_init_creates_expected_tables(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"files", "messages", "tool_calls", "plan", "dismissed_tips"}
        self.assertTrue(expected.issubset(tables), f"Missing: {expected - tables}")

    def test_init_is_idempotent(self):
        init_db(self.db_path)
        init_db(self.db_path)

    def test_migration_adds_prompt_flags_and_forces_rescan(self):
        # A database from before is_prompt/tool_use_id existed. Built from
        # the schema text (not DROP COLUMN) to work on SQLite < 3.35.
        old_schema = (SCHEMA
                      .replace("  tool_calls_json         TEXT,\n  is_prompt               INTEGER NOT NULL DEFAULT 0,\n  is_meta                 INTEGER NOT NULL DEFAULT 0\n",
                               "  tool_calls_json         TEXT\n")
                      .replace("  timestamp     TEXT    NOT NULL,\n  tool_use_id   TEXT\n",
                               "  timestamp     TEXT    NOT NULL\n")
                      .replace("CREATE INDEX IF NOT EXISTS idx_tools_use_id  ON tool_calls(tool_use_id);", ""))
        self.assertNotIn("is_prompt", old_schema)
        self.assertNotIn("tool_use_id", old_schema)
        with sqlite3.connect(self.db_path) as c:
            c.executescript(old_schema)
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES ('u','s','p','user','t')")
            c.execute("INSERT INTO files (path, mtime, bytes_read, scanned_at) VALUES ('f.jsonl', 1, 100, 1)")
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            msg_cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
            tool_cols = {r[1] for r in c.execute("PRAGMA table_info(tool_calls)")}
            self.assertTrue({"is_prompt", "is_meta"} <= msg_cols)
            self.assertIn("tool_use_id", tool_cols)
            # Old rows can't carry the new flags, so files are replayed on next scan.
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)

    def test_connect_returns_row_factory(self):
        init_db(self.db_path)
        with connect(self.db_path) as c:
            r = c.execute("SELECT 1 AS one").fetchone()
        self.assertEqual(r["one"], 1)


if __name__ == "__main__":
    unittest.main()
