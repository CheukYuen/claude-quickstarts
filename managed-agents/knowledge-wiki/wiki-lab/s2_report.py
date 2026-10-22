#!/usr/bin/env python3
"""Build S2 delivery artifacts from logged calls; never infer missing reviews."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import subprocess

import corpus
import s2
import s2_compare
import s2_qa

HERE = Path(__file__).resolve().parent
PHASES = ('pilot-stopped', 'budget-stopped', 'full', 'in-progress')
SPLIT_SECTION_START = '<!-- s2-report:failed-splits:start -->'
SPLIT_SECTION_END = '<!-- s2-report:failed-splits:end -->'


def read(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def show(value, digits=3):
    if value is None:
        return '未测得'
    if isinstance(value, float):
        return format(value, '.' + str(digits) + 'f')
    return str(value)


def percent(value):
    return format(value * 100, '.2f') + '%' if value is not None else '未测得'


def complete_outcome(result, prompt_sha=None):
    if result.get('status') == 'completed':
        return prompt_sha is None or result.get('binding', {}).get('prompt_template_sha256') == prompt_sha
    return bool(result.get('status') == 'split' and result.get('children') and
                all(complete_outcome(child, prompt_sha) for child in result['children']))


def extraction_complete(root, chunks, phase):
    """A completion claim needs both outcomes and every current accepted leaf."""
    root = Path(root)
    try:
        result = read(root / 'full-extraction.json', {})
        outcomes = result.get('outcomes', [])
        current = s2.sha(HERE / 'prompts/extract-records.md')
        if not (phase in ('full', 'budget-stopped') and chunks and len(outcomes) == len(chunks) and
                {row.get('chunk_id') for row in outcomes} == {row['id'] for row in chunks} and
                all(complete_outcome(row, current) for row in outcomes)):
            return False
        # Request-mode receipts cannot stand in for a firstpass structural
        # leaf, even if their IDs or source spans happen to coincide.
        inputs = {key: value for key, value in s2.collection_inputs(root).items()
                  if isinstance(key, str) and value[1] == 'coverage'}
        if not inputs:
            return False
        for identifier in inputs:
            accepted = read(root / 'accepted' / (identifier + '.json'), {})
            if (accepted.get('chunk_id') != identifier or
                    accepted.get('binding', {}).get('prompt_template_sha256') != current):
                return False
            s2.check_collected_response(root, accepted, inputs)
        return True
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return False


def current_structural_leaves(root):
    """Keep full current firstpass chunks, including exact core fragments."""
    leaves = []
    for key, value in s2.collection_inputs(Path(root)).items():
        if isinstance(key, str):
            chunk, mode, _request = value
            if mode == 'coverage':
                leaves.append(chunk)
    return leaves


def update_split_chunks_markdown(root):
    """Append or replace only the report-owned section of the initial plan.

    Current topology comes from the same bound coverage inputs as collection;
    orphan or historical receipts and request chunks cannot become active rows.
    """
    root = Path(root)
    active = {chunk['id']: chunk for chunk in current_structural_leaves(root)}
    parents, leaves = [], []

    def visit(chunk):
        path = root / 'splits' / (chunk['id'] + '.json')
        if not path.exists():
            return
        receipt = read(path)
        relative = path.relative_to(root).as_posix()
        parents.append(dict(id=chunk['id'], reason=receipt.get('reason', '未记录'), receipt=relative))
        for child in receipt['children']:
            if child['id'] in active:
                current = active[child['id']]
                leaves.append(dict(id=current['id'], source=current['source'],
                    core_pages=current.get('core_pages', []), context_pages=current.get('context_pages', []),
                    sections=current.get('sections', []), chars=current.get('chars'), sha256=current['sha256'],
                    split_parent=chunk['id'], reason=receipt.get('reason', '未记录'), receipt=relative))
            else:
                visit(child)

    for chunk in read(root / 'chunks.json', []):
        visit(chunk)
    result = dict(parents=parents, leaves=leaves, chunks_document_updated=False)
    path = root / 'CHUNKS.md'
    if not path.exists():
        if parents:
            raise ValueError('Cannot preserve missing initial CHUNKS.md while reporting splits')
        return result
    original = path.read_bytes().decode('utf-8')
    if (original.count(SPLIT_SECTION_START) != original.count(SPLIT_SECTION_END) or
            original.count(SPLIT_SECTION_START) > 1 or
            (SPLIT_SECTION_START in original and original.index(SPLIT_SECTION_END) < original.index(SPLIT_SECTION_START))):
        raise ValueError('Malformed report-owned split section in CHUNKS.md')

    def cell(value):
        return str(value).replace('|', '&#124;').replace('\r\n', '<br>').replace('\n', '<br>')

    lines = [SPLIT_SECTION_START, '## 失败后结构拆分', '',
             '本节由报告代码更新；上方初始计划全文保留，chunks.json不变。'
             '以下父块已被拆分，不再用于有效抽取；当前仅使用其有效结构子叶，不含补抽块。', '']
    for parent in parents:
        lines.append('- 父块`%s`：%s；[拆分回执](%s)。' %
                     (cell(parent['id']), cell(parent['reason']), parent['receipt']))
    if leaves:
        lines += ['', '|子叶块|来源|核心PDF页序|只读上下文页|章节|原文字符|SHA-256|直接父块|原因|拆分回执|',
                  '|---|---|---|---|---|---:|---|---|---|---|']
        for row in leaves:
            values = [row['id'], row['source'], ','.join(map(str, row['core_pages'])),
                      ','.join(map(str, row['context_pages'])), '；'.join(row['sections']),
                      show(row['chars']), row['sha256'], row['split_parent'], row['reason']]
            lines.append('|' + '|'.join(cell(value) for value in values) +
                         '|[JSON](%s)|' % row['receipt'])
    else:
        lines += ['暂无失败后的结构拆分；初始计划仍为当前结构叶块。']
    lines += ['', SPLIT_SECTION_END, '']
    section = '\n'.join(lines)
    if SPLIT_SECTION_START in original:
        start = original.index(SPLIT_SECTION_START)
        end = original.index(SPLIT_SECTION_END) + len(SPLIT_SECTION_END)
        if end < len(original) and original[end] == '\n':
            end += 1
        updated = original[:start] + section + original[end:]
    else:
        updated = original + ('\n' if original.endswith('\n') else '\n\n') + section
    if updated != original:
        path.write_bytes(updated.encode('utf-8'))
    result['chunks_document_updated'] = True
    return result


def completed_source_ids(root):
    """Allow finished documents to be compared if another exhausts the budget.

    collect_records has already revalidated every current accepted response.
    Here only the complete set of active firstpass structural leaves counts.
    """
    leaves = defaultdict(list)
    for key, value in s2.collection_inputs(Path(root)).items():
        if isinstance(key, str):
            chunk, mode, _request = value
            if mode == 'coverage':
                leaves[chunk['source']].append(chunk)
    current = s2.sha(HERE / 'prompts/extract-records.md')
    complete = []
    for source, chunks in leaves.items():
        saved = [read(Path(root) / 'accepted' / (chunk['id'] + '.json'), {}) for chunk in chunks]
        if saved and all(row.get('status') == 'completed' and
                         row.get('binding', {}).get('prompt_template_sha256') == current and
                         row.get('binding', {}).get('chunk_sha256') == chunk['sha256']
                         for row, chunk in zip(saved, chunks)):
            complete.append(source)
    return sorted(complete)


def comparison_region_readiness(root, exclusion_scope=None, completed_sources=None):
    """After collect revalidation, intersect current firstpass core completion.

    A read-only context page never completes a core region. A shared page is
    ready only when ALL active core leaves touching it are current accepted.
    Mixed excluded boundaries stay unavailable until that source is complete.
    """
    root, scope = Path(root), exclusion_scope or {}
    complete_sources = set(completed_source_ids(root) if completed_sources is None else completed_sources)
    current = s2.sha(HERE / 'prompts/extract-records.md')
    touching, source_ids = defaultdict(dict), set()
    for key, value in s2.collection_inputs(root).items():
        if not isinstance(key, str):
            continue  # Request-mode supplements cannot establish firstpass readiness.
        chunk, mode, _request = value
        if mode != 'coverage':
            continue
        source_ids.add(chunk['source'])
        accepted = read(root / 'accepted' / (chunk['id'] + '.json'), {})
        done = (accepted.get('status') == 'completed' and
                accepted.get('binding', {}).get('prompt_template_sha256') == current and
                accepted.get('binding', {}).get('chunk_sha256') == chunk['sha256'])
        core = chunk.get('core_pages', sorted({fragment['page'] for fragment in chunk.get('fragments', [])}))
        for page in core:
            touching[(chunk['source'], page)][chunk['id']] = done
    full = {source: set(pages) for source, pages in scope.get('fully_excluded_pages', {}).items()}
    mixed = {source: set(pages) for source, pages in scope.get('mixed_or_unconfirmed_pages', {}).items()}
    for mapping in (full, mixed):
        for source, pages in mapping.items():
            source_ids.add(source)
            for page in pages:
                touching.setdefault((source, page), {})
    ready, details = {source: [] for source in sorted(source_ids)}, []
    for (source, page), leaves in sorted(touching.items()):
        incomplete = sorted(key for key, done in leaves.items() if not done)
        excluded, boundary = page in full.get(source, set()), page in mixed.get(source, set())
        permitted = bool(leaves and not incomplete and not excluded and
                         (not boundary or source in complete_sources))
        if permitted:
            ready[source].append(page)
        reason = ('fully excluded page; Choice rules only' if excluded else
                  'mixed exclusion boundary in incomplete source' if boundary and source not in complete_sources else
                  'no active firstpass core spans' if not leaves else
                  'some core leaves are incomplete' if incomplete else 'all touching core leaves current accepted')
        details.append(dict(source=source, page=page, ready=permitted, core_chunk_ids=sorted(leaves),
            incomplete_core_chunk_ids=incomplete, source_extraction_complete=source in complete_sources,
            fully_excluded=excluded, mixed_boundary=boundary, reason=reason))
    return dict(ready_pages_by_source=ready, pages=details, prompt_template_sha256=current,
        limitation='仅确定未匹配项可进入回读的区域，不是全量覆盖率分母；相邻页同值不能反推就绪。')


def _attempt_error(attempt):
    for field in ('error', 'launch_error', 'artifact_error', 'event_parse_error'):
        if attempt.get(field):
            return str(attempt[field])
    if not attempt.get('log'):
        return None
    log = Path(attempt['log'])
    acceptance = read(log / 'acceptance.json', {})
    if acceptance.get('error'):
        return str(acceptance['error'])
    messages = []
    if (log / 'events.jsonl').exists():
        for line in (log / 'events.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') in ('error', 'turn.failed'):
                message = event.get('message', event.get('error', {}).get('message'))
                if message:
                    messages.append(str(message))
    return messages[-1] if messages else None


def firstpass_chunk_status(root, attempts, phase):
    """Observe current structural leaves after collect_records proved acceptance.

    This does not grant acceptance or change any records. Calls from older
    templates/input hashes and request-mode calls cannot mark a leaf started.
    """
    root = Path(root)
    current = s2.sha(HERE / 'prompts/extract-records.md')
    rows = []
    for key, value in s2.collection_inputs(root).items():
        if not isinstance(key, str):
            continue
        chunk, mode, _request = value
        if mode != 'coverage':
            continue
        calls = [attempt for attempt in attempts if attempt.get('chunk_id') == chunk['id']
                 and attempt.get('chunk_sha256') == chunk['sha256']
                 and attempt.get('prompt_template_sha256') == current
                 and attempt.get('prompt_version') == s2.PROMPT_VERSION
                 and attempt.get('mode', 'coverage') == 'coverage']
        calls.sort(key=lambda attempt: (attempt.get('call_number') or 0, attempt.get('started_at') or 0))
        saved = read(root / 'accepted' / (chunk['id'] + '.json'), {})
        binding = saved.get('binding', {})
        accepted = bool(saved.get('status') == 'completed' and binding.get('prompt_template_sha256') == current
                        and binding.get('prompt_version') == s2.PROMPT_VERSION
                        and binding.get('chunk_sha256') == chunk['sha256'])
        if accepted:
            status = 'completed'
        elif calls:
            last_status = s2_compare.effective_status(calls[-1])
            status = 'completed_unaccepted' if last_status == 'completed' else last_status
        else:
            status = 'budget_not_started' if phase == 'budget-stopped' else 'not_started'
        last_error, last_error_call = None, None
        for attempt in calls:
            error = _attempt_error(attempt)
            if error:
                last_error, last_error_call = error, attempt.get('call_number')
        rows.append(dict(source=chunk['source'], id=chunk['id'], pages=chunk.get('core_pages', []),
            chars=chunk.get('chars'), accepted_complete=accepted, status=status,
            calls=[dict(call_number=attempt.get('call_number'), stage=attempt.get('stage'),
                        status=s2_compare.effective_status(attempt), runtime_status=attempt.get('status'),
                        log=attempt.get('log')) for attempt in calls],
            last_error=last_error, last_error_call_number=last_error_call))
    rows.sort(key=lambda row: (row['source'], row['id']))
    completed = sum(row['accepted_complete'] for row in rows)
    return dict(schema='wiki-lab-s2-firstpass-status/1', phase=phase, prompt_version=s2.PROMPT_VERSION,
                leaves=len(rows), completed=completed, incomplete=len(rows)-completed,
                counts=dict(Counter(row['status'] for row in rows)), rows=rows,
                limitation='仅当前有效首轮结构叶块，不含补抽或已被拆分的父块；收下状态由此前collect_records完整复验，未知错误保留null。')


def _page_label(pages):
    ranges = []
    for page in sorted(set(pages)):
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ','.join(str(start) if start == end else '%s–%s' % (start, end) for start, end in ranges)


def firstpass_status_markdown(status):
    lines = ['当前有效首轮结构叶块%s个，已完成%s个，未完成%s个；逐块调用编号、状态及最后错误见'
             '[firstpass-chunk-status.json](firstpass-chunk-status.json)。不含补抽或拆分前的父块。' %
             (status['leaves'], status['completed'], status['incomplete'])]
    unfinished = [row for row in status['rows'] if not row['accepted_complete']]
    if unfinished:
        lines += ['', '|未完成来源/块|核心页序|核心字符|状态|调用编号/状态|最后错误|',
                  '|---|---|---:|---|---|---|']
        for row in unfinished:
            calls = '; '.join('#%s %s' % (call['call_number'], call['status']) for call in row['calls']) or '无'
            error = row['last_error']
            error = error.replace('|', '\\|').replace('\n', ' ')[:200] if error else '未报告'
            lines.append('|%s/%s|%s|%s|%s|%s|%s|' % (row['source'], row['id'], _page_label(row['pages']),
                show(row['chars']), row['status'], calls, error))
    return '\n'.join(lines)


def capacity_observations(root, attempts, split_chunks):
    """Observe every bound split call without using failed parents as density."""
    root = Path(root)
    current = s2.sha(HERE / 'prompts/extract-records.md')
    nodes, parent_of = [], {}
    for row in split_chunks.get('parents', []):
        receipt = read(root / row['receipt'])
        parent = receipt['parent']
        nodes.append(dict(kind='failed_parent', chunk=parent, receipt=row['receipt'],
                          reason=receipt.get('reason')))
        for child in receipt['children']:
            parent_of[child['id']] = parent['id']
    for leaf in split_chunks.get('leaves', []):
        nodes.append(dict(kind='current_leaf', chunk=leaf, receipt=leaf['receipt'], reason=leaf.get('reason')))
    rows = []
    for node in nodes:
        chunk = node['chunk']
        calls = [attempt for attempt in attempts if attempt.get('chunk_id') == chunk['id']
            and attempt.get('chunk_sha256') == chunk['sha256']
            and attempt.get('prompt_template_sha256') == current
            and attempt.get('prompt_version') == s2.PROMPT_VERSION
            and attempt.get('mode', 'coverage') == 'coverage' and not attempt.get('is_probe')]
        calls.sort(key=lambda attempt: (attempt.get('call_number') or 0, attempt.get('started_at') or 0))
        saved = read(root / 'accepted' / (chunk['id'] + '.json'), {}) if node['kind'] == 'current_leaf' else {}
        binding = saved.get('binding', {})
        accepted = (saved.get('status') == 'completed' and saved.get('chunk_id') == chunk['id'] and
            binding.get('prompt_template_sha256') == current and binding.get('prompt_version') == s2.PROMPT_VERSION and
            binding.get('chunk_sha256') == chunk['sha256'] and isinstance(saved.get('records'), list))
        for attempt in calls or [None]:
            usage = s2_compare.usage_row(attempt) if attempt is not None else {}
            selected = bool(accepted and (attempt is None or
                (usage['status'] == 'completed' and attempt.get('log') is not None and saved.get('log') is not None and
                 Path(attempt['log']).resolve() == Path(saved['log']).resolve())))
            rows.append(dict(kind=node['kind'], id=chunk['id'], source=chunk.get('source'), chars=chunk.get('chars'),
                split_parent=parent_of.get(chunk['id']), split_reason=node['reason'], split_receipt=node['receipt'],
                call_number=attempt.get('call_number') if attempt is not None else None,
                stage=attempt.get('stage') if attempt is not None else None,
                status=usage.get('status') if attempt is not None else saved.get('status') if accepted else None,
                seconds=usage.get('seconds'), output_tokens=usage.get('output'),
                accepted_records=len(saved['records']) if selected else None,
                log=attempt.get('log') if attempt is not None else saved.get('log') if accepted else None,
                accepted_current_leaf=selected, eligible_for_valid_density=bool(selected and usage.get('output') is not None)))
    rechunk = read(root / 'RECHUNK-REPORT.json', {})
    old_plan, old_preparation = [], {}
    if rechunk.get('history'):
        history = Path(rechunk['history'])
        history = (history if history.is_absolute() else root / history).resolve()
        if history.is_relative_to(root.resolve()):
            old_plan = read(history / 'chunks.json', [])
            old_preparation = read(history / 'PREPARATION.json', {})
    plan = read(root / 'chunks.json', [])
    changed_old = [row for row in rechunk.get('content_mapping', []) if not row.get('unchanged_at')]
    old_hashes = {chunk['sha256'] for chunk in old_plan}
    changed_new = [dict(id=chunk['id'], chars=chunk.get('chars'), sections=chunk.get('sections', []))
                   for chunk in plan if chunk.get('sha256') not in old_hashes] if old_plan else None
    pilot_calls = [attempt for attempt in attempts if re.match(r'^pilot-r\d+-', attempt.get('stage', ''))]
    covered_roots = []
    for chunk in plan:
        saved = read(root / 'accepted' / (chunk['id'] + '.json'), {})
        binding = saved.get('binding', {})
        if (saved.get('status') == 'completed' and binding.get('prompt_template_sha256') == current and
                binding.get('prompt_version') == s2.PROMPT_VERSION and binding.get('chunk_sha256') == chunk.get('sha256') and
                any(attempt.get('chunk_id') == chunk['id'] and attempt.get('log') == saved.get('log')
                    for attempt in pilot_calls)):
            covered_roots.append(chunk['id'])
    ideal_calls = len(pilot_calls) + len(plan) - len(covered_roots) if pilot_calls and plan else None
    return dict(schema='wiki-lab-s2-capacity-observations/1', rows=rows,
        failed_parent_ids=[node['chunk']['id'] for node in nodes if node['kind'] == 'failed_parent'],
        current_leaf_ids=[node['chunk']['id'] for node in nodes if node['kind'] == 'current_leaf'],
        rechunk_adjustment=dict(status=rechunk.get('status'), reason=rechunk.get('reason'),
            old_limit=old_preparation.get('core_char_limit'), new_limit=rechunk.get('max_chars'),
            changed_old_chunks=[dict(id=row['old_id'], chars=row.get('chars')) for row in changed_old],
            changed_new_chunks=changed_new, protected_ids=rechunk.get('protected_ids'),
            protected_hashes_unchanged=rechunk.get('protected_hashes_unchanged'),
            first_note7_content_unchanged=rechunk.get('first_note7_content_unchanged')),
        initial_budget_plan=dict(root_chunks=len(plan), registered_pilot_calls=len(pilot_calls),
            pilot_rounds=sorted({re.match(r'^pilot-(r\d+)-', a['stage']).group(1) for a in pilot_calls}),
            current_pilot_roots=covered_roots, ideal_total_calls_without_extra_failures=ideal_calls,
            allowance_without_extra_failures=s2.CALL_LIMIT-ideal_calls if ideal_calls is not None else None,
            call_limit=s2.CALL_LIMIT),
        scope='当前结构拆分父块及当前子叶；只列同template/version/chunkSHA的已登记coverage调用，重试逐次保留。',
        limitation='失败父块仅表示调用代价，不计有效记录密度；接受数取此前collect完整复验的对应当前accepted回执。'
                   '未知用量或耗时保留null。所列实测不足以验证密集表通用上限，不推安全阈值或ETA。')


def capacity_observations_markdown(observation, char_limit):
    limit = '%s字符' % show(char_limit) if char_limit is not None else '当前设定'
    lines = []
    adjustment = observation['rechunk_adjustment']
    if adjustment['status'] is not None:
        old = '+'.join(show(row['chars']) for row in adjustment['changed_old_chunks']) or '未测得'
        new = '+'.join(show(row['chars']) for row in adjustment['changed_new_chunks']) if adjustment['changed_new_chunks'] else '未测得'
        lines += ['RECHUNK登记上限%s→%s字符；理由：%s。内容映射中的变动旧块%s字符→变动新块%s字符。'
                  '受保护试点%s的哈希不变：%s；首块合并项目附注的哈希不变：%s。'
                  '具体章节和原始回执见[RECHUNK-REPORT.json](RECHUNK-REPORT.json)。这不构成密集表容量验证。' %
                  (show(adjustment['old_limit']), show(adjustment['new_limit']), show(adjustment['reason']), old, new,
                   '、'.join(adjustment['protected_ids'] or []) or '未登记', show(adjustment['protected_hashes_unchanged']),
                   show(adjustment['first_note7_content_unchanged'])), '']
    budget = observation['initial_budget_plan']
    if budget['ideal_total_calls_without_extra_failures'] is not None:
        lines += ['初始根块%s个；已登记%s轮试点共%s次调用，其中%s个当前根块可复用。'
                  '只算剩余根块各一次、不增加失败或重试时，理想合计%s/%s次，余量%s次；'
                  '实际拆分、失败和补抽都需另占同一预算，此算术不是ETA。' %
                  (budget['root_chunks'], len(budget['pilot_rounds']), budget['registered_pilot_calls'],
                   len(budget['current_pilot_roots']), budget['ideal_total_calls_without_extra_failures'],
                   budget['call_limit'], budget['allowance_without_extra_failures']), '']
    lines += ['失败后拆分的容量观测（当前上限%s）：' % limit, '']
    if observation['rows']:
        lines += ['|角色|块ID|直接父块|核心字符|调用|状态|耗时秒|输出token|当前收下记录|',
                  '|---|---|---|---:|---:|---|---:|---:|---:|']
        for row in observation['rows']:
            role = '失败父块' if row['kind'] == 'failed_parent' else '当前子叶'
            values = [role, row['id'], row['split_parent'] or '—', show(row['chars']), show(row['call_number']),
                      show(row['status']), show(row['seconds']), show(row['output_tokens']), show(row['accepted_records'])]
            lines.append('|' + '|'.join(str(value).replace('|', '&#124;') for value in values) + '|')
    else:
        lines += ['当前拓扑暂无失败拆分回执，没有可列示的父块/子叶实测。']
    lines += ['', '已登记调用与拆分依据见[capacity-observations.json](capacity-observations.json)。'
              '失败父块的耗时和输出是额外代价，不计有效记录密度；重试逐次列出，未知不补零。'
              '这些内容的超时与成功子叶不能验证%s是密集表的通用上限，也不能推断安全阈值或ETA。' % limit]
    return '\n'.join(lines)


def choice_responsibility_scope(manifest, exclusions):
    """Only fully excluded pages transfer automatically to Choice by number.

    A page citation cannot place a P1 assertion inside a partially excluded
    boundary page. Keep exact spans for review instead of guessing by value.
    """
    lengths = {(source['id'], page['page']): page.get('chars')
               for source in manifest.get('sources', []) for page in source.get('page_records', [])}
    intervals = defaultdict(list)
    for exclusion in exclusions:
        if exclusion.get('source') == 'S34':
            for fragment in exclusion.get('fragments', []):
                intervals[('S34', fragment['page'])].append((fragment['start'], fragment['end']))
    full, mixed, details = defaultdict(list), defaultdict(list), []
    for (source, page), spans in sorted(intervals.items()):
        length, cursor = lengths.get((source, page)), 0
        valid = length is not None and length > 0
        for start, end in sorted(spans):
            if length is None or not (0 <= start <= end <= length) or start > cursor:
                valid = False
            cursor = max(cursor, end)
        whole = bool(valid and cursor == length)
        (full if whole else mixed)[source].append(page)
        details.append(dict(source=source, page=page, page_chars=length,
            excluded_spans=[dict(start=start, end=end) for start, end in sorted(spans)],
            automatic_choice_responsibility=whole,
            reason='entire frozen page excluded' if whole else 'mixed page or unknown length; citation cannot locate assertion span'))
    return dict(fully_excluded_pages=dict(full), mixed_or_unconfirmed_pages=dict(mixed), pages=details,
        limitation='仅全页排除的财务报表页可按数值自动归Choice；混合边界页保留PDF候选或人工判断，不按同值猜页内位置。')


def pending_scope_diagnostics(comparison, exclusion_scope, regions):
    """Explain conservative pending labels without changing comparison or samples."""
    groups = ('全页排除', '引用触及排除或混合页', '仅保留core未就绪', '其他缺页')
    full = {source: set(pages) for source, pages in exclusion_scope.get('fully_excluded_pages', {}).items()}
    mixed = {source: set(pages) for source, pages in exclusion_scope.get('mixed_or_unconfirmed_pages', {}).items()}
    core = {(row['source'], row['page']): row for row in regions.get('pages', [])
            if row.get('core_chunk_ids')}
    rows, counts = [], Counter({group: 0 for group in groups})
    for item in comparison.get('items', []):
        if item.get('status') != '待抽取':
            continue
        source, pages = item['source'], list(item.get('pages', []))
        fully_excluded = sorted(set(pages) & full.get(source, set()))
        mixed_pages = sorted(set(pages) & mixed.get(source, set()))
        missing = sorted(page for page in set(pages) if (source, page) not in core)
        unready = sorted(page for page in set(pages)
                         if (source, page) in core and not core[(source, page)].get('ready'))
        if pages and set(pages) <= full.get(source, set()):
            group = groups[0]
            reason = '所引页全部排除，且当前没有满足数值规则的Choice候选；不表示这些页因预算尚未运行。'
        elif fully_excluded or mixed_pages:
            group = groups[1]
            reason = '引用触及全页排除或混合/未确认边界，不能按页码定位断言所在字符范围，也不能直接判漏抽。'
        elif pages and not missing and unready:
            group = groups[2]
            reason = '所引页均在保留的core范围内，但仍有当前结构叶块未就绪；此分组不单独推断未就绪原因。'
        else:
            group = groups[3]
            reason = ('条目没有可用引页。' if not pages else
                      '部分引页没有保留core页依据，不能判断是否已完成。' if missing else
                      '引页没有未就绪core依据，现有范围信息不足以解释该保守标签。')
        counts[group] += 1
        rows.append(dict(item_id=item['item_id'], source=source, pages=pages, group=group, reason=reason,
                         fully_excluded_pages=fully_excluded, mixed_or_unconfirmed_pages=mixed_pages,
                         unready_core_pages=unready, pages_without_core=missing))
    return dict(schema='wiki-lab-s2-pending-scope/1', status='待抽取', item_count=len(rows),
                group_priority=list(groups), counts=dict(counts), items=rows,
                limitation='仅解释现有待抽取标签，不修改对照状态、记录、抽样或裁决；排除/边界项不能判漏抽，Choice候选不等于科目覆盖。')


def rejection_summary(root, attempts):
    rows, all_checks, format_errors = [], Counter(), Counter()
    for attempt in attempts:
        if attempt.get('is_probe'):
            continue
        log = Path(attempt['log'])
        acceptance = read(log / 'acceptance.json', {})
        errors, row_format_errors = Counter(), Counter()
        for rejected in acceptance.get('rejected', []):
            # Count each check family at most once per rejected record.
            families = set()
            for error in rejected.get('errors', []):
                field = error.partition(':')[0]
                field = re.sub(r'\[\d+\]', '[]', field)
                families.add(field)
            errors.update(families)
            if any(',' in str(n.get('value', '')) for n in rejected.get('record', {}).get('numbers', [])):
                row_format_errors['numbers.value_contains_comma'] += 1
        all_checks.update(errors)
        format_errors.update(row_format_errors)
        output = acceptance.get('output_records')
        if output is None:
            output = attempt.get('output_records')
        rejected_count = len(acceptance['rejected']) if 'rejected' in acceptance else None
        rows.append(dict(stage=attempt['stage'], chunk_id=attempt.get('chunk_id'),
                         status=acceptance.get('status') or s2_compare.effective_status(attempt), output_records=output,
                         prompt_version=attempt.get('prompt_version'),
                         accepted=len(acceptance['records']) if isinstance(acceptance.get('records'), list) else None,
                         rejected=rejected_count, measured_output_records=output if rejected_count is not None else None,
                         context_only=len(acceptance['discarded']) if isinstance(acceptance.get('discarded'), list) else None,
                         rate=rejected_count/output if output and rejected_count is not None else None, by_check=dict(errors),
                         numeric_format_errors=dict(row_format_errors),
                         suspected_truncation=bool(acceptance.get('suspected_truncation'))))
    measured = [row for row in rows if row['output_records'] is not None and row['rejected'] is not None]
    denominator = sum(row['output_records'] for row in measured)
    numerator = sum(row['rejected'] for row in measured)
    def version_totals(selected):
        validated = [row for row in selected if row['output_records'] is not None and row['rejected'] is not None]
        output = sum(row['output_records'] for row in validated) if validated else None
        rejected = sum(row['rejected'] for row in validated) if validated else None
        checks, numeric = Counter(), Counter()
        for row in validated:
            checks.update(row['by_check'])
            numeric.update(row['numeric_format_errors'])
        return dict(attempts=len(selected), individually_validated_calls=len(validated),
            unvalidated_calls=len(selected)-len(validated), measured_output_records=output,
            rejected_records=rejected, rate=rejected/output if output else None,
            by_check=dict(checks), numeric_format_errors=dict(numeric),
            status_counts=dict(Counter(row['status'] for row in selected)),
            suspected_truncations=sum(row['suspected_truncation'] for row in selected))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['prompt_version'] or 'unversioned'].append(row)
    by_version = {version: version_totals(selected) for version, selected in sorted(grouped.items())}
    current_version = s2.PROMPT_VERSION
    current = dict(prompt_version=current_version, **version_totals(grouped.get(current_version, [])))
    return dict(blocks=rows, by_check=dict(all_checks), measured_output_records=denominator,
                numeric_format_errors=dict(format_errors),
                status_counts=dict(Counter(row['status'] for row in rows)),
                suspected_truncations=sum(row['suspected_truncation'] for row in rows),
                rejected_records=numerator, rate=numerator/denominator if denominator else None,
                by_prompt_version=by_version, current_prompt_version=current_version, current_prompt=current,
                limitation='仅可解析且完成逐条核验的输出有拒收率；整块失败和未完成调用不是零拒收。')


def synthetic_probe_observations(attempts):
    """Only registered schema probes supply non-PDF input-cost observations."""
    rows = []
    for attempt in attempts:
        if not attempt.get('is_probe'):
            continue
        usage = s2_compare.usage_row(attempt)
        rows.append(dict(stage=attempt.get('stage'), call_number=attempt.get('call_number'),
            status=s2_compare.effective_status(attempt), prompt_version=attempt.get('prompt_version'),
            prompt_characters=attempt.get('prompt_chars'), synthetic_source_characters=attempt.get('source_chars'),
            input_tokens=usage['input'], cached_input_tokens=usage['cached'],
            uncached_input_tokens=usage['uncached'], output_tokens=usage['output'],
            reasoning_output_tokens=usage['reasoning']))
    return dict(rows=rows, registered_calls=len(rows),
        measured_input_calls=sum(row['input_tokens'] is not None for row in rows),
        unknown_input_calls=sum(row['input_tokens'] is None for row in rows),
        scope='is_probe登记的S2单页合成文字探测，不含真实PDF；只读已报告调用用量。',
        schema_tokens=None,
        limitation='整次输入含合成文字、格式约束、prompt及运行时等非PDF开销；未单独计量Schema token，'
                   '不以字符数或调用差分归因各项开销，也不能把未缓存输入等同原文token。')


def schema_diagnostics(root, attempts):
    rows = []
    for attempt in attempts:
        if not attempt.get('is_probe') and 'schema' not in attempt.get('stage', ''):
            continue
        log, errors = Path(attempt['log']), []
        events = log / 'events.jsonl'
        if events.exists():
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') in ('error', 'turn.failed'):
                    message = event.get('message', event.get('error', {}).get('message'))
                    if message and message not in errors:
                        errors.append(message)
                if event.get('item', {}).get('type') == 'error':
                    message = event['item'].get('message')
                    if message and message not in errors:
                        errors.append(message)
        rows.append(dict(stage=attempt['stage'], attempt=attempt.get('attempt'),
            status=attempt['status'], command=attempt['command'], cli=attempt.get('cli'),
            configuration=attempt.get('configuration'), errors_exact=errors,
            stderr_path=str(log / 'stderr.log'), final_exists=(log / 'final.json').exists(),
            prompt_chars=attempt.get('prompt_chars')))
    probes = [read(path) for path in sorted(Path(root).glob('pilot-r*-schema.json'))]
    files = {}
    for name, filename in (('stored', 'stored-records.json'), ('model_full', 'model-full.json'),
                           ('model_transport', 'model-transport.json')):
        path = Path(root) / 'schemas' / filename
        data = path.read_bytes() if path.is_file() else None
        files[name] = dict(path=str(path), exists=data is not None,
            characters=len(data.decode('utf-8')) if data is not None else None,
            sha256=s2.sha(path) if data is not None else None)
    return dict(attempts=rows, rounds=probes,
        schema_files=files, schema_characters={name: value['characters'] for name, value in files.items()},
        field_guide_characters=len(s2.records.field_guide()),
        character_count_scope='Schema按run内实际文件UTF-8解码后计字符，含文件空白；field_guide按当前records.field_guide()计字符。',
        synthetic_probe_usage=synthetic_probe_observations(attempts),
        accepted=any(p.get('ok') for p in probes),
        transport_used=any(p.get('transport') for p in probes),
        validation='传输Schema采用接口兼容子集；完整字段和证据约束仍由records.py执行。'
                   '只有uniqueItems取得单项拒绝回执，不能断言其余移除关键字均不受支持。')


def cli_observations(attempts):
    """Read-only version probes are shell operations, never model calls."""
    observations = []
    executables = list(dict.fromkeys(a['command'][0] for a in attempts if a.get('command')))
    for executable in executables:
        try:
            process = subprocess.run([executable, '--version'], text=True, capture_output=True, timeout=10)
            observations.append(dict(executable=executable, version=process.stdout.strip(),
                                     exit_code=process.returncode, stderr=process.stderr.strip()))
        except (OSError, subprocess.TimeoutExpired) as error:
            observations.append(dict(executable=executable, error=str(error)))
    return observations


def output_projection(pdf_records, attempts, root):
    # Per-call output includes reasoning (already a subset) and JSON envelope.
    current_sha = s2.sha(HERE / 'prompts/extract-records.md')
    eligible = [a for a in attempts if not a.get('is_probe') and a.get('chunk_id') and
            a.get('prompt_template_sha256') == current_sha and
            s2_compare.effective_status(a) == 'completed' and a.get('usage', {}).get('output_tokens') is not None]
    # The cost ledger retains every call. Output density instead represents one
    # selected response per input block, never duplicate successful pilots.
    by_chunk = defaultdict(list)
    for attempt in eligible:
        by_chunk[attempt['chunk_id']].append(attempt)
    good, omitted = [], []
    for chunk, values in by_chunk.items():
        saved = read(Path(root) / 'accepted' / (chunk + '.json'), {})
        chosen = next((a for a in values if a.get('log') == saved.get('log')), None)
        chosen = chosen or max(values, key=lambda a: (a.get('call_number', 0), a.get('started_at', 0)))
        good.append(chosen)
        omitted.extend(a.get('call_number', a.get('stage')) for a in values if a is not chosen)
    output = sum(a['usage']['output_tokens'] for a in good) if good else None
    chars = sum(a.get('source_chars', 0) for a in good)
    accepted = sum(a.get('accepted_records', len(read(Path(a['log']) / 'acceptance.json', {}).get('records', []))) for a in good)
    p0 = read(HERE / 'runs/p0-20261021/corpus-manifest.json', {}).get('summary', {})
    ratio = output / chars if output is not None and chars else None
    return dict(measured_success_calls=len(good), measured_output_tokens=output,
        selected_calls=[dict(call_number=a.get('call_number'), stage=a.get('stage'), chunk_id=a['chunk_id'],
                            prompt_version=a.get('prompt_version')) for a in good],
        duplicate_success_calls_excluded=omitted,
        measured_source_chars=chars, measured_accepted_records=accepted,
        output_tokens_per_accepted_record=output/accepted if output is not None and accepted else None,
        accepted_record_json_chars=sum(len(json.dumps(r, ensure_ascii=False)) for r in pdf_records),
        p0_sources=p0.get('sources'), p0_source_chars=p0.get('chars'),
        p0_output_tokens_by_char=ratio*p0['chars'] if ratio is not None and p0.get('chars') else None,
        p0_records_by_char=accepted/chars*p0['chars'] if chars and p0.get('chars') and accepted else None,
        extrapolation_status='未测得' if ratio is None else '已测成功块的纯字符线性情景，非预算或交付时间承诺',
        limitations=['样本输出密度、IPO/附注/研报结构及排除主表比例不同；53份全量不可视为同分布。',
                     '密度仅用当前prompt每块最终选定的一次成功输出；全部轮次、失败及重复调用仍计实际成本。',
                     'P0外推仅输出token，不把推理token重复相加，也不推断账单。'])


def measured_call_densities(attempts):
    """Per-attempt pilot density is measurable even when full extraction is not."""
    rows = []
    for attempt in attempts:
        if attempt.get('is_probe') or not attempt.get('chunk_id'):
            continue
        row = s2_compare.usage_row(attempt)
        chars = attempt.get('source_chars')
        row.update(chunk_id=attempt['chunk_id'], call_number=attempt.get('call_number'),
                   prompt_version=attempt.get('prompt_version'), source_chars=chars,
                   ratios={field + '_per_10000_source_chars': row[field]*10000/chars
                           if row[field] is not None and chars else None
                           for field in ('input', 'cached', 'uncached', 'output', 'reasoning')},
                   cache_ratio_reported=row['cached']/row['input'] if row['input'] and row['cached'] is not None else None,
                   output_tokens_per_accepted_record=row['output']/row['records'] if row['output'] is not None and row['records'] else None)
        rows.append(row)
    return dict(rows=rows, scope='每次已送入原文的调用；失败未报告为null，不把多轮输入或输出密度当作唯一内容量。')


def s46_same_input_cost(p1_attempts, s2_attempts, source_chars=7771):
    """S46 is whole-document input in both pipelines; retain all failed calls."""
    if isinstance(p1_attempts, dict):
        p1_attempts = p1_attempts.get('attempts', [])
    p1 = [a for a in p1_attempts if a.get('stage') == 'extract-S46.c01']
    all_s46 = [a for a in s2_attempts if (a.get('chunk_id', '').split('.')[0] == 'S46') and not a.get('is_probe')]
    s46 = [a for a in all_s46 if a.get('source_chars') == source_chars]
    groups = defaultdict(list)
    for attempt in s46:
        round_match = re.search(r'pilot-(r\d+)-', attempt.get('stage', ''))
        group = round_match.group(1) if round_match else attempt.get('prompt_version', 'unversioned')
        groups[group].append(attempt)
    rows = measured_call_densities(s46)['rows']
    # A zero-command run can still hit a shared prompt/schema/runtime prefix
    # cache. The trace does not identify which prefix was cached.
    high_cache = [row for row in rows if row['cache_ratio_reported'] is not None and row['cache_ratio_reported'] >= .9]
    zero_command_high_cache = [row for row in high_cache if row['commands'] == 0]
    return dict(source_chars=source_chars, same_document_pages=[1, 2, 3, 4],
        excluded_nonidentical_input_calls=[a.get('call_number') for a in all_s46 if a.get('source_chars') != source_chars],
        p1=dict(all=s2_compare.usage_totals(p1),
                success=s2_compare.usage_totals([a for a in p1 if a.get('status') == 'completed']),
                failed=s2_compare.usage_totals([a for a in p1 if a.get('status') != 'completed'])),
        s2_by_round={key: dict(all=s2_compare.usage_totals(values),
                    success=s2_compare.usage_totals([a for a in values if s2_compare.effective_status(a) == 'completed']),
                    failed=s2_compare.usage_totals([a for a in values if s2_compare.effective_status(a) != 'completed']))
                    for key, values in groups.items()},
        s2_rows=rows,
        hypothesis_status='未建立',
        overall_hypothesis_evaluated=False,
        observed_high_cache_calls=[row['call_number'] for row in high_cache],
        interpretation=('已测S46存在90%以上缓存命中；零命令没有带来低缓存的必然结果。'
                        '这是一份资料的观测，不能据此否定S2整体聚合缓存占比下降；总体幅度须比较实际聚合用量。'
                        if zero_command_high_cache else 'S46单份资料的观测不构成整体假设判定；总体幅度须比较实际聚合用量。'),
        limitations=['缓存可能复用跨调用的共同前缀、Schema或CLI基础上下文；事件用量未细分缓存来源，不能断言命中原因。',
                     '输入原文相同，输出契约不同（P1笔记、S2 JSON记录），不是等输出任务。',
                     'P1首次失败未报告用量；保留unknown-call计数，不按零计；S2跨轮成本不合并成一次抽取。'])


def overall_cost_observations(usage, source_chars, complete, attempts=None):
    """Keep aggregate cache evidence separate from token/character comparison."""
    baseline, measured = usage['p1']['extraction_only'], usage['s2']['all']
    old, new = baseline['cache_ratio_reported'], measured['cache_ratio_reported']
    difference = (new-old)*100 if old is not None and new is not None else None
    direction = ('未测得' if difference is None else '下降' if difference < 0 else '上升' if difference > 0 else '相同')
    scope = '全量抽取已完成，仅统计已报告用量' if complete else '部分结果，不能判全量'
    cache = dict(p1_scope='P1全部抽取尝试的已报告聚合用量',
        s2_scope='S2全部尝试的已报告聚合用量，含探测、失败、重试和旧版prompt',
        p1_input_tokens=baseline['input'], p1_cached_input_tokens=baseline['cached'],
        s2_input_tokens=measured['input'], s2_cached_input_tokens=measured['cached'],
        p1_cache_ratio=old, s2_cache_ratio=new, difference_percentage_points=difference,
        observed_direction=direction, completeness=scope,
        p1_input_unknown_calls=baseline['input_unknown_calls'], s2_input_unknown_calls=measured['input_unknown_calls'],
        interpretation='仅报告实际占比差和变化方向；不设置“大幅”或“显著”的判断阈值。',
        limitations=['未报告用量的调用未按零填补，真实全成本占比仍可能不同。',
                     'P1抽取过财务报表页，S2排除该范围；输出契约、切块与重试安排也不同。',
                     'S46单份资料的高缓存观测不能否定总体聚合占比下降。'])
    uncached = dict(status='未建立', uncached_input_tokens=measured['uncached'],
        original_source_characters=source_chars, original_source_tokens=None,
        input_unit='token', source_unit='字符', source_scope='当前首轮切块计划的核心原文字符，非重复调用输入',
        completeness=scope,
        interpretation='token数与字符数单位不同；未单独计量原文token，不能断言未缓存输入约等于原文。',
        limitations=['未缓存输入还包括Schema、prompt和运行时基础指令。',
                     '全部尝试包含探测、失败、重试与重复原文输入，字符分母没有重复相加。',
                     '缓存命中来源没有分解，不能从总用量反推出单独原文token。'])
    return dict(cache_share=cache, uncached_vs_source=uncached,
        synthetic_probe_usage=synthetic_probe_observations(attempts or []),
        joint_conclusion=dict(status='未建立',
            reason='缓存占比变化按聚合证据单列；原文token未单独计量，联合假设尚不能确认。',
            completeness=scope))


def overall_cost_markdown(observation):
    cache, uncached = observation['cache_share'], observation['uncached_vs_source']
    text = ('\n整体缓存观测：%s。S2已报告聚合占比%s，P1抽取已报告聚合占比%s，'
        'S2减P1为%s个百分点（%s）。%s S2范围含全部尝试，P1范围为抽取部分；未知调用未补零，'
        'P1主表范围和S2不同。\n\n'
        '未缓存输入与原文：S2已报告未缓存输入%s token；当前切块计划核心原文%s字符。'
        '%s 未缓存输入包含Schema、prompt、运行时基础指令及重复调用；这两项没有相同的计量范围。\n\n'
        '第15节联合结论：%s。%s 证据和限制见[overall-cost-observations.json](overall-cost-observations.json)。\n' %
        (cache['completeness'], percent(cache['s2_cache_ratio']), percent(cache['p1_cache_ratio']),
         show(cache['difference_percentage_points']), cache['observed_direction'], cache['interpretation'],
         show(uncached['uncached_input_tokens']), show(uncached['original_source_characters']), uncached['interpretation'],
         observation['joint_conclusion']['status'], observation['joint_conclusion']['reason']))
    probes = observation.get('synthetic_probe_usage', {})
    if probes.get('rows'):
        text += ('\n合成Schema探测观测：以下is_probe调用仅输入单页合成文字，没有真实PDF；'
                 'prompt字符和token分别保留各自单位。\n\n'
                 '|登记探测|prompt字符|合成文字字符|输入token|缓存token|未缓存token|\n'
                 '|---|---:|---:|---:|---:|---:|\n')
        for row in probes['rows']:
            text += '|%s|%s|%s|%s|%s|%s|\n' % (row['stage'], show(row['prompt_characters']),
                show(row['synthetic_source_characters']), show(row['input_tokens']),
                show(row['cached_input_tokens']), show(row['uncached_input_tokens']))
        text += '\n' + probes['limitation'] + '\n'
    else:
        text += '\n合成Schema探测观测：没有可引用的已登记探测；不推断其输入用量或Schema token。\n'
    return text


def pilot_cost_markdown(densities, s46):
    lines = ['', '## 已测块密度（独立于全量对照）', '',
        '全量未完成时下面每次调用仍有实测分母；完整S2每万字比率保持null。重复轮次分行，未知失败用量为未知。', '',
        '|调用|版本|原文字符|输入/万字|缓存/万字|未缓存/万字|输出/万字|推理/万字|输出/收下记录|',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for row in densities['rows']:
        ratios = row['ratios']
        lines.append('|' + '|'.join([str(row['stage']), str(row['prompt_version']), show(row['source_chars'])] +
            [show(ratios[field + '_per_10000_source_chars']) for field in ('input', 'cached', 'uncached', 'output', 'reasoning')] +
            [show(row['output_tokens_per_accepted_record'])]) + '|')
    lines += ['', '## S46整份同输入成本', '',
        'P1与S2都输入p1–4、7771字符。下面包含失败；P1首次失败未知用量没有补零。输出任务由笔记变为记录JSON。', '',
        '|组别|调用|输入|缓存|未缓存|输出|推理|命令|秒|输入未知调用|',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    groups = [('P1-' + name, value) for name, value in s46['p1'].items()]
    groups += [('S2-' + round_name + '-' + name, value) for round_name, values in s46['s2_by_round'].items()
               for name, value in values.items()]
    for name, value in groups:
        lines.append('|' + name + '|' + '|'.join(show(value[field]) for field in
            ('calls', 'input', 'cached', 'uncached', 'output', 'reasoning', 'commands', 'seconds', 'input_unknown_calls')) + '|')
    lines += ['', 'S46单份资料观测（不是总体假设判定）：%s' % s46['interpretation'], '',
        '零命令只说明没有可见的工具循环，不能推出缓存命中为零或很低。'
        '跨调用共同前缀、Schema和CLI基础上下文都可能被缓存；事件未说明命中来源，不能据此确定原因。', '']
    return '\n'.join(lines)


def a01_ids(coverage, root=None, pdf_records=None):
    found = {key for rows in coverage.values() for row in rows if row['item_id'] == 'A01'
             and row['status'] == '已抽' for key in row['record_ids']}
    # One rejected record downgrades the checklist item; the other accepted
    # A01 records still require Choice comparison. Preserve that provenance
    # from block coverage instead of silently skipping their value checks.
    if root is not None:
        for path in (Path(root) / 'accepted').glob('*.json'):
            result = read(path, {})
            if result.get('binding', {}).get('prompt_template_sha256') != s2.sha(HERE / 'prompts/extract-records.md'):
                continue
            found.update(key for row in result.get('coverage', []) if row['item_id'] == 'A01'
                         for key in row['record_ids'])
    if pdf_records is not None:
        found &= {r['record_id'] for r in pdf_records}
    return sorted(found)


def reverse_error_breakdown(queues):
    """Count explicit reverse-review labels without inferring content errors."""
    reviewed = [row['review'] for row in queues.get('queues', {}).get('reverse', [])
                if row.get('review', {}).get('status') == 'reviewed']
    errors = [review for review in reviewed if review.get('decision') == '错误']
    tag = '反向候选误判'
    only = sum(set(review.get('error_types', [])) == {tag} for review in errors)
    mixed = sum(tag in review.get('error_types', []) and set(review['error_types']) != {tag} for review in errors)
    return dict(metric='新增候选无效率', reviewed=len(reviewed), invalid_candidates=len(errors),
        invalid_candidate_rate=len(errors) / len(reviewed) if reviewed else None,
        candidate_matching_only=only, candidate_matching_with_other_types=mixed,
        other_error_records=len(errors) - only - mixed,
        limitation='仅按显式error_types分组；反向候选误判不等于记录内容错误，不计入80条语义样本错误率。')


def build_report(root, phase, reason='', reviews_path=None):
    if phase not in PHASES:
        raise ValueError('Unknown S2 report phase')
    root = Path(root).resolve()
    # Fail before generating acceptance claims if any protected input changed.
    intact = s2.integrity(root)
    pdf_records, coverage = s2.collect_records(root)
    attempts = s2.summarize_usage(root)
    manifest = read(root / 'corpus-manifest.json', {})
    chunks = read(root / 'chunks.json', [])
    complete = extraction_complete(root, chunks, phase)
    completed_sources = completed_source_ids(root)
    firstpass = firstpass_chunk_status(root, attempts, phase)
    split_chunks = update_split_chunks_markdown(root)
    capacity = capacity_observations(root, attempts, split_chunks)
    choice_records = read(root / 'choice/records.json', [])
    choice_summary = read(root / 'choice/summary.json', {})
    aliases = {source['source_id']: source['id'] for source in manifest.get('sources', [])}
    exclusion_scope = choice_responsibility_scope(manifest, read(root / 'excluded-pages.json', []))
    excluded = exclusion_scope['fully_excluded_pages']
    regions = comparison_region_readiness(root, exclusion_scope, completed_sources)
    inventory = read(HERE / 'runs/s1-20261021/p1-field-fit.json', {'items': []})
    compared = s2_compare.compare_inventory(inventory, pdf_records, aliases, excluded, choice_records,
                                           extraction_complete=complete, completed_sources=completed_sources,
                                           ready_pages_by_source=regions['ready_pages_by_source'])
    pending_scope = pending_scope_diagnostics(compared, exclusion_scope, regions)
    queues = s2_compare.make_review_queues(compared, pdf_records, current_structural_leaves(root), aliases,
                                         chunk_plan_scope='current structural leaves')
    corpus.dump(root / 'review-queues.json', queues)
    if reviews_path:
        queues = s2_compare.apply_reviews(queues, read(reviews_path))
        corpus.dump(root / 'reviewed-queues.json', queues)
    reviews = s2_compare.review_summary(queues)
    corpus.dump(root / 'semantic-review.json', dict(review_state='complete' if reviews['semantic']['reviewed'] == reviews['semantic']['total']
                and reviews['semantic']['total'] else 'pending', summary=reviews['semantic'],
                rows=queues['queues']['semantic']))
    corpus.dump(root / 'coverage-review.json', dict(summary=reviews['unmatched'], rows=queues['queues']['unmatched']))
    corpus.dump(root / 'reverse-review.json', dict(summary=reviews['reverse'], rows=queues['queues']['reverse']))
    planned_chars = sum(c.get('chars', 0) for c in chunks)
    p1_usage = read(HERE / 'runs/p1-20261021/usage.json', {'attempts': []})
    usage = s2_compare.usage_comparison(p1_usage,
        attempts, planned_chars if complete else None, len(pdf_records))
    usage['s2']['planned_source_chars'] = planned_chars
    usage['limitations'].append('全量未完成时，每万字的S2全量对照比率为null；已测成功块的密度另列output-projection.json。')
    usage['s2']['phase'] = phase
    usage['s2']['extraction_complete'] = complete
    projection = output_projection(pdf_records, attempts, root)
    densities = measured_call_densities(attempts)
    same_input = s46_same_input_cost(p1_usage, attempts)
    overall_cost = overall_cost_observations(usage, planned_chars, complete, attempts)
    rejection = rejection_summary(root, attempts)
    schema = schema_diagnostics(root, attempts)
    qa = s2_qa.write_qa(root, pdf_records + choice_records, a01_ids(coverage, root, pdf_records) if pdf_records else None)
    environment = cli_observations(attempts)
    prompt_rows = [{key: attempt.get(key) for key in ('stage', 'attempt', 'prompt_version', 'prompt_chars', 'prompt_sha256',
                                                      'prompt_template_sha256', 'field_guide_sha256')}
                   for attempt in attempts]
    gates = [dict(path=str(path.relative_to(root)), **read(path, {})) for path in sorted(root.glob('pilot-r*-gate.json'))]
    corpus.dump(root / 'cli-observations.json', environment)
    for name, value in [('comparison.json', compared), ('usage-comparison.json', usage),
                        ('output-projection.json', projection), ('rejection-summary.json', rejection),
                        ('schema-diagnostics.json', schema), ('measured-call-density.json', densities),
                        ('s46-same-input-cost.json', same_input), ('prompt-lengths.json', prompt_rows),
                        ('overall-cost-observations.json', overall_cost),
                        ('firstpass-chunk-status.json', firstpass),
                        ('capacity-observations.json', capacity),
                        ('pending-scope-diagnostics.json', pending_scope),
                        ('choice-responsibility-scope.json', exclusion_scope)]:
        corpus.dump(root / name, value)
    corpus.dump(root / 'comparison-regions.json', dict(**regions, item_scope=compared['region_summary']))
    usage_text = s2_compare.usage_markdown(usage)
    usage_text += pilot_cost_markdown(densities, same_input)
    usage_text += overall_cost_markdown(overall_cost)
    usage_text += ('\n本报告阶段：%s；全量抽取完成：%s。未抽取部分不视为漏抽；未作人工语义裁决不视为零错误。\n' % (phase, complete))
    (root / 'USAGE.md').write_text(usage_text)
    preparation = read(root / 'PREPARATION.json', {})
    summary = dict(phase=phase, reason=reason, extraction_complete=complete,
        completed_sources=completed_sources,
        comparison_regions=regions, comparison_region_summary=compared['region_summary'],
        firstpass_chunk_status=firstpass,
        split_chunks=split_chunks,
        capacity_observations=capacity,
        accepted_pdf_records=len(pdf_records), choice_records=len(choice_records),
        call_count=len(attempts), reserved_calls=read(root / 'usage.json', {}).get('calls_reserved'),
        call_limit=s2.CALL_LIMIT, comparison_counts=compared['counts'], reviews=reviews,
        pending_scope_diagnostics=pending_scope,
        reverse_error_breakdown=reverse_error_breakdown(queues),
        rejection=rejection, schema_accepted=schema['accepted'], transport_schema=schema['transport_used'],
        qa_summary=qa['summary'], integrity_ok=intact['ok'], projection=projection,
        configurations=[json.loads(value) for value in sorted({json.dumps(a.get('configuration', {}), sort_keys=True)
                        for a in attempts})],
        measured_call_density=densities, s46_same_input_cost=same_input,
        overall_cost_observations=overall_cost,
        choice_responsibility_scope=exclusion_scope,
        prompt_rows=prompt_rows, pilot_gates=gates,
        pilot_matches=[dict(path=str(path.relative_to(root)), **{key: value for key, value in read(path, {}).items()
                         if key in ('reference_count', 'extracted_count', 'matched_reference_count', 'reference_match_ratio', 'passed')})
                       for path in sorted(root.glob('pilot-r*-S46-match.json'))])
    corpus.dump(root / 'delivery-summary.json', summary)
    (root / 'REPORT.md').write_text(report_markdown(summary, usage, schema, coverage, preparation,
        choice_summary, qa, environment, root))
    return summary


def report_markdown(summary, usage, schema, coverage, prep, choice, qa, environment, root):
    totals = usage['s2']['all']
    phase = summary['phase']
    status = ('全量抽取已结束，人工验收状态见下文。' if summary['extraction_complete'] else
              '全量抽取尚未完成；现有记录与成本仅代表已运行部分，不能据此认定全量S2的覆盖率或总体错误率。')
    same = summary['s46_same_input_cost']
    s46_observations = ['S46 `%s`：输入%s、缓存%s、未缓存%s、输出%s、推理%s tokens；缓存占比%s，命令%s，%s秒。' %
        (row['stage'], show(row['input']), show(row['cached']), show(row['uncached']), show(row['output']),
         show(row['reasoning']), percent(row['cache_ratio_reported']), show(row['commands']), show(row['seconds']))
        for row in same['s2_rows']]
    same_narrative = '\n\n'.join(s46_observations + [
        'S46单份资料观测（不是总体结论）：%s 零命令只说明没有可见的工具循环，不能推出缓存必然大幅下降；'
        '缓存的具体来源尚未被事件细分。每次已测块的万字比率和S46同输入P1分列见'
        '[USAGE.md](USAGE.md)、[s46-same-input-cost.json](s46-same-input-cost.json)。' %
        same['interpretation']])
    lines = ['# wiki-lab v2 S2 交付记录', '', status, '',
             '阶段：`%s`。%s 停在S2，不进入S3；本轮不commit、不push。' % (phase, summary['reason']), '',
             '程序检查、语义抽查、研究接受及用户接受分别记录；本报告不授予后两项。', '',
             '## 输入和切块', '',
             '重新准备%d份资料、%d页、%d字符；逐页与P1哈希核对见[INPUT-PAGES.json](INPUT-PAGES.json)。'
             '初始计划%d块，上限%s字符，核心原文%s字符。' % (prep.get('input_sources', 0), prep.get('input_pages', 0),
             prep.get('input_chars', 0), prep.get('structural_chunks', 0), show(prep.get('core_char_limit')),
             show(prep.get('core_chars'))), '',
             '章节、页序、精确字符范围、块哈希及排除原因见[CHUNKS.md](CHUNKS.md)、[chunks.json](chunks.json)。'
             'CHUNKS.md的“失败后结构拆分”节另列当前实际子叶及回执；已拆父块不再用于有效抽取。'
             'S34“二、财务报表”未送模型；合并三表原表科目由Choice承担，母公司主表与所有者权益变动表不入本轮记录。'
             '附注中的母公司项目未被这项主表排除规则自动删除。S42仍只用p1–6，p7–9不补取。', '',
             'P1对照中的自动Choice责任只限全页排除的页；混合边界页%s保留PDF候选或人工判断，'
             '不以同值猜测条目处在页内哪个片段。精确区间见'
             '[choice-responsibility-scope.json](choice-responsibility-scope.json)和[excluded-pages.json](excluded-pages.json)。' %
             json.dumps(summary['choice_responsibility_scope']['mixed_or_unconfirmed_pages'], ensure_ascii=False), '',
             '“由Choice承担”是用户指定数值匹配规则下的候选责任标签，不证明科目、范围或期间已覆盖。'
             '排除范围含母公司主表和所有者权益变动表，Choice实际只转换合并三表；'
             '这两类条目即使同值获此标签，本轮仍无对应记录，不能据此宣称补齐。', '',
             '初值2万字符来自S1每条约1300–1500字符JSON及S46约六倍输出量。实际调整、失败候选与输入哈希保护见'
             '[RECHUNK-REPORT.json](RECHUNK-REPORT.json)（如有）及[chunk-splits/](chunk-splits/)（如有）。', '',
             '字数上限调整的原因和输入哈希保护见本轮RECHUNK回执；实际超时与拆分代价列在下表，'
             '不能只凭较小子叶成功就确认原上限对密集表通用。', '',
             capacity_observations_markdown(summary['capacity_observations'], prep.get('core_char_limit')), '',
             firstpass_status_markdown(summary['firstpass_chunk_status']), '',
             '## Schema、prompt与运行环境', '',
             '--output-schema已接受：%s；使用传输Schema：%s。精简字段说明由records.py生成，prompt不重复整份Schema；'
             '代码补source、disclosed_on、extraction_version、verification，并给记录及覆盖引用加块前缀。' %
             (schema['accepted'], schema['transport_used']), '',
             'run内实际Schema文件字符数：stored %s、model-full %s、model-transport %s；'
             '当前records.field_guide()为%s字符。Schema字符数包含文件格式空白，缺文件保留未知；'
             '字符数不是token数，未单独计量Schema token。' %
             (show(schema['schema_characters']['stored']), show(schema['schema_characters']['model_full']),
              show(schema['schema_characters']['model_transport']), show(schema['field_guide_characters'])), '',
             '原始拒绝信息、CLI路径和配置见[schema-diagnostics.json](schema-diagnostics.json)；原样events/stderr仍保留在logs。'
             '传输Schema只用于接口兼容，完整约束仍由代码检查。', '',
             (('原始Schema的strict错误指向`locator.pdf_pages.uniqueItems`。' if
                'uniqueItems' in json.dumps(schema, ensure_ascii=False) else '') +
              '传输版从同一定义移除`$schema/title/format/pattern/minLength/minimum/maximum/uniqueItems/minItems/maxItems`；'
              '对象结构、类型、必填项和枚举保留，完整字段约束仍逐条校验。'
              if schema['transport_used'] else '本轮未使用传输Schema。'), '',
             '|调用阶段/尝试|状态|prompt字符|CLI|', '|---|---|---:|---|']
    for row in schema['attempts']:
        cli = row.get('cli') or {}
        lines.append('|%s/%s|%s|%s|%s|' % (row['stage'], row['attempt'], row['status'], show(row['prompt_chars']),
                                                 cli.get('version', row['command'][0])))
    for observation in environment:
        lines.extend(['', '- `%s`：%s。' % (observation['executable'], observation.get('version', observation.get('error')))])
    for configuration in summary.get('configurations', []):
        lines.extend(['', '启动时沿用CLI配置：模型`%s`，推理强度`%s`；没有传入`-m`或推理强度覆盖。每次配置回执保存在record.json。' %
                      (configuration.get('model', '未记录'), configuration.get('model_reasoning_effort', '未记录'))])
    lines.extend(['', '抽取调用的实际prompt长度（含本块原文、来源元数据、清单及生成的字段说明）：', '',
                  '|阶段|版本|字符|', '|---|---|---:|'])
    for row in summary['prompt_rows']:
        if 'schema' not in row['stage']:
            lines.append('|%s|%s|%s|' % (row['stage'], row['prompt_version'], show(row['prompt_chars'])))
    for gate in summary['pilot_gates']:
        lines.extend(['', '第%d轮试点放行：%s；%s。' %
                      (gate['round'], gate['ok'], '；'.join(gate['errors']) or '全部条件满足')])
    legacy = [a for a in schema['attempts'] if a['command'][0] == 'codex' and a['status'] == 'failed']
    if legacy:
        lines.extend(['', '首次未先锁定P1所用CLI路径，PATH中的旧CLI导致探测失败；这次可避免的失败仍计预算。'
                      '随后使用与P1匹配的本地CLI，未改model或推理强度。'
                      '旧CLI0.144.2、新CLI0.163.0的只读版本观测见[cli-observations.json](cli-observations.json)。'])
    lines += ['', '## 用量与P1对照', '',
              '已登记%s/%s次调用；输入%s、缓存%s、未缓存%s、输出%s、推理%s tokens；命令%s，累计调用耗时%s秒（并发调用分别计时，并非墙钟总时长）。'
              '未报告输入用量%d次，未知部分不按零计算。逐次、成功/失败及P1全部18次与抽取部分的分列见[USAGE.md](USAGE.md)。' %
              (summary['reserved_calls'], summary['call_limit'], show(totals['input']), show(totals['cached']),
               show(totals['uncached']), show(totals['output']), show(totals['reasoning']), show(totals['commands']),
               show(totals['seconds']), totals['input_unknown_calls']), '',
              overall_cost_markdown(summary['overall_cost_observations']), '',
              same_narrative, '',
              '超时%d次；疑似截断%s次，逐条拒收统计见[rejection-summary.json](rejection-summary.json)。'
              '没有模型用量或计费回执的部分不推断金额。' %
              (totals['by_status'].get('timed_out', 0), summary['rejection']['suspected_truncations']), '',
              '上述成本仅为登记的codex exec调用；主会话及子agent的开发、审查和逐页回读用量未计量，不能视为零。', '',
              '## 覆盖与错误', '',
              '初始切块全部完成的文档：%s。其他文档只在全部所引页都就绪时将未匹配/候选项送入回读，仍不代表语义接受。' %
              ('、'.join(summary.get('completed_sources', [])) or '暂无'), '',
              '引页就绪条目范围：%s条P1条目，其中整份来源尚未完成但引页均就绪%s条；可回读未匹配/候选%s条。'
              '这些数量只界定回读范围，不能作为全量覆盖率分母。未就绪区域已有同值代码匹配%s条，'
              '仍只提示存在候选记录，不证明该页全部覆盖。每页须全部相关core叶块通过，context不计；'
              '未完成文档的混合排除边界页不算就绪，全页排除仍只按Choice规则处理。'
              '逐页依据和按来源条目范围见[comparison-regions.json](comparison-regions.json)。' %
              (summary['comparison_region_summary']['region_complete_items'],
               summary['comparison_region_summary']['partial_region_complete_items'],
               summary['comparison_region_summary']['ready_unmatched_items'],
               summary['comparison_region_summary']['unready_code_match_items']), '',
              '收下PDF抽取记录%d条；P1可落成记录的盘点条目%d条。代码候选分布：%s。'
              '代码同值匹配可能误配，不能代替语义验收。' %
              (summary['accepted_pdf_records'], read(root / 'comparison.json', {}).get('item_count', 0),
               json.dumps(summary['comparison_counts'], ensure_ascii=False)), '',
              '“待抽取”是对照保守标签，不等于%s条均因40次预算未运行。范围分组：%s。'
              '排除或边界项不能判漏抽；母公司主表及权益变动表仍无对应记录。Choice同值候选不等于科目覆盖。'
              '逐条ID、引页和分组原因见[pending-scope-diagnostics.json](pending-scope-diagnostics.json)。' %
              (summary['pending_scope_diagnostics']['item_count'],
               json.dumps(summary['pending_scope_diagnostics']['counts'], ensure_ascii=False)), '',
              '逐条对照见[comparison.json](comparison.json)；固定随机种子：未匹配20261022、反向20261023、语义20261024。'
              'S34已就绪区域及S42的可回读未匹配项按来源/块分层共抽60条，'
              '三份公告未匹配项全部看；反向40条、语义80条按来源/类型分层。样本不足时保留实际数量。'
              '未裁决样本保持pending，不自动写正确或零错误。', '',
              '语义和反向样本由主会话逐条查看冻结页；未匹配项可由子agent分工回读，主会话汇总复查。'
              '实际reviewer与依据随各份draft及合并回执的drafts.entries映射留存；不是独立人类复核。'
              '分层样本的错误比例只描述实际样本，不作为全体记录的加权错误率估计。', '',
              '逐条抽查裁决、错误类型及页码依据见[未匹配回读](coverage-review.json)、'
              '[反向检查](reverse-review.json)、[语义抽查](semantic-review.json)；原草稿与绑定回执保存在reviews/。', '',
              'P1的[原交付记录](../p1-20261021/REPORT.md)留有25个候选的定向回读和E01–E04修正，'
              '没有同口径80条随机记录样本。P1笔记也不使用本轮记录Schema；因此不能把其修正项数除以1899，'
              '或与S2逐条拒收率相减，声称错误率升降。这里只比较已知错误类型和本轮样本结果。', '',
              '|人工队列|已裁决|实际样本|比率口径|比率|', '|---|---:|---:|---|---:|']
    for name, review in summary['reviews'].items():
        metric = '新增候选无效率' if name == 'reverse' else '语义错误率（可判定分母）' if name == 'semantic' else '不计算'
        lines.append('|%s|%d|%d|%s|%s|' % (name, review['reviewed'], review['total'], metric,
                                        percent(review['error_rate'])))
    for name, review in summary['reviews'].items():
        lines += ['', '%s裁决分布：%s；错误类型：%s。' % (name,
                  json.dumps(review['counts'], ensure_ascii=False), json.dumps(review['error_types'], ensure_ascii=False))]
    reverse = summary['reverse_error_breakdown']
    lines += ['', 'reverse的“错误”表示新增内容候选无效，其新增候选无效率不能当作抽取内容错误率。'
              '错误裁决%s条中，仅标“反向候选误判”%s条，另有其他错误类型%s条，'
              '两类标签并存%s条；后两类的具体内容判断见逐条error_types与页码依据。'
              '“反向候选误判”表示P1已有该事实但匹配规则漏判，不进入80条semantic样本的语义错误率；'
              'semantic只按自己的固定样本和显式裁决计算。' %
              (reverse['invalid_candidates'], reverse['candidate_matching_only'], reverse['other_error_records'],
               reverse['candidate_matching_with_other_types'])]
    if (root / 'READBACK-NOTES.md').exists():
        lines += ['', '固定样本之外的定向回读及核验表示限制见[READBACK-NOTES.md](READBACK-NOTES.md)。'
                  '这些观察没有改变拒收结果，也不替代80条语义样本。', '']
    rejection = summary['rejection']
    current_rejection = rejection['current_prompt']
    lines += ['', '全部尝试（含旧版prompt）的可核验输出逐条拒收：%d/%d，比例%s；按检查项为%s。'
              '这是全部尝试合计，不是定稿版本的拒收率。整块失败及未完成输出不作零拒收。'
              '拒收、仅落上下文页丢弃、引用降级分别记录。' %
              (rejection['rejected_records'], rejection['measured_output_records'], percent(rejection['rate']),
               json.dumps(rejection['by_check'], ensure_ascii=False)), '',
              '定稿当前版本`%s`：拒收%s条，逐条核验输出分母%s条，比率%s；完成逐条核验%s次，未完成%s次。'
              '本版按检查项为%s；本版格式错误为%s。未取得逐条核验结果时保留null，不写零。' %
              (current_rejection['prompt_version'], show(current_rejection['rejected_records']),
               show(current_rejection['measured_output_records']), percent(current_rejection['rate']),
               current_rejection['individually_validated_calls'], current_rejection['unvalidated_calls'],
               json.dumps(current_rejection['by_check'], ensure_ascii=False),
               json.dumps(current_rejection['numeric_format_errors'], ensure_ascii=False)), '',
              '|prompt版本|尝试|已逐条核验|未逐条核验|拒收记录|核验输出分母|拒收率|检查项|',
              '|---|---:|---:|---:|---:|---:|---:|---|']
    for version, totals in rejection['by_prompt_version'].items():
        lines.append('|%s|%s|%s|%s|%s|%s|%s|%s|' %
            (version, totals['attempts'], totals['individually_validated_calls'], totals['unvalidated_calls'],
             show(totals['rejected_records']), show(totals['measured_output_records']), percent(totals['rate']),
             json.dumps(totals['by_check'], ensure_ascii=False)))
    lines += ['', '全部尝试的格式错误另计：%s。numbers.value必须是没有千分位逗号的十进制字符串；源摘录仍保留原样逗号。'
              '代码拒收不自动推定其他字段语义错误；语义错误率仍须逐页裁决。' %
              json.dumps(rejection['numeric_format_errors'], ensure_ascii=False), '',
              '|试点/调用|prompt版|输出记录|拒收记录|拒收率|', '|---|---|---:|---:|---:|']
    for block in rejection['blocks']:
        lines.append('|%s|%s|%s|%s|%s|' % (block['stage'], show(block['prompt_version']), show(block['output_records']),
                                          show(block['rejected']), percent(block['rate'])))
    lines += ['', '|S46试点对照|匹配S1记录|S1记录|比例|满足80%|', '|---|---:|---:|---:|---|']
    for match in summary['pilot_matches']:
        lines.append('|%s|%s|%s|%s|%s|' % (match['path'], show(match.get('matched_reference_count')),
                      show(match.get('reference_count')), percent(match.get('reference_match_ratio')), match.get('passed')))
    lines += ['',
              '文档级清单合并如下；详细依据、页序和记录编号见[document-coverage.json](document-coverage.json)。', '',
              '|文档|已抽|本文档无|未覆盖|', '|---|---:|---:|---:|']
    for source, rows in coverage.items():
        counts = Counter(row['status'] for row in rows)
        lines.append('|%s|%d|%d|%d|' % (source, counts['已抽'], counts['本文档无'], counts['未覆盖']))
    lines += ['', '“已抽”按任一块已抽即成立的合并规则产生，不代表每期每格完整；'
              '块依据里已声明的部分省略仍需保留。单同值的P1候选匹配同样不能证明复合条目全部覆盖。', '']
    lines += ['', '## Choice与记录间一致', '',
              '按字段级认定224个原表科目，6期共1344个字段期间机会；实际生成%s条，空值跳过%s，异常非空值%s。'
              '5个派生和120个待定（含元数据）不转；共跳过30和720个字段期间。查值结果：%s。'
              '来源清单行、版本链、SHA-256及每期FIRSTNOTICEDATE绑定见[choice/binding.json](choice/binding.json)。'
              '只接受ReportType=1，无API调用。' % (show(choice.get('records')), show(choice.get('empty_original_values')),
              show(choice.get('invalid_original_values')), json.dumps(choice.get('value_checks', {}), ensure_ascii=False)), '',
              '同键同披露日数值冲突%s项；A01对Choice为%s。查值命中仅证明全文同值存在，不能代替科目/列定位或语义验收。'
              '没有PDF输入时，A01明确为未运行。质检队列不是研究缺口。' %
              (qa['summary']['same_key_conflicts'], json.dumps(qa['summary']['choice_pdf_a01'], ensure_ascii=False)), '',
              'A01存在主体、指标、期间相同的候选时，完整键差异计数为%s；仍标未对齐，不比较数值。' %
              json.dumps(qa['summary'].get('a01_unaligned_key_fields', {}), ensure_ascii=False), '',
              '分项合计：%s；分部对集团：%s。上下限、单位/期间或抵销口径不足的记录列为无法比较；约数/舍入差额单列。' %
              (json.dumps(qa['summary']['additivity'], ensure_ascii=False), json.dumps(qa['summary']['segment_group'], ensure_ascii=False)), '',
              '合计报警只评价记录中提供的可加组；缺少父层分项或嵌套小计关系时，不能据此断言原报表算错。'
              '已回读的具体原因另见READBACK-NOTES.md（如有）。', '',
              '股权对价455百万元覆盖链：%s；400吨→400吨/年覆盖链：%s。键差异逐项列subject、indicator、period及其他维度，'
              '不按数值推断主体同一。详见[qa-queue.json](qa-queue.json)。' %
              (qa['cross_document_diagnostics']['equity_consideration_455_million']['status'],
               qa['cross_document_diagnostics']['capacity_400_tonnes']['status']), '']
    for target, diagnostic in qa['cross_document_diagnostics'].items():
        lines += ['`%s`：收下集中出现来源%s，预期来源%s。' %
                  (target, '、'.join(diagnostic['observed_sources']) or '无', '、'.join(diagnostic['expected_sources'])), '',
                  '|记录|主体|指标|期间|', '|---|---|---|---|']
        for row in diagnostic.get('target_records', []):
            key = row['object_key']
            lines.append('|%s|%s|%s|%s|' % (row['record_id'], key['subject'], key['indicator'], key['period']))
        fields = Counter(field for pair in diagnostic['unaligned_pairs'] for field in pair['different_fields'])
        lines += ['', '跨来源候选键差异计数：%s；尚未映射的名称和期间不自动归一。' %
                  json.dumps(dict(fields), ensure_ascii=False), '']
    lines += ['## 进入S3之前要决定', '',
              '1. JSON输出量：已测成功PDF调用每条收下记录输出%s tokens（含推理和封套）。'
              '按已测块字符密度外推P0的%s份、%s字符，输出约%s tokens、记录约%s条。%s；'
              '样本结构、主表排除比例及IPO差异使它只能作情景，不能当预算。' %
              (show(summary['projection']['output_tokens_per_accepted_record']), show(summary['projection']['p0_sources']),
               show(summary['projection']['p0_source_chars']), show(summary['projection']['p0_output_tokens_by_char']),
               show(summary['projection']['p0_records_by_char']), summary['projection']['extrapolation_status']),
              '2. 切块上限：结合密集附注试点输出、截断与拒收率决定是否保留2万字符；结构边界识别失败必须停止报告。',
              '3. 财务报表页：是否继续由Choice承担合并三表，以及母公司主表、权益变动表是否另设抽取范围。',
              '4. 主体与指标命名：法定全称和统一期间标签仍可能产生跨公告键差异；Choice已有其他应收款（合计/分项）、'
              '财务费用（利润表/现金流调节）同名口径，应由S3命名和主体登记表决策，不在S2悄悄改字段表。',
              '5. S2未执行块、未覆盖项及已发现错误如何处理，是否另行授权补充验证。'
              '本轮40次预算不延长，不把代码检查当研究接受。', '',
              '6. 类型与覆盖粒度：是否约束多指标表格打包成statement、嵌套小计关系、中英文数词及年度source_unit表示；'
              '如何发现已读历史列省略，以及避免任一同值或任一块已抽掩盖部分缺失。已测每条记录成本不等于正确原子事实成本。', '',
              '7. 日期和期间合同：历史事件occurred_on已知时，as_of能否取当前公告观察日；'
              '通用方法的期间“不适用”和“未知”应如何区分。',
              '8. prospective关系的nature边界：已披露的未来关系安排何时属于actual事实、何时属于guidance；'
              '关系状态与事实性质如何组合，须在S3明确，不追改本轮输出或核验。', '',
              '## 完整性与交付边界', '',
              'P1、S1两个run目录（含内部Git文件）、两份设计稿、p1-sample标签及指定HEAD/分支完整性：%s。'
              '见[INPUT-INTEGRITY.json](INPUT-INTEGRITY.json)。不改共享资料库和S1取数预算。'
              '本轮没有计算层、研究问题清单、主体登记表、页面生成或问答评测。' % summary['integrity_ok'], '',
              ('测试回执见[tests-final.log](tests-final.log)；本报告不凭测试通过推断来源语义正确。' if
               (root / 'tests-final.log').is_file() else
               '测试回执另见本轮保存的测试日志；本报告不凭测试通过推断来源语义正确。'), '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--phase', choices=PHASES, required=True)
    parser.add_argument('--reason', default='')
    parser.add_argument('--reviews', type=Path, help='explicit hash-bound reviewer judgments')
    args = parser.parse_args()
    result = build_report(args.run_dir, args.phase, args.reason, args.reviews)
    print(json.dumps({key: result[key] for key in ('phase', 'extraction_complete', 'accepted_pdf_records', 'choice_records', 'call_count')},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
