# Known Limitations

None of these are blockers — the dashboard still gives you useful information. They're the rough edges you'll notice if you look hard.

## Skills token counts depend on the transcript format

The **tokens-per-call** column is measured from the skill text Claude Code injects after each Skill call (an `isMeta` message linked by `sourceToolUseID`), so built-in, plugin and project-local skills are all covered. Transcripts from Claude Code versions that don't write that link fall back to the size of the skill's `SKILL.md` under `~/.claude/skills/`, `~/.claude/scheduled-tasks/` or `~/.claude/plugins/` (marked `*` in the UI), and stay blank otherwise. Token counts are a chars÷4 estimate, not a tokenizer count.

## Cost for Pro / Max / Max-20x users is shown as API-equivalent, not subscription value

The Settings route lets you select your pricing plan, but the Overview cost number is always the API-equivalent (what the same usage would have cost on pay-per-token rates). If you're on Pro you pay a flat $20/month regardless of how much of that API-equivalent number you rack up. We don't do "subscription ROI" math yet — Anthropic doesn't publish per-plan rate limits as public JSON, and faking it would be worse than not doing it.

## Cowork sessions are invisible

If you use Claude's Cowork mode (server-side sessions, not local `claude` CLI), those sessions don't write JSONL to `~/.claude/projects/` and the dashboard can't see them.

## Non-standard model names get tier-fallback pricing

If a transcript references a model ID not in `pricing.json`, dated snapshots (`claude-haiku-4-5-20251001`) are priced like their alias; anything else is estimated from the tier substring (`fable`/`mythos` / `opus` / `sonnet` / `haiku`) at the current model's rates for that tier. The UI prefixes these costs with `~`. Older models billed differently from the current tier (e.g. Opus 4.1 at $15/$75) are over- or under-estimated until they're added to `pricing.json`. If the model name contains none of those substrings, it adds no cost.

## First scan takes a few seconds

The first scan reads every JSONL (roughly 10s per 100k messages). Progress is committed file by file, so stopping it with Ctrl+C and restarting picks up where it left off. Subsequent scans are incremental (mtime + byte-offset tracking in the `files` table). Upgrading to a version that adds a schema column (such as `is_prompt`) clears the cache once and rescans from scratch.

## Prompt attribution is by time order

Each API call is attributed to the most recent human prompt in its session. Background work that keeps running after you send a new message is attributed to the new prompt. The attribution query uses SQL window functions, which need the SQLite bundled with your Python to be 3.25 (2018) or newer — check with `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.

## Running two dashboards against the same DB

Both will fight over the SQLite file and you'll see inconsistent numbers and occasional `database is locked` errors. Only run one at a time. If you want to view the dashboard from a second device, use `HOST=0.0.0.0` on the one running machine and point the second device's browser at it.
