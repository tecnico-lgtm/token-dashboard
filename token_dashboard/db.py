"""SQLite schema, connection, and shared query helpers."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Union

from .naming import best_project_name, project_name_for, project_names  # noqa: F401  (re-exported)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT,
  is_prompt               INTEGER NOT NULL DEFAULT 0,
  is_meta                 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid  TEXT    NOT NULL,
  session_id    TEXT    NOT NULL,
  project_slug  TEXT    NOT NULL,
  tool_name     TEXT    NOT NULL,
  target        TEXT,
  result_tokens INTEGER,
  is_error      INTEGER NOT NULL DEFAULT 0,
  timestamp     TEXT    NOT NULL,
  tool_use_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);
CREATE INDEX IF NOT EXISTS idx_tools_msg     ON tool_calls(message_uuid);
CREATE INDEX IF NOT EXISTS idx_tools_use_id  ON tool_calls(tool_use_id);

CREATE TABLE IF NOT EXISTS plan (
  k TEXT PRIMARY KEY,
  v TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key       TEXT PRIMARY KEY,
  dismissed_at  REAL NOT NULL
);
"""


def default_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        _migrate_add_message_id(c)
        _migrate_add_prompt_flags(c)
        c.executescript(SCHEMA)


def _reset_scan_state(conn) -> None:
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM tool_calls")
    conn.execute("DELETE FROM files")


def _migrate_add_message_id(conn) -> None:
    """Add messages.message_id for streaming-snapshot dedup.

    Why: pre-migration rows were summed from all streaming snapshots (over-count).
    How to apply: if the old table exists without the column, add it and clear
    messages/tool_calls/files so the next scan replays JSONLs cleanly. Source
    of truth is on disk; rescanning is cheap.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "message_id" in cols:
        return
    conn.execute("ALTER TABLE messages ADD COLUMN message_id TEXT")
    _reset_scan_state(conn)
    conn.commit()


def _migrate_add_prompt_flags(conn) -> None:
    """Add messages.is_prompt / is_meta and tool_calls.tool_use_id.

    Why: tool results, isMeta injections and command output are all
    type='user' records, so counting or joining on type='user' mistook them
    for human prompts; tool_use_id links a Skill call to the skill body
    loaded after it. Both can only be derived from the raw JSONL, so
    existing rows are cleared and the next scan replays the files.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not has_table:
        return
    msg_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    tool_cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    if "is_prompt" in msg_cols and "tool_use_id" in tool_cols:
        return
    if "is_prompt" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN is_prompt INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE messages ADD COLUMN is_meta INTEGER NOT NULL DEFAULT 0")
    if "tool_use_id" not in tool_cols:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN tool_use_id TEXT")
    _reset_scan_state(conn)
    conn.commit()


@contextmanager
def connect(path: Union[str, Path]):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _range_clause(since, until, col: str = "timestamp"):
    where, args = [], []
    if since:
        where.append(f"{col} >= ?"); args.append(since)
    if until:
        where.append(f"{col} < ?"); args.append(until)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def overview_totals(db_path, since=None, until=None) -> dict:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COUNT(DISTINCT session_id) AS sessions,
             COALESCE(SUM(is_prompt),0)               AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages WHERE 1=1 {rng}
    """
    with connect(db_path) as c:
        return dict(c.execute(sql, args).fetchone())


PROMPT_USAGE_SQL = """
  WITH seq AS (
    SELECT uuid, session_id, project_slug, type, timestamp, model, is_prompt,
           prompt_text, prompt_chars,
           input_tokens, output_tokens, cache_read_tokens,
           cache_create_5m_tokens, cache_create_1h_tokens,
           SUM(is_prompt) OVER (PARTITION BY session_id ORDER BY timestamp, rowid
                                ROWS UNBOUNDED PRECEDING) AS grp
      FROM messages
  ),
  usage AS (
    SELECT session_id, grp, model, COUNT(*) AS api_calls,
           SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
           SUM(cache_read_tokens) AS cache_read_tokens,
           SUM(cache_create_5m_tokens) AS cache_create_5m_tokens,
           SUM(cache_create_1h_tokens) AS cache_create_1h_tokens
      FROM seq WHERE type='assistant' AND grp > 0
     GROUP BY session_id, grp, model
  )
  SELECT p.uuid AS user_uuid, p.session_id, p.project_slug, p.timestamp,
         p.prompt_text, p.prompt_chars,
         u.model, COALESCE(u.api_calls,0) AS api_calls,
         COALESCE(u.input_tokens,0) AS input_tokens, COALESCE(u.output_tokens,0) AS output_tokens,
         COALESCE(u.cache_read_tokens,0) AS cache_read_tokens,
         COALESCE(u.cache_create_5m_tokens,0) AS cache_create_5m_tokens,
         COALESCE(u.cache_create_1h_tokens,0) AS cache_create_1h_tokens
    FROM seq p
    LEFT JOIN usage u ON u.session_id = p.session_id AND u.grp = p.grp
   WHERE p.is_prompt = 1 {rng}
"""


def prompt_usage(db_path, since=None, until=None) -> list:
    """One row per (human prompt, model) with the usage of every API call it caused.

    Each assistant record belongs to the most recent prompt in its session, so
    a prompt's cost covers the whole tool loop and any subagents it spawned,
    not just the first reply.
    """
    rng, args = _range_clause(since, until, col="p.timestamp")
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(PROMPT_USAGE_SQL.format(rng=rng), args)]


def project_summary(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT project_slug,
             COUNT(DISTINCT session_id) AS sessions,
             COALESCE(SUM(is_prompt), 0)     AS turns,
             COALESCE(SUM(input_tokens), 0)  AS input_tokens,
             COALESCE(SUM(output_tokens), 0) AS output_tokens,
             SUM(input_tokens)+SUM(output_tokens)
               +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS billable_tokens,
             SUM(cache_read_tokens) AS cache_read_tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY project_slug
       ORDER BY billable_tokens DESC
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        names = project_names(c)
    for r in rows:
        r["project_name"] = names.get(r["project_slug"], r["project_slug"])
    return rows


def tool_token_breakdown(db_path, since=None, until=None) -> list:
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT tool_name,
             COUNT(*) AS calls,
             COALESCE(SUM(result_tokens),0) AS result_tokens
        FROM tool_calls
       WHERE tool_name NOT IN ('_tool_result', '_skill_body') {rng}
       GROUP BY tool_name
       ORDER BY calls DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def recent_sessions(db_path, limit: int = 20, since=None, until=None,
                    include_empty: bool = False) -> list:
    """Most recently active sessions first.

    ``tokens`` is billable (input + output + cache writes), matching the
    Projects and Prompts views. Sessions where Claude never answered (a
    stray ``/model`` or an immediate exit) are hidden unless include_empty.
    """
    rng, args = _range_clause(since, until)
    having = "" if include_empty else """
      HAVING SUM(input_tokens + output_tokens + cache_read_tokens
                 + cache_create_5m_tokens + cache_create_1h_tokens) > 0"""
    sql = f"""
      SELECT session_id, project_slug,
             MIN(timestamp) AS started, MAX(timestamp) AS ended,
             COALESCE(SUM(is_prompt), 0) AS turns,
             SUM(input_tokens)+SUM(output_tokens)
               +SUM(cache_create_5m_tokens)+SUM(cache_create_1h_tokens) AS tokens,
             SUM(cache_read_tokens) AS cache_read_tokens
        FROM messages m
       WHERE 1=1 {rng}
       GROUP BY session_id {having}
       ORDER BY ended DESC
       LIMIT ?
    """
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        names = project_names(c)
    for r in rows:
        r["project_name"] = names.get(r["project_slug"], r["project_slug"])
    return rows


def session_turns(db_path, session_id: str) -> list:
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def daily_token_breakdown(db_path, since=None, until=None) -> list:
    """One row per day: stacked bar data for input/output/cache_read/cache_create."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT substr(timestamp, 1, 10) AS day,
             COALESCE(SUM(input_tokens),0)      AS input_tokens,
             COALESCE(SUM(output_tokens),0)     AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)
               + COALESCE(SUM(cache_create_1h_tokens),0) AS cache_create_tokens
        FROM messages
       WHERE timestamp IS NOT NULL {rng}
       GROUP BY day
       ORDER BY day ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def skill_body_tokens(db_path, since=None, until=None) -> dict:
    """{skill: average tokens of the skill text loaded per call}, measured.

    The Skill tool_result is only a tiny "Launching skill" ack; the real
    content arrives in an isMeta message the scanner stores as a
    ``_skill_body`` row keyed by the Skill call's tool_use_id.
    """
    rng, args = _range_clause(since, until, col="s.timestamp")
    sql = f"""
      SELECT s.target AS skill, CAST(ROUND(AVG(b.result_tokens)) AS INTEGER) AS tokens
        FROM tool_calls s
        JOIN tool_calls b ON b.tool_use_id = s.tool_use_id AND b.tool_name = '_skill_body'
       WHERE s.tool_name = 'Skill' AND s.target IS NOT NULL {rng}
       GROUP BY s.target
    """
    with connect(db_path) as c:
        return {r["skill"]: r["tokens"] for r in c.execute(sql, args)}


def skill_breakdown(db_path, since=None, until=None) -> list:
    """Per-skill invocation counts, distinct sessions, last-used timestamp.

    Token sizes come separately from ``skill_body_tokens`` (measured from the
    transcript) with the on-disk SKILL.md catalog as a fallback.
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT target AS skill,
             COUNT(*) AS invocations,
             COUNT(DISTINCT session_id) AS sessions,
             MAX(timestamp) AS last_used
        FROM tool_calls
       WHERE tool_name = 'Skill' AND target IS NOT NULL AND target != '' {rng}
       GROUP BY target
       ORDER BY invocations DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count. Caller computes cost via pricing."""
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]
