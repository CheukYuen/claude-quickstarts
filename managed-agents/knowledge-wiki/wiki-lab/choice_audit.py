#!/usr/bin/env python3
"""Offline Choice field mapping and report-wide value alarms (no SDK calls).

Vendor documentation stays in invest-lab. This module reads it when rebuilding
the map; the public map contains field names, our mappings and decisions only.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

import corpus

HERE = Path(__file__).resolve().parent
DEFAULT_VENDOR = 'vendor/choice/choice-20261004-yofc-financials-market.json'
DEFAULT_DOCS = Path('/Users/leon/Stock/invest-lab/.agents/skills/choice-quantapi-skill/references/indicators/ctr')
TABLE_DOCS = {
    'BalanceStatementSHSZ': '00010002000100010001.md',
    'IncomeStatementSHSZ': '00010002000100010002.md',
    'CashFlowStatementSHSZ': '00010002000100010003.md',
}
HK_TABLE_DOCS = {
    'BalanceStatementHK': '0002000100010001.md',
    'IncomeStatementHK': '0002000100010002.md',
    'CashFlowStatementHK': '0002000100010003.md',
}
HK_METADATA = {'REPORTDATE', 'STARTDATE', 'ENDDATE', 'REPORTTYPE', 'BBNJR',
               'IPO', 'COMPANYTYPE', 'CURTYPE', 'NOTICEDATE',
               'ACCOUNTINGSTANDARDS', 'AUDITOPINION', 'AUDITOR'}
METADATA = {'REPORTDATE', 'STR_ISLISTINGREPORT', 'reportType', 'STR_COMBINETYPE',
            'FIRSTNOTICEDATE', 'REPORTSOURCETYPE', 'OPINIONTYPE'}
DERIVED = {
    ('BalanceStatementSHSZ', 'ACCOUNTBILLREC'): ('BILLREC + ACCOUNTREC', ['BILLREC', 'ACCOUNTREC']),
    ('BalanceStatementSHSZ', 'ACCOUNTBILLPAY'): ('BILLPAY + ACCOUNTPAY', ['BILLPAY', 'ACCOUNTPAY']),
    ('BalanceStatementSHSZ', 'OTHERPAY'): ('TOTAL_OTHER_PAYABLE - INTERESTPAY - DIVIDENDPAY',
                                         ['TOTAL_OTHER_PAYABLE', 'INTERESTPAY', 'DIVIDENDPAY']),
    ('IncomeStatementSHSZ', 'OTHERCINCOME'): ('PARENTOTHERCINCOME + MINORITYOTHERCINCOME',
                                           ['PARENTOTHERCINCOME', 'MINORITYOTHERCINCOME']),
}
# Only naming equivalents confirmed by reading the report's table headings.
# No numeric page/row/period-column location is emitted or used for acceptance.
LABEL_ALIASES = {
    ('BalanceStatementSHSZ', 'TOTAL_OTHER_RECE'): '其他应收款',
    ('BalanceStatementSHSZ', 'TOTAL_OTHER_PAYABLE'): '其他应付款',
    ('BalanceStatementSHSZ', 'SUMPARENTEQUITY'): '归属于母公司所有者权益（或股东权益）合计',
    ('BalanceStatementSHSZ', 'SUMLIABSHEQUITY'): '负债和所有者权益（或股东权益）总计',
    ('IncomeStatementSHSZ', 'OPERATETAX'): '税金及附加',
    ('IncomeStatementSHSZ', 'PARENTOTHERCINCOME'): '归属母公司所有者的其他综合收益的税后净额',
    ('CashFlowStatementSHSZ', 'EMPLOYEEPAY'): '支付给职工及为职工支付的现金',
    ('CashFlowStatementSHSZ', 'DEC_XJJXJDJWJZJ'): '现金及现金等价物净增加额',
}
PERIOD_LIMITS = {
    ('BalanceStatementSHSZ', 'OTHERPAY'): '2026H1附注存在分项；2024返回值为其他应付款合计扣股利的重组。字段级保守排除，不把空值当零参与计算。',
    ('IncomeStatementSHSZ', 'OTHERCINCOME'): '2026H1原表列示合计；2025、2024原表合计行未填值，Choice合计可由归母和少数股东分项相加。字段级保守排除。',
}


def unpack(value):
    """Decode the existing choice_capture.pack representation, without rewriting it."""
    if isinstance(value, dict):
        if value.get('__type__') in ('list', 'tuple'):
            return [unpack(v) for v in value['items']]
        return {k: unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unpack(v) for v in value]
    return value


def options_dict(options: str) -> dict:
    return {key.strip().lower(): value.strip() for key, value in
            (part.split('=', 1) for part in options.split(',') if '=' in part)}


def ctr_rows(query_result: dict) -> list:
    result = unpack(query_result['result'])
    if result.get('ErrorCode') != 0:
        raise ValueError('Choice return is not successful')
    indicators = result.get('Indicators', [])
    rows = []
    for values in result.get('Data', {}).values():
        if len(values) != len(indicators):
            raise ValueError('Choice indicator/data length mismatch')
        rows.append(dict(zip(indicators, values)))
    return rows


def normal_code(code: str) -> str:
    code = code.upper()
    if code.endswith('.HK'):
        return str(int(code[:-3])) + '.HK'
    return code


def manifest_index(source_root: Path) -> dict:
    grouped = corpus.read_manifest(source_root / 'manifests/sources.jsonl')
    return {sid: corpus.resolve_records(rows) for sid, rows in grouped.items()}


def find_report(code: str, first_notice_date: str, index: dict,
                report_date: str | None = None) -> dict | None:
    """Select by security + disclosure date, and optionally financial period.

    Ambiguity is an error, never last-row-wins. A different disclosure date cannot
    silently substitute a later report containing comparatives.
    """
    try:
        date.fromisoformat(first_notice_date)
    except (ValueError, TypeError):
        return None
    candidates = []
    for binding in index.values():
        row = binding['record']
        if (row.get('company_code') and normal_code(row['company_code']) == normal_code(code)
                and row.get('published_date') == first_notice_date
                and row.get('doc_type') in ('annual', 'interim', 'quarterly')):
            if report_date and row.get('period'):
                period = str(row['period']).lower()
                suffix = {'03': 'q1', '06': 'h1', '09': 'q3', '12': ''}.get(report_date[5:7])
                if suffix is None:
                    continue
                expected = report_date[:4] + suffix
                if period != expected.lower():
                    continue
            candidates.append(binding)
    if len(candidates) > 1:
        raise ValueError(f'Ambiguous report: {code} {first_notice_date}')
    return candidates[0] if candidates else None


def load_report_text(binding: dict | None, source_root: Path) -> tuple:
    if binding is None:
        return None, {'reason': '证券代码与FIRSTNOTICEDATE未找到对应定期报告'}
    row = binding['record']
    source = corpus.source_path(row['path'], source_root)
    if corpus.sha256(source) != row['sha256']:
        raise ValueError(f'Report hash mismatch: {row["source_id"]}')
    evidence = {'source_id': row['source_id'], 'sha256': row['sha256'],
                'manifest_version': binding['version'],
                'manifest_line': binding['line'],
                'manifest_normalized_sha256': binding['normalized_sha256']}
    if not row.get('text_dir'):
        return None, dict(evidence, reason='对应报告没有逐页文字')
    directory = corpus.source_path(row['text_dir'], source_root)
    paths = sorted(directory.glob('p[0-9][0-9][0-9][0-9].txt'))
    if not paths or (row.get('pages') and len(paths) != row['pages']):
        return None, dict(evidence, reason='对应报告逐页文字缺页，未作全报告查值')
    page_hashes = {p.name: corpus.sha256(p) for p in paths}
    evidence['page_text_sha256'] = page_hashes
    return '\n'.join(p.read_text(encoding='utf-8') for p in paths), evidence


def numeric_value(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def integer_text_index(text: str) -> set:
    """Read complete numeric tokens, preserving sign and parentheses negatives.

    Not substring matching: 123 cannot match 1234 or -123. Commas and decimal
    zeroes are presentation, so 1,234.00 matches 1234. No unit conversion here:
    the A-share audit compares Choice yuan against report yuan verbatim values.
    """
    found = set()
    pattern = r'(?<![\d.,])(?:\(\s*|（\s*)?[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*[)）])?(?!\d|[.,]\d)'
    for match in re.finditer(pattern, text):
        token = re.sub(r'\s+', '', match.group()).replace(',', '').replace('−', '-')
        if token.startswith(('(', '（')) and token.endswith((')', '）')):
            token = '-' + token[1:-1]
        value = numeric_value(token)
        if value is not None:
            found.add(value)
    return found


def check_values(code: str, first_notice_date: str, values: dict,
                 source_root: Path = corpus.SOURCE_ROOT, index: dict | None = None,
                 report_date: str | None = None, categories: dict | None = None,
                 text_override: str | None = None) -> dict:
    """Reusable report-wide alarm; values are field -> numeric value.

    Every field returns 查到/查不到/未查值. Choice derived or undecided fields are
    excluded from factual acceptance regardless of textual matches. This returns
    no page/row/column locations and is not a semantic or source-line check.
    text_override supports already-bound HK experiments, not automatic fallback.
    """
    source_root = Path(source_root)
    if text_override is None:
        index = index if index is not None else manifest_index(source_root)
        binding = find_report(code, first_notice_date, index, report_date)
        text, evidence = load_report_text(binding, source_root)
    else:
        text, evidence = text_override, {'reason': '调用方显式提供已绑定的报告全文'}
    tokens = integer_text_index(text) if text is not None else set()
    checks = []
    for field, value in values.items():
        number = numeric_value(value)
        category = (categories or {}).get(field, '原表科目')
        status = ('未查值' if text is None or number is None else
                  '查到' if number in tokens else '查不到')
        verification = ('待复核' if status == '查不到' else
                        '未查值' if status == '未查值' else '代码通过')
        checks.append({'field': field, 'value': value, 'status': status,
                       'category': category, 'verification_status': verification,
                       'direct_use_allowed': category == '原表科目',
                       'reason': ('空值或非数值不当作零' if number is None else
                                  evidence.get('reason') if text is None else
                                  '报告全文数值检索；不证明科目或期间列一致')})
    return {'code': code, 'first_notice_date': first_notice_date,
            'report_date': report_date, 'report_binding': evidence, 'checks': checks}


def field_definitions(path: Path) -> dict:
    text = path.read_text(encoding='utf-8').split('## 报表字段说明', 1)[1]
    result = {}
    for line in text.splitlines():
        if line.startswith('|'):
            parts = [p.strip() for p in line.split('|')[1:-1]]
            if len(parts) >= 2 and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9+]*', parts[1]):
                result[parts[1]] = parts[0]
    return result


def clean_label(label: str) -> str:
    label = re.sub(r'\((?:元|新)\)|（(?:元|新)）', '', label)
    label = re.sub(r'^(?:[一二三四五六七八九十]+、|\([一二三四五六七八九十]+\)|其中[:：]|减[:：]|加[:：])', '', label)
    return label


def label_key(text: str) -> str:
    return re.sub(r'[\s:：,，、()（）]', '', text)


def build_field_map(payload: dict, docs_root: Path, report_text: str) -> dict:
    """Match labels as well as values; unseen or ambiguous definitions stay pending."""
    # Table extraction can place a value between two lines of a wrapped label.
    # Remove numeric tokens for this heading-only check; no cell locator results.
    text_key = label_key(re.sub(r'[+\-−]?\d[\d,.]*', '', report_text))
    fields = []
    seen = set()
    for query in payload['queries']:
        if query['query']['function'] != 'ctr':
            continue
        table = query['query']['args'][0]
        if table not in TABLE_DOCS or table in seen:
            continue
        seen.add(table)
        definitions = field_definitions(docs_root / TABLE_DOCS[table])
        indicators = unpack(query['result']['Indicators'])
        if set(definitions) - set(indicators):
            raise ValueError(f'Documented fields missing from return: {table}')
        for field in indicators:
            documented = field in definitions
            label = definitions.get(field, field)
            name = clean_label(label)
            derived = DERIVED.get((table, field))
            alias = LABEL_ALIASES.get((table, field))
            matched_label = (name if label_key(name) in text_key else
                             alias if alias and label_key(alias) in text_key else None)
            if not documented:
                category, reason = '待定', '返回字段未见于对应文档；保留原样字段名（含尾空白），不猜测定义和单位'
            elif field in METADATA:
                category, reason = '待定', '返回元数据；不生成财务指标记录'
            elif derived:
                category, reason = 'Choice 派生', '字段说明结合报表科目及分项算式核对；非因查不到值而判派生'
            elif '平衡项目' in label or '其他项目' in label:
                category, reason = '待定', '文档仅列字段名，未说明平衡/其他项目计算；暂不直接采用'
            elif matched_label:
                category, reason = '原表科目', '字段说明的科目名在长飞报告中存在；查值独立进行'
            else:
                category, reason = '待定', '字段说明有名称，但未确认对应长飞原表科目名；不得仅因值相等判原表'
            is_eps = field in ('BASICEPS', 'DILUTEDEPS')
            fields.append({'table': table, 'choice_field': field,
                           'choice_label': label, 'original_table_item': matched_label,
                           'candidate_original_item': name, 'category': category,
                           'role': 'metadata' if field in METADATA else 'metric',
                           'record_metric': table + '.' + field,
                           'unit_rule': {'choice_unit': None if field in METADATA or not documented else '元/股' if is_eps else '元',
                                         'currency': None if field in METADATA or not documented else 'CNY',
                                         'multiplier_to_record': '1',
                                         'null_means': '未返回，不补零'},
                           'classification_basis': reason,
                           'derived_formula': derived[0] if derived else None,
                           'derived_inputs': derived[1] if derived else [],
                           'period_limits': PERIOD_LIMITS.get((table, field)),
                           'documentation_path': str(docs_root / TABLE_DOCS[table])})
    return {'format_version': 'choice-fields/1', 'scope': '长飞一般企业A股ctr；不推广至港股或金融企业',
            'classification_limits': '原表科目名称匹配不作逐值定位；待定字段须人工确认后再使用。元数据不属于原表财务科目。',
            'fields': fields}


def verify_disclosures(payload: dict, index: dict) -> list:
    results = []
    for query in payload['queries']:
        q = query['query']
        if q['function'] != 'ctr' or q['args'][0] not in TABLE_DOCS:
            continue
        options = options_dict(q['args'][2])
        if options.get('reporttype') != '1':
            raise ValueError('STOP: an A-share query does not use ReportType=1')
        for row in ctr_rows(query):
            if row.get('REPORTDATE') != options.get('reportdate'):
                raise ValueError('STOP: requested and returned report dates differ')
            if row.get('reportType') != '合并报表':
                raise ValueError('STOP: returned report type is not 合并报表')
            report = find_report(options['secucode'], row['FIRSTNOTICEDATE'], index, options['reportdate'])
            if report is None:
                raise ValueError('STOP: FIRSTNOTICEDATE does not match a periodic-report disclosure')
            results.append({'table': q['args'][0], 'report_date': row['REPORTDATE'],
                            'first_notice_date': row['FIRSTNOTICEDATE'],
                            'requested_report_type': options['reporttype'],
                            'returned_report_type': row['reportType'],
                            'report_source_id': report['record']['source_id'],
                            'manifest_published_date': report['record']['published_date'],
                            'status': '一致'})
    return results


def reconcile_derived_fields(table: str, row: dict) -> list:
    """Retain the arithmetic evidence without interpreting absent inputs as zero."""
    checks = []
    for (definition_table, field), (formula, inputs) in DERIVED.items():
        if table != definition_table:
            continue
        components = {name: row.get(name) for name in inputs}
        numbers = [numeric_value(components[name]) for name in inputs]
        if any(number is None for number in numbers):
            computed = None
            status = '未核算：有空分项，不补零'
        else:
            computed = (numbers[0] - sum(numbers[1:]) if field == 'OTHERPAY' else sum(numbers))
            status = '相等' if computed == numeric_value(row.get(field)) else '不相等'
        checks.append({'field': field, 'formula': formula, 'inputs': components,
                       'returned_value': row.get(field),
                       'computed_value': str(computed) if computed is not None else None,
                       'status': status, 'period_limits': PERIOD_LIMITS.get((table, field))})
    return checks


def run_audit(source_root: Path, vendor_relative: str, docs_root: Path,
              run_dir: Path, field_map_path: Path) -> dict:
    index = manifest_index(source_root)
    path = corpus.source_path(vendor_relative, source_root)
    payload = json.loads(path.read_text(encoding='utf-8'))
    source_id = path.stem
    vendor_binding = index[source_id]
    if corpus.sha256(path) != vendor_binding['record']['sha256']:
        raise ValueError('Vendor file hash mismatch')
    disclosures = verify_disclosures(payload, index)
    report_cache = {}
    for info in disclosures:
        if info['report_date'][:4] not in ('2026', '2025', '2024'):
            continue
        sid = info['report_source_id']
        if sid not in report_cache:
            report_cache[sid] = load_report_text(index[sid], source_root)
    field_map = build_field_map(payload, docs_root, '\n'.join(t or '' for t, _ in report_cache.values()))
    corpus.dump(field_map_path, field_map)
    categories = {(f['table'], f['choice_field']): f['category'] for f in field_map['fields']}
    reports = []
    for query in payload['queries']:
        q = query['query']
        if q['function'] != 'ctr' or q['args'][0] not in TABLE_DOCS:
            continue
        table = q['args'][0]
        options = options_dict(q['args'][2])
        if options['reportdate'][:4] not in ('2026', '2025', '2024'):
            continue
        for row in ctr_rows(query):
            values = {k: v for k, v in row.items()
                      if numeric_value(v) is not None and numeric_value(v) == numeric_value(v).to_integral_value()}
            report = check_values(options['secucode'], row['FIRSTNOTICEDATE'], values,
                                  source_root, index, options['reportdate'],
                                  {k: categories[(table, k)] for k in values})
            report['table'] = table
            report['derived_reconciliations'] = reconcile_derived_fields(table, row)
            reports.append(report)
    all_checks = [c for r in reports for c in r['checks']]
    counts = dict(Counter(c['status'] for c in all_checks))
    missing = [dict(table=r['table'], report_date=r['report_date'], **c)
               for r in reports for c in r['checks'] if c['status'] == '查不到']
    result = {'input_source_id': source_id, 'input_sha256': corpus.sha256(path),
              'manifest_line': vendor_binding['line'], 'disclosures': disclosures,
              'field_counts_by_table': dict(Counter(f['table'] for f in field_map['fields'])),
              'field_categories': dict(Counter(f['category'] for f in field_map['fields'])),
              'integer_values': len(all_checks), 'value_checks': counts,
              'missing_values': missing, 'reports': reports,
              'expected_comparison': {'values': 444, 'found': 435, 'missing': 9},
              'matches_design_counts': len(all_checks) == 444 and counts.get('查到') == 435 and len(missing) == 9,
              'limitations': ['查值只看整份报告是否有同值，不证明科目行、期间列或抄录语义正确。',
                              '查不到值不自动判派生；字段分类同时读文档并保留待定。']}
    corpus.dump(run_dir / 'choice-a-share-audit.json', result)
    lines = ['# 长飞 Choice 字段及查值核对', '',
             f'- 输入：{source_id}，SHA-256 `{result["input_sha256"]}`。',
             f'- 18 次 ctr 查询均 ReportType=1，18 个返回均合并报表；6 期 FIRSTNOTICEDATE 与清单披露日全部一致。',
             f'- 2026H1、2025、2024：{len(all_checks)} 个整数值，查到 {counts.get("查到", 0)}，查不到 {len(missing)}；与设计计数{"一致" if result["matches_design_counts"] else "不一致"}。',
             f'- 字段数：{json.dumps(result["field_counts_by_table"], ensure_ascii=False)}。',
             f'- 分类：{json.dumps(result["field_categories"], ensure_ascii=False)}。', '',
             '|报表|报告期|字段|数值|分类|', '|---|---|---|---:|---|']
    lines += [f'|{m["table"]}|{m["report_date"]}|{m["field"]}|{m["value"]}|{m["category"]}|' for m in missing]
    lines += ['', '完整逐字段结果和冻结文字哈希见 choice-a-share-audit.json。',
              '查到仅表示同值在对应报告全文出现；不表示已逐值定位或独立研究验收。',
              '字段说明只提供名称而没有计算定义的字段保留待定，不因缺值或同值擅自定性。',
              '设计表述的边界：435个查到值中也有被保守排除的派生/重组字段，不能说435个全是原表科目或已证明逐项抄录正确。',
              'OTHERCINCOME在2026H1有原表合计，2025和2024合计行空白而分项可相加；OTHERPAY在2026H1附注存在分项，2024返回值为合计扣股利的重组。字段级仍保守排除，未将空值当零计算。',
              '资产负债表返回原样字段AGENCYASSETS（末尾空格）未出现在对应一般企业文档中，保留原字段并标待定。']
    (run_dir / 'choice-a-share-audit.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def hk_document_contract(docs_root: Path) -> dict:
    """Read documentation in place; do not copy vendor documentation to the repo."""
    contracts = {}
    for table, filename in HK_TABLE_DOCS.items():
        path = docs_root / filename
        text = path.read_text(encoding='utf-8')
        definitions = field_definitions(path)
        contracts[table] = {
            'path': str(path), 'sha256': corpus.sha256(path),
            'notice_fields': [field for field in ('FIRSTNOTICEDATE', 'NOTICEDATE') if field in definitions],
            'report_type_options': {'1': '合并报表', '2': '合并报表(调整)',
                                    '3': '母公司报表', '4': '母公司报表(调整)'},
            'report_type_documented': all(term in text for term in
                                          ('ReportType', '1 合并报表', '2 合并报表(调整)',
                                           '3 母公司报表', '4 母公司报表(调整)')),
            'definitions': definitions,
        }
    return contracts


def hk_scaled_value(value, choice_label: str, report_amount_divisor='1000',
                    report_eps_multiplier='100') -> dict:
    """Scale documented yuan amounts to report thousands; EPS yuan to cents.

    No guessed conversion for undocumented fields. Raw amounts and scaling are
    both retained in the ignored run report, never rewritten in the SDK capture.
    """
    number = numeric_value(value)
    if number is None:
        return {'value': None, 'source_unit': None, 'report_unit': None, 'multiplier': None}
    if '每股' in choice_label:
        multiplier = Decimal(str(report_eps_multiplier))
        source_unit, report_unit = '元/股', '分/股'
    elif '(元)' in choice_label or '（元）' in choice_label:
        multiplier = Decimal(1) / Decimal(str(report_amount_divisor))
        source_unit, report_unit = '元', '千元'
    else:
        return {'value': None, 'source_unit': None, 'report_unit': None, 'multiplier': None}
    return {'value': format(number * multiplier, 'f'), 'source_unit': source_unit,
            'report_unit': report_unit, 'multiplier': format(multiplier, 'f')}


def hk_field_category(field: str, label: str | None, decision: dict | None = None) -> tuple:
    """Same-value matches never promote a field into an original report subject.

    Optional decisions are report-specific review notes under runs/, not a new HK
    field mapping. Explicit calculated labels override an optimistic decision.
    """
    if field in HK_METADATA:
        return '元数据', '返回元数据，不作为财务科目查值'
    if label is None:
        return '待定', '返回字段未见于对应HK说明页，未猜测定义'
    if '计算' in label:
        return 'Choice 派生', '接口字段说明明确标为计算；即使同值也不升级为原表科目'
    if decision:
        category = decision.get('category')
        if category not in ('原表科目', 'Choice 派生', '待定') or not decision.get('basis'):
            raise ValueError('HK report-specific decision requires category and basis')
        return category, decision['basis']
    return '待定', '文档仅列科目名；尚未人工确认与本份英文年报的口径关系'


def review_hk_capture(capture_path: Path, source_root: Path, report_source_id: str,
                      docs_root: Path, run_dir: Path, decisions_path: Path | None = None,
                      report_amount_divisor='1000', report_eps_multiplier='100') -> dict:
    """Offline S1 HK interface experiment. No SDK, record conversion or HK map.

    Bind the exact report explicitly: NOTICEDATE may identify an earlier results
    announcement, so it must not silently select a substitute for the requested
    annual report. check_values is reused with that bound report's frozen text.
    """
    capture_path, source_root, run_dir = Path(capture_path), Path(source_root), Path(run_dir)
    if run_dir.resolve().parent != (HERE / 'runs').resolve() or not run_dir.name.startswith('s1-'):
        raise ValueError('HK experiment outputs must stay under runs/s1-*')
    if decisions_path is not None:
        decisions_path = Path(decisions_path)
        if run_dir.resolve() not in decisions_path.resolve().parents:
            raise ValueError('HK report-specific decisions must stay inside this S1 run')
    decisions = json.loads(decisions_path.read_text(encoding='utf-8')) if decisions_path else {}
    index = manifest_index(source_root)
    binding = index[report_source_id]
    report = binding['record']
    if not normal_code(report['company_code']).endswith('.HK'):
        raise ValueError('HK experiment requires a Hong Kong filing')
    text, report_binding = load_report_text(binding, source_root)
    if text is None:
        raise ValueError('HK experiment requires complete frozen report text')
    if Decimal(str(report_amount_divisor)) <= 0 or Decimal(str(report_eps_multiplier)) <= 0:
        raise ValueError('Unit multipliers must be positive')
    contracts = hk_document_contract(docs_root)
    payload = json.loads(capture_path.read_text(encoding='utf-8'))
    capture_sha256 = corpus.sha256(capture_path)
    capture_manifest_binding = None
    if capture_path.parent.name == 'choice' and capture_path.parent.parent.name == 'vendor':
        fetch_root = capture_path.parent.parent.parent
        capture_index = manifest_index(fetch_root)
        capture_row = capture_index[capture_path.stem]
        if capture_row['record']['sha256'] != capture_sha256 or corpus.source_path(
                capture_row['record']['path'], fetch_root) != capture_path.resolve():
            raise ValueError('HK capture does not match its append-only manifest registration')
        capture_manifest_binding = {key: capture_row[key] for key in
                                    ('version', 'line', 'normalized_sha256', 'raw_line_sha256')}
    tables, failures = [], []
    tokens = integer_text_index(text)
    for query in payload.get('queries', []):
        q = query['query']
        if q.get('function') != 'ctr' or q['args'][0] not in HK_TABLE_DOCS:
            continue
        table = q['args'][0]
        options = options_dict(q['args'][2])
        if normal_code(options['secucode']) != normal_code(report['company_code']):
            raise ValueError('HK query security does not match the bound annual report')
        if options.get('reporttype') != '1':
            raise ValueError('S1 HK experiment only requests original consolidated ReportType=1')
        if query['result'].get('ErrorCode') != 0:
            failures.append({'table': table, 'result': query['result']})
            continue
        rows = ctr_rows(query)
        if not rows:
            failures.append({'table': table, 'reason': '成功码但没有报表行'})
        for row in rows:
            definitions = contracts[table]['definitions']
            scaled, classifications = {}, {}
            for field, value in row.items():
                if field in HK_METADATA:
                    continue
                scale = hk_scaled_value(value, definitions.get(field, ''),
                                         report_amount_divisor, report_eps_multiplier)
                category, basis = hk_field_category(field, definitions.get(field),
                                                    decisions.get(table, {}).get(field))
                scaled[field] = scale
                classifications[field] = {'category': category, 'classification_basis': basis}
            alarm = check_values(report['company_code'], report['published_date'],
                                 {field: value['value'] for field, value in scaled.items()},
                                 source_root=source_root, report_date=options.get('reportdate'),
                                 categories={field: info['category'] for field, info in classifications.items()},
                                 text_override=text)
            checks = []
            for item in alarm['checks']:
                field = item['field']
                item.update(classifications[field], raw_value=row[field],
                            label=definitions.get(field), unit_conversion=scaled[field],
                            magnitude_match=(numeric_value(scaled[field]['value']) is not None and
                                             abs(numeric_value(scaled[field]['value'])) in {abs(v) for v in tokens}),
                            direct_use_allowed=False)
                checks.append(item)
            notices = {field: row.get(field) for field in ('FIRSTNOTICEDATE', 'NOTICEDATE') if field in row}
            tables.append({'table': table, 'requested_options': options, 'rows': len(rows),
                           'indicator_count': len(row), 'reported_date': row.get('REPORTDATE'),
                           'returned_report_type': row.get('REPORTTYPE'),
                           'currency': row.get('CURTYPE'), 'notice_fields': notices,
                           'annual_report_disclosed_on': report['published_date'],
                           'notice_matches_annual_report': {field: value == report['published_date']
                                                          for field, value in notices.items()},
                           'checks': checks,
                           'value_checks': dict(Counter(item['status'] for item in checks)),
                           'field_categories': dict(Counter(item['category'] for item in checks))})
    all_checks = [item for table in tables for item in table['checks']]
    nonzero_checks = [item for item in all_checks if numeric_value(item['raw_value']) not in (None, Decimal(0))]
    public_contracts = {table: {key: value for key, value in contract.items() if key != 'definitions'}
                        for table, contract in contracts.items()}
    result = {'experiment': 'S1 HK ctr original consolidated statements',
              'capture_path': str(capture_path), 'capture_sha256': capture_sha256,
              'capture_manifest_binding': capture_manifest_binding,
              'capture_login': payload.get('login'), 'captured_at': payload.get('captured_at'),
              'report_binding': report_binding, 'documentation': public_contracts,
              'decisions_path': str(decisions_path) if decisions_path else None,
              'decisions_sha256': corpus.sha256(decisions_path) if decisions_path else None,
              'program_sha256': corpus.sha256(Path(__file__)), 'tables': tables, 'failures': failures,
              'successful_tables': sorted({table['table'] for table in tables}),
              'all_three_tables_returned': set(HK_TABLE_DOCS) == {table['table'] for table in tables},
              'financial_fields': len(all_checks),
              'nonempty_numeric_fields': sum(item['unit_conversion']['value'] is not None for item in all_checks),
              'value_checks': dict(Counter(item['status'] for item in all_checks)),
              'field_categories': dict(Counter(item['category'] for item in all_checks)),
              'counts': {'successful_tables': len({table['table'] for table in tables}),
                         'returned_fields_by_table': {table['table']: table['indicator_count'] for table in tables},
                         'financial_fields': len(all_checks),
                         'nonempty_numeric_fields': sum(item['unit_conversion']['value'] is not None for item in all_checks),
                         'statuses': dict(Counter(item['status'] for item in all_checks)),
                         'categories': dict(Counter(item['category'] for item in all_checks)),
                         'magnitude_only_matches': sum(item['magnitude_match'] and item['status'] == '查不到' for item in all_checks),
                         'nonzero_numeric_fields': len(nonzero_checks),
                         'nonzero_statuses': dict(Counter(item['status'] for item in nonzero_checks)),
                         'returned_zero_fields': sum(numeric_value(item['raw_value']) == Decimal(0) for item in all_checks)},
              'disclosures': [{'table': table['table'], 'returned_notice_fields': table['notice_fields'],
                               'annual_report_disclosed_on': table['annual_report_disclosed_on'],
                               'notice_matches_annual_report': table['notice_matches_annual_report']} for table in tables],
              'report_types': [{'table': table['table'], 'requested': table['requested_options'].get('reporttype'),
                                'returned': table['returned_report_type']} for table in tables],
              'limits': ['金额按文档元转年报千元；每股值按元转年报分。不改写SDK返回。',
                         '查到只表示报告全文出现同值，不证明科目、符号、期间列正确；绝对值匹配另列，未改为查到。',
                         'NOTICEDATE不自动等同FIRSTNOTICEDATE，亦不自动等同年报PDF披露日。',
                         'SDK原样返回的零只计返回字段；全文零命中不证明该科目有披露或数值为零。未把空值转零。',
                         '仅记录HK接口实验，不建港股字段对照表，不转L1，不因此直接采用Choice港股财务值。']}
    run_dir.mkdir(parents=True, exist_ok=True)
    corpus.dump(run_dir / 'choice-hk-interface-audit.json', result)
    lines = ['# 俊知HK完整三表：S1接口后离线核对', '',
             f'- 捕获：`{capture_path.name}`；SHA-256 `{result["capture_sha256"]}`。',
             f'- 原表：`{report_source_id}`；金额单位为千元、基本每股收益为人民币分。',
             f'- 成功返回：{len(result["successful_tables"])} / 3 张。财务数值字段{result["financial_fields"]}个，非空可换算数值{result["nonempty_numeric_fields"]}个，其中SDK原样零值{result["counts"]["returned_zero_fields"]}个。',
             f'- 有符号查值：{json.dumps(result["value_checks"], ensure_ascii=False)}；分类：{json.dumps(result["field_categories"], ensure_ascii=False)}。',
             f'- 排除零值后：{result["counts"]["nonzero_numeric_fields"]}个非零值，{json.dumps(result["counts"]["nonzero_statuses"], ensure_ascii=False)}；查不到中{result["counts"]["magnitude_only_matches"]}个仅绝对值命中，不改为查到。',
             '- 三份接口说明均列NOTICEDATE；ReportType文档支持1合并原始、2合并调整、3母公司原始、4母公司调整。本轮只请求1，没有调用2作对照。', '',
             '|接口|字段数|公告日期字段及值|返回报表类型|同值查到/查不到/未查值|', '|---|---:|---|---|---|']
    for table in tables:
        lines.append(f'|{table["table"]}|{table["indicator_count"]}|{json.dumps(table["notice_fields"], ensure_ascii=False)}|{table["returned_report_type"]}|{json.dumps(table["value_checks"], ensure_ascii=False)}|')
    lines += ['', '## 文档明示计算及其他自定口径', '']
    for table in tables:
        for item in table['checks']:
            if item['category'] == 'Choice 派生':
                lines.append(f'- {table["table"]}.{item["field"]}（{item["label"]}）：{item["classification_basis"]}；查值{item["status"]}。')
    lines += ['', '逐字段数值、换算、分类依据、未查到和未查值清单见 choice-hk-interface-audit.json。']
    lines += ['', *result['limits']]
    (run_dir / 'choice-hk-interface-audit.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with (run_dir / 'choice-hk-operations.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'operation': 'offline_hk_review',
                                'at': datetime.now(timezone.utc).isoformat(),
                                'capture_path': str(capture_path), 'capture_sha256': result['capture_sha256'],
                                'program_sha256': result['program_sha256'],
                                'decisions_sha256': result['decisions_sha256'],
                                'output_sha256': corpus.sha256(run_dir / 'choice-hk-interface-audit.json')},
                               ensure_ascii=False) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=corpus.SOURCE_ROOT)
    parser.add_argument('--vendor', default=DEFAULT_VENDOR)
    parser.add_argument('--docs-root', type=Path, default=DEFAULT_DOCS)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--field-map', type=Path, default=HERE / 'choice_fields/ctr-a-share.json')
    parser.add_argument('--hk-capture', type=Path, help='Review a captured HK experiment offline instead of the A-share audit')
    parser.add_argument('--hk-report', default='1300.HK-20260422-annual-2025')
    parser.add_argument('--hk-decisions', type=Path, help='Report-specific review notes under this S1 run')
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.hk_capture:
        result = review_hk_capture(args.hk_capture, args.source_root, args.hk_report,
                                   args.docs_root, args.run_dir, args.hk_decisions)
        print(json.dumps({key: result[key] for key in ('successful_tables', 'financial_fields',
                         'nonempty_numeric_fields', 'value_checks', 'field_categories')}, ensure_ascii=False))
        return
    result = run_audit(args.source_root, args.vendor, args.docs_root, args.run_dir, args.field_map)
    print(json.dumps({k: result[k] for k in ('integer_values', 'value_checks', 'field_categories', 'matches_design_counts')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
