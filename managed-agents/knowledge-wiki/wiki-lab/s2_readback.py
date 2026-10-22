#!/usr/bin/env python3
"""Export hash-bound source readback packets. Never make review judgments."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

import s2_compare


QUEUES = ('semantic', 'unmatched', 'reverse')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _positive_pages(value, label):
    if not isinstance(value, list) or any(isinstance(p, bool) or not isinstance(p, int) or p < 1 for p in value):
        raise ValueError(label + ': pages must be positive integer arrays')
    return set(value)


def _fence(text, language=''):
    longest = max((len(m.group()) for m in re.finditer(r'`+', text)), default=0)
    marker = '`' * max(3, longest + 1)
    return marker + language + '\n' + text + ('' if text.endswith('\n') else '\n') + marker + '\n'


def _source_index(manifest, root):
    aliases = {}
    for source in manifest.get('sources', []):
        if not isinstance(source, dict) or not source.get('id') or not source.get('source_id'):
            raise ValueError('Malformed corpus source identity')
        by_page = {}
        for binding in source.get('page_records', []):
            if not isinstance(binding, dict):
                raise ValueError('Malformed frozen page binding')
            page = binding.get('page')
            _positive_pages([page], 'corpus page')
            if page in by_page:
                raise ValueError('Duplicate frozen page binding: %s p%s' % (source['id'], page))
            digest = binding.get('text_sha256')
            if (not isinstance(binding.get('corpus_path'), str) or not isinstance(digest, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise ValueError('Malformed frozen page binding: %s p%s' % (source['id'], page))
            path = Path(binding['corpus_path'])
            path = path if path.is_absolute() else root / path
            path = path.resolve()
            if not path.is_relative_to(root):
                raise ValueError('Frozen page lies outside the run: ' + str(path))
            by_page[page] = dict(path=path, sha256=binding['text_sha256'])
        entry = dict(source=source, pages=by_page)
        for alias in (source['id'], source['source_id']):
            if alias in aliases:
                raise ValueError('Duplicate corpus source alias: ' + alias)
            aliases[alias] = entry
    return aliases


def _stored_records(root):
    index = {}
    paths = sorted((root / 'records').glob('*.json'))
    choice = root / 'choice/records.json'
    if choice.exists():
        paths.append(choice)
    for path in paths:
        value = json.loads(path.read_bytes())
        rows = value.get('records', []) if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError('Malformed stored record list: ' + str(path))
        for record in rows:
            key = record.get('record_id') if isinstance(record, dict) else None
            if not isinstance(key, str) or not key:
                raise ValueError('Malformed stored record identity: ' + str(path))
            if key in index and index[key] != record:
                raise ValueError('Conflicting stored record identity: ' + key)
            index[key] = record
    return index


def _candidate_ids(row):
    result = set(row.get('no_number_candidates', []))
    for name in ('matches', 'choice_matches', 'base_key_candidates'):
        for match in row.get(name, []):
            if isinstance(match, dict) and match.get('record_id'):
                result.add(match['record_id'])
    if any(not isinstance(key, str) for key in result):
        raise ValueError('Malformed candidate record identity')
    return sorted(result)


def _candidate_overview_record(record):
    """Project existing fields only; this is no candidate or quality decision."""
    return deepcopy(dict(record_id=record['record_id'], record_type=record.get('record_type'),
        pages=record.get('locator', {}).get('pdf_pages', []), object_key=record.get('object_key'),
        nature=record.get('nature'), scope=record.get('detail', {}).get('scope'),
        dimensions=record.get('dimensions', []), numbers=record.get('numbers', []), detail=record.get('detail')))


def export_packets(run_dir, queue, batch_size=10, snapshot=None):
    """Write hash-bound packets, compact overviews and a JSON index.

    Existing identical packets are reusable. A changed queue, batch size or
    source binding cannot silently overwrite an earlier readback export.
    """
    if queue not in QUEUES or isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError('Expected a supported queue and positive batch size')
    if snapshot is not None and (not isinstance(snapshot, str) or
                                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', snapshot)):
        raise ValueError('Snapshot must be a simple safe directory name (letters, digits, _ or -)')
    root = Path(run_dir).resolve()
    destination = root / 'readback'
    if snapshot is not None:
        destination /= snapshot
    if not destination.resolve().is_relative_to(root / 'readback') or (root / 'readback').is_symlink():
        raise ValueError('Readback destination must stay within run/readback')
    queue_path, manifest_path = root / 'review-queues.json', root / 'corpus-manifest.json'
    queue_bytes, manifest_bytes = queue_path.read_bytes(), manifest_path.read_bytes()
    queues, manifest = json.loads(queue_bytes), json.loads(manifest_bytes)
    rows = queues.get('queues', {}).get(queue)
    if not isinstance(rows, list):
        raise ValueError('Review queue is absent or malformed: ' + queue)
    aliases, records = _source_index(manifest, root), _stored_records(root)
    pages, planned_rows, seen = {}, [], set()

    def entry_for(alias):
        if not isinstance(alias, str) or alias not in aliases:
            raise ValueError('Review source is absent from frozen corpus: ' + str(alias))
        return aliases[alias]

    def page_ref(alias, page):
        entry = entry_for(alias)
        source, bindings = entry['source'], entry['pages']
        if page not in bindings:
            raise ValueError('Missing frozen page binding: %s p%s' % (source['id'], page))
        key = (source['id'], page)
        if key not in pages:
            binding = bindings[page]
            try:
                data = binding['path'].read_bytes()
            except OSError as error:
                raise ValueError('Missing frozen page file: ' + str(binding['path'])) from error
            if _sha(data) != binding['sha256']:
                raise ValueError('Frozen page hash mismatch: %s p%s' % key)
            pages[key] = dict(source=source['id'], source_id=source['source_id'], page=page,
                sha256=binding['sha256'], path=str(binding['path'].relative_to(root)),
                text=data.decode('utf-8'))
        return key

    def record_refs(record):
        if not isinstance(record, dict) or not isinstance(record.get('source'), dict):
            raise ValueError('Malformed review record source')
        if record['source'].get('kind') == 'choice':
            return set()
        if record['source'].get('kind') != 'pdf':
            raise ValueError('Unknown review record source kind')
        alias = record['source'].get('source_id')
        declared = _positive_pages(record.get('locator', {}).get('pdf_pages'), 'record locator')
        if not declared:
            raise ValueError('PDF review record has no cited pages')
        for quote in record.get('excerpts', []):
            quote_pages = _positive_pages([quote.get('page')], 'record excerpt')
            if not quote_pages <= declared:
                raise ValueError('Excerpt page is outside the record locator')
        return {page_ref(alias, page) for page in declared}

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Malformed review row')
        row_id = row.get('item_id') if queue == 'unmatched' else row.get('record_id')
        if not isinstance(row_id, str) or not row_id or row_id in seen:
            raise ValueError('Missing or duplicate review row identity: ' + str(row_id))
        seen.add(row_id)
        source = entry_for(row.get('source'))['source']
        cited = _positive_pages(row.get('pages', []), 'review row')
        for name in ('review', 'manual_review'):
            review = row.get(name, {})
            if isinstance(review, dict):
                cited |= _positive_pages(review.get('evidence_pages', []), name + ' evidence')
        refs = {page_ref(source['id'], page) for page in cited}
        primary = row.get('record') or records.get(row_id)
        if queue != 'unmatched':
            if not primary or primary.get('record_id') != row_id:
                raise ValueError('Sampled record is missing or has a mismatched identity: ' + row_id)
            if entry_for(primary.get('source', {}).get('source_id'))['source']['id'] != source['id']:
                raise ValueError('Review row source differs from its sampled record')
            refs |= record_refs(primary)
        elif not cited:
            raise ValueError('Unmatched P1 item has no cited or evidence pages: ' + row_id)
        candidates = []
        for key in _candidate_ids(row):
            candidate = records.get(key)
            candidates.append(dict(record_id=key, available=candidate is not None, record=candidate))
            if candidate is not None:
                refs |= record_refs(candidate)
        planned_rows.append(dict(row_id=row_id, row=row, sampled_record=primary,
                                 candidate_records=candidates, page_keys=sorted(refs)))

    packets, files = [], {}
    for start in range(0, len(planned_rows), batch_size):
        selected = planned_rows[start:start + batch_size]
        filename = '%s-%03d.md' % (queue, start // batch_size + 1)
        compact_name = filename[:-3] + '-compact.json'
        overview_name = filename[:-3] + '-candidate-overview.json'
        refs = sorted({key for row in selected for key in row['page_keys']})
        candidates = {candidate['record_id']: candidate for row in selected for candidate in row['candidate_records']}
        overview = dict(schema='wiki-lab-s2-candidate-overview/1', queue=queue,
            queue_sha256=_sha(queue_bytes), queues_sha256=s2_compare.json_hash(queues),
            corpus_manifest_sha256=_sha(manifest_bytes), packet=filename,
            candidate_record_ids=sorted(candidates),
            records=[_candidate_overview_record(candidate['record']) for key, candidate in sorted(candidates.items())
                     if candidate['available']],
            unavailable_record_ids=[key for key, candidate in sorted(candidates.items()) if not candidate['available']],
            judgments_created=False)
        overview_data = _json_bytes(overview)
        files[overview_name] = overview_data
        compact_rows = [dict(row_id=row['row_id'], p1_text=row['row'].get('text'),
            pages=[dict(source=key[0], page=key[1], sha256=pages[key]['sha256']) for key in row['page_keys']],
            candidate_record_ids=[c['record_id'] for c in row['candidate_records']]) for row in selected]
        compact = dict(schema='wiki-lab-s2-readback-compact/1', queue=queue, queue_sha256=_sha(queue_bytes),
            queues_sha256=s2_compare.json_hash(queues),
            corpus_manifest_sha256=_sha(manifest_bytes), packet=filename, rows=compact_rows,
            candidate_overview_path=overview_name, candidate_overview_sha256=_sha(overview_data), judgments_created=False)
        compact_data = _json_bytes(compact)
        files[compact_name] = compact_data
        lines = ['# S2 原文回读包：%s / %03d' % (queue, start // batch_size + 1), '',
                 '本文件仅汇集已有抽样条目、记录和冻结原文。未生成任何审阅裁决。', '',
                 '队列 SHA-256：`%s`' % _sha(queue_bytes), '',
                 '精简条目概览：[%s](%s)。' % (compact_name, compact_name),
                 '去重候选字段概览：[%s](%s)，仅投影已有记录字段；完整候选原件在包末按ID去重列出。' %
                 (overview_name, overview_name), '']
        printed_records = {}
        for row in selected:
            queue_row = {key: value for key, value in row['row'].items() if key != 'record'}
            lines += ['## 条目 ' + row['row_id'], '', '原始队列条目（record全文另列）：',
                      _fence(json.dumps(queue_row, ensure_ascii=False, indent=2), 'json')]
            if row['sampled_record'] is not None:
                primary = row['sampled_record']
                key = primary['record_id']
                if key in printed_records and printed_records[key] != primary:
                    raise ValueError('Conflicting sampled record identity: ' + key)
                if key not in printed_records:
                    lines += ['完整抽样记录：', _fence(json.dumps(primary, ensure_ascii=False, indent=2), 'json')]
                    printed_records[key] = primary
            if row['candidate_records']:
                lines += ['候选记录ID引用（全文见包末）：',
                          _fence(json.dumps([c['record_id'] for c in row['candidate_records']], ensure_ascii=False), 'json')]
            lines += ['本条原文页：' + '；'.join('%s p%d' % key for key in row['page_keys']), '']
        candidate_bindings = []
        if candidates:
            lines += ['## 候选记录全文（包内去重）', '']
        for key, candidate in sorted(candidates.items()):
            record = candidate['record']
            digest = _sha(_json_bytes(record)) if candidate['available'] else None
            candidate_bindings.append(dict(record_id=key, available=candidate['available'], sha256=digest))
            lines += ['### 候选记录 ' + key, '']
            if not candidate['available']:
                lines += [_fence(json.dumps(dict(record_id=key, available=False), ensure_ascii=False), 'json')]
            elif key in printed_records:
                if printed_records[key] != record:
                    raise ValueError('Conflicting sampled/candidate record identity: ' + key)
                lines += ['SHA-256：`%s`；全文已在上述完整抽样记录中列出。' % digest, '']
            else:
                lines += ['SHA-256：`%s`' % digest, '', _fence(json.dumps(record, ensure_ascii=False, indent=2), 'json')]
                printed_records[key] = record
        lines += ['## 冻结逐页全文', '']
        for key in refs:
            page = pages[key]
            lines += ['### %s p%d' % key, '', 'SHA-256：`%s`' % page['sha256'], '', _fence(page['text'], 'text')]
        data = ('\n'.join(lines) + '\n').encode('utf-8')
        files[filename] = data
        packets.append(dict(path=filename, sha256=_sha(data), compact_path=compact_name,
            compact_sha256=_sha(compact_data), candidate_records=candidate_bindings,
            candidate_overview_path=overview_name, candidate_overview_sha256=_sha(overview_data),
            row_ids=[row['row_id'] for row in selected],
            rows=[dict(row_id=row['row_id'], pages=[dict(source=key[0], page=key[1], sha256=pages[key]['sha256'])
                for key in row['page_keys']], candidate_record_ids=[c['record_id'] for c in row['candidate_records']]) for row in selected],
            pages=[{k: value for k, value in pages[key].items() if k != 'text'} for key in refs]))
    index = dict(schema='wiki-lab-s2-readback/1', queue=queue, queue_sha256=_sha(queue_bytes),
        queues_sha256=s2_compare.json_hash(queues),
        corpus_manifest_sha256=_sha(manifest_bytes), batch_size=batch_size, row_count=len(rows),
        judgments_created=False, packets=packets)
    if snapshot is not None:
        index['snapshot'] = snapshot
    filename = queue + '-index.json'
    files[filename] = _json_bytes(index)
    # Check all bindings again before the first write, including files cached
    # across several rows. Data are already in memory and cannot drift midway.
    if queue_path.read_bytes() != queue_bytes or manifest_path.read_bytes() != manifest_bytes:
        raise ValueError('Review queue or corpus manifest changed during export')
    for page in pages.values():
        if _sha((root / page['path']).read_bytes()) != page['sha256']:
            raise ValueError('Frozen page changed during export')
    for name, data in files.items():
        path = destination / name
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Existing readback binding differs; preserve prior packets: ' + str(path))
    expected_packets = {name for name in files if name.endswith('.md') or name.endswith('-compact.json')
                        or name.endswith('-candidate-overview.json')}
    existing_packets = {path.name for path in destination.glob(queue + '-[0-9][0-9][0-9].md')}
    existing_packets.update(path.name for path in destination.glob(queue + '-[0-9][0-9][0-9]-compact.json'))
    existing_packets.update(path.name for path in destination.glob(queue + '-[0-9][0-9][0-9]-candidate-overview.json'))
    if existing_packets - expected_packets:
        raise ValueError('Unexpected existing packets for this queue; preserve prior export')
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = destination / name
        if not path.exists():
            path.write_bytes(data)
    return index


def _run_path(root, value, label):
    """Accept an absolute path, a run-relative path, or a cwd-relative run path."""
    path = Path(value)
    if not path.is_absolute():
        direct = path.resolve()
        path = direct if direct.is_relative_to(root) else root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError(label + ' must stay within the run: ' + str(path))
    return path


def bind_review_drafts(root, paths, output, review_id):
    """Bind explicitly authored drafts to the current queue; create no decisions.

    Unmatched source reviews survive unrelated population changes only with an
    exact per-source records hash. Random semantic/reverse samples require the
    full current queue hash. The final object is validated by apply_reviews but
    that function's derived reviewed queue is never written here.
    """
    root = Path(root).resolve()
    if not paths or not isinstance(review_id, str) or not review_id.strip():
        raise ValueError('Draft paths and explicit review_id are required')
    destination = _run_path(root, output, 'Review output')
    if destination.suffix != '.json' or any(destination.is_relative_to(root / name) for name in
                                           ('records', 'choice', 'corpus')):
        raise ValueError('Review output must be a JSON review file, not a record or source file')
    queue_path, manifest_path = root / 'review-queues.json', root / 'corpus-manifest.json'
    if destination in (queue_path, manifest_path):
        raise ValueError('Review output must not replace its queue or corpus manifest')
    queue_bytes, manifest_bytes = queue_path.read_bytes(), manifest_path.read_bytes()
    queues = json.loads(queue_bytes)
    aliases = _source_index(json.loads(manifest_bytes), root)
    current_hash = s2_compare.json_hash(queues)
    row_index = {}
    for queue, rows in queues.get('queues', {}).items():
        if queue not in QUEUES or not isinstance(rows, list):
            raise ValueError('Malformed current review queue')
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('Malformed current review row')
            key = (queue, row.get('item_id', row.get('record_id')))
            if not isinstance(key[1], str) or key in row_index:
                raise ValueError('Malformed or duplicate current review row')
            row_index[key] = row
    combined, provenance, reviewers, seen = [], [], set(), set()
    inputs = {queue_path: queue_bytes, manifest_path: manifest_bytes}
    source_inputs = {}
    for value in paths:
        path = _run_path(root, value, 'Review draft')
        if path == destination:
            raise ValueError('Review output must not replace a draft')
        data = path.read_bytes()
        draft = json.loads(data)
        if not isinstance(draft, dict) or not all(isinstance(draft.get(k), str) and draft[k].strip()
                                                for k in ('reviewer', 'review_id')):
            raise ValueError('Every draft requires its explicit reviewer and review_id')
        decisions = draft.get('decisions')
        hashes = draft.get('source_records_sha256', {})
        if not isinstance(decisions, list) or not isinstance(hashes, dict):
            raise ValueError('Malformed draft decisions or source records bindings')
        canonical_hashes = {}
        for alias, digest in hashes.items():
            if alias not in aliases or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                raise ValueError('Unknown source or malformed source records SHA in draft')
            short = aliases[alias]['source']['id']
            source_path = (root / 'records' / (short + '.json')).resolve()
            if source_path.parent != root / 'records':
                raise ValueError('Source records path must stay within run/records')
            try:
                source_data = source_path.read_bytes()
            except OSError as error:
                raise ValueError('Draft source records are missing: ' + short) from error
            if _sha(source_data) != digest:
                raise ValueError('Draft source records hash mismatch: ' + short)
            if short in canonical_hashes and canonical_hashes[short] != digest:
                raise ValueError('Conflicting source aliases in draft')
            canonical_hashes[short] = digest
            source_inputs[source_path] = source_data
        keys = []
        for entry in decisions:
            if not isinstance(entry, dict):
                raise ValueError('Malformed authored review decision')
            key = (entry.get('queue'), entry.get('id'))
            if not all(isinstance(part, str) and part for part in key):
                raise ValueError('Malformed authored review entry identity')
            if key not in row_index or key in seen:
                raise ValueError('Unknown or duplicate authored review entry: ' + repr(key))
            row = row_index[key]
            alias = row.get('source')
            if alias not in aliases:
                raise ValueError('Current review row has unknown source')
            short = aliases[alias]['source']['id']
            if key[0] == 'unmatched':
                if short not in canonical_hashes:
                    raise ValueError('Unmatched draft lacks matching source records binding: ' + short)
            elif draft.get('queues_sha256') != current_hash:
                raise ValueError('Semantic/reverse draft requires exact current queues_sha256')
            seen.add(key)
            keys.append(dict(queue=key[0], id=key[1]))
            combined.append(deepcopy(entry))
        reviewers.add(draft['reviewer'])
        provenance.append(dict(path=str(path.relative_to(root)), sha256=_sha(data), reviewer=draft['reviewer'],
            review_id=draft['review_id'], source_records_sha256=canonical_hashes,
            original_queues_sha256=draft.get('queues_sha256'), entries=keys))
        inputs[path] = data
    result = dict(schema='wiki-lab-s2-bound-reviews/1', reviewer='; '.join(sorted(reviewers)),
        review_id=review_id, queues_sha256=current_hash, queue_file_sha256=_sha(queue_bytes),
        decisions=combined, drafts=provenance,
        binding_policy='unmatched: exact source records; semantic/reverse: exact current whole queue',
        decisions_created=False)
    # This validates queue membership, decision vocabulary, required reasons,
    # evidence and error types. It does not make or infer a review decision.
    s2_compare.apply_reviews(queues, result)
    for path, data in {**inputs, **source_inputs}.items():
        if path.read_bytes() != data:
            raise ValueError('Draft, source records or current queue changed during binding')
    data = _json_bytes(result)
    if destination.exists():
        if destination.read_bytes() != data:
            raise ValueError('Existing bound review differs; preserve prior review')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return result


def _retained_readback_indices(root, draft, decisions, old_queue_hash, manifest_hash, inputs):
    """Prove authored IDs belonged to the exported old sample, not just a source."""
    references = draft.get('readback_indices', [])
    if not isinstance(references, list) or (decisions and not references):
        raise ValueError('Retained draft requires saved readback_indices')
    if decisions and old_queue_hash is None:
        raise ValueError('Retained draft requires old queues_sha256')
    selected, provenance, seen_paths = {}, [], set()
    for reference in references:
        expected = None
        if isinstance(reference, str):
            value = reference
        elif isinstance(reference, dict) and isinstance(reference.get('path'), str):
            value, expected = reference['path'], reference.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('Malformed readback index SHA binding')
        else:
            raise ValueError('Malformed saved readback index reference')
        path = _run_path(root, value, 'Saved readback index')
        if path in seen_paths:
            raise ValueError('Duplicate saved readback index')
        seen_paths.add(path)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ValueError('Saved readback index is missing: ' + str(value)) from error
        if expected is not None and _sha(data) != expected:
            raise ValueError('Saved readback index raw SHA mismatch')
        index = json.loads(data)
        if not isinstance(index, dict) or index.get('schema') != 'wiki-lab-s2-readback/1' or index.get('queue') not in QUEUES:
            raise ValueError('Malformed saved readback index')
        queue = index['queue']
        if index.get('queues_sha256') != old_queue_hash:
            raise ValueError('Saved readback index old queues_sha256 mismatch')
        if index.get('corpus_manifest_sha256') != manifest_hash:
            raise ValueError('Saved readback index corpus manifest hash mismatch')
        packets, ids = index.get('packets'), []
        if not isinstance(packets, list):
            raise ValueError('Malformed saved readback index packets')
        for packet in packets:
            rows = packet.get('row_ids') if isinstance(packet, dict) else None
            if not isinstance(rows, list) or not all(isinstance(key, str) and key for key in rows):
                raise ValueError('Malformed saved readback selected IDs')
            if 'rows' in packet and (not isinstance(packet['rows'], list) or
                    [row.get('row_id') if isinstance(row, dict) else None for row in packet['rows']] != rows):
                raise ValueError('Saved readback packet row identities disagree')
            ids.extend(rows)
        if len(ids) != len(set(ids)) or type(index.get('row_count')) is not int or index['row_count'] != len(ids):
            raise ValueError('Saved readback index selected count or duplicate ID mismatch')
        selected.setdefault(queue, set()).update(ids)
        provenance.append(dict(path=str(path.relative_to(root)), sha256=_sha(data), queue=queue,
            queues_sha256=old_queue_hash, corpus_manifest_sha256=manifest_hash, selected_ids=ids))
        inputs[path] = data
    for entry in decisions:
        if entry['queue'] not in selected:
            raise ValueError('Missing old readback index for reviewed queue: ' + entry['queue'])
        if entry['id'] not in selected[entry['queue']]:
            raise ValueError('Authored decision was never selected in old readback sample: ' + entry['id'])
    for queue in {entry['queue'] for entry in decisions}:
        required = {entry['id'] for entry in decisions if entry['queue'] == queue}
        if not any(binding['queue'] == queue and required <= set(binding['selected_ids']) for binding in provenance):
            raise ValueError('No single old readback index covers all authored decisions for queue: ' + queue)
    return provenance


def rebind_retained_review(root, draft_path, output, review_id):
    """Keep authored judgments only for unchanged sources still sampled now.

    Validate identities, old exported sample membership and source bindings
    even for entries no longer selected.
    The explicit operation transfers decisions; it never creates or edits one.
    """
    root = Path(root).resolve()
    if not isinstance(review_id, str) or not review_id.strip():
        raise ValueError('Explicit review_id is required')
    path = _run_path(root, draft_path, 'Review draft')
    destination = _run_path(root, output, 'Review output')
    if destination.suffix != '.json' or any(destination.is_relative_to(root / name) for name in
                                           ('records', 'choice', 'corpus')):
        raise ValueError('Review output must be a JSON review file, not a record or source file')
    queue_path, manifest_path = root / 'review-queues.json', root / 'corpus-manifest.json'
    comparison_path = root / 'comparison.json'
    if destination in (path, queue_path, manifest_path, comparison_path):
        raise ValueError('Review output must not replace its draft, queue, manifest or comparison')
    data, queue_bytes, manifest_bytes = path.read_bytes(), queue_path.read_bytes(), manifest_path.read_bytes()
    draft, queues = json.loads(data), json.loads(queue_bytes)
    if not isinstance(draft, dict) or not all(isinstance(draft.get(key), str) and draft[key].strip()
                                            for key in ('reviewer', 'review_id')):
        raise ValueError('Draft requires explicit reviewer and review_id')
    decisions, hashes = draft.get('decisions'), draft.get('source_records_sha256')
    if not isinstance(decisions, list) or not isinstance(hashes, dict):
        raise ValueError('Draft requires decisions and source_records_sha256')
    old_queue_hash = draft.get('queues_sha256')
    if old_queue_hash is not None and (not isinstance(old_queue_hash, str) or
                                      not re.fullmatch(r'[0-9a-f]{64}', old_queue_hash)):
        raise ValueError('Old queues_sha256 must be a canonical SHA-256')
    manifest_hash = _sha(manifest_bytes)
    if any(isinstance(entry, dict) and entry.get('queue') in ('semantic', 'reverse') for entry in decisions):
        if old_queue_hash is None:
            raise ValueError('Semantic/reverse retained draft requires old queues_sha256')
        if draft.get('corpus_manifest_sha256') != manifest_hash:
            raise ValueError('Semantic/reverse retained draft corpus manifest hash mismatch')
    aliases = _source_index(json.loads(manifest_bytes), root)
    inputs = {path: data, queue_path: queue_bytes, manifest_path: manifest_bytes}
    canonical_hashes, source_data, record_index = {}, {}, {}
    for alias, digest in hashes.items():
        if alias not in aliases or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Unknown source or malformed source records SHA in draft')
        short = aliases[alias]['source']['id']
        source_path = (root / 'records' / (short + '.json')).resolve()
        if source_path.parent != root / 'records':
            raise ValueError('Source records path must stay within run/records')
        try:
            original = source_path.read_bytes()
        except OSError as error:
            raise ValueError('Draft source records are missing: ' + short) from error
        if _sha(original) != digest:
            raise ValueError('Draft source records hash mismatch: ' + short)
        if short in canonical_hashes and canonical_hashes[short] != digest:
            raise ValueError('Conflicting source aliases in draft')
        canonical_hashes[short] = digest
        source_data[short] = (source_path, original)
        inputs[source_path] = original
    # Identity lookup uses the whole per-source record population, not just
    # current sample rows. A missing source hash is rejected for each entry.
    for short in sorted({entry['source']['id'] for entry in aliases.values()}):
        source_path = (root / 'records' / (short + '.json')).resolve()
        if source_path.parent != root / 'records':
            raise ValueError('Source records path must stay within run/records')
        if not source_path.exists():
            continue
        original = source_data[short][1] if short in source_data else source_path.read_bytes()
        value = json.loads(original)
        rows = value.get('records') if isinstance(value, dict) else value
        if not isinstance(rows, list):
            raise ValueError('Malformed per-source record population: ' + short)
        for record in rows:
            key = record.get('record_id') if isinstance(record, dict) else None
            alias = record.get('source', {}).get('source_id') if isinstance(record, dict) else None
            if not isinstance(key, str) or not key or key in record_index:
                raise ValueError('Malformed or duplicate stored record identity: ' + str(key))
            if alias not in aliases or aliases[alias]['source']['id'] != short:
                raise ValueError('Stored record source disagrees with per-source file: ' + key)
            record_index[key] = (short, record)
    row_index = {}
    for queue, rows in queues.get('queues', {}).items():
        if queue not in QUEUES or not isinstance(rows, list):
            raise ValueError('Malformed current review queue')
        for row in rows:
            key = (queue, row.get('item_id', row.get('record_id')) if isinstance(row, dict) else None)
            if not isinstance(key[1], str) or key in row_index:
                raise ValueError('Malformed or duplicate current review row')
            row_index[key] = row
    comparison_index = {}
    if any(isinstance(entry, dict) and entry.get('queue') == 'unmatched' for entry in decisions):
        comparison_bytes = comparison_path.read_bytes()
        inputs[comparison_path] = comparison_bytes
        comparison = json.loads(comparison_bytes)
        if not isinstance(comparison.get('items'), list):
            raise ValueError('Malformed full P1 comparison population')
        for item in comparison['items']:
            key = item.get('item_id') if isinstance(item, dict) else None
            if not isinstance(key, str) or key in comparison_index:
                raise ValueError('Malformed or duplicate P1 comparison identity')
            comparison_index[key] = item
    validation_queues, seen, retained, skipped = deepcopy(queues), set(), [], []
    for queue in QUEUES:
        validation_queues.setdefault('queues', {}).setdefault(queue, [])
    for entry in decisions:
        if not isinstance(entry, dict):
            raise ValueError('Malformed authored review decision')
        key = (entry.get('queue'), entry.get('id'))
        if key[0] not in QUEUES or not isinstance(key[1], str) or not key[1] or key in seen:
            raise ValueError('Unknown or duplicate authored review entry: ' + repr(key))
        seen.add(key)
        if key[0] == 'unmatched':
            original = comparison_index.get(key[1])
            if original is None:
                raise ValueError('Unknown original P1 item: ' + key[1])
            alias = original.get('source')
            if alias not in aliases:
                raise ValueError('Original P1 item has unknown source')
            short = aliases[alias]['source']['id']
        else:
            if key[1] not in record_index:
                raise ValueError('Unknown original stored record: ' + key[1])
            short, record = record_index[key[1]]
            original = dict(record_id=key[1], source=short, record=record,
                            pages=record.get('locator', {}).get('pdf_pages', []))
        if short not in canonical_hashes:
            raise ValueError('Retained draft lacks matching source records binding: ' + short)
        _positive_pages(entry.get('evidence_pages'), 'Authored review evidence')
        if key in row_index:
            alias = row_index[key].get('source')
            if alias not in aliases or aliases[alias]['source']['id'] != short:
                raise ValueError('Current sampled source disagrees with original identity')
            retained.append(deepcopy(entry))
        else:
            validation_queues['queues'][key[0]].append(original)
            skipped.append(dict(queue=key[0], id=key[1]))
    old_indices = _retained_readback_indices(root, draft, decisions, old_queue_hash, manifest_hash, inputs)
    if destination in inputs:
        raise ValueError('Review output must not replace a saved readback index or binding input')
    # Even valid-but-unselected entries must contain an explicit legal decision,
    # reason and evidence. This scratch validation writes no new queue.
    s2_compare.apply_reviews(validation_queues, dict(reviewer=draft['reviewer'], review_id=draft['review_id'],
        queues_sha256=s2_compare.json_hash(validation_queues), decisions=decisions))
    transferred = [dict(queue=entry['queue'], id=entry['id']) for entry in retained]
    result = dict(schema='wiki-lab-s2-retained-reviews/1', reviewer=draft['reviewer'], review_id=review_id,
        queues_sha256=s2_compare.json_hash(queues), queue_file_sha256=_sha(queue_bytes),
        corpus_manifest_sha256=manifest_hash, source_records_sha256=canonical_hashes,
        decisions=retained, decisions_created=False,
        retention=dict(draft_path=str(path.relative_to(root)), draft_sha256=_sha(data),
            original_queues_sha256=old_queue_hash, original_review_id=draft['review_id'],
            original_reviewer=draft['reviewer'], original_corpus_manifest_sha256=draft.get('corpus_manifest_sha256'),
            source_records_sha256=canonical_hashes, transferred=transferred,
            readback_indices=old_indices, skipped_not_selected=skipped),
        binding_policy='explicit rebind: old exported sample membership, unchanged source records '
                       'and current sample intersection; no new judgments')
    s2_compare.apply_reviews(queues, result)
    for input_path, original in inputs.items():
        if input_path.read_bytes() != original:
            raise ValueError('Draft, source records or current queue changed during retained binding')
    rendered = _json_bytes(result)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open('xb') as stream:
            stream.write(rendered)
    except FileExistsError:
        if destination.read_bytes() != rendered:
            raise ValueError('Existing retained review differs; preserve prior review')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--queue', choices=QUEUES)
    mode.add_argument('--bind-drafts', type=Path, nargs='+')
    mode.add_argument('--rebind-retained', type=Path)
    parser.add_argument('--batch-size', type=int, default=10)
    parser.add_argument('--snapshot', help='export to a separate safe directory within readback/')
    parser.add_argument('--review-id')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.rebind_retained:
        if args.snapshot is not None:
            parser.error('--snapshot requires --queue')
        if not args.review_id or not args.output:
            parser.error('--rebind-retained requires --review-id and --output')
        result = rebind_retained_review(args.run_dir, args.rebind_retained, args.output, args.review_id)
        print(json.dumps(dict(decisions=len(result['decisions']),
            skipped_not_selected=len(result['retention']['skipped_not_selected']), queues_sha256=result['queues_sha256'])))
    elif args.bind_drafts:
        if args.snapshot is not None:
            parser.error('--snapshot requires --queue')
        if not args.review_id or not args.output:
            parser.error('--bind-drafts requires --review-id and --output')
        result = bind_review_drafts(args.run_dir, args.bind_drafts, args.output, args.review_id)
        print(json.dumps(dict(decisions=len(result['decisions']), drafts=len(result['drafts']),
                             queues_sha256=result['queues_sha256'])))
    else:
        if args.review_id or args.output:
            parser.error('--review-id and --output require --bind-drafts or --rebind-retained')
        index = export_packets(args.run_dir, args.queue, args.batch_size, snapshot=args.snapshot)
        directory = args.run_dir.resolve() / 'readback'
        if args.snapshot is not None:
            directory /= args.snapshot
        print(json.dumps(dict(queue=index['queue'], rows=index['row_count'], packets=len(index['packets']),
                             index=str(directory / (args.queue + '-index.json')))))


if __name__ == '__main__':
    main()
