---
name: compare-maps-notion-lists
description: Compare a Google Maps saved/shared place-list URL with a Notion page or database URL. Use when the user supplies both links and wants count checks, missing or extra places, duplicate names, renamed places, abbreviations, or other list inconsistencies, with direct Notion edit links for every reported difference.
---

# Compare Google Maps and Notion Place Lists

Perform a read-only, item-by-item comparison. Do not edit either list unless asked.

## Workflow

1. Read Notion through the Notion connector. If the URL is a database/view, query the relevant region/group, then fetch every included state or child page.
2. Record both Notion property totals and the actual numbered place entries. Report stale per-page counts even when errors cancel in the aggregate.
3. Read Google Maps through the browser workflow when no Maps connector exists. Record the header count, scroll until all place cards are loaded, and preserve duplicate cards.
4. Compare as multisets, not sets. Two identical displayed names may represent different cities or addresses.
5. Normalize conservatively: case, punctuation, apostrophes, accents, and obvious notes. Use city/state/address and surrounding context to recognize abbreviations, renames, and `parent area: child place` forms. Do not silently merge uncertain matches.
6. Classify findings as:
   - only in Notion;
   - only in Maps;
   - same place, different name or abbreviation;
   - duplicate or ambiguous name;
   - Notion count inconsistent with its body.

## Output

Lead with both raw counts and whether the memberships truly match. Then provide a compact table with `Type`, `Google Maps`, `Notion`, `Suggested change`, and `Edit in Notion`.

Every discrepancy must include a clickable Notion link:

- For Notion-only or naming differences, link the exact source page/row.
- For Maps-only places, link the most relevant state/city Notion page where the place should be added.
- If no destination page can be determined, link the supplied Notion database/view and say that placement is uncertain.

Mention harmless naming variants separately from true membership differences. Also state duplicate-card counts when Google Maps' total differs from its unique displayed-name count.
