---
name: backup-notion
description: Back up Notion with the read-only API as the daily primary workflow and official exports as a periodic supplement. Save and verify dated local snapshots. Use for Notion 日常备份、API 自动备份、定期官方导出、备份校验.
---

# Notion backup

API is the daily primary workflow; official exports are periodic supplements. Default daily mode incrementally backs up ordinary pages and keeps database schemas/rows as properties and links. Database rows and descendants do not get body/comment reads or attachment downloads. This is structural, not authorship detection; self-written database notes follow it too.

```bash
python3 scripts/api_backup.py run
# Periodic full refresh of ordinary pages, blocks, attachments and page comments:
python3 scripts/api_backup.py run --mode audit
# Only when explicitly requested:
python3 scripts/api_backup.py run --database-pages full
```

Resolve paths relative to this skill. Python 3.9+, no packages or model calls. Credentials stay local. Daily mode compares current `last_edited_time` with the latest verified success: unchanged ordinary pages reuse verified raw responses, Markdown and hardlinked assets; changed/new pages are fully read. It skips comments and the second metadata read. Missing/corrupt reuse inputs fall back to a fresh read. With no reusable baseline, the first daily run reads all ordinary pages.

Audit mode never reuses page content. It rereads ordinary page blocks/assets, configured page comments, and metadata after content to detect concurrent edits. Run audits periodically; timestamps may not expose every descendant-side change. Both modes verify independent snapshots and checkpoint entities. Default database rows use batch queries. `API/database-links/` holds link-only-database history; explicit database-full mode uses `API/`. Resume automatically preserves the snapshot's mode.

Launch once and wait. Use `progress /absolute/snapshot` only when needed. Do not repeatedly scan raw files, load scripts/docs, or reissue `run` during normal execution.

Handle the returned JSON:

- `archived`: report snapshot link, refreshed/reused/link-only counts and integrity. Coverage is the selected mode, not whole-workspace completeness or full restoration.
- `partial` / `archived_with_warnings`: inspect relevant `report.json` entries and describe gaps. Never label partial coverage successful.
- `needs_resume`: use `resume /absolute/snapshot` when continuation is authorized. Optimization does not authorize resuming a user-requested pause.
- `paused`: report retained progress, not completion. `pause /absolute/snapshot` requests a graceful stop; `resume /absolute/snapshot` continues after exit/restart. Both `run` and `resume` accept `--max-seconds N`. Legacy snapshots without checkpoints cannot use resume; preserve their process/files.
- `needs_credential` / `failed`: diagnose the safe error using [setup.md](references/setup.md). Never print tokens, signed URLs or raw HTTP bodies. Connector login is independent of script credentials.

`status` checks credentials locally; `check` validates API/root access. `progress` and `verify /absolute/snapshot` need no token. Read [setup.md](references/setup.md) for recovery, configuration or coverage questions.

Scheduling is separate and only installed when requested, using the environment's scheduling tools. Python itself needs no model; Codex automations use tokens. Keep scheduling prompts short and report failures/coverage warnings. No supplementary-export frequency is configured.

For official export requests, read [export.md](references/export.md). For supplied ZIPs, use `python3 scripts/run_backup.py run --source '/absolute/export.zip'`; configuration: [zip-setup.md](references/zip-setup.md). Only requested deep audits need [deep-audit.md](references/deep-audit.md). API runs never refresh the official-export date. Never automatically delete older snapshots or source downloads.
