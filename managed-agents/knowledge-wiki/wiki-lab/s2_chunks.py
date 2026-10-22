#!/usr/bin/env python3
"""Re-prepare P1 inputs and cut exact frozen-text spans at structural headings.

The heading vocabulary is the periodic-report template, never a page-number map.
Missing/duplicate template headings or discontinuous note numbers are fatal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

import corpus


HERE = Path(__file__).resolve().parent
DEFAULT_P1 = HERE / 'runs/p1-20261021'
DEFAULT_REGISTRY = HERE / 'runs/p0-20261021/source-registry.json'
SECTION_NAMES = ('释义', '公司简介和主要财务指标', '管理层讨论与分析',
                 '公司治理、环境和社会', '重要事项', '股份变动及股东情况',
                 '债券相关情况', '财务报告')
NOTE_NAMES = ('审计报告', '财务报表', '公司基本情况', '财务报表的编制基础',
              '重要会计政策及会计估计', '税项', '合并财务报表项目注释', '研发支出',
              '合并范围的变更', '在其他主体中的权益', '政府补助', '与金融工具相关的风险',
              '公允价值的披露', '关联方及关联交易', '股份支付', '承诺及或有事项',
              '资产负债表日后事项', '其他重要事项', '母公司财务报表主要项目注释', '补充资料')
CHINESE_NUMBERS = ('一', '二', '三', '四', '五', '六', '七', '八', '九', '十',
                   '十一', '十二', '十三', '十四', '十五', '十六', '十七',
                   '十八', '十九', '二十')


class BoundaryError(ValueError):
    """A structural boundary was not identifiable without guessing."""


def _require_s2_run(run_dir):
    run_dir = Path(run_dir).resolve()
    if run_dir.parent != HERE / 'runs' or not run_dir.name.startswith('s2-'):
        raise ValueError('S2 output must be wiki-lab/runs/s2-*')
    return run_dir


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _pages(source):
    result = []
    for record in source['page_records']:
        path = Path(record['corpus_path'])
        # read_bytes preserves CR/LF, unlike universal-newline read_text.
        text = path.read_bytes().decode('utf-8')
        if _hash(text) != record['text_sha256']:
            raise ValueError('Frozen page hash changed: %s p%s' % (source['id'], record['page']))
        result.append({'page': record['page'], 'text': text,
                       'text_sha256': record['text_sha256']})
    return result


def _flatten(pages):
    offset, result, texts = 0, [], []
    for page in pages:
        result.append({**page, 'global_start': offset, 'global_end': offset + len(page['text'])})
        texts.append(page['text'])
        offset += len(page['text'])
    return ''.join(texts), result


def _exact_heading(text, prefix, title, start=0, end=None):
    # A whole physical line prevents the table of contents and table rows
    # from supplying boundaries, even if they repeat the same heading words.
    pattern = r'(?m)^[ \t]*' + prefix + r'[ \t]*' + re.escape(title) + r'[ \t]*\r?$'
    found = list(re.finditer(pattern, text[start:end]))
    if len(found) != 1:
        raise BoundaryError('Expected one heading %s; found %d' % (title, len(found)))
    return start + found[0].start()


def _fragments(pages, start, end):
    result = []
    for page in pages:
        lo, hi = max(start, page['global_start']), min(end, page['global_end'])
        if lo < hi:
            a, b = lo - page['global_start'], hi - page['global_start']
            result.append({'page': page['page'], 'start': a, 'end': b,
                           'text': page['text'][a:b], 'page_sha256': page['text_sha256']})
    return result


def periodic_units(pages):
    """Return exact disjoint content spans and the financial-statement exclusion."""
    text, indexed = _flatten(pages)
    headings = [_exact_heading(text, '第' + CHINESE_NUMBERS[i] + '节', title)
                for i, title in enumerate(SECTION_NAMES)]
    if headings != sorted(headings):
        raise BoundaryError('Periodic-report section headings are out of order')
    financial = headings[-1]
    notes = [_exact_heading(text, CHINESE_NUMBERS[i] + '、', title, start=financial)
             for i, title in enumerate(NOTE_NAMES)]
    if notes != sorted(notes):
        raise BoundaryError('Financial note headings are out of order')
    units, excluded = [], []

    def add(start, end, title, parent, **extra):
        if end > start:
            units.append({'title': title, 'parent': parent, 'start': start, 'end': end,
                          'fragments': _fragments(indexed, start, end), **extra})

    # The original front matter is retained as its own structure, including
    # the contents. It is not silently omitted from the same-input experiment.
    add(0, headings[0], '封面、重要提示及目录', '前置内容')
    for i, start in enumerate(headings[:-1]):
        add(start, headings[i + 1], '第%s节 %s' % (CHINESE_NUMBERS[i], SECTION_NAMES[i]),
            '第%s节' % CHINESE_NUMBERS[i])
    # Section VIII and the audit report are kept; statements stop exactly
    # at note III, even when the boundary lies partway through a page.
    add(financial, notes[1], '第八节 财务报告 / 一、审计报告', '财务附注前段')
    excluded.append({'source': 'S34', 'title': '二、财务报表',
                     'reason': '合并三表由Choice承担；母公司报表及所有者权益变动表本轮不进记录',
                     'fragments': _fragments(indexed, notes[1], notes[2])})
    for i in range(2, len(notes)):
        start, end = notes[i], notes[i + 1] if i + 1 < len(notes) else len(text)
        title = '%s、%s' % (CHINESE_NUMBERS[i], NOTE_NAMES[i])
        if i != 6:
            add(start, end, title, '财务附注前段' if i < 6 else '财务附注后段')
            continue
        # The numbered items under VII must be continuous. The expected next
        # number avoids treating asset-table movement rows as note boundaries.
        body = text[start:end]
        candidates = list(re.finditer(r'(?m)^[ \t]*(\d{1,3})[、.．][ \t]*([^\r\n]+?)[ \t]*\r?$', body))
        candidates = [m for m in candidates if len(m.group(2).strip()) <= 80
                      and not re.search(r'\d', m.group(2))
                      and not re.search(r'\s{2,}\S', m.group(2).strip())]
        chosen, expected = [], 1
        for match in candidates:
            number = int(match.group(1))
            if number == expected:
                chosen.append(match)
                expected += 1
        if not chosen or chosen[0].group(2).strip() != '货币资金':
            raise BoundaryError('Note VII first item was not identified')
        # Later numbered note candidates above the expected index imply a
        # missing boundary, rather than permission to merge through it.
        if any(int(m.group(1)) >= expected for m in candidates):
            raise BoundaryError('Discontinuous note VII numbering at %d' % expected)
        if chosen[-1].group(2).strip() != '其他':
            raise BoundaryError('Note VII final numbered item was not identified')
        for j, match in enumerate(chosen):
            lo = start if j == 0 else start + match.start()
            hi = start + chosen[j + 1].start() if j + 1 < len(chosen) else end
            add(lo, hi, title + ' / ' + match.group(0).strip(), title,
                note_group=title, note_number=int(match.group(1)))
    return units, excluded


def _has_table_header(text):
    return any(_header_line(line) for line in text.splitlines())


def _header_line(line):
    """A printed multi-column header, not an arbitrary unit or prose line."""
    folded = line.casefold()
    words = ('项目', '本期', '上期', '期末', '期初', '子公司名称', '注册资本',
             '持股比例', '账面', '金额', '余额', '名称', '收入', '成本',
             'investment rating', 'company', 'ticker', 'bloomberg', 'analyst',
             'currency', 'previous', 'current')
    if (sum(word in folded for word in words) >= 2 and re.search(r'\S[ \t]+\S', line)
            and not re.search(r'[。；;]', line) and len(line.strip()) <= 220):
        return True
    # Broker tables commonly identify columns solely by dated A/E or quarter
    # labels. Two such labels on one multi-column line establish a header.
    dates = re.findall(r'\b(?:20\d{2}[AE]|[1-4]Q\d{2})\b', line, re.I)
    return len(dates) >= 2


def _content_lines(text):
    result = []
    for line in text.replace('\f', '\n').splitlines():
        stripped = line.strip()
        if not stripped or re.fullmatch(r'\d+\s*/\s*\d+', stripped):
            continue
        if re.search(r'20\d{2}\s*年?(?:半年度|年度)报告$', stripped):
            continue
        result.append(line)
    return result


def _table_row(line):
    stripped = line.strip()
    if re.fullmatch(r'[-−(（]?\d[\d,\.()%－−-]*[)）]?', stripped):
        return True
    cells = re.split(r'\s{2,}|\t+', stripped)
    return ((len(cells) >= 2 and any(re.search(r'\d', cell) for cell in cells))
            or len(cells) >= 3)


def _table_terminator(line):
    stripped = line.strip()
    if re.match(r'^(?:第[一二三四五六七八九十]+节|[一二三四五六七八九十]+、|'
                r'\d+[、.．]\s*\D|\(?\d+\)\s*\D|其他说明|注\s*\d*\s*[:：])', stripped):
        return True
    return not _table_row(line) and bool(re.search(r'[。；;]', stripped))


def _continuation_header(before, after, pages):
    """Prove table continuity at this boundary or carry nothing; never guess.

    A new header on the first core page is already available to the model.
    Otherwise the first row must continue an unbroken table ending the prior
    core span, with a detectable header in that very table. Unknown header
    continuation is fatal rather than borrowing a header from elsewhere.
    """
    next_lines = _content_lines(after['text'])
    if not next_lines:
        return [], 'no_content_after_boundary'
    first_row = next((i for i, line in enumerate(next_lines) if _table_row(line)), None)
    first_header = next((i for i, line in enumerate(next_lines) if _header_line(line)), None)
    if first_header is not None and first_header <= (first_row if first_row is not None else 3):
        return [], 'next_core_contains_table_header'
    # A paragraph, section title, or other substantive sentence before the
    # first numeric/multi-column row starts a fresh item rather than a table.
    if first_row is None or first_row > 1 or any(_table_terminator(x) for x in next_lines[:first_row + 1]):
        return [], 'boundary_not_inside_table'
    # Include only the printed prefix before a partial first fragment. This
    # lets an adaptive split carry a same-page header immediately preceding
    # that fragment, while table terminators still prevent crossing tables.
    before = list(before)
    if before and before[0]['start']:
        first_page = next(p for p in pages if p['page'] == before[0]['page'])
        before[0] = {**before[0], 'text': first_page['text'][:before[0]['end']]}
    previous = [(fragment['page'], line) for fragment in before for line in _content_lines(fragment['text'])]
    if not previous or not _table_row(previous[-1][1]):
        return [], 'prior_core_does_not_end_in_table'
    header_page = None
    for page_number, line in reversed(previous):
        if _header_line(line):
            header_page = page_number
            break
        if _table_terminator(line):
            break
    if header_page is None:
        raise BoundaryError('Table continuation at p%d lacks an unbroken, identifiable table header' % after['page'])
    page = next(p for p in pages if p['page'] == header_page)
    return [{'page': page['page'], 'start': 0, 'end': len(page['text']),
             'text': page['text'], 'page_sha256': page['text_sha256']}], 'verified_continuing_table_header'


def _split_large_unit(unit, max_chars, pages):
    """Split only at page edges; carry the most recent table-header page."""
    if sum(len(f['text']) for f in unit['fragments']) <= max_chars:
        return [unit]
    page_lookup = {p['page']: p for p in pages}
    parts, pending, processed, chars = [], [], [], 0
    for fragment in unit['fragments']:
        if pending and chars + len(fragment['text']) > max_chars:
            parts.append({**unit, 'fragments': pending,
                          'split_at_page_boundary': True})
            context, reason = _continuation_header(processed, fragment, pages)
            pending, chars = [], 0
            # Stored on the next part, not the one just completed.
            unit = {**unit, 'context_pages': [f['page'] for f in context], 'context_reason': reason}
        pending.append(fragment)
        processed.append(fragment)
        chars += len(fragment['text'])
    if pending:
        parts.append({**unit, 'fragments': pending, 'split_at_page_boundary': True})
    # The first part cannot refer to a previous page. Intermediate parts keep
    # the carry page established when they were started.
    for part in parts:
        part.setdefault('context_pages', [])
        part['context_pages'] = [n for n in part['context_pages']
                                 if n not in {f['page'] for f in part['fragments']}]
        part['context_fragments'] = [
            {'page': n, 'start': 0, 'end': len(page_lookup[n]['text']),
             'text': page_lookup[n]['text'], 'page_sha256': page_lookup[n]['text_sha256']}
            for n in part['context_pages']]
    return parts


def _render(source, fragments, context_fragments=()):
    texts = ['[SOURCE: %s]\n[DISCLOSED_ON: %s]\n' % (source['id'], source['date'])]
    for context, fs in ((True, context_fragments), (False, fragments)):
        for fragment in fs:
            texts.append('\n[PAGE: %d] [MODE: %s] [CHARS: %d:%d]\n' % (
                fragment['page'], 'READ_ONLY_CONTEXT' if context else 'EXTRACT',
                fragment['start'], fragment['end']))
            texts.append(fragment['text'])
    return ''.join(texts)


def build_chunks(manifest, run_dir, max_chars=20000):
    if max_chars < 1:
        raise ValueError('max_chars must be positive')
    run_dir = Path(run_dir)
    chunks, exclusions, boundaries = [], [], []
    for source in manifest['sources']:
        pages = _pages(source)
        text, indexed = _flatten(pages)
        if source['id'] == 'S34':
            units, excluded = periodic_units(pages)
        else:
            title = '研报全文（已有PDF p1–6）' if source['id'] == 'S42' else '公告全文'
            units = [{'title': title, 'parent': title, 'start': 0, 'end': len(text),
                      'fragments': _fragments(indexed, 0, len(text))}]
            excluded = []
        for exclusion in excluded:
            exclusions.append({**exclusion, 'pages': sorted({f['page'] for f in exclusion['fragments']})})
        expanded = []
        for unit in units:
            boundaries.append({'source': source['id'], 'title': unit['title'],
                               'parent': unit['parent'], 'pages': [f['page'] for f in unit['fragments']],
                               'start': unit['start'], 'end': unit['end']})
            expanded.extend(_split_large_unit(unit, max_chars, pages))
        pending, chars, index = [], 0, 1

        def write_pending():
            nonlocal pending, chars, index
            if not pending:
                return
            fragments = [f for u in pending for f in u['fragments']]
            # Never concatenate across the omitted statements or any other
            # gap: core fragments keep exact per-page character boundaries.
            context = [f for u in pending for f in u.get('context_fragments', [])]
            core_pages = sorted({f['page'] for f in fragments})
            context = [f for f in context if f['page'] not in core_pages]
            rendered = _render(source, fragments, context)
            chunk_id = '%s.s%02d' % (source['id'], index)
            path = run_dir / 'corpus/chunks' / (chunk_id + '.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rendered.encode('utf-8'))
            chunks.append({'id': chunk_id, 'source_id': source['id'], 'source': source['id'],
                           'path': str(path.resolve()), 'sha256': _hash(rendered),
                           'chars': chars, 'wrapped_chars': len(rendered),
                           'sources': [{'id': source['id'], 'pages': core_pages}],
                           'core_pages': core_pages, 'context_pages': sorted({f['page'] for f in context}),
                           'sections': [u['title'] for u in pending],
                           'parent': pending[0]['parent'],
                           'note_group': pending[0].get('note_group'),
                           'fragments': fragments, 'context_fragments': context,
                           'units': pending,
                           'single_page_over_limit': len(core_pages) == 1 and chars > max_chars,
                           'context_reasons': sorted({u['context_reason'] for u in pending if u.get('context_reason')})})
            pending, chars, index = [], 0, index + 1

        for unit in expanded:
            size = sum(len(f['text']) for f in unit['fragments'])
            if pending and (pending[-1]['parent'] != unit['parent'] or chars + size > max_chars
                            or unit.get('context_pages') or pending[-1].get('split_at_page_boundary')):
                write_pending()
            pending.append(unit)
            chars += size
        write_pending()
    # The existing S42 text limitation is part of the declared exclusions.
    for source in manifest['sources']:
        omitted = sorted(set(range(1, source['pdf_page_count'] + 1)) - set(source['pages']))
        if omitted:
            exclusions.append({'source': source['id'], 'pages': omitted,
                               'reason': 'P1冻结输入仅这些已有逐页文字；本轮不补提取', 'fragments': []})
    corpus.dump(run_dir / 'chunks.json', chunks)
    corpus.dump(run_dir / 'structural-boundaries.json', boundaries)
    corpus.dump(run_dir / 'excluded-pages.json', exclusions)
    write_chunks_md(run_dir, chunks, exclusions, max_chars)
    return chunks


def write_chunks_md(run_dir, chunks, exclusions, max_chars):
    lines = ['# S2 结构切块', '', '核心原文字数上限：%d；按原文标题识别边界，页内边界用字符区间保存。' % max_chars,
             '超限条目只在页边界切；上下文页标为只读，不能单独支持新记录。', '',
             '|块|来源|核心PDF页序|只读上下文页|章节|原文字符|SHA-256|',
             '|---|---|---|---|---|---:|---|']
    for c in chunks:
        lines.append('|%s|%s|%s|%s|%s|%d|%s|' % (
            c['id'], c['source_id'], ','.join(map(str, c['core_pages'])),
            ','.join(map(str, c['context_pages'])), '；'.join(c['sections']), c['chars'], c['sha256']))
    lines += ['', '## 排除页和范围', '']
    for ex in exclusions:
        spans = ['p%d 字符%d:%d' % (f['page'], f['start'], f['end']) for f in ex['fragments']]
        lines.append('- %s PDF页序%s：%s。%s' % (ex['source'], ex['pages'], ex['reason'], '；'.join(spans)))
    lines += ['', '完整标题边界见 structural-boundaries.json；排除范围逐页字符区间见 excluded-pages.json。', '']
    (Path(run_dir) / 'CHUNKS.md').write_text('\n'.join(lines), encoding='utf-8')


def compare_pages(manifest, p1_manifest):
    old = {s['id']: s for s in p1_manifest['sources']}
    if set(old) != {s['id'] for s in manifest['sources']}:
        raise ValueError('S2 sources differ from P1')
    rows = []
    for source in manifest['sources']:
        previous = old[source['id']]
        if source['source_id'] != previous['source_id'] or source['pdf_sha256'] != previous['pdf_sha256']:
            raise ValueError('S2 original differs from P1: ' + source['id'])
        before = {p['page']: p['text_sha256'] for p in previous['page_records']}
        after = {p['page']: p['text_sha256'] for p in source['page_records']}
        if before != after:
            raise ValueError('S2 per-page hashes differ from P1: ' + source['id'])
        rows.append({'source': source['id'], 'pages': len(after), 'status': 'identical',
                     'page_hashes': {str(n): value for n, value in after.items()}})
    return {'status': 'passed', 'pages': sum(r['pages'] for r in rows), 'sources': rows}


def prepare(run_dir, p1_run=DEFAULT_P1, registry_path=DEFAULT_REGISTRY,
            max_chars=20000, source_root=corpus.SOURCE_ROOT):
    run_dir, p1_run, registry_path = _require_s2_run(run_dir), Path(p1_run).resolve(), Path(registry_path).resolve()
    if run_dir == p1_run:
        raise ValueError('S2 output cannot replace its P1 baseline')
    if run_dir.exists() and (run_dir / 'corpus-manifest.json').exists():
        raise FileExistsError('Prepared S2 corpus is immutable')
    run_dir.mkdir(parents=True, exist_ok=True)
    registry_bytes = registry_path.read_bytes()
    (run_dir / 'source-registry.json').write_bytes(registry_bytes)
    manifest = corpus.prepare(run_dir, scope='p1', registry_path=run_dir / 'source-registry.json',
                              max_chars=max_chars, source_root=source_root)
    if registry_path.read_bytes() != registry_bytes:
        raise ValueError('Frozen P0 registry changed')
    if (run_dir / 'source-registry.json').read_bytes() != registry_bytes:
        raise ValueError('Shared manifest adds sources to the frozen P0 registry; stop')
    p1 = json.loads((p1_run / 'corpus-manifest.json').read_text(encoding='utf-8'))
    receipt = compare_pages(manifest, p1)
    corpus.dump(run_dir / 'INPUT-PAGES.json', receipt)
    # P1-style temporary chunks are not extraction inputs; remove only these
    # freshly generated files before installing the structural cut.
    for old in manifest['chunks']:
        Path(old['path']).unlink()
    chunks = build_chunks(manifest, run_dir, max_chars)
    write_preparation_receipt(run_dir, manifest, chunks, registry_bytes, max_chars)
    return {**manifest, 'chunks': chunks, 'page_integrity': receipt}


def write_preparation_receipt(run_dir, manifest, chunks, registry_bytes, max_chars):
    """Keep the generic preparer's immutable summary distinct from S2 counts."""
    corpus.dump(run_dir / 'PREPARATION.json', {
        'schema': 'wiki-lab-s2-preparation/1',
        'source_manifest': str(run_dir / 'corpus-manifest.json'),
        'source_manifest_sha256': corpus.sha256(run_dir / 'corpus-manifest.json'),
        'frozen_registry_sha256': hashlib.sha256(registry_bytes).hexdigest(),
        'input_pages': sum(len(s['pages']) for s in manifest['sources']),
        'input_sources': len(manifest['sources']),
        'input_chars': sum(s['chars'] for s in manifest['sources']),
        'structural_chunks': len(chunks), 'core_chars': sum(c['chars'] for c in chunks),
        'context_chars': sum(sum(len(f['text']) for f in c['context_fragments']) for c in chunks),
        'core_char_limit': max_chars,
        'initial_limit_basis': 'S1每条约1300–1500字符JSON；S46输出约原文六倍；附注表格更密，待四次试点实测后调整',
        'corpus_manifest_summary_note': 'corpus-manifest.json保留通用准备器的原始摘要；S2结构切块清单以chunks.json及本回执为准',
        'chunks_sha256': corpus.sha256(run_dir / 'chunks.json'),
        'exclusions_sha256': corpus.sha256(run_dir / 'excluded-pages.json'),
    })


def split_chunk(chunk, source, run_dir, reason='caller_requested_split'):
    """Bisect at the nearest saved structure boundary, falling back to pages.

    The caller must record why it requested the split. A failed overlong block
    is never retried unchanged. Result IDs use .a/.b so earlier logs stay bound.
    """
    units = chunk['units']
    sizes = [sum(len(f['text']) for f in u['fragments']) for u in units]
    if len(units) > 1:
        target, running = sum(sizes) / 2, 0
        choices = []
        for i, size in enumerate(sizes[:-1], 1):
            running += size
            choices.append((abs(target - running), i))
        boundary = min(choices)[1]
        groups = [units[:boundary], units[boundary:]]
        kind = 'saved_structure_boundary'
    else:
        fragments = units[0]['fragments']
        if len({f['page'] for f in fragments}) < 2:
            raise BoundaryError('Cannot split a single-page item at a page boundary')
        total, running, options = sum(len(f['text']) for f in fragments), 0, []
        for i, fragment in enumerate(fragments[:-1], 1):
            running += len(fragment['text'])
            if fragment['page'] != fragments[i]['page']:
                options.append((abs(total / 2 - running), i))
        boundary = min(options)[1]
        first, second = fragments[:boundary], fragments[boundary:]
        context, context_reason = _continuation_header(first, second[0], _pages(source))
        groups = [[{**units[0], 'fragments': first}],
                  [{**units[0], 'fragments': second, 'context_fragments': context,
                    'context_pages': [f['page'] for f in context], 'context_reason': context_reason}]]
        kind = 'page_boundary'
    result = []
    for suffix, group in zip(('a', 'b'), groups):
        fragments = [f for u in group for f in u['fragments']]
        context = [f for u in group for f in u.get('context_fragments', [])]
        pages = sorted({f['page'] for f in fragments})
        context = [f for f in context if f['page'] not in pages]
        rendered = _render(source, fragments, context)
        chunk_id = chunk['id'] + '.' + suffix
        path = Path(run_dir) / 'corpus/chunks' / (chunk_id + '.md')
        path.write_bytes(rendered.encode('utf-8'))
        result.append({**chunk, 'id': chunk_id, 'path': str(path.resolve()),
                       'sha256': _hash(rendered), 'chars': sum(len(f['text']) for f in fragments),
                       'wrapped_chars': len(rendered), 'core_pages': pages,
                       'context_pages': sorted({f['page'] for f in context}),
                       'sources': [{'id': source['id'], 'pages': pages}],
                       'fragments': fragments, 'context_fragments': context, 'units': group,
                       'sections': [u['title'] for u in group], 'split_from': chunk['id'],
                       'split_boundary_kind': kind,
                       'context_reasons': sorted({u['context_reason'] for u in group if u.get('context_reason')})})
    receipt = {'split_at': time.time(), 'reason': reason, 'original_id': chunk['id'],
               'original_sha256': chunk['sha256'], 'boundary_kind': kind,
               'children': [{key: child[key] for key in ('id', 'sha256', 'chars', 'core_pages', 'context_pages')}
                            for child in result]}
    receipt_path = Path(run_dir) / 'chunk-splits' / (chunk['id'] + '-' + chunk['sha256'][:12] + '.json')
    if receipt_path.exists():
        old_receipt = json.loads(receipt_path.read_text())
        receipt['previous_attempts'] = old_receipt.get('previous_attempts', []) + [
            {key: value for key, value in old_receipt.items() if key != 'previous_attempts'}]
    corpus.dump(receipt_path, receipt)
    return result


def rechunk(run_dir, max_chars=20000, *, reason, protected_ids=('S46.s01', 'S34.s03'), preview=False):
    """Build a candidate, preserve prior inputs, then install after hash guards.

    Recutting never changes frozen page texts or the corpus manifest. Failed
    candidates and their reports remain inspectable; existing pilot inputs
    must remain byte-identical or installation stops before any active change.
    """
    run_dir = _require_s2_run(run_dir)
    if not reason.strip():
        raise ValueError('Rechunking requires an explicit audit reason')
    corpus.verify_prepared(run_dir / 'corpus-manifest.json')
    manifest = json.loads((run_dir / 'corpus-manifest.json').read_text())
    old = json.loads((run_dir / 'chunks.json').read_text())
    old_hash = corpus.sha256(run_dir / 'chunks.json')
    attempts = run_dir / 'rechunk-attempts'
    attempts.mkdir(exist_ok=True)
    candidate = attempts / ('attempt-%03d' % (len(list(attempts.glob('attempt-*'))) + 1))
    candidate.mkdir()
    report = {'requested_at': time.time(), 'reason': reason, 'old_manifest_sha256': old_hash,
              'old_chunks': len(old), 'max_chars': max_chars, 'protected_ids': list(protected_ids),
              'status': 'building'}
    corpus.dump(candidate / 'RECHUNK-REPORT.json', report)
    try:
        new = build_chunks(manifest, candidate, max_chars)
        old_by_id, new_by_id = ({chunk['id']: chunk for chunk in values} for values in (old, new))
        changed = [key for key in protected_ids if key not in old_by_id or key not in new_by_id
                   or old_by_id[key]['sha256'] != new_by_id[key]['sha256']]
        if changed:
            raise BoundaryError('Protected pilot input hashes would change: ' + ', '.join(changed))
    except Exception as error:
        report.update(status='rejected', error=str(error))
        corpus.dump(candidate / 'RECHUNK-REPORT.json', report)
        raise
    report.update(new_chunks=len(new), core_chars=sum(c['chars'] for c in new),
                  context_chars=sum(len(f['text']) for c in new for f in c.get('context_fragments', [])),
                  largest_core_chars=max((c['chars'] for c in new), default=0),
                  content_mapping=[{'old_id': chunk['id'], 'chars': chunk['chars'],
                      'unchanged_at': [c['id'] for c in new if c['sha256'] == chunk['sha256']]}
                      for chunk in old], protected_hashes_unchanged=True)
    for label, values in (('old', old), ('new', new)):
        first_note7 = next((c for c in values if c.get('note_group') == '七、合并财务报表项目注释'), None)
        if first_note7:
            report[label + '_first_note7'] = {key: first_note7[key] for key in ('id', 'chars', 'sha256')}
    if 'old_first_note7' in report and 'new_first_note7' in report:
        report['first_note7_content_unchanged'] = report['old_first_note7']['sha256'] == report['new_first_note7']['sha256']
    if preview:
        report.update(status='previewed', candidate=str(candidate),
                      active_manifest_unchanged=corpus.sha256(run_dir / 'chunks.json') == old_hash)
        corpus.dump(candidate / 'RECHUNK-REPORT.json', report)
        return new
    history = run_dir / 'chunk-history' / old_hash
    history.mkdir(parents=True, exist_ok=True)
    names = ('chunks.json', 'CHUNKS.md', 'structural-boundaries.json', 'excluded-pages.json', 'PREPARATION.json')
    for name in names:
        if (run_dir / name).exists() and not (history / name).exists():
            shutil.copyfile(run_dir / name, history / name)
    if not (history / 'corpus/chunks').exists():
        shutil.copytree(run_dir / 'corpus/chunks', history / 'corpus/chunks')
    report.update(status='installing', history=str(history))
    corpus.dump(candidate / 'RECHUNK-REPORT.json', report)
    for chunk in new:
        target = run_dir / 'corpus/chunks' / Path(chunk['path']).name
        shutil.copyfile(chunk['path'], target)
        chunk['path'] = str(target)
    for name in ('CHUNKS.md', 'structural-boundaries.json', 'excluded-pages.json'):
        shutil.copyfile(candidate / name, run_dir / name)
    corpus.dump(run_dir / 'chunks.json', new)
    write_preparation_receipt(run_dir, manifest, new, (run_dir / 'source-registry.json').read_bytes(), max_chars)
    report.update(status='installed', new_chunks=len(new), new_manifest_sha256=corpus.sha256(run_dir / 'chunks.json'),
                  protected_hashes_unchanged=True, history=str(history))
    corpus.dump(candidate / 'RECHUNK-REPORT.json', report)
    corpus.dump(run_dir / 'RECHUNK-REPORT.json', report)
    return new


def preview_rechunk(run_dir, max_chars=20000, *, reason, protected_ids=('S46.s01', 'S34.s03')):
    """Persist a candidate and comparison report without replacing active input."""
    return rechunk(run_dir, max_chars, reason=reason, protected_ids=protected_ids, preview=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--p1-run', type=Path, default=DEFAULT_P1)
    parser.add_argument('--registry', type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument('--max-chars', type=int, default=20000)
    parser.add_argument('--verify-prepared', action='store_true',
                        help='Recheck frozen pages and save S2 preparation receipt without recutting')
    parser.add_argument('--rechunk', action='store_true', help='Preserve prior chunks and install a checked revision')
    parser.add_argument('--preview-rechunk', action='store_true', help='Persist candidate only; leave active chunks unchanged')
    parser.add_argument('--reason', help='Required audit reason for --rechunk or --preview-rechunk')
    args = parser.parse_args()
    if args.rechunk or args.preview_rechunk:
        if args.rechunk and args.preview_rechunk:
            parser.error('Choose --rechunk or --preview-rechunk, not both')
        if not args.reason:
            parser.error('--rechunk/--preview-rechunk requires --reason')
        new = rechunk(args.run_dir, args.max_chars, reason=args.reason, preview=args.preview_rechunk)
        output = {'chunks': len(new), 'max_chars': args.max_chars, 'preview': args.preview_rechunk}
        if args.preview_rechunk and new:
            output['candidate'] = str(Path(new[0]['path']).parents[2])
        print(json.dumps(output, ensure_ascii=False))
        raise SystemExit(0)
    if args.verify_prepared:
        run_dir = _require_s2_run(args.run_dir)
        corpus.verify_prepared(run_dir / 'corpus-manifest.json')
        manifest = json.loads((run_dir / 'corpus-manifest.json').read_text())
        previous = json.loads((args.p1_run / 'corpus-manifest.json').read_text())
        receipt = compare_pages(manifest, previous)
        chunks = json.loads((run_dir / 'chunks.json').read_text())
        frozen_registry = args.registry.read_bytes()
        if frozen_registry != (run_dir / 'source-registry.json').read_bytes():
            raise ValueError('Frozen registry differs from S2 copy')
        write_preparation_receipt(run_dir, manifest, chunks, frozen_registry, args.max_chars)
        result = {**manifest, 'chunks': chunks, 'page_integrity': receipt}
    else:
        result = prepare(args.run_dir, args.p1_run, args.registry, args.max_chars)
    print(json.dumps({'pages': result['page_integrity']['pages'],
                      'chunks': len(result['chunks']), 'max_chars': args.max_chars}, ensure_ascii=False))
