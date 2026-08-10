---
name: compare-dida365-apple-notes-events
description: Compare dated personal activities in Dida365 (TickTick) with the Apple Notes note named 《活动安排》 and report schedule discrepancies or missing entries. Use when the user asks to reconcile, audit, or cross-check these two schedules. This skill is strictly read-only and must only suggest changes, never edit either source.
---

# Compare Dida365 and Apple Notes Activities

1. Read 《活动安排》 from Apple Notes with Computer Use. Do not type, select, format, or modify anything.
2. Read Dida365 with its MCP tools. Prefer `filter_tasks` for the relevant date range and user timezone. Never call create, update, complete, move, comment, or delete tools.
3. Keep leisure and major personal activities such as travel, concerts, museums, races, courses, and outings. Exclude work blocks, routines, chores, reminders, and preparation tasks unless the Notes schedule includes that category.
4. Match equivalent or compound activity names fuzzily. For all-day Dida365 ranges, treat a midnight `dueDate` as an exclusive endpoint: the displayed final day is the preceding calendar day.
5. Report only:
   - activities present in both sources whose dates or times differ;
   - relevant dated Dida365 activities missing from 《活动安排》;
   - uncertain matches that require confirmation.

For each item, show the Notes value, Dida365 value, and a concise proposed change. State the comparison range and timezone. Do not make changes, offer to apply them, or invoke any write-capable tool.
