#!/usr/bin/env python3
"""Offline S1 note inventory and reproducible, page-bound sample preparation.

The program contains no issuer excerpts. Selection recipes and the resulting
records belong under ignored runs/. This is a format review, not an extractor.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil

from corpus import dump, normalized_sha256, sha256


def fields_for_types(kinds, extra=()):
    """Route final types through the record definition, including manual changes."""
    from records import COMMON, DETAILS
    fields = list(COMMON) if kinds else []
    for kind in kinds:
        definition = DETAILS[kind]
        variants = definition.get('anyOf', [definition])
        for variant in variants:
            fields.extend('detail.' + name for name in variant['properties'])
    return list(dict.fromkeys(fields + list(extra)))


def note_items(text):
    """Inventory one bullet, prose paragraph or table data row as one P1 item.

    Headings, headers and separators are retained as context. Captions are also
    independent inventory items, since they can contain substantive facts. A data
    row is deliberately NOT counted once per numeric cell. This denominator is
    appropriate for auditing an already-written narrative note.
    """
    lines = text.splitlines()
    headings, context, header = [], [], []
    rows = []
    paragraph, first = [], 0

    def flush():
        nonlocal paragraph
        if not paragraph:
            return
        body = '\n'.join(paragraph)
        is_caption = bool(re.search(r'(下表|以下两表|下列三张|下列两表|以下按年度|下列为|以下五行)', body))
        rows.append({'line': first, 'form': 'paragraph', 'text': body,
                     'headings': list(headings), 'context': deepcopy(context[-2:]),
                     'columns': []})
        if is_caption:
            context.append({'line': first, 'text': body})
        paragraph = []

    for i, line in enumerate(lines, 1):
        if line.startswith('#'):
            flush()
            level = len(line) - len(line.lstrip('#'))
            headings = headings[:level - 1] + [line.lstrip('# ').strip()]
            header = []
        elif line.lstrip().startswith('|'):
            flush()
            cells = [x.strip() for x in line.strip().strip('|').split('|')]
            next_line = lines[i].strip() if i < len(lines) else ''
            if re.match(r'^\|[\s:|\-]+\|$', line.strip()):
                continue
            if re.match(r'^\|[\s:|\-]+\|$', next_line):
                header = cells
                continue
            rows.append({'line': i, 'form': 'table_row', 'text': line,
                         'headings': list(headings), 'context': deepcopy(context[-2:]),
                         'columns': [{'name': name, 'text': value}
                                     for name, value in zip(header, cells)]})
        elif not line.strip():
            flush()
        else:
            if re.match(r'^\s*[-*]\s+', line):
                flush()
            if not paragraph:
                first = i
            paragraph.append(line)
    flush()
    return rows


def mapping(item):
    """Explicit field fit, not a claim of semantic extraction/verification."""
    body = item['text']
    context = '\n'.join(x['text'] for x in item['context'])
    title = ' / '.join(item['headings'])
    all_text = title + '\n' + context + '\n' + body
    # Absent-scope and operational notes are coverage evidence, not issuer facts.
    stripped = re.sub(r'^[-\s]*', '', body)
    if re.match(r'^[^。]{0,80}披露日', stripped):
        stripped = stripped.split('。', 1)[-1]
    pure_absence = bool(re.match(r'(?:本块所选页未见|本笔记保留|本次输入范围)', stripped))
    if pure_absence or ('资料范围' in title and item['form'] == 'paragraph'):
        return {'fit': 'non_l1', 'record_types': [], 'fields': [],
                'reason': '运行范围、缺口或省略说明；保留在覆盖/质检记录，不制造披露事实',
                'destination': 'coverage.checked_scope / coverage.absence_basis'}
    changes = []
    kinds = []
    if item['form'] == 'table_row':
        column_names = ' '.join(x['name'] for x in item['columns'])
        if re.search(r'原文名称.*关系|股东.*职务|姓名.*职|Company.*Ticker|原文股东|企业集团构成|关联方名单', title + column_names):
            kinds.append('relationship')
        if re.search(r'\d|NA|NM|空白|—', body) and not re.search(r'Company.*Ticker|姓名.*职', column_names):
            kinds.append('metric')
        if not kinds:
            kinds.append('statement')
        if re.search(r'机构预测|BofA 预测|盈利修订|机构分部净利润估计', all_text):
            kinds = ['statement']
            changes.append('detail.statement_type=forecast、origin：机构模型值不误记为公司实绩')
        if re.search(r'原文名称|股本|股东|注册资本|业务性质|直接|间接|分部|其中|小计|国家或地区|账龄|客户', all_text):
            changes.append('dimensions：限定分部、期间列、主体/匿名客户所在表与层级，不重复相加')
        if re.search(r'NA|NM|n\.a\.|空白|—|口径待|未单列|未另列|未重列|疑点|需核对|待核', all_text):
            changes.append('value_status/value_note、limitations：原表空白或未知单位及列位疑点不补零')
        if re.search(r'预测|估计|测算|假设|敏感性|Previous|Current|New|Old|年限|折旧率|减值测试', all_text):
            changes.append('nature、dimensions：估计/预测/模型/条件测算及预测版本分开')
    else:
        if re.search(r'须|义务|承诺|退还|没收|期限|保证金|触发|偿债支持|偿付支持', body):
            kinds.append('obligation')
        if re.search(r'更名|全资附属|持股|股权链|董事名单|任总裁|聘.*任|关联方|同一主体', body):
            kinds.append('relationship')
        if re.search(r'竞拍成功|签署|完成.*登记|过户|中标|入选|批准|换届|已支付|已全数支付', body):
            kinds.append('event')
        kinds.append('statement')
        if re.search(r'组成|本金|利息|不.*相加|对价.*(?:包含|计入|一部分)|重复', body):
            changes.append('components/additivity/as_of：金额组成、包含关系与基准日分开')
        if 'obligation' in kinds:
            changes.append('detail.deadline/conditions/consequences：义务方、起算事件和条件分开')
        if re.search(r'未见|未知|不详|未另列|不能|不.*推|疑点|续表|续文|跨块|列位', body):
            changes.append('limitations/date_notes/verification：未知日期、源文疑点及范围限制显式保留')
        if re.search(r'CRU|转引|转述|共同出处|第三方', body):
            changes.append('origin：陈述方、第三方及共同出处分开')
    return {'fit': 'revised' if changes else 'direct', 'record_types': sorted(set(kinds)),
            'fields': fields_for_types(kinds), 'reason': '；'.join(changes) or '五类基础字段可直接表达；表格按指标和期间列拆记录',
            'destination': 'L1；并列论断拆成多个记录，不把笔记段落整体当核验事实'}


def review_groups(items):
    """Group a common table layout without deciding whether its rows fit."""
    groups = []
    previous = None
    for item in items:
        signature = (item['note'], item['form'], tuple(item['headings']),
                     tuple(x['name'] for x in item['columns']),
                     tuple(x['line'] for x in item['context']))
        if signature != previous or item['form'] != 'table_row':
            groups.append(dict(group_id=f"G{len(groups) + 1:03d}", note=item['note'],
                               headings=item['headings'], context=item['context'],
                               form=item['form'], columns=[x['name'] for x in item['columns']],
                               item_ids=[], lines=[], row_labels=[]))
        group = groups[-1]
        group['item_ids'].append(item['item_id'])
        group['lines'].append(item['line'])
        group['row_labels'].append(item['columns'][0]['text'] if item['columns'] else item['text'])
        item['review_group'] = group['group_id']
        previous = signature
    for group in groups:
        group['items_sha256'] = normalized_sha256([
            {key: item[key] for key in ('item_id', 'line', 'text', 'headings', 'context', 'columns')}
            for item in items if item['item_id'] in group['item_ids']])
    return groups


def apply_manual_review(payload, manual_path):
    """Manual decisions are explicit, snapshot-bound, and absent by default."""
    manual = json.loads(manual_path.read_text(encoding='utf-8')) if manual_path else {}
    files = {x['path']: x['sha256'] for x in payload['files']}
    if manual and manual.get('note_sha256') != files:
        raise ValueError('Manual review is bound to different source-note bytes')
    if manual and manual.get('review_groups_sha256') != normalized_sha256(payload['review_groups']):
        raise ValueError('Manual review group definition changed; review must be rebound explicitly')
    groups = {x['group_id']: x for x in payload['review_groups']}
    decisions = {}
    for decision in manual.get('decisions', []):
        selected = []
        for selector in decision['groups']:
            if re.fullmatch(r'G\d{3}-G\d{3}', selector):
                first, last = map(lambda x: int(x[1:]), selector.split('-'))
                selected.extend(f'G{i:03d}' for i in range(first, last + 1))
            else:
                selected.append(selector)
        for group_id in selected:
            if group_id not in groups or group_id in decisions:
                raise ValueError(f'Unknown or repeated manual group {group_id}')
            if decision['decision'] not in ('fits', 'non_l1', 'unfit') or not decision.get('reason'):
                raise ValueError('Incomplete manual review decision')
            decisions[group_id] = decision
    for item in payload['items']:
        decision = decisions.get(item['review_group'])
        if decision is None:
            item['manual_review'] = dict(status='unreviewed', decision=None, reason='尚无人工作出逐项字段判断')
            continue
        item['manual_review'] = dict(status='reviewed', decision=decision['decision'],
                                     reviewer=manual['reviewer'], review_id=manual['review_id'],
                                     group=item['review_group'], reason=decision['reason'])
        for key in ('caption_review', 'previous_group'):
            if key in decision:
                item['manual_review'][key] = deepcopy(decision[key])
        if 'record_types' in decision:
            item['mapping']['record_types'] = decision['record_types']
        item['mapping']['fields'] = fields_for_types(item['mapping']['record_types'],
                                                     decision.get('fields', []))
        if 'fit' in decision:
            item['mapping']['fit'] = decision['fit']
        if decision['decision'] in ('unfit', 'non_l1'):
            item['mapping']['fit'] = decision['decision']
            item['mapping']['reason'] = decision['reason']
    for assignment in manual.get('fit_assignments', []):
        for item in payload['items']:
            if item['review_group'] in assignment['groups']:
                if item['manual_review']['decision'] != 'fits':
                    raise ValueError('Fit assignment requires an explicit fits decision')
                item['mapping']['fit'] = assignment['fit']
                item['manual_review']['fit_basis'] = assignment['reason']
    payload['manual_review'] = dict(
        path=str(manual_path) if manual_path else None,
        sha256=sha256(manual_path) if manual_path else None,
        counts=dict(Counter(x['manual_review']['decision'] or 'unreviewed' for x in payload['items'])),
        conclusion='pending' if any(x['manual_review']['status'] == 'unreviewed' or
                                    x['manual_review']['decision'] == 'unfit' for x in payload['items']) else 'format_fits',
        scope='字段可表示性；不核验全部P1数值、完整性或主体同一性')
    payload['counts'] = dict(Counter(x['mapping']['fit'] for x in payload['items']))
    for row in payload['files']:
        row['counts'] = dict(Counter(x['mapping']['fit'] for x in payload['items'] if x['note'] == row['path']))


def expand_metric_layout(specs, layouts, source_defaults=None):
    result = []
    for spec in specs:
        parts = layouts.get(spec['record_id'])
        if not parts:
            result.append(spec)
            continue
        if isinstance(parts, dict) and parts.get('by_period_label'):
            current_period = (parts.get('current_period') or spec.get('period') or
                              source_defaults[spec['short']]['period'])
            parts = []
            for index, number in enumerate(spec['number_specs']):
                label = number['label']
                is_period = bool(re.fullmatch(r'\d{4}H[12]|\d{4}-\d{2}-\d{2}|\d{4}', label))
                period = label if is_period else current_period
                part = dict(number_index=index,
                            record_id=spec['record_id'] if index == 0 else f'{spec["record_id"]}-{index}',
                            period=period)
                if is_period:
                    part['as_of'] = (label[:4] + ('-06-30' if label.endswith('H1') else '-12-31')
                                     if re.fullmatch(r'\d{4}H[12]|\d{4}', label) else label)
                else:
                    part['indicator'] = spec['indicator'] + '：' + label
                    part['dimensions'] = [dict(name='change_basis', value='原表' + label)]
                parts.append(part)
        for part in parts:
            entry = deepcopy(spec)
            entry['number_specs'] = [entry['number_specs'][part['number_index']]]
            entry.update({k: deepcopy(v) for k, v in part.items() if k != 'number_index'})
            result.append(entry)
    return result


def inventory(p1, output, manual_path=None):
    notes = sorted((p1 / 'wiki/sources').glob('*.md'))
    if len(notes) != 8:
        raise ValueError('S1 expects the eight frozen P1 source notes')
    result = []
    files = []
    for path in notes:
        items = note_items(path.read_text(encoding='utf-8'))
        for i, item in enumerate(items, 1):
            item.update({'item_id': f'{path.stem}.I{i:04d}',
                         'note': path.relative_to(p1).as_posix(), 'mapping': mapping(item)})
            result.append(item)
        files.append({'path': path.relative_to(p1).as_posix(), 'sha256': sha256(path),
                      'lines': len(path.read_text(encoding='utf-8').splitlines()),
                      'items': len(items), 'counts': dict(Counter(x['mapping']['fit'] for x in items))})
    payload = {'schema': 'wiki-lab-s1-fit/1',
               'method': 'bullet_or_paragraph_or_table_row; captions counted and inherited; not scalar-cell count',
               'limitation': '字段可表示性审阅；并非全量转录，也不证明P1论断正确或抽取完整',
               'files': files, 'counts': dict(Counter(x['mapping']['fit'] for x in result)),
               'items': result}
    payload['review_groups'] = review_groups(result)
    payload['classification_basis'] = 'mapping为规则预分类；只有manual_review明确reviewed的项目才有人工作出格式判断。规则未匹配到unfit不是验收通过。'
    apply_manual_review(payload, manual_path)
    # This traces the minimum extraction inventory to the already-read notes.
    # It does not claim that every item has been transcribed into a new record.
    rules = {
        'a-share-periodic.json': ('S34', {
            'A01': '主要会计数据|主要财务指标|集团财务摘要', 'A02': '分部',
            'A03': '非经常性损益', 'A04': '收入|成本', 'A05': '现金流',
            'A06': '借款|债券|债务', 'A07': '担保', 'A08': '关联',
            'A09': '承诺|或有', 'A10': '期后', 'A11': '分红|股利|利润分配',
            'A12': '产能|在建工程', 'A13': '管理层|增长原因|主要因', 'A14': '风险'}),
        'hk-transaction-announcement.json': ('S46|S48|S50', {
            'H01': '阶段|签约|竞拍|过户', 'H02': '对价', 'H03': '付款|已付|支付',
            'H04': '偿债支持|保证金|其他义务', 'H05': '期限|起算|条件',
            'H06': '财务摘要', 'H07': '评估|估值|交易理由|对价依据',
            'H08': '主体|股权链|持股关系|更名|产能', 'H09': '上市规则'})}
    checklist_review = []
    for filename, (short_pattern, patterns) in rules.items():
        path = Path(__file__).resolve().parent / 'checklists' / filename
        checklist = json.loads(path.read_text(encoding='utf-8'))
        entries = []
        for entry in checklist['items']:
            matched = [x['item_id'] for x in result
                       if re.match(short_pattern, x['item_id']) and
                       re.search(patterns[entry['id']], ' '.join(x['headings']) + ' ' + x['text'])]
            entries.append(dict(item_id=entry['id'], name=entry['name'],
                                p1_item_ids=matched, field_types=entry['record_types'],
                                conclusion='关键词索引候选，非抽取完整性证明；字段支持结论见对应条目的人工审阅'))
        checklist_review.append(dict(path=str(path), sha256=sha256(path), items=entries))
    payload['checklist_review'] = checklist_review
    dump(output / 'p1-field-fit.json', payload)
    return payload


def select_excerpt(text, selector):
    """Select by exact anchors, preserving every original byte inside a span."""
    if 'whole_page' in selector:
        raise ValueError('whole_page excerpts are not accepted; use start/end/line anchors')
    start = selector['start']
    begin = text.index(start)
    if selector.get('end'):
        finish = text.index(selector['end'], begin) + len(selector['end'])
    elif selector.get('line'):
        finish = text.find('\n', begin)
        if finish < 0:
            finish = len(text)
    else:
        finish = begin + len(start)
    return text[begin:finish]


def archive_sample_rejection(output, clear=False, record_attempt=False):
    """Keep diagnostic bytes and their rejection time; clear only after success."""
    diagnostic = output / 'sample-validation-errors.json'
    if not diagnostic.exists():
        return None
    digest = sha256(diagnostic)
    archived = output / 'sample-rejections' / (digest + '.json')
    if not archived.exists():
        archived.parent.mkdir(parents=True, exist_ok=True)
        # Old diagnostics have no embedded timestamp. Preserve their file time
        # when archiving, rather than dating an old rejection to this rebuild.
        shutil.copy2(diagnostic, archived)
    elif sha256(archived) != digest:
        raise ValueError(f'Archived sample diagnostic changed: {archived}')
    if record_attempt:
        # Identical diagnostics share one content file, but a later failed
        # attempt still has its own time and must remain the latest rejection.
        from fetch import append
        append(output / 'sample-rejections' / 'attempts.jsonl', {
            'rejected_at': datetime.fromtimestamp(diagnostic.stat().st_mtime, timezone.utc).isoformat(),
            'diagnostic': archived.name, 'sha256': digest})
    if clear:
        diagnostic.unlink()
    return archived


def build_samples(p1, output, recipe_path):
    import records
    archive_sample_rejection(output)
    manifest = json.loads((p1 / 'corpus-manifest.json').read_text(encoding='utf-8'))
    sources = {x['id']: x for x in manifest['sources']}
    pages = {}
    input_hashes = {}
    for source in sources.values():
        for row in source['page_records']:
            path = Path(row['corpus_path'])
            if sha256(path) != row['text_sha256']:
                raise ValueError(f'Frozen page changed: {path}')
            pages[(source['source_id'], row['page'])] = path.read_text(encoding='utf-8')
            input_hashes[str(path)] = row['text_sha256']
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    specs = expand_metric_layout(recipe['records'], recipe.get('metric_layouts', {}), recipe.get('defaults', {}))
    batches = {}
    errors = {}
    for raw_spec in specs:
        spec = deepcopy(recipe.get('defaults', {}).get(raw_spec['short'], {}))
        spec.update(deepcopy(raw_spec))
        short = spec.pop('short')
        source = sources[short]
        if 'page' in spec:
            raise ValueError('Page-only recipes are not accepted; use anchored selectors')
        selectors = spec.pop('selectors')
        excerpts = [{'page': s['page'],
                     'text': select_excerpt(pages[(source['source_id'], s['page'])], s)}
                    for s in selectors]
        kind = spec.pop('record_type')
        record_id = spec.pop('record_id')
        subject = spec.pop('subject')
        indicator = spec.pop('indicator')
        period = spec.pop('period')
        inferred_kind = ('half_year' if re.fullmatch(r'\d{4}H[12]', period) else
                         'annual' if re.fullmatch(r'\d{4}E?', period) else
                         'instant' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', period) else
                         'range' if re.fullmatch(r'\d{4}-\d{4}', period) else
                         'event' if period in ('本次交易', '公告日', '报告日') else 'unspecified')
        spec['period'] = dict(kind=inferred_kind, label=period)
        as_of = spec.pop('as_of', None)
        detail = spec.pop('detail', deepcopy(recipe.get('type_details', {}).get(kind, {})))
        defaults = spec.pop('number_defaults', {})
        if 'number_specs' in spec:
            spec['numbers'] = [records.number(**dict(defaults, **number))
                               for number in spec.pop('number_specs')]
        rec = records.make_record(kind, record_id, source['source_id'], subject,
                                  indicator, period, source['date'], as_of,
                                  excerpts, detail, **spec)
        problem = records.validate_record(rec, pages=pages)
        if problem:
            errors[record_id] = problem
        batches.setdefault(short, []).append(rec)
    if errors:
        dump(output / 'sample-validation-errors.json', errors)
        archive_sample_rejection(output, record_attempt=True)
        raise ValueError(f'{len(errors)} samples rejected; see sample-validation-errors.json')
    for short, rows in batches.items():
        dump(output / 'records' / f'{short}.json', rows)
    all_records = [x for rows in batches.values() for x in rows]
    batch_errors = records.validate_output(dict(schema_version=records.VERSION,
                                                records=all_records, coverage=[]), pages)
    if batch_errors:
        raise ValueError(f'Combined sample validation failed: {batch_errors}')
    relations = records.coverage_relations(all_records)
    page_reviews = []
    for review in recipe.get('page_reviews', []):
        source = sources[review['short']]
        by_id = {row['record_id']: row for row in batches[review['short']]}
        for page in source['pages']:
            text = pages[(source['source_id'], page)]
            paragraphs = [x.strip() for x in re.split(r'\n\s*\n', text) if x.strip()]
            for index, paragraph in enumerate(paragraphs, 1):
                if re.fullmatch(r'[–\-—]\s*\d+\s*[–\-—]\s*\f?', paragraph):
                    continue
                matches = [x for x in review['decisions']
                           if x['page'] == page and x['anchor'] in paragraph]
                if len(matches) != 1:
                    raise ValueError(f'Page review requires one explicit decision: {review["short"]} p{page} block{index}: {paragraph[:60]}')
                decision = matches[0]
                if set(decision['record_ids']) - set(by_id):
                    raise ValueError('Page review refers to unknown typed record')
                if not decision.get('number_handling'):
                    raise ValueError('Every paragraph needs explicit numeric handling')
                page_reviews.append(dict(source_id=source['source_id'],
                                         paragraph=index, text=paragraph, **decision))
    dump(output / 'page-review.json', page_reviews)
    dump(output / 'sample-overrides.json', relations)
    summary = {'validated_at': datetime.now(timezone.utc).isoformat(),
               'records': len(all_records), 'by_source': {k: len(v) for k, v in batches.items()},
               'record_types': dict(Counter(x['record_type'] for x in all_records)),
               'recipe_sha256': sha256(recipe_path), 'frozen_page_sha256': input_hashes,
               'errors': 0, 'overrides': relations,
               'page_review_paragraphs': len(page_reviews),
               'historical_rejections': 'sample-rejections/' if any((output / 'sample-rejections').glob('*.json')) else None,
               'limits': ['摘录/数值/枚举通过不证明语义、主体同一性或全报告抽取完整']}
    dump(output / 'sample-validation.json', summary)
    archive_sample_rejection(output, clear=True)
    return summary


def coverage_report(output, payload, sample_summary=None):
    counts = payload['counts']
    review = payload['manual_review']
    manual_counts = review['counts']
    lines = ['# S1：P1 逐条字段覆盖审阅', '',
             '计数单位：一条列表项、一个正文段落（含引表说明）或一张表的数据行算一条；一行中的多个期间/指标单元格不重复计数。标题、表头保留为上下文；引表说明另计独立条目并逐条裁决，也向后续表行传递上下文。', '',
             f"P1 八份来源笔记共 **{len(payload['items'])} 条**：直接能放下 {counts.get('direct', 0)}；补足格式后能放下 {counts.get('revised', 0)}；仍放不下 {counts.get('unfit', 0)}；覆盖/质检/范围说明 {counts.get('non_l1', 0)}（不制造L1事实）。", '',
             f"人工审阅 {len(payload['items']) - manual_counts.get('unreviewed', 0)} 条，未审 {manual_counts.get('unreviewed', 0)} 条；{len(payload['review_groups'])} 个表格布局/正文组的裁决见 [manual-review.json](manual-review.json)，每条均保留人工审阅标志、组编号和具体理由。结论：{review['conclusion']}。", '',
             '先读八份笔记全文，再按连续同表同口径的行及独立段落检查字段可表示性。规则只作预分类；人工裁决会纠正类型（例如会计门槛由实绩 metric 改为 definition statement），最终字段按裁决类型从 records 定义重新路由。未提供裁决文件时所有条目默认未审，不能由规则的零 unfit 推出覆盖通过。裁决绑定八份笔记 SHA-256、每组成员/内容哈希及全组规范化 SHA-256；分组或内容改变会拒绝套用旧裁决。', '',
             '“直接”指基础 metric 已能表达的简单科目/期间/合并范围/数值；“补足格式”指需本轮完整结构中的维度、未知状态、归因、金额组成、关系或条件期限等字段。此分组不等于新增了1781项字段，也不等于全部条目已转录为L1。', '',
             '逐条原文、行号、表头、字段和原因见 [p1-field-fit.json](p1-field-fit.json)。该表审阅字段可表示性；只有 records/ 内样例走逐字摘录/数值校验，不将全表映射冒充全量转录。', '',
             '| 来源笔记 | 条数 | 直接 | 补格式 | 非L1 |', '|---|---:|---:|---:|---:|']
    for row in payload['files']:
        c = row['counts']
        lines.append(f"| {row['path']} | {row['items']} | {c.get('direct', 0)} | {c.get('revised', 0)} | {c.get('non_l1', 0)} |")
    lines += ['', '## 补足的格式点', '',
              '- numbers 数组按每个量列明单位、币种、精度、区间端点及修饰词；预测年份与预测版本、历史数与机构模型分开。',
              '- dimensions 保留分部内外收入、母公司/集团、匿名客户表内身份、币种列和层级；空白、NA、NM及未知单位通过 value_status/value_note 和 limitations 保留。',
              '- 多页 excerpts 支持续表表头与数值跨页；部分源文本列位/口径疑点保留待复核。',
              '- obligations 把义务方、期限、起算事件、条件、退款/没收后果及付款状态分开；components/additivity 保留金额包含关系。',
              '- relationships 用 null 日期加 date_notes 保留未知起止日、未来持股与更名；不倒填完成日。',
              '- origin 保留转引陈述方、原始出处和共同出处；市场评级/目标价用 is_market 过滤。', '',
              '## 实质问题的人工裁决', '',
              '下表G编号为返工前的问题索引；本轮组编号及继承关系见manual-review.json的previous_group。', '',
              '| 返工前条目组 | 实质问题 | 处理 |', '|---|---|---|',
              '| G013/G025/G027–G028等 | 一行同时有本期、上期、同比；期末存量与期间流量不同 | 每个期间单独metric；存量用期末日期键，同比另指标/维度；S34样例已拆开 |',
              '| G010/G012 | CRU预测被报告多处转引 | secondary、陈述方/原始出处/common_origin_id分开，保留共同出处 |',
              '| G070/G092/G097/G399/G407–G408 | 会计门槛、寿命区间、假设敏感性不是披露实绩/经营指引 | statement definition/judgment；区间端点有label，假设写dimensions |',
              '| G146/G149/G194/G490/G493等 | 一元差、原文0.07%、负数列位、未知币种/单位 | 保留原值或statement numbers及疑点，不改错、不补零；NA/NM和未知单位要说明 |',
              '| G358–G372/G435/G437等 | 多币种注册资本、低持股控制、担保到期未解除 | 补实际单位/币种枚举；持股量与控制关系/判断拆开，合同维度及义务状态独立 |',
              '| G524/G527–G531/G525/G540–G545 | 研报实际/预测年度和Previous/Current等版本混合 | 历史转引metric与机构预测statement分开；逐年度/版本键，不套实绩覆盖 |',
              '| S46全文 | 交易、期限、关系、定义、董事名单及折算声明 | 38条typed记录，37段全文审阅索引列明record IDs及数字处置；通用责任声明省略，不建L1记录 |',
              '| G583/G595/G608–G629 | 455对价组成、259.4偿付支持、付款基准日、条件履约保证金、更名、400吨到吨/年 | 拆独立指标，components/additivity/as_of和义务条件；未知日期null；S48到S50同键覆盖保留旧值 |', '',
              '## 清单对照', '',
              'A股14项和港股9项必抽项目提供到P1条目编号的关键词索引候选（p1-field-fit.json 的 checklist_review）；候选命中本身不证明完整性，格式支持依据逐项人工裁决。清单是最低必抽项目；董事名单、定义和详细会计政策等附加内容仍由五类记录承载。S46未载付款安排、偿付支持或标的财务摘要等，应按全文四页已查范围写本文档无；不得补零或倒填S48/S50事实。', '',
              '## 仍放不下及边界', '',
              '没有发现必须增加第六类 L1 记录的内容。纯缺口、查过范围、操作元数据和省略说明属于覆盖/质检，不是发行人事实。P1 本身的列位、单位、匿名主体、跨块缺文及原文疑点仍保留，不因格式可以容纳就视为已核实。', '']
    unfit = [x for x in payload['items'] if x['mapping']['fit'] == 'unfit']
    for row in unfit:
        lines.append(f"- {row['item_id']}：{row['mapping']['reason']}")
    if sample_summary:
        lines += ['## 有界样例验证', '',
                  f"records/ 共 {sample_summary['records']} 条；逐页冻结哈希、摘录、数值和字段校验错误为 0。逐来源数量：{json.dumps(sample_summary['by_source'], ensure_ascii=False)}。", '',
                  f"S46 全文四页的 {sample_summary['page_review_paragraphs']} 段（含定义与通用责任声明）在 [page-review.json](page-review.json) 逐段指向结构化交易、关系、期限、定义等记录，并说明数字、日期、标识及重复主张的处置。没有用空numbers的整段转贴充当全文抽取。S48/S50、S34、S42 按本轮要求选难点，不声称长报告已全量转录。S42评级/目标价另回读P1 market笔记和冻结页；不计入八份sources笔记分母。", '',
                  'S34跨块续表同时引用p168与p169，700/624.6/75.4万股和31.24/21.40元/股均来自p169续表页并进入numbers。样例核验仍为待复核：程序通过不等于语义审定或用户验收。', '']
    added = [x for x in payload['items'] if 'caption_review' in x['manual_review']]
    if added:
        lines += ['## S1返工新增引表条目', '',
                  f'本轮新增{len(added)}条；每段独立裁决，表格说明也计入分母。字段去向、对应表行条目和独立事实拆分保存在manual-review.json / p1-field-fit.json的caption_review。', '',
                  '| 本轮条目 | 笔记行 | 裁决 | 字段去向及事实处理 |', '|---|---|---|---|']
        for item in added:
            decision = item['manual_review']['caption_review']
            lines.append(f"| {item['item_id']} | {item['note']}:{item['line']} | {decision['kind']} | {item['manual_review']['reason'].replace('|', '/')} |")
    (output / 'COVERAGE.md').write_text('\n'.join(lines), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--p1', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recipe', type=Path)
    parser.add_argument('--manual-review', type=Path)
    parser.add_argument('--review-index', action='store_true')
    parser.add_argument('--index-start', type=int, default=1)
    parser.add_argument('--index-end', type=int, default=10000)
    parser.add_argument('--init-manual-review', type=Path)
    parser.add_argument('--bind-manual-groups', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.output.resolve().parent != (root / 'runs').resolve() or not args.output.name.startswith('s1-'):
        raise ValueError('S1 review output must be a wiki-lab runs/s1-* directory')
    if args.output.resolve() == args.p1.resolve() or args.p1.resolve() in args.output.resolve().parents:
        raise ValueError('P1 input is read-only')
    args.output.mkdir(parents=True, exist_ok=True)
    from s1 import snapshot
    for filename in ('p1-field-fit.json', 'COVERAGE.md', 'sample-validation.json',
                     'page-review.json', 'sample-overrides.json'):
        snapshot(args.output, filename)
    for path in sorted((args.output / 'records').glob('*.json')):
        snapshot(args.output, str(path.relative_to(args.output)))
    audit = dict(at=datetime.now(timezone.utc).isoformat(), program_sha256=sha256(Path(__file__)),
                 p1=str(args.p1), recipe=str(args.recipe) if args.recipe else None,
                 manual_review_sha256=sha256(args.manual_review) if args.manual_review else None)
    try:
        payload = inventory(args.p1, args.output, args.manual_review)
        if args.bind_manual_groups:
            manual = json.loads(args.bind_manual_groups.read_text(encoding='utf-8'))
            if manual.get('review_groups_sha256'):
                raise ValueError('Refuse to replace existing manual group binding')
            manual['review_groups_sha256'] = normalized_sha256(payload['review_groups'])
            dump(args.bind_manual_groups, manual)
        if args.init_manual_review:
            if args.init_manual_review.exists():
                raise ValueError('Refuse to overwrite a manual review')
            dump(args.init_manual_review, dict(
                schema='wiki-lab-s1-manual-review/1', reviewer='p1_coverage agent',
                review_id='S1-field-fit-manual-20261022',
                note_sha256={x['path']: x['sha256'] for x in payload['files']},
                review_groups_sha256=normalized_sha256(payload['review_groups']),
                basis='先逐份全文阅读，再逐表/逐段检查字段可表示性；同表同口径的行成组审阅，组内每条保留独立标志；不复核全部原文数值。',
                decisions=[]))
        if args.review_index:
            for group in payload['review_groups'][args.index_start - 1:args.index_end]:
                label = ' ; '.join(group['row_labels'])
                if group['form'] != 'table_row':
                    label = re.sub(r'^.*?披露日[：:][0-9-]+[。；;]?', '', label)
                text = (group['headings'][-1] + ' | ' +
                        ' / '.join(group['columns']) + ' | ' + label[:260])
                print(f"{group['group_id']} {group['note']} L{min(group['lines'])}-{max(group['lines'])} n={len(group['item_ids'])} {text}")
        summary = build_samples(args.p1, args.output, args.recipe) if args.recipe else None
        coverage_report(args.output, payload, summary)
        audit.update(status='passed', inventory_sha256=sha256(args.output / 'p1-field-fit.json'),
                     sample_validation_sha256=sha256(args.output / 'sample-validation.json') if summary else None)
    except Exception as error:
        audit.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        with (args.output / 'review-operations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(audit, ensure_ascii=False) + '\n')
    print(json.dumps({'items': len(payload['items']), 'counts': payload['counts'],
                      'manual_review': payload['manual_review'],
                      'unreviewed_groups': sorted({x['review_group'] for x in payload['items'] if x['manual_review']['status'] == 'unreviewed'}),
                      'samples': summary and summary['records']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
