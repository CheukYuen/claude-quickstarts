#!/usr/bin/env python3
"""Plan at most one bounded, source-only S2 supplemental extraction per document.

No model calls. Coverage notes locate sections but never become prompt evidence.
Only exact non-excluded spans from frozen pages are rendered into request input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import corpus
import s2_chunks


def _load(path):
    return json.loads(Path(path).read_text())


def _basis_text(item):
    basis = item.get('basis', [])
    return '\n'.join(str(text) for text in basis) if isinstance(basis, list) else str(basis)


def page_pointers(text, source):
    """Bounded PDF-page syntax; numbers without a page label are not pointers."""
    patterns = (
        r'(?i)(?:\bpp?\.?\s*|\bPDF\s*(?:page(?:s)?|页序?)?\s*)(\d{1,4})'
        r'(?:\s*[-–—~至到]\s*(\d{1,4}))?',
        r'(?:第|页序\s*|页\s*)(\d{1,4})(?:\s*[-–—~至到]\s*(\d{1,4}))?\s*页?',
    )
    pages = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            # Reject explicitly foreign-source pointers instead of quietly
            # taking the same page number from this document.
            prior_source = re.findall(r'(?<![A-Za-z0-9])S\d+(?![A-Za-z0-9])',
                                      text[max(0, match.start()-20):match.start()])
            if prior_source and prior_source[-1] != source:
                continue
            left, right = int(match.group(1)), int(match.group(2) or match.group(1))
            if 0 < left <= right <= 5000:
                pages.update(range(left, right + 1))
    return sorted(pages)


def _unit_aliases(unit):
    title = unit['title']
    aliases = {title, title.replace(' ', '')}
    for part in title.split(' / '):
        aliases.add(part)
        clean = re.sub(r'^(?:第[一二三四五六七八九十]+节|[一二三四五六七八九十]+、|\d+[、.．])\s*', '', part)
        if len(clean) >= 4:
            aliases.add(clean)
        section = re.match(r'^(第[一二三四五六七八九十]+节)', part)
        if section:
            aliases.add(section.group(1))
        note = re.match(r'^([一二三四五六七八九十]+)、', part)
        if note:
            aliases.add('附注' + note.group(1))
    return aliases


def _all_units(chunks):
    result = []
    for chunk in chunks:
        if chunk.get('units'):
            for unit in chunk['units']:
                result.append(dict(unit=unit, chunk=chunk))
        else:
            result.append(dict(unit=dict(title='；'.join(chunk.get('sections', [])) or chunk['id'],
                                         fragments=chunk['fragments']), chunk=chunk))
    return result


def _named_units(text, units):
    compact = re.sub(r'\s+', '', text)
    matches = [entry for entry in units if any(re.sub(r'\s+', '', alias) in compact
                for alias in _unit_aliases(entry['unit']) if alias)]
    # The eighth top-level periodic-report section includes all its notes,
    # even though only the audit-introduction unit repeats that section label.
    if '第八节' in compact or '财务报告' in compact:
        starts = [entry['unit'].get('start') for entry in units
                  if entry['unit']['title'].startswith('第八节')]
        starts = [start for start in starts if start is not None]
        if starts:
            anchor = min(starts)
            matches += [entry for entry in units if entry['unit'].get('start', -1) >= anchor]
    return matches


def _page_map(source):
    result = {}
    for page in source['page_records']:
        path = Path(page['corpus_path'])
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != page['text_sha256']:
            raise ValueError('Frozen page changed: %s p%d' % (source['id'], page['page']))
        result[page['page']] = dict(text=raw.decode('utf-8'), sha256=digest)
    return result


def _subtract(start, end, removed):
    intervals = [(start, end)]
    for left, right in removed:
        remaining = []
        for lo, hi in intervals:
            if right <= lo or left >= hi:
                remaining.append((lo, hi))
            else:
                if lo < left:
                    remaining.append((lo, left))
                if right < hi:
                    remaining.append((right, hi))
        intervals = remaining
    return intervals


def canonical_spans(fragments, pages, excluded=()):
    """Deduplicate overlaps and subtract exclusions using frozen offsets."""
    removed = {}
    for fragment in excluded:
        removed.setdefault(fragment['page'], []).append((fragment['start'], fragment['end']))
    intervals = {}
    for fragment in fragments:
        number = fragment['page']
        if number not in pages:
            raise ValueError('Request fragment page is outside frozen input')
        text = pages[number]['text']
        start, end = fragment.get('start', 0), fragment.get('end', len(text))
        if not (0 <= start <= end <= len(text)) or fragment.get('text', text[start:end]) != text[start:end]:
            raise ValueError('Request fragment differs from its frozen page span')
        intervals.setdefault(number, []).extend(_subtract(start, end, removed.get(number, [])))
    result = []
    for number, values in sorted(intervals.items()):
        merged = []
        for start, end in sorted(set(values)):
            if start == end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            result.append(dict(page=number, start=start, end=end,
                text=pages[number]['text'][start:end], page_sha256=pages[number]['sha256']))
    return result


def _attempted(root, source):
    for path in (root / 'logs').glob('*/attempt-*/record.json'):
        row = _load(path)
        if row.get('mode') == 'request' and row.get('chunk_id', '').split('.')[0] == source:
            return str(path)
    ledger = root / 'calls.jsonl'
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            row = json.loads(line)
            stage = row.get('stage', '')
            if stage in ('request-' + source, 'supplement-' + source) or stage.endswith('-' + source + '.request1'):
                return str(ledger) + '#call-' + str(row.get('number'))
    return None


def _save_plan(root, plan):
    path = root / 'request-plans' / (plan['source'] + '.json')
    if path.exists():
        old = path.read_bytes()
        if json.loads(old) != plan:
            digest = hashlib.sha256(old).hexdigest()
            history = root / 'request-plans/history' / (plan['source'] + '-' + digest + '.json')
            if not history.exists():
                history.parent.mkdir(parents=True, exist_ok=True)
                history.write_bytes(old)
    corpus.dump(path, plan)


def plan_requests(root, max_chars=20000):
    """Return plans; no-pointer/over-limit/prior-attempt cases are unschedulable."""
    root = Path(root).resolve()
    if max_chars < 1:
        raise ValueError('Request max_chars must be positive')
    manifest = _load(root / 'corpus-manifest.json')
    chunks = _load(root / 'chunks.json')
    coverage = _load(root / 'document-coverage.json')
    exclusions = _load(root / 'excluded-pages.json') if (root / 'excluded-pages.json').exists() else []
    plans = []
    for source in manifest['sources']:
        short = source['id']
        missing = [item for item in coverage.get(short, []) if item['status'] not in ('已抽', '本文档无')]
        plan = dict(source=short, stage='request-' + short, eligible=False, reason='no_uncovered_items', item_ids=[],
                    uncovered_item_ids=[item['item_id'] for item in missing], unresolved_item_ids=[],
                    chunk=None, request='', max_chars=max_chars, item_plans=[])
        prior = _attempted(root, short)
        if prior:
            plan.update(reason='already_attempted', prior_attempt=prior)
        elif missing:
            available = [chunk for chunk in chunks if chunk.get('source', chunk['id'].split('.')[0]) == short]
            units, pages = _all_units(available), _page_map(source)
            excluded = [fragment for ex in exclusions if ex['source'] == short for fragment in ex.get('fragments', [])]
            selected, context = [], []
            sections = set()
            for item in missing:
                basis = _basis_text(item)
                explicit = page_pointers(basis, short)
                named = _named_units(basis, units)
                checked = sorted(set(item.get('checked_pages', [])))
                pointed_pages = explicit or ([] if named else checked)
                candidates = named or units
                fragments = [fragment for entry in candidates for fragment in entry['unit']['fragments']
                             if not pointed_pages or fragment['page'] in pointed_pages]
                method = 'basis_pages_and_sections' if explicit and named else 'basis_pages' if explicit else (
                    'basis_sections' if named else 'checked_pages' if checked else 'no_pointer')
                if method == 'no_pointer':
                    fragments = []
                fragments = canonical_spans(fragments, pages, excluded)
                selected_pages = {fragment['page'] for fragment in fragments}
                item_plan = dict(item_id=item['item_id'], name=item.get('name', item['item_id']), method=method,
                    explicit_pages=explicit, checked_pages=checked,
                    selected_pages=sorted(selected_pages), sections=sorted({entry['unit']['title'] for entry in candidates
                        if any(fragment['page'] in selected_pages for fragment in entry['unit']['fragments'])}),
                    status='selected' if fragments else 'unschedulable',
                    reason='pointed_source_spans' if fragments else 'no_available_section_or_page_pointer')
                plan['item_plans'].append(item_plan)
                if not fragments:
                    plan['unresolved_item_ids'].append(item['item_id'])
                    continue
                plan['item_ids'].append(item['item_id'])
                selected.extend(fragments)
                sections.update(item_plan['sections'])
                for entry in candidates:
                    if any(fragment['page'] in selected_pages for fragment in entry['unit']['fragments']):
                        context.extend(entry['unit'].get('context_fragments', []))
                        if not entry['chunk'].get('units'):
                            context.extend(entry['chunk'].get('context_fragments', []))
            core = canonical_spans(selected, pages, excluded)
            context = canonical_spans(context, pages, excluded)
            core_pages = sorted({fragment['page'] for fragment in core})
            context = [fragment for fragment in context if fragment['page'] not in core_pages]
            chars = sum(len(fragment['text']) for fragment in core)
            plan.update(core_chars=chars, context_chars=sum(len(fragment['text']) for fragment in context))
            if not core:
                plan['reason'] = 'no_available_pointer'
            elif chars > max_chars:
                plan['reason'] = 'blocked_overlimit'
                plan['detail'] = '每份文档最多补抽一次；所指范围%d字符超过%d上限，不能拆成多次或静默截短。' % (chars, max_chars)
            else:
                chunk_id = short + '.request1'
                text = s2_chunks._render(source, core, context)
                path = root / 'request-inputs' / (chunk_id + '.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.read_bytes() != text.encode('utf-8'):
                    previous = path.read_bytes()
                    old_hash = hashlib.sha256(previous).hexdigest()
                    history = path.parent / 'history' / (chunk_id + '-' + old_hash + '.md')
                    if not history.exists():
                        history.parent.mkdir(parents=True, exist_ok=True)
                        history.write_bytes(previous)
                path.write_bytes(text.encode('utf-8'))
                chunk = dict(id=chunk_id, source=short, source_id=source['source_id'],
                    path=str(path), sha256=hashlib.sha256(text.encode()).hexdigest(), chars=chars,
                    wrapped_chars=len(text), sources=[dict(id=short, pages=core_pages)], core_pages=core_pages,
                    context_pages=sorted({fragment['page'] for fragment in context}), fragments=core,
                    context_fragments=context, sections=sorted(sections), units=[], request_item_ids=plan['item_ids'])
                names = [item['item_id'] + ' ' + item.get('name', item['item_id']) for item in missing
                         if item['item_id'] in plan['item_ids']]
                request = ('只定向抽取这些必抽清单项：' + '；'.join(names) + '。'
                    '只使用本次输入的章节和页内范围，不能凭先前记录、笔记或未读部分补全。'
                    '其余清单项标本块未覆盖。已抽项目仍须逐字摘录、局部编号和覆盖页码。'
                    '本次核心PDF页序：' + ','.join(map(str, core_pages)) + '；章节：' + '；'.join(sorted(sections)) + '。')
                plan.update(eligible=True, reason='ready', chunk=chunk, request=request)
        _save_plan(root, plan)
        plans.append(plan)
    return plans


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--max-chars', type=int, default=20000)
    args = parser.parse_args()
    plans = plan_requests(args.run_dir, args.max_chars)
    print(json.dumps([dict(source=plan['source'], eligible=plan['eligible'], reason=plan['reason'],
                           item_ids=plan['item_ids']) for plan in plans], ensure_ascii=False))
