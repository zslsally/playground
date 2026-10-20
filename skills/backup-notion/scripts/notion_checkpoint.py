"""Private, atomic entity-boundary checkpoints; no credentials or network calls."""
from collections import deque
import hashlib
import json
from pathlib import Path

from notion_api import BackupError
from notion_assets import sha256


class PauseRequested(Exception):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_path(snapshot, relative):
    path = snapshot / relative
    if path.is_symlink() or not path.resolve().is_relative_to(snapshot.resolve()):
        raise BackupError('invalid_checkpoint_path')
    return path


def record_file(backup, path):
    relative = path.relative_to(backup.snapshot).as_posix()
    backup.checkpoint_files[relative] = {'bytes': path.stat().st_size, 'sha256': sha256(path)}


def save(backup, write_json):
    backup.assets.flush()
    value = {
        'version': 2, 'series': backup.manifest['series'], 'started': backup.started,
        'queue': list(backup.queue), 'queued': sorted(backup.queued),
        'completed': sorted(backup.completed), 'blocks': sorted(backup.blocks),
        'raw_count': backup.raw_count, 'page_count': backup.page_count,
        'errors': backup.errors, 'warnings': backup.warnings, 'index': backup.index,
        'markdown_ids': sorted(backup.markdown), 'prior': sorted(backup.prior),
        'discovered': backup.discovered, 'comments_denied': getattr(backup, 'comments_denied', False),
        'requests': backup.request_offset + backup.client.requests,
        'files': backup.checkpoint_files,
        'link_pages': backup.link_pages, 'page_parents': backup.page_parents,
        'link_descendants': backup.link_descendants,
        'page_candidates': backup.page_candidates, 'page_states': backup.page_states,
        'entity_files': backup.entity_files, 'reused_pages': backup.reused_pages,
        'refreshed_pages': backup.refreshed_pages, 'reuse_fallbacks': backup.reuse_fallbacks,
        'reused_assets': backup.reused_assets,
        'assets': {'links': backup.assets.links, 'failures': backup.assets.failures,
                   'external': sorted(backup.assets.external)},
    }
    write_json(backup.snapshot / '.work/checkpoint.json', {'sha256': digest(value), 'state': value})
    write_json(backup.snapshot / 'progress.json', {
        'status': 'running', 'snapshot': str(backup.snapshot), 'pages': backup.page_count,
        'entities': len(backup.completed), 'pending_entities': len(backup.queue),
        'saved_responses': backup.raw_count, 'api_requests': value['requests'],
        'attachments': len(set(backup.assets.links.values())),
        'link_only_pages': len(backup.link_pages),
        'reused_pages': backup.reused_pages, 'refreshed_pages': backup.refreshed_pages,
        'reuse_fallbacks': backup.reuse_fallbacks,
        'errors': len(backup.errors), 'warnings': len(backup.warnings),
        'checkpoint_available': True,
    })


def restore(backup):
    path = backup.snapshot / '.work/checkpoint.json'
    if not path.is_file():
        raise BackupError('legacy_snapshot_has_no_checkpoint')
    envelope = json.loads(path.read_text())
    value = envelope['state']
    if envelope['sha256'] != digest(value) or value['version'] not in (1, 2):
        raise BackupError('checkpoint_integrity_failed')
    if value['series'] != backup.manifest['series']:
        raise BackupError('checkpoint_scope_mismatch')
    for relative, info in value['files'].items():
        target = checked_path(backup.snapshot, relative)
        if (not target.is_file() or target.stat().st_size != info['bytes']
                or sha256(target) != info['sha256']):
            raise BackupError('checkpoint_file_integrity_failed')
    for relative in set(value['assets']['links'].values()):
        target = checked_path(backup.snapshot, relative)
        if not relative.startswith('assets/') or not target.is_file() or sha256(target) != target.stem:
            raise BackupError('checkpoint_attachment_integrity_failed')
    backup.queue = deque(tuple(item) for item in value['queue'])
    for name in ('queued', 'completed', 'prior'):
        setattr(backup, name, {tuple(item) for item in value[name]})
    backup.blocks = set(value['blocks'])
    for name in ('page_count', 'errors', 'warnings', 'index', 'started', 'discovered', 'comments_denied'):
        setattr(backup, name, value[name])
    backup.checkpoint_files = value['files']
    backup.link_pages = value.get('link_pages', {})
    backup.page_parents = value.get('page_parents', {})
    backup.link_descendants = value.get('link_descendants', {})
    backup.page_candidates = value.get('page_candidates', {})
    backup.page_states = value.get('page_states', {})
    backup.entity_files = value.get('entity_files', {})
    backup.reused_pages = value.get('reused_pages', 0)
    backup.refreshed_pages = value.get('refreshed_pages', 0)
    backup.reuse_fallbacks = value.get('reuse_fallbacks', 0)
    backup.reused_assets = value.get('reused_assets', False)
    backup.markdown = {ident: json.loads(checked_path(backup.snapshot, '.work/markdown/' + ident + '.json').read_text())
                       for ident in value['markdown_ids']}
    # Keep orphan responses from the interrupted entity; never overwrite them.
    backup.raw_count = max([value['raw_count']] + [int(p.stem) for p in (backup.snapshot / 'raw').glob('*.json')])
    backup.request_offset = value['requests']
    backup.assets.links = value['assets']['links']
    backup.assets.failures = value['assets']['failures']
    backup.assets.external = set(value['assets']['external'])


def progress(snapshot):
    snapshot = Path(snapshot).resolve()
    manifest = json.loads((snapshot / 'manifest.json').read_text())
    path = snapshot / 'progress.json'
    result = json.loads(path.read_text()) if path.exists() else {
        'snapshot': str(snapshot), 'checkpoint_available': False,
        'note': 'Legacy snapshot; a running manifest does not prove a live process.'}
    result['status'] = manifest['status']
    if manifest['status'] not in ('running', 'paused'):
        result.update(manifest.get('counts', {}))
    result['pause_requested'] = (snapshot / '.pause-request').exists()
    return result
