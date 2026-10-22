"""Comparison protocol tests. All text and financial values are synthetic."""
from copy import deepcopy
import unittest

import records
import s2_compare as compare


def record(key='r1', value='125', page=2, source='synthetic-source', kind='metric', text=None):
    return dict(record_id=key, record_type=kind,
                source=dict(source_id=source), locator=dict(pdf_pages=[page]),
                excerpts=[dict(page=page, text=text or ('Revenue ' + str(value)))],
                numbers=[] if value is None else [records.number(value, '元')])


def item(key, text, fit='direct'):
    return dict(item_id=key, text=text, mapping=dict(fit=fit), context=[], headings=[])


class NumericTests(unittest.TestCase):
    def test_decimal_normalization_preserves_exact_long_integer(self):
        self.assertEqual(compare.canonical_number('1,250.5000'), '1250.5')
        self.assertEqual(compare.canonical_number('（123.00）'), '-123')
        self.assertEqual(compare.canonical_number('− 0.00'), '0')
        value = '1234567890123456789012345678901234567890'
        self.assertEqual(compare.canonical_number(value), value)
        self.assertIsNone(compare.canonical_number('NaN'))

    def test_chinese_numbers_citations_dates_and_negative_amounts(self):
        text = '收入125元，变化− 3.50%；损失（1,250）；日期2026年3月1日、2026-03-01。[S34 p2] 2026H1'
        self.assertEqual(compare.numbers_in_text(text), {'125', '-3.5', '-1250'})
        self.assertEqual(compare.numbers_in_text('收入125, next amount 2.0.'), {'125', '2'})

    def test_source_unit_magnitude_and_stored_value_both_available(self):
        r = record(value='125000000')
        r['numbers'] = [records.number('125000000', '元', source_unit='百万元', decimals=0)]
        self.assertEqual(compare.record_numbers(r), {'125000000', '125'})


class PilotTests(unittest.TestCase):
    def test_same_type_and_numeric_with_source_guard(self):
        left = [record('left')]
        self.assertTrue(compare.pilot_match(left, [record('right')])['passed'])
        self.assertFalse(compare.pilot_match(left, [record('right', kind='event')])['passed'])
        self.assertFalse(compare.pilot_match(left, [record('right', source='other')])['passed'])

    def test_exact_page_spans_allow_short_overlap_but_not_disjoint_quotes(self):
        a = record('left', None, text='alpha beta')
        b = record('right', None, text='beta gamma')
        page_map = {('synthetic-source', 2): 'alpha beta gamma delta'}
        found = compare.pilot_match([a], [b], page_map)
        self.assertTrue(found['passed'])
        self.assertEqual(found['matches'][0]['excerpt_overlap']['method'], 'frozen_page_span')
        b['excerpts'][0]['text'] = 'gamma delta'
        self.assertFalse(compare.pilot_match([a], [b], page_map)['passed'])
        b['excerpts'][0] = dict(page=3, text='alpha beta')
        self.assertFalse(compare.pilot_match([a], [b], page_map)['passed'])

    def test_unmatched_both_sides_and_eighty_percent_gate(self):
        refs = [record('a%d' % n, str(100+n)) for n in range(5)]
        found = compare.pilot_match(refs, [record('b%d' % n, str(100+n)) for n in range(4)])
        self.assertEqual(found['reference_match_ratio'], .8)
        self.assertTrue(found['passed'])
        self.assertEqual([r['record_id'] for r in found['unmatched_reference']], ['a4'])
        found = compare.pilot_match(refs, [record('other', '999')])
        self.assertEqual(len(found['unmatched_extracted']), 1)


class InventoryTests(unittest.TestCase):
    def test_inventory_denominator_source_adjacent_page_and_numeric_requirement(self):
        inv = [item('S34.c1.I1', 'Revenue 125 [S34 p3]'),
               item('S34.c1.I2', 'Revenue 125 [S34 p8]', 'revised'),
               item('S34.c1.I3', 'qualitative fact [S34 p2]'),
               item('S34.c1.I4', 'not a record', 'non_l1'),
               item('S42.c1.I1', 'Revenue 125 [S42 p2]')]
        result = compare.compare_inventory(inv, [record()], {'synthetic-source': 'S34'})
        self.assertEqual(result['item_count'], 4)
        self.assertEqual([r['status'] for r in result['items']], ['代码匹配', '未匹配', '候选', '未匹配'])

    def test_only_latest_caption_supplies_inherited_pages(self):
        row = item('S34.c1.I1', '| Income | 125 |')
        row['context'] = [dict(text='old caption [S34 p1]'), dict(text='new caption [S34 pp4–5]')]
        self.assertEqual(compare.inventory_pages(row, 'S34'), [4, 5])
        self.assertEqual(compare.parse_pages('[S34 p1, p3; S42 pp2–4]', 'S34'), [1, 3])
        self.assertEqual(compare.parse_pages('[S34 p1; S42 pp2–4]', 'S42'), [2, 3, 4])

    def test_choice_responsibility_requires_excluded_pages_and_value_match(self):
        inv = [item('S34.c1.I1', 'Revenue 125 [S34 p4]'), item('S34.c1.I2', 'Revenue 125 [S34 p8]')]
        result = compare.compare_inventory(inv, [], excluded_pages={'S34': [4]}, choice_records=[record('c1')])
        self.assertEqual([r['status'] for r in result['items']], ['由Choice承担', '未匹配'])
        self.assertEqual(result['items'][0]['choice_matches'][0]['record_id'], 'c1')

    def test_incomplete_extraction_is_pending_and_not_a_claim_of_missing_coverage(self):
        result = compare.compare_inventory([item('S34.c1.I1', 'Revenue 125 [S34 p4]')], [], extraction_complete=False)
        self.assertEqual(result['evaluation_status'], 'pending_extraction')
        self.assertEqual(result['items'][0]['status'], '待抽取')
        queues = compare.make_review_queues(result, [])
        self.assertEqual(queues['review_status'], 'not_ready')
        self.assertEqual(queues['queues']['unmatched'], [])

    def test_completed_announcement_sources_remain_reviewable_during_partial_run(self):
        inv = [item('S34.c1.I1', 'Revenue 987 [S34 p2]'),
               item('S46.c1.I1', 'Revenue 987 [S46 p2]'),
               item('S46.c1.I2', 'qualitative fact [S46 p2]'),
               item('S48.c1.I1', 'Revenue 987 [S48 p2]'),
               item('S50.c1.I1', 'Revenue 987 [S50 p2]')]
        recs = [record(source='S46')]
        result = compare.compare_inventory(inv, recs, extraction_complete=False,
                                           completed_sources=['S50', 'S46', 'S48'])
        self.assertEqual(result['evaluation_status'], 'partial_sources')
        self.assertFalse(result['extraction_complete'])
        self.assertEqual(result['completed_sources'], ['S46', 'S48', 'S50'])
        self.assertEqual([row['status'] for row in result['items']],
                         ['待抽取', '未匹配', '候选', '未匹配', '未匹配'])
        self.assertEqual([row['source_extraction_complete'] for row in result['items']],
                         [False, True, True, True, True])
        self.assertTrue(result['reverse_unmatched'][0]['source_extraction_complete'])
        queues = compare.make_review_queues(result, recs)
        self.assertEqual(queues['review_status'], 'partial_ready')
        self.assertEqual(len(queues['queues']['unmatched']), 4)
        self.assertTrue(all(row['source'] != 'S34' for row in queues['queues']['unmatched']))
        self.assertTrue(all(row['review']['status'] == 'pending' for row in queues['queues']['unmatched']))

    def test_completed_source_aliases_and_full_run_flags(self):
        inv = [item('S46.c1.I1', 'Revenue 987 [S46 p2]')]
        partial = compare.compare_inventory(inv, [], {'synthetic-full-source': 'S46'},
                    extraction_complete=False, completed_sources=['synthetic-full-source'])
        self.assertEqual(partial['completed_sources'], ['S46'])
        self.assertEqual(partial['items'][0]['status'], '未匹配')
        full = compare.compare_inventory(inv, [], extraction_complete=True)
        self.assertEqual(full['completed_sources'], ['S46'])
        self.assertTrue(full['items'][0]['source_extraction_complete'])
        self.assertEqual(compare.make_review_queues(full, [])['review_status'], 'pending')

    def test_ready_pages_enable_partial_source_readback_but_never_infer_neighbor_readiness(self):
        inv = [item('S34.c1.I1', 'Value 987 [S34 p2]'),
               item('S34.c1.I2', 'Value 987 [S34 pp2–3]'),
               item('S34.c1.I3', 'Value 125 [S34 p3]'),
               item('S34.c1.I4', 'qualitative fact [S34 p2]')]
        recs = [record(source='synthetic-full-source', page=2)]
        result = compare.compare_inventory(inv, recs, {'synthetic-full-source': 'S34'},
            extraction_complete=False, ready_pages_by_source={'synthetic-full-source': [2]})
        self.assertEqual(result['evaluation_status'], 'partial_regions')
        self.assertEqual([r['status'] for r in result['items']], ['未匹配', '待抽取', '代码匹配', '候选'])
        self.assertTrue(all(not r['source_extraction_complete'] for r in result['items']))
        self.assertEqual([r['comparison_region_complete'] for r in result['items']], [True, False, False, True])
        self.assertEqual(result['region_summary']['partial_region_complete_items'], 2)
        self.assertEqual(result['region_summary']['ready_unmatched_items'], 2)
        self.assertEqual(result['region_summary']['unready_code_match_items'], 1)
        queues = compare.make_review_queues(result, recs, source_aliases={'synthetic-full-source': 'S34'})
        self.assertEqual(queues['review_status'], 'partial_ready')
        self.assertEqual([row['item_id'] for row in queues['queues']['unmatched']], ['S34.c1.I1', 'S34.c1.I4'])

    def test_fully_excluded_pages_keep_choice_rules_and_full_run_behavior_is_unchanged(self):
        inv = [item('S34.c1.I1', 'Value 125 [S34 p4]'), item('S34.c1.I2', 'Value 987 [S34 p4]')]
        partial = compare.compare_inventory(inv, [], excluded_pages={'S34': [4]}, choice_records=[record('choice')],
            extraction_complete=False, ready_pages_by_source={'S34': [2, 4]})
        self.assertEqual([r['status'] for r in partial['items']], ['由Choice承担', '待抽取'])
        self.assertEqual(partial['ready_pages_by_source'], {'S34': [2]})
        self.assertTrue(all(not r['comparison_region_complete'] for r in partial['items']))
        original = compare.compare_inventory(inv, [], excluded_pages={'S34': [4]}, choice_records=[record('choice')])
        full = compare.compare_inventory(inv, [], excluded_pages={'S34': [4]}, choice_records=[record('choice')],
                                         ready_pages_by_source={'S34': [2]})
        self.assertEqual([r['status'] for r in full['items']], [r['status'] for r in original['items']])
        self.assertTrue(all(r['comparison_region_complete'] and r['source_extraction_complete'] for r in full['items']))

    def test_sixty_large_source_reviews_combine_ready_s34_region_with_completed_s42(self):
        inv = [item('S34.c1.I%03d' % n, 'Value 987 [S34 p2]') for n in range(70)]
        inv += [item('S34.c2.I%03d' % n, 'Value 987 [S34 p3]') for n in range(70)]
        inv += [item('S42.c1.I%03d' % n, 'Value 987 [S42 p2]') for n in range(20)]
        result = compare.compare_inventory(inv, [], extraction_complete=False, completed_sources=['S42'],
                                           ready_pages_by_source={'S34': [2]})
        chunks = [dict(id='S34.ready', source='S34', core_pages=[2]),
                  dict(id='S34.pending', source='S34', core_pages=[3]),
                  dict(id='S42.full', source='S42', core_pages=[2])]
        queues = compare.make_review_queues(result, [], chunks)
        sampled = queues['queues']['unmatched']
        self.assertEqual(len(sampled), 60)
        self.assertEqual(queues['population_counts']['unmatched_large'], 90)
        self.assertEqual({row['source'] for row in sampled}, {'S34', 'S42'})
        self.assertTrue(all(row['pages'] == [2] and row['comparison_region_complete'] for row in sampled))


class ReviewTests(unittest.TestCase):
    def test_boundary_page_uses_largest_core_span_and_ignores_context(self):
        row = dict(source='S34', pages=[5])
        chunks = [dict(id='S34.previous', source='S34', core_pages=[4, 5],
                       fragments=[dict(page=4, start=0, end=900), dict(page=5, start=0, end=65)]),
                  dict(id='S34.section2', source='S34', core_pages=[5, 6],
                       fragments=[dict(page=5, start=65, end=1797), dict(page=6, start=0, end=30)]),
                  dict(id='S34.context-only', source='S34', core_pages=[7],
                       fragments=[dict(page=7, start=0, end=500)],
                       context_pages=[5], context_fragments=[dict(page=5, start=0, end=4000)])]
        self.assertEqual(compare._chunk_for(row, chunks), 'S34.section2')
        self.assertEqual(compare._chunk_for(row, [chunks[-1]]), 'unassigned')
        # Even a legacy sources.pages list must not override explicit empty core.
        context = dict(id='S34.empty-core', source='S34', core_pages=[], fragments=[],
                       sources=[dict(id='S34', pages=[5])], context_pages=[5])
        self.assertEqual(compare._chunk_for(row, [context]), 'unassigned')

    def test_core_overlap_sums_pages_and_ties_keep_plan_order(self):
        row = dict(source='S34', pages=[2, 3])
        chunks = [dict(id='first', source='S34', core_pages=[2, 3],
                       fragments=[dict(page=2, text='a' * 10), dict(page=3, text='b' * 15)]),
                  dict(id='second', source='S34', core_pages=[2],
                       fragments=[dict(page=2, text='c' * 25)])]
        self.assertEqual(compare._chunk_for(row, chunks), 'first')
        self.assertEqual(compare._chunk_for(row, list(reversed(chunks))), 'second')
        simple = [dict(id='one-page', source='S34', core_pages=[2]),
                  dict(id='two-pages', source='S34', core_pages=[2, 3], context_pages=[4])]
        self.assertEqual(compare._chunk_for(row, simple), 'two-pages')

    def test_samples_are_reproducible_and_stratified(self):
        rows = [dict(item_id='i%03d' % n, source='S34' if n % 2 else 'S42', chunk='c%d' % (n % 7))
                for n in range(150)]
        key = lambda row: (row['source'], row['chunk'])
        a = compare.stratified_sample(rows, 60, key, 99)
        b = compare.stratified_sample(list(reversed(rows)), 60, key, 99)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 60)
        self.assertEqual({key(r) for r in a}, {key(r) for r in rows})

    def test_other_stratum_growth_preserves_internal_ranking_when_quota_is_available(self):
        rows = [dict(item_id='%s.%02d' % (source, n), source=source)
                for source in ('S34', 'S42', 'S46') for n in range(20)]
        expanded = rows + [dict(item_id='S34.%02d' % n, source='S34') for n in range(20, 70)]
        key = lambda row: (row['source'], 'metric')
        for size in (3, 6, 12, 30):
            first = compare.stratified_sample(rows, size, key, 20261024)
            later = compare.stratified_sample(expanded, size, key, 20261024)
            for source in ('S42', 'S46'):
                before_ids = [row['item_id'] for row in first if row['source'] == source]
                after_ids = [row['item_id'] for row in later if row['source'] == source]
                self.assertEqual(len(before_ids), size // 3)
                self.assertEqual(before_ids, after_ids)
            self.assertEqual(later, compare.stratified_sample(list(reversed(expanded)), size, key, 20261024))

    def test_sampling_metadata_binds_algorithm_and_independent_stratum_seeds(self):
        inv = [item('S34.c1.I%d' % n, 'Value 987 [S34 p2]') for n in range(3)]
        recs = [record('r%d' % n, str(2000 + n)) for n in range(3)]
        result = compare.compare_inventory(inv, recs, {'synthetic-source': 'S34'})
        queues = compare.make_review_queues(result, recs, source_aliases={'synthetic-source': 'S34'})
        self.assertEqual(queues['seeds'], dict(unmatched=20261022, reverse=20261023, semantic=20261024))
        self.assertEqual(queues['sampling']['algorithm'], 'per-stratum-sha256-v1')
        self.assertIn('SHA256', queues['sampling']['stratum_seed_rule'])
        semantic = queues['sampling']['strata']['semantic']
        self.assertEqual(semantic, [dict(key=('S34', 'metric'),
            seed=compare._stratum_seed(20261024, ('S34', 'metric')))])
        self.assertNotEqual(semantic[0]['seed'], queues['sampling']['strata']['reverse'][0]['seed'])
        self.assertEqual(compare._stratum_seed(20261024, ('S34', 'metric')),
                         compare._stratum_seed(20261024, ['S34', 'metric']))

    def test_announcement_unmatched_all_large_sixty_reverse_forty_semantic_eighty(self):
        inv = [item('S34.c1.I%d' % n, 'Value 987 [S34 p2]') for n in range(90)]
        inv += [item('S46.c1.I%d' % n, 'Value 987 [S46 p2]') for n in range(7)]
        recs = [record('r%d' % n, str(2000+n)) for n in range(100)]
        result = compare.compare_inventory(inv, recs, {'synthetic-source': 'S34'})
        queues = compare.make_review_queues(result, recs, source_aliases={'synthetic-source': 'S34'})
        self.assertEqual(queues['actual_counts'], dict(unmatched=67, reverse=40, semantic=80))
        self.assertTrue(all(row['review']['status'] == 'pending' for row in queues['queues']['semantic']))

    def test_review_requires_queue_binding_and_error_evidence(self):
        result = compare.compare_inventory([], [record()])
        queues = compare.make_review_queues(result, [record()])
        review = dict(queues_sha256=compare.json_hash(queues), reviewer='synthetic reviewer', review_id='review-1',
                      decisions=[dict(queue='semantic', id='r1', decision='错误', reason='wrong period',
                                      error_types=['期间'], evidence_pages=[2])])
        updated = compare.apply_reviews(queues, review)
        self.assertEqual(updated['review_summary']['semantic']['error_rate'], 1)
        wrong = deepcopy(review)
        wrong['queues_sha256'] = 'wrong'
        with self.assertRaises(ValueError):
            compare.apply_reviews(queues, wrong)
        wrong = deepcopy(review)
        wrong['decisions'][0]['error_types'] = []
        with self.assertRaises(ValueError):
            compare.apply_reviews(queues, wrong)


class UsageTests(unittest.TestCase):
    def test_unknown_failures_are_not_measured_zero_and_reasoning_not_added(self):
        success = dict(stage='extract-a', status='completed', usage=dict(input_tokens=1000, cached_input_tokens=800,
                      output_tokens=100, reasoning_output_tokens=20), tools=[dict(type='command_execution')], seconds=10)
        failure = dict(stage='extract-b', status='timed_out', usage={}, tools=[], seconds=30)
        result = compare.usage_comparison([success, failure], [failure], source_chars=0, accepted_records=0)
        p1 = result['p1']['extraction_only']
        self.assertEqual(p1['input'], 1000)
        self.assertEqual(p1['input_unknown_calls'], 1)
        self.assertEqual(p1['output'], 100)
        self.assertEqual(p1['uncached'], 200)
        self.assertIsNone(result['s2']['all']['input'])
        self.assertIsNone(result['s2']['ratios']['input_per_10000_source_chars'])
        self.assertIsNone(result['s2']['output_tokens_per_accepted_record'])

    def test_cli_completed_but_output_failed_is_not_success(self):
        attempt = dict(stage='extract-a', status='completed', s2_status='invalid_output', usage={}, seconds=2)
        result = compare.usage_comparison([], [attempt], source_chars=100, accepted_records=0)
        self.assertEqual(result['s2']['success']['calls'], 0)
        self.assertEqual(result['s2']['failed']['calls'], 1)
        self.assertEqual(result['s2_rows'][0]['status'], 'invalid_output')
        self.assertIn('未知', compare.usage_markdown(result))


if __name__ == '__main__':
    unittest.main()
