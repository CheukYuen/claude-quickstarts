#!/usr/bin/env python3
"""Prepare pinned, page-preserving wiki-lab inputs without invoking a model."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import subprocess


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = Path('/Users/leon/Stock/invest-sources')
CUTOFF = '2026-10-15'
HOLDOUT_IDS = (
    '002309.SZ-20251215-announcement-2025-098',
    'report-ubs-20260923-china-optical-fiber',
    'report-ubs-20260923-yofc-coverage-initiation',
)
P1_IDS = (
    '1300.HK-20260913-announcement-qinghai-possible-acquisition',
    '1300.HK-20260920-announcement-qinghai-acquisition',
    '1300.HK-20260925-announcement-qinghai-supplemental',
    '601869.SH-20260822-interim-2026h1',
    'report-bofa-20260828-zhongtian-2q26-review',
)
PEER_SECTIONS = {
    '600487.SH-20260827-interim-2026H1': (7, 82, 83),
    '600522.SH-20260828-interim-2026H1': (7, 26, 27),
    '600498.SH-20260827-interim-2026H1': (7, 13, 14),
    '300548.sz-20260822-interim-2026h1': (10, 24, 25),
}
# The shared library deliberately contains selected text pages for these reports.
# This is a local-input limitation, not permission to extract the missing PDF pages.
EXISTING_SELECTED_PAGES = {
    'report-bofa-20260828-zhongtian-2q26-review': [1, 2, 3, 4, 5, 6],
    'report-ubs-20260923-china-optical-fiber': [1, 2, 3, 4, 9, 12],
    'report-ubs-20260923-yofc-coverage-initiation': [1, 2, 3, 4, 5, 7],
}
COMPANY_REPORTS = {
    'report-dbs-20261005-trigiant-equity-explorer': 'trigiant',
    'report-fxbaogao-20260917-trigiant-everbright-meeting-notes': 'trigiant',
    'report-huachuang-20260824-yofc-2026h1-review': 'yofc',
    'report-huatai-20260824-yofc-fiber-price-2026h1-review': 'yofc',
    'report-guolianminsheng-20260830-yofc-2026h1-review': 'yofc',
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def normalized_sha256(record: dict) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def source_path(relative: str, source_root: Path = SOURCE_ROOT) -> Path:
    path = (source_root / relative).resolve()
    if not path.is_relative_to(source_root.resolve()):
        raise ValueError(f'Source path escapes read-only root: {relative}')
    return path


def read_manifest(path: Path) -> dict:
    """Keep every record identity; a repeated source_id is not another source."""
    grouped = defaultdict(list)
    for line_number, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            continue
        record = json.loads(raw)
        grouped[record['source_id']].append({
            'record': record, 'line': line_number,
            'raw_line_sha256': hashlib.sha256(raw).hexdigest(),
            'normalized_sha256': normalized_sha256(record),
            'version': record.get('manifest_record_version') or 'm1',
            'version_inferred': not bool(record.get('manifest_record_version')),
        })
    return dict(grouped)


def resolve_records(rows: list, manifest_relative: str = 'manifests/sources.jsonl') -> dict:
    """Follow exact predecessor hashes; reject forks, cycles and loose duplicates."""
    if not rows:
        raise ValueError('No source records')
    nodes = {}
    for row in rows:
        version = row['version']
        if version in nodes:
            raise ValueError(f'Duplicate manifest version {version} for {row["record"]["source_id"]}')
        nodes[version] = row
    children = defaultdict(list)
    roots = []
    sid = rows[0]['record']['source_id']
    for version, row in nodes.items():
        record = row['record']
        if record['source_id'] != sid:
            raise ValueError('Mixed source identities')
        predecessor = record.get('supersedes_manifest_ref')
        if predecessor is None:
            roots.append(version)
            continue
        if predecessor.get('path') != manifest_relative or predecessor.get('source_id') != sid:
            raise ValueError(f'Unresolved manifest predecessor for {sid} {version}')
        previous = predecessor.get('manifest_record_version')
        if previous not in nodes:
            raise ValueError(f'Missing predecessor {sid} {previous}')
        if predecessor.get('normalized_sha256') != nodes[previous]['normalized_sha256']:
            raise ValueError(f'Predecessor hash mismatch for {sid} {version}')
        # Metadata revisions cannot silently substitute another PDF or source edition.
        old = nodes[previous]['record']
        for key in ('sha256', 'path', 'source_version'):
            if old.get(key) != record.get(key):
                raise ValueError(f'Metadata revision changed source identity: {sid} {key}')
        if not record.get('metadata_revision', {}).get('reason'):
            raise ValueError(f'Missing metadata revision reason: {sid} {version}')
        children[previous].append(version)
    if len(roots) != 1 or any(len(values) > 1 for values in children.values()):
        raise ValueError(f'Manifest chain has fork or multiple roots: {sid}')
    chain, current = [], roots[0]
    while current not in chain:
        chain.append(current)
        following = children.get(current, [])
        if not following:
            break
        current = following[0]
    if len(chain) != len(nodes):
        raise ValueError(f'Manifest chain disconnected or cyclic: {sid}')
    chosen = dict(nodes[chain[-1]])
    chosen['chain'] = [{k: nodes[v][k] for k in (
        'version', 'version_inferred', 'line', 'raw_line_sha256', 'normalized_sha256')}
        for v in chain]
    return chosen



def prepare_vendor(run_dir: Path, company_code: str = '601869.SH',
                   report_periods: list[str] | None = None,
                   vendor_filename: str = 'choice-20261004-yofc-financials-market.json',
                   source_root: Path = SOURCE_ROOT) -> dict:
    """Bind an existing vendor capture by manifest chain and financial periods.

    No API or filesystem write occurs outside runs/. Unlike PDF selection, null
    published_date is permitted: every accepted CTR row must have FIRSTNOTICEDATE.
    The raw capture remains in the shared library, as do PDF originals.
    """
    from datetime import date as calendar_date
    # Local import avoids the choice_audit -> corpus module dependency at import.
    import choice_audit

    run_dir, source_root = Path(run_dir).resolve(), Path(source_root).resolve()
    if not run_dir.is_relative_to(HERE / 'runs'):
        raise ValueError('Vendor outputs must stay inside wiki-lab/runs/')
    if Path(vendor_filename).name != vendor_filename:
        raise ValueError('vendor_filename must be a filename, not a path')
    manifest_path = source_root / 'manifests/sources.jsonl'
    manifest_hash = sha256(manifest_path)
    grouped = read_manifest(manifest_path)
    candidates = []
    for rows in grouped.values():
        # Resolve only candidates, while preserving the same strict chain rules.
        if not any(row['record'].get('kind') == 'vendor'
                   and (row['record'].get('company_code') or '').upper() == company_code.upper()
                   and Path(row['record'].get('path', '')).name == vendor_filename
                   for row in rows):
            continue
        binding = resolve_records(rows)
        record = binding['record']
        if record.get('kind') == 'vendor' and (record.get('company_code') or '').upper() == company_code.upper():
            candidates.append(binding)
    if len(candidates) != 1:
        raise ValueError(f'Expected one bound vendor capture, found {len(candidates)}')
    binding = candidates[0]
    record = binding['record']
    path = source_path(record['path'], source_root)
    if path.suffix.lower() != '.json' or not str(record['path']).startswith('vendor/'):
        raise ValueError('Vendor capture must be JSON under vendor/')
    actual_hash = sha256(path)
    if actual_hash != record['sha256']:
        raise ValueError(f'Vendor hash mismatch: {record["source_id"]}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    expected = set(report_periods or [])
    queries, found_periods = [], set()
    for query_index, query in enumerate(payload.get('queries', [])):
        definition = query.get('query', {})
        args = definition.get('args', [])
        if definition.get('function') != 'ctr' or not args or args[0] not in choice_audit.TABLE_DOCS:
            continue
        options = choice_audit.options_dict(args[2])
        if options.get('secucode', '').upper() != company_code.upper():
            continue
        if options.get('reporttype') != '1':
            raise ValueError('STOP: vendor CTR does not use ReportType=1')
        period = options.get('reportdate')
        rows = choice_audit.ctr_rows(query)
        if len(rows) != 1:
            raise ValueError('STOP: expected exactly one company/period CTR row')
        for row in rows:
            if row.get('reportType') != '合并报表':
                raise ValueError('STOP: vendor returned type is not original consolidated report')
            if row.get('REPORTDATE') != period:
                raise ValueError('STOP: requested and returned report periods differ')
            notice = row.get('FIRSTNOTICEDATE')
            try:
                if calendar_date.fromisoformat(notice).isoformat() != notice:
                    raise ValueError(notice)
            except (ValueError, TypeError):
                raise ValueError('STOP: vendor row lacks valid FIRSTNOTICEDATE')
            if notice > CUTOFF:
                raise ValueError('STOP: vendor FIRSTNOTICEDATE is after cutoff')
        if not expected or period in expected:
            queries.append({'index': query_index, 'table': args[0], 'report_period': period,
                            'report_type': options['reporttype']})
            found_periods.add(period)
    if not queries or (expected and found_periods != expected):
        raise ValueError('Missing requested vendor financial period')
    if sha256(manifest_path) != manifest_hash or sha256(path) != actual_hash:
        raise ValueError('Vendor input changed while binding')
    result = {'schema': 'wiki-lab-vendor-binding/1', 'source_id': record['source_id'],
              'company_code': company_code, 'vendor_path': str(path), 'vendor_sha256': actual_hash,
              'manifest_path': str(manifest_path), 'manifest_sha256': manifest_hash,
              'record_binding': {key: binding[key] for key in
                  ('version', 'version_inferred', 'line', 'raw_line_sha256', 'normalized_sha256', 'chain')},
              'metadata_record': record, 'queries': queries,
              'report_periods': sorted(found_periods),
              'published_date_basis': 'FIRSTNOTICEDATE per financial period; manifest published_date may be null'}
    dump(run_dir / 'choice/binding.json', result)
    return result


def classify(record: dict) -> str | None:
    sid = record['source_id']
    code = record.get('company_code')
    if sid in COMPANY_REPORTS:
        return COMPANY_REPORTS[sid]
    if code in ('1300.HK', '01300.HK') and record.get('kind') == 'filing':
        return 'trigiant'
    if code == '601869.SH' and record.get('kind') == 'filing':
        if record.get('doc_type') == 'annual' and record.get('period') in tuple(str(y) for y in range(2021, 2026)):
            return 'yofc'
        if sid in P1_IDS or sid in (
            '601869.sh-20260930-announcement-investor-record-20260924',
            '601869.sh-20260821-announcement-interim-dividend-plan',
            '601869.sh-20260911-announcement-egm-dividend-resolution',
        ):
            return 'yofc'
    if sid in PEER_SECTIONS or sid == P1_IDS[-1]:
        return 'optical-fiber'
    return None


def page_range(record: dict) -> list[int]:
    sid = record['source_id']
    if sid in EXISTING_SELECTED_PAGES:
        return EXISTING_SELECTED_PAGES[sid]
    if sid in PEER_SECTIONS:
        start, end, _ = PEER_SECTIONS[sid]
        return list(range(start, end + 1))
    count = record.get('pages')
    if not isinstance(count, int) or count < 1:
        raise ValueError(f'Invalid PDF page count: {sid}')
    return list(range(1, count + 1))


def verify_peer_section(record: dict, source_root: Path) -> dict:
    start, end, next_page = PEER_SECTIONS[record['source_id']]
    directory = source_path(record['text_dir'], source_root)
    beginning = (directory / f'p{start:04d}.txt').read_text(encoding='utf-8')
    following = (directory / f'p{next_page:04d}.txt').read_text(encoding='utf-8')
    contents = (directory / 'p0003.txt').read_text(encoding='utf-8')
    if not re.search(r'第三节\s+管理层讨论与分析', beginning):
        raise ValueError(f'Peer section opening changed: {record["source_id"]}')
    if not re.search(r'第四节\s+公司治理', following):
        raise ValueError(f'Peer section ending changed: {record["source_id"]}')
    if '管理层讨论与分析' not in contents:
        raise ValueError(f'Peer contents page changed: {record["source_id"]}')
    return {'title': '第三节 管理层讨论与分析', 'pdf_pages': [start, end],
            'next_section_pdf_page': next_page, 'verification': 'contents_and_actual_section_boundaries',
            'boundary_page_hashes': {str(n): sha256(directory / f'p{n:04d}.txt')
                                     for n in (3, start, end, next_page)},
            'scope_note': '整页保留；不含后续财务报表分部附注。'}


def freeze_registry(path: Path, records: list[dict]) -> dict:
    """Append new sources only; never recompute existing short identifiers."""
    existed = path.exists()
    if existed:
        registry = json.loads(path.read_text(encoding='utf-8'))
        if registry.get('cutoff') != CUTOFF:
            raise ValueError('Registry cutoff mismatch')
    else:
        registry = {'schema': 'wiki-lab-registry/1', 'cutoff': CUTOFF, 'mapping': {}}
    mapping = registry['mapping']
    if len(set(mapping.values())) != len(mapping) or any(not re.fullmatch(r'S\d{2,}', v) for v in mapping.values()):
        raise ValueError('Invalid or duplicate short identifiers')
    next_id = max((int(v[1:]) for v in mapping.values()), default=0) + 1
    appended = False
    for record in sorted(records, key=lambda r: (r['published_date'], r['source_id'])):
        if record['source_id'] not in mapping:
            mapping[record['source_id']] = f'S{next_id:02d}'
            next_id += 1
            appended = True
    if not existed or appended:
        dump(path, registry)
    return registry


def verify_prepared(manifest_path: Path) -> dict:
    """Recheck the entire bound local-input fingerprint after model work."""
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if sha256(Path(manifest['manifest_path'])) != manifest['manifest_sha256']:
        raise ValueError('Shared source manifest changed after preparation')
    pages, originals = 0, 0
    for source in manifest['sources'] + manifest['holdouts']:
        if sha256(Path(source['pdf_path'])) != source['pdf_sha256']:
            raise ValueError(f'Original changed: {source["source_id"]}')
        originals += 1
        for page in source['page_records']:
            if sha256(Path(page['text_path'])) != page['text_sha256']:
                raise ValueError(f'Shared page text changed: {source["source_id"]} p{page["page"]}')
            if page['corpus_path'] and sha256(Path(page['corpus_path'])) != page['text_sha256']:
                raise ValueError(f'Copied page text changed: {source["id"]} p{page["page"]}')
            pages += 1
    return {'status': 'passed', 'originals': originals, 'page_text_files': pages,
            'shared_manifest_sha256': manifest['manifest_sha256']}


def _prepare_source(row: dict, short: str | None, source_root: Path, run_dir: Path,
                    copy_text: bool) -> dict:
    record = row['record']
    pdf = source_path(record['path'], source_root)
    actual_pdf_hash = sha256(pdf)
    if actual_pdf_hash != record['sha256']:
        raise ValueError(f'Original hash mismatch: {record["source_id"]}')
    text_dir = source_path(record['text_dir'], source_root)
    available = sorted(int(p.stem[1:]) for p in text_dir.glob('p[0-9][0-9][0-9][0-9].txt'))
    expected = EXISTING_SELECTED_PAGES.get(record['source_id'], list(range(1, record['pages'] + 1)))
    if available != expected:
        raise ValueError(f'Missing/unexpected page text: {record["source_id"]}')
    pages = page_range(record)
    page_records = []
    for n in pages:
        original = text_dir / f'p{n:04d}.txt'
        raw = original.read_bytes()
        text = raw.decode('utf-8')
        copied = run_dir / 'corpus' / 'pages' / str(short) / original.name
        if copy_text:
            copied.parent.mkdir(parents=True, exist_ok=True)
            copied.write_bytes(raw)
        page_records.append({'page': n, 'text_path': str(original),
                             'corpus_path': str(copied) if copy_text else None,
                             'text_sha256': hashlib.sha256(raw).hexdigest(), 'chars': len(text),
                             'empty_text': not bool(text.strip())})
    category = classify(record)
    tags = [f'公司:{category}'] if category in ('yofc', 'trigiant') else ['行业:optical-fiber']
    source = {
        'id': short, 'source_id': record['source_id'], 'title': record['title'],
        'date': record['published_date'], 'kind': record.get('kind'), 'doc_type': record.get('doc_type'),
        'object': category, 'tags': tags, 'cutoff': CUTOFF,
        'pdf_path': str(pdf), 'pdf_sha256': actual_pdf_hash, 'pdf_hash_status': 'verified_against_bound_record',
        'pdf_page_count': record['pages'], 'pages': pages, 'page_records': page_records,
        'available_text_pages': available,
        'missing_text_pages': sorted(set(range(1, record['pages'] + 1)) - set(available)),
        'chars': sum(p['chars'] for p in page_records),
        'source_url': record.get('source_url'), 'retrieved_at': record.get('retrieved_at'),
        'source_version': record.get('source_version') or 'v1-unspecified-in-record',
        'record_binding': {k: row[k] for k in ('version', 'version_inferred', 'line', 'raw_line_sha256', 'normalized_sha256', 'chain')},
        'metadata_record': record,
        'limitations': record.get('limitations', []) + ([record['note']] if record.get('note') else []),
    }
    if record['source_id'] in PEER_SECTIONS:
        source['section'] = verify_peer_section(record, source_root)
    if source['missing_text_pages']:
        source['limitations'].append(f'共享库已有逐页文字仅PDF页序{available}；缺少{source["missing_text_pages"]}页文字。本次不补提取，不能声称覆盖整份PDF。')
    if any(p['empty_text'] for p in page_records):
        source['limitations'].append('存在空白逐页文字；保留页序，不能证明PDF该页没有内容。')
    return source


def _chunks(sources: list[dict], run_dir: Path, max_chars: int, merge_monthly: bool) -> list[dict]:
    result, monthly = [], []

    def write_chunk(pages: list, chunk_id: str) -> None:
        text = []
        by_source = defaultdict(list)
        tags = set()
        for source, page in pages:
            by_source[source['id']].append(page['page'])
            tags.update(source['tags'])
            # Markers are wrappers. Original page bytes are copied separately, unchanged.
            text.append(f'\n[SOURCE: {source["id"]}]\n[PAGE: {page["page"]}]\n[DATE: {source["date"]}]\n')
            text.append(Path(page['corpus_path']).read_bytes().decode('utf-8'))
        path = run_dir / 'corpus' / 'chunks' / f'{chunk_id}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(text), encoding='utf-8')
        raw_chars = sum(p['chars'] for _, p in pages)
        result.append({'id': chunk_id, 'path': str(path), 'sha256': sha256(path),
                       'chars': raw_chars, 'wrapped_chars': len(''.join(text)),
                       'sources': [{'id': sid, 'pages': ns} for sid, ns in by_source.items()],
                       'tags': sorted(tags), 'single_page_over_limit': len(pages) == 1 and raw_chars > max_chars})

    for source in sources:
        if merge_monthly and 'monthly-return' in source['source_id']:
            monthly.append(source)
            continue
        pending, chars, index = [], 0, 1
        for page in source['page_records']:
            if pending and chars + page['chars'] > max_chars:
                write_chunk(pending, f'{source["id"]}.c{index:02d}')
                pending, chars, index = [], 0, index + 1
            pending.append((source, page))
            chars += page['chars']
        if pending:
            write_chunk(pending, f'{source["id"]}.c{index:02d}')
    pending, chars, index, previous = [], 0, 1, None
    for source in monthly:
        # A monthly-return source is never split merely to merge it with another.
        adjacent = previous is None or (date.fromisoformat(source['date']) - date.fromisoformat(previous)).days <= 35
        if pending and (chars + source['chars'] > max_chars or not adjacent):
            write_chunk(pending, f'C{index:03d}')
            pending, chars, index = [], 0, index + 1
        pending.extend((source, p) for p in source['page_records'])
        chars += source['chars']
        previous = source['date']
    if pending:
        write_chunk(pending, f'C{index:03d}')
    return result


def prepare(run_dir: Path, scope: str = 'p0', registry_path: Path | None = None,
            max_chars: int = 100000, source_root: Path = SOURCE_ROOT, profile: str | None = None) -> dict:
    """Verify local originals; freeze P0 catalog or prepare only the five P1 inputs."""
    scope = profile or scope
    if scope not in ('p0', 'p1') or max_chars < 1:
        raise ValueError('Expected scope p0/p1 and positive max_chars')
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_relative_to(HERE / 'runs'):
        raise ValueError('Run outputs must stay inside wiki-lab/runs/')
    ignored = subprocess.run(['git', 'check-ignore', '-q', str(run_dir / 'corpus')], cwd=HERE)
    if ignored.returncode:
        raise ValueError('Add runs/ to .gitignore before preparing inputs')
    if (run_dir / 'corpus-manifest.json').exists():
        raise FileExistsError('Prepared corpus is immutable: choose a new run directory')
    if run_dir.exists() and any(p.is_symlink() for p in run_dir.rglob('*')):
        raise ValueError('Run directory must not contain symbolic links')
    manifest = source_root / 'manifests/sources.jsonl'
    input_manifest_hash = sha256(manifest)
    grouped = read_manifest(manifest)
    initial, held, excluded = [], [], []
    for sid, rows in grouped.items():
        if not any(classify(row['record']) or sid in HOLDOUT_IDS for row in rows):
            continue
        row = resolve_records(rows)
        record = row['record']
        disclosed = record.get('published_date')
        if not disclosed or disclosed > CUTOFF:
            excluded.append({'source_id': sid, 'reason': 'missing_or_after_cutoff_date'})
        elif not record.get('text_dir'):
            excluded.append({'source_id': sid, 'reason': 'no_existing_page_text'})
        elif sid in HOLDOUT_IDS:
            held.append(row)
        else:
            initial.append(row)
    if set(HOLDOUT_IDS) != {row['record']['source_id'] for row in held}:
        raise ValueError('Missing one of three explicit holdouts')
    if set(P1_IDS) - {row['record']['source_id'] for row in initial}:
        raise ValueError('Missing required P1 input')
    run_dir.mkdir(parents=True, exist_ok=True)
    registry_path = Path(registry_path).resolve() if registry_path else run_dir / 'source-registry.json'
    if not registry_path.is_relative_to(HERE / 'runs'):
        raise ValueError('Registry must stay inside wiki-lab/runs/')
    registry = freeze_registry(registry_path, [row['record'] for row in initial])
    local_registry = run_dir / 'source-registry.json'
    if local_registry != registry_path:
        if local_registry.exists():
            if json.loads(local_registry.read_text(encoding='utf-8')) != registry:
                raise ValueError('Existing local registry differs; choose a new run directory')
        else:
            dump(local_registry, registry)
    selected = [row for row in initial if scope == 'p0' or row['record']['source_id'] in P1_IDS]
    selected.sort(key=lambda row: int(registry['mapping'][row['record']['source_id']][1:]))
    sources = [_prepare_source(row, registry['mapping'][row['record']['source_id']], source_root, run_dir, True)
               for row in selected]
    holdouts = [_prepare_source(row, None, source_root, run_dir, False) for row in held]
    chunks = _chunks(sources, run_dir, max_chars, merge_monthly=scope == 'p0')
    summary = {'sources': len(sources), 'pages': sum(len(s['pages']) for s in sources),
               'chars': sum(s['chars'] for s in sources), 'chunks': len(chunks),
               'holdouts': len(holdouts), 'holdout_pages': sum(len(s['pages']) for s in holdouts),
               'holdout_chars': sum(s['chars'] for s in holdouts),
               'by_object': {obj: {'sources': sum(s['object'] == obj for s in sources),
                                   'pages': sum(len(s['pages']) for s in sources if s['object'] == obj),
                                   'chars': sum(s['chars'] for s in sources if s['object'] == obj)}
                             for obj in ('trigiant', 'yofc', 'optical-fiber')}}
    if sha256(manifest) != input_manifest_hash:
        raise ValueError('Shared source manifest changed during preparation')
    result = {'schema': 'wiki-lab-corpus/1', 'scope': scope, 'cutoff': CUTOFF,
              'cutoff_basis': 'published_date; later retrieval/metadata revisions remain visible and are not historical snapshots',
              'manifest_path': str(manifest), 'manifest_sha256': input_manifest_hash,
              'registry_path': str(run_dir / 'source-registry.json'),
              'registry_sha256': sha256(run_dir / 'source-registry.json'),
              'max_chunk_chars': max_chars, 'sources': sources, 'holdouts': holdouts,
              'excluded': excluded, 'summary': summary}
    dump(run_dir / 'corpus-manifest.json', result)
    dump(run_dir / 'chunks.json', chunks)
    dump(run_dir / '_sources.json', {'cutoff': CUTOFF, 'sources': sources})
    lines = ['# P0/P1 来源和块清单', '', f'截点：{CUTOFF}；范围：{scope}；只读本地已有原件及逐页文字。', '',
             '每份来源按元数据前驱哈希链绑定唯一叶记录；m1 缺省仅指原登记版本。',
             '披露日用于截点筛选，取得日及元数据修订日另列，不声称重建历史时点状态。', '',
             '|短号|来源 ID|披露日|记录版本（行）|PDF页序|字符|', '|---|---|---|---|---|---:|']
    for source in sources:
        binding = source['record_binding']
        ns = source['pages']
        lines.append(f'|{source["id"]}|{source["source_id"]}|{source["date"]}|{binding["version"]}（L{binding["line"]}）|{ns[0]}–{ns[-1]} / {source["pdf_page_count"]}|{source["chars"]:,}|')
    lines += ['', '## 留出的三份资料', '', '这三份只核对原件、页文字和元数据版本，不创建抽取块，也不分配初始短号；未来只追加短号。', '']
    for source in holdouts:
        lines.append(f'- {source["source_id"]}：{source["date"]}；{source["record_binding"]["version"]}；{len(source["pages"])}页。')
    lines += ['', '## 同行中报章节', '']
    for sid, (start, end, following) in PEER_SECTIONS.items():
        lines.append(f'- {sid}：管理层讨论 PDF p{start}–{end}；p{following}进入第四节。已核对目录和实际首尾页；不含后续财务附注。')
    lines += ['', '## 切块', '', '|块|来源与PDF页序|字符|SHA-256|', '|---|---|---:|---|']
    for chunk in chunks:
        spans = '; '.join(f'{s["id"]} p{s["pages"][0]}–{s["pages"][-1]}' for s in chunk['sources'])
        lines.append(f'|{chunk["id"]}|{spans}|{chunk["chars"]:,}|{chunk["sha256"]}|')
    lines += ['', '完整原件/页文字哈希、逐条清单修订链、空白页标记见 corpus-manifest.json；块输入哈希见 chunks.json。', '',
              '## 规模', '', '```json', json.dumps(summary, ensure_ascii=False, indent=2), '```', '']
    (run_dir / 'CORPUS.md').write_text('\n'.join(lines), encoding='utf-8')
    return {**result, 'chunks': chunks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--scope', '--profile', dest='scope', choices=('p0', 'p1'), default='p0')
    parser.add_argument('--registry', type=Path)
    parser.add_argument('--max-chars', type=int, default=100000)
    parser.add_argument('--prepare-only', action='store_true', help='Preparation is always model-free')
    args = parser.parse_args()
    output = prepare(args.run_dir, scope=args.scope, registry_path=args.registry, max_chars=args.max_chars)
    print(json.dumps(output['summary'], ensure_ascii=False, indent=2))
