---
name: export-timepill-diary
description: Export a private Timepill (胶囊日记) account without modifying it. Use for full or named/year notebook backups, including “导出2026年胶囊日记”, with diaries, comments, images, rescan, and append.
---

# Export Timepill Diary

Run `python3 scripts/export_timepill.py`, resolving the script relative to this file. Account, output, and fixed export settings live in the ignored `config.local.json`; copy `config.example.json` first if it is missing. Inspect local settings when reporting configuration and edit them only when asked. Never store passwords there.

Map a named/year notebook such as “2026年胶囊日记” to `--notebook "2026"`. Use full-account mode only when explicitly requested. Selected notebooks are exact-matched by ID or subject and stored under `notebooks/<subject>--<id>/`; shared account files stay at the output root.

Inspect that notebook's `manifest.json` before running:

- No manifest: run without a mode.
- Incomplete `initial`/`rescan`, or suspected historical gaps: use `--rescan`.
- Otherwise, a complete export or interrupted `append`: use `--append`.

`--rescan` checks the whole notebook while reusing stored records. `--append` scans from the last successfully covered China-time date minus the configured overlap through today; the checkpoint advances only after success. A legacy interrupted append without a checkpoint performs one full recovery scan. Existing comments refresh only in today's overlap window; new diaries still fetch comments, while a reported zero count skips the endpoint.

The script reads the macOS `codex-timepill-export` Keychain item, then prompts invisibly if absent. It stores no password and uses only `GET`; API requests are sequential and credential-free image downloads use the configured worker pool.

Raw diaries and comment threads are versioned JSONL; images are content-deduplicated under `media/`. Treat `manifest.json` as authoritative and report scope, completion, counts, output, append window, and unresolved errors. Never mix accounts or notebook scopes.
