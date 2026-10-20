# API setup and coverage

## One-time setup

Copy `config.example.json` to `config.local.json` beside SKILL.md if it does not exist. Preserve existing ZIP settings when adding `api`. Keep mode 600 and Git-ignored. `api.destination` is separate from the legacy ZIP `destination`. Choose a folder outside the repository, normally `~/Documents/Notion Backups/<workspace>/API`.

Use an official Notion internal connection with **Read content**, sharing the intended top-level pages/databases with it, or an official personal access token with the intended workspace access. Ordinary API content backup does not require the Enterprise admin-export API. Read comments is a separate capability for `comments: page` or `all`. Reuse known root IDs where available; change roots only for an intentional scope change. The first root anchors the intended account before a backup starts; other inaccessible roots produce partial coverage. `check` tests every root. This tests access, not workspace-owner identity.

Credentials stay outside config and code:

- Set `NOTION_BACKUP_TOKEN` in the invoking process's environment, or
- In macOS **Keychain Access**, create a password item named `codex-notion-backup`, account `notion-backup`, with the token as its password. The script invokes `security find-generic-password` and captures output privately. The login Keychain must be unlocked and permit access for unattended runs.

Use the service/account values from the actual local configuration; the values above are defaults. Create a **generic password item in the login keychain** using Keychain Access (Command-N). If its startup dialog offers **Open Passwords** or **Open Keychain Access**, choose **Open Keychain Access**. A similarly named entry in the newer Passwords app does not establish that `security find-generic-password` can find it. When status reports `needs_credential`, check the app, keychain, item type and both configured fields before assuming the token is invalid. Metadata-only checks can use `security find-generic-password -s <service> -a <account>` without `-w`; sandbox failures should be distinguished from a missing item.

Do not paste tokens into chat, shell history, scheduling prompts or command-line arguments. Do not reuse cookies or extract connector credentials. A connected Notion app is independent of this script. Missing/locked credentials return `needs_credential` without creating a snapshot. Installing the skill does not provision credentials.

Use absolute paths for scheduling:

```bash
python3 /absolute/skill/scripts/api_backup.py --config /absolute/skill/config.local.json check
python3 /absolute/skill/scripts/api_backup.py --config /absolute/skill/config.local.json run
python3 /absolute/skill/scripts/api_backup.py --config /absolute/skill/config.local.json run --mode audit
```

Python 3.9+; no packages required. `status` is local-only; `check` reads the API without snapshots. `run` defaults to daily incremental mode; `run --mode audit` forces a full refresh of ordinary pages. A run creates a snapshot unless another unfinished snapshot requires attention. Exit 0 includes paused/progress results, not just successful archives; schedulers must inspect JSON status and warning counts. Exit 2 means missing setup, needs-resume, partial data or failure. No scheduler is installed by the script. For scheduling requests, follow the environment's scheduling tools and the user's timezone/frequency. An OS scheduler can execute Python without model tokens; a Codex automation still invokes a model.

## Pause, resume and progress

```bash
python3 scripts/api_backup.py progress '/absolute/snapshot'
python3 scripts/api_backup.py pause '/absolute/snapshot'
python3 scripts/api_backup.py resume '/absolute/snapshot'
# Optional bounded session; pauses rather than finalizing on timeout:
python3 scripts/api_backup.py run --max-seconds 300
```

The CLI prints the snapshot path/mode once at startup and a compact result at exit. Resume reads the saved mode; an explicit conflicting `--mode` is rejected. `progress` reads a small local file without credentials/network; counts describe the last committed entity, not the in-flight page. A running manifest alone does not prove a live process. `pause` only requests a stop; confirm the runner exits with `paused`. SIGINT/SIGTERM also request a graceful pause. The runner stops at an API-response boundary and drains queued downloads, so the time budget is not a hard deadline.

Checkpoints are atomically saved after discovery and each entity. Resume verifies committed raw/Markdown/CSV/attachment hashes, skips completed entities, and rereads the interrupted entity from its beginning. A crash during discovery repeats discovery. Already-written responses are retained with new sequence numbers. `.work/` contains private recovery state and original Markdown, including signed attachment URLs; credentials are never checkpointed. Final manifests exclude recovery/progress files.

Resume continues the original nontransactional snapshot; completed pages may have changed during the pause. Legacy snapshots lack checkpoints and cannot be safely reconstructed from raw responses alone: preserve their paused process and files. Never auto-resume a user-requested pause to test an optimization. No retention/deletion is automatic.

## Configuration

`database_pages`: `links` by default, or `full` only when explicitly requested. Override for one invocation with `run --database-pages full`; use the same flag on resume. Default mode keeps database schemas, batch-query CSV/raw properties, titles, URL-property values and Notion links in `links.json`. It skips database row bodies, comments, property-item pagination and attachment bytes, including descendants even if search or prior inventory finds them separately. Missing external URLs retain Notion links. Ordinary pages outside databases still receive full reads. Authorship is not inferred: self-written database entries are also link-only by default.

`comments` configures audit mode (`page` by default). Daily mode does not request comments. An unchanged page's comments may change independently of its content timestamp, which is why audits remain necessary.

## Daily incremental and audit modes

Daily mode always refreshes search, database schemas and batch-query link records. For ordinary pages, it compares API `last_edited_time` with `page_states` from `latest-success.json`. An unchanged page reuses prior note/raw files and content-addressed assets after verifying the source hashes; the new snapshot still has its own manifest and usable paths. A changed/new page, a missing legacy state, or any invalid reuse input triggers a full live page read. Counts expose `reused_pages`, `refreshed_pages`, and `reuse_fallbacks`.

Audit mode ignores the incremental baseline, walks every ordinary page's block tree, downloads/validates attachments, reads configured comments and retrieves page metadata afterward to detect edits during the run. A successful audit becomes the next incremental baseline. Use audits periodically and official exports less frequently; neither schedule is installed automatically. `last_edited_time` is an optimization hint, not proof that every descendant-side change is represented, so daily runs must not replace audits.

Modes have separate histories and scope signatures. `api.destination` remains the base: default snapshots go under `API/database-links/snapshots/`; full snapshots retain `API/snapshots/`. Earlier full backups and any paused full process remain untouched. `--database-pages full` restores the old full-data scope, not a different token or account. Do not resume a snapshot under another mode.

`api.roots`: a nonempty list of `{type: "page" | "database" | "data_source", id: "UUID"}`. `discover_search: true` adds visible indexed pages/data sources; false limits discovery to roots, their descendants/databases and prior inventory. Search does not establish whole-workspace completeness. A different workspace, root set, search mode, API version or comment scope requires a new API destination; the runner rejects mixing series.

`comments`: `page` (default, unresolved page-level discussions), `all` (also discussions on visited blocks; many more requests), or `none`. `max_attachment_bytes`: 512 MiB by default per file; larger files fail visibly. External file objects retain URLs only and produce a warning. Hosted files require HTTPS on supported Notion/S3 hosts. Attachments never receive the API bearer token, including across redirects.

## Saved data and limits

`API/snapshots/<UTC timestamp>-<unique suffix>/` contains:

- `raw/`: current responses plus verified page-response bundles reused by daily mode, including unknown fields, metadata, schemas, paginated properties/rollups, blocks, Markdown responses and audit comments. Expiring signed URLs and private note data remain in these private files.
- `notes/<page-id>.md`: full-scope pages only, with downloaded URLs rewritten locally. Truncated/unsupported Markdown is reported; raw block data is retained. Rendering varies across viewers.
- `links.json`: default mode's database row titles, original URL properties, Notion URLs and inline properties; descendant omissions are listed separately. Inline properties may be truncated by the API; full mode retains property-item pagination.
- `databases/<data-source-id>.csv`: row IDs, titles and JSON properties from query responses. Full property pagination remains in `raw/`. CSV is a convenience view, not an official export.
- `assets/`: downloaded files hardlinked to shared `API/asset-store/` by content hash. Snapshots contain their own file entries and can be copied independently. Daily runs download bytes before deduplication; no timestamp-only skipping. Treat hardlinked assets as immutable.
- `index.json`, `report.json`, `manifest.json`: titles/file locations, limitations/errors, entity inventory and SHA-256/size for each file. `verify` recomputes listed hashes. Hashes detect corruption, not malicious rewriting of data and manifests together.

The runner uses a local lock, a minimum 0.34-second interval between API request starts (network time counts toward it), bounded retries, pagination-cycle detection and a request budget. Hosted attachments use four workers and a bounded queue; the API stays sequential. Identical URLs are downloaded once per snapshot. Plain short title/rich-text values without mentions and relations explicitly marked `has_more: false` are already preserved in page metadata, so extra property calls are skipped. Mentions, limit-boundary/unknown shapes, people, rollups and formulas retain separate reads; see [page response limits](https://developers.notion.com/reference/retrieve-a-page).

Retry-After above 60 seconds stops for a later run. An inaccessible prior entity is retried and reported, not assumed deleted; its ID remains in subsequent inventory. `latest-success.json` advances only without warnings; `latest-attempt.json` tracks verified attempts including partial ones. Neither advances on pause. A new `run` refuses unfinished snapshots, preventing an accidental second full download.

Daily runs refresh discovery/database records and reread changed ordinary pages; audits reread all ordinary-page content because timestamps do not prove every descendant-side change. Snapshots are nontransactional; audits report edits detected during reading. API-inaccessible pages, resolved comments, version history, permissions/sharing settings and unsupported API content are outside guaranteed coverage. Data sources reaching the 10,000-query-result boundary are marked partial and need a future partitioned-query implementation. Periodic official exports remain useful; this mirror does not promise one-click full-fidelity restoration.

## Maintainer contracts

Checked 2026-09-16; pinned `Notion-Version: 2026-03-11`. Only GET plus POST `/search` and `/data_sources/{id}/query`. Refresh contracts when changing versions or diagnosing an API error, not every backup.

- [Official SDK client](https://github.com/makenotion/notion-sdk-js/blob/main/src/Client.ts)
- [Search](https://developers.notion.com/reference/post-search)
- [Data source query](https://developers.notion.com/reference/query-a-data-source)
- [Property pagination](https://developers.notion.com/reference/retrieve-a-page-property)
- [Enhanced Markdown](https://developers.notion.com/reference/retrieve-page-markdown)
- [Comments](https://developers.notion.com/reference/list-comments)
- [Comment attachments](https://developers.notion.com/reference/comment-attachment)
- [Request limits](https://developers.notion.com/reference/request-limits)
