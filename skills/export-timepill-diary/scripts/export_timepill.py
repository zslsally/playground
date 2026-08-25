#!/usr/bin/env python3
"""Read-only Timepill backup with interruption-safe append mode."""

import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


VERSION = "0.9.0"
BASE_URL = "https://open.timepill.net/api"
KEYCHAIN_SERVICE = "codex-timepill-export"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.local.json"
CONFIG_KEYS = {
    "account",
    "output",
    "page_size",
    "api_delay_seconds",
    "request_timeout_seconds",
    "retries",
    "export_comments",
    "export_media",
    "media_workers",
    "max_media_bytes",
    "append_overlap_days",
}


def load_config(path: Path) -> Dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"Missing {path}; copy config.example.json to config.local.json"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    missing = CONFIG_KEYS - set(config)
    unknown = set(config) - CONFIG_KEYS
    if missing or unknown:
        raise ValueError(
            f"Invalid config keys; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    text = lambda value: isinstance(value, str) and bool(value.strip())
    boolean = lambda value: isinstance(value, bool)
    positive_int = lambda value: isinstance(value, int) and not isinstance(value, bool) and value > 0
    nonnegative_int = lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0
    positive_number = lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    )
    rules = (
        (("account", "output"), text, "a non-empty string"),
        (("export_comments", "export_media"), boolean, "true or false"),
        (("page_size", "media_workers", "max_media_bytes"), positive_int, "a positive integer"),
        (("retries", "append_overlap_days"), nonnegative_int, "a non-negative integer"),
        (("api_delay_seconds", "request_timeout_seconds"), positive_number, "a positive number"),
    )
    for keys, valid, description in rules:
        for key in keys:
            if not valid(config[key]):
                raise ValueError(f"Config {key} must be {description}")
    return config


CONFIG = load_config(CONFIG_PATH)
FILES = {
    "profile": "profile.json",
    "notebooks": "notebooks.json",
    "diaries": "diaries.jsonl",
    "threads": "comment_threads.jsonl",
    "media": "media.jsonl",
    "errors": "errors.jsonl",
    "manifest": "manifest.json",
}
SHARED_FILE_KEYS = {"profile", "notebooks"}
IMAGE_KEY_PARTS = ("photo", "image", "cover", "icon", "avatar")
IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/svg+xml": ".svg",
}


class ApiError(RuntimeError):
    pass


class NoRedirectHandler(HTTPRedirectHandler):
    """Keep API credentials on the configured origin."""

    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def shanghai_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def parse_iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def manifest_covered_through_date(manifest: Dict[str, Any]) -> Optional[date]:
    coverage = manifest.get("coverage")
    if isinstance(coverage, dict) and coverage.get("through_date") is not None:
        parsed = parse_iso_date(coverage["through_date"])
        if parsed is None:
            raise ValueError("Existing manifest has an invalid coverage through_date")
        return parsed

    if not manifest.get("complete"):
        return None

    options = manifest.get("options")
    if isinstance(options, dict):
        # Read checkpoints written before the rescan/append rename.
        for key in ("append_end_date", "sync_end_date", "sync_date"):
            parsed = parse_iso_date(options.get(key))
            if parsed is not None:
                return parsed

    finished_at = manifest.get("finished_at")
    if isinstance(finished_at, str):
        try:
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            return finished.astimezone(ZoneInfo("Asia/Shanghai")).date()
        except ValueError:
            pass
    return None


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(compact_json(value).encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def append_jsonl(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(compact_json(value) + "\n")


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"Warning: ignoring malformed {path.name}:{line_number}: {exc}", file=sys.stderr)
                continue
            if isinstance(value, dict):
                yield value


def load_keys(path: Path, key: str) -> Set[str]:
    result: Set[str] = set()
    for record in iter_jsonl(path):
        metadata = record.get("_export")
        if isinstance(metadata, dict) and metadata.get(key) is not None:
            result.add(str(metadata[key]))
    return result


def load_thread_hashes(path: Path) -> Dict[str, Set[str]]:
    result: Dict[str, Set[str]] = {}
    for record in iter_jsonl(path):
        metadata = record.get("_export")
        if not isinstance(metadata, dict) or metadata.get("diary_id") is None:
            continue
        diary_id = str(metadata["diary_id"])
        digest = metadata.get("payload_hash")
        if digest is None:
            digest = payload_hash(record.get("response"))
        result.setdefault(diary_id, set()).add(str(digest))
    return result


def count_jsonl(path: Path) -> int:
    return sum(1 for _ in iter_jsonl(path))


def latest_comment_count(path: Path) -> int:
    latest: Dict[str, Any] = {}
    for record in iter_jsonl(path):
        metadata = record.get("_export")
        if isinstance(metadata, dict) and metadata.get("diary_id") is not None:
            latest[str(metadata["diary_id"])] = record.get("response")
    count = 0
    for response in latest.values():
        try:
            count += len(response_items(response, "comments"))
        except ValueError:
            pass
    return count


def check_profile_identity(path: Path, profile: Any) -> None:
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("profile")
    except (AttributeError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot verify account from existing {path.name}: {exc}") from exc
    previous_id = previous.get("id") if isinstance(previous, dict) else None
    current_id = profile.get("id") if isinstance(profile, dict) else None
    if previous_id is None or current_id is None:
        raise ValueError("Cannot verify the account id for the existing export")
    if str(previous_id) != str(current_id):
        raise ValueError("Authenticated account does not match the existing export")


def write_account_metadata(
    root: Path,
    profile: Any,
    notebook_response: Any,
    notebook_count: int,
) -> None:
    atomic_json(
        root / FILES["profile"],
        {"_export": {"fetched_at": utc_now()}, "profile": profile},
    )
    atomic_json(
        root / FILES["notebooks"],
        {
            "_export": {"fetched_at": utc_now(), "count": notebook_count},
            "response": notebook_response,
        },
    )


def safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def response_items(payload: Any, label: str) -> List[Dict[str, Any]]:
    candidate = payload
    if isinstance(payload, dict):
        for key in ("items", label, "data"):
            if isinstance(payload.get(key), list):
                candidate = payload[key]
                break
    if not isinstance(candidate, list):
        raise ValueError(f"Expected a list for {label}, got {type(payload).__name__}")
    if not all(isinstance(item, dict) for item in candidate):
        raise ValueError(f"Expected objects in {label} response")
    return candidate


def normalize_notebook_selector(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"(\d{4})年(?:胶囊日记|日记)?", value)
    return match.group(1) if match else value


def select_notebook(notebooks: List[Dict[str, Any]], raw_selector: str) -> Dict[str, Any]:
    selector = normalize_notebook_selector(raw_selector)
    matches = [
        item
        for item in notebooks
        if str(item.get("id", "")) == selector
        or str(item.get("subject", "")).strip().casefold() == selector.casefold()
    ]
    if not matches:
        available = ", ".join(str(item.get("subject") or item.get("id")) for item in notebooks)
        raise ValueError(f"Notebook not found: {raw_selector}. Available: {available}")
    if len(matches) > 1:
        ids = ", ".join(str(item.get("id")) for item in matches)
        raise ValueError(f"Notebook name is ambiguous: {raw_selector}; use one of these ids: {ids}")
    if matches[0].get("id") is None:
        raise ValueError(f"Selected notebook has no id: {raw_selector}")
    return matches[0]


def safe_path_component(value: Any) -> str:
    component = re.sub(r"[\x00-\x1f/:\\]+", "_", str(value)).strip(" .")
    return (component or "notebook")[:80]


def notebook_output(root: Path, notebook: Dict[str, Any]) -> Path:
    notebook_id = str(notebook["id"])
    id_component = safe_path_component(notebook_id)
    parent = prepare_directory(root / "notebooks")
    suffix = f"--{id_component}"
    existing = [entry for entry in parent.iterdir() if entry.is_dir() and entry.name.endswith(suffix)]
    if len(existing) > 1:
        raise ValueError(f"Multiple output directories found for notebook id {notebook_id}")
    if existing:
        return existing[0]
    subject = safe_path_component(notebook.get("subject") or notebook_id)
    return parent / f"{subject}{suffix}"


def find_comment_count(item: Dict[str, Any]) -> Optional[int]:
    for key in ("comment_count", "comments_count"):
        if key in item:
            return safe_int(item[key])
    return None


def diary_date(item: Dict[str, Any]) -> Optional[str]:
    created = item.get("created")
    if isinstance(created, str) and len(created) >= 10:
        return created[:10]
    return None


def media_urls(
    value: Any,
    path: str = "$",
    image_context: bool = False,
) -> Iterable[Dict[str, str]]:
    if isinstance(value, str):
        parsed = urlparse(value)
        if image_context and parsed.scheme in ("http", "https") and parsed.netloc:
            yield {"url": value, "field": path}
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            child_context = image_context or any(
                part in str(key).lower() for part in IMAGE_KEY_PARTS
            )
            yield from media_urls(child, child_path, child_context)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from media_urls(child, f"{path}[{index}]", image_context)


def media_extension(url: str, content_type: str) -> str:
    if content_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[content_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".svg"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".img"


class TimepillClient:
    def __init__(self, account: str, password: str) -> None:
        credentials = base64.b64encode(f"{account}:{password}".encode("utf-8")).decode("ascii")
        self.authorization = "Basic " + credentials
        self.base_url = BASE_URL
        self.request_count = 0
        self.media_request_count = 0
        self.media_request_lock = threading.Lock()
        self.api_opener = build_opener(NoRedirectHandler())
        self.media_opener = build_opener()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(params)

        for attempt in range(CONFIG["retries"] + 1):
            request = Request(
                url,
                method="GET",
                headers={
                    "Authorization": self.authorization,
                    "Accept": "application/json",
                    "Source": "TimepillBackup",
                    "User-Agent": f"Codex-Timepill-Export/{VERSION}",
                },
            )
            self.request_count += 1
            try:
                with self.api_opener.open(
                    request,
                    timeout=CONFIG["request_timeout_seconds"],
                ) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                time.sleep(CONFIG["api_delay_seconds"])
                return json.loads(raw.decode(charset))
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
                if exc.code == 401:
                    raise ApiError("Timepill authentication failed (HTTP 401)") from exc
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt >= CONFIG["retries"]:
                    raise ApiError(f"HTTP {exc.code} for {url}: {body}") from exc
                retry_after = safe_int(exc.headers.get("Retry-After"))
                time.sleep(float(retry_after if retry_after is not None else 2**attempt))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt >= CONFIG["retries"]:
                    raise ApiError(f"GET {url} failed: {exc}") from exc
                time.sleep(float(2**attempt))
        raise ApiError(f"GET {url} failed")

    def download_image(self, url: str, directory: Path) -> Dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApiError(f"Unsupported media URL: {url}")

        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        temporary = directory / f".{url_digest}.part"
        for attempt in range(CONFIG["retries"] + 1):
            with self.media_request_lock:
                self.media_request_count += 1
            request = Request(
                url,
                method="GET",
                headers={"Accept": "image/*", "User-Agent": f"Codex-Timepill-Export/{VERSION}"},
            )
            try:
                with self.media_opener.open(
                    request,
                    timeout=CONFIG["request_timeout_seconds"],
                ) as response:
                    content_type = response.headers.get_content_type().lower()
                    if not content_type.startswith("image/"):
                        raise ApiError(f"Media URL returned {content_type}, not an image: {url}")
                    content_length = safe_int(response.headers.get("Content-Length"))
                    if content_length is not None and content_length > CONFIG["max_media_bytes"]:
                        raise ApiError(f"Image exceeds configured size limit: {url}")
                    digest = hashlib.sha256()
                    size = 0
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    os.fchmod(descriptor, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > CONFIG["max_media_bytes"]:
                                raise ApiError(f"Image exceeds configured size limit: {url}")
                            digest.update(chunk)
                            handle.write(chunk)
                content_hash = digest.hexdigest()
                destination = directory / f"{content_hash}{media_extension(url, content_type)}"
                if destination.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    temporary.replace(destination)
                destination.chmod(0o600)
                return {
                    "sha256": content_hash,
                    "bytes": size,
                    "content_type": content_type,
                    "path": destination.name,
                }
            except HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt >= CONFIG["retries"]:
                    temporary.unlink(missing_ok=True)
                    raise ApiError(f"HTTP {exc.code} while downloading image: {url}") from exc
            except ApiError:
                temporary.unlink(missing_ok=True)
                raise
            except (URLError, TimeoutError, OSError) as exc:
                temporary.unlink(missing_ok=True)
                if attempt >= CONFIG["retries"]:
                    raise ApiError(f"Image download failed for {url}: {exc}") from exc
            time.sleep(float(2**attempt))
        raise ApiError(f"Image download failed: {url}")


class Exporter:
    def __init__(
        self,
        args: argparse.Namespace,
        client: TimepillClient,
        output: Path,
        account_output: Optional[Path] = None,
        notebook: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.append = args.append
        self.client = client
        self.account_output = account_output or output
        self.notebook = notebook
        self.paths = {
            key: (self.account_output if key in SHARED_FILE_KEYS else output) / filename
            for key, filename in FILES.items()
        }
        self.start_request_count = client.request_count
        self.start_media_request_count = client.media_request_count
        self.started = False
        self.media_executor: Optional[ThreadPoolExecutor] = None
        self.pending_media: Dict[Future[Dict[str, Any]], Dict[str, Any]] = {}
        self.media_dir = output / "media"
        self.errors_this_run = 0
        self.expected_thread_ids: Set[str] = set()

        self.diary_keys = load_keys(self.paths["diaries"], "record_key")
        self.thread_ids = load_keys(self.paths["threads"], "diary_id")
        self.media_urls = load_keys(self.paths["media"], "url")
        self.media_attempted_urls: Set[str] = set()
        self.thread_hashes = load_thread_hashes(self.paths["threads"])

        previous: Dict[str, Any] = {}
        if self.paths["manifest"].exists():
            try:
                previous = json.loads(self.paths["manifest"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        today = shanghai_today()
        previous_coverage = manifest_covered_through_date(previous)
        self.target_coverage_date = today.isoformat()
        self.append_end_date: Optional[str] = None
        self.append_start_date: Optional[str] = None
        self.append_refresh_start_date: Optional[str] = None
        if args.append:
            self.append_end_date = today.isoformat()
            overlap = timedelta(days=CONFIG["append_overlap_days"])
            self.append_refresh_start_date = (today - overlap).isoformat()
            if previous_coverage is not None:
                self.append_start_date = (previous_coverage - overlap).isoformat()
            # A legacy interrupted append has no durable checkpoint, so its start
            # stays unset and pagination performs one safe full recovery scan.
        mode = "append" if args.append else "rescan" if args.rescan else "initial"
        options = {
            "mode": mode,
            "notebook_id": notebook.get("id") if notebook else None,
            "notebook_subject": notebook.get("subject") if notebook else None,
            "page_size": CONFIG["page_size"],
            "delay_seconds": CONFIG["api_delay_seconds"],
            "request_timeout_seconds": CONFIG["request_timeout_seconds"],
            "retries": CONFIG["retries"],
            "comments": CONFIG["export_comments"],
            "media": CONFIG["export_media"],
            "media_workers": CONFIG["media_workers"] if CONFIG["export_media"] else 0,
            "max_media_bytes": CONFIG["max_media_bytes"],
        }
        if args.append:
            options.update(
                {
                    "append_overlap_days": CONFIG["append_overlap_days"],
                    "append_from_covered_through_date": (
                        previous_coverage.isoformat() if previous_coverage else None
                    ),
                    "append_start_date": self.append_start_date,
                    "append_end_date": self.append_end_date,
                    "append_comment_refresh_start_date": self.append_refresh_start_date,
                }
            )
        self.manifest: Dict[str, Any] = {
            "format_version": 1,
            "exporter_version": VERSION,
            "source": client.base_url,
            "started_at": previous.get("started_at", utc_now()),
            "run_started_at": utc_now(),
            "finished_at": None,
            "complete": False,
            "coverage": {
                "timezone": "Asia/Shanghai",
                "through_date": previous_coverage.isoformat() if previous_coverage else None,
            },
            "options": options,
            "files": {
                key: os.path.relpath(path, output) for key, path in self.paths.items()
            },
            "media_directory": "media" if CONFIG["export_media"] else None,
            "counts": previous.get("counts", {}),
            "unresolved": previous.get("unresolved", {}),
        }

    def save_manifest(self) -> None:
        self.manifest["api_requests_this_run"] = self.client.request_count - self.start_request_count
        self.manifest["media_requests_this_run"] = (
            self.client.media_request_count - self.start_media_request_count
        )
        atomic_json(self.paths["manifest"], self.manifest)

    def should_refresh_comments(self, item: Dict[str, Any]) -> bool:
        created = diary_date(item)
        return bool(
            self.append
            and created is not None
            and self.append_refresh_start_date is not None
            and self.append_end_date is not None
            and self.append_refresh_start_date <= created <= self.append_end_date
        )

    def error(self, stage: str, exc: Exception, **context: Any) -> None:
        self.errors_this_run += 1
        append_jsonl(
            self.paths["errors"],
            {
                "_export": {"failed_at": utc_now(), "stage": stage, **context},
                "error": {"type": type(exc).__name__, "message": str(exc)},
            },
        )
        print(f"  Warning: {stage} failed: {exc}", file=sys.stderr)

    def export_media(self, payload: Any, source_type: str, source_id: Any) -> None:
        if not CONFIG["export_media"]:
            return
        for media in media_urls(payload):
            self.export_media_url(media["url"], media["field"], source_type, source_id)

    def export_media_url(self, url: str, field: str, source_type: str, source_id: Any) -> None:
        if url in self.media_urls or url in self.media_attempted_urls:
            return
        self.media_attempted_urls.add(url)
        if self.media_executor is None:
            if self.media_dir.exists() and not self.media_dir.is_dir():
                raise ValueError(f"Media path is not a directory: {self.media_dir}")
            self.media_dir.mkdir(mode=0o700, exist_ok=True)
            self.media_dir.chmod(0o700)
            self.media_executor = ThreadPoolExecutor(
                max_workers=CONFIG["media_workers"],
                thread_name_prefix="timepill-media",
            )
        future = self.media_executor.submit(
            self.client.download_image,
            url,
            self.media_dir,
        )
        self.pending_media[future] = {
            "url": url,
            "field": field,
            "source_type": source_type,
            "source_id": source_id,
        }
        if len(self.pending_media) >= CONFIG["media_workers"] * 4:
            completed, _ = wait(tuple(self.pending_media), return_when=FIRST_COMPLETED)
            for completed_future in completed:
                self._consume_media(completed_future)

    def _consume_media(self, future: Future[Dict[str, Any]]) -> None:
        metadata = self.pending_media.pop(future)
        try:
            downloaded = future.result()
            append_jsonl(
                self.paths["media"],
                {
                    "_export": {
                        **metadata,
                        "fetched_at": utc_now(),
                    },
                    "media": {**downloaded, "relative_path": f"media/{downloaded['path']}"},
                },
            )
            self.media_urls.add(str(metadata["url"]))
        except Exception as exc:
            self.error(
                "media",
                exc,
                **metadata,
            )

    def finish_media(self) -> None:
        for future in as_completed(tuple(self.pending_media)):
            self._consume_media(future)
        if self.media_executor is not None:
            self.media_executor.shutdown(wait=True)
            self.media_executor = None

    def abort_media(self) -> None:
        for future in tuple(self.pending_media):
            if future.cancel():
                self.pending_media.pop(future)
        self.finish_media()

    def retry_failed_media(self) -> None:
        pending: Dict[str, Dict[str, Any]] = {}
        for record in iter_jsonl(self.paths["errors"]):
            metadata = record.get("_export")
            if not isinstance(metadata, dict) or metadata.get("stage") != "media":
                continue
            url = metadata.get("url")
            if isinstance(url, str) and url not in self.media_urls:
                pending[url] = metadata
        for url, metadata in pending.items():
            self.export_media_url(
                url,
                str(metadata.get("field", "$")),
                str(metadata.get("source_type", "unknown")),
                metadata.get("source_id"),
            )

    def run(
        self,
        profile: Any = None,
        notebook_response: Any = None,
    ) -> bool:
        if profile is None:
            profile = self.client.get("/users/my")
        if self.notebook is None:
            check_profile_identity(self.paths["profile"], profile)
        self.save_manifest()
        self.started = True
        if CONFIG["export_media"]:
            self.retry_failed_media()
        if self.notebook is None:
            self.export_media(profile, "profile", profile.get("id") if isinstance(profile, dict) else None)

        if notebook_response is None:
            notebook_response = self.client.get("/notebooks/my")
        all_notebooks = response_items(notebook_response, "notebooks")
        notebooks = [self.notebook] if self.notebook is not None else all_notebooks
        if self.notebook is None:
            write_account_metadata(
                self.account_output,
                profile,
                notebook_response,
                len(all_notebooks),
            )
            self.export_media(notebook_response, "notebooks", None)
        else:
            self.export_media(self.notebook, "notebook", self.notebook.get("id"))
        print(f"Found {len(notebooks)} notebook(s).")

        for index, notebook in enumerate(notebooks, 1):
            notebook_id = notebook.get("id")
            subject = str(notebook.get("subject", ""))
            print(f"[{index}/{len(notebooks)}] {subject or notebook_id}")
            if notebook_id is None:
                self.error("notebook", ValueError("Notebook has no id"), notebook=notebook)
                continue
            self.export_notebook(str(notebook_id), subject)

        self.finish_media()
        missing_threads = (
            self.expected_thread_ids - self.thread_ids
            if CONFIG["export_comments"]
            else set()
        )
        complete = not missing_threads and self.errors_this_run == 0
        self.manifest.update(
            {
                "finished_at": utc_now(),
                "complete": complete,
                "counts": {
                    "notebooks": len(notebooks),
                    "diaries": len(load_keys(self.paths["diaries"], "diary_id")),
                    "diary_versions": count_jsonl(self.paths["diaries"]),
                    "comment_threads": len(load_keys(self.paths["threads"], "diary_id")),
                    "comment_thread_versions": count_jsonl(self.paths["threads"]),
                    "comments": latest_comment_count(self.paths["threads"]),
                    "media": count_jsonl(self.paths["media"]),
                    "error_history": count_jsonl(self.paths["errors"]),
                    "errors_this_run": self.errors_this_run,
                },
                "unresolved": {
                    "comment_threads": sorted(missing_threads),
                },
            }
        )
        if complete:
            self.manifest["coverage"]["through_date"] = self.target_coverage_date
        self.save_manifest()
        return complete

    def export_notebook(self, notebook_id: str, subject: str) -> None:
        page, observed = 1, 0
        fingerprints: Set[str] = set()
        while True:
            try:
                payload = self.client.get(
                    f"/notebooks/{quote(notebook_id, safe='')}/diaries",
                    {"page": page, "page_size": CONFIG["page_size"]},
                )
                items = response_items(payload, "diaries")
            except Exception as exc:
                self.error("diary_page", exc, notebook_id=notebook_id, page=page)
                return

            fingerprint = hashlib.sha256(compact_json(items).encode("utf-8")).hexdigest()
            if items and fingerprint in fingerprints:
                self.error(
                    "pagination",
                    RuntimeError("API repeated a page"),
                    notebook_id=notebook_id,
                    page=page,
                )
                return
            fingerprints.add(fingerprint)

            if not items:
                return

            if self.append and self.append_start_date is not None:
                dates = [diary_date(item) for item in items]
                if all(
                    created is not None and created < self.append_start_date
                    for created in dates
                ):
                    return

            for item_index, item in enumerate(items):
                created = diary_date(item)
                if (
                    self.append_start_date is not None
                    and created is not None
                    and created < self.append_start_date
                ):
                    continue
                self.export_diary(notebook_id, subject, page, item_index, item)

            observed += len(items)
            total = safe_int(payload.get("count")) if isinstance(payload, dict) else None
            if (total is not None and observed >= total) or (
                total is None and len(items) < CONFIG["page_size"]
            ):
                return
            page += 1
            if page > 100000:
                self.error("pagination", RuntimeError("Exceeded 100000 pages"), notebook_id=notebook_id)
                return

    def export_diary(
        self,
        notebook_id: str,
        subject: str,
        page: int,
        item_index: int,
        item: Dict[str, Any],
    ) -> None:
        raw_id = item.get("id")
        digest = payload_hash(item)
        record_key = f"{raw_id}:{digest}" if raw_id is not None else f"missing:{notebook_id}:{digest}"
        metadata = {
            "record_key": record_key,
            "diary_id": raw_id,
            "notebook_id": notebook_id,
            "notebook_subject": subject,
            "page": page,
            "item_index": item_index,
            "fetched_at": utc_now(),
            "payload_hash": digest,
        }
        if record_key not in self.diary_keys:
            append_jsonl(self.paths["diaries"], {"_export": metadata, "diary": item})
            self.diary_keys.add(record_key)
        if raw_id is None:
            self.error("diary", ValueError("Diary has no id"), record_key=record_key)
            return

        diary_id = str(raw_id)
        self.export_media(item, "diary", diary_id)
        if not CONFIG["export_comments"]:
            return
        self.expected_thread_ids.add(diary_id)
        if diary_id in self.thread_ids and not self.should_refresh_comments(item):
            return

        if find_comment_count(item) == 0:
            response: Any = []
            self.store_comment_thread(
                diary_id,
                metadata,
                response,
                requested=False,
                reason="comment_count_zero",
            )
            return

        try:
            response = self.client.get(f"/diaries/{quote(diary_id, safe='')}/comments")
            response_items(response, "comments")
            self.store_comment_thread(diary_id, metadata, response, requested=True)
            self.export_media(response, "comments", diary_id)
        except Exception as exc:
            self.error("comments", exc, diary_id=diary_id, notebook_id=notebook_id)

    def store_comment_thread(
        self,
        diary_id: str,
        metadata: Dict[str, Any],
        response: Any,
        requested: bool,
        reason: Optional[str] = None,
    ) -> None:
        digest = payload_hash(response)
        if digest not in self.thread_hashes.get(diary_id, set()):
            exported = {
                **metadata,
                "diary_id": diary_id,
                "requested": requested,
                "payload_hash": digest,
            }
            if reason:
                exported["reason"] = reason
            append_jsonl(
                self.paths["threads"],
                {"_export": exported, "response": response},
            )
            self.thread_hashes.setdefault(diary_id, set()).add(digest)
        self.thread_ids.add(diary_id)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Timepill profile, notebooks, diaries, comments, and images as JSON/JSONL."
    )
    parser.add_argument(
        "--notebook",
        action="append",
        metavar="ID_OR_NAME",
        help="Export one notebook by exact id or name",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rescan",
        action="store_true",
        help="Scan the whole notebook again and fill missing records",
    )
    mode.add_argument(
        "--append",
        action="store_true",
        help="Scan from the last complete coverage checkpoint through today",
    )
    args = parser.parse_args(argv)
    if args.notebook and len(args.notebook) > 1:
        parser.error("--notebook may be specified only once")
    args.notebook = args.notebook[0] if args.notebook else None
    return args


def keychain_password() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    try:
        password = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                CONFIG["account"],
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if password.returncode != 0:
        return None
    return password.stdout.rstrip("\n")


def prepare_directory(path: Path, reuse: Optional[bool] = None) -> Path:
    path = path.expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"Output is not a directory: {path}")
    if reuse is True and not path.exists():
        raise ValueError(f"Cannot reuse missing export directory: {path}")
    if reuse is False and path.exists() and any(
        entry.name != ".DS_Store" for entry in path.iterdir()
    ):
        raise ValueError(f"Output directory is not empty; use --rescan or --append: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def validate_existing_export(
    path: Path,
    args: argparse.Namespace,
    notebook_id: Optional[str] = None,
) -> None:
    if not (args.rescan or args.append):
        return
    manifest_path = path / FILES["manifest"]
    if not manifest_path.exists():
        raise ValueError(f"Cannot reuse a directory without {FILES['manifest']}: {path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read existing {FILES['manifest']}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise ValueError("Existing export has an unsupported manifest format")
    if str(manifest.get("source", "")).rstrip("/") != BASE_URL:
        raise ValueError("Existing export uses a different Timepill API source")

    options = manifest.get("options")
    if not isinstance(options, dict):
        options = {}
    existing_notebook_id = options.get("notebook_id")
    if notebook_id is not None and str(existing_notebook_id) != notebook_id:
        raise ValueError("Existing export belongs to a different notebook")
    if notebook_id is None and existing_notebook_id is not None:
        raise ValueError("Existing export is notebook-scoped; use --notebook")
    covered = manifest_covered_through_date(manifest)
    if covered is not None and covered > shanghai_today():
        raise ValueError("Existing manifest coverage is later than today in Asia/Shanghai")
    previous_mode = options.get("mode")
    # Old manifests used sync=true instead of mode=append.
    previous_append = previous_mode == "append" or (
        previous_mode is None and bool(options.get("sync"))
    )
    if args.append and not manifest.get("complete") and not previous_append:
        raise ValueError(
            "The initial export is incomplete; finish it with --rescan before using --append"
        )


def run_export(
    exporter: Exporter,
    output: Path,
    retry_command: str,
    failure_label: str,
    *run_args: Any,
) -> int:
    try:
        complete = exporter.run(*run_args)
    except KeyboardInterrupt:
        exporter.abort_media()
        if exporter.started:
            exporter.error("fatal", RuntimeError("Interrupted by user"))
            exporter.save_manifest()
        print(f"Interrupted. Retry with {retry_command}", file=sys.stderr)
        return 130
    except Exception as exc:
        exporter.abort_media()
        if exporter.started:
            exporter.error("fatal", exc)
            exporter.save_manifest()
        print(f"{failure_label}: {exc}", file=sys.stderr)
        print(f"Partial output retained at: {output}", file=sys.stderr)
        return 1

    print(f"Export directory: {output}")
    print(f"Complete: {str(complete).lower()}")
    print(json.dumps(exporter.manifest["counts"], ensure_ascii=False, sort_keys=True))
    return 0 if complete else 1


def run_selected_notebook(
    args: argparse.Namespace,
    client: TimepillClient,
    root: Path,
) -> int:
    try:
        profile = client.get("/users/my")
        notebook_response = client.get("/notebooks/my")
        all_notebooks = response_items(notebook_response, "notebooks")
        check_profile_identity(root / FILES["profile"], profile)
        notebook = select_notebook(all_notebooks, args.notebook)
    except Exception as exc:
        print(f"Export failed before notebook selection: {exc}", file=sys.stderr)
        return 1

    notebook_id = str(notebook["id"])
    subject = str(notebook.get("subject", ""))
    try:
        output = prepare_directory(
            notebook_output(root, notebook),
            args.rescan or args.append,
        )
        validate_existing_export(output, args, notebook_id)
        exporter = Exporter(args, client, output, root, notebook)
        write_account_metadata(root, profile, notebook_response, len(all_notebooks))
    except Exception as exc:
        print(f"Notebook export failed ({subject or notebook_id}): {exc}", file=sys.stderr)
        return 1

    mode = "--append" if args.append else "--rescan"
    return run_export(
        exporter,
        output,
        f"--notebook {notebook_id} {mode}",
        f"Notebook export failed ({subject or notebook_id})",
        profile,
        notebook_response,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.notebook:
            output = prepare_directory(Path(CONFIG["output"]))
        else:
            output = prepare_directory(Path(CONFIG["output"]), args.rescan or args.append)
            validate_existing_export(output, args)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        account = CONFIG["account"]
        password = keychain_password() or ""
        if not password:
            password = getpass.getpass("Timepill password: ")
    except (EOFError, KeyboardInterrupt):
        print("Interrupted before authentication", file=sys.stderr)
        return 130
    if not password:
        print("Error: password is required", file=sys.stderr)
        return 2

    client = TimepillClient(account, password)
    try:
        if args.notebook:
            return run_selected_notebook(args, client, output)
        exporter = Exporter(args, client, output)
    except KeyboardInterrupt:
        print("Interrupted before export", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        print(f"Partial output retained at: {output}", file=sys.stderr)
        return 1

    mode = "--append" if args.append else "--rescan"
    return run_export(exporter, output, mode, "Export failed")


if __name__ == "__main__":
    raise SystemExit(main())
