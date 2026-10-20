# Optional source audit

Use for an explicit fresh completeness audit or a new missing-page concern, not every successful backup.

Confirm the connector workspace ID with `fetch self` when not already known. Fetch the relevant source schema, then obtain page IDs and titles using a supported query. Paginate until complete: SQL commonly returns 100 rows and `has_more`; use stable ordering and keyset/offset continuation as appropriate. Never compare a truncated result. Compare ID sets and title multisets, not just totals. Select narrow columns and save large tool results to a private local report instead of printing all rows into the conversation.

`local-verification.json` identifies missing prior IDs and unresolved local links. Inspect only the needed source pages. Empty databases may omit CSV files; Home views, My Tasks, recent activity and directory views may be system/aggregate surfaces. Classify using current evidence; do not suppress by filename alone. An old empty-database observation does not prove it remains empty.

Write fresh source results separately in `source-audit.json` with workspace ID, observation time, scope and limitations. Historical reports and the local baseline comparison cannot claim current live equality. Source-body comparisons, permissions, comments and restore fidelity require their own checks if requested. Do not modify original ZIPs or Notion data during an audit.
