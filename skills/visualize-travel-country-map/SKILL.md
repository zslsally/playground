---
name: visualize-travel-country-map
description: Generate and optionally publish a Datawrapper country choropleth from an audited, read-only Notion country-summary database, coloring countries by the summarized City count. Use to create, refresh, or publish the map from Country, ISO3, and City fields. Do not compare Maps, audit names or counts, follow source relations, or edit Notion.
---

# Publish Travel Country Map

Turn audited country-summary rows into a country map colored by visited-city count.

## Boundaries

- Treat the supplied country-summary database as the sole Notion source. Read only `Country`, `ISO3`, and `City`.
- Never open Google Maps, page bodies, or source relations, and never verify names against other records or external sources.
- Perform only structural input checks. On missing values, duplicate keys, or invalid types, stop and recommend `$compare-maps-notion-lists`; never repair data.
- Keep personal URLs, generated data, chart IDs, and tokens out of the repository.
- Never use Google Sheets, create a new visualization, or expose credentials.

## Workflow

1. Resolve the supplied Notion country-summary database or view and read its visible summary rows.
2. Require `Country` as the title, `ISO3` as text, and `City` as a rendered nonnegative integer. If the connector returns an opaque `formulaResult://` value for `City`, read the rendered table through the existing signed-in Notion browser; do not expand its relations.
3. Require unique countries and ISO3 codes, with ISO3 exactly three uppercase letters. Stop on any invalid row.
4. Write a temporary UTF-8 CSV sorted by `iso3` with exactly:

```csv
iso3,country,visited,cities_visited
CHN,China,1,53
USA,United States,1,136
```

5. Re-read the CSV and verify its structure and totals. For generation or preview, stop and report its path.
6. For refresh or publish, update only the existing map:

```bash
python3 scripts/publish_datawrapper.py --chart-id MAP_ID /absolute/path/countries.csv
```

The script validates before uploading, reads the token from `DATAWRAPPER_API_TOKEN` or macOS Keychain service `codex-datawrapper-api-token`, updates only the title and description, and republishes the same map. It deletes the CSV only after all publish steps succeed; dry-runs and failures retain it.

## Datawrapper

- Match by `iso3`, color by `cities_visited`, and show `country` plus `cities_visited` in the tooltip.
- Set the visible title to `我的旅行地图` and the description to `N 个国家 · M 个城市/目的地` on every publish. Preserve all other map metadata and styling.
- If the basemap lacks a valid microstate such as `VAT`, keep the CSV correct and report the limitation.

## Report

Report the summary database, totals, temporary CSV path and whether it was deleted, chart ID, title, summary, and publication result. State that Notion was not modified and no source relations were opened.
