"""Synthetic Choice positions and deterministic S2b samples; no source text."""
from copy import deepcopy
import unittest
from unittest import mock

import records
import s2_choice
import s2_qa
from test_s2_choice import query, field_map, binding
from test_s2_qa import metric


class ChoiceNamesTests(unittest.TestCase):
    def renamed_map(self):
        mapping = field_map()
        mapping['s2b_naming'] = {'version': 'synthetic-1'}
        mapping['fields'][0].update(record_indicator='主表/收入',
                                    conversion_policy='emit',
                                    conversion_basis='Synthetic main position')
        mapping['fields'][1].update(record_indicator='补充资料/收入',
                                    conversion_policy='skip',
                                    conversion_basis='Synthetic old duplicate suppressed')
        return mapping

    def test_record_indicator_and_suppression_do_not_change_printed_name(self):
        mapping = self.renamed_map()
        result = s2_choice.convert_payload({'queries': [query()]}, binding(), mapping, index={})
        self.assertEqual(mapping['fields'][0]['original_table_item'], '营业收入')
        self.assertEqual(result['records'][0]['object_key']['indicator'], '主表/收入')
        self.assertEqual(result['summary']['suppressed_duplicate_field_periods'], 1)
        self.assertEqual(result['summary']['field_period_opportunities'], 1)
        self.assertEqual(result['summary']['same_key_conflicts'], 0)
        self.assertEqual(result['summary']['empty_original_values'], 0)
        self.assertIn('s2b-choice', result['records'][0]['extraction_version'])

    def test_duplicate_emitted_indicator_and_undocumented_skip_refused(self):
        mapping = self.renamed_map()
        mapping['fields'][1].update(conversion_policy='emit', record_indicator='主表/收入')
        with self.assertRaisesRegex(ValueError, 'Duplicate emitted'):
            s2_choice.convert_payload({'queries': [query()]}, binding(), mapping, index={})
        mapping = self.renamed_map()
        del mapping['fields'][1]['conversion_basis']
        with self.assertRaisesRegex(ValueError, 'conversion_basis'):
            s2_choice.validate_conversion_names(mapping)

    def test_different_positions_are_both_emitted_even_with_equal_numbers(self):
        mapping = self.renamed_map()
        mapping['fields'][1]['conversion_policy'] = 'emit'
        capture = query()
        capture['result']['Data']['SYN'][capture['result']['Indicators'].index('EMPTY')] = 125
        result = s2_choice.convert_payload({'queries': [capture]}, binding(), mapping, index={})
        self.assertEqual(len(result['records']), 2)
        self.assertEqual(result['summary']['same_key_conflicts'], 0)
        self.assertEqual({r['object_key']['indicator'] for r in result['records']},
                         {'主表/收入', '补充资料/收入'})


class SemanticSampleTests(unittest.TestCase):
    def population(self):
        return {chunk: [metric(f'{chunk}-{i}', str(10+i)) for i in range(12)]
                for chunk in ('b1', 'b2', 'b3', 'b4', 'b5', 'b6')}

    def test_thirty_stratified_deterministic_order_independent_and_nonmutating(self):
        population = self.population()
        before = deepcopy(population)
        result = s2_qa.semantic_sample(population)
        reordered = {k: list(reversed(v)) for k, v in reversed(list(population.items()))}
        self.assertEqual(result, s2_qa.semantic_sample(reordered))
        self.assertEqual(result['selected'], 30)
        self.assertEqual(set(result['selected_by_chunk'].values()), {5})
        self.assertEqual(population, before)
        self.assertTrue(all(r['review']['status'] == 'pending' for r in result['rows']))

    def test_population_binding_changes_with_record_and_duplicate_ids_rejected(self):
        population = self.population()
        before = s2_qa.semantic_sample(population)['population_sha256']
        population['b1'][0]['numbers'][0]['value'] = '999'
        self.assertNotEqual(before, s2_qa.semantic_sample(population)['population_sha256'])
        population['b2'][0]['record_id'] = population['b1'][0]['record_id']
        with self.assertRaisesRegex(ValueError, 'Repeated'):
            s2_qa.semantic_sample(population)


if __name__ == '__main__':
    unittest.main()
