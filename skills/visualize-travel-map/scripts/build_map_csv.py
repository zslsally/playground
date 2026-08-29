#!/usr/bin/env python3
"""Convert read-only Notion query results into validated travel-map CSVs."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Any


MAPS = {
    "world": (("ISO3", "Country"), ["iso3", "country", "visited", "cities_visited"]),
    "china": (("Key", "Region"), ["id", "region", "visited", "cities_visited"]),
    "us": (("Key", "State"), ["id", "region", "visited", "cities_visited"]),
}


class BuildError(ValueError):
    pass


def text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not (value := re.sub(r"\*\*(.*?)\*\*", r"\1", value).strip()):
        raise BuildError(f"{field} must be non-empty text.")
    return value


def integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BuildError(f"{field} must be a nonnegative integer.")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value.strip(" *")):
        value = int(value.strip(" *"))
    if not isinstance(value, int) or value < 0:
        raise BuildError(f"{field} must be a nonnegative integer.")
    return value


def results(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        raise BuildError("Input JSON must contain a non-empty results array.")
    return rows


def notion_rows(map_type: str, records: list[dict[str, Any]]) -> list[list[str]]:
    (key_field, label_field), _ = MAPS[map_type]
    output, seen = [], set()
    for index, record in enumerate(records, 1):
        key = text(record.get(key_field), f"row {index} {key_field}")
        label = text(record.get(label_field), f"row {index} {label_field}")
        if map_type == "world" and not re.fullmatch(r"[A-Z]{3}", key):
            raise BuildError(f"row {index} ISO3 must be three uppercase letters.")
        if key in seen:
            raise BuildError(f"duplicate {map_type} map key: {key}")
        raw = record.get("City")
        blank = raw is None or isinstance(raw, str) and not raw.strip()
        if blank and map_type == "world":
            raise BuildError(f"row {index} City must be a nonnegative integer.")
        count = 0 if blank else integer(raw, f"row {index} City")
        output.append([key, label, "0" if blank else "1", str(count)])
        seen.add(key)
    return sorted(output)


def complete_basemap(
    rows: list[list[str]],
    payload: Any,
    key_attr: str,
    label_attr: str,
    prefer_labels: bool,
    require_complete: bool,
    validate_only: bool,
    ignore_keys: list[str],
) -> list[list[str]]:
    try:
        region = payload["meta"]["regions"]
        geometries = payload["content"]["objects"][region]["geometries"]
        basemap = {
            text(item["properties"].get(key_attr), key_attr): text(
                item["properties"].get(label_attr), label_attr
            )
            for item in geometries
        }
    except (KeyError, TypeError) as exc:
        raise BuildError("Unsupported basemap JSON.") from exc

    source = {row[0]: row for row in rows}
    unmatched = source.keys() - basemap.keys()
    unknown = sorted(unmatched - set(ignore_keys))
    missing = sorted(basemap.keys() - source.keys())
    if unknown:
        raise BuildError(f"keys missing from basemap: {', '.join(unknown)}")
    for key in unmatched & set(ignore_keys):
        source.pop(key)
    if require_complete and missing:
        raise BuildError(f"Notion rows missing basemap keys: {', '.join(missing)}")
    if validate_only:
        return sorted(source.values())

    output = []
    for key, label in basemap.items():
        if key in source:
            row = source[key].copy()
            if prefer_labels:
                row[1] = label
        else:
            row = [key, label, "0", "0"]
        output.append(row)
    return sorted(output)


def render(map_type: str, payload: Any, basemap: Any = None, args: Any = None) -> bytes:
    rows = notion_rows(map_type, results(payload))
    if basemap is not None:
        rows = complete_basemap(
            rows,
            basemap,
            args.basemap_key_attr,
            args.basemap_label_attr,
            args.prefer_basemap_labels,
            args.require_complete_source,
            args.validate_basemap_only,
            args.ignore_key,
        )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(MAPS[map_type][1])
    writer.writerows(rows)
    return output.getvalue().encode()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-type", choices=MAPS, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--basemap-json", type=Path)
    parser.add_argument("--basemap-key-attr")
    parser.add_argument("--basemap-label-attr")
    parser.add_argument("--prefer-basemap-labels", action="store_true")
    parser.add_argument("--require-complete-source", action="store_true")
    parser.add_argument("--validate-basemap-only", action="store_true")
    parser.add_argument("--ignore-key", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        options = (args.basemap_json, args.basemap_key_attr, args.basemap_label_attr)
        if any(options) != all(options):
            raise BuildError("basemap JSON, key, and label arguments are required together.")
        if (args.require_complete_source or args.validate_basemap_only) and not args.basemap_json:
            raise BuildError("basemap validation requires --basemap-json.")
        payload = json.loads(args.input.read_text())
        basemap = json.loads(args.basemap_json.read_text()) if args.basemap_json else None
        data = render(args.map_type, payload, basemap, args)
        args.output.write_bytes(data)
        print(f"Built {args.map_type} CSV with {len(data.decode().splitlines()) - 1} rows: {args.output}")
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, BuildError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
