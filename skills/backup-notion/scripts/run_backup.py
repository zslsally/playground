#!/usr/bin/env python3
"""Configured, compact Notion export intake. No Notion/network/UI operations."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import backup_archive

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config.local.json'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def config_at(path):
    cfg = read_json(path)
    for key in ('workspace', 'destination', 'scope', 'format'):
        if not cfg.get(key):
            raise ValueError('Missing configuration field: ' + key)
    if not cfg['workspace'].get('name') or cfg['format'] not in ('markdown', 'html', 'pdf'):
        raise ValueError('Invalid workspace or format')
    cfg['destination'] = str(Path(cfg['destination']).expanduser().resolve())
    cfg['series_key'] = hashlib.sha256(json.dumps(
        [cfg['workspace'], cfg['scope'], cfg['format']], sort_keys=True).encode()).hexdigest()
    return cfg


def existing(cfg):
    root = Path(cfg['destination'])
    explicit = Path(cfg['baseline_snapshot']).expanduser().resolve() if cfg.get('baseline_snapshot') else None
    records = []
    for path in root.glob('*/manifest.json'):
        try:
            m = read_json(path)
            if m.get('status') != 'archive_verified':
                continue
            if m.get('series_key') != cfg['series_key'] and path.parent.resolve() != explicit:
                continue
            if m.get('format') != cfg['format']:
                continue
            records.append((path.parent, m))
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda pair: pair[1]['created_at'], reverse=True)


def baseline_from(records, exclude=None):
    for path, m in records:
        if path == exclude or not m.get('baseline_eligible', True):
            continue
        report_path = path / 'local-verification.json'
        if not report_path.exists():
            report_path = path / 'verification.json'
        report = read_json(report_path) if report_path.exists() else {}
        return m, report, path
    return None


def pending(cfg):
    path = Path(cfg['destination']) / '.export-pending.json'
    return read_json(path) if path.exists() else None


def discover(cfg):
    wait_for = pending(cfg)
    since = wait_for['requested_epoch'] if wait_for else 0
    candidates, settling = set(), 0
    for folder in cfg.get('search_dirs', ['~/Desktop', '~/Downloads']):
        for pattern in cfg.get('patterns', ['*_Export-*.zip', 'Export-*.zip']):
            for path in Path(folder).expanduser().glob(pattern):
                if not path.is_file() or path.stat().st_mtime < since:
                    continue
                if time.time() - path.stat().st_mtime < cfg.get('settle_seconds', 5):
                    settling += 1
                    continue
                candidates.add(path.resolve())
    return sorted(candidates), settling


def status(cfg):
    paths, settling = discover(cfg)
    records = existing(cfg)
    return {'status': 'export_pending' if pending(cfg) else 'ready',
            'workspace': cfg['workspace'], 'destination': cfg['destination'],
            'candidate_count': len(paths), 'candidates': [str(p) for p in paths[:5]],
            'settling': settling, 'latest_snapshot': str(records[0][0]) if records else None}


def requested(cfg):
    root = Path(cfg['destination'])
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.export-pending.json'
    if path.exists():
        return {'status': 'export_pending', 'record': str(path), 'new_request_recorded': False}
    data = {'requested_at': datetime.now(timezone.utc).isoformat(), 'requested_epoch': time.time(),
            'workspace': cfg['workspace'], 'scope': cfg['scope'], 'format': cfg['format']}
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return {'status': 'export_pending', 'record': str(path), 'new_request_recorded': True}


def finish_pending(cfg, directory):
    path = Path(cfg['destination']) / '.export-pending.json'
    if path.exists():
        data = read_json(path)
        data.update(status='archived', snapshot=str(directory), completed_at=datetime.now(timezone.utc).isoformat())
        (directory / 'export-request.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        path.unlink()


def run(cfg, args):
    import audit_backup
    deadline = time.monotonic() + args.wait_seconds
    while True:
        if args.source:
            paths, settling = [args.source.expanduser().resolve(strict=True)], 0
        else:
            paths, settling = discover(cfg)
        if paths or time.monotonic() >= deadline:
            break
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    if not paths:
        return {'status': 'export_pending' if pending(cfg) else 'needs_export', 'settling': settling}
    if len(paths) != 1:
        return {'status': 'choose_source', 'candidate_count': len(paths), 'candidates': [str(p) for p in paths[:5]]}
    source = paths[0]
    source_hash = backup_archive.digest(source)
    records = existing(cfg)
    duplicate = next(((p, m) for p, m in records if m['archive']['sha256'] == source_hash), None)
    if duplicate:
        directory, manifest = duplicate
        backup_archive.verify(directory)
        cached = directory / 'local-verification.json'
        cached_valid = cached.exists() and manifest.get('local_verification_report', {}).get('sha256') == backup_archive.digest(cached)
        if cached_valid and not args.force_audit:
            if not args.dry_run:
                finish_pending(cfg, directory)
            return {'status': 'already_archived', 'snapshot': str(directory),
                    'fresh_backup': False, 'bytes': manifest['archive']['bytes'],
                    'files': len(manifest['files']), 'checks': manifest['local_summary'],
                    'coverage': manifest['coverage'], 'live_source_checked': False,
                    'restore': 'not_tested',
                    'report': str(directory / 'local-verification.md')}
        baseline = baseline_from(records, exclude=directory)
        # Migrated legacy snapshots can seed known link notices for their own first local audit.
        if baseline is None and (directory / 'verification.json').exists():
            baseline = (manifest, read_json(directory / 'verification.json'), directory)
    else:
        directory, manifest = None, None
        baseline = baseline_from(records)
    if not duplicate:
        options = SimpleNamespace(source=source, destination=Path(cfg['destination']), scope=cfg['scope'],
                                  format=cfg['format'], dry_run=args.dry_run,
                                  max_bytes=20 * 1024**3, max_files=200000, quiet=True)
        if args.dry_run:
            before = source_hash
            files, warnings, counts = backup_archive.inspect_archive(source, options.max_bytes, options.max_files)
            if not files or backup_archive.digest(source) != before:
                raise ValueError('Empty or changing archive')
            expected = {'markdown': {'.md', '.csv'}, 'html': {'.html', '.htm'}, 'pdf': {'.pdf'}}[cfg['format']]
            if not expected.intersection(counts):
                warnings.append({'kind': 'selected_format_not_found'})
            manifest = {'files': files, 'warnings': warnings, 'counts_by_extension': counts,
                        'archive': {'sha256': before, 'bytes': source.stat().st_size},
                        'csv_records': sum(e.get('csv_records', 0) for e in files)}
        else:
            result = backup_archive.snapshot(options)
            directory = Path(result['snapshot'])
            manifest = read_json(directory / 'manifest.json')
            if manifest['archive']['sha256'] != source_hash:
                raise ValueError('Source changed before snapshot copy')
            # Record ownership before optional checks, so an interrupted audit can resume.
            manifest.update(series_key=cfg['series_key'], workspace=cfg['workspace'],
                            source={'path': str(source), 'sha256': source_hash}, baseline_eligible=False)
            backup_archive.save_manifest(directory, manifest)
    report = audit_backup.audit(source if args.dry_run else directory / 'export.zip', manifest, baseline)
    if args.dry_run and backup_archive.digest(source) != source_hash:
        raise ValueError('Archive changed during local audit')
    summary = audit_backup.compact(report)
    if not args.dry_run:
        manifest.update(series_key=cfg['series_key'], workspace=cfg['workspace'],
                        source={'path': str(source), 'sha256': source_hash})
        audit_backup.write_reports(directory, manifest, report)
        finish_pending(cfg, directory)
    return {'status': 'dry_run_verified' if args.dry_run else ('already_archived' if duplicate else 'archived'),
            'fresh_backup': not args.dry_run and not bool(duplicate),
            'snapshot': str(directory) if directory else None,
            'bytes': manifest['archive']['bytes'], 'files': len(manifest['files']),
            'markdown': manifest['counts_by_extension'].get('.md', 0),
            'csv': manifest['counts_by_extension'].get('.csv', 0),
            'attachments': sum(v for k, v in manifest['counts_by_extension'].items() if k not in ('.md', '.csv', '.html', '.htm')),
            'archive_notices': len(manifest['warnings']), 'checks': summary,
            'coverage': report['coverage'], 'live_source_checked': False, 'restore': 'not_tested',
            'report': str(directory / 'local-verification.md') if directory and not args.dry_run else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    subs = parser.add_subparsers(dest='command', required=True)
    subs.add_parser('status')
    subs.add_parser('requested')
    run_parser = subs.add_parser('run')
    run_parser.add_argument('--source', type=Path)
    run_parser.add_argument('--dry-run', action='store_true')
    run_parser.add_argument('--force-audit', action='store_true')
    run_parser.add_argument('--wait-seconds', type=int, choices=range(61), default=0, metavar='0..60')
    args = parser.parse_args()
    try:
        cfg = config_at(args.config)
        executable = cfg.get('python')
        if executable and Path(executable).expanduser().resolve() != Path(sys.executable).resolve():
            os.execv(str(Path(executable).expanduser()), [executable, str(Path(__file__).resolve()), *sys.argv[1:]])
        os.umask(0o077)
        result = status(cfg) if args.command == 'status' else requested(cfg) if args.command == 'requested' else run(cfg, args)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        message = str(exc) if type(exc) is ValueError else 'Check local config/input and retained snapshot'
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'message': message,
                          'snapshot': getattr(exc, 'snapshot', None)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
