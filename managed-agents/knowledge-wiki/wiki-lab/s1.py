#!/usr/bin/env python3
"""S1-only reproducible preparation, immutable-input audit and test receipts.

No model calls. No S2 extractor. Data-bearing outputs are confined to runs/.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

from corpus import HERE, dump, sha256
from fetch import active_terminal_stop, append, now, read_rows


def snapshot(run, filename):
    """Preserve a prior generated receipt by content hash before refreshing it."""
    path = run / filename
    if path.exists():
        saved = run / 'history' / path.stem / (sha256(path) + path.suffix)
        if not saved.exists():
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(path.read_bytes())


def rejection_history(run, samples):
    """Describe the latest archived diagnostic and the subsequent validation."""
    from datetime import datetime, timedelta, timezone
    archives = list((run / 'sample-rejections').glob('*.json'))
    if not archives:
        return '- sample-rejections/没有已归档的拒收诊断。'
    if any(sha256(path) != path.stem for path in archives):
        raise ValueError('Archived sample diagnostic changed')
    local_zone = timezone(timedelta(hours=8))
    candidates = [(datetime.fromtimestamp(path.stat().st_mtime, local_zone), path, False)
                  for path in archives]
    for attempt in read_rows(run / 'sample-rejections' / 'attempts.jsonl'):
        diagnostic = run / 'sample-rejections' / attempt['diagnostic']
        if diagnostic not in archives or sha256(diagnostic) != attempt['sha256']:
            raise ValueError('Sample rejection attempt refers to a missing or changed diagnostic')
        candidates.append((datetime.fromisoformat(attempt['rejected_at']), diagnostic, True))
    rejected_at, latest, timed_attempt = max(candidates, key=lambda row: (row[0], row[1].name, row[2]))
    rejected_at = rejected_at.astimezone(local_zone)
    errors = json.loads(latest.read_text(encoding='utf-8'))
    reasons = '；'.join(f'`{record_id}`：' + '、'.join(problems)
                       for record_id, problems in errors.items())
    validation_path = run / 'sample-validation.json'
    validated_at = (datetime.fromisoformat(samples['validated_at']) if samples.get('validated_at')
                    else datetime.fromtimestamp(validation_path.stat().st_mtime, local_zone))
    passed_later = (samples.get('errors') == 0 and validated_at > rejected_at
                    and not (run / 'sample-validation-errors.json').exists())
    result = (f'该次拒收之后，{validated_at.astimezone(local_zone).isoformat(timespec="seconds")}的'
              f'一次校验记录显示{samples["records"]}条样例全部通过；该次成功重建已将顶层错误文件归档移走。'
              if passed_later else '尚无其后全部通过且顶层错误文件已移走的校验结果。')
    time_basis = ('拒收时间取attempts.jsonl中的失败尝试时间。' if timed_attempt else
                  '旧诊断未内嵌时间，拒收时间取归档时保留的文件修改时间。')
    return (f'- 最近一次已归档拒收：{rejected_at.isoformat(timespec="seconds")}；{reasons}。'
            f'{result}诊断见 [sample-rejections/{latest.name}](sample-rejections/{latest.name})；'
            f'{time_basis}')


def fingerprint(p1):
    return {str(p.relative_to(HERE)): sha256(p)
            for p in sorted(p1.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}


def baseline(run):
    path = run / 'INPUT-BASELINE.json'
    if path.exists():
        raise ValueError('S1 baseline already exists; never replace it')
    p1 = HERE / 'runs/p1-20261021'
    protected = {name: sha256(HERE / name) for name in ('DESIGN.md', 'DESIGN-v2.md')}
    tag = subprocess.check_output(['git', '-C', str(p1 / 'wiki'), 'rev-parse', 'refs/tags/p1-sample'], text=True).strip()
    dump(path, {'created_at': now(), 'branch': subprocess.check_output(['git', 'branch', '--show-current'], cwd=HERE, text=True).strip(),
                'p1_files': fingerprint(p1), 'protected': protected, 'p1_tag_object': tag})
    docs = '/Users/leon/Stock/invest-lab/.agents/skills/choice-quantapi-skill/references/'
    hk = {'schema': 'wiki-lab-requests/1', 'source_slug': 's1-trigiant-hk-statements', 'company_code': '01300.HK',
          'title': 'S1 Trigiant HK complete statements 2025-12-31',
          'documentation': [docs + 'functions/ctr.md', docs + 'help/error-codes.md'] +
                           [docs + 'indicators/ctr/000200010001000' + str(i) + '.md' for i in range(1, 4)],
          'requests': [{'id': 's1-hk-' + str(i), 'purpose': 'S1 HK complete statements interface test', 'function': 'ctr',
                        'args': [table, '', 'SecuCode=01300.HK,ReportDate=2025-12-31,ReportType=1,Ispandas=0'], 'kwargs': {}}
                       for i, table in enumerate(('BalanceStatementHK', 'IncomeStatementHK', 'CashFlowStatementHK'), 1)]}
    fx = {'schema': 'wiki-lab-requests/1', 'source_slug': 's1-fx-rmb-spot-members', 'company_code': None,
          'title': 'S1 FX alternative: RMB spot members',
          'documentation': [docs + 'functions/sector.md', docs + 'indicators/sector-indicator-category-index.md'],
          'requests': [{'id': 's1-fx-1', 'purpose': 'S1 FX: untried RMB spot sector; obtain HKD code before quotation query',
                        'function': 'sector', 'args': ['114002001', '2026-10-21', 'Ispandas=0'], 'kwargs': {}}]}
    dump(run / 'requests-hk.json', hk)
    dump(run / 'requests-fx.json', fx)
    return {'status': 'prepared', 'p1_files': len(fingerprint(p1)), 'run': str(run)}


def verify(run):
    old = json.loads((run / 'INPUT-BASELINE.json').read_text())
    current = fingerprint(HERE / 'runs/p1-20261021')
    changed = sorted(k for k in set(old['p1_files']) | set(current) if old['p1_files'].get(k) != current.get(k))
    protected = {name: sha256(HERE / name) for name in old['protected']}
    tag = subprocess.check_output(['git', '-C', str(HERE / 'runs/p1-20261021/wiki'), 'rev-parse', 'refs/tags/p1-sample'], text=True).strip()
    result = {'checked_at': now(), 'p1_changed_files': changed, 'design_unchanged': protected == old['protected'],
              'p1_tag_unchanged': tag == old['p1_tag_object'], 'p1_files': len(current)}
    result['passed'] = not changed and result['design_unchanged'] and result['p1_tag_unchanged']
    dump(run / 'INPUT-INTEGRITY.json', result)
    return result


def tests(run):
    import os
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-v']
    result = subprocess.run(command, cwd=HERE, env=env, capture_output=True, text=True)
    snapshot(run, 'TESTS.txt')
    snapshot(run, 'TESTS.json')
    (run / 'TESTS.txt').write_text(result.stdout + result.stderr, encoding='utf-8')
    receipt = {'at': now(), 'command': command, 'returncode': result.returncode, 'log_sha256': sha256(run / 'TESTS.txt')}
    dump(run / 'TESTS.json', receipt)
    return receipt


def fx_members_request(run, midpoint=False):
    """Prepare a documented, previously untried latest-members request."""
    sector = '114001' if midpoint else '114002001'
    slug = 'midpoint' if midpoint else 'rmb-spot'
    path = run / ('requests-fx-' + slug + '.json')
    if path.exists():
        raise ValueError('FX request already prepared; reuse its bytes, do not overwrite')
    original = json.loads((run / 'requests-fx.json').read_text())
    original.update(source_slug='s1-fx-' + slug + '-members', title='S1 FX ' + slug + ' latest members')
    original['requests'] = [{'id': 's1-fx-2' if midpoint else 's1-fx-1',
                             'purpose': 'S1 FX documented alternative sector; find returned HKD code without guessing',
                             'function': 'sector', 'args': [sector, now()[:10], 'Ispandas=0'], 'kwargs': {}}]
    dump(path, original)
    append(run / 'operations.jsonl', {'at': now(), 'action': 'prepare_fx_members', 'path': str(path), 'sha256': sha256(path)})
    return {'request': str(path), 'sector': sector, 'calls': 1}


def fx_quote_request(run):
    """Use the actual returned HKD/CNY member; never invent a vendor code."""
    from choice_audit import unpack
    matches = []
    for row in read_rows(run / 'fetch/manifests/sources.jsonl'):
        path = run / 'fetch' / row['path']
        if sha256(path) != row['sha256']:
            raise ValueError('Changed FX capture')
        payload = json.loads(path.read_text())
        for query in payload.get('queries', []):
            if query['query']['function'] != 'sector':
                continue
            result = unpack(query.get('result', {}))
            if result.get('ErrorCode') != 0:
                continue
            codes, data, fields = result.get('Codes', []), result.get('Data', []), result.get('Indicators', [])
            if fields != ['SECUCODE', 'SECURITYSHORTNAME'] or len(data) != len(codes) * 2:
                continue
            for offset in range(0, len(data), 2):
                code, name = data[offset:offset + 2]
                if code in codes and 'HKD' in code and 'CNY' in code and '港' in name:
                    matches.append((code, name, row['source_id'], row['sha256']))
    if len(matches) != 1:
        raise ValueError('Expected exactly one actually returned, named HKD/CNY member')
    code, name, source_id, digest = matches[0]
    path = run / 'requests-fx-quote.json'
    if path.exists():
        raise ValueError('Quote request already exists; do not overwrite')
    docs = '/Users/leon/Stock/invest-lab/.agents/skills/choice-quantapi-skill/references/'
    doc = {'schema': 'wiki-lab-requests/1', 'source_slug': 's1-fx-hkd-midpoint-quotes', 'company_code': None,
           'title': 'S1 FX named HKD historical quotation test',
           'code_binding': {'code': code, 'returned_name': name, 'source_id': source_id, 'sha256': digest},
           'documentation': [docs + 'functions/csd.md', docs + 'indicators/csd/110008.md', docs + 'indicators/csd/110008011.md'],
           'requests': [{'id': 's1-fx-3', 'purpose': 'S1 FX historical CLOSE and SNAME using actual returned member; midpoint kept distinct from spot',
                         'function': 'csd', 'args': [code, 'CLOSE,SNAME', '2026-10-12', '2026-10-15',
                                                   'Period=1,AdjustFlag=1,Order=1,Market=0,FillData=0,Ispandas=0'], 'kwargs': {}}]}
    dump(path, doc)
    append(run / 'operations.jsonl', {'at': now(), 'action': 'prepare_fx_quote', 'path': str(path), 'sha256': sha256(path)})
    return {'request': str(path), 'code': code, 'name': name, 'calls': 1}


def inspect_fx_query(query, source_id, digest):
    """Separate transport success, returned identifiers and numeric quotations."""
    from choice_audit import numeric_value, unpack
    q = query['query']
    result = unpack(query.get('result', {}))
    summary = {'source_id': source_id, 'sha256': digest, 'query': q,
               'error_code': result.get('ErrorCode'), 'error_message': result.get('ErrorMsg'),
               'numeric_quote_count': 0, 'quotes': []}
    if result.get('ErrorCode') != 0:
        summary.update(outcome='no_data_or_error', reason=result.get('ErrorMsg') or query.get('exception'))
    elif q['function'] == 'sector':
        summary.update(outcome='members_returned', codes=result.get('Codes', []),
                       reason='成分代码与名称不是汇率报价；非股票板块只能查最新成分')
    else:
        fields, dates = result.get('Indicators', []), result.get('Dates', [])
        for code in result.get('Codes', []):
            columns = result.get('Data', {}).get(code, [])
            for index, day in enumerate(dates):
                close = columns[fields.index('CLOSE')][index] if 'CLOSE' in fields else None
                name = columns[fields.index('SNAME')][index] if 'SNAME' in fields else None
                summary['quotes'].append({'code': code, 'date': day.replace('/', '-'), 'field': 'CLOSE',
                                          'raw_value': close, 'name': name,
                                          'basis': 'midpoint' if name and '中间价' in name else 'unconfirmed'})
                summary['numeric_quote_count'] += int(numeric_value(close) is not None)
        count = summary['numeric_quote_count']
        summary.update(outcome='numeric_quotes' if count else 'no_numeric_quote',
                       reason='保留供应商报价口径，不自动替代Wind即期' if count else
                              'ErrorCode=0且名称存在，但CLOSE为空；只有元数据不能判取数成功')
    return summary


def interface_audit(run):
    """Read existing captures and local docs only. Never retries a stopped SDK."""
    source_root = Path('/Users/leon/Stock/invest-sources')
    old = []
    for path in sorted((source_root / 'vendor/choice').glob('choice-20261015-bc-fx-*.json')):
        item = json.loads(path.read_text(encoding='utf-8'))
        old.append({'path': str(path), 'sha256': sha256(path), 'query': item['query'],
                    'response': item['response'],
                    'interpretation': '证券搜索无匹配或板块无数据；尚未验证港币报价时间序列'})
    logs = read_rows(run / 'fetch/calls.jsonl')
    batches = [row for row in logs if row['event'] == 'batch_finished']
    starts = [row for row in logs if row['event'] == 'call_started']
    manifest = read_rows(run / 'fetch/manifests/sources.jsonl')
    fx_checks = []
    hk_returns = {}
    for row in manifest:
        if sha256(run / 'fetch' / row['path']) != row['sha256']:
            raise ValueError('Captured Choice return changed')
        payload = json.loads((run / 'fetch' / row['path']).read_text())
        for query in payload.get('queries', []):
            q = query['query']
            if q['function'] in ('sector', 'csd'):
                fx_checks.append(inspect_fx_query(query, row['source_id'], row['sha256']))
            elif q['function'] == 'ctr' and q['args'][0].endswith('StatementHK'):
                from choice_audit import ctr_rows, unpack
                result = unpack(query['result'])
                hk_returns[q['args'][0]] = {'source_id': row['source_id'], 'sha256': row['sha256'],
                                           'fields': result['Indicators'], 'rows': ctr_rows(query)}
    docs = Path('/Users/leon/Stock/invest-lab/.agents/skills/choice-quantapi-skill/references/indicators/ctr')
    search_doc = docs.parent.parent / 'functions/secucode.md'
    declarations = []
    for i, name in enumerate(('BalanceStatementHK', 'IncomeStatementHK', 'CashFlowStatementHK'), 1):
        path = docs / ('000200010001000' + str(i) + '.md')
        text = path.read_text(encoding='utf-8')
        declarations.append({'table': name, 'documentation_path': str(path), 'documentation_sha256': sha256(path),
                             'declares_NOTICEDATE': '|NOTICEDATE|' in text,
                             'declares_FIRSTNOTICEDATE': 'FIRSTNOTICEDATE' in text,
                             'report_type_parameter': '1 合并报表 / 2 合并报表(调整) / 3 母公司 / 4 母公司(调整)',
                             'tested_return_fields': hk_returns.get(name, {}).get('fields')})
    hk_path = run / 'choice-hk-interface-audit.json'
    hk_audit = json.loads(hk_path.read_text()) if hk_path.exists() else None
    # Bind and verify the exact old byte prefix retained by the resume event.
    import hashlib
    prefix = b''
    prefix_preserved = True
    for raw in (run / 'fetch/calls.jsonl').read_bytes().splitlines(keepends=True):
        event = json.loads(raw)
        if event['event'] == 'network_resume_authorized':
            prefix_preserved &= hashlib.sha256(prefix).hexdigest() == event['previous_log_sha256']
        prefix += raw
    if not prefix_preserved:
        raise ValueError('Original failure log prefix changed')
    terminal = active_terminal_stop(logs)
    state = 'blocked' if terminal else 'complete' if len(hk_returns) == 3 and hk_audit and len(fx_checks) == 3 else 'pending'
    numeric_quotes = sum(row['numeric_quote_count'] for row in fx_checks)
    value = {'checked_at': now(), 'status': state,
             'data_calls_used': len(starts), 'data_call_cap': 8,
             'remaining_calls': 8 - len(starts), 'original_failure_log_prefix_unchanged': prefix_preserved,
             'resume_events': [row for row in logs if row['event'] == 'network_resume_authorized'],
             'login_attempts_in_python': len([r for r in logs if r['event'] == 'login']),
             'login_retry_note': 'SDK内部切换不增加Python层start次数；用户明确授权恢复后的各批登录另计',
             'batches': batches, 'captures': manifest, 'hk_documentation': declarations,
             'hk_return_summary': hk_returns,
             'hk_value_checks': {'path': str(hk_path.relative_to(run)), 'sha256': sha256(hk_path)} if hk_audit else None,
             'hk_reason': '已执行三表接口，原表与自定口径判断见独立HK审计；未转成正式记录',
             'prior_fx_queries': old,
             'prior_fx_reason_limit': '只确认返回为空；不能仅凭空返回判断权限、SDK能力或产品无报价',
             'fx_documentation_review': {'path': str(search_doc), 'sha256': sha256(search_doc),
                                         'finding': '019确为文档列示外汇类别，但功能简介仅承诺股票实体识别，文档范围不一致；两次实体搜索没有取得证券代码，不是报价查询。114002002列为外汇即期，114002001另列人民币即期。具体后台空值原因未提供。'},
             'fx_checks': fx_checks, 'fx_calls_used': len(fx_checks), 'fx_call_cap': 3,
             'fx_numeric_quotes': numeric_quotes,
             'fx_fallback': '继续用Wind；Choice没有取到数值，且中间价与即期不能互换' if not numeric_quotes else
                            '保留Wind既有口径；新报价尚未做采用认定',
             'network_permission_reexecution': '同一命令在网络权限执行时被terminal_stop保护拦下，未调用SDK；未删日志、未重置预算'}
    snapshot(run, 'interface-tests.json')
    dump(run / 'interface-tests.json', value)
    prior_audits = {row.get('capture_sha256') for row in logs if row['event'] == 'interface_interpretation'}
    for row in fx_checks:
        if row['sha256'] not in prior_audits and not (run / 'rework-baseline.json').exists():
            append(run / 'fetch/calls.jsonl', {'event': 'interface_interpretation', 'at': now(),
                                             'capture_sha256': row['sha256'], 'source_id': row['source_id'],
                                             'outcome': row['outcome'], 'reason': row['reason'],
                                             'numeric_quote_count': row['numeric_quote_count']})
    return value


def rework_section(run, fit, audit, hk):
    """Rebuild the S1 rework comparison from content-addressed old artifacts."""
    baseline_path = run / 'rework-baseline.json'
    if not baseline_path.exists():
        return []
    from collections import Counter
    from copy import deepcopy
    import re
    import statistics
    from records import MAX_EXCERPT_CHARS
    baseline = json.loads(baseline_path.read_text())

    def archived(name):
        path = Path(name)
        digest = baseline['archived_files'][name]
        saved = run / 'history' / path.stem / (digest + path.suffix)
        if sha256(saved) != digest:
            raise ValueError(f'Rework baseline archive changed: {name}')
        return json.loads(saved.read_text())

    manifest = json.loads((HERE / 'runs/p1-20261021/corpus-manifest.json').read_text())
    pages = {(source['source_id'], row['page']): Path(row['corpus_path']).read_text()
             for source in manifest['sources'] for row in source['page_records']}
    current = [row for path in sorted((run / 'records').glob('*.json'))
               for row in json.loads(path.read_text())]
    quotes = [(r['source']['source_id'], q['page'], q['text']) for r in current for q in r['excerpts']]
    compact = lambda text: re.sub(r'\s+', '', text)
    lengths = [len(compact(q[2])) for q in quotes]
    metrics = dict(records=len(current), excerpts=len(quotes), median=statistics.median(lengths),
                   maximum=max(lengths), over_limit=sum(n > MAX_EXCERPT_CHARS for n in lengths),
                   page_80_percent=sum(len(compact(text)) >= .8 * len(compact(pages[(sid, page)]))
                                       for sid, page, text in quotes),
                   max_shared=max(Counter(quotes).values()))
    old_records = {r['record_id']: r for name in baseline['archived_files']
                   if name.startswith('records/') for r in archived(name)}
    current_ids = {r['record_id'] for r in current}
    removed = sorted(set(old_records) - current_ids)
    added = sorted(current_ids - set(old_records))
    payment_changes, unexpected = [], []
    for row in current:
        old = deepcopy(old_records[row['record_id']])
        new = deepcopy(row)
        for record in (old, new):
            record.pop('excerpts')
            record['locator'].pop('pdf_pages')
        if row['record_type'] == 'obligation':
            before, after = old['detail'].pop('payment_status'), new['detail'].pop('payment_status')
            if before != after:
                payment_changes.append(dict(record_id=row['record_id'], before=before, after=after))
        if old != new:
            unexpected.append(row['record_id'])
    old_map_path = run / baseline['field_map_archive']
    old_map = {(f['table'], f['choice_field']): f for f in json.loads(old_map_path.read_text())['fields']}
    new_map = json.loads((HERE / 'choice_fields/ctr-a-share.json').read_text())
    reclassified = []
    for field in new_map['fields']:
        previous = old_map[(field['table'], field['choice_field'])]
        if previous['category'] != field['category']:
            reclassified.append(dict(table=field['table'], field=field['choice_field'],
                label=field['choice_label'], before=previous['category'], after=field['category'],
                basis=field['classification_basis'], formula=field['derived_formula']))

    def statuses(value, key):
        return {(table['table'], table.get('report_date', table.get('reported_date')), check['field']): check['status']
                for table in value[key] for check in table['checks']}

    value_differences = {}
    for name, value, key in (('choice-a-share-audit.json', audit, 'reports'),
                              ('choice-hk-interface-audit.json', hk, 'tables')):
        before, after = statuses(archived(name), key), statuses(value, key)
        value_differences[name] = [dict(table=k[0], period=k[1], field=k[2],
                                       before=before.get(k), after=after.get(k))
                                  for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
    fetch_files = {str(p.relative_to(run)): sha256(p) for p in sorted((run / 'fetch').rglob('*')) if p.is_file()}
    fetch_changed = sorted(k for k in set(fetch_files) | set(baseline['fetch_files'])
                           if fetch_files.get(k) != baseline['fetch_files'].get(k))
    captions = [item for item in fit['items'] if 'caption_review' in item['manual_review']]
    checks = dict(excerpt_before=baseline['excerpt_metrics'], excerpt_after=metrics,
                  field_categories_before=baseline['field_categories'], field_categories_after=audit['field_categories'],
                  reclassified_fields=reclassified, inventory_before=baseline['inventory_items'],
                  inventory_after=len(fit['items']), added_items=len(captions),
                  added_decisions=dict(Counter(x['manual_review']['caption_review']['kind'] for x in captions)),
                  payment_changes=payment_changes, records_removed=removed, records_added=added,
                  unexpected_record_changes=unexpected, fetch_changed_files=fetch_changed,
                  value_status_differences=value_differences,
                  unchanged_record_scope='除excerpts、对应locator.pdf_pages及列明的payment_status外，全部字段与旧记录逐项相等')
    checks['passed'] = (not unexpected and not added and removed == ['S46-exchange-disclaimer']
                        and not fetch_changed and not metrics['over_limit'] and not metrics['page_80_percent']
                        and all(x['after'] == 'not_applicable' for x in payment_changes)
                        and not fit['manual_review']['counts'].get('unreviewed', 0))
    snapshot(run, 'REWORK-CHECKS.json')
    dump(run / 'REWORK-CHECKS.json', checks)
    if not checks['passed']:
        raise ValueError('S1 rework comparison failed; see REWORK-CHECKS.json')
    lines = ['## S1 返工', '',
             '范围：A与B1–B5；沿用s1-20261021。原产物按内容哈希保存在history/；新旧拒收诊断均追加到sample-rejections/。本轮没有模型/SDK调用，fetch/全部文件哈希不变，取数预算不变。', '',
             '摘录按每条记录中的每段计数，含复用；字符数去除全部空白。80%分母为对应冻结页去空白后的全文；共用数按来源、页码、逐字片段三项完全相同统计，含表头。', '',
             '| 指标 | 返工前 | 返工后 |', '|---|---:|---:|']
    for label, key in (('记录数', 'records'), ('摘录段数', 'excerpts'), ('长度中位数', 'median'),
                       ('最大长度', 'maximum'), (f'超过{MAX_EXCERPT_CHARS}字符的段数', 'over_limit'),
                       ('占所在页80%以上的段数', 'page_80_percent'), ('同一段最多共用记录数（含表头）', 'max_shared')):
        lines.append(f"| {label} | {baseline['excerpt_metrics'][key]} | {metrics[key]} |")
    lines += ['', 'S34每项主要会计数据取一行，列头及单位另段；折行科目取完整逻辑行。S46通用交易所责任声明省略，page-review.json保留省略原因及空record_ids，其他记录不增不删。', '',
              '| 缺陷 | 修改及测试 | 对S1结果的影响 |', '|---|---|---|',
              '| B1 数字拼接 | normalize_whitespace在两个数字字符之间保留一个分隔符；两侧同规则。test_layout_whitespace_never_joins_adjacent_numbers覆盖50 50/5050、百分比、小数及千分位；保留汉字排版容忍 | 既有样例未发现伪拼数字；重切后全部通过 |',
              f'| B2 字段子串分类 | 按完整行科目清理后相等匹配；两行科目可隔一行纯数值、总跨度至多三行，分组/空页边界不跨越。test_row_labels_require_complete_name_and_bounded_wrapping及build_field_map子串反例 | {len(reclassified)}字段改判，详见下表；查值与分类分开 |',
              '| B3 负号 | 两处识别-、−、－和空白，及半角/全角括号。两个test_spaced_and_fullwidth_negatives / test_spaced_and_fullwidth_negative_tokens检验负数命中、正数不命中 | 长飞444值仍435查到、9查不到，逐字段状态变化0；港股逐字段状态变化' + str(len(value_differences['choice-hk-interface-audit.json'])) + ' |',
              '| B4 说明段漏盘点 | 引表段作为独立条目，也向后续表行传递上下文。test_caption_with_independent_fact_is_inventoried和test_counts_rows_not_cells_and_inherits_caption | 2073→2123；新增50条逐段裁决，未审0，明细见下文 |',
              '| B5 开产义务误记已付 | payment_status新增not_applicable；test_obligation_unknown_trigger_and_deadline_dates_stay_explicit验证枚举及未知日期。样例逐项比较检查其他字段未改 | 六条非付款义务改为not_applicable；已付保证金记录保留 |',
              '| A 摘录过长 | MAX_EXCERPT_CHARS=500；test_excerpt_limit_and_short_separate_spans覆盖长段/整页拒收、短句分段、表头和行分段；whole_page及page-only配方拒收 | 500上限保持，重切后最大' + str(metrics['maximum']) + '；无超限、无80%整页式摘录 |', '',
              '| 字段类别 | 返工前 | 返工后 |', '|---|---:|---:|']
    for category in ('原表科目', 'Choice 派生', '待定'):
        lines.append(f"| {category} | {baseline['field_categories'].get(category, 0)} | {audit['field_categories'].get(category, 0)} |")
    lines += ['', '| 改判字段 | 原类别→本轮类别 | 依据 |', '|---|---|---|']
    for field in reclassified:
        basis = field['basis'] + (' 公式：' + field['formula'] if field['formula'] else '')
        lines.append(f"| {field['table']}.{field['field']} | {field['before']}→{field['after']} | {basis} |")
    lines += ['', f'DEFERTAX的三个捕获期均等于DEFERTAXASSETREDUCE；DEFERTAXLIABADD为空。只用该观察和原表缺独立合计行作为派生依据，不将空分项当零补入公式核算。其余{len(reclassified) - 1}项没有派生公式依据，改为待定。', '',
              f"P1盘点：{baseline['inventory_items']}→{len(fit['items'])}；新增50段中45段为表格说明、5段兼有独立事实；全部逐段裁决，未审0。表格说明的单位、币种、期间、范围、限制等具体字段以及对应表行条目编号，见[新增条目裁决](rework-added-items.json)、[完整覆盖表](COVERAGE.md)和manual-review.json的caption_review。旧条目按笔记、物理行号、原文完全相等继承审阅，并显式重绑新分组指纹，保留previous_group。", '',
              '| 新增独立事实所在笔记行 | 裁决 |', '|---|---|']
    for item in captions:
        if item['manual_review']['caption_review']['kind'] != '表格说明':
            lines.append(f"| {item['note']}:{item['line']} | {item['manual_review']['reason']} |")
    lines += ['', '付款状态变更：', '']
    lines += [f"- {x['record_id']}：{x['before']}→{x['after']}。" for x in payment_changes]
    lines += ['', '逐项对比确认：除摘录、对应定位页和上述付款状态外，其他记录字段均未改；未新增记录，仅删除指定的S46-exchange-disclaimer。本轮未发现可确定且需要另行修正的其他字段错误；此结论不替代全量语义核验。', '',
              '全部样例、测试和INPUT-INTEGRITY.json通过；本轮额外检查见[REWORK-CHECKS.json](REWORK-CHECKS.json)。程序检查、字段适配审阅与研究/用户接受仍分别记录；停在S1，等待用户审阅。', '']
    return lines


def cleanup_section(run, fit, audit, hk, samples, integrity, receipt):
    """Keep the bounded cleanup comparison separate from the earlier rework."""
    baseline_path = run / 'cleanup-baseline.json'
    if not baseline_path.exists():
        return []
    import hashlib
    baseline = json.loads(baseline_path.read_text())

    def archived(name):
        path = Path(name)
        digest = baseline['archived_files'][name]
        saved = run / 'history' / path.stem / (digest + path.suffix)
        if sha256(saved) != digest:
            raise ValueError(f'Cleanup baseline archive changed: {name}')
        return json.loads(saved.read_text())

    old_map_path = run / baseline['field_map_archive']
    if sha256(old_map_path) != old_map_path.stem:
        raise ValueError('Cleanup field map archive changed')
    old_map = {(f['table'], f['choice_field']): f for f in json.loads(old_map_path.read_text())['fields']}
    new_map = json.loads((HERE / 'choice_fields/ctr-a-share.json').read_text())
    reclassified = [dict(table=f['table'], field=f['choice_field'],
                         before=old_map[(f['table'], f['choice_field'])]['category'], after=f['category'])
                    for f in new_map['fields']
                    if f['category'] != old_map[(f['table'], f['choice_field'])]['category']]
    aliases = {('BalanceStatementSHSZ', 'SUMSHEQUITY'),
               ('IncomeStatementSHSZ', 'MINORITYOTHERCINCOME')}
    alias_values = [dict(table=t['table'], period=t['report_date'], **c)
                    for t in audit['reports'] for c in t['checks'] if (t['table'], c['field']) in aliases]

    def statuses(value, key):
        return {(t['table'], t.get('report_date', t.get('reported_date')), c['field']): c['status']
                for t in value[key] for c in t['checks']}

    status_changes = {}
    for name, value, key in (('choice-a-share-audit.json', audit, 'reports'),
                              ('choice-hk-interface-audit.json', hk, 'tables')):
        before, after = statuses(archived(name), key), statuses(value, key)
        status_changes[name] = [dict(table=k[0], period=k[1], field=k[2],
                                     before=before.get(k), after=after.get(k))
                                for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
    records = [r for p in sorted((run / 'records').glob('*.json')) for r in json.loads(p.read_text())]
    record_hashes = {r['record_id']: hashlib.sha256(json.dumps(r, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                     for r in records}
    record_changes = sorted(k for k in set(record_hashes) | set(baseline['record_sha256'])
                            if record_hashes.get(k) != baseline['record_sha256'].get(k))
    fetch_files = {str(p.relative_to(run)): sha256(p) for p in sorted((run / 'fetch').rglob('*')) if p.is_file()}
    fetch_changes = sorted(k for k in set(fetch_files) | set(baseline['fetch_files'])
                           if fetch_files.get(k) != baseline['fetch_files'].get(k))
    review = json.loads((run / 'caption-cleanup-review.json').read_text())
    summary = review['summary']
    before = {tuple(d['groups']): d for d in archived('manual-review.json')['decisions']}
    manual = json.loads((run / 'manual-review.json').read_text())
    changed_groups = [g for d in manual['decisions'] if d != before.get(tuple(d['groups'])) for g in d['groups']]
    checks = dict(field_categories_before=baseline['field_categories'], field_categories_after=audit['field_categories'],
                  reclassified_fields=reclassified, alias_values=alias_values, value_status_differences=status_changes,
                  changed_manual_groups=changed_groups, captions_reviewed=summary['reviewed'],
                  record_changes=record_changes, fetch_changed_files=fetch_changes,
                  recipe_unchanged=sha256(run / 'sample-recipe.json') == baseline['recipe_sha256'],
                  review_bound_to_manual=review['manual_review_after_sha256'] == sha256(run / 'manual-review.json'),
                  samples=samples['records'], sample_errors=samples['errors'],
                  top_level_diagnostic_absent=not (run / 'sample-validation-errors.json').exists(),
                  input_integrity_passed=integrity['passed'], tests_returncode=receipt['returncode'])
    checks['passed'] = (len(reclassified) == 2 and {(x['table'], x['field']) for x in reclassified} == aliases
                        and all(x['before'] == '待定' and x['after'] == '原表科目' for x in reclassified)
                        and not record_changes and not fetch_changes and checks['recipe_unchanged']
                        and checks['review_bound_to_manual'] and summary['reviewed'] == 50
                        and set(changed_groups) == {x['group'] for x in summary['changes']}
                        and all(not changes for changes in status_changes.values())
                        and samples['records'] == 123 and samples['errors'] == 0
                        and checks['top_level_diagnostic_absent'] and integrity['passed'] and receipt['returncode'] == 0)
    snapshot(run, 'CLEANUP-CHECKS.json')
    dump(run / 'CLEANUP-CHECKS.json', checks)
    if not checks['passed']:
        raise ValueError('S1 cleanup comparison failed; see CLEANUP-CHECKS.json')
    lines = ['## S1 收尾', '',
             '基于未提交的S1返工继续；本节以cleanup-baseline.json的222/5/122为收尾前基线。此前返工的累计对比仍在上一节，旧报告与产物按内容哈希留在history/。停在S1，不进入S2。', '',
             '| 步骤 | 修改 | 验证 |', '|---|---|---|',
             '| 1 跨行最长匹配 | 至多两段名称、三个物理行；只有前一名称行无数值才继续，取最长候选，不吞独立完整行 | 三项合成测试：test_wrapped_label_keeps_only_longest_candidate、test_name_line_with_values_cannot_join_next_name、test_longest_wrapped_label_allows_intervening_value_line |',
             '| 2 两个别名 | 总权益及少数股东其他综合收益完整行名；各附依据注释 | 下表六项均用check_values核对；2026H1权益依据合并第38页，不取母公司第40页 |',
             '| 3 重建字段表/离线核对 | 重新生成ctr-a-share.json，执行README的A股与港股两条命令 | A股435/444；港股逐字段查值状态变化0；仅两个别名字段改判 |',
             '| 4 表格说明 | G571补全39行，G576按行区分%与x；另查其余48条 | 50条逐段核对关联范围及单位，已改与未改逐条列在下文；重跑s1_review.py后2123条、未审0 |',
             '| 5 拒收历史 | 从sample-rejections诊断及失败尝试时间生成报告；成功后同哈希去重归档并移走顶层错误文件 | SampleRejectionTests覆盖失败→成功、保留时间/去重、动态最近拒收/重复失败及当前仍失败时不称通过 |', '',
             '| 字段类别 | 收尾前 | 收尾后 |', '|---|---:|---:|']
    for category in ('原表科目', 'Choice 派生', '待定'):
        lines.append(f"| {category} | {baseline['field_categories'].get(category, 0)} | {audit['field_categories'].get(category, 0)} |")
    lines += ['', '改判清单：', '']
    lines += [f"- {f['table']}.{f['field']}：{f['before']}→{f['after']}（新增显式别名）；其余字段无改判。" for f in reclassified]
    lines += ['', '| 别名字段 | 期间 | Choice值 | 查值结果 |', '|---|---|---:|---|']
    for value in alias_values:
        lines.append(f"| {value['table']}.{value['field']} | {value['period']} | {value['value']:,.0f} | {value['status']} |")
    lines += ['', 'SUMSHEQUITY：2026H1合并权益20,380,126,306＝归母16,364,360,875＋少数股东4,015,765,431；“或股东权益”是同义名称。MINORITYOTHERCINCOME：第42页−13,734,262对应少数股东其他综合收益税后净额，折行间有纯数值行。全文查值与上述行名认定分开；命中不证明逐值列定位。', '',
              f"表格说明复核{summary['reviewed']}条：关联范围修正{summary['association_corrections']}处、单位说明修正{summary['unit_corrections']}处；以下为全部{len(summary['changes'])}条改动，其余48条除列明问题外未发现可确定漏行。逐条关联行、前后理由及冻结页证据见[caption-cleanup-review.json](caption-cleanup-review.json)。", '',
              '| 裁决 | 已改内容 |', '|---|---|']
    lines += [f"| {change['group']} | {change['after_reason'].replace('|', '/')}" +
              (f"关联行{change['before_count']}→{change['after_count']}；{change['change']}" if change['added_item_ids'] else '') + ' |'
              for change in summary['changes']]
    lines += ['', '拿不准、保留原裁决的项目：', '']
    lines += [f"- {item['group']}：{item['issue']} {item['action']}" for item in summary['uncertain']]
    lines += ['', rejection_history(run, samples), '',
              '123条样例逐项与收尾前完全相等，sample-recipe.json与fetch/全部14个文件哈希不变；指定不修的S46-further-disclosure摘录保持原样。两个设计、P1目录和p1-sample标签通过INPUT-INTEGRITY.json；全部单元测试通过。补充核对见[CLEANUP-CHECKS.json](CLEANUP-CHECKS.json)。', '']
    return lines


def report(run):
    interfaces = interface_audit(run)
    audit = json.loads((run / 'choice-a-share-audit.json').read_text())
    fit = json.loads((run / 'p1-field-fit.json').read_text())
    manual = fit.get('manual_review', {})
    fit_counts = fit['counts']
    samples = json.loads((run / 'sample-validation.json').read_text())
    integrity = verify(run)
    receipt = json.loads((run / 'TESTS.json').read_text())
    log = (run / 'TESTS.txt').read_text()
    import re
    test_count = re.search(r'Ran (\d+) tests?', log)
    hk_path = run / 'choice-hk-interface-audit.json'
    hk = json.loads(hk_path.read_text()) if hk_path.exists() else {}
    hk_counts = hk.get('counts', {})
    fx_checks = interfaces['fx_checks']
    members = next((row.get('codes', []) for row in fx_checks
                    if row['query']['function'] == 'sector' and row['query']['args'][0] == '114001'), [])
    quotes = [quote for row in fx_checks for quote in row['quotes']]
    quote_identity = [{'code': row['code'], 'name': row['name'], 'basis': row['basis']}
                      for row in quotes[:1]]
    snapshot(run, 'REPORT.md')
    snapshot(run, 'ARTIFACTS.json')
    lines = ['# S1 交付记录', '',
             'S1 格式、离线验证及两项接口测试已完成；汇率测试未取得报价数值，继续用Wind。停在 S1，不进入 S2。没有模型抽取、全量抽取、commit 或 push。' if interfaces['status'] == 'complete' else 'S1仍有未完成接口验证，见下文状态；不进入S2。', '',
             '## 格式和 P1 覆盖', '',
             f'- 八份笔记分母：{len(fit["items"])} 条（段落/列表项/表数据行，不是每数值一条）。完整映射、审阅范围及限制见 [COVERAGE.md](COVERAGE.md) 与 [p1-field-fit.json](p1-field-fit.json)。',
             f'- 格式结论：{"可容纳P1已抽内容" if manual.get("conclusion") == "format_fits" else "人工字段适配审阅尚待完成"}。直接适配{fit_counts.get("direct", 0)}条，补格式后适配{fit_counts.get("revised", 0)}条，非L1说明{fit_counts.get("non_l1", 0)}条；未审{manual.get("counts", {}).get("unreviewed", 0)}条、仍放不下{manual.get("counts", {}).get("unfit", 0)}条。该判断由本轮agent逐组作出，绑定笔记与分组指纹，供用户复核，不等于用户接受。',
             f'- 已绑定冻结页的实际样例：{samples["records"]} 条；分布 {json.dumps(samples["by_source"], ensure_ascii=False)}；摘录/数值/字段错误 {samples["errors"]}。见 [sample-validation.json](sample-validation.json) 和 [records/](records/)。',
             '- 格式扩展：跨页摘录、共用结构化数字、金额组成/可加性、未知日期说明、合并范围及维度、空白/NA/NM、转引共同出处和市场标记。多期间和同比拆成独立查询键；实绩、机构预测及不同陈述方不自动互相覆盖。',
             rejection_history(run, samples),
             '- S48 的吨口径与 S50 吨/年更新保留旧记录及覆盖关系；其他难点的实际范围以 COVERAGE 为准。', '',
             '## 长飞 Choice 离线核对', '',
             f'- 三期 {audit["integer_values"]} 个整数值：查到 {audit["value_checks"].get("查到", 0)}，查不到 {audit["value_checks"].get("查不到", 0)}，与设计计数一致。9个未查到值落在 ACCOUNTBILLREC、ACCOUNTBILLPAY、OTHERPAY、OTHERCINCOME。',
             '- 18次查询全部 ReportType=1，18个返回均为合并报表；6期 FIRSTNOTICEDATE 与清单中对应定期报告披露日全部一致。',
             f'- 349个表字段分类：{json.dumps(audit["field_categories"], ensure_ascii=False)}。待定项不会自动当原表科目接入；元数据包含在待定计数中。',
             '- 查值函数按证券代码、FIRSTNOTICEDATE和报告期绑定报告及哈希，返回查到/查不到/未查值；不输出逐值页/行/列定位。全文同值不证明科目及期间正确。',
             '- 设计措辞需保留边界：OTHERCINCOME/OTHERPAY的原表披露情况随期间不同；派生字段按字段级保守排除。不能把435个全文命中说成435项逐科目抄录全部正确。详见 [choice-a-share-audit.md](choice-a-share-audit.md)。', '',
             '## 港股接口和汇率', '',
             '- 首次c.start()返回10002002，原失败返回、原清单行和日志前缀保持不变。用户2026-10-22明确放开网络并授权继续后，程序追加绑定确切停止事件的network_resume_authorized；没有删除失败、没有重置预算。原失败阶段报告已按内容哈希存入history/。',
             f'- 本轮累计数据调用{interfaces["data_calls_used"]}/8，剩余{interfaces["remaining_calls"]}。其中港股三表3次、FX{interfaces["fx_calls_used"]}/3次；剩余纠参预算不再使用。每批均登录并finally退出，未force-login或activate。',
             f'- 港股三表全部成功。字段数：{json.dumps(hk_counts.get("returned_fields_by_table", {}), ensure_ascii=False)}。披露日期字段：{json.dumps(hk.get("disclosures", []), ensure_ascii=False)}；返回报表类型：{json.dumps(hk.get("report_types", []), ensure_ascii=False)}。没有FIRSTNOTICEDATE。文档参数ReportType支持1/2/3/4区分原始/调整、合并/母公司，本轮只调用1，未额外调用2。',
             f'- 港股{hk_counts.get("financial_fields", 0)}个财务字段中，{hk_counts.get("returned_zero_fields", 0)}个返回零，不能据零值命中认定披露；{hk_counts.get("nonzero_numeric_fields", 0)}个非零值中，有符号查到{hk_counts.get("nonzero_statuses", {}).get("查到", 0)}个、查不到{hk_counts.get("nonzero_statuses", {}).get("查不到", 0)}个，其中{hk_counts.get("magnitude_only_matches", 0)}个仅绝对值相同。类别：{json.dumps(hk_counts.get("categories", {}), ensure_ascii=False)}。金额元转千元、每股元转人民币分后调用通用查值函数。全文命中不证明科目语义。详见 [港股接口审计](choice-hk-interface-audit.md)。没有建港股字段表、转正式L1或改为直接用Choice。',
             f'- FX第1次：人民币即期板块114002001返回无数据；第2次：人民币中间价板块114001取得{len(members)}个代码；第3次：使用真实返回币对{json.dumps(quote_identity, ensure_ascii=False)}查询2026-10-12至15的CLOSE,SNAME，返回{len(quotes)}个日期，数字报价{interfaces["fx_numeric_quotes"]}条。名称有值而CLOSE全为空。ErrorCode=0和名称返回不等于取得报价，后验解释另追加留痕，原始回执不改。',
             '- 结论：FX尚无可用报价；中间价与即期也不互换，港币汇率继续用Wind。历史空返回的后台原因仍未知，不能认定无权限。详见 [接口及逐次结果](interface-tests.json)。', '',
             '## 进入 S2 前的决定和未决事项', '',
             '1. invest-sources写入规则：建议允许经批准的独立取数执行器作为追加写入者，沿用Source ID、完整返回、哈希和单写锁；研究agent仍只提请求。具体建议在 [S1_NOTES.md](../../S1_NOTES.md)，本轮未改共享库。',
             '2. 港股口径：完整接口已测通，但仍有供应商计算、科目重组、符号差异及大量零值；沿用原文抽取，不因接口成功改成直接采用Choice。FX暂继续Wind，本轮不扩大取数预算。',
             f'3. 字段待定：{audit["field_categories"].get("待定", 0)}个待定字段（含21个元数据）本轮保守排除。原表/派生判断的期间差异是否要在S2前改成按期间分类，仍待决定。',
             '4. Schema对codex exec --output-schema的接受情况仍留到S2首次调用；run.py单轮抽取未实现。S2亦不做逐值定位。', '',
             '## 验证与受保护输入', '',
             f'- 全部单元测试：{test_count.group(1) if test_count else "见日志"}项，退出码{receipt["returncode"]}，含P1原有测试。原始日志 [TESTS.txt](TESTS.txt)，回执 [TESTS.json](TESTS.json)。',
             f'- 受保护输入校验：{integrity["p1_files"]}个P1文件无变更，两个设计文件无变更，p1-sample标签对象无变更；详见 [INPUT-INTEGRITY.json](INPUT-INTEGRITY.json)。',
             '- 原文、研报摘录、Choice返回及样例仅在被忽略的runs下；公开测试仅合成文字。所有重复生成操作由仓库程序执行，run内无一次性Python脚本。程序检查通过不等于研究或用户接受。交付文件指纹见 [ARTIFACTS.json](ARTIFACTS.json)。', '',
             '## 复现入口', '',
             '用法见 [README.md](../../README.md)。格式/Schema、清单、字段表、取数器及离线审阅程序在仓库内；数据配方sample-recipe.json和逐条人工审阅资料留在本run。重建不调用模型，也不会自动重复Choice请求。', '']
    lines += rework_section(run, fit, audit, hk)
    lines += cleanup_section(run, fit, audit, hk, samples, integrity, receipt)
    (run / 'REPORT.md').write_text('\n'.join(lines), encoding='utf-8')
    public = ['records.py', 'RECORDS.md', 'schemas/extract-output.json', 'prompts/extract-records.md',
              'fetch.py', 'choice_audit.py', 's1_review.py', 's1.py', 'S1_NOTES.md', 'README.md',
              'test_records.py', 'test_fetch.py', 'test_choice_audit.py', 'test_s1_review.py', 'test_s1_interfaces.py']
    public += [str(p.relative_to(HERE)) for folder in ('checklists', 'choice_fields') for p in sorted((HERE / folder).glob('*.json'))]
    artifacts = {'created_at': now(), 'public_files': {name: sha256(HERE / name) for name in public},
                 'run_files': {str(p.relative_to(run)): sha256(p) for p in sorted(run.rglob('*'))
                               if p.is_file() and p.name not in ('ARTIFACTS.json', 'operations.jsonl', '.writer.lock')}}
    dump(run / 'ARTIFACTS.json', artifacts)
    append(run / 'operations.jsonl', {'at': now(), 'action': 'report', 'program_sha256': sha256(Path(__file__)),
                                     'report_sha256': sha256(run / 'REPORT.md'), 'interfaces_sha256': sha256(run / 'interface-tests.json')})
    return {'report': str(run / 'REPORT.md'), 'interface_status': interfaces['status'], 'tests_returncode': receipt['returncode']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify', 'tests', 'report', 'fx-spot-request', 'fx-midpoint-request', 'fx-quote-request'))
    parser.add_argument('--run-dir', type=Path, default=HERE / 'runs/s1-20261021')
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if not run.is_relative_to((HERE / 'runs').resolve()) or not run.name.startswith('s1-'):
        parser.error('Use a new runs/s1-* directory')
    run.mkdir(parents=True, exist_ok=True)
    result = {'prepare': baseline, 'verify': verify, 'tests': tests, 'report': report,
              'fx-spot-request': fx_members_request,
              'fx-midpoint-request': lambda r: fx_members_request(r, midpoint=True),
              'fx-quote-request': fx_quote_request}[args.action](run)
    print(json.dumps(result, ensure_ascii=False))
    return int(result.get('returncode', 0) != 0 or result.get('passed') is False)


if __name__ == '__main__':
    raise SystemExit(main())
