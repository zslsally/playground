# Official export, when a fresh ZIP is needed

Read workspace name/ID from `run_backup.py status` and confirm the signed-in UI matches. If scope is ambiguous, resolve it before export. The saved destination applies only to that workspace and scope.

On this Mac, start with `cua.getApp('notion.id')`, then `super+comma` to reach settings. Read the current AX state, choose workspace **通用 / General → 工作空间内容 / Export**. Use Markdown & CSV, **默认视图 / Default view**, **所有内容 / All content**, and folders for subpages, unless the user requests otherwise. These are export options; do not edit source database views. Derive fresh element IDs; never save AX indexes or screen coordinates in config.

Use CUA for UI interactions. Prefer relevant AX text over screenshots/full page dumps. When using `getAXState({emit:false})`, output the settings/dialog context and relevant actionable labels with their original IDs; re-read after each action. Do not run broad app inventory, inspect unrelated notes, or search Mail during normal export startup. If this app route is unavailable, use an available signed-in browser via CUA.

After visible confirmation, record `requested`. Notion may generate the export asynchronously for hours. A pending request and download link are not a local backup. Download through the supported browser/app flow, or continue when the user has downloaded it. Run the local pipeline once to wait up to 45 seconds; then return pending instead of repeatedly polling UI. Only inspect an export email if the user requests that route or it is needed to retrieve an available completed export.

For a failed large export, use separately named top-level batches with explicit `--source` and a separately configured scope/destination; reconcile expected roots before calling the batch set complete. Never route page-only exports into a full-workspace baseline. HTML is appropriate when comments/offline viewing matter; PDF is for individual reading copies. Do not mix formats in a comparison series.

Official references (refresh only when behavior changes):
- https://www.notion.com/help/export-your-content
- https://www.notion.com/help/back-up-your-data

No private export endpoints or browser-cookie extraction. Connector page text is not an official ZIP. The Enterprise admin export API is a separate, eligibility-dependent workflow; this skill's ordinary API mirror does not trigger that ZIP export. Recurring scheduling is optional and must reflect actual available unattended capabilities rather than promising them.
