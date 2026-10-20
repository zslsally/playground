#!/usr/bin/env python3
"""Preserve and inspect Notion export ZIPs without extracting archive paths."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import unicodedata
import zipfile


CHUNK = 1024 * 1024


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def inspect_archive(path, max_bytes, max_files):
    entries, warnings = [], []
    budget = {'bytes': 0, 'files': 0}

    def walk(source, chain, depth):
        if depth > 4:
            raise ValueError('Nested ZIP depth exceeds 4')
        with zipfile.ZipFile(source) as archive:
            names = set()
            for info in archive.infolist():
                name = info.filename
                parts = PurePosixPath(name).parts
                if (not parts or name.startswith('/') or '\\' in name
                        or '..' in parts or ':' in parts[0] or '\x00' in name):
                    raise ValueError('Unsafe archive member path')
                normalized = unicodedata.normalize('NFC', name.rstrip('/')).casefold()
                if normalized in names:
                    raise ValueError('Duplicate or case-colliding archive member')
                names.add(normalized)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError('Archive contains a symbolic link')
                if info.flag_bits & 1:
                    raise ValueError('Encrypted archive is not supported')
                budget['files'] += 1
                if budget['files'] > max_files:
                    raise ValueError('Archive exceeds entry limit')
                if info.is_dir():
                    continue
                if budget['bytes'] + info.file_size > max_bytes:
                    raise ValueError('Archive exceeds expanded byte limit')
                extension = PurePosixPath(name).suffix.lower()
                # Spooling keeps large CSVs and nested ZIPs off the Python heap.
                with tempfile.SpooledTemporaryFile(max_size=8 * CHUNK) as spool:
                    h, size = hashlib.sha256(), 0
                    with archive.open(info) as member:
                        for chunk in iter(lambda: member.read(CHUNK), b''):
                            size += len(chunk)
                            budget['bytes'] += len(chunk)
                            if budget['bytes'] > max_bytes:
                                raise ValueError('Archive exceeds expanded byte limit')
                            h.update(chunk)
                            if extension in ('.csv', '.zip'):
                                spool.write(chunk)
                    if size != info.file_size:
                        raise ValueError('Archive member size mismatch')
                    entry = {'path': chain + [name], 'bytes': size,
                             'sha256': h.hexdigest(), 'extension': extension}
                    entries.append(entry)
                    if size == 0:
                        warnings.append({'kind': 'empty_file', 'path': entry['path']})
                    if extension == '.zip':
                        warnings.append({'kind': 'nested_zip_inspected', 'path': entry['path']})
                        spool.seek(0)
                        walk(spool, chain + [name], depth + 1)
                    elif extension == '.csv':
                        spool.seek(0)
                        wrapper = io.TextIOWrapper(spool, encoding='utf-8-sig', newline='')
                        try:
                            reader = csv.reader(wrapper, strict=True)
                            header = next(reader, None)
                            entry['csv_records'] = sum(1 for _ in reader) if header is not None else 0
                        except (UnicodeError, csv.Error):
                            warnings.append({'kind': 'csv_parse_failed', 'path': entry['path']})
                        finally:
                            wrapper.detach()

    csv.field_size_limit(max_bytes)
    walk(path, [], 0)
    counts = dict(Counter(e['extension'] or '(no extension)' for e in entries))
    return entries, warnings, counts


def save_manifest(directory, manifest):
    temp = directory / 'manifest.json.tmp'
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(directory / 'manifest.json')


def snapshot(args):
    source = args.source.expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError('Source must be a ZIP file')
    created = datetime.now(timezone.utc)
    manifest = {'schema_version': 1, 'created_at': created.isoformat(),
                'scope': args.scope, 'format': args.format, 'status': 'incomplete',
                'coverage': 'not_checked', 'restore': 'not_tested'}
    directory = None
    try:
        before = digest(source)
        target = source
        if not args.dry_run:
            destination = args.destination.expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix=created.strftime('%Y-%m-%dT%H%M%SZ-'), dir=destination))
            save_manifest(directory, manifest)
            target = directory / 'export.zip'
            shutil.copyfile(source, target)
        copied = digest(target)
        if copied != before or digest(source) != before:
            raise ValueError('Source changed during backup or copy hash mismatch')
        entries, warnings, counts = inspect_archive(target, args.max_bytes, args.max_files)
        if digest(target) != copied:
            raise ValueError('Archive changed during inspection')
        if not entries:
            raise ValueError('Archive has no files')
        expected = {'markdown': {'.md', '.csv'}, 'html': {'.html', '.htm'}, 'pdf': {'.pdf'}}[args.format]
        if not expected.intersection(counts):
            warnings.append({'kind': 'selected_format_not_found'})
        manifest.update(status='archive_verified', archive={'bytes': target.stat().st_size, 'sha256': copied},
                        files=entries, counts_by_extension=counts, warnings=warnings,
                        csv_records=sum(e.get('csv_records', 0) for e in entries))
        if directory:
            save_manifest(directory, manifest)
        return {'status': manifest['status'], 'snapshot': str(directory) if directory else None,
                'dry_run': args.dry_run, 'files': len(entries), 'counts_by_extension': counts,
                'csv_records': manifest['csv_records'], 'warnings': len(warnings),
                'sha256': copied, 'coverage': 'not_checked', 'restore': 'not_tested'}
    except Exception as exc:
        if directory:
            manifest.update(status='failed', error_type=type(exc).__name__,
                            error=str(exc) if type(exc) is ValueError else 'Archive or filesystem error')
            save_manifest(directory, manifest)
            setattr(exc, 'snapshot', str(directory))
            if not getattr(args, 'quiet', False):
                print(json.dumps({'status': 'failed', 'snapshot': str(directory)}))
        raise


def verify(directory):
    directory = directory.expanduser().resolve(strict=True)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or manifest.get('status') != 'archive_verified':
        raise ValueError('Snapshot was not successfully archived with a supported schema')
    archive = directory / 'export.zip'
    if archive.stat().st_size != manifest['archive']['bytes'] or digest(archive) != manifest['archive']['sha256']:
        raise ValueError('Archive checksum or size mismatch')
    return {'status': 'checksum_verified', 'snapshot': str(directory),
            'warnings': len(manifest.get('warnings', [])),
            'coverage': manifest.get('coverage', 'not_checked'), 'restore': manifest.get('restore', 'not_tested')}


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('snapshot', help='Copy a ZIP into a new dated snapshot and inspect it')
    create.add_argument('--source', required=True, type=Path)
    create.add_argument('--destination', required=True, type=Path)
    create.add_argument('--scope', required=True)
    create.add_argument('--format', choices=['markdown', 'html', 'pdf'], required=True)
    create.add_argument('--dry-run', action='store_true')
    create.add_argument('--max-bytes', type=positive_int, default=20 * 1024**3,
                        help='Expanded byte budget including nested ZIPs (default: 20 GiB)')
    create.add_argument('--max-files', type=positive_int, default=200000)
    check = commands.add_parser('verify', help='Verify the ZIP against its manifest; does not prove source coverage')
    check.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        result = snapshot(args) if args.command == 'snapshot' else verify(args.directory)
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile, NotImplementedError) as exc:
        reason = str(exc) if type(exc) is ValueError else 'Inspect the local input/snapshot'
        parser.exit(1, f'Backup failed ({type(exc).__name__}): {reason}.\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
