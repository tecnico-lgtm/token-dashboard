"""Rule-based tips engine — produces actionable suggestions from SQLite."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import List, Optional

from .db import connect, prompt_usage
from .pricing import _tier_from_name, cost_for, load_pricing


def _iso_days_ago(today_iso: str, n: int) -> str:
    d = datetime.fromisoformat(today_iso.replace("Z", ""))
    return (d - timedelta(days=n)).isoformat()


def _key(category: str, scope: str) -> str:
    return f"{category}:{scope}"


def _is_dismissed(db_path, key: str) -> bool:
    with connect(db_path) as c:
        r = c.execute("SELECT dismissed_at FROM dismissed_tips WHERE tip_key=?", (key,)).fetchone()
    if not r:
        return False
    return (time.time() - r["dismissed_at"]) < 14 * 86400


def dismiss_tip(db_path, key: str) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT OR REPLACE INTO dismissed_tips (tip_key, dismissed_at) VALUES (?, ?)",
            (key, time.time()),
        )
        c.commit()


def cache_discipline_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    sql = """
      SELECT project_slug,
             SUM(cache_read_tokens) AS cr,
             SUM(input_tokens + cache_create_5m_tokens + cache_create_1h_tokens) AS rebuild
        FROM messages
       WHERE type='assistant' AND timestamp >= ?
       GROUP BY project_slug
       HAVING (cr + rebuild) > 100000
    """
    out = []
    with connect(db_path) as c:
        for row in c.execute(sql, (since,)):
            total = (row["cr"] or 0) + (row["rebuild"] or 0)
            hit = (row["cr"] or 0) / total if total else 0
            if hit < 0.40:
                key = _key("cache", row["project_slug"])
                if _is_dismissed(db_path, key):
                    continue
                out.append({
                    "key": key,
                    "category": "cache",
                    "title": f"Low cache hit rate in {row['project_slug']}",
                    "body": f"Cache hit rate is {hit*100:.0f}% over the last 7 days. Sessions that restart context frequently rebuild cache. Consider longer-lived sessions or fewer context resets.",
                    "scope": row["project_slug"],
                })
    return out


def _redundant_reads(conn, since: str) -> List[dict]:
    """Per file: Reads that re-fetched content Claude had already seen.

    A Read right after an Edit/Write of the same file is Claude checking its
    own change, and the first Read in a session is unavoidable — neither is
    counted. Edits and Writes themselves are never counted as reads.
    """
    counts, sessions = {}, {}
    seen, dirty = set(), set()
    for row in conn.execute("""
      SELECT session_id, tool_name, target
        FROM tool_calls
       WHERE tool_name IN ('Read','Edit','Write') AND target IS NOT NULL AND timestamp >= ?
       ORDER BY session_id, timestamp, id
    """, (since,)):
        k = (row["session_id"], row["target"])
        if row["tool_name"] != "Read":
            dirty.add(k)
            seen.add(k)
            continue
        if k in dirty:
            dirty.discard(k)
        elif k in seen:
            counts[row["target"]] = counts.get(row["target"], 0) + 1
            sessions.setdefault(row["target"], set()).add(row["session_id"])
        seen.add(k)
    return [{"target": t, "n": n, "sessions": len(sessions[t])}
            for t, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def repeated_target_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        for row in [r for r in _redundant_reads(c, since) if r["n"] > 10][:10]:
            key = _key("repeat-file", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-file",
                "title": f"{row['target']} re-read {row['n']} times",
                "body": f"Claude re-read this file {row['n']} times across {row['sessions']} sessions in the past 7 days without having changed it in between. A short summary of it in CLAUDE.md would save those reads.",
                "scope": row["target"],
            })
        for row in c.execute("""
          SELECT target, COUNT(*) AS n
            FROM tool_calls
           WHERE tool_name='Bash' AND timestamp >= ?
           GROUP BY target HAVING n > 15
           ORDER BY n DESC LIMIT 10
        """, (since,)):
            key = _key("repeat-bash", row["target"] or "?")
            if _is_dismissed(db_path, key):
                continue
            out.append({
                "key": key, "category": "repeat-bash",
                "title": f"`{row['target']}` ran {row['n']} times",
                "body": f"This bash command ran {row['n']} times in the past 7 days. Consider a watch flag or shell alias.",
                "scope": row["target"],
            })
    return out


SHORT_PROMPT_OUTPUT_TOKENS = 500


def right_size_tips(db_path, today_iso: Optional[str] = None,
                    pricing: Optional[dict] = None) -> List[dict]:
    """Whole prompts answered on Opus with a short total reply.

    Judged per prompt (every API call it triggered, tool loop included), not
    per API step: a multi-step task has many short steps but can't switch
    model halfway, while a quick question answered in a few hundred tokens
    genuinely could have run on Sonnet.
    """
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    pricing = pricing or load_pricing()
    prompts = {}
    for row in prompt_usage(db_path, since=since):
        if row["api_calls"]:
            prompts.setdefault(row["user_uuid"], []).append(row)
    n, api_opus, api_sonnet = 0, 0.0, 0.0
    for rows in prompts.values():
        if any(_tier_from_name(r["model"]) != "opus" for r in rows):
            continue
        if sum(r["output_tokens"] for r in rows) >= SHORT_PROMPT_OUTPUT_TOKENS:
            continue
        n += 1
        for r in rows:
            api_opus += cost_for(r["model"], r, pricing)["usd"] or 0.0
            api_sonnet += cost_for("sonnet", r, pricing)["usd"] or 0.0
    savings = api_opus - api_sonnet
    if n < 10 or savings < 1.0:
        return []
    key = _key("right-size", "opus-short-prompts-7d")
    if _is_dismissed(db_path, key):
        return []
    return [{
        "key": key, "category": "right-size",
        "title": f"{n} quick Opus prompts might fit on Sonnet",
        "body": (f"{n} prompts in the last 7 days got a complete answer in under "
                 f"{SHORT_PROMPT_OUTPUT_TOKENS} output tokens on Opus, costing ~${api_opus:.2f}. "
                 f"At current Sonnet rates they'd have cost ~${api_sonnet:.2f} (savings ~${savings:.2f}). "
                 f"Switching model mid-session resets the cache, so this pays off for quick "
                 f"questions asked in a separate session."),
        "scope": "opus-short-prompts-7d",
    }]


def outlier_tips(db_path, today_iso: Optional[str] = None) -> List[dict]:
    today_iso = today_iso or datetime.utcnow().isoformat()
    since = _iso_days_ago(today_iso, 7)
    out = []
    with connect(db_path) as c:
        big = c.execute("""
          SELECT COUNT(*) AS n, AVG(result_tokens) AS avg_t
            FROM tool_calls
           WHERE tool_name='_tool_result' AND result_tokens > 50000 AND timestamp >= ?
        """, (since,)).fetchone()
        if big and (big["n"] or 0) >= 5:
            key = _key("tool-bloat", "result-50k+")
            if not _is_dismissed(db_path, key):
                out.append({
                    "key": key, "category": "tool-bloat",
                    "title": f"{big['n']} tool results over 50k tokens this week",
                    "body": f"Average size is {int(big['avg_t']):,} tokens. Pipe long Bash output to head/tail and ask for narrower file reads.",
                    "scope": "result-50k+",
                })
        for row in c.execute("""
          SELECT agent_id, COUNT(*) AS n,
                 AVG(input_tokens+output_tokens) AS mean_t,
                 MAX(input_tokens+output_tokens) AS max_t
            FROM messages
           WHERE is_sidechain=1 AND agent_id IS NOT NULL AND timestamp >= ?
           GROUP BY agent_id HAVING n >= 10
        """, (since,)):
            if (row["max_t"] or 0) > 6 * (row["mean_t"] or 1) and (row["max_t"] or 0) > 50_000:
                key = _key("subagent-outlier", row["agent_id"])
                if _is_dismissed(db_path, key):
                    continue
                out.append({
                    "key": key, "category": "subagent-outlier",
                    "title": f"Subagent {row['agent_id']} has cost outliers",
                    "body": f"Largest invocation used {int(row['max_t']):,} tokens vs mean {int(row['mean_t']):,}. Worth checking what those did differently.",
                    "scope": row["agent_id"],
                })
    return out


def all_tips(db_path, today_iso: Optional[str] = None,
             pricing: Optional[dict] = None) -> List[dict]:
    return [
        *cache_discipline_tips(db_path, today_iso),
        *repeated_target_tips(db_path, today_iso),
        *right_size_tips(db_path, today_iso, pricing),
        *outlier_tips(db_path, today_iso),
    ]
