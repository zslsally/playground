"""Local export checks; detailed output is returned to the runner, not printed."""
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile

TEXT_LIMIT = 32 * 1024**2
NOTE_ID = re.compile(r'([0-9a-f]{32})\.(?:md|html?)$')


def relative(name):
    parts = PurePosixPath(name).parts
    return '/'.join(parts[1:]) if len(parts) > 1 and parts[0].startswith('Export-') else name


def file_map(manifest):
    return {e['path'][0]: e['sha256'] for e in manifest['files'] if len(e['path']) == 1}


def page_map(manifest):
    result = {}
    for name, sha in file_map(manifest).items():
        match = NOTE_ID.search(name)
        if match:
            result.setdefault(match[1], set()).add(sha)
    return result


def link_key(item, hashes):
    source = item['source']
    match = NOTE_ID.search(source)
    return (match[1] if match else relative(source), relative(item['target']), hashes.get(source))


def audit(archive_path, manifest, baseline=None):
    previous, previous_report, previous_path = baseline or ({'files': []}, {}, None)
    current_ids, old_ids = page_map(manifest), page_map(previous)
    missing_ids = sorted(old_ids.keys() - current_ids.keys())
    changes = {
        'baseline': str(previous_path) if previous_path else None,
        'current_page_ids': len(current_ids), 'previous_page_ids': len(old_ids),
        'added_ids': sorted(current_ids.keys() - old_ids.keys()), 'missing_ids': missing_ids,
        'changed_ids': sorted(k for k in current_ids.keys() & old_ids.keys() if current_ids[k] != old_ids[k]),
        'files_before': len(previous['files']) if previous_path else None,
        'files_now': len(manifest['files']),
        'attachments_now': sum(e['extension'] not in ('.md', '.csv', '.html', '.htm') for e in manifest['files']),
        'attachments_before': sum(e['extension'] not in ('.md', '.csv', '.html', '.htm') for e in previous['files']) if previous_path else None,
    }
    old_details = previous_report.get('checks', {}).get('markdown_links', {}).get('details', [])
    old_hashes, hashes = file_map(previous), file_map(manifest)
    known = {link_key(item, old_hashes) for item in old_details}
    checks = {'comparison': changes}
    links = {'status': 'checked', 'files_parsed': 0, 'local_links': 0,
             'external_links_not_fetched': 0, 'details': [], 'skipped_files': []}
    checks['markdown_links'] = links
    try:
        from markdown_it import MarkdownIt
        parser = MarkdownIt('commonmark')
    except ImportError:
        parser = None
        links['status'] = 'skipped_missing_markdown_it'
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        dirs = set()
        for name in names:
            parts = PurePosixPath(name).parts
            dirs.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        for name in sorted(names):
            if name.endswith('.zip'):
                links['skipped_files'].append({'path': name, 'reason': 'nested_zip_links_not_checked'})
                continue
            if not name.endswith('.md') or parser is None:
                continue
            if archive.getinfo(name).file_size > TEXT_LIMIT:
                links['skipped_files'].append({'path': name, 'reason': 'text_size_limit'})
                continue
            try:
                body = archive.read(name).decode('utf-8-sig')
            except UnicodeDecodeError:
                links['skipped_files'].append({'path': name, 'reason': 'invalid_utf8'})
                continue
            stack = list(parser.parse(body))
            links['files_parsed'] += 1
            while stack:
                token = stack.pop()
                stack.extend(token.children or [])
                if token.type not in ('link_open', 'image'):
                    continue
                url = token.attrGet('src' if token.type == 'image' else 'href') or ''
                try:
                    parts = urlsplit(url)
                except ValueError:
                    links['skipped_files'].append({'path': name, 'reason': 'invalid_link_url'})
                    continue
                if parts.scheme or parts.netloc:
                    links['external_links_not_fetched'] += 1
                    continue
                if not parts.path:
                    continue
                links['local_links'] += 1
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path)))
                if target not in names and target.rstrip('/') not in dirs:
                    item = {'source': name, 'target': target, 'kind': token.type}
                    item['previously_observed'] = link_key(item, hashes) in known
                    links['details'].append(item)
    links['new_unresolved'] = sum(not x['previously_observed'] for x in links['details'])
    links['previously_observed_unresolved'] = sum(x['previously_observed'] for x in links['details'])
    if parser and not links['files_parsed']:
        links['status'] = 'not_applicable_no_markdown' if not manifest['counts_by_extension'].get('.md') else 'incomplete'
    report = {
        'schema_version': 1, 'checks': checks,
        'coverage': 'compared_with_previous_snapshot' if previous_path else 'initial_baseline_only',
        'live_source_checked': False, 'restore': 'not_tested',
        'limitations': ['Historical comparison does not prove current source completeness.',
                        'No online page-body, database-row, permission or restore audit was performed.',
                        'Remote URLs, raw HTML links, anchors and rendered layout are not checked.',
                        'Previously observed link notices remain unresolved; prior empty-database evidence may be stale.'],
    }
    return report


def compact(report):
    diff = report['checks']['comparison']
    links = report['checks']['markdown_links']
    return {'page_ids': diff['current_page_ids'], 'added_ids': len(diff['added_ids']),
            'missing_ids': len(diff['missing_ids']), 'changed_ids': len(diff['changed_ids']),
            'files_delta': diff['files_now'] - diff['files_before'] if diff['files_before'] is not None else None,
            'attachments_delta': diff['attachments_now'] - diff['attachments_before'] if diff['attachments_before'] is not None else None,
            'new_link_notices': links['new_unresolved'],
            'previous_link_notices': links['previously_observed_unresolved'],
            'link_check': links['status'], 'skipped_link_checks': len(links['skipped_files'])}


def write_reports(directory, manifest, report):
    from backup_archive import save_manifest
    report_path = directory / 'local-verification.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = compact(report)
    text = '# Notion backup verification\n\n'
    text += f"Archive integrity verified. {len(manifest['files'])} files; {manifest['archive']['bytes']:,} bytes.\n\n"
    text += f"Coverage: {report['coverage']}. Live source checked: no. Restore tested: no.\n\n"
    for key, value in summary.items():
        text += f'- {key}: {value}\n'
    text += '\nDetails are in local-verification.json.\n\n' + '\n'.join('- ' + x for x in report['limitations']) + '\n'
    (directory / 'local-verification.md').write_text(text, encoding='utf-8')
    manifest['coverage'] = report['coverage']
    manifest['local_verification_report'] = {'path': 'local-verification.json', 'sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}
    manifest['local_summary'] = summary
    # Missing previous IDs remain a concern on later runs instead of vanishing into the baseline.
    manifest['baseline_eligible'] = not summary['missing_ids']
    save_manifest(directory, manifest)
