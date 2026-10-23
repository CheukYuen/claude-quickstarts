"""Synthetic-only S2b compact transport, expansion and evidence-policy tests."""
from copy import deepcopy
import json
import unittest

import records


def synthetic_output():
    return dict(schema_version=records.COMPACT_VERSION,
        entities=[dict(id='e1', type='company', names=['示例股份有限公司', '示例公司'],
                       canonical='示例股份有限公司', renamed_from=[])],
        tables=[dict(id='t1', name='主要会计数据', pages=[1],
            header=[dict(page=1, text='单位：元 2025 2024 同比')], entity='e1',
            scope='consolidated', unit='元', currency='CNY',
            columns=[dict(label='', period='2025', kind='annual', nature='actual',
                          as_of='2025-12-31', unit=None, currency=None, dimensions=[], extras=[]),
                     dict(label='', period='2024', kind='annual', nature='actual',
                          as_of='2024-12-31', unit=None, currency=None, dimensions=[], extras=[]),
                     dict(label='同比', period='2025', kind='annual', nature='actual',
                          as_of='2025-12-31', unit='%', currency=None, dimensions=[], extras=[])],
            rows=[dict(id='r1', name='营业收入', excerpts=[dict(page=1, text='营业收入 1,234 (56) 20%')],
                       values=['1,234', '(56)', '20%'], extras=[]),
                  dict(id='r2', name='每股收益', excerpts=[dict(page=1, text='每股收益 NA NM')],
                       values=['NA', 'NM', ''], extras=[])], extras=[])],
        records=[], coverage=[dict(item_id='A01', status='已抽', record_ids=['t1'],
                                   checked_pages=[1], basis='本块主要数据表')],
        outside_index=[dict(page=1, title='会计估计')])


def accept(output, page=None):
    if page is None:
        page = '示例股份有限公司\n单位：元 2025 2024 同比\n营业收入 1,234 (56) 20%\n每股收益 NA NM\n会计估计'
    return records.accept_model_output(output, source_id='synthetic', disclosed_on='2026-03-01',
        extraction_version='extract-records-s2b-1', prefix='sample', pages={('synthetic', 1): page},
        checklist={'items': [{'id': 'A01'}]}, compact=True)


class CompactSchemaTests(unittest.TestCase):
    def test_every_transport_object_is_strict_and_has_all_keys_required(self):
        schema = records.compact_model_output_schema()
        def visit(value):
            if isinstance(value, dict):
                if value.get('type') == 'object':
                    self.assertEqual(set(value['required']), set(value['properties']))
                    self.assertFalse(value['additionalProperties'])
                self.assertFalse(set(value) & {'format', 'minLength', 'pattern', 'uniqueItems'})
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(schema)
        self.assertLess(len(json.dumps(schema, ensure_ascii=False, indent=2)), 20000)
        self.assertEqual(schema, records.model_output_schema(transport=True, compact=True))
        self.assertFalse(records._fields(synthetic_output(), schema))

    def test_synthetic_table_expands_with_source_precision(self):
        result = accept(synthetic_output())
        self.assertEqual(len(result['records']), 5)
        self.assertFalse(result['rejected'])
        self.assertEqual(result['blank_cells'], 1)
        rows = result['records']
        self.assertEqual([r['numbers'][0]['value'] for r in rows[:3]], ['1234', '-56', '20'])
        self.assertEqual(rows[2]['object_key']['indicator'], '营业收入（同比）')
        self.assertEqual(rows[2]['numbers'][0]['unit'], '%')
        self.assertIsNone(rows[2]['numbers'][0]['currency'])
        self.assertEqual([r['detail']['value_status'] for r in rows[3:]],
                         ['not_available', 'not_meaningful'])
        self.assertTrue(all(r['object_key']['subject'] == '示例股份有限公司' for r in rows))
        self.assertEqual(result['coverage'][0]['record_ids'], [r['record_id'] for r in rows])
        self.assertEqual(result['outside_index'], synthetic_output()['outside_index'])

    def test_percentage_column_without_printed_sign_has_no_inherited_currency(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['values'][2] = '20'
        result = accept(output)
        self.assertIsNone(result['records'][2]['numbers'][0]['currency'])

    def test_printed_parenthesized_percent_and_plus(self):
        for raw, value in (('(20%)', '-20'), ('(20)%', '-20'), ('+20%', '20')):
            output = synthetic_output()
            output['tables'][0]['rows'][0]['values'][2] = raw
            text = '营业收入 1,234 (56) ' + raw
            output['tables'][0]['rows'][0]['excerpts'][0]['text'] = text
            result = accept(output, '单位：元 2025 2024 同比\n' + text + '\n每股收益 NA NM')
            self.assertFalse(result['rejected'], raw)
            self.assertEqual(result['records'][2]['numbers'][0]['value'], value)
            self.assertEqual(result['records'][2]['numbers'][0]['unit'], '%')

    def test_printed_percentage_point_and_multiple_suffixes(self):
        for raw, value, unit in (('增加16.69个百分点', '16.69', '百分点'),
                                 ('增加14.92个百分点', '14.92', '百分点'),
                                 ('1.7ppts', '1.7', '百分点'), ('2.0 ppt', '2.0', '百分点'),
                                 ('3.5百分点', '3.5', '百分点'),
                                 ('1.5x', '1.5', '倍'), ('3.2 倍', '3.2', '倍')):
            output = synthetic_output()
            output['tables'][0]['rows'][0]['values'][2] = raw
            text = '营业收入 1,234 (56) ' + raw
            output['tables'][0]['rows'][0]['excerpts'][0]['text'] = text
            result = accept(output, '单位：元 2025 2024 同比\n' + text + '\n每股收益 NA NM')
            self.assertFalse(result['rejected'], raw)
            item = result['records'][2]['numbers'][0]
            self.assertEqual((item['value'], item['unit'], item['source_unit'], item['currency']),
                             (value, unit, unit, None))
            self.assertEqual(result['records'][2]['excerpts'][0]['text'], text)

    def test_decrease_prefix_does_not_invent_negative_value(self):
        output = synthetic_output()
        raw = '减少1.7ppts'
        output['tables'][0]['rows'][0]['values'][2] = raw
        text = '营业收入 1,234 (56) ' + raw
        output['tables'][0]['rows'][0]['excerpts'][0]['text'] = text
        result = accept(output, '单位：元 2025 2024 同比\n' + text + '\n每股收益 NA NM')
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['rejected'][0]['record']['numbers'][0]['value'], '减少1.7')

    def test_mismatched_column_count_is_structural_error(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['values'].pop()
        with self.assertRaisesRegex(ValueError, 'value count'):
            accept(output)

    def test_suspicious_missing_column_is_accepted_and_queued(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['excerpts'][0]['text'] += ' 99'
        result = accept(output, '单位：元 2025 2024 同比\n营业收入 1,234 (56) 20% 99\n每股收益 NA NM')
        self.assertEqual(len(result['records']), 5)
        self.assertFalse(result['rejected'])
        self.assertEqual(result['suspicious_missing_columns'], 1)
        self.assertEqual(len(result['soft_queue']), 3)
        self.assertTrue(all('疑似漏列' in item['issues'] for item in result['soft_queue']))

    def test_sparse_values_override_without_changing_stored_contract(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['extras'] = [
            dict(key='additivity', value=json.dumps(dict(group=None, role='total', reason=''))),
            dict(key='limitations', value='["合成口径"]')]
        result = accept(output)
        self.assertFalse(result['rejected'])
        self.assertEqual(len(result['soft_queue']), 3)
        self.assertEqual(result['records'][0]['limitations'], ['合成口径'])
        self.assertFalse(records._fields(result['records'][0], records.RECORD_SCHEMA))

    def test_duplicate_or_unknown_sparse_fields_are_refused(self):
        for extras in ([dict(key='source', value='{}')],
                       [dict(key='is_market', value='true'), dict(key='is_market', value='false')],
                       [dict(key='origin', value='{broken}')]):
            output = synthetic_output()
            output['tables'][0]['extras'] = extras
            with self.assertRaises(ValueError):
                accept(output)

    def test_canonical_name_must_be_in_names(self):
        output = synthetic_output()
        output['entities'][0]['canonical'] = '外部公司'
        with self.assertRaisesRegex(ValueError, 'canonical'):
            accept(output)

    def test_contiguous_excerpt_rule_stays_hard(self):
        output = synthetic_output()
        result = accept(output, '单位：元 2025 2024 同比\n营业收入 1,234 侧栏内容 (56) 20%\n每股收益 NA NM')
        self.assertEqual(len(result['rejected']), 3)
        self.assertEqual(len(result['records']), 2)
        self.assertTrue(all(any('absent from frozen page' in e for e in r['errors'])
                            for r in result['rejected']))

    def test_number_and_excerpt_length_stay_hard(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['values'][0] = '999'
        result = accept(output)
        self.assertEqual(len(result['rejected']), 1)
        output = synthetic_output()
        text = '营业收入 ' + '字' * 501 + ' 1,234 (56) 20%'
        output['tables'][0]['rows'][0]['excerpts'][0]['text'] = text
        result = accept(output, '单位：元 2025 2024 同比\n' + text + '\n每股收益 NA NM')
        self.assertEqual(len(result['rejected']), 3)
        self.assertTrue(all(any('exceeds 500' in e for e in r['errors']) for r in result['rejected']))

    def test_sparse_guide_uses_canonical_shapes_and_enums(self):
        guide = records.compact_field_guide()
        for value in ('origin={speaker:string|null,original_source:string|null,common_origin_id:string|null}',
                      'date_notes=[{field:string,reason:string}]',
                      'relationship_type:ownership|subsidiary|renamed_to|director',
                      'status:current|prospective|historical|unspecified',
                      'statement_type:explanation|forecast|guidance|judgment|definition|risk|',
                      'metric.detail=', 'event.detail=', 'obligation.detail=',
                      'relationship.detail=', 'statement.detail='):
            self.assertIn(value, guide)
        self.assertIn('不存在source键', guide)
        self.assertIn('statement.detail不能含scope', guide)

    def test_entity_ids_in_role_fields_expand_to_canonical_names(self):
        for kind, role_fields in (
                ('relationship', {'subject': 'e1', 'object': 'e2'}),
                ('obligation', {'obligor': 'e1', 'beneficiary': 'e2'}),
                ('statement', {'speaker': 'e2'})):
            output = synthetic_output()
            output['entities'].append(dict(id='e2', type='company', names=['乙股份有限公司'],
                                          canonical='乙股份有限公司', renamed_from=[]))
            output['tables'] = []
            output['records'] = [dict(id='r1', type=kind, entity='e1', indicator='合成关系',
                period='本次交易', kind='event', nature='actual', as_of='2025-12-31',
                excerpts=[dict(page=1, text='示例股份有限公司与乙股份有限公司合成关系')], numbers=[],
                extras=[dict(key='detail.' + key, value=json.dumps(value))
                        for key, value in role_fields.items()] + [dict(key='origin',
                        value=json.dumps(dict(speaker='e2', original_source=None, common_origin_id=None)))])]
            output['coverage'][0]['record_ids'] = ['r1']
            result = accept(output, '示例股份有限公司与乙股份有限公司合成关系')
            self.assertFalse(result['rejected'])
            record = result['records'][0]
            for key, value in role_fields.items():
                self.assertEqual(record['detail'][key],
                                 {'e1': '示例股份有限公司', 'e2': '乙股份有限公司'}[value])
            self.assertEqual(record['origin']['speaker'], '乙股份有限公司')
            self.assertEqual(record['object_key']['subject'], '示例股份有限公司')

    def test_entity_resolution_preserves_unknown_source_roles(self):
        item = dict(detail=dict(subject='e1', object='原文所述受托人'),
                    origin=dict(speaker='未知第三方'))
        records._resolve_entity_fields(item, {'e1': {'canonical': '示例股份有限公司'}})
        self.assertEqual(item['detail']['subject'], '示例股份有限公司')
        self.assertEqual(item['detail']['object'], '原文所述受托人')
        self.assertEqual(item['origin']['speaker'], '未知第三方')

    def test_invalid_independent_record_does_not_discard_valid_peers(self):
        output = synthetic_output()
        output['tables'] = []
        raw = dict(id='good', type='metric', entity='e1', indicator='营业收入',
                   period='2025', kind='annual', nature='actual', as_of='2025-12-31',
                   excerpts=[dict(page=1, text='营业收入 1,234')],
                   numbers=[dict(value='1234', unit='元', currency='CNY', source_unit='元')], extras=[])
        for field, bad_value in (('period', ''), ('kind', 'made-up'), ('nature', 'made-up'),
                                 ('type', 'made-up'), ('indicator', 123), ('as_of', 'bad-date'),
                                 ('excerpts', 'bad-type'), ('numbers', 'bad-type'),
                                 ('numbers', [True]), ('numbers', [dict(value=1234, unit='元',
                                  currency='CNY', source_unit='元')]), ('extras', 'bad-type')):
            bad = deepcopy(raw)
            bad.update(id='bad', **{field: bad_value})
            output['records'] = [deepcopy(raw), bad]
            output['coverage'][0]['record_ids'] = ['good', 'bad']
            result = accept(output)
            self.assertEqual([r['record_id'] for r in result['records']], ['sample-good'], field)
            self.assertEqual([r['record']['record_id'] for r in result['rejected']], ['sample-bad'], field)
            if field in ('period', 'kind', 'nature', 'as_of'):
                actual = result['rejected'][0]['record']
                actual = actual['period']['label' if field == 'period' else 'kind'] if field in ('period', 'kind') else actual[field]
                self.assertEqual(actual, bad_value)

    def test_malformed_sparse_detail_preserves_valid_table_peers(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['extras'] = [
            dict(key='detail', value='"合成限制说明"'),
            dict(key='detail.scope', value='"consolidated"')]
        result = accept(output)
        self.assertEqual(len(result['rejected']), 3)
        self.assertEqual(len(result['records']), 2)
        self.assertTrue(all(item['record']['detail'] == '合成限制说明' for item in result['rejected']))
        self.assertTrue(all(any('expected' in error for error in item['errors'])
                            for item in result['rejected']))

    def test_malformed_sparse_detail_preserves_valid_nontable_peers(self):
        output = synthetic_output()
        output['records'] = [dict(id='bad', type='metric', entity='e1', indicator='营业收入',
            period='2025', kind='annual', nature='actual', as_of='2025-12-31',
            excerpts=[dict(page=1, text='营业收入 1,234')],
            numbers=[dict(value='1234', unit='元', currency='CNY', source_unit='元')],
            extras=[dict(key='detail', value='"合成限制说明"'),
                    dict(key='detail.scope', value='"consolidated"')])]
        output['coverage'][0]['record_ids'].append('bad')
        result = accept(output)
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(len(result['records']), 5)
        self.assertEqual(result['rejected'][0]['record']['detail'], '合成限制说明')

    def test_empty_column_period_rejects_only_affected_cells(self):
        output = synthetic_output()
        good_table = deepcopy(output['tables'][0])
        good_table['id'] = 't2'
        good_table['rows'][0]['id'] = 'r3'
        good_table['rows'][1]['id'] = 'r4'
        output['tables'].append(good_table)
        output['tables'][0]['columns'][0]['period'] = ''
        output['coverage'][0]['record_ids'].append('t2')
        result = accept(output)
        self.assertEqual(len(result['records']), 8)
        self.assertEqual({item['record']['record_id'] for item in result['rejected']},
                         {'sample-r1-c001', 'sample-r2-c001'})
        self.assertTrue(all(item['record']['period']['label'] == '' for item in result['rejected']))
        self.assertTrue(all(any('empty text' in error for error in item['errors'])
                            for item in result['rejected']))
        self.assertTrue({'sample-r3-c001', 'sample-r4-c001'} <=
                        {item['record_id'] for item in result['records']})

    def test_invalid_dates_and_enums_stay_hard(self):
        output = synthetic_output()
        output['tables'][0]['columns'][0]['as_of'] = '2025-02-30'
        with self.assertRaisesRegex(ValueError, 'invalid ISO date'):
            accept(output)
        output = synthetic_output()
        output['tables'][0]['scope'] = 'made-up'
        with self.assertRaisesRegex(ValueError, 'illegal enum'):
            accept(output)


class S2bPolicyTests(unittest.TestCase):
    def test_integer_word_and_time_denominator_are_opt_in(self):
        three = records.number('3', '年')
        self.assertFalse(records.number_in_excerpt(three, '任期三年'))
        self.assertTrue(records.number_in_excerpt(three, '任期三年', allow_integer_words=True))
        self.assertTrue(records.number_in_excerpt(records.number('2', '个'), 'two plants', allow_integer_words=True))
        self.assertFalse(records.number_in_excerpt(three, '任期十三年', allow_integer_words=True))
        self.assertFalse(records.number_in_excerpt(three, 'threesome', allow_integer_words=True))
        value = records.number('400', '吨/年', source_unit='吨')
        with self.assertRaises(ValueError):
            records.number_in_excerpt(value, '400 吨')
        self.assertTrue(records.number_in_excerpt(value, '400 吨', allow_time_denominator=True))
        with self.assertRaises(ValueError):
            records.number_in_excerpt(records.number('400', '元/股', source_unit='元'),
                                      '400 元', allow_time_denominator=True)

    def test_null_dates_and_missing_attribution_are_soft(self):
        output = synthetic_output()
        output['tables'][0]['columns'][0].update(as_of=None, nature='secondary')
        result = accept(output)
        self.assertFalse(result['rejected'])
        self.assertEqual(len(result['soft_queue']), 2)
        first = result['soft_queue'][0]['issues']
        self.assertIn('转引缺original_source或common_origin_id', first)
        self.assertIn('as_of: null日期缺说明', first)

    def test_non_table_integer_fact_and_statement_bundle(self):
        output = synthetic_output()
        output.update(tables=[], outside_index=[], records=[dict(id='r1', type='statement', entity='e1',
            indicator='任期与人数', period='本次任职', kind='event', nature='actual', as_of=None,
            excerpts=[dict(page=1, text='任期三年，共two人')],
            numbers=[dict(value='3', unit='年', currency=None, source_unit='年'),
                     dict(value='2', unit='人', currency=None, source_unit='人')], extras=[])])
        output['coverage'][0]['record_ids'] = ['r1']
        result = accept(output, '示例股份有限公司任期三年，共two人')
        self.assertEqual(len(result['records']), 1)
        self.assertFalse(result['rejected'])
        self.assertIn('statement疑似打包多个指标或多期数值', result['soft_queue'][0]['issues'])

    def test_other_semantic_rules_are_soft_under_s2b(self):
        output = synthetic_output()
        output['tables'][0]['columns'][0].update(unit='未知', nature='institution_forecast')
        result = accept(output)
        self.assertFalse(result['rejected'])
        issues = result['soft_queue'][0]['issues']
        self.assertIn('未知单位缺口径说明', issues)
        self.assertIn('机构预测缺origin.speaker', issues)
        record = result['records'][0]
        self.assertTrue(any('unknown numeric unit' in e for e in records.validate_record(record)))
        self.assertTrue(any('origin.speaker' in e for e in records.validate_record(record)))
        record.update(record_type='statement', detail=dict(speaker='研究机构', content='目标价',
            statement_type='market_target', attribution_note=''), nature='actual', is_market=False)
        self.assertFalse(records.validate_record(record, policy='s2b'))
        self.assertIn('评级或目标价缺is_market标记', records.soft_record_issues(record))

    def test_invalid_printed_cell_rejects_only_that_record(self):
        output = synthetic_output()
        output['tables'][0]['rows'][0]['values'][0] = '1234*'
        result = accept(output)
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(len(result['records']), 4)
        self.assertTrue(any('illegal string format' in e for e in result['rejected'][0]['errors']))

    def test_null_date_note_can_be_explicitly_supplied(self):
        output = synthetic_output()
        output['tables'][0]['columns'][0].update(as_of=None, extras=[
            dict(key='date_notes', value='[{"field":"as_of","reason":"仅有月份"}]')])
        result = accept(output)
        self.assertFalse(result['soft_queue'])

    def test_direct_validator_default_policy_is_unchanged(self):
        record = accept(synthetic_output())['records'][0]
        record['as_of'] = None
        self.assertTrue(any('null date' in error for error in records.validate_record(record)))
        self.assertFalse(records.validate_record(record, policy='s2b'))

class ColumnMetricMeaningTests(unittest.TestCase):
    def test_distinct_metric_column_labels_survive_same_period_expansion(self):
        output = synthetic_output()
        table = output['tables'][0]
        table['rows'] = table['rows'][:1]
        table['rows'][0]['name'] = '客户组'
        table['rows'][0]['values'] = ['1,234', '56', '20%']
        table['rows'][0]['excerpts'] = [dict(page=1, text='客户组 1,234 56 20%')]
        for column, label in zip(table['columns'], ['账面余额', '坏账准备', '计提比例(%)']):
            column.update(label=label, period='2025', kind='annual', as_of='2025-12-31')
        expanded = records.expand_compact_output(output)['records']
        self.assertEqual([r['object_key']['indicator'] for r in expanded],
                         ['客户组（账面余额）', '客户组（坏账准备）', '客户组（计提比例(%)）'])
        self.assertEqual(len({tuple(r['object_key'].values()) for r in expanded}), 3)


if __name__ == '__main__':
    unittest.main()
