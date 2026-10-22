#!/usr/bin/env python3
"""S2 code-only consistency alarms. These are QA items, never research gaps."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import itertools
import json
from pathlib import Path

import corpus
import records as contract


def normalized_number(number):
    """Comparable value signature; scaling uses the record contract's unit table."""
    unit = number['unit']
    family, scale = contract.SCALES.get(unit, (unit, Decimal(1)))
    return number.get('currency'), family, Decimal(number['value']) * scale


def numeric_signature(record):
    return tuple(sorted(set(normalized_number(n) for n in record['numbers']), key=repr))


def _ids(values):
    return sorted({r['record_id'] for r in values})


def _precision(number):
    # decimals describes the source display. Convert its half-unit to record unit.
    source = number['source_unit']
    _, source_scale = contract.SCALES.get(source, (source, Decimal(1)))
    _, target_scale = contract.SCALES.get(number['unit'], (number['unit'], Decimal(1)))
    return Decimal('0.5') * Decimal(1).scaleb(-number['decimals']) * source_scale / target_scale


def _sum_compare(totals, components, category):
    """Compare exact Decimals; explicit approximation/rounding goes to own queue."""
    rows = []
    # Equivalent duplicates (e.g. a repeated note) must not be summed twice.
    unique = {}
    for record in components:
        identity = (contract.object_key(record), numeric_signature(record))
        unique.setdefault(identity, record)
    components = list(unique.values())
    for total in totals:
        participants = [total] + components
        if any(len(record['numbers']) != 1 for record in participants):
            rows.append({'kind': category, 'status': '无法比较', 'record_ids': _ids(participants),
                         'reason': '合计或分项含零个/多个数值，不能推断相加列'})
            continue
        if any(r['numbers'][0]['qualifier'] not in ('exact', 'approximate') for r in participants):
            rows.append({'kind': category, 'status': '无法比较', 'record_ids': _ids(participants),
                         'reason': '分项或合计为上下限，不能当作等式相加'})
            continue
        units = {(r['numbers'][0]['unit'], r['numbers'][0]['currency'], r['period']['label'])
                 for r in participants}
        if len(units) != 1:
            rows.append({'kind': category, 'status': '无法比较', 'record_ids': _ids(participants),
                         'reason': '单位、币种或期间不一致'})
            continue
        amount = Decimal(total['numbers'][0]['value'])
        subtotal = sum((Decimal(r['numbers'][0]['value']) for r in components), Decimal(0))
        difference = amount - subtotal
        rounding_bound = sum((_precision(r['numbers'][0]) for r in participants), Decimal(0))
        approximate = any(r['numbers'][0]['qualifier'] != 'exact' for r in participants)
        status = ('相等' if not difference else '约数或舍入差额' if approximate or abs(difference) <= rounding_bound
                  else '不相等')
        rows.append({'kind': category, 'status': status, 'record_ids': _ids(participants),
                     'total_record_id': total['record_id'], 'component_record_ids': _ids(components),
                     'total': str(amount), 'component_sum': str(subtotal),
                     'difference': str(difference), 'rounding_bound': str(rounding_bound),
                     'unit': total['numbers'][0]['unit'],
                     'reason': '仅比较同组已提供分项；不将未披露/未抽出的分项补零'})
    return rows


def additive_checks(records):
    groups = defaultdict(list)
    for record in records:
        group = record['additivity']['group']
        if group and record['additivity']['role'] in ('total', 'component'):
            groups[(record['source']['source_id'], record['disclosed_on'],
                    record['object_key']['subject'], record['period']['label'],
                    record['nature'], group)].append(record)
    result = []
    for values in groups.values():
        totals = [r for r in values if r['additivity']['role'] == 'total']
        components = [r for r in values if r['additivity']['role'] == 'component']
        # Groups mixing segments and consolidation are handled by the stricter
        # segment comparator below, which can refuse incompatible populations.
        if any(r['detail'].get('scope') == 'segment' for r in values):
            continue
        if totals and components:
            result.extend(_sum_compare(totals, components, 'additivity'))
    return result


_SEGMENT_DIMENSIONS = ('分部', '业务分部', '经营分部', 'segment')
_ELIMINATION = ('抵销', '抵消', 'elimination')


def _is_elimination(record):
    return any(any(token in (d['name'] + d['value']).lower() for token in _ELIMINATION)
               for d in record['dimensions'])


def _is_external(record):
    return any(any(token in (d['name'] + d['value']).lower() for token in ('对外', 'external'))
               for d in record['dimensions'])


def _comparable_dimensions(record):
    return tuple(sorted((d['name'], d['value']) for d in record['dimensions']
                        if d['name'].lower() not in _SEGMENT_DIMENSIONS
                        and not any(t in (d['name'] + d['value']).lower()
                                    for t in _ELIMINATION + ('对外', 'external'))))


def segment_checks(records):
    groups = defaultdict(list)
    for record in records:
        if record['record_type'] != 'metric' or not record['numbers']:
            continue
        if record['detail'].get('scope') not in ('segment', 'consolidated'):
            continue
        groups[(record['object_key']['subject'], record['object_key']['indicator'],
                record['period']['label'], record['disclosed_on'], record['nature'])].append(record)
    result = []
    for values in groups.values():
        segments = [r for r in values if r['detail']['scope'] == 'segment']
        if not segments:
            continue
        totals = [r for r in values if r['detail']['scope'] == 'consolidated'
                  and not _is_elimination(r) and r['additivity']['role'] != 'component']
        eliminations = [r for r in values if _is_elimination(r)]
        participants = segments + [r for r in eliminations if r not in segments]
        reason = None
        if not totals:
            reason = '未提供同主体、指标、期间、性质及披露日的合并数'
        elif len({_comparable_dimensions(r) for r in totals + participants}) > 1:
            reason = '分部与集团的其他口径维度不一致'
        elif len({r['additivity']['group'] for r in totals + participants}) != 1 or not totals[0]['additivity']['group']:
            reason = '缺少共同可加组，无法确认分部集合完整且不重复'
        elif any(r['additivity']['role'] != 'component' for r in participants):
            reason = '分部未明确标为同组分项'
        elif not eliminations and not all(_is_external(r) for r in segments):
            reason = '非全部对外口径，且未提供分部间抵销；不假设抵销为零'
        if reason:
            result.append({'kind': 'segment_group', 'status': '无法比较',
                           'record_ids': _ids(values), 'reason': reason})
        else:
            result.extend(_sum_compare(totals, participants, 'segment_group'))
    return result


def _key_difference(left, right):
    differences = []
    for field in ('subject', 'indicator', 'period'):
        if left['object_key'][field] != right['object_key'][field]:
            differences.append(field)
    for field in ('record_type', 'nature', 'dimensions'):
        if left[field] != right[field]:
            differences.append(field)
    if left['detail'].get('scope') != right['detail'].get('scope'):
        differences.append('scope')
    if not differences and contract.object_key(left) != contract.object_key(right):
        differences.append('attribution')
    return differences


def cross_document_diagnostics(records):
    """Explicit user-requested numeric targets, with strict object-key chains."""
    aliases = {
        '1300.HK-20260913-announcement-qinghai-possible-acquisition': 'S46',
        '1300.HK-20260920-announcement-qinghai-acquisition': 'S48',
        '1300.HK-20260925-announcement-qinghai-supplemental': 'S50',
    }
    def short(record):
        source_id = record['source']['source_id']
        return aliases.get(source_id, source_id)
    pool = [r for r in records if short(r) in ('S46', 'S48', 'S50')]
    targets = {
        'equity_consideration_455_million': [r for r in pool if any(
            normalized_number(n) == ('CNY', 'money', Decimal(455000000)) for n in r['numbers'])],
        'capacity_400_tonnes': [r for r in pool if any(
            Decimal(n['value']) == Decimal(400) and n['unit'] in ('吨', '吨/年') for n in r['numbers'])],
    }
    result = {}
    for target, values in targets.items():
        groups = defaultdict(list)
        for record in values:
            groups[contract.object_key(record)].append(record)
        expected = {'S46', 'S48', 'S50'} if target.startswith('equity') else {'S48', 'S50'}
        # A shared key alone is insufficient: unknown/equal disclosure dates do
        # not make a replacement chain under records.coverage_relations.
        def linked_sources(group):
            relations = contract.coverage_relations(group)
            connected = {r['record_id'] for r in relations} | {
                new for relation in relations for new in relation['covered_by']}
            return {short(r) for r in group if r['record_id'] in connected}
        result[target] = {'status': '连成覆盖链' if any(expected <= linked_sources(group) for group in groups.values()) else '未连成覆盖链',
            'record_ids': _ids(values), 'expected_sources': sorted(expected),
            'observed_sources': sorted({short(r) for r in values}),
            'target_records': [dict(record_id=r['record_id'], source=short(r), object_key=r['object_key'],
                record_type=r['record_type'], nature=r['nature'], scope=r['detail'].get('scope'),
                dimensions=r['dimensions'], numbers=r['numbers']) for r in values],
            'chains': [{'record_ids': _ids(group), 'relations': contract.coverage_relations(group)}
                       for group in groups.values()],
            'unaligned_pairs': [{'left': left['record_id'], 'right': right['record_id'],
                                  'different_fields': _key_difference(left, right),
                                  'left_key': left['object_key'], 'right_key': right['object_key']}
                                 for left, right in itertools.combinations(values, 2)
                                 if short(left) != short(right)
                                 and contract.object_key(left) != contract.object_key(right)]}
    return result


def choice_alignment(records, a01_record_ids):
    pdf = [r for r in records if r['source']['kind'] == 'pdf' and r['record_id'] in set(a01_record_ids)]
    choice = defaultdict(list)
    base_keys = defaultdict(list)
    for record in records:
        if record['source']['kind'] == 'choice':
            choice[contract.object_key(record)].append(record)
            base_keys[tuple(record['object_key'][key] for key in ('subject', 'indicator', 'period'))].append(record)
    rows = []
    for record in pdf:
        candidates = choice.get(contract.object_key(record), [])
        # A01 historical comparison data may be disclosed again in a later report.
        # Align the strict economic key, retaining both disclosure dates explicitly.
        if not candidates:
            same_base = base_keys.get(tuple(record['object_key'][key] for key in ('subject', 'indicator', 'period')), [])
            rows.append({'kind': 'choice_pdf_a01', 'status': '未对齐', 'record_ids': [record['record_id']],
                         'object_key': record['object_key'],
                         'base_key_candidates': [{'record_id': other['record_id'],
                             'different_fields': _key_difference(record, other)} for other in same_base],
                         'reason': ('主体、指标、期间三字段相同，但完整对象键的范围/维度等不同；仍不比数值'
                                    if same_base else '没有相同的主体、指标、期间三字段键；不按数值猜对应')})
            continue
        same_date = [r for r in candidates if r['disclosed_on'] == record['disclosed_on']]
        for other in same_date or candidates:
            rows.append({'kind': 'choice_pdf_a01',
                         'status': '相等' if numeric_signature(record) == numeric_signature(other) else '不相等',
                         'record_ids': [record['record_id'], other['record_id']],
                         'disclosed_on': [record['disclosed_on'], other['disclosed_on']],
                         'object_key': record['object_key'],
                         'pdf_numbers': record['numbers'], 'choice_numbers': other['numbers']})
    return rows


def build_qa(records, a01_record_ids=None):
    """Return detailed checks and an actionable QA queue, without mutating records."""
    groups = defaultdict(list)
    for record in records:
        groups[(contract.object_key(record), record['disclosed_on'])].append(record)
    conflicts = []
    for values in groups.values():
        for left, right in itertools.combinations(values, 2):
            if left['numbers'] and right['numbers'] and numeric_signature(left) != numeric_signature(right):
                conflicts.append({'kind': 'same_key_conflict', 'status': '数值冲突',
                    'record_ids': [left['record_id'], right['record_id']],
                    'object_key': left['object_key'], 'disclosed_on': left['disclosed_on'],
                    'numbers': [left['numbers'], right['numbers']]})
    sums = additive_checks(records)
    segments = segment_checks(records)
    alignment = choice_alignment(records, a01_record_ids or [])
    cross = cross_document_diagnostics(records)
    alarms = [{'kind': 'choice_value_lookup', 'status': '待复核', 'record_ids': [r['record_id']],
               'reason': r['verification']['note']} for r in records
              if r['source']['kind'] == 'choice' and r['verification']['status'] == '待复核']
    queue = conflicts + alarms + [row for row in sums + segments + alignment if row['status'] != '相等']
    for target, diagnostic in cross.items():
        if diagnostic['status'] != '连成覆盖链':
            queue.append({'kind': 'cross_document_coverage', 'target': target,
                          'status': diagnostic['status'], 'record_ids': diagnostic['record_ids'],
                          'reason': '见cross_document_diagnostics中的键差异和缺少来源'})
    for item in queue:
        identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        item['qa_id'] = 'QA-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        item['queue_type'] = '质检问题，不是研究缺口'
    return {'schema': 'wiki-lab-s2-qa/1', 'queue': queue,
            'same_key_conflicts': conflicts, 'additivity': sums, 'segment_group': segments,
            'choice_pdf_a01': alignment, 'coverage_relations': contract.coverage_relations(records),
            'cross_document_diagnostics': cross,
            'summary': {'records': len(records), 'queue_items': len(queue),
                        'by_kind': dict(Counter(row['kind'] for row in queue)),
                        'same_key_conflicts': len(conflicts),
                        'choice_pdf_a01': dict(Counter(row['status'] for row in alignment)),
                        'a01_unaligned_key_fields': dict(Counter(field for row in alignment
                            if row['status'] == '未对齐' for field in {field for candidate in row.get('base_key_candidates', [])
                                for field in candidate['different_fields']})),
                        'a01_input_status': 'provided' if a01_record_ids is not None else 'not supplied; alignment not run',
                        'additivity': dict(Counter(row['status'] for row in sums)),
                        'segment_group': dict(Counter(row['status'] for row in segments))}}


def write_qa(run_dir, records, a01_record_ids=None):
    result = build_qa(records, a01_record_ids)
    corpus.dump(Path(run_dir) / 'qa-queue.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--records', type=Path, nargs='+', required=True)
    parser.add_argument('--a01-record-ids', type=Path)
    args = parser.parse_args()
    values = []
    for path in args.records:
        value = json.loads(path.read_text(encoding='utf-8'))
        values.extend(value if isinstance(value, list) else value['records'])
    ids = json.loads(args.a01_record_ids.read_text()) if args.a01_record_ids else None
    print(json.dumps(write_qa(args.run_dir, values, ids)['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
