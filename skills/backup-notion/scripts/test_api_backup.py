"""Offline behavioral tests. Synthetic content, no Notion credentials or network."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from api_backup import (Backup, check, lock, previous_manifest, successful_manifest, verify,
                        needs_property_fetch, destination_for)
import notion_checkpoint as checkpoint
from notion_api import APIError, BackupError, Client, paginate
from notion_assets import Assets, file_objects, hosted_url

A, B, D, S = 'a' * 32, 'b' * 32, 'd' * 32, 'e' * 32
URL = 'https://prod-files-secure.s3.us-west-2.amazonaws.com/test/photo?secret=SIGNED'


def listing(items, cursor=None):
    return {'object': 'list', 'results': items, 'has_more': cursor is not None, 'next_cursor': cursor}


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, missing=False, comments_denied=False, truncated=False):
        self.requests = 0
        self.missing, self.comments_denied, self.truncated = missing, comments_denied, truncated
        self.calls = []

    def request(self, method, path, params=None):
        self.requests += 1
        self.calls.append((method, path, params))
        if path == '/users/me':
            return {'object': 'user', 'type': 'bot', 'id': 'f' * 32}
        if path == '/search':
            if not params.get('start_cursor'):
                return listing([{'object': 'page', 'id': A}], 'page2')
            return listing([] if self.missing else [{'object': 'data_source', 'id': S}])
        if path == '/comments':
            if self.comments_denied:
                raise APIError(403)
            return listing([])
        if path == '/databases/' + D:
            return {'object': 'database', 'id': D, 'data_sources': [{'id': S}]}
        if path == '/data_sources/' + S:
            return {'object': 'data_source', 'id': S, 'parent': {'type': 'database_id', 'database_id': D}}
        if path == '/data_sources/' + S + '/query':
            return listing([] if self.missing else [{'object': 'page', 'id': B, 'properties': {}}])
        if path.startswith('/pages/'):
            ident = path.split('/')[2]
            if self.missing and ident == B:
                raise APIError(404)
            if '/properties/' in path:
                if not params.get('start_cursor'):
                    return listing([{'object': 'property_item', 'type': 'relation', 'relation': {'id': B}}], 'prop2')
                return {**listing([]), 'property_item': {'rollup': {'type': 'number', 'number': 42}}}
            if path.endswith('/markdown'):
                return {'object': 'page_markdown', 'id': ident, 'markdown': '# Synthetic\n![Photo](' + URL + ')',
                        'truncated': self.truncated, 'unknown_block_ids': [D] if self.truncated else []}
            return {'object': 'page', 'id': ident, 'last_edited_time': '2026-01-01',
                    'properties': {'Relations': {'id': 'x%3Ay', 'type': 'relation', 'relation': []}},
                    'cover': {'type': 'file', 'file': {'url': URL, 'expiry_time': 'tomorrow'}},
                    'future_unknown_field': {'keep': True}}
        if path.startswith('/blocks/'):
            ident = path.split('/')[2]
            return listing([{'object': 'block', 'id': B, 'type': 'child_page', 'has_children': True}]
                           if ident == A and not self.missing else [])
        raise AssertionError((method, path, params))


class TransportTests(unittest.TestCase):
    def test_limiter_counts_network_time_instead_of_adding_sleep(self):
        clock = [0.0]
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        client = Client('secret', opener=Opener(*[Response(b'{}') for _ in range(3)]),
                        sleep=sleep, clock=lambda: clock[0])
        client.request('GET', '/users/me')
        self.assertEqual(sleeps, [])
        clock[0] += .15
        client.request('GET', '/users/me')
        self.assertAlmostEqual(sleeps[0], .25)
        clock[0] += .8
        client.request('GET', '/users/me')
        self.assertEqual(len(sleeps), 1)

    def test_property_optimization_preserves_uncertain_values(self):
        plain = {'type': 'text', 'text': {'content': 'Synthetic'}}
        self.assertFalse(needs_property_fetch({'type': 'title', 'title': [plain]}))
        self.assertTrue(needs_property_fetch({'type': 'title', 'title': [plain] * 25}))
        self.assertTrue(needs_property_fetch({'type': 'title', 'title': [{'type': 'mention'}]}))
        self.assertTrue(needs_property_fetch({'type': 'title'}))
        self.assertFalse(needs_property_fetch({'type': 'relation', 'has_more': False}))
        for prop in ({'type': 'relation'}, {'type': 'relation', 'has_more': True},
                     {'type': 'formula'}, {'type': 'rollup'}, {'type': 'people'}):
            self.assertTrue(needs_property_fetch(prop))

    def test_read_allowlist_retry_and_no_secret_errors(self):
        sleep = []
        opener = Opener(HTTPError('sensitive-url', 429, 'secret', {'Retry-After': '2'}, io.BytesIO()),
                        Response(b'{"object":"user"}'))
        client = Client('test-secret', opener=opener, sleep=sleep.append)
        self.assertEqual(client.request('GET', '/users/me')['object'], 'user')
        self.assertEqual(client.requests, 2)
        self.assertIn(2, sleep)
        for method, path in [('PATCH', '/pages/' + A), ('POST', '/pages'),
                             ('GET', '/users/me?token=secret'), ('GET', '//evil.test')]:
            with self.assertRaisesRegex(BackupError, 'non_read_endpoint_refused'):
                client.request(method, path)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0].get_header('Notion-version'), '2026-03-11')

    def test_pagination_cycle_retains_responses_and_fails(self):
        class Repeated:
            def request(self, *args):
                return listing([{'id': A}], 'same')
        saved = []
        with self.assertRaisesRegex(BackupError, 'repeated_cursor'):
            list(paginate(Repeated(), 'POST', '/search', save=saved.append))
        self.assertEqual(len(saved), 2)

    def test_retry_after_not_silently_shortened(self):
        client = Client('secret', opener=Opener(HTTPError('url', 429, '', {'Retry-After': '120'}, io.BytesIO())),
                        sleep=lambda _: None)
        with self.assertRaisesRegex(BackupError, 'requires_later_run'):
            client.request('GET', '/users/me')


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = {'workspace': {'name': 'Synthetic'}, 'api': {
            'destination': str(self.root / 'out'), 'roots': [{'type': 'page', 'id': A}], 'comments': 'page',
            'database_pages': 'full'}}

    def test_default_links_mode_skips_rows_descendants_and_all_row_assets(self):
        c, f = 'c' * 32, 'f' * 32
        class Library(FakeClient):
            def request(self, method, path, params=None):
                rows = [{'object': 'page', 'id': ident, 'parent': {'type': 'data_source_id', 'data_source_id': S},
                         'properties': {'URL': {'type': 'url', 'url': 'https://example.com/original' if ident == B else None}},
                         'cover': {'type': 'file', 'file': {'url': URL + '-library'}}} for ident in (B, c)]
                if path == '/search':
                    self.requests += 1; self.calls.append((method, path, params))
                    return listing([{'object': 'page', 'id': f, 'parent': {'type': 'page_id', 'page_id': B}}] + rows +
                                   [{'object': 'data_source', 'id': S}])
                if path == '/data_sources/' + S + '/query':
                    self.requests += 1; self.calls.append((method, path, params))
                    return listing(rows)
                return super().request(method, path, params)
        self.cfg['api'].pop('database_pages')
        client = Library()
        backup, result = self.run_backup(client)
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['pages'], 3)
        self.assertEqual(result['link_only_pages'], 1)
        self.assertEqual(result['link_only_descendants'], 0)
        for _, path, params in client.calls:
            self.assertFalse(any(ident in path for ident in (B,)))
            self.assertFalse(path == '/comments' and params['block_id'] in (B,))
        links = json.loads((backup.snapshot / 'links.json').read_text())
        self.assertEqual(len(links['pages']), 1)
        self.assertEqual(set(links['pages'][0]), {'id', 'urls'})
        self.assertEqual(links['pages'][0]['urls'], {'URL': 'https://example.com/original'})
        self.assertTrue(any(path == '/pages/' + c + '/markdown' for _, path, _ in client.calls))
        self.assertTrue(any(path == '/pages/' + f + '/markdown' for _, path, _ in client.calls))
        self.assertEqual(len(list((backup.snapshot / 'assets').iterdir())), 1)
        self.assertEqual(verify(backup.snapshot)['status'], 'verified')
        known = json.loads((backup.snapshot / 'manifest.json').read_text())['known_entities']
        self.assertIn(['page', B], known)
        self.assertIn(['page', c], known)
        self.assertEqual(destination_for(self.cfg).name, 'database-links-v2')

    def test_link_state_survives_pause_resume_and_modes_have_distinct_series(self):
        from api_backup import series
        self.cfg['api']['database_pages'] = 'links'
        backup = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets)
        backup.remember_page({'object': 'page', 'id': B, 'parent': {'type': 'data_source_id', 'data_source_id': S},
                              'properties': {}})
        checkpoint.save(backup, __import__('api_backup').write_json)
        backup.pause_requested = True
        self.assertEqual(backup.run()['status'], 'paused')
        resumed = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets, resume=backup.snapshot)
        self.assertNotIn(B, resumed.link_pages)
        resumed.assets.close()
        links_series = series(self.cfg)
        self.cfg['api']['database_pages'] = 'full'
        self.assertNotEqual(links_series, series(self.cfg))

    def test_unindexed_database_parent_is_resolved_before_child_content_or_assets(self):
        f = 'f' * 32
        class UnindexedParent(FakeClient):
            def request(self, method, path, params=None):
                if path == '/search':
                    self.requests += 1; self.calls.append((method, path, params))
                    return listing([{'object': 'page', 'id': f, 'parent': {'type': 'page_id', 'page_id': B}}])
                if path in ('/pages/' + f, '/pages/' + B):
                    self.requests += 1; self.calls.append((method, path, params))
                    return {'object': 'page', 'id': f if path.endswith(f) else B,
                            'parent': {'type': 'page_id', 'page_id': B} if path.endswith(f) else
                                      {'type': 'data_source_id', 'data_source_id': S},
                            'properties': {}, 'cover': {'type': 'file', 'file': {'url': URL + '-excluded'}}}
                return super().request(method, path, params)
        self.cfg['api']['database_pages'] = 'links'
        backup, result = self.run_backup(UnindexedParent())
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['pages'], 3)
        self.assertEqual(result['link_only_descendants'], 0)
        self.assertTrue(any(path == '/pages/' + f + '/markdown' for _, path, _ in backup.client.calls))
        self.assertEqual(len(list((backup.snapshot / 'assets').iterdir())), 1)

    def assets(self, store, snapshot, max_bytes):
        return Assets(store, snapshot, max_bytes,
                      opener=Opener(*[Response(b'photo-data', {'Content-Type': 'image/png'})
                                      for _ in range(20)]), workers=1)

    def paused_backup(self, abrupt=False):
        class Interrupted(FakeClient):
            def request(self, method, path, params=None):
                if path == '/pages/' + B + '/markdown':
                    if abrupt:
                        raise RuntimeError('simulated_process_crash')
                    raise checkpoint.PauseRequested()
                return super().request(method, path, params)
        backup = Backup(self.cfg, Interrupted(), self.root / 'out', assets_factory=self.assets,
                        run_mode='audit')
        if abrupt:
            with self.assertRaisesRegex(RuntimeError, 'simulated_process_crash'):
                backup.run()
        else:
            result = backup.run()
            self.assertEqual(result['status'], 'paused')
        return backup

    def test_pause_resume_skips_completed_pages_preserves_raw_and_attachments(self):
        backup = self.paused_backup()
        before = {p.name: p.read_bytes() for p in (backup.snapshot / 'raw').glob('*.json')}
        client = FakeClient()
        resumed = Backup(self.cfg, client, self.root / 'out', assets_factory=self.assets,
                         resume=backup.snapshot, run_mode='audit')
        result = resumed.run()
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['pages'], 2)
        self.assertFalse(any(path.startswith('/pages/' + A) for _, path, _ in client.calls))
        self.assertFalse(any(path == '/search' for _, path, _ in client.calls))
        self.assertTrue(any(path == '/blocks/' + B + '/children' for _, path, _ in client.calls))
        for name, content in before.items():
            self.assertEqual((backup.snapshot / 'raw' / name).read_bytes(), content)
        self.assertIn('../assets/', (backup.snapshot / 'notes' / (A + '.md')).read_text())
        self.assertEqual(verify(backup.snapshot)['status'], 'verified')

    def test_crash_resume_and_corruption_guard(self):
        backup = self.paused_backup(abrupt=True)
        resumed = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets,
                         resume=backup.snapshot, run_mode='audit')
        self.assertEqual(resumed.run()['status'], 'archived')
        other = self.paused_backup()
        target = next((other.snapshot / 'assets').iterdir())
        target.write_bytes(b'corrupt')
        with self.assertRaisesRegex(BackupError, 'attachment_integrity_failed'):
            Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets,
                   resume=other.snapshot, run_mode='audit')

    def test_pause_marker_and_time_budget_stop_without_success_pointer(self):
        backup = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets)
        (backup.snapshot / '.pause-request').write_text('{}')
        self.assertEqual(backup.run()['status'], 'paused')
        self.assertEqual(backup.client.requests, 0)
        self.assertFalse((self.root / 'out/latest-success.json').exists())
        resumed = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets, resume=backup.snapshot)
        resumed.deadline = time.monotonic() - 1
        self.assertEqual(resumed.run()['status'], 'paused')
        self.assertEqual(resumed.client.requests, 0)

    def test_legacy_resume_refused_without_modifying_original(self):
        backup = Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets)
        (backup.snapshot / '.work/checkpoint.json').unlink()
        original = (backup.snapshot / 'manifest.json').read_bytes()
        with self.assertRaisesRegex(BackupError, 'legacy_snapshot_has_no_checkpoint'):
            Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets,
                   resume=backup.snapshot, run_mode='daily')
        self.assertEqual((backup.snapshot / 'manifest.json').read_bytes(), original)

    def test_resume_rejects_corrupt_raw_and_scope_change(self):
        backup = self.paused_backup()
        self.cfg['api']['database_pages'] = 'links'
        with self.assertRaisesRegex(BackupError, 'scope_mismatch'):
            Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets,
                   resume=backup.snapshot, run_mode='audit')
        self.cfg['api']['database_pages'] = 'full'
        raw = backup.snapshot / 'raw/0000001.json'
        raw.write_text('{}')
        with self.assertRaisesRegex(BackupError, 'file_integrity_failed'):
            Backup(self.cfg, FakeClient(), self.root / 'out', assets_factory=self.assets,
                   resume=backup.snapshot, run_mode='audit')

    def test_cli_progress_without_token_and_new_run_refuses_unfinished(self):
        backup = self.paused_backup()
        script = str(Path(__file__).with_name('api_backup.py'))
        environment = dict(os.environ)
        environment.pop('NOTION_BACKUP_TOKEN', None)
        result = subprocess.run([sys.executable, script, 'progress', str(backup.snapshot)],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['status'], 'paused')
        config_path = self.root / 'config.local.json'
        config_path.write_text(json.dumps(self.cfg))
        config_path.chmod(0o600)
        environment['NOTION_BACKUP_TOKEN'] = 'synthetic-not-sent'
        result = subprocess.run([sys.executable, script, '--config', str(config_path), 'run'],
                                env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)['status'], 'needs_resume')
        self.assertEqual(len(list((self.root / 'out/snapshots').iterdir())), 1)

    def run_backup(self, client=None, prior=None, run_mode='audit', baseline=None):
        backup = Backup(self.cfg, client or FakeClient(), self.root / 'out', prior, self.assets,
                        run_mode=run_mode, baseline=baseline)
        report = backup.run()
        return backup, report

    def test_complete_paginated_recursive_snapshot_and_offline_files(self):
        backup, result = self.run_backup()
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['pages'], 2)
        self.assertEqual(result['entities'], 4)
        self.assertEqual(verify(backup.snapshot)['status'], 'verified')
        all_raw = [json.loads(p.read_text()) for p in (backup.snapshot / 'raw').glob('*.json')]
        self.assertTrue(any(r['response'].get('future_unknown_field') == {'keep': True} for r in all_raw))
        self.assertTrue(any(r['response'].get('property_item', {}).get('rollup', {}).get('number') == 42 for r in all_raw))
        self.assertTrue(any('/properties/x%3Ay' in r['path'] for r in all_raw))
        body = (backup.snapshot / 'notes' / (A + '.md')).read_text()
        self.assertNotIn('SIGNED', body)
        self.assertIn('../assets/', body)
        self.assertEqual(len(list((backup.snapshot / 'assets').iterdir())), 1)
        self.assertTrue((backup.snapshot / 'databases' / (S + '.csv')).exists())

    def test_prior_missing_id_retried_and_success_pointer_retained(self):
        first, _ = self.run_backup()
        destination = self.root / 'out'
        prior = previous_manifest(destination, self.cfg)
        before = (destination / 'latest-success.json').read_bytes()
        second, report = self.run_backup(FakeClient(missing=True), prior)
        self.assertEqual(report['status'], 'partial')
        self.assertIn(('GET', '/pages/' + B, None), second.client.calls)
        self.assertEqual((destination / 'latest-success.json').read_bytes(), before)
        self.assertIn(['page', B], json.loads((second.snapshot / 'manifest.json').read_text())['known_entities'])
        self.assertEqual(verify(first.snapshot)['status'], 'verified')

    def test_comments_denied_and_truncated_markdown_are_visible(self):
        backup, report = self.run_backup(FakeClient(comments_denied=True, truncated=True))
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(sum(path == '/comments' for _, path, _ in backup.client.calls), 1)
        detail = json.loads((backup.snapshot / 'report.json').read_text())
        self.assertIn('markdown_incomplete_raw_blocks_retained', [w['code'] for w in detail['warnings']])

    def test_corrupted_snapshot_and_manifest_fail_verification(self):
        backup, _ = self.run_backup()
        note = next((backup.snapshot / 'notes').glob('*.md'))
        note.write_text('corrupted')
        with self.assertRaisesRegex(BackupError, 'integrity'):
            verify(backup.snapshot)
        (backup.snapshot / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(BackupError, 'previous_manifest_integrity'):
            previous_manifest(self.root / 'out', self.cfg)

    def test_repeated_run_deduplicates_storage_and_scope_guard(self):
        first, _ = self.run_backup()
        second, _ = self.run_backup(prior=previous_manifest(self.root / 'out', self.cfg))
        asset1 = next((first.snapshot / 'assets').iterdir())
        asset2 = next((second.snapshot / 'assets').iterdir())
        self.assertEqual(asset1.stat().st_ino, asset2.stat().st_ino)
        self.cfg['api']['roots'].append({'type': 'page', 'id': 'c' * 32})
        with self.assertRaisesRegex(BackupError, 'scope_changed'):
            previous_manifest(self.root / 'out', self.cfg)

    def test_daily_incremental_reuses_unchanged_pages_and_audit_refreshes(self):
        first, _ = self.run_backup(run_mode='audit')
        destination = self.root / 'out'
        baseline = successful_manifest(destination, self.cfg)
        prior = previous_manifest(destination, self.cfg)
        client = FakeClient()
        daily, result = self.run_backup(client, prior, run_mode='daily', baseline=baseline)
        self.assertEqual(result['status'], 'archived')
        self.assertEqual(result['reused_pages'], 2)
        self.assertEqual(result['refreshed_pages'], 0)
        self.assertEqual(result['reuse_fallbacks'], 0)
        self.assertFalse(any(path.endswith('/markdown') or path.startswith('/blocks/') or path == '/comments'
                             for _, path, _ in client.calls))
        self.assertNotEqual((first.snapshot / 'notes' / (A + '.md')).stat().st_ino,
                            (daily.snapshot / 'notes' / (A + '.md')).stat().st_ino)
        self.assertEqual(next((first.snapshot / 'assets').iterdir()).stat().st_ino,
                         next((daily.snapshot / 'assets').iterdir()).stat().st_ino)
        self.assertEqual(verify(daily.snapshot)['status'], 'verified')

        audit_client = FakeClient()
        audit, audited = self.run_backup(audit_client, previous_manifest(destination, self.cfg),
                                         run_mode='audit')
        self.assertEqual(audited['reused_pages'], 0)
        self.assertEqual(audited['refreshed_pages'], 2)
        self.assertTrue(any(path.endswith('/markdown') for _, path, _ in audit_client.calls))
        self.assertTrue(any(path == '/comments' for _, path, _ in audit_client.calls))
        self.assertEqual(verify(audit.snapshot)['status'], 'verified')

    def test_daily_refreshes_only_changed_page_and_bad_baseline_falls_back(self):
        first, _ = self.run_backup(run_mode='audit')
        destination = self.root / 'out'
        baseline = successful_manifest(destination, self.cfg)

        class OneChanged(FakeClient):
            def request(self, method, path, params=None):
                result = super().request(method, path, params)
                if path == '/pages/' + B and '/properties/' not in path and not path.endswith('/markdown'):
                    result['last_edited_time'] = '2026-02-02'
                return result

        client = OneChanged()
        daily, result = self.run_backup(client, previous_manifest(destination, self.cfg),
                                        run_mode='daily', baseline=baseline)
        self.assertEqual(result['reused_pages'], 1)
        self.assertEqual(result['refreshed_pages'], 1)
        self.assertFalse(any(path in ('/pages/' + A + '/markdown', '/blocks/' + A + '/children')
                             for _, path, _ in client.calls))
        self.assertTrue(any(path == '/pages/' + B + '/markdown' for _, path, _ in client.calls))
        self.assertEqual(verify(daily.snapshot)['status'], 'verified')

        clean_baseline = successful_manifest(destination, self.cfg)
        record = clean_baseline['entity_files']['page:' + A][0]['path']
        (Path(clean_baseline['_snapshot_path']) / record).write_text('corrupt')
        fallback, fallback_result = self.run_backup(FakeClient(), previous_manifest(destination, self.cfg),
                                                    run_mode='daily', baseline=clean_baseline)
        self.assertEqual(fallback_result['status'], 'archived')
        self.assertEqual(fallback_result['reuse_fallbacks'], 1)
        self.assertGreaterEqual(fallback_result['refreshed_pages'], 1)
        self.assertEqual(verify(fallback.snapshot)['status'], 'verified')

    def test_lock_prevents_overlap(self):
        with lock(self.root / 'out'):
            with self.assertRaisesRegex(BackupError, 'already_running'):
                with lock(self.root / 'out'):
                    pass

    def test_cli_missing_token_no_snapshot_and_private_files(self):
        path = self.root / 'config.local.json'
        path.write_text(json.dumps(self.cfg))
        path.chmod(0o600)
        environment = dict(os.environ)
        environment.pop('NOTION_BACKUP_TOKEN', None)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('api_backup.py')),
                                 '--config', str(path), 'run'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'needs_credential')
        self.assertFalse((self.root / 'out').exists())
        path.chmod(0o644)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('api_backup.py')),
                                 '--config', str(path), 'status'], env=environment, capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['error'], 'config_requires_mode_600')


class AssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_comment_attachment_without_type_is_downloadable(self):
        comment = {'object': 'comment', 'attachments': [
            {'category': 'image', 'file': {'url': URL, 'expiry_time': 'tomorrow'}}]}
        self.assertEqual(list(file_objects(comment)), [('file', URL)])
        self.assertEqual(list(file_objects({'type': 'bookmark', 'bookmark': {'url': URL}})), [])

    def test_downloads_overlap_and_duplicate_content_commits_safely(self):
        barrier = threading.Barrier(2)
        guard = threading.Lock()
        class ParallelOpener:
            def __init__(self):
                self.count = 0
            def open(self, request, timeout):
                with guard:
                    self.count += 1
                barrier.wait(timeout=5)
                return Response(b'identical-content', {'Content-Type': 'image/png'})
        opener = ParallelOpener()
        assets = Assets(self.root / 'store', self.root / 'snapshot', opener=opener, workers=2)
        try:
            assets.collect([{'type': 'file', 'file': {'url': URL}},
                            {'type': 'file', 'file': {'url': URL}},
                            {'type': 'file', 'file': {'url': URL + '2'}}])
            report = assets.summary()
            self.assertEqual(report['failures'], [])
            self.assertEqual(report['unique_files'], 1)
            self.assertEqual(opener.count, 2)
        finally:
            assets.close()

    def test_redirect_revalidated_and_bearer_never_sent(self):
        opener = Opener(HTTPError(URL, 302, '', {'Location': 'http://127.0.0.1/secret'}, io.BytesIO()))
        assets = Assets(self.root / 'store', self.root / 'snapshot', opener=opener)
        assets.collect({'type': 'file', 'file': {'url': URL}})
        self.assertEqual(assets.summary()['failures'][0]['error'], 'attachment_host_not_allowed')
        self.assertIsNone(opener.requests[0].get_header('Authorization'))
        self.assertNotIn('SIGNED', json.dumps(assets.summary()))
        self.assertFalse(hosted_url('https://notion.so.evil.test/file'))

    def test_size_limit_cleanup_and_external_policy(self):
        assets = Assets(self.root / 'store', self.root / 'snapshot', max_bytes=3,
                        opener=Opener(Response(b'1234')))
        assets.collect([{'type': 'file', 'file': {'url': URL}},
                        {'type': 'external', 'external': {'url': 'https://example.com/file'}}])
        self.assertEqual(assets.summary()['external_urls_retained_only'], 1)
        self.assertEqual(assets.summary()['failures'][0]['error'], 'attachment_size_limit')
        self.assertEqual(list((self.root / 'store').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
