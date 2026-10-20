#!/usr/bin/env python3
"""One-command API snapshot. No model, browser session, or third-party dependencies."""
import argparse
from collections import deque
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import shutil
import signal
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit
import uuid

from notion_api import APIError, BackupError, Client, VERSION, credential, paginate
from notion_assets import Assets, sha256
import notion_checkpoint as checkpoint

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config.local.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(value):
    try:
        return uuid.UUID(value).hex
    except (ValueError, TypeError, AttributeError):
        raise BackupError('invalid_notion_id') from None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def config(path):
    if not path.is_file():
        raise BackupError('needs_configuration')
    if path.stat().st_mode & 0o077:
        raise BackupError('config_requires_mode_600')
    cfg = json.loads(path.read_text())
    api = cfg.get('api', {})
    if not api.get('destination') or not api.get('roots'):
        raise BackupError('needs_api_configuration')
    if api.get('comments', 'page') not in ('page', 'all', 'none'):
        raise BackupError('invalid_comments_scope')
    for root in api['roots']:
        if root.get('type') not in ('page', 'database', 'data_source'):
            raise BackupError('invalid_root_type')
        uid(root.get('id'))
    if api.get('database_pages', 'links') not in ('links', 'full'):
        raise BackupError('invalid_database_pages_mode')
    if api.get('version', VERSION) != VERSION:
        raise BackupError('unsupported_api_version')
    if int(api.get('max_attachment_bytes', 512 * 1024 * 1024)) <= 0:
        raise BackupError('invalid_attachment_limit')
    return cfg


def series(cfg):
    api = cfg['api']
    identity = {'workspace': cfg.get('workspace', {}), 'version': api.get('version', VERSION),
                'roots': sorted((r['type'], uid(r['id'])) for r in api['roots']),
                'discover_search': api.get('discover_search', True),
                'backup_policy': 'daily_incremental_audit_v1'}
    if api.get('database_pages', 'links') == 'links':
        identity['database_pages'] = 'links_v1'
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def destination_for(cfg):
    base = Path(cfg['api']['destination']).expanduser().resolve()
    return base / 'database-links' if cfg['api'].get('database_pages', 'links') == 'links' else base


@contextmanager
def lock(destination):
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination / '.run.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError('backup_already_running') from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def verify(snapshot):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    files = manifest.get('files', {})
    if manifest.get('schema') != 1 or not files:
        raise BackupError('invalid_snapshot_manifest')
    for name, record in files.items():
        path = snapshot / name
        if (path.is_symlink() or not path.resolve().is_relative_to(snapshot)
                or not path.is_file() or path.stat().st_size != record['bytes']
                or sha256(path) != record['sha256']):
            raise BackupError('snapshot_integrity_failed')
    return {'status': 'verified', 'snapshot': str(snapshot), 'files': len(files),
            'backup_status': manifest['status']}


def title_of(obj):
    value = obj.get('title', [])
    for prop in obj.get('properties', {}).values():
        if prop.get('type') == 'title':
            value = prop.get('title', [])
            break
    return ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in value)


def needs_property_fetch(prop):
    kind = prop.get('type')
    if kind not in ('title', 'rich_text', 'relation', 'people', 'rollup', 'formula'):
        return False
    if kind == 'relation' and prop.get('has_more') is False:
        return False
    if kind in ('title', 'rich_text'):
        value = prop.get(kind)
        # Keep pagination for mentions, unknown shapes, and limit-boundary values.
        if (isinstance(value, list) and len(value) < 25 and not prop.get('has_more')
                and all(item.get('type') in ('text', 'equation') for item in value)):
            return False
    return True


class Backup:
    def __init__(self, cfg, client, destination, previous=None, assets_factory=Assets, resume=None,
                 run_mode='daily', baseline=None):
        self.cfg, self.api, self.client = cfg, cfg['api'], client
        self.destination = Path(destination).resolve()
        self.snapshot = Path(resume).resolve() if resume else self.destination / 'snapshots' / (
            datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
        if resume:
            if self.snapshot.parent != (self.destination / 'snapshots').resolve():
                raise BackupError('resume_outside_destination')
        else:
            self.snapshot.mkdir(parents=True, mode=0o700)
        self.assets = assets_factory(self.destination / 'asset-store', self.snapshot,
                                     max_bytes=self.api.get('max_attachment_bytes', 512 * 1024 * 1024))
        self.previous, self.baseline = previous or {}, baseline or {}
        self.run_mode = run_mode
        if run_mode not in ('daily', 'audit'):
            raise BackupError('invalid_run_mode')
        self.comments_mode = 'none' if run_mode == 'daily' else self.api.get('comments', 'page')
        self.consistency_check = run_mode == 'audit'
        self.queue, self.queued, self.completed, self.blocks = deque(), set(), set(), set()
        self.raw_count, self.page_count = 0, 0
        self.errors, self.warnings, self.index = [], [], []
        self.markdown, self.started = {}, now()
        self.page_candidates, self.page_states, self.entity_files = {}, {}, {}
        self.active_entity = None
        self.reused_pages, self.refreshed_pages, self.reuse_fallbacks = 0, 0, 0
        self.reused_assets = False
        self.baseline_path = (Path(self.baseline['_snapshot_path']).resolve()
                              if self.baseline.get('_snapshot_path') else None)
        self.database_pages = self.api.get('database_pages', 'links')
        self.link_pages, self.page_parents, self.link_descendants = {}, {}, {}
        self.discovered, self.pause_requested = False, False
        self.deadline = None
        self.request_offset, self.checkpoint_files = 0, {}
        self.prior = {tuple(item) for item in self.previous.get('known_entities', [])}
        self.manifest = {'schema': 1, 'status': 'running', 'started': self.started,
                         'mode': self.run_mode,
                         'series': series(cfg), 'known_entities': sorted(self.prior),
                         'scope': 'Configured roots, prior IDs and API-visible search results',
                         'workspace': cfg.get('workspace', {})}
        if resume:
            existing = json.loads((self.snapshot / 'manifest.json').read_text())
            if existing['status'] not in ('running', 'paused'):
                raise BackupError('snapshot_already_finalized')
            if existing.get('mode', 'audit') != self.run_mode:
                raise BackupError('resume_mode_mismatch')
            checkpoint.restore(self)
            self.manifest['started'] = self.started
            self.manifest['known_entities'] = sorted(self.prior | self.queued)
            (self.snapshot / '.pause-request').unlink(missing_ok=True)
        write_json(self.snapshot / 'manifest.json', self.manifest)
        checkpoint.save(self, write_json)

    def check_pause(self):
        if (self.pause_requested or (self.snapshot / '.pause-request').exists()
                or (self.deadline is not None and time.monotonic() >= self.deadline)):
            raise checkpoint.PauseRequested()

    def warn(self, code, entity=None):
        self.warnings.append({'code': code, 'entity': entity})

    def fail(self, code, entity=None):
        self.errors.append({'code': code, 'entity': entity})

    def record_entity_file(self, path, kind='raw'):
        if self.active_entity:
            relative = path.relative_to(self.snapshot).as_posix()
            self.entity_files.setdefault(self.active_entity, []).append(
                {'path': relative, 'kind': kind})

    def remember_page(self, obj):
        if obj.get('object') != 'page':
            return
        ident = uid(obj['id'])
        self.page_candidates[ident] = obj
        parent = obj.get('parent', {})
        self.page_parents[ident] = parent
        source = parent.get('data_source_id') or parent.get('database_id')
        if not source or self.database_pages != 'links':
            return
        urls = {name: value['url'] for name, value in obj.get('properties', {}).items()
                if value.get('type') == 'url' and isinstance(value.get('url'), str)
                and urlsplit(value['url']).scheme in ('http', 'https')}
        self.link_pages[ident] = {'id': ident, 'title': title_of(obj), 'original_urls': urls,
                                 'notion_url': obj.get('url') or 'https://www.notion.so/' + ident,
                                 'parent_id': uid(source), 'properties': obj.get('properties', {}),
                                 'mode': 'links_only', 'property_values': 'inline_not_paginated'}
        self.completed.add(('page', ident))

    def link_root(self, ident):
        seen = set()
        while ident not in seen:
            if ident in self.link_pages:
                return ident
            seen.add(ident)
            parent = self.page_parents.get(ident, {})
            if parent.get('type') != 'page_id' or not parent.get('page_id'):
                return None
            ident = uid(parent['page_id'])
        return None

    def resolve_link_root(self, ident):
        if self.database_pages != 'links':
            return None
        current, seen = ident, set()
        while current not in seen:
            if current in self.link_pages:
                return current
            seen.add(current)
            parent = self.page_parents.get(current, {})
            if parent.get('type') != 'page_id' or not parent.get('page_id'):
                return None
            current = uid(parent['page_id'])
            if current not in self.page_parents:
                # A descendant can appear in search even when its database row does not.
                obj = self.get('/pages/' + current)
                if current in self.link_pages:
                    self.discover(obj)
        return None

    def attachment_payload(self, value):
        if isinstance(value, list):
            return [self.attachment_payload(item) for item in value]
        if isinstance(value, dict):
            if value.get('object') in ('database', 'data_source'):
                return None
            if value.get('object') == 'page' and self.link_root(uid(value['id'])):
                return None
            return {key: self.attachment_payload(item) for key, item in value.items()}
        return value

    def discard_link_queue(self):
        retained = deque()
        for kind, ident in self.queue:
            root = self.link_root(ident) if kind == 'page' else None
            if root:
                self.completed.add((kind, ident))
                if root != ident:
                    self.link_descendants[ident] = root
            else:
                retained.append((kind, ident))
        self.queue = deque(sorted(retained, key=lambda item: item[0] == 'page'))

    def baseline_source(self, relative):
        if not self.baseline_path or not relative or relative.startswith('/'):
            raise BackupError('invalid_incremental_baseline')
        source = (self.baseline_path / relative).resolve()
        if not source.is_relative_to(self.baseline_path) or source.is_symlink() or not source.is_file():
            raise BackupError('incremental_baseline_file_missing')
        record = self.baseline.get('files', {}).get(relative)
        if (not record or source.stat().st_size != record.get('bytes')
                or sha256(source) != record.get('sha256')):
            raise BackupError('incremental_baseline_integrity_failed')
        return source

    def link_from_baseline(self, relative, destination=None, hardlink=False):
        source = self.baseline_source(relative)
        target = self.snapshot / (destination or relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            if target.stat().st_size != source.stat().st_size or sha256(target) != sha256(source):
                raise BackupError('incremental_target_conflict')
            return target
        if hardlink:
            try:
                os.link(source, target)
                return target
            except OSError:
                pass
        if not target.exists():
            shutil.copy2(source, target)
        return target

    def can_reuse_page(self, ident, page):
        if self.run_mode != 'daily' or not self.baseline_path:
            return False
        state = self.baseline.get('page_states', {}).get(ident)
        records = self.baseline.get('entity_files', {}).get('page:' + ident)
        return (isinstance(state, dict) and state.get('last_edited_time')
                and state.get('last_edited_time') == page.get('last_edited_time')
                and isinstance(records, list) and records
                and 'notes/' + ident + '.md' in self.baseline.get('files', {}))

    def reuse_page(self, ident, page):
        key = 'page:' + ident
        records = self.baseline['entity_files'][key]
        # Validate all inputs before changing the new snapshot.
        kept = [record for record in records if record.get('kind') != 'comments']
        for record in kept:
            self.baseline_source(record.get('path'))
        self.baseline_source('notes/' + ident + '.md')
        if not self.reused_assets:
            asset_paths = [relative for relative in sorted(self.baseline.get('files', {}))
                           if relative.startswith('assets/')]
            for relative in asset_paths:
                self.baseline_source(relative)
            for relative in asset_paths:
                self.link_from_baseline(relative, hardlink=True)
            self.reused_assets = True
        current = []
        for record in kept:
            self.raw_count += 1
            target_rel = 'raw/%07d.json' % self.raw_count
            target = self.link_from_baseline(record['path'], target_rel)
            checkpoint.record_file(self, target)
            current.append({'path': target_rel, 'kind': record.get('kind', 'raw')})
        self.entity_files[key] = current
        self.link_from_baseline('notes/' + ident + '.md')
        self.page_states[ident] = {'last_edited_time': page.get('last_edited_time'),
                                   'title': title_of(page), 'reused': True}
        self.index.append({'type': 'page', 'id': ident, 'title': title_of(page),
                           'file': 'notes/' + ident + '.md', 'reused': True})
        self.reused_pages += 1
        self.page_count += 1

    def save_response(self, method, path, response, params=None):
        self.raw_count += 1
        target = self.snapshot / 'raw' / ('%07d.json' % self.raw_count)
        write_json(target, {'method': method, 'path': path, 'params': params, 'response': response})
        checkpoint.record_file(self, target)
        self.record_entity_file(target, 'comments' if path == '/comments' else 'raw')
        for obj in [response] + response.get('results', []):
            self.remember_page(obj)
        # Search is discovery only. Full pages/query responses provide the assets later.
        defer_page_assets = self.database_pages == 'links' and response.get('object') == 'page'
        if path != '/search' and not defer_page_assets:
            self.assets.collect(self.attachment_payload(response) if self.database_pages == 'links' else response)
        self.check_pause()

    def get(self, path, params=None):
        self.check_pause()
        result = self.client.request('GET', path, params)
        self.save_response('GET', path, result, params)
        return result

    def each(self, method, path, params=None):
        self.check_pause()
        return paginate(self.client, method, path, params,
                        save_request=lambda result, args: self.save_response(method, path, result, args))

    def enqueue(self, kind, ident):
        item = kind, uid(ident)
        if kind not in ('page', 'database', 'data_source'):
            raise BackupError('unknown_entity_type')
        if item not in self.queued:
            self.queued.add(item)
            self.queue.append(item)

    def discover(self, obj):
        kind = obj.get('object')
        self.remember_page(obj)
        if kind in ('page', 'database', 'data_source'):
            if kind != 'page' or not self.link_root(uid(obj['id'])):
                self.enqueue(kind, obj['id'])
            elif uid(obj['id']) not in self.link_pages:
                ident = uid(obj['id'])
                self.link_descendants[ident] = self.link_root(ident)
                self.completed.add(('page', ident))
        parent = obj.get('parent', {})
        # A database returned by search may expose multiple data sources.
        if parent.get('type') == 'database_id':
            self.enqueue('database', parent['database_id'])
        if parent.get('type') == 'data_source_id':
            self.enqueue('data_source', parent['data_source_id'])

    def comments(self, ident, is_page=False):
        mode = self.comments_mode
        if mode == 'none' or (mode == 'page' and not is_page):
            return
        # Fail once on capability denial rather than issuing thousands of doomed calls.
        if getattr(self, 'comments_denied', False):
            return
        try:
            list(self.each('GET', '/comments', {'block_id': ident}))
        except APIError as exc:
            if exc.status == 403:
                self.comments_denied = True
                self.fail('comments_capability_missing', ident)
            else:
                raise

    def walk_blocks(self, page_id):
        pending = deque([page_id])
        while pending:
            parent = uid(pending.popleft())
            if parent in self.blocks:
                continue
            self.blocks.add(parent)
            for block in self.each('GET', '/blocks/' + parent + '/children'):
                ident, kind = uid(block['id']), block.get('type')
                if kind == 'child_page':
                    self.enqueue('page', ident)
                elif kind == 'child_database':
                    self.enqueue('database', ident)
                else:
                    if block.get('has_children'):
                        pending.append(ident)
                    if kind == 'synced_block':
                        source = block.get(kind, {}).get('synced_from') or {}
                        if source.get('block_id'):
                            pending.append(source['block_id'])
                if kind == 'unsupported':
                    self.warn('unsupported_block_raw_only', ident)
                self.comments(ident)

    def page(self, ident):
        root = self.link_root(ident)
        if root:
            if root != ident:
                self.link_descendants[ident] = root
            return
        candidate = self.page_candidates.get(ident)
        page = (candidate if candidate and isinstance(candidate.get('properties'), dict)
                and isinstance(candidate.get('last_edited_time'), str)
                else self.get('/pages/' + ident))
        root = self.resolve_link_root(ident)
        if root:
            if root != ident:
                self.link_descendants[ident] = root
            return
        if self.database_pages == 'links':
            self.assets.collect(page)
        if self.can_reuse_page(ident, page):
            try:
                self.reuse_page(ident, page)
                return
            except BackupError:
                # Reuse is an optimization. A bad/missing baseline must trigger a fresh read.
                self.reuse_fallbacks += 1
        self.index.append({'type': 'page', 'id': ident, 'title': title_of(page),
                           'file': 'notes/' + ident + '.md'})
        # Preserve additional items and final rollup metadata beyond inline limits.
        for prop in page.get('properties', {}).values():
            if needs_property_fetch(prop):
                prop_id = quote(unquote(prop['id']), safe='')
                try:
                    list(self.each('GET', '/pages/' + ident + '/properties/' + prop_id))
                except APIError as exc:
                    if exc.status == 401:
                        raise
                    self.fail('property_' + str(exc), ident)
        try:
            rendered = self.get('/pages/' + ident + '/markdown')
            if not isinstance(rendered.get('markdown'), str):
                raise BackupError('invalid_markdown_response')
            self.markdown[ident] = rendered['markdown']
            target = self.snapshot / '.work/markdown' / (ident + '.json')
            write_json(target, rendered['markdown'])
            checkpoint.record_file(self, target)
            if rendered.get('truncated') or rendered.get('unknown_block_ids'):
                self.warn('markdown_incomplete_raw_blocks_retained', ident)
        except APIError as exc:
            if exc.status == 401:
                raise
            self.fail('markdown_' + str(exc), ident)
        self.walk_blocks(ident)
        self.comments(ident, is_page=True)
        if self.consistency_check:
            after = self.get('/pages/' + ident)
            if self.database_pages == 'links':
                self.assets.collect(after)
            if after.get('last_edited_time') != page.get('last_edited_time'):
                self.warn('page_changed_during_backup', ident)
        self.page_states[ident] = {'last_edited_time': page.get('last_edited_time'),
                                   'title': title_of(page), 'reused': False}
        self.refreshed_pages += 1
        self.page_count += 1

    def database(self, ident):
        obj = self.get('/databases/' + ident)
        sources = obj.get('data_sources')
        if not isinstance(sources, list):
            raise BackupError('database_data_sources_missing')
        self.index.append({'type': 'database', 'id': ident, 'title': title_of(obj)})
        for source in sources:
            self.enqueue('data_source', source['id'])

    def data_source(self, ident):
        obj = self.get('/data_sources/' + ident)
        self.index.append({'type': 'data_source', 'id': ident, 'title': title_of(obj),
                           'file': 'databases/' + ident + '.csv'})
        self.discover(obj)
        path = self.snapshot / 'databases' / (ident + '.csv')
        path.parent.mkdir(exist_ok=True, mode=0o700)
        count = 0
        with path.open('w', encoding='utf-8', newline='') as output:
            writer = csv.writer(output)
            writer.writerow(['id', 'title', 'properties_json'])
            for page in self.each('POST', '/data_sources/' + ident + '/query'):
                self.discover(page)
                # Properties remain lossless JSON. Full paginated values are in raw/.
                writer.writerow([page['id'], title_of(page),
                                 json.dumps(page.get('properties', {}), ensure_ascii=False)])
                count += 1
        if count >= 10000:
            self.fail('data_source_query_limit_requires_partitioning', ident)
        checkpoint.record_file(self, path)

    def run(self):
        try:
            return self._run()
        except checkpoint.PauseRequested:
            # The last committed checkpoint still contains the interrupted entity.
            # Never checkpoint partially walked block trees as completed work.
            self.assets.flush()
            self.manifest['status'] = 'paused'
            write_json(self.snapshot / 'manifest.json', self.manifest)
            result = checkpoint.progress(self.snapshot)
            result['status'] = 'paused'
            return result
        finally:
            self.assets.close()

    def _run(self):
        try:
            if not self.discovered:
                self.get('/users/me')
                for root in self.api['roots']:
                    self.enqueue(root['type'], root['id'])
                for kind, ident in sorted(self.prior):
                    self.enqueue(kind, ident)
            if not self.discovered and self.api.get('discover_search', True):
                try:
                    for obj in self.each('POST', '/search'):
                        self.discover(obj)
                except BackupError as exc:
                    if isinstance(exc, APIError) and exc.status == 401:
                        raise
                    self.fail(str(exc), 'search')
            self.discovered = True
            self.discard_link_queue()
            checkpoint.save(self, write_json)
            while self.queue:
                self.check_pause()
                kind, ident = self.queue.popleft()
                self.active_entity = kind + ':' + ident
                try:
                    getattr(self, kind)(ident)
                    self.completed.add((kind, ident))
                except BackupError as exc:
                    self.fail(str(exc), kind + ':' + ident)
                    if isinstance(exc, APIError) and exc.status == 401:
                        break
                    if str(exc) in ('request_budget_exceeded', 'retry_after_requires_later_run'):
                        break
                finally:
                    self.active_entity = None
                self.discard_link_queue()
                checkpoint.save(self, write_json)
        except BackupError as exc:
            self.fail(str(exc))
        self.check_pause()
        return self.finish()

    def finish(self):
        self.assets.flush()
        notes = self.snapshot / 'notes'
        notes.mkdir(exist_ok=True, mode=0o700)
        for ident, markdown in self.markdown.items():
            for url, local in self.assets.links.items():
                markdown = markdown.replace(url, '../' + local)
                markdown = markdown.replace(url.replace('&', '&amp;'), '../' + local)
            (notes / (ident + '.md')).write_text(markdown, encoding='utf-8')
        asset_report = self.assets.summary()
        snapshot_asset_count = len(list((self.snapshot / 'assets').glob('*')))
        asset_report['snapshot_files'] = snapshot_asset_count
        asset_report['reused_files'] = max(0, snapshot_asset_count - asset_report['unique_files'])
        if asset_report['failures']:
            self.fail('attachment_downloads_failed')
        if asset_report['external_urls_retained_only']:
            self.warn('external_files_urls_only')
        missing = sorted(self.prior - self.completed)
        if missing:
            self.fail('prior_entities_not_fully_read')
        status = 'partial' if self.errors else ('archived_with_warnings' if self.warnings else 'archived')
        index = self.index + [{'type': 'page', 'id': ident, 'title': item['title'],
                               'mode': 'links_only', 'file': 'links.json'}
                              for ident, item in sorted(self.link_pages.items())]
        if self.database_pages == 'links':
            write_json(self.snapshot / 'links.json', {'pages': list(self.link_pages.values()),
                       'descendants_covered_by_parent_link': self.link_descendants,
                       'descendant_links': [{'id': ident, 'notion_url': 'https://www.notion.so/' + ident,
                                             'parent_link_page_id': parent}
                                            for ident, parent in sorted(self.link_descendants.items())]})
        write_json(self.snapshot / 'index.json', index)
        write_json(self.snapshot / 'report.json', {
            'status': status, 'errors': self.errors, 'warnings': self.warnings,
            'prior_entities_not_fully_read': missing, 'attachments': asset_report,
            'coverage': {'mode': self.run_mode, 'resolved_comments': False,
                         'comments': self.comments_mode,
                         'incremental_reuse': self.run_mode == 'daily',
                         'consistency_check': self.consistency_check,
                         'database_pages': self.database_pages,
                         'link_only_pages': len(self.link_pages),
                         'link_only_omissions': 'Database row bodies, comments, property pagination and attachment bytes; rows without external URLs retain Notion links' if self.database_pages == 'links' else None,
                         'external_files': 'URLs only', 'workspace_completeness': 'not established',
                         'restore': 'not a full-fidelity workspace restore', 'transactional': False}})
        files = {}
        for path in sorted(self.snapshot.rglob('*')):
            if (path.is_file() and path.name not in ('manifest.json', 'progress.json', '.pause-request')
                    and '.work' not in path.relative_to(self.snapshot).parts):
                files[path.relative_to(self.snapshot).as_posix()] = {
                    'bytes': path.stat().st_size, 'sha256': sha256(path)}
        self.manifest.update({'status': status, 'finished': now(),
                              'known_entities': sorted(self.prior | self.queued | self.completed),
                              'completed_entities': sorted(self.completed), 'files': files,
                              'page_states': self.page_states,
                              'entity_files': self.entity_files,
                              'counts': {'pages': self.page_count, 'entities': len(self.completed),
                                         'reused_pages': self.reused_pages,
                                         'refreshed_pages': self.refreshed_pages,
                                         'reuse_fallbacks': self.reuse_fallbacks,
                                         'link_only_pages': len(self.link_pages),
                                         'link_only_descendants': len(self.link_descendants),
                                         'api_requests': self.request_offset + self.client.requests,
                                         'attachments': snapshot_asset_count,
                                         'errors': len(self.errors), 'warnings': len(self.warnings)}})
        write_json(self.snapshot / 'manifest.json', self.manifest)
        verify(self.snapshot)
        pointer = {'snapshot': self.snapshot.relative_to(self.destination).as_posix(),
                   'manifest_sha256': sha256(self.snapshot / 'manifest.json'), 'series': series(self.cfg)}
        write_json(self.destination / 'latest-attempt.json', pointer)
        if status == 'archived':
            write_json(self.destination / 'latest-success.json', pointer)
        return {'status': status, 'snapshot': str(self.snapshot), 'integrity': 'verified',
                **self.manifest['counts']}


def pointer_manifest(destination, cfg, name, missing_ok=True):
    pointer = destination / name
    if not pointer.exists():
        if missing_ok:
            return None
        raise BackupError('snapshot_pointer_missing')
    value = json.loads(pointer.read_text())
    if value['series'] != series(cfg):
        raise BackupError('scope_changed_use_new_destination')
    path = (destination / value['snapshot']).resolve()
    if not path.is_relative_to(destination.resolve()):
        raise BackupError('invalid_previous_snapshot_path')
    manifest = path / 'manifest.json'
    if sha256(manifest) != value['manifest_sha256']:
        raise BackupError('previous_manifest_integrity_failed')
    result = json.loads(manifest.read_text())
    result['_snapshot_path'] = str(path)
    return result


def previous_manifest(destination, cfg):
    return pointer_manifest(destination, cfg, 'latest-attempt.json')


def successful_manifest(destination, cfg):
    return pointer_manifest(destination, cfg, 'latest-success.json')


def check(client, cfg, anchor_only=False):
    client.request('GET', '/users/me')
    # Explicit roots anchor the intended account/scope; search alone is not an identity check.
    roots = cfg['api']['roots'][:1] if anchor_only else cfg['api']['roots']
    for root in roots:
        plural = {'page': 'pages', 'database': 'databases', 'data_source': 'data_sources'}[root['type']]
        client.request('GET', '/' + plural + '/' + uid(root['id']))
    return {'status': 'ready', 'accessible_roots': len(roots),
            'workspace_identity': 'anchored_by_configured_root_access', 'api_requests': client.requests}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'check'):
        sub.add_parser(command).add_argument('--database-pages', choices=('links', 'full'))
    for command in ('run', 'resume'):
        runner = sub.add_parser(command)
        if command == 'resume':
            runner.add_argument('snapshot', type=Path)
        runner.add_argument('--max-seconds', type=float, default=0,
                            help='Pause at the next request boundary after this duration (0: unlimited).')
        runner.add_argument('--database-pages', choices=('links', 'full'))
        runner.add_argument('--mode', choices=('daily', 'audit'))
    for command in ('progress', 'pause'):
        sub.add_parser(command).add_argument('snapshot', type=Path)
    sub.add_parser('verify').add_argument('snapshot', type=Path)
    args = parser.parse_args()
    try:
        if hasattr(args, 'max_seconds') and (not 0 <= args.max_seconds < float('inf')):
            raise BackupError('invalid_time_budget')
        if args.command == 'verify':
            result = verify(args.snapshot)
        elif args.command in ('progress', 'pause'):
            result = checkpoint.progress(args.snapshot)
            if args.command == 'pause':
                if result['status'] != 'running' or not result['checkpoint_available']:
                    raise BackupError('pause_requires_running_checkpoint_backup')
                write_json(args.snapshot / '.pause-request', {'requested_at': now()})
                result.update(status='pause_requested', pause_requested=True)
        else:
            cfg = config(args.config)
            api = cfg['api']
            if args.database_pages:
                api['database_pages'] = args.database_pages
            token = credential(api)
            destination = destination_for(cfg)
            if args.command == 'status':
                result = {'status': 'configured' if token else 'needs_credential',
                          'destination': str(destination), 'roots': len(api['roots']),
                          'token_available': bool(token), 'schedule': 'not_managed_by_this_script'}
                result['database_pages'] = api.get('database_pages', 'links')
                result['default_mode'] = 'daily'
                result['audit_mode'] = 'run --mode audit'
            elif not token:
                result = {'status': 'needs_credential', 'token_env': api.get('token_env', 'NOTION_BACKUP_TOKEN'),
                          'keychain_service': api.get('keychain_service'), 'backup_created': False}
            else:
                client = Client(token, version=api.get('version', VERSION), interval=0.34)
                if args.command == 'check':
                    result = check(client, cfg)
                else:
                    with lock(destination):
                        resume = args.snapshot if args.command == 'resume' else None
                        run_mode = args.mode or 'daily'
                        if resume:
                            if not (resume / '.work/checkpoint.json').is_file():
                                raise BackupError('legacy_snapshot_has_no_checkpoint')
                            saved = json.loads((resume / 'manifest.json').read_text())
                            saved_mode = saved.get('mode', 'audit')
                            if args.mode and args.mode != saved_mode:
                                raise BackupError('resume_mode_mismatch')
                            run_mode = saved_mode
                        else:
                            unfinished = []
                            for path in sorted((destination / 'snapshots').glob('*/manifest.json')):
                                if json.loads(path.read_text()).get('status') in ('running', 'paused'):
                                    unfinished.append(str(path.parent))
                            if unfinished:
                                print(json.dumps({'status': 'needs_resume', 'snapshots': unfinished}))
                                return 2
                        # Refuse a wrong account before creating a snapshot.
                        check(client, cfg, anchor_only=True)
                        previous = None if resume else previous_manifest(destination, cfg)
                        baseline = successful_manifest(destination, cfg) if run_mode == 'daily' else None
                        backup = Backup(cfg, client, destination, previous, resume=resume,
                                        run_mode=run_mode, baseline=baseline)
                        backup.deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
                        print(json.dumps({'status': 'started', 'snapshot': str(backup.snapshot),
                                          'checkpoint_available': True, 'mode': run_mode}),
                              file=sys.stderr, flush=True)
                        def request_pause(signum, frame):
                            backup.pause_requested = True
                        old_handlers = {sig: signal.signal(sig, request_pause)
                                        for sig in (signal.SIGINT, signal.SIGTERM)}
                        try:
                            result = backup.run()
                        finally:
                            for sig, handler in old_handlers.items():
                                signal.signal(sig, handler)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] in ('verified', 'ready', 'configured', 'archived',
                                         'archived_with_warnings', 'running', 'paused', 'pause_requested') else 2
    except BackupError as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        # Never dump raw response bodies, token values or URLs in a traceback.
        print(json.dumps({'status': 'failed', 'error': 'local_io_or_invalid_data',
                          'hint': 'Check configuration and any running snapshot; previous snapshots are retained.'}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
