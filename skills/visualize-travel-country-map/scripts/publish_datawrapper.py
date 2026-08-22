#!/usr/bin/env python3
"""Validate and publish a city-count country CSV, deleting it only on success."""

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


API_BASE = "https://api.datawrapper.de/v3"
KEYCHAIN_SERVICE = "codex-datawrapper-api-token"
MAP_TITLE = "我的旅行地图"
EXPECTED_HEADER = ["iso3", "country", "visited", "cities_visited"]


class PublishError(RuntimeError):
    """A safe, user-facing publication failure."""


def validate_chart_id(value: str) -> str:
    chart_id = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{5}", chart_id):
        raise PublishError("Datawrapper map ID must be exactly five letters or digits.")
    return chart_id


def validate_csv(path: Path) -> tuple[bytes, int, int]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except FileNotFoundError as exc:
        raise PublishError(f"CSV not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise PublishError("CSV must be UTF-8 without invalid bytes.") from exc

    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or rows[0] != EXPECTED_HEADER:
        raise PublishError("CSV header must be exactly: iso3,country,visited,cities_visited")
    if len(rows) == 1:
        raise PublishError("CSV contains no country rows.")

    previous = ""
    seen: set[str] = set()
    city_total = 0
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != 4:
            raise PublishError(f"CSV row {line_number} must contain exactly four columns.")
        iso3, country, visited, cities_visited = row
        if not re.fullmatch(r"[A-Z]{3}", iso3):
            raise PublishError(f"CSV row {line_number} has an invalid ISO3 code.")
        if not country.strip():
            raise PublishError(f"CSV row {line_number} has an empty country label.")
        if country != country.strip():
            raise PublishError(f"CSV row {line_number} has surrounding country whitespace.")
        if visited != "1":
            raise PublishError(f"CSV row {line_number} must have visited=1.")
        if not re.fullmatch(r"0|[1-9][0-9]*", cities_visited):
            raise PublishError(f"CSV row {line_number} has an invalid cities_visited count.")
        if iso3 in seen:
            raise PublishError(f"CSV contains duplicate ISO3 code: {iso3}")
        if previous and iso3 < previous:
            raise PublishError("CSV rows must be sorted by ISO3 code.")
        seen.add(iso3)
        previous = iso3
        city_total += int(cities_visited)

    return raw, len(rows) - 1, city_total


def load_token() -> str:
    token = os.environ.get("DATAWRAPPER_API_TOKEN", "").strip()
    if token:
        return token

    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None

    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise PublishError(
        "Missing Datawrapper token. Set DATAWRAPPER_API_TOKEN or store it in "
        f"macOS Keychain service {KEYCHAIN_SERVICE}."
    )


def api_request(
    method: str,
    url: str,
    token: str,
    data: bytes | None = None,
    content_type: str | None = None,
) -> None:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise PublishError(f"Datawrapper returned HTTP {exc.code}{suffix}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"Could not reach Datawrapper: {exc.reason}") from exc


def publish_chart(
    chart_id: str,
    csv_bytes: bytes,
    token: str,
    summary: str,
) -> None:
    chart_url = f"{API_BASE}/charts/{chart_id}"
    metadata = json.dumps(
        {
            "title": MAP_TITLE,
            "metadata": {"describe": {"intro": summary, "hide-title": False}},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    api_request("PUT", f"{chart_url}/data", token, csv_bytes, "text/csv")
    api_request("PATCH", chart_url, token, metadata, "application/json")
    api_request("POST", f"{chart_url}/publish", token)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate, label, summarize, upload, and publish a city-count country CSV."
    )
    parser.add_argument("csv_path", type=Path, help="UTF-8 country CSV to publish")
    parser.add_argument("--chart-id", required=True, help="Existing five-character map ID")
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate without reading credentials or using the network"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        chart_id = validate_chart_id(args.chart_id)
        csv_bytes, country_count, city_total = validate_csv(args.csv_path)
        summary = f"{country_count} 个国家 · {city_total} 个城市/目的地"
        if args.dry_run:
            print(
                f"Validated {MAP_TITLE} — {summary} for Datawrapper map {chart_id}; "
                f"no upload sent; CSV retained at {args.csv_path}."
            )
            return 0
        publish_chart(chart_id, csv_bytes, load_token(), summary)
        try:
            args.csv_path.unlink()
        except OSError as exc:
            print(f"Published Datawrapper map {chart_id}: {MAP_TITLE} — {summary}")
            print(f"Warning: could not delete CSV; retained at {args.csv_path}: {exc}", file=sys.stderr)
        else:
            print(
                f"Published Datawrapper map {chart_id}: {MAP_TITLE} — {summary}; "
                f"deleted CSV {args.csv_path}."
            )
        return 0
    except PublishError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if args.csv_path.is_file():
            print(f"CSV retained at {args.csv_path}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
