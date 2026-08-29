#!/usr/bin/env python3
"""Validate and publish one existing Datawrapper travel map."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API = "https://api.datawrapper.de/v3"
KEYCHAIN = "codex-datawrapper-api-token"
MAPS = {
    "world": ("旅行地图：世界", "国家", ["iso3", "country", "visited", "cities_visited"]),
    "china": ("旅行地图：中国", "省级地区", ["id", "region", "visited", "cities_visited"]),
    "us": ("旅行地图：美国", "州/地区", ["id", "region", "visited", "cities_visited"]),
}


class PublishError(RuntimeError):
    pass


def validate_csv(path: Path, map_type: str) -> tuple[bytes, int, int, int, int]:
    try:
        raw = path.read_bytes()
        rows = list(csv.reader(io.StringIO(raw.decode())))
    except (OSError, UnicodeError) as exc:
        raise PublishError(f"Cannot read UTF-8 CSV: {path}") from exc

    header = MAPS[map_type][2]
    if not rows or rows[0] != header or len(rows) == 1:
        raise PublishError(f"CSV must start with {','.join(header)} and contain data.")

    seen, previous = set(), ""
    visited_count = city_total = max_count = blank_unvisited = 0
    for line, row in enumerate(rows[1:], 2):
        if len(row) != 4:
            raise PublishError(f"row {line} must have four columns.")
        key, label, visited, count = row
        if map_type == "world" and not re.fullmatch(r"[A-Z]{3}", key):
            raise PublishError(f"row {line} has invalid ISO3.")
        if map_type != "world" and re.fullmatch(r"(?:CN-[0-9]{2}|US-[A-Z]{2})", key):
            raise PublishError(f"row {line} must use the exact basemap key.")
        if not label or label != label.strip() or key in seen or previous and key < previous:
            raise PublishError(f"row {line} has an invalid label, duplicate key, or sort order.")
        if visited not in {"0", "1"} or count and not count.isdigit():
            raise PublishError(f"row {line} has invalid visited/count values.")
        if visited == "1" and not count or visited == "0" and count not in {"", "0"}:
            raise PublishError(f"row {line} has inconsistent visited/count values.")
        number = int(count or 0)
        visited_count += visited == "1"
        city_total += number
        max_count = max(max_count, number)
        blank_unvisited += visited == "0" and not count
        seen.add(key)
        previous = key
    if not visited_count:
        raise PublishError("CSV has no visited regions.")
    return raw, visited_count, city_total, max_count, blank_unvisited


def token() -> str:
    if value := os.environ.get("DATAWRAPPER_API_TOKEN", "").strip():
        return value
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise PublishError(f"Missing token in DATAWRAPPER_API_TOKEN or Keychain service {KEYCHAIN}.")


def request(
    method: str,
    url: str,
    auth: str,
    data: bytes | None = None,
    content_type: str | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {auth}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers, method=method), timeout=30
        ) as response:
            body = response.read()
            return json.loads(body) if body and "json" in response.headers.get_content_type() else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise PublishError(f"Datawrapper HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"Could not reach Datawrapper: {exc.reason}") from exc


def zero_grey_colorscale(chart: dict[str, Any], maximum: int) -> dict[str, Any]:
    try:
        current = chart["metadata"]["visualize"]["colorscale"]
        palette = [
            {"color": stop["color"], "position": float(stop["position"])}
            for stop in current["colors"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise PublishError("Chart has no reusable continuous colorscale.") from exc
    if len(palette) < 2:
        raise PublishError("Chart has no reusable continuous colorscale.")

    if len(palette) >= 4 and [stop["color"] for stop in palette[:2]] == ["#ededed"] * 2:
        old = palette[2]["position"]
        if 0 < old < 1:
            palette = [
                {"color": stop["color"], "position": (stop["position"] - old) / (1 - old)}
                for stop in palette[2:]
            ]

    maximum = max(1, maximum)
    edge = 1 / maximum
    colors = [
        {"color": "#ededed", "position": 0},
        {"color": "#ededed", "position": edge - min(0.000001, edge / 1000)},
        *[
            {"color": stop["color"], "position": edge + stop["position"] * (1 - edge)}
            for stop in palette
        ],
    ]
    return {**current, "mode": "continuous", "rangeMin": 0, "rangeMax": maximum, "colors": colors}


def publish(
    chart_id: str,
    csv_data: bytes,
    auth: str,
    map_type: str,
    basemap: str | None,
    map_key: str | None,
    zero_is_grey: bool,
    maximum: int,
) -> None:
    title, _, header = MAPS[map_type]
    key_column, label_column = header[:2]
    chart_url = f"{API}/charts/{chart_id}"
    visualize: dict[str, Any] = {
        "hide-empty-regions": False,
        "tooltip": {
            "enabled": True,
            "sticky": True,
            "title": f"{{{{ {label_column} }}}}",
            "body": "{{ visited == 1 ? cities_visited : '' }}",
            "fields": {label_column: label_column, "visited": "visited", "cities_visited": "cities_visited"},
        },
    }
    if zero_is_grey:
        visualize["colorscale"] = zero_grey_colorscale(request("GET", chart_url, auth), maximum)
    if basemap:
        visualize.update({"basemap": basemap, "map-key-attr": map_key})

    metadata = json.dumps(
        {
            "title": title,
            "metadata": {
                "axes": {"keys": key_column, "values": "cities_visited"},
                "describe": {"intro": "", "hide-title": False},
                "visualize": visualize,
            },
        },
        ensure_ascii=False,
    ).encode()
    request("PUT", f"{chart_url}/data", auth, csv_data, "text/csv")
    request("PATCH", chart_url, auth, metadata, "application/json")
    request("POST", f"{chart_url}/publish", auth)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--chart-id", required=True)
    parser.add_argument("--map-type", choices=MAPS, default="world")
    parser.add_argument("--basemap")
    parser.add_argument("--map-key-attr")
    parser.add_argument("--zero-is-grey", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if bool(args.basemap) != bool(args.map_key_attr):
            raise PublishError("--basemap and --map-key-attr are required together.")
        if args.map_type != "world" and not args.dry_run and not args.basemap:
            raise PublishError("Regional publication requires its verified basemap and key.")
        if not re.fullmatch(r"[A-Za-z0-9]{5}", args.chart_id):
            raise PublishError("Chart ID must be five letters or digits.")
        data, visited, cities, maximum, blanks = validate_csv(args.csv_path, args.map_type)
        if args.zero_is_grey and blanks:
            raise PublishError("--zero-is-grey requires cities_visited=0 for unvisited rows.")
        title, unit, _ = MAPS[args.map_type]
        summary = f"{visited} 个{unit} · {cities} 个城市/目的地"
        if args.dry_run:
            print(f"Validated {title} — {summary}; retained {args.csv_path}")
            return 0
        publish(
            args.chart_id,
            data,
            token(),
            args.map_type,
            args.basemap,
            args.map_key_attr,
            args.zero_is_grey,
            maximum,
        )
        try:
            args.csv_path.unlink()
        except OSError as exc:
            print(f"Published {title} — {summary}")
            print(f"Warning: could not delete CSV; retained at {args.csv_path}: {exc}", file=sys.stderr)
        else:
            print(f"Published {title} — {summary}; deleted {args.csv_path}")
        return 0
    except PublishError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.csv_path.is_file():
            print(f"CSV retained at {args.csv_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
