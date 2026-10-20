"""Read-only Notion transport and private credentials; Python 3.9+, stdlib only."""
import json
import os
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

VERSION = '2026-03-11'


class BackupError(Exception):
    """Only safe, machine-readable text may be placed in this exception."""


class APIError(BackupError):
    def __init__(self, status):
        self.status = status
        super().__init__('notion_http_' + str(status))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credential(cfg):
    value = os.environ.get(cfg.get('token_env', 'NOTION_BACKUP_TOKEN'), '').strip()
    if value:
        return value
    if os.path.exists('/usr/bin/security') and cfg.get('keychain_service'):
        args = ['/usr/bin/security', 'find-generic-password', '-s', cfg['keychain_service']]
        if cfg.get('keychain_account'):
            args += ['-a', cfg['keychain_account']]
        try:
            result = subprocess.run(args + ['-w'], capture_output=True, timeout=15)
            if result.returncode == 0:
                return result.stdout.decode().strip()
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            pass
    return None


class Client:
    def __init__(self, token, version=VERSION, opener=None, sleep=time.sleep,
                 interval=0.4, max_requests=200000, clock=time.monotonic):
        self.token, self.version = token, version
        self.opener = opener or build_opener(NoRedirect())
        self.sleep, self.interval = sleep, interval
        self.max_requests, self.requests = max_requests, 0
        self.clock, self.last_start = clock, None

    @staticmethod
    def allowed(method, path):
        ident = r'[a-fA-F0-9-]{32,36}'
        reads = [r'/users/me', rf'/pages/{ident}', rf'/pages/{ident}/markdown',
                 rf'/pages/{ident}/properties/[^/?#]+', rf'/blocks/{ident}/children',
                 rf'/databases/{ident}', rf'/data_sources/{ident}', r'/comments']
        return ((method == 'GET' and any(re.fullmatch(p, path) for p in reads)) or
                (method == 'POST' and (path == '/search' or
                                      re.fullmatch(rf'/data_sources/{ident}/query', path))))

    def request(self, method, path, params=None):
        if not self.allowed(method, path):
            raise BackupError('non_read_endpoint_refused')
        url = 'https://api.notion.com/v1' + path
        data = None
        if method == 'GET' and params:
            url += '?' + urlencode(params)
        elif method == 'POST':
            data = json.dumps(params or {}).encode()
        request = Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Notion-Version': self.version,
            'Content-Type': 'application/json', 'User-Agent': 'local-notion-backup/1'})
        for attempt in range(6):
            if self.requests >= self.max_requests:
                raise BackupError('request_budget_exceeded')
            # Network and local processing time already count toward the interval.
            if self.last_start is not None:
                remaining = self.interval - (self.clock() - self.last_start)
                if remaining > 0:
                    self.sleep(remaining)
            self.last_start = self.clock()
            self.requests += 1
            try:
                with self.opener.open(request, timeout=60) as response:
                    body = response.read(64 * 1024 * 1024 + 1)
                if len(body) > 64 * 1024 * 1024:
                    raise BackupError('api_response_size_limit')
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise BackupError('unexpected_api_response')
                return parsed
            except HTTPError as exc:
                status, retry = exc.code, exc.headers.get('Retry-After', '')
                exc.close()
                if status not in (429, 500, 502, 503, 504, 529) or attempt == 5:
                    raise APIError(status) from None
                try:
                    delay = max(float(retry), 2 ** attempt) if retry else 2 ** attempt
                except ValueError:
                    raise BackupError('invalid_retry_after') from None
                if not 0 <= delay <= 60:
                    raise BackupError('retry_after_requires_later_run')
                self.sleep(delay)
            except (URLError, TimeoutError, OSError):
                if attempt == 5:
                    raise BackupError('api_network_error') from None
                self.sleep(2 ** attempt)
            except (ValueError, UnicodeError):
                raise BackupError('invalid_api_json') from None


def paginate(client, method, path, params=None, save=None, save_request=None):
    """Retain every response, including property_item metadata on the final page."""
    args, cursors = dict(params or {}), set()
    args['page_size'] = 100
    while True:
        response = client.request(method, path, args)
        if save:
            save(response)
        if save_request:
            save_request(response, dict(args))
        if response.get('object') != 'list':
            yield response
            return
        results = response.get('results')
        if not isinstance(results, list):
            raise BackupError('invalid_paginated_results')
        yield from results
        if not response.get('has_more'):
            return
        cursor = response.get('next_cursor')
        if not cursor or cursor in cursors:
            raise BackupError('invalid_or_repeated_cursor')
        cursors.add(cursor)
        args['start_cursor'] = cursor
