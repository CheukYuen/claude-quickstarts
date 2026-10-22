"""Synthetic tests: no issuer text or Choice private returns in the public tree."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import choice_audit as audit
import corpus


class ValueAlarmTests(unittest.TestCase):
    def test_spaced_and_fullwidth_negative_tokens(self):
        for text in ('- 123', '− 123', '－123', '－ 123', '(123)', '（123）'):
            with self.subTest(text=text):
                tokens = audit.integer_text_index(text)
                self.assertEqual(tokens, {audit.Decimal('-123')})
                result = audit.check_values('000001.SZ', '2025-01-01',
                                            {'negative': -123, 'positive': 123}, text_override=text)
                self.assertEqual([x['status'] for x in result['checks']], ['查到', '查不到'])

    def test_complete_numbers_sign_and_decimals(self):
        tokens = audit.integer_text_index('合成：1,234.00；(2,345)；−67；12345；3.50。')
        self.assertIn(audit.Decimal('1234'), tokens)
        self.assertIn(audit.Decimal('-2345'), tokens)
        self.assertIn(audit.Decimal('-67'), tokens)
        self.assertIn(audit.Decimal('3.5'), tokens)
        self.assertNotIn(audit.Decimal('123'), tokens)
        self.assertNotIn(audit.Decimal('2345'), tokens)

    def test_found_is_independent_of_direct_use(self):
        result = audit.check_values('00001.HK', '2025-01-01', {'A': 12, 'B': None, 'C': 123},
                                    categories={'A': 'Choice 派生'}, text_override='12 与 1234')
        a, b, c = result['checks']
        self.assertEqual(a['status'], '查到')
        self.assertEqual(a['verification_status'], '代码通过')
        self.assertFalse(a['direct_use_allowed'])
        self.assertEqual(b['status'], '未查值')
        self.assertEqual(c['status'], '查不到')
        self.assertEqual(c['verification_status'], '待复核')

    def test_no_matching_report_is_unchecked(self):
        result = audit.check_values('000001.SZ', '2025-01-01', {'A': 12}, index={})
        self.assertEqual(result['checks'][0]['status'], '未查值')

    def test_hk_code_zero_padding_and_period_binding(self):
        record = {'source_id': 'synthetic', 'company_code': '1.HK', 'published_date': '2025-04-01',
                  'doc_type': 'annual', 'period': '2024'}
        index = {'synthetic': {'record': record}}
        self.assertIsNotNone(audit.find_report('00001.HK', '2025-04-01', index, '2024-12-31'))
        self.assertIsNone(audit.find_report('00001.HK', '2025-04-02', index, '2024-12-31'))
        self.assertIsNone(audit.find_report('00001.HK', '2025-04-01', index, '2023-12-31'))
        index['other'] = {'record': dict(record, source_id='other')}
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            audit.find_report('1.HK', '2025-04-01', index, '2024-12-31')

    def test_hash_binding_and_missing_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'document.pdf').write_bytes(b'synthetic pdf')
            (root / 'pages').mkdir()
            (root / 'pages/p0001.txt').write_text('Synthetic amount 456', encoding='utf-8')
            record = {'source_id': 'synthetic', 'path': 'document.pdf',
                      'sha256': corpus.sha256(root / 'document.pdf'), 'pages': 1, 'text_dir': 'pages'}
            binding = {'record': record, 'version': 'm1', 'line': 1, 'normalized_sha256': 'synthetic'}
            text, evidence = audit.load_report_text(binding, root)
            self.assertIn('456', text)
            self.assertIn('p0001.txt', evidence['page_text_sha256'])
            record['pages'] = 2
            self.assertIsNone(audit.load_report_text(binding, root)[0])
            record['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                audit.load_report_text(binding, root)


class ChoiceReturnTests(unittest.TestCase):
    def test_row_labels_require_complete_name_and_bounded_wrapping(self):
        keys = {audit.label_key(x) for x in ('合成资产', '合成负债', '递延税', '归属于合成股东权益合计')}
        text = ('其中：合成资产（增加以“－”号填列）  123  99\n'
                '一、加：合成负债  456\n递延税资产减少  20\n'
                '归属于合成股东权\n益合计  100\n'
                '本文提到合成资产以及递延税，不是科目。')
        found = audit.report_row_labels(text, keys)
        self.assertEqual(found, keys - {'递延税'})
        self.assertNotIn('归属于合成股东权益合计', audit.report_row_labels(
            '归属于合成股东权\n\n益合计', keys))
        self.assertIn('归属于合成股东权益合计', audit.report_row_labels(
            '归属于合成股东权\n                           123  99\n益合计', keys))
        self.assertIn('合成资产', audit.report_row_labels(
            '合成资产（增加以\n                         123  99\n“－”号填列）', keys))
        self.assertNotIn('合成资产分类甲', audit.report_row_labels(
            '合成资产（分类甲）  123', {'合成资产分类甲'}))

    def test_wrapped_label_keeps_only_longest_candidate(self):
        keys = {'合成资产', '合成资产减少'}
        self.assertEqual(audit.report_row_labels('合成资产\n减少 123', keys),
                         {'合成资产减少'})

    def test_name_line_with_values_cannot_join_next_name(self):
        keys = {'合成资产', '合成资产合计'}
        self.assertEqual(audit.report_row_labels('合成资产 100\n合计 200', keys),
                         {'合成资产'})
        self.assertNotIn('合成股东权益合计', audit.report_row_labels(
            '合成股东权 100\n益合计', {'合成股东权益合计'}))

    def test_longest_wrapped_label_allows_intervening_value_line(self):
        keys = {'合成资产', '合成资产减少'}
        self.assertEqual(audit.report_row_labels('合成资产\n    100 200\n减少 123', keys),
                         {'合成资产减少'})
        self.assertEqual(audit.report_row_labels(
            '合成资产\n合计 200', {'合成资产', '合计', '合成资产合计'}),
                         {'合成资产', '合计'})

    @staticmethod
    def query(report_type='1', return_type='合并报表'):
        return {'query': {'function': 'ctr', 'args': ['BalanceStatementSHSZ', '',
                f'SecuCode=000001.SZ,ReportDate=2024-12-31,ReportType={report_type},Ispandas=0']},
                'result': {'ErrorCode': 0, 'Indicators': {'__type__': 'list', 'items':
                ['REPORTDATE', 'FIRSTNOTICEDATE', 'reportType', 'VALUE']},
                'Data': {'0': {'__type__': 'list', 'items':
                ['2024-12-31', '2025-04-01', return_type, 456]}}}}

    def test_decode_original_return_format(self):
        self.assertEqual(audit.ctr_rows(self.query())[0]['VALUE'], 456)
        bad = self.query()
        bad['result']['Data']['0']['items'].pop()
        with self.assertRaisesRegex(ValueError, 'length mismatch'):
            audit.ctr_rows(bad)

    def test_derivation_never_fills_null_with_zero(self):
        result = audit.reconcile_derived_fields('BalanceStatementSHSZ',
                  {'ACCOUNTBILLREC': 30, 'BILLREC': 10, 'ACCOUNTREC': 20,
                   'OTHERPAY': 20, 'TOTAL_OTHER_PAYABLE': 30,
                   'INTERESTPAY': None, 'DIVIDENDPAY': 10})
        checks = {row['field']: row for row in result}
        self.assertEqual(checks['ACCOUNTBILLREC']['status'], '相等')
        self.assertIsNone(checks['OTHERPAY']['computed_value'])
        self.assertIn('不补零', checks['OTHERPAY']['status'])

    def test_report_type_and_notice_date_are_hard_stops(self):
        index = {'s': {'record': {'source_id': 's', 'company_code': '000001.SZ',
                  'doc_type': 'annual', 'period': '2024', 'published_date': '2025-04-01'}}}
        good = {'queries': [self.query()]}
        self.assertEqual(audit.verify_disclosures(good, index)[0]['status'], '一致')
        for q in [self.query('2'), self.query(return_type='合并报表(调整)')]:
            with self.assertRaisesRegex(ValueError, 'STOP'):
                audit.verify_disclosures({'queries': [q]}, index)
        with self.assertRaisesRegex(ValueError, 'STOP'):
            audit.verify_disclosures(good, {})

    def test_map_contains_all_return_fields_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / audit.TABLE_DOCS['BalanceStatementSHSZ']).write_text(
                '## 报表字段说明\n|合成科目(元)|VALUE|描述|\n', encoding='utf-8')
            q = {'query': {'function': 'ctr', 'args': ['BalanceStatementSHSZ', '', '']},
                 'result': {'Indicators': {'__type__': 'list', 'items': ['VALUE', 'UNKNOWN ']}}}
            result = audit.build_field_map({'queries': [q]}, root, '合成科目')
            self.assertEqual(len(result['fields']), 2)
            self.assertEqual(result['fields'][0]['category'], '原表科目')
            substring = audit.build_field_map({'queries': [q]}, root, '合成科目资产减少  123')
            self.assertEqual(substring['fields'][0]['category'], '待定')
            unknown = result['fields'][1]
            self.assertEqual(unknown['choice_field'], 'UNKNOWN ')
            self.assertEqual(unknown['category'], '待定')
            self.assertIsNone(unknown['unit_rule']['choice_unit'])


class HongKongExperimentTests(unittest.TestCase):
    def test_currency_units_and_per_share_cents(self):
        self.assertEqual(audit.hk_scaled_value(1234500, '合成资产(元)')['value'], '1234.500')
        self.assertEqual(audit.hk_scaled_value('0.0592', '基本每股收益(元)')['value'], '5.9200')
        self.assertIsNone(audit.hk_scaled_value(None, '合成资产(元)')['value'])
        self.assertIsNone(audit.hk_scaled_value(12, '未知字段')['value'])

    def test_documented_calculation_cannot_be_promoted_by_matching_value(self):
        category, basis = audit.hk_field_category('IS9', '营业收入(计算)(元)',
                            {'category': '原表科目', 'basis': '合成文本同值'})
        self.assertEqual(category, 'Choice 派生')
        self.assertIn('计算', basis)
        self.assertEqual(audit.hk_field_category('VALUE', '合成资产(元)')[0], '待定')
        self.assertEqual(audit.hk_field_category('NOTICEDATE', '公告日期')[0], '元数据')

    def test_full_offline_review_explicit_report_binding_and_unit_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            source = here / 'source'
            (source / 'manifests').mkdir(parents=True)
            (source / 'text/synthetic').mkdir(parents=True)
            (source / 'synthetic.pdf').write_bytes(b'synthetic HK report')
            (source / 'text/synthetic/p0001.txt').write_text(
                'Synthetic report RMB thousands: 1,234.5 (2). EPS 5.92 cents.', encoding='utf-8')
            row = dict(source_id='synthetic', path='synthetic.pdf', company_code='1.HK',
                       doc_type='annual', period='2025', published_date='2026-04-22',
                       sha256=corpus.sha256(source / 'synthetic.pdf'), pages=1, text_dir='text/synthetic')
            (source / 'manifests/sources.jsonl').write_text(json.dumps(row) + '\n', encoding='utf-8')
            docs = here / 'docs'
            docs.mkdir()
            queries = []
            for table, filename in audit.HK_TABLE_DOCS.items():
                (docs / filename).write_text('ReportType 1 合并报表 2 合并报表(调整) 3 母公司报表 4 母公司报表(调整)\n'
                    '## 报表字段说明\n|公告日期|NOTICEDATE|说明|\n|合成资产(元)|VALUE|说明|\n'
                    '|合成成本(元)|COST|说明|\n|合成每股收益(元)|EPS|说明|\n', encoding='utf-8')
                fields = ['REPORTDATE', 'NOTICEDATE', 'REPORTTYPE', 'VALUE', 'COST', 'EPS']
                values = ['2025-12-31', '2026-03-26', '合并报表', 1234500, 2000, 0.0592]
                queries.append({'query': {'function': 'ctr', 'args': [table, '',
                    'SecuCode=00001.HK,ReportDate=2025-12-31,ReportType=1,Ispandas=0']},
                    'result': {'ErrorCode': 0, 'Indicators': {'__type__': 'list', 'items': fields},
                               'Data': {'0': {'__type__': 'list', 'items': values}}}})
            capture = here / 'synthetic-capture.json'
            capture.write_text(json.dumps({'queries': queries, 'login': {'ErrorCode': 0}}), encoding='utf-8')
            run = here / 'runs/s1-synthetic'
            with patch.object(audit, 'HERE', here):
                result = audit.review_hk_capture(capture, source, 'synthetic', docs, run)
            self.assertTrue(result['all_three_tables_returned'])
            self.assertEqual(result['counts']['statuses'], {'查到': 6, '查不到': 3})
            self.assertEqual(result['counts']['magnitude_only_matches'], 3)
            self.assertEqual(result['counts']['categories'], {'待定': 9})
            self.assertFalse(result['disclosures'][0]['notice_matches_annual_report']['NOTICEDATE'])
            self.assertEqual(result['report_binding']['source_id'], 'synthetic')
            self.assertTrue((run / 'choice-hk-interface-audit.json').exists())
            for table in result['tables']:
                self.assertTrue(all(not item['direct_use_allowed'] for item in table['checks']))


if __name__ == '__main__':
    unittest.main()
