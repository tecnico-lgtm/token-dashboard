"""Dollar costs for prompts, sessions and projects (usage rows × pricing)."""
from __future__ import annotations

from typing import Dict, List, Optional

from .db import connect, prompt_usage, project_summary, recent_sessions, _range_clause
from .pricing import cost_for

USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens",
              "cache_create_5m_tokens", "cache_create_1h_tokens")

PROMPT_SORTS = {
    "cost":   lambda r: r["cost_usd"],
    "tokens": lambda r: r["billable_tokens"],
    "recent": lambda r: r["timestamp"] or "",
}


def _price(model: Optional[str], usage: dict, pricing: dict) -> dict:
    c = cost_for(model or "", usage, pricing)
    return {
        "usd": c["usd"] or 0.0,
        "cache_read_usd": c["breakdown"].get("cache_read", 0.0),
        # Zero-usage placeholders (model "<synthetic>") aren't guesses.
        "estimated": c["estimated"] and any(usage[k] for k in USAGE_KEYS),
    }


def prompt_costs(db_path, pricing: dict, sort: str = "cost", limit: int = 50,
                 since=None, until=None) -> List[dict]:
    """Every human prompt with the total cost of all API calls it triggered."""
    prompts: Dict[str, dict] = {}
    for row in prompt_usage(db_path, since, until):
        p = prompts.get(row["user_uuid"])
        if p is None:
            p = prompts[row["user_uuid"]] = {
                k: row[k] for k in ("user_uuid", "session_id", "project_slug",
                                    "timestamp", "prompt_text", "prompt_chars")
            }
            p.update(model=None, models=[], api_calls=0, billable_tokens=0,
                     output_tokens=0, cache_read_tokens=0, cost_usd=0.0,
                     cache_read_usd=0.0, estimated=False, _top=-1.0)
        if not row["api_calls"]:
            continue
        price = _price(row["model"], row, pricing)
        p["models"].append(row["model"])
        p["api_calls"] += row["api_calls"]
        p["billable_tokens"] += (row["input_tokens"] + row["output_tokens"]
                                 + row["cache_create_5m_tokens"] + row["cache_create_1h_tokens"])
        p["output_tokens"] += row["output_tokens"]
        p["cache_read_tokens"] += row["cache_read_tokens"]
        p["cost_usd"] += price["usd"]
        p["cache_read_usd"] += price["cache_read_usd"]
        p["estimated"] = p["estimated"] or price["estimated"]
        if price["usd"] > p["_top"]:
            p["_top"], p["model"] = price["usd"], row["model"]
    rows = list(prompts.values())
    for r in rows:
        del r["_top"]
        r["cost_usd"] = round(r["cost_usd"], 6)
        r["cache_read_usd"] = round(r["cache_read_usd"], 6)
    rows.sort(key=PROMPT_SORTS.get(sort, PROMPT_SORTS["cost"]), reverse=True)
    return rows[:limit]


def cost_by(db_path, pricing: dict, key: str, since=None, until=None) -> Dict[str, dict]:
    """{key value: {cost_usd, cache_read_usd, estimated}} for key in session_id/project_slug."""
    if key not in ("session_id", "project_slug"):
        raise ValueError(f"unsupported cost grouping: {key}")
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT {key} AS k, model, {", ".join(f"SUM({c}) AS {c}" for c in USAGE_KEYS)}
        FROM messages
       WHERE type='assistant' {rng}
       GROUP BY {key}, model
    """
    out: Dict[str, dict] = {}
    with connect(db_path) as c:
        for row in c.execute(sql, args):
            price = _price(row["model"], dict(row), pricing)
            agg = out.setdefault(row["k"], {"cost_usd": 0.0, "cache_read_usd": 0.0, "estimated": False})
            agg["cost_usd"] += price["usd"]
            agg["cache_read_usd"] += price["cache_read_usd"]
            agg["estimated"] = agg["estimated"] or price["estimated"]
    return out


def _attach(rows: List[dict], key: str, costs: Dict[str, dict]) -> List[dict]:
    for r in rows:
        c = costs.get(r[key], {})
        r["cost_usd"] = round(c.get("cost_usd", 0.0), 6)
        r["cache_read_usd"] = round(c.get("cache_read_usd", 0.0), 6)
        r["cost_estimated"] = c.get("estimated", False)
    return rows


def project_costs(db_path, pricing: dict, since=None, until=None) -> List[dict]:
    """Projects with cost columns, most expensive first."""
    rows = _attach(project_summary(db_path, since, until), "project_slug",
                   cost_by(db_path, pricing, "project_slug", since, until))
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


def session_costs(db_path, pricing: dict, limit: int = 20, since=None, until=None,
                  include_empty: bool = False) -> List[dict]:
    """Recent sessions with cost columns (order unchanged: most recently active first)."""
    rows = recent_sessions(db_path, limit=limit, since=since, until=until,
                           include_empty=include_empty)
    return _attach(rows, "session_id", cost_by(db_path, pricing, "session_id", since, until))
