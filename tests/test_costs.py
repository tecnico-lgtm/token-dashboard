"""Per-prompt / per-session / per-project cost attribution, end to end.

Fixtures mirror real Claude Code record shapes: tool results, isMeta
injections (image refs, skill bodies) and command output are all
type='user' records that must not count as prompts.
"""
import json
import os
import tempfile
import unittest

from token_dashboard.costs import prompt_costs, project_costs, session_costs
from token_dashboard.db import (
    init_db, connect, overview_totals, skill_body_tokens, tool_token_breakdown,
)
from token_dashboard.pricing import load_pricing
from token_dashboard.scanner import scan_dir

PRICING = load_pricing()


def _user(uuid, ts, content, sid="s1", **extra):
    return {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts,
            "message": {"role": "user", "content": content}, **extra}


def _asst(uuid, ts, msg_id, out, sid="s1", model="claude-opus-4-8", tools=(), cr=0, c1h=0):
    content = [{"type": "text", "text": "..."}]
    content += [{"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
    return {"type": "assistant", "uuid": uuid, "sessionId": sid, "timestamp": ts,
            "message": {"id": msg_id, "model": model, "content": content,
                        "usage": {"input_tokens": 10, "output_tokens": out,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation": {"ephemeral_1h_input_tokens": c1h}}}}


def _result(uuid, ts, tool_use_id, body="ok", sid="s1"):
    return _user(uuid, ts, [{"type": "tool_result", "tool_use_id": tool_use_id, "content": body}], sid=sid)


class _ScanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "c.db")
        self.root = os.path.join(self.tmp, "projects")
        init_db(self.db)

    def _write(self, slug, sid, records):
        d = os.path.join(self.root, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def _scan(self):
        scan_dir(self.root, self.db)


class PromptAttributionTests(_ScanCase):
    def setUp(self):
        super().setUp()
        self._write("proj", "s1", [
            _user("u1", "2026-09-01T00:00:00Z", "ops"),
            _asst("a1", "2026-09-01T00:00:01Z", "m1", 50, tools=[("t1", "Read", {"file_path": "x.py"})]),
            _result("r1", "2026-09-01T00:00:02Z", "t1"),
            _asst("a2", "2026-09-01T00:00:03Z", "m2", 60, tools=[("t2", "Bash", {"command": "ls"})]),
            _result("r2", "2026-09-01T00:00:04Z", "t2"),
            _asst("a3", "2026-09-01T00:00:05Z", "m3", 70),
            # Injected by Claude Code, not typed by the human:
            _user("x1", "2026-09-01T00:01:00Z", "<local-command-stdout>Set model</local-command-stdout>"),
            _user("x2", "2026-09-01T00:01:01Z", [{"type": "text", "text": "[Image: source: a.png]"}], isMeta=True),
            _user("x3", "2026-09-01T00:01:02Z", "[Your previous response had no visible output. Please continue.]"),
            _user("x4", "2026-09-01T00:01:03Z", "subagent task", isSidechain=True),
            _user("u2", "2026-09-01T00:02:00Z", "gg claude"),
            _asst("a4", "2026-09-01T00:02:01Z", "m4", 5, cr=1_000_000),
            _user("u3", "2026-09-01T00:03:00Z", [{"type": "image", "source": {}}]),
        ])
        self._scan()

    def test_only_human_prompts_are_turns(self):
        self.assertEqual(overview_totals(self.db)["turns"], 3)

    def test_prompt_cost_covers_whole_tool_loop(self):
        rows = {r["prompt_text"]: r for r in prompt_costs(self.db, PRICING, limit=10)}
        ops = rows["ops"]
        self.assertEqual(ops["api_calls"], 3)
        self.assertEqual(ops["output_tokens"], 180)
        # opus-4-8: 3×10 in @ $5 + 180 out @ $25 per MTok
        self.assertAlmostEqual(ops["cost_usd"], (30 * 5 + 180 * 25) / 1e6, places=9)

    def test_sort_by_cost_vs_tokens(self):
        by_cost = prompt_costs(self.db, PRICING, sort="cost", limit=10)
        by_tokens = prompt_costs(self.db, PRICING, sort="tokens", limit=10)
        self.assertEqual(by_cost[0]["prompt_text"], "gg claude")  # 1M cache reads = $0.50
        self.assertEqual(by_tokens[0]["prompt_text"], "ops")

    def test_sort_recent_and_image_only_prompt(self):
        rows = prompt_costs(self.db, PRICING, sort="recent", limit=10)
        self.assertEqual(rows[0]["user_uuid"], "u3")
        self.assertEqual(rows[0]["api_calls"], 0)
        self.assertEqual(rows[0]["cost_usd"], 0)


class SessionListTests(_ScanCase):
    def setUp(self):
        super().setUp()
        self._write("proj", "real", [
            _user("u1", "2026-09-01T00:00:00Z", "hi", sid="real"),
            _asst("a1", "2026-09-01T00:00:01Z", "m1", 100, sid="real", c1h=1000),
        ])
        self._write("proj", "ghost", [
            _user("g1", "2026-09-02T00:00:00Z", "<command-name>/model</command-name>", sid="ghost"),
        ])
        self._scan()

    def test_empty_sessions_hidden_by_default(self):
        ids = [r["session_id"] for r in session_costs(self.db, PRICING)]
        self.assertEqual(ids, ["real"])
        ids = [r["session_id"] for r in session_costs(self.db, PRICING, include_empty=True)]
        self.assertEqual(sorted(ids), ["ghost", "real"])

    def test_tokens_include_cache_writes_and_cost(self):
        row = session_costs(self.db, PRICING)[0]
        self.assertEqual(row["tokens"], 10 + 100 + 1000)
        self.assertAlmostEqual(row["cost_usd"], (10 * 5 + 100 * 25 + 1000 * 10) / 1e6, places=9)


class ProjectCostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "p.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript(r"""
            INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens)
            VALUES
              -- More tokens, but cheap Sonnet.
              ('a1','s1','C--Users-g-Desktop-token-dashboard','C:\Users\g\Desktop\token dashboard',
               'assistant','2026-09-01T00:00:00Z','claude-sonnet-5',100000,0,0),
              -- Fewer tokens on Opus, plus a big cache-read bill.
              ('a2','s2','C--Users-g-token-dashboard','C:\Users\g\token dashboard',
               'assistant','2026-09-01T00:00:00Z','claude-opus-4-8',50000,0,2000000);
            """)
            c.commit()

    def test_sorted_by_cost_with_cache_read_cost(self):
        rows = project_costs(self.db, PRICING)
        self.assertEqual(rows[0]["project_slug"], "C--Users-g-token-dashboard")
        self.assertAlmostEqual(rows[0]["cache_read_usd"], 1.00, places=6)
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.25 + 1.00, places=6)

    def test_same_folder_name_is_disambiguated(self):
        names = {r["project_slug"]: r["project_name"] for r in project_costs(self.db, PRICING)}
        self.assertEqual(names["C--Users-g-Desktop-token-dashboard"], r"Desktop\token dashboard")
        self.assertEqual(names["C--Users-g-token-dashboard"], r"g\token dashboard")


class SkillBodyTests(_ScanCase):
    def test_skill_size_measured_from_injected_body(self):
        body = "Base directory for this skill: /x/pdf\n\n" + "x" * 3960
        self._write("proj", "s1", [
            _user("u1", "2026-09-01T00:00:00Z", "make a pdf"),
            _asst("a1", "2026-09-01T00:00:01Z", "m1", 5,
                  tools=[("toolu_1", "Skill", {"skill": "anthropic-skills:pdf"})]),
            _result("r1", "2026-09-01T00:00:02Z", "toolu_1", "Launching skill: anthropic-skills:pdf"),
            _user("b1", "2026-09-01T00:00:03Z", [{"type": "text", "text": body}],
                  isMeta=True, sourceToolUseID="toolu_1"),
        ])
        self._scan()
        self.assertEqual(skill_body_tokens(self.db), {"anthropic-skills:pdf": len(body) // 4})
        # The injected body is neither a prompt nor a tool in the Top tools chart.
        self.assertEqual(overview_totals(self.db)["turns"], 1)
        self.assertEqual([r["tool_name"] for r in tool_token_breakdown(self.db)], ["Skill"])


if __name__ == "__main__":
    unittest.main()
