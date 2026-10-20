"""Download only file objects, never send API credentials to attachment hosts."""
import hashlib
import mimetypes
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError

from notion_api import BackupError, NoRedirect


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def hosted_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and any(
                    host.endswith('.' + domain) for domain in
                    ('amazonaws.com', 'notion.so', 'notion-static.com', 'notion.com')))
    except ValueError:
        return False


def file_objects(value):
    if isinstance(value, dict):
        kind = value.get('type')
        if kind in ('file', 'external') and isinstance(value.get(kind), dict):
            url = value[kind].get('url')
            if isinstance(url, str):
                yield kind, url
        # Comment attachments omit the usual `type: file` discriminator.
        elif 'category' in value and isinstance(value.get('file'), dict):
            url = value['file'].get('url')
            if isinstance(url, str):
                yield 'file', url
        for child in value.values():
            yield from file_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from file_objects(child)


class Assets:
    def __init__(self, store, snapshot, max_bytes=512 * 1024 * 1024, opener=None, workers=4):
        self.store, self.snapshot = Path(store), Path(snapshot)
        self.max_bytes = max_bytes
        self.links, self.failures, self.external = {}, {}, set()
        self.workers = workers
        self.pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        self.pending = {}
        self.commit_lock = threading.Lock()
        self.thread_state = threading.local()
        self.custom_opener = opener

    def drain(self, block=False):
        if not self.pending:
            return
        futures = list(self.pending)
        ready = wait(futures, return_when=FIRST_COMPLETED).done if block else {
            future for future in futures if future.done()}
        for future in ready:
            url = self.pending.pop(future)
            try:
                self.links[url] = future.result()
            except BackupError as exc:
                self.failures[url] = str(exc)

    def flush(self):
        while self.pending:
            self.drain(block=True)

    def close(self):
        try:
            self.flush()
        finally:
            if self.pool:
                self.pool.shutdown(wait=True)

    def collect(self, value):
        self.drain()
        for kind, url in file_objects(value):
            if (url in self.links or url in self.failures or url in self.external
                    or url in self.pending.values()):
                continue
            if kind == 'external':
                self.external.add(url)
                continue
            if self.pool:
                while len(self.pending) >= self.workers * 2:
                    self.drain(block=True)
                self.pending[self.pool.submit(self.download, url)] = url
                continue
            try:
                self.links[url] = self.download(url)
            except BackupError as exc:
                # URL may contain a signed credential; reports expose only its hash.
                self.failures[url] = str(exc)

    def download(self, url):
        # urllib handlers are not shared between worker threads.
        if not hasattr(self.thread_state, 'opener'):
            self.thread_state.opener = self.custom_opener or build_opener(NoRedirect())
        current = url
        response = None
        for _ in range(6):
            if not hosted_url(current):
                raise BackupError('attachment_host_not_allowed')
            try:
                response = self.thread_state.opener.open(Request(current, headers={
                    'User-Agent': 'local-notion-backup/1'}), timeout=60)
                break
            except HTTPError as exc:
                code, location = exc.code, exc.headers.get('Location')
                exc.close()
                if code in (301, 302, 303, 307, 308) and location:
                    from urllib.parse import urljoin
                    current = urljoin(current, location)
                    continue
                raise BackupError('attachment_http_' + str(code)) from None
            except (URLError, OSError, TimeoutError):
                raise BackupError('attachment_network_error') from None
        if response is None:
            raise BackupError('attachment_redirect_limit')
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix='.download-', dir=self.store)
        try:
            total = 0
            with response, os.fdopen(fd, 'wb') as output:
                media_type = response.headers.get('Content-Type', '').split(';')[0].strip()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise BackupError('attachment_size_limit')
                    output.write(chunk)
            digest = sha256(temp)
            cached = self.store / digest
            extension = mimetypes.guess_extension(media_type) or ''
            target = self.snapshot / 'assets' / (digest + extension)
            with self.commit_lock:
                if cached.exists():
                    if sha256(cached) != digest:
                        raise BackupError('cached_attachment_hash_mismatch')
                else:
                    os.replace(temp, cached)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not target.exists():
                    os.link(cached, target)
            return 'assets/' + target.name
        except (OSError, TimeoutError, URLError):
            raise BackupError('attachment_io_error') from None
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def summary(self):
        self.flush()
        return {'downloaded_urls': len(self.links), 'unique_files': len(set(self.links.values())),
                'external_urls_retained_only': len(self.external),
                'failures': [{'url_sha256': hashlib.sha256(url.encode()).hexdigest(), 'error': error}
                             for url, error in self.failures.items()]}
