"""Offline behavior checks; all synthetic data stays in temporary directories."""

import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).with_name('backup_archive.py')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.zip'
        self.destination = self.root / 'snapshots'

    def make_zip(self, files):
        with zipfile.ZipFile(self.source, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, content in files:
                archive.writestr(name, content)

    def run_cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None

    def snapshot(self, *extra, success=True):
        return self.run_cli('snapshot', '--source', self.source, '--destination', self.destination,
                            '--scope', 'Synthetic workspace', '--format', 'markdown', *extra, success=success)

    def test_nested_unicode_csv_and_second_copy(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('笔记.md', '# 原始文本\n![图片](图.png)')
            archive.writestr('图.png', b'example attachment bytes')
            archive.writestr('表.csv', '\ufeffName,Text\nA,"line 1\nline 2"\nB,"x,y"\n')
        self.make_zip([('Export.zip', inner.getvalue())])
        original = self.source.read_bytes()
        report = self.snapshot()
        folder = Path(report['snapshot'])
        self.assertEqual(report['files'], 4)
        self.assertEqual(report['csv_records'], 2)
        self.assertEqual(report['warnings'], 1)
        self.assertEqual((folder / 'export.zip').read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.run_cli('verify', folder)['status'], 'checksum_verified')
        second = self.snapshot()
        self.assertNotEqual(report['snapshot'], second['snapshot'])
        self.assertEqual((folder / 'export.zip').read_bytes(), original)
        self.assertEqual(report['coverage'], 'not_checked')

    def test_dry_run_creates_nothing(self):
        self.make_zip([('a.md', '# note')])
        self.assertTrue(self.snapshot('--dry-run')['dry_run'])
        self.assertFalse(self.destination.exists())

    def test_tampering_detected(self):
        self.make_zip([('a.md', 'original')])
        folder = Path(self.snapshot()['snapshot'])
        archive = folder / 'export.zip'
        data = bytearray(archive.read_bytes())
        data[data.index(b'original')] ^= 1
        archive.write_bytes(data)
        self.run_cli('verify', folder, success=False)

    def test_unsafe_and_colliding_members_rejected(self):
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for members in ([('../escape.md', 'x')], [('A.md', 'a'), ('a.md', 'b')], [(link, '/tmp')]):
            with self.subTest(members=members):
                self.make_zip(members)
                report = self.snapshot(success=False)
                folder = Path(report['snapshot'])
                self.assertEqual(json.loads((folder / 'manifest.json').read_text())['status'], 'failed')
                self.assertTrue((folder / 'export.zip').exists())
                self.assertTrue(self.source.exists())
        self.assertFalse((self.root / 'escape.md').exists())

    def test_crc_empty_and_limits_rejected(self):
        self.make_zip([('a.md', 'original')])
        data = bytearray(self.source.read_bytes())
        data[data.index(b'original')] ^= 1
        self.source.write_bytes(data)
        self.snapshot(success=False)
        self.make_zip([])
        self.snapshot(success=False)
        self.make_zip([('a.md', 'too large'), ('b.md', 'other')])
        self.snapshot('--max-bytes', '2', success=False)
        self.snapshot('--max-files', '1', success=False)

    def test_warnings_are_not_coverage_success(self):
        self.make_zip([('empty.txt', ''), ('bad.csv', b'\xff')])
        report = self.snapshot()
        manifest = json.loads((Path(report['snapshot']) / 'manifest.json').read_text())
        self.assertEqual({w['kind'] for w in manifest['warnings']}, {'empty_file', 'csv_parse_failed'})
        self.assertEqual(manifest['coverage'], 'not_checked')
        self.assertEqual(manifest['restore'], 'not_tested')


if __name__ == '__main__':
    unittest.main()
