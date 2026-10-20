---
name: compare-maps-notion-lists
description: Audit Google Maps and Notion travel lists for missing places, duplicates, names, counts, hierarchy, and country-summary consistency. Default to Notion Source links; offer note-preserving grouping suggestions. Suggest fixes to existing pages and create missing pages only with user authorization.
---

# Compare travel lists

Keep Google Maps and existing Notion pages unchanged. Create missing pages only when authorized. Leave map generation and publishing to `$visualize-travel-map`.

## Sources

- Prefer the user's explicit Maps list; otherwise use each relevant Notion record's `Source` property, matching its actual schema name (including `source`). Skip Maps when the user requests a Notion-only audit.
- Read a shared list once and compare it against the combined in-scope records. For a region within a broader list, compare only its supported geographic subset and disclose the scope; ask if the mapping is uncertain.
- If a needed Source link is missing, unusable, or inaccessible, identify the affected Notion page and ask for a link. Do not silently reuse an old conversation link or claim a complete comparison.

## Audit

1. Read every relevant Notion row/page and every Maps card, including notes and duplicates. Use original travel records as evidence, not just country summaries. Disclose any unreadable or incomplete source.
2. Capture declared totals, actual top-level items, and expanded places from notes/sub-lists separately. Check counts against the record's counting convention; do not treat grouped children as missing or duplicate places merely because they are nested.
3. Compare membership as multisets, ignoring harmless formatting only. Flag missing/extra items, duplicates, stale counts, spelling, aliases, renamed places, and noncanonical labels. Keep uncertain matches separate; never silently normalize names.
4. Compare parent/child relationships separately. Report every hierarchy mismatch as an actionable finding even when expanded coverage matches, and suggest which items to move or group.
5. When a country summary is supplied, check unique, correct `Country`/`ISO3` rows, each source record's single correct country relation, and the summarized `City` total against audited records. For opaque `formulaResult://` values, read the rendered table; if unavailable, report that check as unverified.

## Missing pages

- Search the intended database and summary, including name variants. If creation is not authorized, describe the proposed destination/content and ask whether to create it; a direct creation request already grants authorization.
- Inspect the schema and comparable pages from other countries. Match their province/state/region pages → city/destination headings → attraction lists; keep the country as their shared summary relation.
- Create only the missing records in the authorized scope, preserving names, notes, and individual attractions. Set `Source` when available, leave unknown dates/counts unset, and create a missing country-summary row only if needed for the new records. Existing pages remain unchanged apart from automatic reciprocal relations/rollups.
- Fetch new pages to verify content, counts, and relations; report their links and distinguish the initial audit findings from the post-creation state.

## Grouping suggestions

- Suggest a common park, complex, or site only with containment evidence from the records or an authoritative source; proximity alone is insufficient. Distinguish confirmed groupings from uncertain candidates.
- Give each parent its own copy-ready block. Preserve original attraction names and notes verbatim, including links and multiline notes; keep differing Maps/Notion notes separately labeled and retain any parent notes. If notes are inaccessible, mark the suggestion incomplete.

```text
d:

1. a: 原本备注
2. b
3. c
```

- Put the rationale, evidence, and clickable Notion source/destination links outside the block. Let the user decide whether to merge; never apply suggested groupings or adjust totals automatically.

## Report

- Lead with scope, Maps header/unique-card counts, Notion counts, and whether membership, hierarchy, names, and notes match. State the list links used and whether the user or `Source` supplied them.
- List findings as `Type | Maps | Notion | Suggestion | Edit in Notion` (`—` for a Notion-only audit). Separate real differences, harmless variants, and ambiguities.
- Link each finding to its exact Notion row/page; for a Maps-only item, link the best destination or supplied database if placement is uncertain. For summary mismatches, link both the country row and the relevant source record.
- Offer one optional handoff: `对比已完成。要不要现在运行 $visualize-travel-map 更新旅行地图？` If fixes remain, phrase the offer as running after the user applies them. Run visualization only when the user requests it.
