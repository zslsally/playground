"""Behavior tests for configured intake; no personal files or network access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

SCRIPT = Path(__file__).with_name('run_backup.py')
A, B = 'a' * 32, 'b' * 32


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / 'backups'
        self.cfg = self.root / 'config.local.json'
        self.cfg.write_text(json.dumps({'workspace': {'name': 'Synthetic', 'id': 'test'},
            'destination': str(self.out), 'scope': 'whole workspace', 'format': 'markdown',
            'python': sys.executable, 'search_dirs': [str(self.root)], 'settle_seconds': 0}))

    def zip(self, label, notes=None):
        path = self.root / ('test_Export-' + label + '.zip')
        with zipfile.ZipFile(path, 'w') as z:
            for name, body in (notes or [(f'Note {A}.md', '# note\n[Missing](absent.csv)')]):
                z.writestr('Export-' + label + '/' + name, body)
        return path

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), '--config', str(self.cfg), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_single_command_and_dedup(self):
        source = self.zip('one')
        first = self.cli('run')
        self.assertEqual(first['status'], 'archived')
        self.assertTrue(first['fresh_backup'])
        self.assertEqual(first['checks']['new_link_notices'], 1)
        path = Path(first['snapshot'])
        self.assertEqual((path / 'export.zip').read_bytes(), source.read_bytes())
        self.assertTrue((path / 'local-verification.md').exists())
        self.assertEqual((path / 'export.zip').stat().st_mode & 0o777, 0o600)
        before = (path / 'manifest.json').read_bytes()
        second = self.cli('run')
        self.assertEqual(second['status'], 'already_archived')
        self.assertFalse(second['fresh_backup'])
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 1)
        self.assertEqual((path / 'manifest.json').read_bytes(), before)

    def test_known_links_and_changed_source_hash(self):
        p1 = self.zip('one')
        self.cli('run', '--source', p1)
        p2 = self.zip('two')
        second = self.cli('run', '--source', p2)
        self.assertEqual(second['checks']['previous_link_notices'], 1)
        self.assertEqual(second['checks']['new_link_notices'], 0)
        p3 = self.zip('three', [(f'Note {A}.md', '# changed\n[Missing](absent.csv)')])
        third = self.cli('run', '--source', p3)
        self.assertEqual(third['checks']['new_link_notices'], 1)
        self.assertEqual(third['checks']['changed_ids'], 1)

    def test_missing_ids_not_promoted_as_baseline(self):
        p1 = self.zip('one', [(f'A {A}.md', 'a'), (f'B {B}.md', 'b')])
        self.cli('run', '--source', p1)
        p2 = self.zip('two', [(f'A {A}.md', 'a')])
        second = self.cli('run', '--source', p2)
        self.assertEqual(second['checks']['missing_ids'], 1)
        p3 = self.zip('three', [(f'A {A}.md', 'a')])
        third = self.cli('run', '--source', p3)
        self.assertEqual(third['checks']['missing_ids'], 1)

    def test_dry_run_no_writes_and_ambiguity(self):
        p1 = self.zip('one')
        report = self.cli('run', '--dry-run')
        self.assertEqual(report['status'], 'dry_run_verified')
        self.assertFalse(self.out.exists())
        self.zip('two')
        self.assertEqual(self.cli('run')['status'], 'choose_source')
        self.assertFalse(self.out.exists())
        self.cli('run', '--source', p1)
        snapshot = next(self.out.glob('*/manifest.json'))
        before = snapshot.read_bytes()
        self.cli('run', '--source', p1, '--dry-run', '--force-audit')
        self.assertEqual(snapshot.read_bytes(), before)

    def test_pending_ignores_old_files_and_completes_once(self):
        old = self.zip('old')
        os.utime(old, (100, 100))
        self.cli('requested')
        again = self.cli('requested')
        self.assertFalse(again['new_request_recorded'])
        self.assertEqual(self.cli('run')['status'], 'export_pending')
        self.zip('new')
        result = self.cli('run')
        self.assertEqual(result['status'], 'archived')
        self.assertFalse((self.out / '.export-pending.json').exists())
        self.assertTrue((Path(result['snapshot']) / 'export-request.json').exists())

    def test_corrupt_zip_single_failure_json(self):
        path = self.root / 'test_Export-broken.zip'
        path.write_bytes(b'not a ZIP')
        result = self.cli('run', ok=False)
        self.assertEqual(result['status'], 'failed')
        snapshot = Path(result['snapshot'])
        self.assertEqual(json.loads((snapshot / 'manifest.json').read_text())['status'], 'failed')
        self.assertTrue(path.exists())

    def test_resume_missing_audit_report(self):
        source = self.zip('one')
        first = self.cli('run')
        path = Path(first['snapshot'])
        (path / 'local-verification.json').unlink()
        result = self.cli('run')
        self.assertEqual(result['snapshot'], first['snapshot'])
        self.assertTrue((path / 'local-verification.json').exists())
        self.assertEqual(len(list(self.out.glob('*/manifest.json'))), 1)
        self.assertEqual((path / 'export.zip').read_bytes(), source.read_bytes())

    def test_duplicate_corruption_is_detected(self):
        self.zip('one')
        first = self.cli('run')
        archive = Path(first['snapshot']) / 'export.zip'
        archive.write_bytes(b'corrupt')
        result = self.cli('run', ok=False)
        self.assertEqual(result['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
