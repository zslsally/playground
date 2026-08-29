---
name: visualize-travel-map
description: "Refresh three existing Datawrapper travel maps from read-only Notion summaries."
---

# Travel maps

Read the three configured Notion summaries and refresh the three existing Datawrapper maps:

| map | Notion fields | CSV |
| --- | --- | --- |
| `world` | `Country`, `ISO3`, `City` | `iso3,country,visited,cities_visited` |
| `china` | `Region`, `Key`, `City` | `id=Key,region=Region,visited,cities_visited` |
| `us` | `State`, `Key`, `City` | `id=Key,region=State,visited,cities_visited` |

Use only the listed fields and the shortest reliable read path:

- `world`: skip Notion SQL and row fetches. Open the configured `page_url` once in a signed-in browser, load the complete table, and read only its rendered `Country`, `ISO3`, and `City` columns. Verify the extracted row count and `City` sum against the table footer. Never dereference `formulaResult://` values.
- `china` and `us`: query their configured data sources with SQL that selects only the listed fields.

Never open Maps, relations, unrelated page content, or edit Notion. Validate every source key against its current Datawrapper basemap. Omit unmatched keys listed in that chart's configured `ignored_keys`; stop before publishing and list every other unmatched key. Reject empty/duplicate keys and invalid counts. Blank regional `City` means unvisited and becomes `visited=0,cities_visited=0`.

China must contain every `china-taiwan-provinces/name` key so its Notion `Region` supplies all Chinese labels. Complete missing US rows from `us-states-territories/STATE`, using basemap `NAME` labels. Numeric zero keeps grey regions hoverable.

## Workflow

1. Read sources and chart IDs from ignored `config.local.json`, using the routes above.
2. Use `build_map_csv.py` to create validated UTF-8 CSVs under `/private/tmp`; verify all three with `publish_datawrapper.py --dry-run`.
3. Only after explicit confirmation, update the existing charts. Keep the titles `旅行地图：世界`, `旅行地图：中国`, and `旅行地图：美国`; clear `describe.intro`; show only the numeric city count below each tooltip label. Never create charts.

The publisher reads `DATAWRAPPER_API_TOKEN` or Keychain service `codex-datawrapper-api-token`. It deletes each CSV only after upload, metadata update, and publish all succeed; dry-runs and failures retain it.
