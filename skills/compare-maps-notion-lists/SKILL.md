---
name: compare-maps-notion-lists
description: Audit Google Maps and Notion travel records without editing either source, including country-summary relations and rollups when supplied. Use for missing or extra places, duplicates, stale counts, spelling, aliases, renamed places, canonical labels, and summary consistency. Link every finding to Notion. Do not generate map data or publish visualizations.
---

# Audit Travel Lists

Limit this skill to source correctness. Leave map generation and publishing to `$visualize-travel-map`.

## Boundaries

- Read Google Maps and Notion without editing either source; provide suggestions only.
- Audit membership, names, duplicates, and counts. Never create visualization CSVs, inspect Datawrapper, or publish a map.
- Treat original travel records as audit evidence; never use a country summary as a substitute for inspecting the requested source lists.
- Never merge an uncertain match or silently choose a canonical label.

## Workflow

1. Read every relevant Notion row or page. If a Maps URL is present, load every place card, including duplicates.
2. Capture declared totals, actual items, and exact names while preserving duplicates.
3. Ignore harmless formatting for comparison, then compare as multisets. Keep uncertain matches separate.
4. If a country-summary database is supplied, verify after the source audit that each country has one row, `Country` and `ISO3` are correct and unique, source records are related to the correct country exactly once, and rendered `City` equals the audited source total. If a formula is returned as `formulaResult://`, read its rendered table value instead.
5. Classify missing or extra items, duplicates, stale counts, spelling, aliases, renamed places, noncanonical labels, and summary mismatches.

## Report

- Lead with raw counts and whether membership and names match.
- Use `Type | Maps | Notion | Suggestion | Edit in Notion`; use `—` for a Notion-only audit.
- Link every finding to the exact Notion row or page. For Maps-only items, link the best destination page or the supplied database when placement is uncertain.
- For a summary mismatch, link both the affected country-summary row and the source row that explains the difference.
- Separate harmless variants and ambiguities from real differences. When Maps is present, state its header count and unique-card count.
- End with one optional handoff. If the audit is clean, ask: `对比已完成。要不要现在运行 $visualize-travel-map 更新旅行地图？` If fixes remain, ask the same question for after the user applies them. Never run the visualization without confirmation.
