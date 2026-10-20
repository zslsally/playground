# Local configuration

Create `config.local.json` beside SKILL.md from `config.example.json`, using the user's established workspace and output location. Keep it mode 600 and outside Git tracking. No tokens/cookies/passwords are needed. Set `python` to an available Python 3.9+ executable. Markdown link checks use `markdown-it-py`; use an existing environment containing it when possible. Without it the pipeline reports the check as skipped and still performs archive integrity verification.

`destination` defaults conceptually to `~/Documents/Notion Backups/<workspace>/`. Keep data outside repositories. `scope` and `format` describe the entire comparison series. Reuse them; a user-requested different workspace, page-only scope or format needs a separate configuration/destination, passed using `--config`.

`baseline_snapshot` may point to an existing verified snapshot in the destination, including the initial legacy backup. The runner prefers a newer comparable verified snapshot automatically. It does not assume scope or identity from filenames alone. Imported baselines with historical `verification.json` can seed previously observed link notices; routine reports label this historical evidence rather than rerunning or implying live validation.

Commands:

- `status`: compact settings, pending request and candidate paths; read-only.
- `requested`: record a request **after** the UI confirms it started. Does not start Notion export.
- `run [--source ZIP] [--wait-seconds 0..60] [--dry-run]`: discover, verify, audit, archive/report; dry-run never creates a snapshot or changes pending state.
- `run --source ZIP --dry-run --force-audit`: exercise all local checks against an already archived file without adding a duplicate.
- `run --source ZIP --force-audit`: refresh reports on a checksum-verified existing snapshot using a comparable earlier baseline. Source ZIP bytes remain unchanged.
- Original `backup_archive.py snapshot/verify` remains available for explicit low-level work, not routine orchestration.

If a crash leaves a failed or incomplete snapshot, keep it; a subsequent run creates a separate snapshot. A saved archive without pipeline reports is recoverable: the next run verifies its checksum and completes its reports.

Discovery only scans configured directories non-recursively for ZIP patterns, ignores files younger than `settle_seconds`, and refuses multiple candidates. While an export is pending, automatic discovery ignores files older than that request. A specifically supplied source is accepted even if its timestamp differs. No download is deleted or moved.

The local report checks UTF-8 Markdown, ordinary parsed Markdown links/images, page IDs, prior file/attachment counts and changed/missing per-ID content hashes. It excludes external downloads, raw HTML links, anchor/rendering validation and nested-ZIP link traversal (nested bytes still receive full archive checks). Each text file is limited to 32 MiB for link parsing; skipped checks are reported. JSON counts/paths are bounded on stdout; complete detail stays in the snapshot.
