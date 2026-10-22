#!/usr/bin/env python3
"""S2 read-only conversion of bound Choice original consolidated statements."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import choice_audit
import corpus
import records

DEFAULT_PERIODS = ('2026-06-30', '2025-12-31', '2024-12-31',
                   '2023-12-31', '2022-12-31', '2021-12-31')
DEFAULT_SUBJECT = '长飞光纤光缆股份有限公司'


def period_definition(table, report_date):
    """Canonical labels: balance-sheet date, annual YYYY, half-year YYYYH1."""
    if table == 'BalanceStatementSHSZ':
        return {'kind': 'instant', 'label': report_date}
    year, suffix = report_date[:4], report_date[5:]
    if suffix == '12-31':
        return {'kind': 'annual', 'label': year}
    if suffix == '06-30':
        return {'kind': 'half_year', 'label': year + 'H1'}
    if suffix in ('03-31', '09-30'):
        return {'kind': 'quarter', 'label': year + ('Q1' if suffix == '03-31' else 'Q3')}
    raise ValueError(f'Unsupported financial report period: {report_date}')


def convert_payload(payload, binding, field_map, source_root=corpus.SOURCE_ROOT,
                    subject=DEFAULT_SUBJECT, index=None):
    """Convert only original fields; every emitted value runs check_values.

    The map classification is field-level for every selected period. No numeric
    equality or historical period is allowed to promote a derived/undecided field.
    """
    definitions = {(f['table'], f['choice_field']): f for f in field_map['fields']}
    if len(definitions) != len(field_map['fields']):
        raise ValueError('Duplicate Choice field definition')
    categories = Counter(f['category'] for f in field_map['fields'])
    result, audits, nulls, invalid, skipped = [], [], [], [], Counter()
    seen_queries = set()
    index = index if index is not None else choice_audit.manifest_index(Path(source_root))
    for selected in binding['queries']:
        query = payload['queries'][selected['index']]
        definition = query['query']
        table = definition['args'][0]
        options = choice_audit.options_dict(definition['args'][2])
        if options.get('reporttype') != '1':
            raise ValueError('STOP: only ReportType=1 may be converted')
        if table != selected['table']:
            raise ValueError('STOP: table disagrees with vendor binding')
        if options.get('secucode', '').upper() != binding['company_code'].upper():
            raise ValueError('STOP: company disagrees with vendor binding')
        rows = choice_audit.ctr_rows(query)
        if len(rows) != 1:
            raise ValueError('STOP: expected one company/period CTR row')
        for row in rows:
            report_date, notice = row['REPORTDATE'], row['FIRSTNOTICEDATE']
            if date.fromisoformat(notice).isoformat() != notice or notice > corpus.CUTOFF:
                raise ValueError('STOP: invalid FIRSTNOTICEDATE or disclosure after cutoff')
            if row.get('reportType') != '合并报表':
                raise ValueError('STOP: returned report type is not original consolidated')
            if report_date != selected['report_period'] or options.get('reportdate') != report_date:
                raise ValueError('STOP: report date disagrees with vendor binding')
            identity = table, report_date
            if identity in seen_queries:
                raise ValueError('Duplicate table/period in Choice capture')
            seen_queries.add(identity)
            numeric = {}
            for (field_table, field), mapping in definitions.items():
                if field_table != table:
                    continue
                if field not in row:
                    raise ValueError(f'Documented Choice field missing: {table}.{field}')
                if mapping['category'] != '原表科目':
                    skipped[mapping['category']] += 1
                    continue
                raw = row[field]
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    nulls.append({'table': table, 'field': field, 'report_period': report_date})
                    continue
                if choice_audit.numeric_value(raw) is None:
                    invalid.append({'table': table, 'field': field, 'report_period': report_date,
                                    'value': raw, 'reason': 'nonempty original field is not a finite number'})
                    continue
                numeric[field] = raw
            audit = choice_audit.check_values(options['secucode'], notice, numeric,
                        source_root=source_root, index=index, report_date=report_date,
                        categories={field: '原表科目' for field in numeric})
            audit['table'] = table
            audits.append(audit)
            checks = {item['field']: item for item in audit['checks']}
            for field, raw in numeric.items():
                mapping = definitions[(table, field)]
                rule = mapping['unit_rule']
                period = period_definition(table, report_date)
                value = choice_audit.numeric_value(raw) * Decimal(rule['multiplier_to_record'])
                unit = rule['choice_unit']
                if not mapping['original_table_item'] or not unit:
                    raise ValueError(f'Original Choice field lacks name or unit: {table}.{field}')
                check = checks[field]
                record_id = f'CHOICE.{table}.{report_date}.{field}'
                check['record_id'] = record_id
                record = records.make_record('metric', record_id, binding['source_id'], subject,
                    mapping['original_table_item'], period['label'], notice, report_date, [],
                    {'scope': 'consolidated', 'scope_note': 'ReportType=1（合并报表，当期最初公布）',
                     'value_status': 'reported', 'value_note': ''},
                    source={'kind': 'choice', 'source_id': binding['source_id'],
                            'choice_field': table + '.' + field, 'report_period': report_date},
                    period=period,
                    extraction_version=records.VERSION + '/s2-choice-1',
                    verification={'status': check['verification_status'], 'note': check['reason']},
                    numbers=[records.number(format(value, 'f'), unit, rule['currency'])],
                    limitations=['Choice原表科目直接采用；全文查值只报警，不证明科目行或期间列定位。'])
                errors = records.validate_record(record)
                if errors:
                    raise ValueError(f'Invalid converted Choice record {record_id}: {errors}')
                result.append(record)
    summary = {'field_categories': dict(categories), 'selected_table_periods': len(seen_queries),
               'selected_periods': sorted({period for _, period in seen_queries}),
               'field_period_opportunities': sum(1 for table, period in seen_queries
                   for field_table, field in definitions if table == field_table
                   and definitions[(field_table, field)]['category'] == '原表科目'),
               'records': len(result), 'empty_original_values': len(nulls),
               'invalid_original_values': len(invalid), 'skipped_field_periods': dict(skipped),
               'value_checks': dict(Counter(item['status'] for audit in audits for item in audit['checks'])),
               'verification_statuses': dict(Counter(r['verification']['status'] for r in result)),
               'no_api_calls': True, 'classification_basis': 'field-level mapping, unchanged across periods'}
    return {'records': result, 'value_checks': audits, 'summary': summary,
            'empty_values': nulls, 'invalid_values': invalid}


def convert_choice(run_dir, source_root=corpus.SOURCE_ROOT,
                   field_map_path=corpus.HERE / 'choice_fields/ctr-a-share.json',
                   subject=DEFAULT_SUBJECT, report_periods=DEFAULT_PERIODS):
    run_dir = Path(run_dir)
    binding = corpus.prepare_vendor(run_dir, source_root=Path(source_root),
                                    report_periods=list(report_periods))
    payload = json.loads(Path(binding['vendor_path']).read_text(encoding='utf-8'))
    field_map = json.loads(Path(field_map_path).read_text(encoding='utf-8'))
    expected_categories = {'原表科目': 224, 'Choice 派生': 5, '待定': 120}
    actual_categories = dict(Counter(f['category'] for f in field_map['fields']))
    if actual_categories != expected_categories:
        raise ValueError(f'S2 frozen field-category counts changed: {actual_categories}')
    expected_queries = {(table, period) for table in choice_audit.TABLE_DOCS for period in report_periods}
    if {(q['table'], q['report_period']) for q in binding['queries']} != expected_queries or len(binding['queries']) != len(expected_queries):
        raise ValueError('S2 requires all three original tables for every selected period')
    result = convert_payload(payload, binding, field_map, Path(source_root), subject)
    result['summary']['field_map_sha256'] = corpus.sha256(Path(field_map_path))
    result['summary']['vendor_sha256'] = binding['vendor_sha256']
    for name, value in (('records', result['records']), ('value-checks', result['value_checks']),
                        ('summary', result['summary']), ('empty-values', result['empty_values']),
                        ('invalid-values', result['invalid_values'])):
        corpus.dump(run_dir / 'choice' / (name + '.json'), value)
    result['binding'] = binding
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--source-root', type=Path, default=corpus.SOURCE_ROOT)
    parser.add_argument('--field-map', type=Path, default=corpus.HERE / 'choice_fields/ctr-a-share.json')
    args = parser.parse_args()
    print(json.dumps(convert_choice(args.run_dir, args.source_root, args.field_map)['summary'],
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
