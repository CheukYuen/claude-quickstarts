"""Synthetic evidence only: never include licensed/source-report text in tests."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import records


def sample():
    return records.make_record(
        'metric', 'r1', 'synthetic-report', 'ExampleCo', 'revenue', '2025',
        '2026-03-01', '2025-12-31', [dict(page=2, text='Revenue 1,250.50')],
        dict(scope='consolidated', scope_note='Synthetic group', value_status='reported', value_note=''),
        numbers=[records.number('1250500', '元', 'CNY', '千元', 2)],
        period=dict(kind='annual', label='2025'))


class RecordTests(unittest.TestCase):
    def test_schema_is_generated_from_runtime_definition(self):
        actual = json.loads((Path(__file__).parent / 'schemas/extract-output.json').read_text())
        self.assertEqual(actual, records.extract_output_schema())

    def test_fields_and_whitespace_normalized_verbatim_quote(self):
        pages = {('synthetic-report', 2): 'Title\n Revenue\n  1,250.50\nfooter'}
        self.assertEqual(records.validate_record(sample(), pages), [])

    def test_wrong_page_rejects_even_if_quote_is_on_another_page(self):
        pages = {('synthetic-report', 1): 'Revenue 1,250.50', ('synthetic-report', 2): 'Other page'}
        self.assertTrue(any('absent from frozen' in x for x in records.validate_record(sample(), pages)))

    def test_all_locator_pages_must_exist_even_without_an_excerpt(self):
        item = sample()
        item['locator']['pdf_pages'] = [2, 999]
        pages = {('synthetic-report', 2): 'Revenue 1,250.50'}
        self.assertTrue(any('locator.pdf_pages' in x and '999' in x for x in records.validate_record(item, pages)))
        item['locator']['pdf_pages'] = [1, 2]
        pages[('synthetic-report', 1)] = 'Table header'
        self.assertEqual(records.validate_record(item, pages), [])

    def test_fabricated_joined_excerpt_rejected(self):
        item = sample()
        item['excerpts'][0]['text'] = 'Revenue footer'
        pages = {('synthetic-report', 2): 'Revenue 1,250.50 footer'}
        errors = records.validate_record(item, pages)
        self.assertTrue(any('excerpt is absent' in x for x in errors))

    def test_exact_number_unit_conversion(self):
        for value, unit, source_unit, decimals, text in (
                ('150000000', '元', '亿元', 2, 'Amount 1.50'),
                ('150000000', '元', '百万元', 0, 'Amount 150'),
                ('150000', '千元', '百万元', 1, 'Amount 150.0'),
                ('1.25', '亿元', '万元', 0, 'Amount 12,500'),
                ('10000000', '芯公里', '万芯公里', 0, 'Length 1,000'),
                ('7000000', '股', '万股', 0, 'Shares 700'),
                ('200000000', 'GB', '亿GB', 0, 'Traffic 2'),
                ('25000', '份额', '万份额', 1, 'Interests 2.5'),
                ('200000000', '芯公里', '亿芯公里', 0, 'Length 2')):
            with self.subTest(value=value, unit=unit):
                self.assertTrue(records.number_in_excerpt(records.number(value, unit, source_unit=source_unit,
                                                                         decimals=decimals), text))

    def test_review_discovered_units_and_currencies_are_legal(self):
        for unit, value in (('dB/km', '0.04'), ('GB', '120'), ('亿GB', '2.5'), ('份额', '100'),
                            ('万份额', '2.5'), ('人', '500'), ('户', '250'), ('基点', '100')):
            item = sample()
            item['numbers'] = [records.number(value, unit)]
            item['excerpts'] = [dict(page=2, text='Synthetic measure ' + value)]
            self.assertEqual(records.validate_record(item), [], unit)
        for currency in ('KRW', 'THB'):
            item = sample()
            item['numbers'][0]['currency'] = currency
            self.assertEqual(records.validate_record(item), [], currency)
        with self.assertRaises(ValueError):
            records.formatted_number(records.number('25000', '份额', source_unit='万股'))

    def test_parenthesized_negatives_and_signed_values(self):
        item = records.number('-1250.5', '千元', decimals=1)
        for text in ('Cost (1,250.5)', 'Cost （1,250.5）', 'Cost -1,250.5', 'Cost −1,250.5'):
            self.assertTrue(records.number_in_excerpt(item, text))
        self.assertFalse(records.number_in_excerpt(records.number('1250.5', '千元'), 'Cost (1,250.5)'))

    def test_numeric_boundaries_and_adjacent_table_columns(self):
        item = records.number('50', '元')
        for text in ('150', '50.5', '1,050', '-50', '(50)', '（50）', '- 50', '( 50 )'):
            self.assertFalse(records.number_in_excerpt(item, text), text)
        self.assertTrue(records.number_in_excerpt(item, '40 50 60'))
        self.assertTrue(records.number_in_excerpt(item, '50, plus a fee'))
        self.assertTrue(records.number_in_excerpt(records.number('1.157', 'HKD/CNY'), 'Ratio 1.157. Next sentence.'))

    def test_wrong_decimals_incompatible_units_and_precision_loss(self):
        self.assertFalse(records.number_in_excerpt(records.number('1250.5', '元', decimals=1), '1,250.50'))
        with self.assertRaises(ValueError):
            records.formatted_number(records.number('1250', '元', source_unit='吨'))
        with self.assertRaises(ValueError):
            records.formatted_number(records.number('1250.5', '元', decimals=0))

    def test_null_date_requires_explanation_and_calendar_validity(self):
        item = sample()
        item['as_of'] = None
        self.assertTrue(any('null date' in x for x in records.validate_record(item)))
        item['date_notes'] = [dict(field='as_of', reason='No reference date disclosed')]
        self.assertEqual(records.validate_record(item), [])
        item['disclosed_on'] = '2025-02-30'
        self.assertTrue(any('invalid ISO date' in x for x in records.validate_record(item)))

    def test_obligation_unknown_trigger_and_deadline_dates_stay_explicit(self):
        item = records.make_record('obligation', 'o1', 'synthetic-contract', 'ExampleBuyer',
            'production-obligation', 'transaction-1', '2026-03-01', '2026-03-01',
            [dict(page=1, text='Buyer shall begin production within six months after transfer.')],
            dict(obligor='ExampleBuyer', beneficiary=None, action='Cause production to begin',
                 deadline=dict(amount=6, unit='months', from_event='transfer', from_date=None, deadline_on=None),
                 conditions=['Transfer occurs'], consequences=[], payment_status='unspecified'))
        errors = records.validate_record(item)
        self.assertTrue(any('detail.deadline.from_date' in x for x in errors))
        self.assertTrue(any('detail.deadline.deadline_on' in x for x in errors))
        item['date_notes'] = [dict(field='detail.deadline.from_date', reason='Transfer date not given'),
                              dict(field='detail.deadline.deadline_on', reason='Do not calculate without trigger date')]
        self.assertEqual(records.validate_record(item), [])
        item['detail']['deadline']['amount'] = 99
        self.assertTrue(any('detail.deadline.amount' in x for x in records.validate_record(item)))

    def test_deadline_amount_requires_matching_units_in_one_phrase(self):
        def deadline(amount, unit):
            return dict(amount=amount, unit=unit, from_event='transfer', from_date=None, deadline_on=None)
        for amount, unit, text in (
                (6, 'months', 'Within six months after transfer.'),
                (10, 'business_days', 'Within ten business days after confirmation.'),
                (2, 'years', 'For two years after signing.'),
                (5, 'calendar_days', 'Within five calendar days after receipt.'),
                (26, 'months', 'Within twenty-six months.'),
                (6, 'months', 'Within six (6) months.'),
                (6, 'months', 'Within 6 months.'),
                (6, 'months', '过户后六个月内。'),
                (10, 'business_days', '确认后十个工作日内。'),
                (2, 'years', '签约后两年内。')):
            self.assertTrue(records.deadline_in_excerpt(deadline(amount, unit), text), text)
        for amount, unit, text in (
                (99, 'months', 'Within six months after transfer.'),
                (6, 'months', 'Pay 6 million. Production starts in ten months.'),
                (6, 'months', 'Within six business days.'),
                (6, 'months', 'Within (6) months.'),
                (6, 'months', 'Within - 6 months.'),
                (1, 'months', 'someone months'),
                (6, 'months', 'Within twenty-six months.'),
                (6, 'months', 'Occurred on 2026年6月30日。'),
                (30, 'calendar_days', 'Occurred on 2026年6月30日。'),
                (2026, 'years', 'Occurred in 2026年。'),
                (6, 'months', 'Occurred on 二〇二六年六月三十日。'),
                (30, 'calendar_days', 'Occurred on 六月三十日。'),
                (6, 'months', 'Occurred on 2026-06-30.')):
            self.assertFalse(records.deadline_in_excerpt(deadline(amount, unit), text), text)

    def test_enum_and_unknown_fields_rejected(self):
        for field, value in (('nature', 'guessed'), ('is_market', 0), ('surprise', 'extra')):
            item = sample()
            item[field] = value
            self.assertTrue(records.validate_record(item), field)

    def test_choice_exception_needs_vendor_provenance(self):
        item = sample()
        item.update(source=dict(kind='choice', source_id='choice-synthetic', choice_field='REVENUE', report_period='2025-12-31'),
                    excerpts=[], locator=dict(pdf_pages=[], table='IncomeStatement', section=None),
                    verification=dict(status='未查值', note='Corresponding report absent'))
        self.assertEqual(records.validate_record(item, {}), [])
        item['source']['choice_field'] = None
        self.assertTrue(records.validate_record(item, {}))

    def test_attributed_forecast_and_market_flags(self):
        item = records.make_record('statement', 's1', 'synthetic-report', 'ExampleCo', 'profit-forecast', '2027',
            '2026-03-01', '2027-12-31', [dict(page=1, text='Model forecast 120.0')],
            dict(speaker='Example Research', content='Forecast', statement_type='forecast', attribution_note=''),
            numbers=[records.number('120.0', '百万元', 'CNY')], nature='secondary')
        self.assertTrue(any('common_origin' in x for x in records.validate_record(item)))
        item['origin'] = dict(speaker='Example Research', original_source='Synthetic Forecast Survey', common_origin_id='survey-2026')
        self.assertEqual(records.validate_record(item), [])
        item['detail']['statement_type'] = 'market_target'
        self.assertTrue(any('market content' in x for x in records.validate_record(item)))

    def test_coverage_is_auditable_and_exact(self):
        output = dict(schema_version=records.VERSION, records=[sample()], coverage=[
            dict(item_id='revenue', status='已抽', record_ids=['r1'], checked_pages=[2], basis='Synthetic revenue row'),
            dict(item_id='guarantees', status='本文档无', record_ids=[], checked_pages=[3], basis='Synthetic no-guarantee disclosure')])
        checklist = dict(items=[dict(id='revenue'), dict(id='guarantees')])
        self.assertEqual(records.validate_output(output, checklist=checklist), [])
        output['coverage'][1]['checked_pages'] = []
        self.assertTrue(records.validate_output(output, checklist=checklist))
        output['coverage'][1]['checked_pages'] = [3]
        output['coverage'][0]['record_ids'] = ['missing']
        self.assertTrue(records.validate_output(output, checklist=checklist))

    def test_each_checklist_item_has_key_types_and_absence_evidence_rule(self):
        for path in (Path(__file__).parent / 'checklists').glob('*.json'):
            checklist = json.loads(path.read_text())
            ids = [item['id'] for item in checklist['items']]
            self.assertEqual(len(ids), len(set(ids)), path.name)
            for item in checklist['items']:
                self.assertTrue(item['name'])
                self.assertTrue(item['absence_basis'])
                self.assertTrue(set(item['record_types']).issubset(records.DETAILS))
                self.assertTrue(item['object_keys'])
                for key in item['object_keys']:
                    self.assertEqual(set(key), {'subject', 'indicator', 'period'})

    def test_later_disclosure_covers_old_record_without_mutating_it(self):
        old = sample()
        later = deepcopy(old)
        later.update(record_id='r2', disclosed_on='2026-04-01')
        before = deepcopy([old, later])
        self.assertEqual(records.coverage_relations([old, later]), [dict(record_id='r1', covered_by=['r2'])])
        self.assertEqual([old, later], before)
        self.assertEqual(records.coverage_relations([old, later], '2026-03-15'), [])
        later['disclosed_on'] = old['disclosed_on']
        self.assertEqual(records.coverage_relations([old, later]), [])

    def test_segment_dimensions_and_scope_do_not_cover_each_other(self):
        old = sample()
        new = deepcopy(old)
        new.update(record_id='r2', disclosed_on='2026-04-01')
        old['dimensions'] = [dict(name='revenue_scope', value='external')]
        new['dimensions'] = [dict(name='revenue_scope', value='intersegment')]
        self.assertEqual(records.coverage_relations([old, new]), [])
        old['dimensions'] = new['dimensions'] = []
        new['detail']['scope'] = 'parent'
        self.assertEqual(records.coverage_relations([old, new]), [])

    def test_actual_and_forecast_keep_separate_coverage_chains(self):
        old = sample()
        new = deepcopy(old)
        new.update(record_id='r2', disclosed_on='2026-04-01', nature='institution_forecast')
        self.assertEqual(records.coverage_relations([old, new]), [])

    def test_record_types_keep_separate_coverage_chains(self):
        old = records.make_record('event', 'e1', 'synthetic-report', 'ExampleCo', 'status', '2026',
            '2026-03-01', '2026-03-01', [dict(page=1, text='Transfer completed')],
            dict(event_type='transfer', occurred_on='2026-03-01', stage='completed', description='Transfer completed'))
        new = deepcopy(old)
        new.update(record_id='s2', disclosed_on='2026-04-01', record_type='statement',
                   detail=dict(speaker='ExampleCo', content='Transfer completed', statement_type='explanation', attribution_note=''))
        self.assertEqual(records.coverage_relations([old, new]), [])

    def test_different_institution_forecasts_do_not_cover_each_other(self):
        old = sample()
        old.update(nature='institution_forecast',
                   origin=dict(speaker='Research A', common_origin_id=None, original_source=None))
        new = deepcopy(old)
        new.update(record_id='r2', disclosed_on='2026-04-01')
        new['origin']['speaker'] = 'Research B'
        self.assertEqual(records.coverage_relations([old, new]), [])
        new['origin']['speaker'] = 'Research A'
        self.assertEqual(records.coverage_relations([old, new]), [dict(record_id='r1', covered_by=['r2'])])

    def test_nonstatement_institution_forecast_requires_a_named_speaker(self):
        item = sample()
        item['nature'] = 'institution_forecast'
        for speaker in (None, '', '   '):
            item['origin']['speaker'] = speaker
            self.assertTrue(any('requires origin.speaker' in x for x in records.validate_record(item)), speaker)
        item['origin']['speaker'] = 'Example Research'
        self.assertEqual(records.validate_record(item), [])

    def test_statements_keep_speaker_and_original_source_in_coverage_key(self):
        old = records.make_record('statement', 's1', 'synthetic-report', 'ExampleCo', 'demand', '2027',
            '2026-03-01', '2027-12-31', [dict(page=1, text='Demand forecast')],
            dict(speaker='Survey Publisher', content='Demand forecast', statement_type='forecast', attribution_note=''),
            nature='secondary', origin=dict(speaker='Survey Publisher', original_source='Survey 2026', common_origin_id='survey-2026'))
        new = deepcopy(old)
        new.update(record_id='s2', disclosed_on='2026-04-01')
        for field, new_value in (('speaker', 'Other Publisher'), ('original_source', 'Other Survey'),
                                 ('common_origin_id', 'other-common-origin')):
            changed = deepcopy(new)
            if field == 'speaker':
                changed['detail']['speaker'] = new_value
            else:
                changed['origin'][field] = new_value
            self.assertEqual(records.coverage_relations([old, changed]), [], field)
        self.assertEqual(records.coverage_relations([old, new]), [dict(record_id='s1', covered_by=['s2'])])

    def test_unit_clarification_can_cover_earlier_capacity_wording(self):
        old = sample()
        old.update(numbers=[records.number('400', '吨')],
                   excerpts=[dict(page=2, text='Capacity 400')])
        new = deepcopy(old)
        new.update(record_id='r2', disclosed_on='2026-04-01', numbers=[records.number('400', '吨/年')])
        self.assertEqual(records.coverage_relations([old, new]), [dict(record_id='r1', covered_by=['r2'])])

    def test_na_nm_metrics_require_nonblank_value_note(self):
        for status in ('not_available', 'not_meaningful'):
            item = sample()
            item['numbers'] = []
            item['detail']['value_status'] = status
            for note in ('', '   '):
                item['detail']['value_note'] = note
                self.assertTrue(records.validate_record(item), (status, note))
            item['detail']['value_note'] = 'Synthetic source displays NA/NM; do not substitute zero'
            self.assertEqual(records.validate_record(item), [])


if __name__ == '__main__':
    unittest.main()
