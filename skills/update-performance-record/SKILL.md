---
name: update-performance-record
description: Read Apple Calendar on macOS and add missing performance records to an existing Notion database, for a requested date range or the available history. Use for updating or reconciling a performance log; exclude missed events and report conflicts. Calendar is read-only and Notion is create-only.
---

# Update Performance Record

## Boundaries

- Apple Calendar is strictly read-only. Never create, edit, delete, move, respond to invitations, or change calendar settings.
- Notion is **create-only** in the user-specified existing database. Never edit, delete, archive, move, or fill blanks in an existing record, including empty rows and rows created earlier in the same run. Never alter existing page content or database structure, properties, select options, relations, views, filters, or sorts. Read-only comparison requests authorize no creates.
- An update request authorizes clearly missing, unambiguous additions without further confirmation. Withhold conflicting or unclear candidates and ask the user to resolve them. Confirmation of a candidate authorizes its addition, not changes to existing records or schema.
- Without a requested range, scan relevant personal calendars through today. Exclude events starting after the run's current time, including later today; report encountered future events separately without adding them. Calendar presence establishes a scheduled event, not confirmed attendance; never invent attendance or reviews.
- Keep raw exports, source identifiers, snapshots, and run reports in a private temporary directory outside the repository. Store only reusable instructions and code in the repository. Do not install the skill elsewhere unless requested.

## Resolve the date range

- Support natural-language windows such as `过去一个月`, `最近 30 天`, `前两周`, `上个月`, and explicit start/end dates. Resolve them against the current date in the requested timezone (Mac-local by default) and state the concrete inclusive dates in the report. Do not reuse dates from a previous run.
- `过去一个月` means the same day of the previous month through today; clamp to that month's last day when necessary. `最近 N 天` includes today and the preceding N−1 dates. `上个月` means the complete previous calendar month. Convert an inclusive end date to the next local date for the script's exclusive `--to` boundary. Use calendar arithmetic, not fixed 24-hour milliseconds across daylight-saving transitions.
- For weekly updates, `前两周` means the rolling 14 dates ending today, not the previous two calendar weeks. Recompute the window on every run; overlapping weekly windows still require normal Notion deduplication.
- Apply the window to the performance's start date, not its creation or modification date. A bounded update is not a full-history audit and will not detect older missed markers or corrections outside its scope. Without a range, retain the historical scan behavior above.
- For a bounded request, pass the same `--from`/`--to` to title discovery and detail reads; do not start with a full-history title inventory. The reader filters event objects by start date in Calendar before reading details. Keep Notion duplicate checks across the destination, because an existing conflicting date can fall outside the Calendar window.

## Read both sources

1. Fetch the supplied Notion page, then its database and data source schema. Resolve IDs live; a page ID is not necessarily a database ID. Learn the fields, select options, and date conventions.
2. Read all destination rows, paginating until complete. Prefer unfiltered data-source reads; a filtered view cannot prove a record is absent. Use faithful rows/page fetches when SQL text omits rich text or links. Keep a snapshot of existing rows and schema for verification.
3. Use the bundled macOS JXA reader (paths are relative to this skill):

   ```bash
   umask 077
   performance_tmp=$(mktemp -d /private/tmp/performance-record.XXXXXX)
   osascript -l JavaScript scripts/read_calendar.js --list
   osascript -l JavaScript scripts/read_calendar.js --calendar 'NAME_FROM_LIST' --titles > "$performance_tmp/titles.json"
   osascript -l JavaScript scripts/read_calendar.js --calendar 'NAME_FROM_LIST' --match 'REVIEWED_TITLE_PATTERN' > "$performance_tmp/events.json"
   ```

   For a requested range, add `--from YYYY-MM-DD --to YYYY-MM-DD` to both export commands. For example, September 1–30 uses `--from 2026-09-01 --to 2026-10-01`. For a small window, omit `--titles`/`--match` and read all details in that window directly.

   Multiple `--calendar` arguments are supported. Select personal activity calendars; normally omit holiday, birthday, course, and work feeds. When using title discovery, review the complete list within the range, including uncertain titles, then fetch details using a pattern that also matches missed/canceled suffixes. Escape literal title punctuation when constructing a regex. Titles alone may not reveal an event's purpose, so inspect ambiguous entries. A title-pattern-filtered export alone does not prove full coverage.

   Omit date filters only for an unrestricted historical scan. `--from` is inclusive and `--to` exclusive, using Mac-local dates. For travel, pad the read window by one date on each side to cover timezone differences, then apply the requested venue-local dates before creating records; boundary padding does not authorize out-of-range additions. These exports do not expand recurrence. Use only successful, complete JSON output; an interrupted read is not an empty calendar.
4. If the shell sandbox blocks Apple Events, use the environment's normal execution approval mechanism for this read-only script. Permission denial is not an empty calendar. If access remains unavailable, stop dependent creates and report the limitation. Never bypass macOS privacy permissions through Calendar's private database.

## City mapping and overrides

Read [config.json](config.json) on each run; this is the single source for personal city grouping rules. It affects `City` on new records only, never the source location or existing Notion rows.

1. Resolve the actual venue/address to its municipality using a map/place lookup (for example, Apple Maps or Google Maps). Use the municipality, not the broader metro area, and keep the map link in the private evidence. If lookup is unavailable or ambiguous, report the candidate for confirmation rather than guessing.
2. Look up that city in `city_overrides`; otherwise use the map's city in the destination's existing naming convention. Match city names case-insensitively after trimming whitespace. Corroborate translated names or aliases against the same map place rather than using substring matches. If a same-name city makes the intended grouping unclear, or normalized keys specify conflicting outputs, ask for confirmation.
3. Each mapping value is the exact destination select label. Missing options still require user action; config never authorizes schema changes. Keep `Theater` exactly as required by the location rules below. Report any incompatible existing City value without changing it.

Edit the config only when the user requests a grouping change. `city_overrides` is a simple object mapping map city names to Notion city labels, for example `"Harrison": "New York"`; `{}` disables all overrides. Keys and values must be nonempty strings. Invalid config must be reported before dependent creates, not silently ignored. Do not infer additional city mappings or retain removed rules outside the config.

## Reconcile

- Review titles, locations, notes, and URLs; performers and show titles may contain no generic theater keywords. Exclude classes, ticket-buying reminders, films, and general outings. Withhold uncertain invitations or unclear attendance markers.
- **Exclude missed and canceled occurrences before matching.** Recognize explicit attendance markers in titles, notes, or status, including `missed` (case-insensitive), `未去`, and `未参加`, before stripping title suffixes. Check mirrored copies too; an unmarked copy must not reintroduce a missed occurrence. If it already exists in Notion, report the conflict and link without altering the row. Exclude it from source-based eligible-performance counts and distinguish those from raw Notion row counts; do not claim attendance is verified.
- Match by normalized title/aliases plus local performance date; use venue and time to disambiguate. Normalize punctuation, markdown emphasis, ticket prefixes, and Chinese/English aliases for comparison only. Preserve real repeat visits on different dates. If differing dates could represent rescheduling rather than separate visits, report both source values and withhold the extra row. Deduplicate mirrored copies by UID and occurrence, then corroborate equivalent copies by title/date/venue, checking missed markers across copies.
- The script supplies UTC instants and Mac-local dates, not the original event timezone. For travel events use a source-supported venue timezone before deriving the Notion date. Do not slice UTC timestamps into dates. All-day end dates are exclusive. Follow the destination's date-only convention where present.
- Recurrence rules and exceptions do not guarantee expanded occurrences. A date-filtered export can omit an older recurring master whose later occurrences fall in the window. Do not treat an empty result or `scannedEventObjects` as proof of complete occurrence coverage. Resolve relevant recurring performances through an occurrence-aware read or Calendar UI; otherwise disclose the recurrence limitation and withhold unresolved occurrences.
- For `Theater`, use the Calendar event's nonempty `location` directly, preserving its wording even when it is an address or a Chinese venue name. Do not replace it with a web-derived or normalized venue name. If the location is ambiguous or conflicts with another source, list the issue for user confirmation instead of silently correcting it.
- If `location` is empty or whitespace-only, search for that performance on its local date using its title and city/tour clues. Prefer date-specific organizer, venue, or official ticket sources; a production's current venue does not establish its historic venue. Automatically populate `Theater` in the new row when reliably verified, and recommend that the user manually add the location to Calendar, with source links. Conflicting or insufficient evidence requires confirmation; withhold that candidate.
- Derive other new-record fields from calendar evidence first and use primary sources if the category requires verification. Do not guess missing information, silently create an incomplete record to avoid ambiguity, or classify every Broadway production as a musical; list unresolved fields for confirmation.
- If a new record requires a city/category option or property absent from the existing schema, withhold it and explain the required user action. Never add options, substitute an unsupported value, or silently omit the field.

## Create and verify

1. Prepare proposed rows with source occurrences, field values, and evidence; separate exclusions and unresolved candidates.
2. Re-read destination rows and schema immediately before writing. Remove candidates already represented or involved in unresolved matches. Check every property and select value against the live schema and read the current create-tool contract.
3. Create confident rows under the discovered `data_source_id`, using only supported properties. Do not use update, delete, move, archive, or template-application tools.
4. Save each result. After a timeout or ambiguous response, query the relevant title/date before retrying; never blindly repeat a create. If a row exists but has incorrect values, report it rather than correcting it. Stop affected creates on unresolved identity or schema errors while completing independent clear additions.
5. Read back each created row and compare all expected fields. Compare existing rows and schema with the snapshot; report unexpected differences without restoring them or assuming who made them. Rerun matching without writing to ensure added occurrences produce no further candidates; totals alone are insufficient.

For unattended runs, complete independent clear additions and report withheld items without waiting for a reply. A later run must not treat silence as confirmation. Load the current skill and config each time; the schedule belongs in the automation, not in this skill or its config.

## Report after every update

Use these two numbered sections even when nothing was added or access was blocked. Briefly state the resolved date range, calendars actually scanned, any recurrence limitation, additions-only Notion writes, and Calendar's read-only status. Put unambiguous missed exclusions and future events encountered within the scan in a short informational note outside the confirmation list.

**1. 已更新条目** — Give the added count and list each successfully created and verified record with date, title, category, city, venue, and Notion link. If none, write `无`. For researched locations included in these rows, add source links and recommend manual Calendar updates; these verified additions do not require confirmation.

**2. 需要用户确认的地方** — List conflicts, unclear candidates, missing fields/options, or unresolved access/verification problems, with the precise decision or action needed and Calendar/Notion values and links where applicable. Distinguish withheld rows, unverified create results, and rows created with incorrect fields; never describe them as successfully completed. If none, write `无`.
