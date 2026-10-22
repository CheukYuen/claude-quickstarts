#!/usr/bin/env python3
"""S2 comparison, reproducible review queues and honest usage accounting.

Code matches are candidates for coverage, not semantic acceptance. Review decisions
remain explicitly pending until a reviewer supplies source-page evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import random
import re

import records as contract

SEED = 20261022
CHUNK_ASSIGNMENT_METHOD = ('largest core-fragment character overlap on cited pages; '
                           'core-page-count fallback; plan-order ties; context excluded')
SAMPLING_ALGORITHM = 'per-stratum-sha256-v1'
STRATUM_SEED_RULE = ('SHA256 of UTF-8 canonical JSON [base_seed, stratum_key] '
                     '(ensure_ascii=False, sort_keys=True, separators=(",", ":")); '
                     'digest interpreted as an unsigned big-endian integer')
REVIEW_DECISIONS = {
    'unmatched': ('S2漏抽', 'P1条目不应成为记录', '匹配规则漏判', '由Choice承担'),
    'reverse': ('新增的正确内容', '错误'),
    'semantic': ('正确', '错误', '拿不准'),
}
# Chinese prose has no word boundaries around numbers ("收入125元"). Only
# numeric punctuation prevents a partial token; alphabetic/Chinese units do not.
NUMBER_RE = re.compile(r'(?<![\d.,])(?:[−－-]\s*)?\d+(?:,\d{3})*(?:\.\d+)?(?!\d|[.,]\d)')


def canonical_number(value):
    """Canonical decimal, without a float conversion or exponent notation."""
    text = str(value).strip().replace(',', '').replace('−', '-').replace('－', '-')
    text = re.sub(r'\s+', '', text)
    if text.startswith(('(', '（')) and text.endswith((')', '）')):
        text = '-' + text[1:-1]
    try:
        number = Decimal(text)
        if not number.is_finite():
            return None
        if number == 0:
            return '0'
        # Decimal.normalize() uses the current context and can round a long
        # exact integer. Formatting and trimming preserve all input digits.
        plain = format(number, 'f')
        return plain.rstrip('0').rstrip('.') if '.' in plain else plain
    except InvalidOperation:
        return None


def numbers_in_text(text):
    """Source-representation numbers; dates/citations never count as values.

    A four-digit year standing alone is omitted. Other plain numbers are retained
    as requested by the comparison protocol; common percentages may therefore
    create false candidates, which the semantic review must detect.
    """
    text = re.sub(r'\[[^\]]*\]', ' ', text)
    text = re.sub(r'(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)', ' ', text)
    text = re.sub(r'(?<!\d)\d{4}年(?:\s*\d{1,2}月(?:\s*\d{1,2}日)?)?', ' ', text)
    text = re.sub(r'(?<![A-Za-z\d])20\d{2}(?:H[12]|Q[1-4]|E)(?![A-Za-z\d])', ' ', text)
    text = re.sub(r'\b20\d{2}\b', ' ', text)
    found = set()
    for match in NUMBER_RE.finditer(text):
        token = match.group()
        start, end = match.span()
        # Bracketed financial negatives, including Chinese fullwidth brackets.
        before, after = text[:start].rstrip(), text[end:].lstrip()
        if before.endswith(('(', '（')) and after.startswith((')', '）')):
            token = '-' + token.lstrip('-−－ ')
        value = canonical_number(token)
        if value is not None:
            found.add(value)
    return found


def record_numbers(record):
    """Use declared numbers, both stored and original printed magnitudes."""
    values = set()
    for number in record.get('numbers', []):
        value = canonical_number(number.get('value', ''))
        if value is not None:
            values.add(value)
        try:
            values.add(canonical_number(contract.formatted_number(number)))
        except (ValueError, KeyError, InvalidOperation):
            pass
    return values - {None}


def source_short(record, aliases=None):
    source = record.get('source', {}).get('source_id', '')
    return (aliases or {}).get(source, source)


def parse_pages(text, short):
    pages = set()
    for bracket in re.findall(r'\[[^\]]*\]', text):
        for match in re.finditer(r'\b' + re.escape(short) + r'\s+(p{1,2}\s*[^\]S]*)', bracket):
            value = match.group(1).replace('pp', '').replace('p', '')
            for number in re.finditer(r'(\d+)\s*(?:[–—-]\s*(\d+))?', value):
                first, last = int(number.group(1)), int(number.group(2) or number.group(1))
                if first <= last and last - first < 10000:
                    pages.update(range(first, last + 1))
    return sorted(pages)


def inventory_pages(item, short):
    own = parse_pages(item.get('text', ''), short)
    if own:
        return own
    # S1 retained prior and current captions. Only the last applicable caption
    # defines the current table; older captions must not widen candidate pages.
    for context in reversed(item.get('context', [])):
        inherited = parse_pages(context.get('text', ''), short)
        if inherited:
            return inherited
    return []


def _compact(text):
    return contract.normalize_whitespace(text)


def excerpt_overlap(left, right, page_texts=None):
    """Find actual intersecting source spans when frozen pages are supplied.

    Without pages, require a contiguous common quote of 20 non-space characters;
    short generic words do not provide a useful overlap match.
    """
    if left.get('source', {}).get('source_id') != right.get('source', {}).get('source_id'):
        return None
    source = left.get('source', {}).get('source_id')
    for a in left.get('excerpts', []):
        for b in right.get('excerpts', []):
            if a.get('page') != b.get('page'):
                continue
            page = a['page']
            av, bv = _compact(a['text']), _compact(b['text'])
            frozen = (page_texts or {}).get((source, page))
            if frozen is None:
                frozen = (page_texts or {}).get(page)
            if frozen is not None:
                frozen = _compact(frozen)
                starts_a = [m.start() for m in re.finditer(re.escape(av), frozen)] if av else []
                starts_b = [m.start() for m in re.finditer(re.escape(bv), frozen)] if bv else []
                for ia in starts_a:
                    for ib in starts_b:
                        lo, hi = max(ia, ib), min(ia + len(av), ib + len(bv))
                        if hi > lo:
                            return dict(page=page, start=lo, end=hi, method='frozen_page_span')
            else:
                match = SequenceMatcher(None, av, bv, autojunk=False).find_longest_match()
                if match.size >= 20:
                    return dict(page=page, chars=match.size, method='common_quote_20_chars')
    return None


def pilot_match(reference_records, extracted_records, page_texts=None):
    """S46 gate: same type and at least one same numeric OR source span overlap.

    Matching is many-to-many: split and merged records are legitimate. The gate
    measures reference record recall; reverse recall is reported separately.
    """
    links, left_seen, right_seen = [], set(), set()
    for left in reference_records:
        for right in extracted_records:
            if left.get('source', {}).get('source_id') != right.get('source', {}).get('source_id'):
                continue
            if left.get('record_type') != right.get('record_type'):
                continue
            common = sorted(record_numbers(left) & record_numbers(right))
            overlap = excerpt_overlap(left, right, page_texts)
            if common or overlap:
                links.append(dict(reference_id=left['record_id'], record_id=right['record_id'],
                                  common_numbers=common, excerpt_overlap=overlap))
                left_seen.add(left['record_id'])
                right_seen.add(right['record_id'])
    count = len(reference_records)
    return dict(method='same_record_type AND (common_normalized_number OR same_page_quote_overlap)',
                overlap_method='intersecting normalized frozen-page spans; fallback contiguous common quote >=20 chars',
                reference_count=count, extracted_count=len(extracted_records), matched_reference_count=len(left_seen),
                reference_match_ratio=len(left_seen) / count if count else None,
                passed=bool(count and len(left_seen) / count >= .8), matches=links,
                unmatched_reference=[r for r in reference_records if r['record_id'] not in left_seen],
                unmatched_extracted=[r for r in extracted_records if r['record_id'] not in right_seen])


def _item_short(item):
    return item['item_id'].split('.')[0]


def compare_inventory(inventory, records, source_aliases=None, excluded_pages=None, choice_records=None,
                      extraction_complete=True, completed_sources=None, ready_pages_by_source=None):
    """Compare the P1 direct/revised inventory, retaining every row and evidence.

    One numeric match only proposes correspondence. Unnumbered entries are never
    counted as matched by code. Choice matching on excluded pages is provisional
    numeric responsibility, not a claim of matched subject/indicator/period.
    """
    items = inventory.get('items', []) if isinstance(inventory, dict) else inventory
    completed_sources = {(source_aliases or {}).get(source, source) for source in (completed_sources or [])}
    ready_pages = defaultdict(set)
    for source, pages in (ready_pages_by_source or {}).items():
        if (not isinstance(pages, (list, tuple, set)) or
            any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in pages)):
            raise ValueError('Ready pages must be positive PDF page arrays')
        ready_pages[(source_aliases or {}).get(source, source)].update(pages)
    for source, pages in (excluded_pages or {}).items():
        ready_pages[(source_aliases or {}).get(source, source)].difference_update(pages)
    if extraction_complete:
        completed_sources.update(_item_short(item) for item in items
                                 if item.get('mapping', {}).get('fit') in ('direct', 'revised'))
        completed_sources.update(source_short(record, source_aliases) for record in records)
    by_source, record_values = defaultdict(list), {}
    for record in records:
        by_source[source_short(record, source_aliases)].append(record)
        record_values[record['record_id']] = record_numbers(record)
    choice_values = [(r, record_numbers(r)) for r in (choice_records or [])]
    rows, reverse = [], defaultdict(list)
    for item in items:
        if item.get('mapping', {}).get('fit') not in ('direct', 'revised'):
            continue
        short = _item_short(item)
        source_complete = extraction_complete or short in completed_sources
        pages = inventory_pages(item, short)
        region_complete = source_complete or bool(pages and set(pages) <= ready_pages[short])
        values = numbers_in_text(item.get('text', ''))
        matching, candidates = [], []
        for record in by_source[short]:
            record_pages = record.get('locator', {}).get('pdf_pages', [])
            if not any(abs(a - b) <= 1 for a in pages for b in record_pages):
                continue
            common = values & record_values[record['record_id']]
            if common:
                matching.append(dict(record_id=record['record_id'], common_numbers=sorted(common)))
                reverse[record['record_id']].append(item['item_id'])
            elif not values:
                candidates.append(record['record_id'])
        excluded = bool(pages and all(p in set((excluded_pages or {}).get(short, [])) for p in pages))
        choice_matches = []
        if excluded:
            for record, cvalues in choice_values:
                common = values & cvalues
                if common:
                    choice_matches.append(dict(record_id=record['record_id'], common_numbers=sorted(common)))
        status = '由Choice承担' if choice_matches else '代码匹配' if matching else '候选' if candidates else '未匹配'
        if not region_complete and status in ('候选', '未匹配'):
            status = '待抽取'
        rows.append(dict(item_id=item['item_id'], source=short, note=item.get('note'), line=item.get('line'),
                         text=item.get('text'), context=item.get('context', []), headings=item.get('headings', []),
                         fit=item['mapping']['fit'], pages=pages, numbers=sorted(values),
                         excluded_financial_pages=excluded, status=status, matches=matching,
                         source_extraction_complete=source_complete,
                         comparison_region_complete=region_complete,
                         no_number_candidates=candidates, choice_matches=choice_matches,
                         manual_review=dict(status='pending', decision=None, reason=None, evidence_pages=[])))
    reverse_rows = [dict(record_id=r['record_id'], source=source_short(r, source_aliases),
                         source_extraction_complete=extraction_complete or source_short(r, source_aliases) in completed_sources,
                         record_type=r.get('record_type'), pages=r.get('locator', {}).get('pdf_pages', []),
                         matched_p1_items=reverse[r['record_id']], record=r)
                    for r in records if not reverse[r['record_id']]]
    counts = Counter(row['status'] for row in rows)
    region_counts = dict(inventory_items=len(rows),
        region_complete_items=sum(row['comparison_region_complete'] for row in rows),
        partial_region_complete_items=sum(row['comparison_region_complete'] and not row['source_extraction_complete'] for row in rows),
        ready_unmatched_items=sum(row['comparison_region_complete'] and row['status'] in ('未匹配', '候选') for row in rows),
        unready_code_match_items=sum(not row['comparison_region_complete'] and row['status'] == '代码匹配' for row in rows),
        pending_items=counts['待抽取'], choice_responsibility_items=counts['由Choice承担'],
        by_source={source: dict(inventory_items=len(selected),
            region_complete_items=sum(row['comparison_region_complete'] for row in selected),
            partial_region_complete_items=sum(row['comparison_region_complete'] and not row['source_extraction_complete'] for row in selected),
            ready_unmatched_items=sum(row['comparison_region_complete'] and row['status'] in ('未匹配', '候选') for row in selected),
            unready_code_match_items=sum(not row['comparison_region_complete'] and row['status'] == '代码匹配' for row in selected),
            pending_items=sum(row['status'] == '待抽取' for row in selected),
            status_counts=dict(Counter(row['status'] for row in selected)))
            for source in sorted({row['source'] for row in rows})
            for selected in [[row for row in rows if row['source'] == source]]})
    partial_regions = any(pages and source not in completed_sources for source, pages in ready_pages.items())
    return dict(schema='wiki-lab-s2-comparison/1',
        evaluation_status='candidate_matching_only' if extraction_complete else
                          'partial_regions' if partial_regions else
                          'partial_sources' if completed_sources else 'pending_extraction',
        extraction_complete=extraction_complete, completed_sources=sorted(completed_sources),
        ready_pages_by_source={source: sorted(pages) for source, pages in sorted(ready_pages.items()) if pages},
        region_summary=region_counts, method=dict(
        source='same source', pages='same or adjacent PDF page',
        numeric='at least one identical canonical decimal in declared stored/source-magnitude numbers',
        no_numeric='candidate only', inherited_page='last applicable S1 caption only',
        sampling_chunk=CHUNK_ASSIGNMENT_METHOD,
        region_readiness='all cited pages ready, or source complete; neighboring value matches never establish readiness',
        choice='fully excluded financial pages only; mixed boundary pages need PDF/manual review; '
               'numeric correspondence, not inferred object-key equivalence',
        limitation='code matches can include false positives; region counts only bound unmatched readback, '
                   'never an overall coverage-rate denominator; code counts do not establish semantic coverage'),
        item_count=len(rows), counts=dict(counts), items=rows, reverse_unmatched=reverse_rows)


def _stratum_seed(seed, name):
    encoded = json.dumps([seed, name], ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return int.from_bytes(hashlib.sha256(encoded).digest(), 'big')


def _sampling_strata(rows, key, seed):
    return [dict(key=name, seed=_stratum_seed(seed, name))
            for name in sorted({key(row) for row in rows}, key=str)]


def stratified_sample(rows, size, key, seed=SEED):
    """Deterministic round-robin allocation ensures each stratum is represented.

    Each stratum has its own derived RNG, so growth in a different stratum
    cannot alter its internal ranking. Small strata are fully reviewed;
    remaining slots cycle over shuffled strata. Final output is ID-sorted.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    names = sorted(groups, key=str)
    for name in names:
        groups[name].sort(key=lambda r: r.get('item_id', r.get('record_id', '')))
        random.Random(_stratum_seed(seed, name)).shuffle(groups[name])
    random.Random(seed).shuffle(names)
    selected = []
    while len(selected) < min(size, len(rows)):
        for name in names:
            if groups[name] and len(selected) < size:
                selected.append(groups[name].pop())
        if not any(groups.values()):
            break
    return sorted(selected, key=lambda r: r.get('item_id', r.get('record_id', '')))


def _chunk_for(row, chunks):
    """Assign the dominant core span on cited pages; ignore read-only context."""
    source, pages = row.get('source'), set(row.get('pages', []))
    best_id, best_score = 'unassigned', 0
    for chunk in chunks:
        chunk_source = chunk.get('source_short', chunk.get('short', chunk.get('source', chunk.get('source_id'))))
        if isinstance(chunk_source, dict):
            chunk_source = chunk_source.get('id', chunk_source.get('short'))
        if chunk_source is None and chunk.get('sources'):
            chunk_source = chunk['sources'][0].get('id')
        if chunk_source != source:
            continue
        if 'core_pages' in chunk:
            core_pages = set(chunk['core_pages'])
        elif 'pages' in chunk:
            core_pages = set(chunk['pages']) - set(chunk.get('context_pages', []))
        else:
            core_pages = set(chunk['sources'][0].get('pages', [])) if chunk.get('sources') else set()
            core_pages -= set(chunk.get('context_pages', []))
        fragments = chunk.get('fragments')
        if fragments:
            # Production chunks contain disjoint exact source spans. Prefer
            # offsets; text-only synthetic/simple fragments use text length.
            score = sum(max(0, fragment['end'] - fragment['start'])
                        if 'start' in fragment and 'end' in fragment else len(fragment.get('text', ''))
                        for fragment in fragments if fragment['page'] in pages
                        and ('core_pages' not in chunk or fragment['page'] in core_pages))
        else:
            score = len(pages & core_pages)
        if score > best_score:
            best_score = score
            best_id = chunk.get('id', chunk.get('chunk_id', 'unknown'))
    return best_id


def make_review_queues(comparison, records, chunks=None, source_aliases=None, seed=SEED,
                       chunk_plan_scope='supplied structural chunks'):
    chunks = chunks.get('chunks', []) if isinstance(chunks, dict) else (chunks or [])
    unmatched = [deepcopy(row) for row in comparison['items'] if row['status'] in ('未匹配', '候选')]
    for row in unmatched:
        row['chunk'] = _chunk_for(row, chunks)
    announcements = [row for row in unmatched if row['source'] in ('S46', 'S48', 'S50')]
    large = [row for row in unmatched if row['source'] in ('S34', 'S42')]
    sampled = stratified_sample(large, 60, lambda r: (r['source'], r['chunk']), seed)
    reverse = stratified_sample(comparison['reverse_unmatched'], 40,
                                lambda r: (r['source'], r['record_type']), seed + 1)
    semantic_pool = [dict(record_id=r['record_id'], source=source_short(r, source_aliases),
                          record_type=r.get('record_type'), pages=r.get('locator', {}).get('pdf_pages', []),
                          record=r) for r in records]
    semantic = stratified_sample(semantic_pool, 80, lambda r: (r['source'], r['record_type']), seed + 2)
    queues = dict(unmatched=announcements + sampled, reverse=reverse, semantic=semantic)
    for name, rows in queues.items():
        for row in rows:
            row['review'] = dict(status='pending', decision=None, reason=None, error_types=[], evidence_pages=[])
        rows.sort(key=lambda r: r.get('item_id', r.get('record_id')))
    return dict(schema='wiki-lab-s2-review-queues/1', seed=seed,
                seeds=dict(unmatched=seed, reverse=seed + 1, semantic=seed + 2),
                sampling=dict(algorithm=SAMPLING_ALGORITHM, stratum_seed_rule=STRATUM_SEED_RULE,
                    row_order='stable item_id/record_id ascending before per-stratum shuffle; select from end',
                    stratum_order='keys sorted by str, then shuffled with Random(base_seed)',
                    allocation='round robin over nonempty strata, stopping at requested total',
                    strata=dict(unmatched=_sampling_strata(large, lambda r: (r['source'], r['chunk']), seed),
                                reverse=_sampling_strata(comparison['reverse_unmatched'],
                                    lambda r: (r['source'], r['record_type']), seed + 1),
                                semantic=_sampling_strata(semantic_pool,
                                    lambda r: (r['source'], r['record_type']), seed + 2))),
                evaluation_status=comparison.get('evaluation_status', 'candidate_matching_only'),
                completed_sources=comparison.get('completed_sources', []),
                ready_pages_by_source=comparison.get('ready_pages_by_source', {}),
                region_summary=comparison.get('region_summary', {}),
                review_status='pending' if comparison.get('extraction_complete', True) else
                              'partial_ready' if comparison.get('completed_sources') or
                              comparison.get('region_summary', {}).get('region_complete_items') else 'not_ready',
                allocation='round robin over source/chunk (unmatched), source/type (reverse and semantic); '
                           'chunk assignment uses core overlap, then plan-order ties',
                chunk_assignment=CHUNK_ASSIGNMENT_METHOD,
                chunk_plan_scope=chunk_plan_scope,
                chunk_plan_ids=[chunk.get('id', chunk.get('chunk_id')) for chunk in chunks],
                unmatched_protocol='all unmatched announcement rows; 60 total from S34/S42 stratified by source/chunk',
                actual_counts={name: len(rows) for name, rows in queues.items()},
                population_counts=dict(unmatched_announcements=len(announcements), unmatched_large=len(large),
                                       reverse=len(comparison['reverse_unmatched']), semantic=len(records)),
                queues=queues, decisions=REVIEW_DECISIONS,
                semantic_checklist=['主体', '指标', '期间和as_of', '性质', '数值和单位换算', '范围和维度', '市场标记', '未知日期说明'])


def apply_reviews(queues, reviews):
    """Bind explicit reviewer judgments to a hash of the generated queues."""
    if reviews.get('queues_sha256') != json_hash(queues):
        raise ValueError('review queues hash mismatch')
    if not reviews.get('reviewer') or not reviews.get('review_id'):
        raise ValueError('reviewer and review_id required')
    result = deepcopy(queues)
    index = {(name, row.get('item_id', row.get('record_id'))): row
             for name, rows in result['queues'].items() for row in rows}
    seen = set()
    for entry in reviews.get('decisions', []):
        key = (entry['queue'], entry['id'])
        if key not in index or key in seen:
            raise ValueError('unknown or repeated review entry: %r' % (key,))
        seen.add(key)
        if entry.get('decision') not in REVIEW_DECISIONS[key[0]]:
            raise ValueError('illegal review decision')
        if not entry.get('reason') or not entry.get('evidence_pages'):
            raise ValueError('review reason and frozen-page evidence required')
        if entry['decision'] == '错误' and not entry.get('error_types'):
            raise ValueError('error review requires error_types')
        index[key]['review'] = dict(status='reviewed', decision=entry['decision'], reason=entry['reason'],
                                   error_types=entry.get('error_types', []), evidence_pages=entry['evidence_pages'],
                                   reviewer=reviews['reviewer'], review_id=reviews['review_id'])
    result['review_summary'] = review_summary(result)
    return result


def review_summary(queues):
    summary = {}
    for name, rows in queues['queues'].items():
        counts = Counter(row['review']['decision'] if row['review']['status'] == 'reviewed' else 'pending' for row in rows)
        errors = counts.get('错误', 0)
        reviewed = len(rows) - counts.get('pending', 0)
        uncertain = counts.get('拿不准', 0)
        assessable = reviewed - uncertain
        summary[name] = dict(counts=dict(counts), reviewed=reviewed, total=len(rows),
                             uncertain=uncertain, assessable=assessable,
                             error_rate=errors / assessable if assessable and name != 'unmatched' else None,
                             errors_per_reviewed=errors / reviewed if reviewed and name != 'unmatched' else None,
                             error_types=dict(Counter(t for row in rows for t in row['review'].get('error_types', []))))
    return summary


def json_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def usage_row(attempt):
    usage = attempt.get('usage', {}) or {}
    input_tokens, cached = usage.get('input_tokens'), usage.get('cached_input_tokens')
    commands = attempt.get('command_count', attempt.get('command_execution_count'))
    if commands is None and 'tools' in attempt:
        commands = sum(tool.get('type') == 'command_execution' for tool in attempt['tools'])
    return dict(stage=attempt.get('stage', attempt.get('chunk_id')), attempt=attempt.get('attempt'),
                status=effective_status(attempt), runtime_status=attempt.get('status'), input=input_tokens, cached=cached,
                uncached=input_tokens - cached if input_tokens is not None and cached is not None else None,
                output=usage.get('output_tokens'), reasoning=usage.get('reasoning_output_tokens'),
                commands=commands, seconds=attempt.get('seconds', attempt.get('elapsed_seconds')),
                prompt_chars=attempt.get('prompt_chars'), records=attempt.get('accepted_records', attempt.get('record_count')),
                output_records=attempt.get('output_records'), rejected_records=attempt.get('rejected_records'))


def effective_status(attempt):
    """A completed CLI turn can still fail S2 record acceptance."""
    return attempt.get('s2_status', attempt.get('status'))


def usage_totals(attempts):
    rows = [usage_row(attempt) for attempt in attempts]
    result = dict(calls=len(rows), by_status=dict(Counter(row['status'] for row in rows)))
    for field in ('input', 'cached', 'uncached', 'output', 'reasoning', 'commands', 'seconds'):
        known = [row[field] for row in rows if row[field] is not None]
        result[field] = sum(known) if known else None
        if field == 'seconds' and result[field] is not None:
            result[field] = round(result[field], 2)
        result[field + '_unknown_calls'] = len(rows) - len(known)
    result['cache_ratio_reported'] = (result['cached'] / result['input']
                                      if result['input'] and result['cached'] is not None else None)
    result['amount'] = None
    return result


def _attempts(value):
    return value.get('attempts', []) if isinstance(value, dict) else value


def usage_comparison(p1, s2, source_chars, accepted_records, original_chars=405124):
    p1, s2 = _attempts(p1), _attempts(s2)
    completed = ('completed', 'accepted', 'success')
    result = dict(schema='wiki-lab-s2-usage/1', p1={}, s2={}, s2_rows=[usage_row(a) for a in s2])
    for name, attempts in [('p1', p1), ('s2', s2)]:
        result[name]['all'] = usage_totals(attempts)
        result[name]['success'] = usage_totals([a for a in attempts if effective_status(a) in completed])
        result[name]['failed'] = usage_totals([a for a in attempts if effective_status(a) not in completed])
    extracts = [a for a in p1 if a.get('stage', '').startswith('extract-')]
    result['p1']['extraction_only'] = usage_totals(extracts)
    result['p1']['extraction_success'] = usage_totals([a for a in extracts if a.get('status') in completed])
    result['p1']['extraction_failed'] = usage_totals([a for a in extracts if a.get('status') not in completed])
    result['s2']['source_chars'] = source_chars
    result['p1']['source_chars'] = original_chars
    for name, chars, totals in [('s2', source_chars, result['s2']['all']),
                                ('p1_extraction', original_chars, result['p1']['extraction_only'])]:
        ratios = {field + '_per_10000_source_chars': totals[field] * 10000 / chars
                  if totals[field] is not None and chars else None
                  for field in ('input', 'cached', 'uncached', 'output', 'reasoning')}
        result[name if name == 's2' else 'p1']['ratios'] = ratios
    output = result['s2']['all']['output']
    result['s2']['output_tokens_per_accepted_record'] = output / accepted_records if output is not None and accepted_records else None
    result['limitations'] = [
        'Unknown usage is null with unknown-call counts, never counted as measured zero.',
        'P1 extracted financial statement pages that S2 excludes; show extraction-only and all-stage costs separately.',
        'S2 totals include probes, failed attempts and retries; accepted-record denominator excludes Choice and rejected records.',
        'P1 checks_rejected attempts are listed under failed even if later accepted by a zero-model repair.',
        'Reasoning tokens are a subset of output tokens and must not be added to output.',
        'No prices or billing amount are inferred.',
        'CLI用量不包含主会话及子agent的开发、审查、逐页回读成本；这些成本本轮未计量，不能视为零。',
    ]
    return result


def usage_markdown(result):
    lines = ['# S2 用量对照', '',
             '汇总“秒”为逐次调用耗时之和；两worker并发时各调用分别计时，不等于运行的墙钟总时长。', '',
             '|范围|调用|输入|缓存|未缓存|输出|推理|命令|秒|未报告输入调用|',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for side in ('p1', 's2'):
        for group, totals in result[side].items():
            if not isinstance(totals, dict) or 'calls' not in totals:
                continue
            fields = [totals.get(k) for k in ('calls', 'input', 'cached', 'uncached', 'output', 'reasoning', 'commands', 'seconds', 'input_unknown_calls')]
            lines.append('|%s %s|%s|' % (side, group, '|'.join('未知' if v is None else str(v) for v in fields)))
    lines.extend(['', '## 每次调用', '', '|块/阶段|尝试|状态|输入|缓存|未缓存|输出|推理|命令|秒|prompt字符|输出记录|收下记录|',
                  '|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|'])
    for row in result['s2_rows']:
        lines.append('|' + '|'.join('未知' if row[k] is None else str(row[k]) for k in
                     ('stage', 'attempt', 'status', 'input', 'cached', 'uncached', 'output', 'reasoning', 'commands', 'seconds', 'prompt_chars', 'output_records', 'records')) + '|')
    lines.extend(['', '```json', json.dumps(dict(p1_ratios=result['p1']['ratios'], s2_ratios=result['s2']['ratios'],
                 output_tokens_per_accepted_record=result['s2']['output_tokens_per_accepted_record']), ensure_ascii=False, indent=2), '```', ''])
    lines += ['- ' + note for note in result['limitations']]
    return '\n'.join(lines) + '\n'


def load_records(path):
    path = Path(path)
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    result = []
    for file in files:
        value = json.loads(file.read_text())
        result.extend(value.get('records', []) if isinstance(value, dict) else value)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--records-dir', type=Path, required=True)
    parser.add_argument('--choice-records', type=Path)
    parser.add_argument('--chunks', type=Path, required=True)
    parser.add_argument('--aliases', type=Path, help='JSON full source_id -> Sxx map')
    parser.add_argument('--excluded-pages', type=Path)
    parser.add_argument('--ready-pages', type=Path, help='JSON ready PDF pages by source, after current leaf-block verification')
    parser.add_argument('--p1-usage', type=Path, required=True)
    parser.add_argument('--s2-usage', type=Path, required=True)
    parser.add_argument('--source-chars', type=int, required=True)
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--incomplete', action='store_true', help='mark unrun extraction as pending, not missed coverage')
    args = parser.parse_args()
    read = lambda p: json.loads(p.read_text())
    aliases = read(args.aliases) if args.aliases else {}
    extracted = load_records(args.records_dir)
    compared = compare_inventory(read(args.inventory), extracted, aliases,
                                 read(args.excluded_pages) if args.excluded_pages else {},
                                 load_records(args.choice_records) if args.choice_records else [],
                                 extraction_complete=not args.incomplete,
                                 ready_pages_by_source=read(args.ready_pages) if args.ready_pages else {})
    queues = make_review_queues(compared, extracted, read(args.chunks), aliases, args.seed)
    usage = usage_comparison(read(args.p1_usage), read(args.s2_usage), args.source_chars, len(extracted))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, value in [('comparison.json', compared), ('review-queues.json', queues), ('usage-comparison.json', usage)]:
        (args.out / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    (args.out / 'USAGE.md').write_text(usage_markdown(usage))


if __name__ == '__main__':
    main()
