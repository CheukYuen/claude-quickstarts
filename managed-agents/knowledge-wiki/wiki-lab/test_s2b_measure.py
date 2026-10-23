"""Synthetic S2b measurement regressions; no source excerpts embedded here."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import s2b_measure as m
import records


def record(rid='r1', value='123', kind='metric', quote='Synthetic revenue 123 units'):
    return dict(record_id=rid, record_type=kind, source=dict(source_id='synthetic'),
                locator=dict(pdf_pages=[1]), excerpts=[dict(page=1, text=quote)],
                numbers=[dict(value=value, unit='units', source_unit='units', decimals=0)],
                object_key=dict(subject='Example', indicator='revenue', period='2025'))


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old, self.new = self.root/'s2', self.root/'s2b'
        self.old.mkdir(); self.new.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_source_features_count_core_numeric_and_layout_rows(self):
        text = 'Synthetic heading 2025\nRevenue   123   456\nPlain 77 prose\n| Cost | 2 | 3 |'
        self.assertEqual(m.source_features(text), dict(characters=len(text), numeric_tokens=6, table_rows=2))
        chunk = dict(fragments=[dict(text='Core 1')], context_fragments=[dict(text='Header 999')])
        self.assertEqual(m.core_text(chunk), 'Core 1')

    def test_linear_fit_and_leave_one_out_predict_exact_line(self):
        rows = [dict(chunk_id=str(i), characters=i, numeric_tokens=i*i, table_rows=1, output_tokens=10+3*i)
                for i in range(1, 8)]
        fitted = m.fit_features(rows)
        self.assertEqual(fitted['best_feature'], 'characters')
        self.assertAlmostEqual(fitted['models']['characters']['coefficient'], 3)
        self.assertAlmostEqual(fitted['models']['characters']['intercept'], 10)
        self.assertAlmostEqual(fitted['models']['characters']['leave_one_out_mae'], 0)
        self.assertGreater(fitted['models']['numeric_tokens']['leave_one_out_mae'], 0)
        self.assertEqual(m.fit_features(rows[:2])['models'], {})

    def test_accepted_binding_wins_over_later_retry(self):
        calls = [dict(chunk_id='B', status='completed', call_number=1, log='first'),
                 dict(chunk_id='B', status='completed', call_number=2, log='second')]
        m.dump(self.old/'accepted/B.json', dict(log='first', records=[]))
        self.assertEqual(m.choose_attempt(self.old, 'B', calls)['call_number'], 1)
        m.dump(self.old/'accepted/B.json', dict(log='missing', records=[]))
        self.assertIsNone(m.choose_attempt(self.old, 'B', calls))

    def test_unknown_usage_stays_null(self):
        row = m.density_row(dict(stage='failed', status='failed', source_chars=10))
        self.assertIsNone(row['input'])
        self.assertIsNone(row['output_tokens_per_record'])
        self.assertIsNone(row['output_tokens_per_10000_characters'])
        self.assertIsNone(m.reduction(100, None))
        self.assertEqual(m.reduction(100, 20), .8)

    def test_costs_use_actual_seconds_and_only_paired_calls(self):
        chunk = 'S46.s01'
        old = dict(chunk_id=chunk, status='completed', usage=dict(input_tokens=100, cached_input_tokens=20,
                   output_tokens=1000, reasoning_output_tokens=100), seconds=632.43, source_chars=100,
                   chunk_sha256='same', log='a')
        new = dict(old, usage=dict(input_tokens=50, cached_input_tokens=0, output_tokens=200,
                   reasoning_output_tokens=10), seconds=100, log='b')
        for root, call in ((self.old, old), (self.new, new)):
            m.dump(root/'usage.json', dict(attempts=[call]))
            m.dump(root/'accepted'/f'{chunk}.json', dict(log=call['log'], records=[record()], rejected=[]))
        result = m.compare_costs(self.old, self.new)
        self.assertEqual(result['paired_blocks'], 1)
        self.assertAlmostEqual(result['paired_totals']['output_reduction'], .8)
        self.assertTrue(result['rows'][0]['same_core_input_hash'])
        mismatch = result['rows'][0]['kickoff_discrepancies']
        self.assertIn(dict(field='seconds', kickoff_claim=632, measured=632.43), mismatch)
        self.assertIsNone(result['rows'][1]['s2b'])

    def test_checklist_only_claimed_complete_items_and_exact_s2_match(self):
        ref = record()
        wrong_type = record('wrong', '123', kind='statement')
        same_numeric = record('match', '123', quote='Other unrelated synthetic excerpt')
        old = dict(records=[ref, record('partial', '999')], coverage=[
            dict(item_id='A01', status='已抽', record_ids=['r1']),
            dict(item_id='A02', status='部分', record_ids=['partial'])])
        m.dump(self.old/'accepted/S46.s01.json', old)
        m.dump(self.new/'accepted/S46.s01.json', dict(records=[wrong_type, same_numeric]))
        result = m.checklist_recall(self.old, self.new, {}, {})
        rows = result['blocks'][0]['rows']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['candidate_match'])
        self.assertEqual([x['record_id'] for x in rows[0]['matches']], ['match'])
        self.assertEqual(result['blocks'][1]['status'], 'not_run')

    def test_review_requires_decision_reason_and_page(self):
        self.assertEqual(m.review_for({}, 'key', ('漏抽',))['status'], 'pending')
        with self.assertRaises(ValueError):
            m.review_for(dict(key=dict(decision='漏抽', reason='x', evidence_pages=[])), 'key', ('漏抽',))
        got = m.review_for(dict(key=dict(decision='漏抽', reason='Synthetic page lacks required value', evidence_pages=[1])), 'key', ('漏抽',))
        self.assertEqual(got['status'], 'reviewed')

    def test_fits_select_effective_leaf_bound_call_excluding_timeout(self):
        chunks = [dict(id='P', chars=100, fragments=[dict(text='Parent 1')])]
        children = [dict(id='P.a', sha256='A', chars=40, fragments=[dict(text='A 1 2')]),
                    dict(id='P.b', sha256='B', chars=60, fragments=[dict(text='B 3 4')])]
        m.dump(self.old/'chunks.json', chunks)
        m.dump(self.old/'splits/P.json', dict(parent=chunks[0], children=children))
        m.dump(self.old/'firstpass-chunk-status.json', dict(rows=[dict(id='P.a', accepted_complete=True),
                 dict(id='P.b', accepted_complete=True), dict(id='missing', accepted_complete=False)]))
        calls = [dict(chunk_id='P', status='timed_out', usage=dict(output_tokens=999)),
                 dict(chunk_id='P.a', status='completed', log='A', call_number=1, usage=dict(output_tokens=100)),
                 dict(chunk_id='P.a', status='completed', log='retry', call_number=2, usage=dict(output_tokens=200)),
                 dict(chunk_id='P.b', status='completed', log='B', call_number=3, usage=dict(output_tokens=300))]
        m.dump(self.old/'usage.json', dict(attempts=calls))
        for cid, log in (('P.a', 'A'), ('P.b', 'B')):
            m.dump(self.old/'accepted'/f'{cid}.json', dict(log=log, records=[]))
        rows, excluded = m.fit_rows(self.old)
        self.assertEqual([r['output_tokens'] for r in rows], [100, 300])
        self.assertEqual([r['characters'] for r in rows], [40, 60])
        self.assertEqual(excluded, [])

    def test_frozen_page_hash_is_verified(self):
        page = self.old/'page.txt'
        page.write_text('Synthetic page only')
        m.dump(self.old/'corpus-manifest.json', dict(sources=[dict(source_id='synthetic', id='S', page_records=[
            dict(page=1, corpus_path=str(page), text_sha256=hashlib.sha256(page.read_bytes()).hexdigest())])]))
        self.assertEqual(m.page_map(self.old)[('synthetic', 1)], 'Synthetic page only')
        page.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Frozen source changed'):
            m.page_map(self.old)

    def test_measurement_output_cannot_overwrite_baseline(self):
        with self.assertRaisesRegex(ValueError, 'Output must be outside'):
            m.build_measurements(self.old, self.old / 'child', self.root / 's1', self.root / 'p0')

    def test_all_attempt_costs_include_probe_failures_and_budget_reservations(self):
        calls = [dict(stage='probe', status='failed', usage={}, seconds=10, is_probe=True),
                 dict(stage='probe', status='completed', usage=dict(input_tokens=50, output_tokens=5),
                      seconds=2, is_probe=True)]
        m.dump(self.new/'usage.json', dict(attempts=calls))
        (self.new/'calls.jsonl').write_text(''.join(json.dumps(dict(number=i, stage='probe'))+'\n' for i in (1, 2)))
        result = m.compare_costs(self.old, self.new)
        self.assertEqual(result['call_budget']['reserved'], 2)
        self.assertEqual(result['call_budget']['remaining'], 14)
        self.assertEqual(result['all_attempt_totals']['calls'], 2)
        self.assertEqual(result['all_attempt_totals']['seconds'], 12)
        self.assertEqual(result['all_attempt_totals']['input_unknown_calls'], 1)
        self.assertEqual(len(result['fixed_input']['s2b_probes']), 2)

    def test_rule_counts_soft_issues_without_rejecting_and_keeps_blank_count(self):
        result = dict(status='completed', output_records=3, records=[record('a'), record('b')],
                      rejected=[dict(record=record('c'), errors=['value missing'])],
                      soft_queue=[dict(record_id='a', issues=['疑似漏列', 'null日期缺说明']),
                                  dict(record_id='b', issues=['疑似漏列'])],
                      blank_cells=4, suspicious_missing_columns=1,
                      outside_index=[dict(page=1, title='Synthetic subsection')])
        m.dump(self.new/'accepted/S46.s01.json', result)
        summary = m.rule_outcomes(self.new)
        totals = summary['selected_output_totals']
        self.assertEqual(totals['accepted_records'], 2)
        self.assertEqual(totals['hard_rejected_records'], 1)
        self.assertEqual(totals['soft_flagged_records'], 2)
        self.assertEqual(totals['soft_rejected_records'], 0)
        self.assertEqual(totals['suspicious_missing_column_rows'], 1)
        self.assertEqual(totals['blank_cells_skipped'], 4)
        self.assertEqual(totals['soft_issue_occurrences']['疑似漏列'], 2)

    def test_choice_alignment_reuses_strict_economic_key(self):
        detail = dict(scope='consolidated', scope_note='', value_status='reported', value_note='')
        pdf = records.make_record('metric', 'pdf', 'synthetic', 'Example', 'Revenue', '2025',
            '2026-03-01', '2025-12-31', [dict(page=1, text='Revenue 10')], detail,
            period=dict(kind='annual', label='2025'), numbers=[records.number('10', '元', 'CNY')])
        choice = deepcopy(pdf)
        choice['record_id'] = 'choice'
        choice['source'] = dict(kind='choice', source_id='synthetic-choice', choice_field='REVENUE', report_period='2025-12-31')
        choice['excerpts'] = []
        choice['locator']['pdf_pages'] = []
        m.dump(self.new/'accepted/S34.s03.json', dict(records=[pdf], coverage=[dict(item_id='A01', record_ids=['pdf'])]))
        m.dump(self.new/'choice/records.json', [choice])
        aligned = m.choice_alignment(self.new)
        self.assertEqual(aligned['counts'], dict(相等=1))
        choice['dimensions'] = [dict(name='Product', value='Synthetic A')]
        m.dump(self.new/'choice/records.json', [choice])
        self.assertEqual(m.choice_alignment(self.new)['counts'], dict(未对齐=1))

    def test_projection_keeps_full_pdf_and_core_scopes_without_split_parent(self):
        parent = dict(id='S34.parent', chars=100, sections=['七、合并财务报表项目注释'])
        children = [dict(id='S34.a', chars=40, sections=['七、合并财务报表项目注释']),
                    dict(id='S34.b', chars=60, sections=['七、合并财务报表项目注释'])]
        m.dump(self.old/'chunks.json', [parent])
        m.dump(self.old/'splits/parent.json', dict(parent=parent, children=children))
        m.dump(self.old/'corpus-manifest.json', dict(sources=[dict(id='S34', page_records=[dict(chars=150)])]))
        p0 = self.root/'p0'
        m.dump(p0/'corpus-manifest.json', dict(sources=[]))
        got = m.extrapolate(self.old, self.new, p0)
        self.assertEqual(got['s34_core_character_mix'], {'附注七': 100})
        self.assertEqual(got['core_scope_check']['all_sources_effective_core_characters'], 100)
        self.assertEqual(got['core_scope_check']['s34_full_pdf_characters'], 150)
        self.assertEqual(got['scenarios'], [])
        self.assertIsNone(got['request_mode_volume'])

    def test_choice_unaligned_reasons_diagnose_without_changing_keys(self):
        pdf = []
        for rid, indicator, period in [('prior', 'Revenue', '2024H1'),
                                      ('change', 'Revenue（同比增减）', '2025'),
                                      ('alias', '总资产', '2025-12-31'),
                                      ('absent', 'ROE', '2025')]:
            item = record(rid)
            item['object_key'] = dict(subject='Example', indicator=indicator, period=period)
            item['period'] = dict(label=period)
            pdf.append(item)
        choice = [dict(record_id='asset', object_key=dict(subject='Example', indicator='资产总计', period='2025-12-31'),
                       period=dict(label='2025-12-31')),
                  dict(record_id='revenue', object_key=dict(subject='Example', indicator='Revenue', period='2025'),
                       period=dict(label='2025'))]
        rows = [dict(status='未对齐', record_ids=[r['record_id']], base_key_candidates=[]) for r in pdf]
        got = m._alignment_reason_rows(rows, pdf, choice)
        self.assertEqual([r['category'] for r in got], ['Choice未选取该期间', '增减列无同名Choice原表科目',
                         '原文与Choice科目名称不同', 'Choice原表转换未含该同名指标'])
        self.assertEqual(got[2]['name_only_candidates'][0]['record_id'], 'asset')
        self.assertEqual(pdf[2]['object_key']['indicator'], '总资产')
        self.assertTrue(all(r['status']=='未对齐' for r in rows))

    def test_choice_saved_whole_run_baseline_keeps_duplicate_row_denominator(self):
        m.dump(self.old/'accepted/S34.s03.json', dict(records=[], coverage=[dict(item_id='A01', record_ids=[])]))
        m.dump(self.old/'qa-queue.json', dict(choice_pdf_a01=[
            dict(status='相等', record_ids=['same-pdf', 'choice-1']),
            dict(status='不相等', record_ids=['same-pdf', 'choice-2'])]))
        got = m.choice_alignment(self.new, self.old)
        self.assertEqual(got['s2_same_block']['pdf_a01_records'], 0)
        self.assertEqual(got['s2_whole_run_saved']['alignment_rows'], 2)
        self.assertEqual(got['s2_whole_run_saved']['unique_pdf_records'], 1)
        self.assertEqual(got['s2_whole_run_saved']['counts'], {'相等': 1, '不相等': 1})

    def test_density_separates_expanded_from_accepted_record_cost(self):
        attempt = dict(status='completed', usage=dict(output_tokens=4200), source_chars=100,
                       output_records=42, accepted_records=40, rejected_records=2)
        row = m.density_row(attempt)
        self.assertEqual(row['expanded_records'], 42)
        self.assertEqual(row['accepted_records'], 40)
        self.assertEqual(row['rejected_records'], 2)
        self.assertEqual(row['output_tokens_per_expanded_record'], 100)
        self.assertEqual(row['output_tokens_per_record'], 105)

    def test_request_retry_densities_keep_each_attempt_acceptance(self):
        calls = []
        for index, count in ((1, 0), (2, 2)):
            log = self.new/f'logs/request/attempt-{index}'
            m.dump(log/'acceptance.json', dict(records=[record(str(i)) for i in range(count)],
                                               rejected=[dict(errors=['bad'])] if not count else [], output_records=2))
            calls.append(dict(chunk_id='R2', mode='request', status='completed', log=str(log),
                              attempt=index, usage=dict(output_tokens=200), source_chars=100))
        m.dump(self.new/'usage.json', dict(attempts=calls))
        m.dump(self.new/'accepted/R2.json', dict(records=[record('a'),record('b')], rejected=[], output_records=2))
        result = m.compare_costs(self.old, self.new)
        self.assertEqual([r['accepted_records'] for r in result['requests']], [0, 2])
        self.assertEqual([r['rejected_records'] for r in result['requests']], [1, 0])

    def test_proposed_output_rule_refuses_infeasible_or_nonmonotonic_threshold(self):
        model = dict(intercept=18000, coefficient=100,
                     predictions=[dict(actual=30000, fitted=21000)])
        got = m.proposed_output_rule(model, 'table_rows')
        self.assertTrue(got['algebraically_feasible'])
        self.assertEqual(got['target_output_tokens'], 40000)
        self.assertEqual(got['maximum_feature_count'], 130)
        self.assertLess(got['zero_feature_prediction_with_margin'], got['target_output_tokens'])
        impossible = m.proposed_output_rule(model, 'table_rows', target_output_tokens=24000)
        self.assertFalse(impossible['algebraically_feasible'])
        self.assertEqual(impossible['status'], 'derivation_not_feasible')
        self.assertIsNone(impossible['maximum_feature_count'])
        model['coefficient'] = -1
        self.assertFalse(m.proposed_output_rule(model, 'characters')['algebraically_feasible'])

    def test_attempt_costs_prefer_revalidated_acceptance_without_changing_usage(self):
        log = self.new/'logs/request/attempt-1'
        m.dump(log/'acceptance.json', dict(records=[], rejected=[dict(errors=['old error'])], output_records=2))
        m.dump(log/'acceptance-revalidated.json', dict(records=[record('a'),record('b')], rejected=[], output_records=2))
        attempt = dict(chunk_id='R2', mode='request', status='completed', log=str(log),
                       usage=dict(output_tokens=200, input_tokens=100), source_chars=100)
        m.dump(self.new/'usage.json', dict(attempts=[attempt]))
        got = m.compare_costs(self.old, self.new)
        for row in (got['requests'][0], got['all_s2b_calls'][0]):
            self.assertEqual(row['accepted_records'], 2)
            self.assertEqual(row['expanded_records'], 2)
            self.assertEqual(row['rejected_records'], 0)
            self.assertEqual(row['output'], 200)
            self.assertTrue(row['validation_receipt'].endswith('acceptance-revalidated.json'))

    def test_section_class_notes_do_not_collapse_into_governance(self):
        self.assertEqual(m.section_class(dict(id='S34.s12', sections=['七、合并财务报表项目注释 / 1、货币资金'])), '附注七')
        self.assertEqual(m.section_class(dict(id='S34.s18', note_group='其他附注', sections=['八、研发支出'])), '其他附注')
        self.assertEqual(m.section_class(dict(id='S34.s09', sections=['五、重要会计政策及会计估计'])), '会计政策')
        self.assertEqual(m.section_class(dict(id='S42.s01', sections=[])), '研报')


if __name__ == '__main__':
    unittest.main()
