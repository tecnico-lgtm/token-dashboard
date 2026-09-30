import os
import tempfile
import unittest

from token_dashboard.db import init_db, connect
from token_dashboard.tips import (
    cache_discipline_tips, repeated_target_tips, right_size_tips,
    outlier_tips, all_tips, dismiss_tip,
)


class CacheTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _ins(self, ts, project, cache_read, cache_create):
        with connect(self.db) as c:
            c.execute("""INSERT INTO messages (uuid, session_id, project_slug, type, timestamp,
                model, input_tokens, output_tokens, cache_read_tokens,
                cache_create_5m_tokens, cache_create_1h_tokens) VALUES
                (?, 's', ?, 'assistant', ?, 'claude-opus-4-7', 100, 100, ?, ?, 0)""",
                (f"uuid-{ts}", project, ts, cache_read, cache_create))
            c.commit()

    def test_low_cache_hit_emits_tip(self):
        self._ins("2026-04-15T00:00:00Z", "projX", 10, 1_000_000)
        tips = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertTrue(any(t["category"] == "cache" for t in tips))

    def test_healthy_cache_no_tip(self):
        for i in range(10):
            self._ins(f"2026-04-15T00:00:0{i}Z", "projY", 1_000_000, 50)
        tips = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(any(t["category"] == "cache" for t in tips))


class RepeatTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model) VALUES ('m1','s1','p','assistant','2026-04-15T00:00:00Z','claude-opus-4-7')")
            for i in range(15):
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) VALUES ('m1','s1','p','Read','src/Root.tsx','2026-04-15T00:00:00Z',0)")
            for i in range(20):
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) VALUES ('m1','s1','p','Bash','npm run lint','2026-04-15T00:00:00Z',0)")
            c.commit()

    def test_repeated_file_and_bash_emit_tips(self):
        tips = repeated_target_tips(self.db, today_iso="2026-04-19T00:00:00")
        cats = [t["category"] for t in tips]
        self.assertIn("repeat-file", cats)
        self.assertIn("repeat-bash", cats)


class RepeatReadSemanticsTests(unittest.TestCase):
    """Edits aren't reads, and re-reading a file right after editing it is legit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _calls(self, names):
        with connect(self.db) as c:
            for i, name in enumerate(names):
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error) VALUES ('m','s1','p',?,'Codice.gs',?,0)",
                          (name, f"2026-04-15T00:00:{i:02d}Z"))
            c.commit()

    def _file_tips(self):
        return [t for t in repeated_target_tips(self.db, today_iso="2026-04-19T00:00:00")
                if t["category"] == "repeat-file"]

    def test_edit_then_verify_cycles_are_not_flagged(self):
        self._calls(["Read"] + ["Edit", "Read"] * 15)
        self.assertEqual(self._file_tips(), [])

    def test_unchanged_rereads_are_flagged_with_honest_count(self):
        self._calls(["Read"] * 13 + ["Edit"] * 10)
        tips = self._file_tips()
        self.assertEqual(len(tips), 1)
        self.assertIn("re-read 12 times", tips[0]["title"])


class RightSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def _prompt(self, c, i, steps, output_tokens):
        """One human prompt followed by `steps` Opus API calls."""
        c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, is_prompt, prompt_text) VALUES (?, 's','p','user',?,1,'q')",
                  (f"u{i}", f"2026-04-18T{i:02d}:00:00Z"))
        for k in range(steps):
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, is_sidechain) VALUES (?, 's','p','assistant',?,'claude-opus-4-7', 1000000, ?, 0, 0, 0, 0)",
                      (f"a{i}-{k}", f"2026-04-18T{i:02d}:00:{k + 1:02d}Z", output_tokens))

    def test_quick_opus_prompts_flagged(self):
        with connect(self.db) as c:
            for i in range(10):
                self._prompt(c, i, steps=1, output_tokens=200)
            c.commit()
        tips = right_size_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertTrue(any(t["category"] == "right-size" for t in tips))
        self.assertIn("10 quick Opus prompts", tips[0]["title"])

    def test_multi_step_tasks_with_short_steps_not_flagged(self):
        # Each API step writes only 100 tokens, but each prompt as a whole
        # wrote 1,000 — a real task, not a quick question.
        with connect(self.db) as c:
            for i in range(10):
                self._prompt(c, i, steps=10, output_tokens=100)
            c.commit()
        tips = right_size_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(any(t["category"] == "right-size" for t in tips))


class OutlierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)

    def test_giant_tool_result_flagged(self):
        with connect(self.db) as c:
            for i in range(20):
                c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES (?, 's','p','user','2026-04-18T00:00:00Z')", (f"u{i}",))
                c.execute("INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error) VALUES (?, 's','p','_tool_result','tu',100000,'2026-04-18T00:00:00Z',0)", (f"u{i}",))
            c.commit()
        tips = outlier_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertTrue(any(t["category"] == "tool-bloat" for t in tips))


class DismissTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('m','s','projZ','assistant','2026-04-15T00:00:00Z','claude-opus-4-7', 100, 100, 10, 1000000, 0)")
            c.commit()

    def test_dismissed_tip_doesnt_reappear(self):
        tips_before = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertTrue(tips_before)
        dismiss_tip(self.db, tips_before[0]["key"])
        tips_after = cache_discipline_tips(self.db, today_iso="2026-04-19T00:00:00")
        self.assertFalse(tips_after)


if __name__ == "__main__":
    unittest.main()
