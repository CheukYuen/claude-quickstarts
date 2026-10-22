"""Synthetic records for S2 numerical QA; all numbers and parties are fictional."""
from copy import deepcopy
import unittest

import records
import s2_qa


def metric(identifier, value='10', *, source='synthetic-report', notice='2026-08-22',
           subject='Example股份有限公司', indicator='营业收入', period='2026H1',
           unit='元', currency='CNY', scope='consolidated', group=None, role='standalone',
           dimensions=(), qualifier='exact', decimals=None):
    return records.make_record('metric', identifier, source, subject, indicator, period, notice,
        '2026-06-30', [dict(page=1, text='Synthetic value ' + value)],
        dict(scope=scope, scope_note='synthetic', value_status='reported', value_note=''),
        period=dict(kind='half_year', label=period), numbers=[records.number(value, unit, currency,
        qualifier=qualifier, decimals=decimals)],
        additivity=dict(group=group, role=role, reason='synthetic same-table group'),
        dimensions=[dict(name=name, value=content) for name, content in dimensions])


class QaTests(unittest.TestCase):
    def test_same_date_conflict_scaled_equality_and_later_coverage(self):
        old = metric('old', '1000000')
        equivalent = metric('same', '1', unit='百万元')
        conflict = metric('conflict', '2')
        later = metric('later', '3', notice='2026-09-01')
        result = s2_qa.build_qa([old, equivalent, conflict, later])
        self.assertEqual(len(result['same_key_conflicts']), 2)
        self.assertTrue(all('later' not in r['record_ids'] for r in result['same_key_conflicts']))
        self.assertEqual(next(r for r in result['coverage_relations'] if r['record_id'] == 'old')['covered_by'], ['later'])
        self.assertTrue(all(r['queue_type'] == '质检问题，不是研究缺口' for r in result['queue']))

    def test_scope_dimensions_nature_and_disclosure_dates_are_separate_keys(self):
        base = metric('base')
        parent = metric('parent', '20', scope='parent')
        dimension = metric('dimension', '30', dimensions=[('产品', 'A')])
        forecast = metric('forecast', '40')
        forecast['nature'] = 'institution_forecast'
        forecast['origin']['speaker'] = 'SyntheticBroker'
        result = s2_qa.build_qa([base, parent, dimension, forecast])
        self.assertEqual(result['same_key_conflicts'], [])

    def test_decimal_sum_deduplicates_equivalent_records_and_flags_true_difference(self):
        total = metric('t', '0.3', group='g', role='total')
        a = metric('a', '0.1', indicator='产品A收入', group='g', role='component')
        b = metric('b', '0.2', indicator='产品B收入', group='g', role='component')
        duplicate = deepcopy(a)
        duplicate['record_id'] = 'a-repeat'
        result = s2_qa.additive_checks([total, a, b, duplicate])
        self.assertEqual(result[0]['status'], '相等')
        total['numbers'][0]['value'] = '9.3'
        result = s2_qa.additive_checks([total, a, b])
        self.assertEqual(result[0]['status'], '不相等')

    def test_rounding_and_approximation_separate_and_limits_refused(self):
        total = metric('t', '10.0', group='g', role='total', decimals=1)
        a = metric('a', '5.0', indicator='A', group='g', role='component', decimals=1)
        b = metric('b', '5.1', indicator='B', group='g', role='component', decimals=1)
        self.assertEqual(s2_qa.additive_checks([total, a, b])[0]['status'], '约数或舍入差额')
        b['numbers'][0]['qualifier'] = 'at_least'
        self.assertEqual(s2_qa.additive_checks([total, a, b])[0]['status'], '无法比较')
        b['numbers'][0]['qualifier'] = 'approximate'
        b['numbers'][0]['value'] = '6.0'
        self.assertEqual(s2_qa.additive_checks([total, a, b])[0]['status'], '约数或舍入差额')

    def test_unit_and_period_mismatch_not_added(self):
        total = metric('t', '10', group='g', role='total')
        a = metric('a', '5', indicator='A', group='g', role='component', unit='万元')
        self.assertEqual(s2_qa.additive_checks([total, a])[0]['status'], '无法比较')
        a['period']['label'] = a['object_key']['period'] = '2025'
        self.assertEqual(s2_qa.additive_checks([total, a]), [])

    def test_segments_external_only_or_explicit_elimination_compare(self):
        total = metric('t', '30', group='segments', role='total')
        a = metric('a', '10', scope='segment', group='segments', role='component',
                   dimensions=[('分部', 'A'), ('收入范围', '对外')])
        b = metric('b', '20', scope='segment', group='segments', role='component',
                   dimensions=[('分部', 'B'), ('收入范围', '对外')])
        self.assertEqual(s2_qa.segment_checks([total, a, b])[0]['status'], '相等')
        for row in (a, b):
            row['dimensions'] = [d for d in row['dimensions'] if d['name'] != '收入范围']
        self.assertEqual(s2_qa.segment_checks([total, a, b])[0]['status'], '无法比较')
        a['numbers'][0]['value'] = '15'
        elimination = metric('elim', '-5', group='segments', role='component', dimensions=[('调整', '分部间抵销')])
        rows = s2_qa.segment_checks([total, a, b, elimination])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], '相等')
        self.assertEqual(s2_qa.segment_checks([a, b])[0]['status'], '无法比较')

    def test_a01_strict_key_no_numeric_guess_and_lookup_alarm(self):
        pdf = metric('pdf', '100')
        choice = metric('choice', '100')
        choice['source']['kind'] = 'choice'
        choice['verification'] = dict(status='待复核', note='synthetic lookup missing')
        choice['object_key']['indicator'] = '收入'  # same number is not a match.
        result = s2_qa.build_qa([pdf, choice], ['pdf'])
        self.assertEqual(result['choice_pdf_a01'][0]['status'], '未对齐')
        self.assertTrue(any(r['kind'] == 'choice_value_lookup' for r in result['queue']))
        choice['object_key']['indicator'] = '营业收入'
        self.assertEqual(s2_qa.choice_alignment([pdf, choice], ['pdf'])[0]['status'], '相等')

    def test_full_source_id_cross_coverage_and_unit_refinement(self):
        ids = ('1300.HK-20260913-announcement-qinghai-possible-acquisition',
               '1300.HK-20260920-announcement-qinghai-acquisition',
               '1300.HK-20260925-announcement-qinghai-supplemental')
        notices = ('2026-09-13', '2026-09-20', '2026-09-25')
        equity = [metric('eq' + str(i), '455', unit='百万元', source=source, notice=notice,
                         indicator='股权对价', period='unspecified') for i, (source, notice) in enumerate(zip(ids, notices))]
        cap = [metric('cap1', '400', unit='吨', source=ids[1], notice=notices[1], indicator='产能', period='unspecified', currency=None),
               metric('cap2', '400', unit='吨/年', source=ids[2], notice=notices[2], indicator='产能', period='unspecified', currency=None)]
        result = s2_qa.cross_document_diagnostics(equity + cap)
        self.assertEqual(result['equity_consideration_455_million']['status'], '连成覆盖链')
        self.assertEqual(result['capacity_400_tonnes']['status'], '连成覆盖链')
        cap[1]['object_key']['subject'] = 'ExampleCo简称'
        result = s2_qa.cross_document_diagnostics(cap)
        self.assertEqual(result['capacity_400_tonnes']['status'], '未连成覆盖链')
        self.assertIn('subject', result['capacity_400_tonnes']['unaligned_pairs'][0]['different_fields'])

    def test_equal_dates_do_not_claim_coverage_chain(self):
        rows = [metric(short, '455', unit='百万元', source=short) for short in ('S46', 'S48', 'S50')]
        self.assertEqual(s2_qa.cross_document_diagnostics(rows)['equity_consideration_455_million']['status'], '未连成覆盖链')


if __name__ == '__main__':
    unittest.main()
