"""Synthetic captures only; no vendor data or licensed report excerpts."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import corpus
import records
import s2_choice


def query(period='2026-06-30', report_type='1', returned='合并报表', notice='2026-08-22'):
    row = {'REPORTDATE': period, 'FIRSTNOTICEDATE': notice, 'reportType': returned,
           'REVENUE': 125, 'EMPTY': None, 'DERIVED': 999, 'META': 'test'}
    return {'query': {'function': 'ctr', 'args': ['IncomeStatementSHSZ', '',
            f'SecuCode=601869.SH,ReportDate={period},ReportType={report_type}']},
            'result': {'ErrorCode': 0, 'Indicators': list(row), 'Data': {'SYN': list(row.values())}}}


def field_map():
    return {'fields': [{'table': 'IncomeStatementSHSZ', 'choice_field': key,
        'category': category, 'original_table_item': label,
        'unit_rule': {'choice_unit': '元' if category == '原表科目' else None,
                      'multiplier_to_record': '1', 'currency': 'CNY'}}
        for key, category, label in (('REVENUE', '原表科目', '营业收入'),
                                    ('EMPTY', '原表科目', '空白科目'),
                                    ('DERIVED', 'Choice 派生', None), ('META', '待定', None))]}


def binding(period='2026-06-30'):
    return {'source_id': 'synthetic-choice', 'company_code': '601869.SH',
            'queries': [{'index': 0, 'table': 'IncomeStatementSHSZ', 'report_period': period}]}


class ChoiceConversionTests(unittest.TestCase):
    def test_canonical_periods_and_legal_full_name(self):
        self.assertEqual(s2_choice.period_definition('BalanceStatementSHSZ', '2026-06-30'),
                         {'kind': 'instant', 'label': '2026-06-30'})
        self.assertEqual(s2_choice.period_definition('IncomeStatementSHSZ', '2026-06-30')['label'], '2026H1')
        self.assertEqual(s2_choice.period_definition('CashFlowStatementSHSZ', '2025-12-31')['label'], '2025')
        result = s2_choice.convert_payload({'queries': [query()]}, binding(), field_map(), index={})
        self.assertEqual(len(result['records']), 1)
        row = result['records'][0]
        self.assertEqual(row['object_key'], {'subject': s2_choice.DEFAULT_SUBJECT,
                         'indicator': '营业收入', 'period': '2026H1'})
        self.assertEqual(row['source']['kind'], 'choice')
        self.assertEqual(row['disclosed_on'], '2026-08-22')
        self.assertEqual(row['excerpts'], [])
        self.assertEqual(row['locator']['pdf_pages'], [])
        self.assertEqual(row['verification']['status'], '未查值')
        self.assertEqual(records.validate_record(row), [])
        self.assertEqual(result['summary']['empty_original_values'], 1)
        self.assertEqual(result['summary']['skipped_field_periods'], {'Choice 派生': 1, '待定': 1})

    def test_audit_every_nonempty_original_value_and_preserve_status(self):
        def audit(code, notice, values, **kwargs):
            self.assertEqual(values, {'REVENUE': 125})
            return {'checks': [{'field': 'REVENUE', 'status': '查不到',
                    'verification_status': '待复核', 'reason': 'synthetic absent'}]}
        with mock.patch('choice_audit.check_values', side_effect=audit) as check:
            result = s2_choice.convert_payload({'queries': [query()]}, binding(), field_map(), index={})
        self.assertEqual(check.call_count, 1)
        self.assertEqual(result['records'][0]['verification']['status'], '待复核')
        self.assertEqual(result['value_checks'][0]['checks'][0]['record_id'], result['records'][0]['record_id'])

    def test_refuse_report_type_company_date_and_duplicate_table(self):
        cases = [query(report_type='2'), query(returned='合并报表(调整)'), query(notice='2026-13-01')]
        for value in cases:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                s2_choice.convert_payload({'queries': [value]}, binding(), field_map(), index={})
        changed = binding()
        changed['company_code'] = '999999.SH'
        with self.assertRaisesRegex(ValueError, 'company'):
            s2_choice.convert_payload({'queries': [query()]}, changed, field_map(), index={})
        changed = binding()
        changed['queries'] *= 2
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            s2_choice.convert_payload({'queries': [query()]}, changed, field_map(), index={})

    def test_empty_not_zero_invalid_not_record_and_fields_remain_field_level(self):
        value = query()
        columns = value['result']['Indicators']
        values = value['result']['Data']['SYN']
        values[columns.index('REVENUE')] = 'NaN'
        result = s2_choice.convert_payload({'queries': [value]}, binding(), field_map(), index={})
        self.assertEqual(result['records'], [])
        self.assertEqual(result['summary']['empty_original_values'], 1)
        self.assertEqual(result['summary']['invalid_original_values'], 1)
        values[columns.index('REVENUE')] = 0
        result = s2_choice.convert_payload({'queries': [value]}, binding(), field_map(), index={})
        self.assertEqual(result['records'][0]['numbers'][0]['value'], '0')
        self.assertFalse(any(r['object_key']['indicator'] == 'DERIVED' for r in result['records']))


class VendorBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=corpus.HERE / 'runs', prefix='s2-synthetic-test-')
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.vendor = self.source / 'vendor/choice/choice-20261004-yofc-financials-market.json'
        self.vendor.parent.mkdir(parents=True)
        self.vendor.write_text(json.dumps({'queries': [query()]}, ensure_ascii=False))
        self.manifest = self.source / 'manifests/sources.jsonl'
        self.manifest.parent.mkdir()
        self.row = {'kind': 'vendor', 'source_id': 'synthetic-choice', 'company_code': '601869.SH',
                    'path': str(self.vendor.relative_to(self.source)), 'sha256': corpus.sha256(self.vendor),
                    'published_date': None}
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self, rows=None):
        self.manifest.write_text(''.join(json.dumps(row) + '\n' for row in (rows or [self.row])))

    def test_manifest_hash_and_null_published_date_bound(self):
        # Unrelated captures may deliberately have no company code.
        self.save_manifest([dict(kind='vendor', source_id='synthetic-unrelated', company_code=None), self.row])
        result = corpus.prepare_vendor(self.base / 'run', source_root=self.source,
                                      report_periods=['2026-06-30'])
        self.assertEqual(result['metadata_record']['published_date'], None)
        self.assertEqual(result['report_periods'], ['2026-06-30'])
        self.assertEqual(result['vendor_sha256'], self.row['sha256'])
        self.assertEqual(result['record_binding']['line'], 2)
        self.assertTrue((self.base / 'run/choice/binding.json').exists())

    def test_reject_wrong_type_hash_period_and_empty_row(self):
        for capture in (query(report_type='2'), query(returned='母公司报表')):
            self.vendor.write_text(json.dumps({'queries': [capture]}, ensure_ascii=False))
            self.row['sha256'] = corpus.sha256(self.vendor)
            self.save_manifest()
            with self.assertRaises(ValueError):
                corpus.prepare_vendor(self.base / 'run', source_root=self.source)
        self.vendor.write_text(json.dumps({'queries': [query()]}))
        self.row['sha256'] = corpus.sha256(self.vendor)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Missing requested'):
            corpus.prepare_vendor(self.base / 'run', source_root=self.source, report_periods=['2025-12-31'])
        self.vendor.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            corpus.prepare_vendor(self.base / 'run', source_root=self.source)
        empty = query()
        empty['result']['Data'] = {}
        self.vendor.write_text(json.dumps({'queries': [empty]}))
        self.row['sha256'] = corpus.sha256(self.vendor)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            corpus.prepare_vendor(self.base / 'run', source_root=self.source)

    def test_version_chain_binding_is_preserved(self):
        previous = deepcopy(self.row)
        previous['manifest_record_version'] = 'm1'
        revised = deepcopy(previous)
        revised['manifest_record_version'] = 'm2'
        revised['metadata_revision'] = {'reason': 'synthetic metadata correction'}
        revised['supersedes_manifest_ref'] = {'path': 'manifests/sources.jsonl',
            'source_id': previous['source_id'], 'manifest_record_version': 'm1',
            'normalized_sha256': corpus.normalized_sha256(previous)}
        self.save_manifest([previous, revised])
        result = corpus.prepare_vendor(self.base / 'run', source_root=self.source)
        self.assertEqual(result['record_binding']['version'], 'm2')
        self.assertEqual([r['version'] for r in result['record_binding']['chain']], ['m1', 'm2'])


if __name__ == '__main__':
    unittest.main()
