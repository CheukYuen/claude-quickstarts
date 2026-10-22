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
        if row['sha256'] not in prior_audits:
            append(run / 'fetch/calls.jsonl', {'event': 'interface_interpretation', 'at': now(),
                                             'capture_sha256': row['sha256'], 'source_id': row['source_id'],
                                             'outcome': row['outcome'], 'reason': row['reason'],
                                             'numeric_quote_count': row['numeric_quote_count']})
    return value


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
             '- 首轮句尾小数标点及万股单位拒收已修正，过程记录在review-operations.jsonl；早期诊断快照被后次诊断覆盖。现存最后一次拒收是“签约日另发公告”误记0天，已改为未知数量而保留同日动作。该诊断已按哈希归档至sample-rejections/，以后每次拒收追加独立快照。',
             '- S48 的吨口径与 S50 吨/年更新保留旧记录及覆盖关系；其他难点的实际范围以 COVERAGE 为准。', '',
             '## 长飞 Choice 离线核对', '',
             f'- 三期 {audit["integer_values"]} 个整数值：查到 {audit["value_checks"].get("查到", 0)}，查不到 {audit["value_checks"].get("查不到", 0)}，与设计计数一致。9个未查到值落在 ACCOUNTBILLREC、ACCOUNTBILLPAY、OTHERPAY、OTHERCINCOME。',
             '- 18次查询全部 ReportType=1，18个返回均为合并报表；6期 FIRSTNOTICEDATE 与清单中对应定期报告披露日全部一致。',
             f'- 349个表字段分类：{json.dumps(audit["field_categories"], ensure_ascii=False)}。待定项不会自动当原表科目接入；元数据包含在待定计数中。',
             '- 查值函数按证券代码、FIRSTNOTICEDATE和报告期绑定报告及哈希，返回查到/查不到/未查值；不输出逐值页/行/列定位。全文同值不证明科目及期间正确。',
             '- 设计措辞需保留边界：OTHERCINCOME/OTHERPAY的原表披露情况随期间不同，4字段在本轮按字段级保守排除。不能把435个全文命中说成435项逐科目抄录全部正确。详见 [choice-a-share-audit.md](choice-a-share-audit.md)。', '',
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
             '3. 字段待定：115个待定字段（含21个元数据）是否先保留排除、还是追加人工确认；本轮保守排除。原表/派生判断的期间差异是否要在S2前改成按期间分类。',
             '4. Schema对codex exec --output-schema的接受情况仍留到S2首次调用；run.py单轮抽取未实现。S2亦不做逐值定位。', '',
             '## 验证与受保护输入', '',
             f'- 全部单元测试：{test_count.group(1) if test_count else "见日志"}项，退出码{receipt["returncode"]}，含P1原有测试。原始日志 [TESTS.txt](TESTS.txt)，回执 [TESTS.json](TESTS.json)。',
             f'- 受保护输入校验：{integrity["p1_files"]}个P1文件无变更，两个设计文件无变更，p1-sample标签对象无变更；详见 [INPUT-INTEGRITY.json](INPUT-INTEGRITY.json)。',
             '- 原文、研报摘录、Choice返回及样例仅在被忽略的runs下；公开测试仅合成文字。所有重复生成操作由仓库程序执行，run内无一次性Python脚本。程序检查通过不等于研究或用户接受。交付文件指纹见 [ARTIFACTS.json](ARTIFACTS.json)。', '',
             '## 复现入口', '',
             '用法见 [README.md](../../README.md)。格式/Schema、清单、字段表、取数器及离线审阅程序在仓库内；数据配方sample-recipe.json和逐条人工审阅资料留在本run。重建不调用模型，也不会自动重复Choice请求。', '']
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
