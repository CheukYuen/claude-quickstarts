"""Synthetic report accounting; no PDF content or external model calls."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import corpus
import s2_compare
import s2_report


class ReportAccountingTests(unittest.TestCase):
    def test_schema_diagnostics_counts_actual_file_characters_and_generated_guide(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            texts = dict(stored='{"合成": "记录"}\r\n', model_full='{"type":"object"}\n',
                         model_transport='{"type":"合成"}\n')
            filenames = dict(stored='stored-records.json', model_full='model-full.json',
                             model_transport='model-transport.json')
            for name, text in texts.items():
                path = root / 'schemas' / filenames[name]
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(text.encode('utf-8'))
            with mock.patch.object(s2_report.s2.records, 'field_guide', return_value='合成字段说明\n'):
                result = s2_report.schema_diagnostics(root, [])
            self.assertEqual(result['schema_characters'], {name: len(text) for name, text in texts.items()})
            self.assertEqual(result['field_guide_characters'], len('合成字段说明\n'))
            for name in texts:
                binding = result['schema_files'][name]
                self.assertTrue(binding['exists'])
                self.assertEqual(binding['sha256'], s2_report.s2.sha(root / 'schemas' / filenames[name]))

    def test_missing_schema_files_stay_unknown_without_regenerating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(s2_report.s2.records, 'field_guide', return_value='Synthetic guide'):
                result = s2_report.schema_diagnostics(root, [])
            self.assertEqual(result['schema_characters'], dict(stored=None, model_full=None, model_transport=None))
            self.assertTrue(all(not binding['exists'] and binding['sha256'] is None
                                for binding in result['schema_files'].values()))
            self.assertEqual(result['field_guide_characters'], len('Synthetic guide'))
            self.assertFalse((root / 'schemas').exists())

    def test_probe_cost_evidence_uses_only_registered_usage_and_preserves_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempts = [dict(stage='pilot-r9-schema', is_probe=True, call_number=1, status='completed',
                prompt_version='synthetic-v9', prompt_chars=71, source_chars=9,
                command=['synthetic-cli'], log=str(root / 'known'),
                usage=dict(input_tokens=1234, cached_input_tokens=0, output_tokens=7)),
                dict(stage='pilot-r10-schema', is_probe=True, call_number=2, status='failed',
                     command=['synthetic-cli'], log=str(root / 'unknown'), usage={}),
                dict(stage='extract-S46.s01', is_probe=False, status='completed',
                     command=['synthetic-cli'], log=str(root / 'extract'), usage=dict(input_tokens=99999))]
            diagnostics = s2_report.schema_diagnostics(root, attempts)
            evidence = diagnostics['synthetic_probe_usage']
            self.assertEqual(evidence['registered_calls'], 2)
            self.assertEqual(evidence['measured_input_calls'], 1)
            self.assertEqual(evidence['unknown_input_calls'], 1)
            known, unknown = evidence['rows']
            self.assertEqual((known['prompt_characters'], known['input_tokens'], known['cached_input_tokens'],
                              known['uncached_input_tokens']), (71, 1234, 0, 1234))
            self.assertIsNone(unknown['prompt_characters'])
            self.assertIsNone(unknown['input_tokens'])
            self.assertIsNone(unknown['cached_input_tokens'])
            self.assertIsNone(unknown['uncached_input_tokens'])
            self.assertIsNone(evidence['schema_tokens'])
            usage = s2_compare.usage_comparison([], attempts, None, 0)
            result = s2_report.overall_cost_observations(usage, 700, False, attempts)
            self.assertEqual(result['synthetic_probe_usage'], evidence)
            self.assertEqual(result['joint_conclusion']['status'], '未建立')
            text = s2_report.overall_cost_markdown(result)
            self.assertIn('|pilot-r9-schema|71|9|1234|0|1234|', text)
            self.assertIn('|pilot-r10-schema|未测得|未测得|未测得|未测得|未测得|', text)
            self.assertNotIn('|extract-S46.s01|', text)
            self.assertIn('没有真实PDF', text)
            self.assertIn('不以字符数或调用差分归因', text)
            self.assertIn('不能把未缓存输入等同原文token', text)

    def test_reverse_matching_errors_are_retained_and_separate_from_semantic_errors(self):
        queues = dict(queues=dict(unmatched=[],
            reverse=[dict(record_id='r%d' % n, review=dict(status='pending')) for n in range(4)],
            semantic=[dict(record_id='s%d' % n, review=dict(status='pending')) for n in range(3)]))
        def decision(queue, key, value, types=None):
            return dict(queue=queue, id=key, decision=value, error_types=types or [],
                        reason='Explicit synthetic review.', evidence_pages=[2])
        decisions = [decision('reverse', 'r0', '错误', ['反向候选误判']),
                     decision('reverse', 'r1', '错误', ['主体']),
                     decision('reverse', 'r2', '错误', ['反向候选误判', '期间']),
                     decision('reverse', 'r3', '新增的正确内容'),
                     decision('semantic', 's0', '正确'),
                     decision('semantic', 's1', '错误', ['单位']),
                     decision('semantic', 's2', '拿不准')]
        reviewed = s2_compare.apply_reviews(queues, dict(queues_sha256=s2_compare.json_hash(queues),
            reviewer='Synthetic reviewer', review_id='test', decisions=decisions))
        self.assertEqual(reviewed['queues']['reverse'][0]['review']['error_types'], ['反向候选误判'])
        result = s2_report.reverse_error_breakdown(reviewed)
        self.assertEqual(result['metric'], '新增候选无效率')
        self.assertEqual(result['invalid_candidates'], 3)
        self.assertEqual(result['candidate_matching_only'], 1)
        self.assertEqual(result['other_error_records'], 1)
        self.assertEqual(result['candidate_matching_with_other_types'], 1)
        self.assertEqual(result['invalid_candidate_rate'], .75)
        self.assertEqual(reviewed['review_summary']['semantic']['error_rate'], .5)
        self.assertEqual(reviewed['review_summary']['semantic']['counts'], {'正确': 1, '错误': 1, '拿不准': 1})

    def test_review_strata_use_current_split_children_and_ignore_request_and_context(self):
        from test_s2_compare import item
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / 'run', Path(temporary) / 'repo'
            (repo / 'prompts').mkdir(parents=True)
            (repo / 'prompts/extract-records.md').write_text('synthetic current prompt')
            current = s2_report.s2.sha(repo / 'prompts/extract-records.md')
            parent = dict(id='S34.parent', source='S34', sha256='parent', core_pages=[5],
                          fragments=[dict(page=5, start=0, end=1800)])
            children = [dict(id='S34.child-a', source='S34', sha256='a', core_pages=[5],
                             fragments=[dict(page=5, start=0, end=500)]),
                        dict(id='S34.child-b', source='S34', sha256='b', core_pages=[5],
                             fragments=[dict(page=5, start=500, end=1800)])]
            context = dict(id='S34.context', source='S34', sha256='context', core_pages=[6],
                           fragments=[dict(page=6, start=0, end=5000)], context_pages=[5],
                           context_fragments=[dict(page=5, start=0, end=1800)])
            corpus.dump(root / 'chunks.json', [parent, context])
            corpus.dump(root / 'splits' / 'S34.parent.json', dict(parent=parent, children=children,
                prompt_template_sha256=current))
            request = dict(id='S34.request', source='S34', sha256='request', core_pages=[5],
                           fragments=[dict(page=5, start=0, end=9000)])
            corpus.dump(root / 'request-plans' / 'one.json', dict(chunk=request, request='synthetic request'))
            comparison = s2_compare.compare_inventory([item('S34.c.I1', 'Value 987 [S34 p5]')], [])
            with mock.patch.object(s2_report.s2, 'HERE', repo):
                leaves = s2_report.current_structural_leaves(root)
            self.assertEqual(leaves, children + [context])
            queues = s2_compare.make_review_queues(comparison, [], leaves,
                chunk_plan_scope='current structural leaves')
            self.assertEqual(queues['chunk_plan_scope'], 'current structural leaves')
            self.assertEqual(queues['chunk_plan_ids'], ['S34.child-a', 'S34.child-b', 'S34.context'])
            self.assertEqual(queues['queues']['unmatched'][0]['chunk'], 'S34.child-b')
            self.assertEqual(queues['actual_counts']['unmatched'], 1)
            self.assertEqual(queues['sampling']['strata']['unmatched'][0]['key'], ('S34', 'S34.child-b'))

    def test_unsplit_current_leaves_preserve_chunk_strata_and_sample(self):
        from test_s2_compare import item
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chunks = [dict(id='S34.a', source='S34', sha256='a', core_pages=[2],
                           fragments=[dict(page=2, start=0, end=500)]),
                      dict(id='S42.a', source='S42', sha256='b', core_pages=[3],
                           fragments=[dict(page=3, start=0, end=700)])]
            corpus.dump(root / 'chunks.json', chunks)
            comparison = s2_compare.compare_inventory([
                item('%s.c.I%03d' % (source, n), 'Value 987 [%s p%d]' % (source, page))
                for source, page in (('S34', 2), ('S42', 3)) for n in range(60)], [])
            leaves = s2_report.current_structural_leaves(root)
            self.assertEqual(leaves, chunks)
            original = s2_compare.make_review_queues(comparison, [], chunks)
            current = s2_compare.make_review_queues(comparison, [], leaves,
                chunk_plan_scope='current structural leaves')
            self.assertEqual(current['queues'], original['queues'])
            self.assertEqual(current['sampling'], original['sampling'])

    def test_choice_responsibility_excludes_mixed_boundary_pages(self):
        manifest = dict(sources=[dict(id='S34', page_records=[dict(page=36, chars=200),
            dict(page=37, chars=100), dict(page=57, chars=300), dict(page=58)])])
        exclusions = [dict(source='S34', pages=[36, 37, 57, 58], fragments=[
            dict(page=36, start=114, end=200), dict(page=37, start=0, end=50),
            dict(page=37, start=50, end=100), dict(page=57, start=0, end=67), dict(page=58, start=0, end=80)])]
        scope = s2_report.choice_responsibility_scope(manifest, exclusions)
        self.assertEqual(scope['fully_excluded_pages'], {'S34': [37]})
        self.assertEqual(scope['mixed_or_unconfirmed_pages'], {'S34': [36, 57, 58]})
        self.assertEqual(scope['pages'][0]['excluded_spans'], [dict(start=114, end=200)])
        # An identical value on Choice must not assign the non-excluded part
        # of a mixed page to the statement source.
        from test_s2_compare import item, record
        inventory = [item('S34.c.I' + str(page), 'Synthetic value 125 [S34 p%d]' % page)
                     for page in (36, 37, 57)]
        result = s2_compare.compare_inventory(inventory, [], excluded_pages=scope['fully_excluded_pages'],
                    choice_records=[record('synthetic-choice', '125')])
        self.assertEqual([row['status'] for row in result['items']], ['未匹配', '由Choice承担', '未匹配'])

    def test_pending_scope_groups_use_dynamic_pages_without_mutating_comparison(self):
        def item(key, pages, status='待抽取'):
            return dict(item_id=key, source='SYN', pages=pages, status=status,
                        manual_review=dict(status='pending', evidence_pages=[]))
        comparison = dict(items=[item('full', [7]), item('cross', [7, 8]), item('mixed', [9]),
            item('core', [8, 10]), item('missing', [11]), item('no-page', []),
            item('choice', [7], '由Choice承担')])
        scope = dict(fully_excluded_pages={'SYN': [7]}, mixed_or_unconfirmed_pages={'SYN': [7, 9]})
        regions = dict(pages=[dict(source='SYN', page=7, core_chunk_ids=[], ready=False),
            dict(source='SYN', page=8, core_chunk_ids=['SYN.a'], ready=True),
            dict(source='SYN', page=9, core_chunk_ids=['SYN.b'], ready=False),
            dict(source='SYN', page=10, core_chunk_ids=['SYN.c'], ready=False)])
        original = deepcopy((comparison, scope, regions))
        result = s2_report.pending_scope_diagnostics(comparison, scope, regions)
        self.assertEqual(result['counts'], {'全页排除': 1, '引用触及排除或混合页': 2,
                                          '仅保留core未就绪': 1, '其他缺页': 2})
        self.assertEqual(result['item_count'], 6)
        rows = {row['item_id']: row for row in result['items']}
        self.assertEqual(rows['full']['group'], '全页排除')
        self.assertEqual(rows['cross']['fully_excluded_pages'], [7])
        self.assertEqual(rows['mixed']['mixed_or_unconfirmed_pages'], [9])
        self.assertEqual(rows['core']['unready_core_pages'], [10])
        self.assertEqual(rows['core']['pages_without_core'], [])
        self.assertEqual(rows['missing']['pages_without_core'], [11])
        self.assertTrue(all(row['reason'] for row in result['items']))
        self.assertEqual((comparison, scope, regions), original)

    def test_document_completion_needs_every_current_structural_leaf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / 'run', Path(temporary) / 'repo'
            (repo / 'prompts').mkdir(parents=True)
            (repo / 'prompts/extract-records.md').write_text('synthetic current')
            current = s2_report.s2.sha(repo / 'prompts/extract-records.md')
            chunks = [dict(id='S46.a', source='S46', sha256='one'),
                      dict(id='S34.a', source='S34', sha256='two'),
                      dict(id='S34.b', source='S34', sha256='three')]
            inputs = {chunk['id']: (chunk, 'coverage', '') for chunk in chunks}
            inputs[('S46.request1', 'request')] = [(dict(source='S46'), 'request', 'synthetic request')]
            def accept(chunk, prompt=current):
                corpus.dump(root / 'accepted' / (chunk['id'] + '.json'), dict(status='completed',
                    binding=dict(prompt_template_sha256=prompt, chunk_sha256=chunk['sha256'])))
            accept(chunks[0]); accept(chunks[1])
            with mock.patch.object(s2_report, 'HERE', repo), \
                 mock.patch.object(s2_report.s2, 'collection_inputs', return_value=inputs):
                self.assertEqual(s2_report.completed_source_ids(root), ['S46'])
                accept(chunks[2])
                self.assertEqual(s2_report.completed_source_ids(root), ['S34', 'S46'])
                accept(chunks[2], 'old')
                self.assertEqual(s2_report.completed_source_ids(root), ['S46'])

    def test_page_readiness_intersects_all_core_leaves_and_keeps_mixed_boundaries_conservative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / 'run', Path(temporary) / 'repo'
            (repo / 'prompts').mkdir(parents=True)
            (repo / 'prompts/extract-records.md').write_text('synthetic current prompt')
            current = s2_report.s2.sha(repo / 'prompts/extract-records.md')
            chunks = [dict(id='S34.a', source='S34', sha256='a', core_pages=[2, 3, 36]),
                      dict(id='S34.b', source='S34', sha256='b', core_pages=[3, 4, 57]),
                      dict(id='S34.context', source='S34', sha256='c', core_pages=[5], context_pages=[2, 99]),
                      dict(id='S42.full', source='S42', sha256='d', core_pages=[1])]
            inputs = {chunk['id']: (chunk, 'coverage', '') for chunk in chunks}
            scope = dict(fully_excluded_pages={'S34': [37]}, mixed_or_unconfirmed_pages={'S34': [36, 57]})
            def accept(chunk, prompt=current):
                corpus.dump(root / 'accepted' / (chunk['id'] + '.json'), dict(status='completed',
                    binding=dict(prompt_template_sha256=prompt, chunk_sha256=chunk['sha256'])))
            accept(chunks[0]); accept(chunks[3])
            with mock.patch.object(s2_report, 'HERE', repo), \
                 mock.patch.object(s2_report.s2, 'collection_inputs', return_value=inputs):
                first = s2_report.comparison_region_readiness(root, scope, ['S42'])
                self.assertEqual(first['ready_pages_by_source']['S34'], [2])
                page3 = next(row for row in first['pages'] if row['source'] == 'S34' and row['page'] == 3)
                self.assertEqual(page3['incomplete_core_chunk_ids'], ['S34.b'])
                page2 = next(row for row in first['pages'] if row['source'] == 'S34' and row['page'] == 2)
                self.assertEqual(page2['core_chunk_ids'], ['S34.a'])
                self.assertFalse(any(row['page'] == 99 for row in first['pages']))
                accept(chunks[1])
                second = s2_report.comparison_region_readiness(root, scope, ['S42'])
                self.assertEqual(second['ready_pages_by_source']['S34'], [2, 3, 4])
                self.assertTrue(all(not row['ready'] for row in second['pages'] if row['page'] in (36, 57)))
                accept(chunks[2])
                full = s2_report.comparison_region_readiness(root, scope, ['S34', 'S42'])
                self.assertEqual(full['ready_pages_by_source']['S34'], [2, 3, 4, 5, 36, 57])
                self.assertFalse(next(row for row in full['pages'] if row['page'] == 37)['ready'])
                accept(chunks[1], 'old-prompt')
                stale = s2_report.comparison_region_readiness(root, scope, ['S42'])
                self.assertNotIn(3, stale['ready_pages_by_source']['S34'])

    def test_measured_pilot_ratios_survive_partial_run_and_failure_unknown(self):
        attempts = [{'stage': 'pilot-r1-S46', 'chunk_id': 'S46.s01', 'call_number': 4,
            'prompt_version': 'v1', 'source_chars': 1000, 'status': 'completed', 'command_count': 0,
            'accepted_records': 5, 'usage': {'input_tokens': 125, 'cached_input_tokens': 120,
                'output_tokens': 50, 'reasoning_output_tokens': 10}},
            {'stage': 'pilot-r2-S46', 'chunk_id': 'S46.s01', 'call_number': 8,
             'prompt_version': 'v2', 'source_chars': 1000, 'status': 'timed_out', 'usage': {}}]
        rows = s2_report.measured_call_densities(attempts)['rows']
        self.assertEqual(rows[0]['ratios']['input_per_10000_source_chars'], 1250)
        self.assertEqual(rows[0]['ratios']['uncached_per_10000_source_chars'], 50)
        self.assertIsNone(rows[1]['ratios']['input_per_10000_source_chars'])
        p1 = [{'stage': 'extract-S46.c01', 'status': 'failed', 'usage': {}},
              {'stage': 'extract-S46.c01', 'status': 'completed',
               'usage': {'input_tokens': 1000, 'cached_input_tokens': 900, 'output_tokens': 30}}]
        result = s2_report.s46_same_input_cost(p1, attempts, source_chars=1000)
        self.assertEqual(result['p1']['all']['calls'], 2)
        self.assertEqual(result['p1']['all']['input_unknown_calls'], 1)
        self.assertEqual(result['hypothesis_status'], '未建立')
        self.assertEqual(result['observed_high_cache_calls'], [4])
        self.assertEqual(set(result['s2_by_round']), {'r1', 'r2'})
        self.assertEqual(result['s2_by_round']['r2']['failed']['input_unknown_calls'], 1)

    def test_s46_high_cache_does_not_override_aggregate_cache_decline(self):
        p1 = [dict(stage='extract-S46.c01', status='completed', command_count=1,
            usage=dict(input_tokens=1000, cached_input_tokens=900, output_tokens=20))]
        s2 = [dict(stage='pilot-r2-S46', chunk_id='S46.s01', call_number=1, status='completed',
                   source_chars=100, command_count=0,
                   usage=dict(input_tokens=100, cached_input_tokens=95, output_tokens=20)),
              dict(stage='extract-S34.s01', chunk_id='S34.s01', call_number=2, status='completed',
                   source_chars=900, command_count=0,
                   usage=dict(input_tokens=900, cached_input_tokens=405, output_tokens=20))]
        same = s2_report.s46_same_input_cost(p1, s2, source_chars=100)
        usage = s2_compare.usage_comparison(p1, s2, source_chars=1000, accepted_records=2)
        overall = s2_report.overall_cost_observations(usage, 1000, True)
        self.assertEqual(same['observed_high_cache_calls'], [1])
        self.assertFalse(same['overall_hypothesis_evaluated'])
        self.assertIn('不能据此否定', same['interpretation'])
        self.assertEqual(overall['cache_share']['s2_cache_ratio'], .5)
        self.assertAlmostEqual(overall['cache_share']['difference_percentage_points'], -40)
        self.assertEqual(overall['cache_share']['observed_direction'], '下降')
        self.assertIn('全量抽取已完成', overall['cache_share']['completeness'])
        self.assertEqual(overall['joint_conclusion']['status'], '未建立')

    def test_partial_aggregate_difference_and_equal_token_character_counts_do_not_prove_original_token_cost(self):
        p1 = [dict(stage='extract-A', status='completed', usage=dict(input_tokens=1000, cached_input_tokens=900))]
        s2 = [dict(stage='extract-B', status='completed', usage=dict(input_tokens=2000, cached_input_tokens=1000)),
              dict(stage='extract-C', status='failed', usage={})]
        usage = s2_compare.usage_comparison(p1, s2, source_chars=None, accepted_records=0)
        result = s2_report.overall_cost_observations(usage, 1000, False)
        self.assertAlmostEqual(result['cache_share']['difference_percentage_points'], -40)
        self.assertEqual(result['cache_share']['s2_input_unknown_calls'], 1)
        self.assertIn('部分结果，不能判全量', result['cache_share']['completeness'])
        uncached = result['uncached_vs_source']
        self.assertEqual(uncached['uncached_input_tokens'], 1000)
        self.assertEqual(uncached['original_source_characters'], 1000)
        self.assertEqual((uncached['input_unit'], uncached['source_unit']), ('token', '字符'))
        self.assertIsNone(uncached['original_source_tokens'])
        self.assertEqual(uncached['status'], '未建立')
        text = s2_report.overall_cost_markdown(result)
        self.assertIn('个百分点', text)
        self.assertIn('单位不同', text)
        self.assertIn('Schema', text)
        self.assertIn('不能断言', text)

    def test_unknown_aggregate_usage_keeps_percentage_point_difference_unknown(self):
        usage = s2_compare.usage_comparison([], [dict(status='failed', usage={})], None, 0)
        result = s2_report.overall_cost_observations(usage, None, False)
        self.assertIsNone(result['cache_share']['difference_percentage_points'])
        self.assertEqual(result['cache_share']['observed_direction'], '未测得')
        self.assertIsNone(result['uncached_vs_source']['uncached_input_tokens'])

    def test_projection_selects_final_current_prompt_once_per_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, repo = Path(temporary) / 'run', Path(temporary) / 'repo'
            (repo / 'prompts').mkdir(parents=True)
            (repo / 'prompts/extract-records.md').write_text('synthetic current prompt')
            corpus.dump(repo / 'runs/p0-20261021/corpus-manifest.json', {'summary': {'sources': 53, 'chars': 1000}})
            current = s2_report.s2.sha(repo / 'prompts/extract-records.md')
            attempts = [dict(stage='pilot-r2-A', chunk_id='S46.s01', call_number=number,
                source_chars=100, prompt_template_sha256=current if number < 3 else 'obsolete',
                status='completed', accepted_records=number*10,
                usage={'output_tokens': number*100}, log=str(root / ('log' + str(number)))) for number in (1, 2, 3)]
            corpus.dump(root / 'accepted/S46.s01.json', {'log': attempts[1]['log']})
            with mock.patch.object(s2_report, 'HERE', repo):
                result = s2_report.output_projection([{}] * 20, attempts, root)
            self.assertEqual(result['measured_success_calls'], 1)
            self.assertEqual(result['measured_output_tokens'], 200)
            self.assertEqual(result['measured_accepted_records'], 20)
            self.assertEqual(result['output_tokens_per_accepted_record'], 10)
            self.assertEqual(result['p0_output_tokens_by_char'], 2000)
            self.assertEqual(result['duplicate_success_calls_excluded'], [1])

    def test_recursive_completion_requires_nonempty_successes_and_prompt_binding(self):
        self.assertFalse(s2_report.complete_outcome({'status': 'split', 'children': []}))
        child = {'status': 'completed', 'binding': {'prompt_template_sha256': 'current'}}
        self.assertTrue(s2_report.complete_outcome({'status': 'split', 'children': [child]}, 'current'))
        self.assertFalse(s2_report.complete_outcome(child, 'new'))
        self.assertFalse(s2_report.complete_outcome({'status': 'timed_out'}))

    def test_unmeasured_rejection_is_null_and_invalid_output_not_zero_rate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / 'attempt'
            log.mkdir()
            corpus.dump(log / 'acceptance.json', {'status': 'invalid_output', 'suspected_truncation': True})
            result = s2_report.rejection_summary(root, [{'stage': 'synthetic', 'log': str(log)}])
            self.assertIsNone(result['rate'])
            self.assertIsNone(result['blocks'][0]['rejected'])
            self.assertEqual(result['suspected_truncations'], 1)

    def test_rejections_count_check_family_once_per_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus.dump(root / 'acceptance.json', {'status': 'completed', 'output_records': 4,
                'records': [{}, {}, {}], 'rejected': [{'errors': ['excerpts[0]: absent', 'excerpts[1]: absent',
                                                                  'numbers[0]: absent']}], 'discarded': []})
            result = s2_report.rejection_summary(root, [{'stage': 'synthetic', 'log': str(root)}])
            self.assertEqual(result['rate'], .25)
            self.assertEqual(result['by_check'], {'excerpts[]': 1, 'numbers[]': 1})

    def test_rejections_separate_all_attempts_from_current_prompt_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus.dump(root / 'old/acceptance.json', dict(status='completed', output_records=20,
                records=[{}] * 16, rejected=[dict(errors=['excerpts[0]: absent', 'excerpts[1]: absent'])] * 4,
                discarded=[]))
            corpus.dump(root / 'current/acceptance.json', dict(status='completed', output_records=10,
                records=[{}] * 9, rejected=[dict(errors=['numbers[0]: bad value', 'numbers[1]: bad value'],
                                               record=dict(numbers=[dict(value='1,250')]))], discarded=[]))
            corpus.dump(root / 'current-failed/acceptance.json', dict(status='invalid_output', output_records=7))
            attempts = [dict(stage='old', log=str(root / 'old'), prompt_version='v1'),
                        dict(stage='current', log=str(root / 'current'), prompt_version='v2'),
                        dict(stage='current-failed', log=str(root / 'current-failed'), prompt_version='v2')]
            with mock.patch.object(s2_report.s2, 'PROMPT_VERSION', 'v2'):
                result = s2_report.rejection_summary(root, attempts)
            self.assertEqual(len(result['blocks']), 3)
            self.assertEqual(result['measured_output_records'], 30)
            self.assertEqual(result['rejected_records'], 5)
            self.assertEqual(result['rate'], 5/30)
            self.assertEqual(result['by_check'], {'excerpts[]': 4, 'numbers[]': 1})
            old = result['by_prompt_version']['v1']
            self.assertEqual((old['rejected_records'], old['measured_output_records'], old['rate']), (4, 20, .2))
            current = result['current_prompt']
            self.assertEqual(result['current_prompt_version'], 'v2')
            self.assertEqual(current['prompt_version'], 'v2')
            self.assertEqual((current['rejected_records'], current['measured_output_records'], current['rate']), (1, 10, .1))
            self.assertEqual(current['attempts'], 2)
            self.assertEqual(current['individually_validated_calls'], 1)
            self.assertEqual(current['unvalidated_calls'], 1)
            self.assertEqual(current['by_check'], {'numbers[]': 1})
            self.assertEqual(current['numeric_format_errors'], {'numbers.value_contains_comma': 1})
            self.assertIsNone(result['blocks'][2]['rate'])
            self.assertIsNone(result['blocks'][2]['rejected'])

    def test_current_prompt_with_only_whole_envelope_failure_keeps_all_rates_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus.dump(root / 'old/acceptance.json', dict(status='completed', output_records=3,
                                                         records=[{}] * 3, rejected=[], discarded=[]))
            corpus.dump(root / 'current/acceptance.json', dict(status='invalid_output', output_records=None))
            attempts = [dict(stage='old', log=str(root / 'old'), prompt_version='v1'),
                        dict(stage='current', log=str(root / 'current'), prompt_version='v2', output_records=7)]
            with mock.patch.object(s2_report.s2, 'PROMPT_VERSION', 'v2'):
                result = s2_report.rejection_summary(root, attempts)
            self.assertEqual(result['measured_output_records'], 3)
            self.assertEqual(result['rate'], 0)
            self.assertEqual(result['blocks'][1]['output_records'], 7)
            current = result['current_prompt']
            self.assertIsNone(current['rejected_records'])
            self.assertIsNone(current['measured_output_records'])
            self.assertIsNone(current['rate'])
            self.assertEqual(current['by_check'], {})
            self.assertEqual(current['individually_validated_calls'], 0)
            self.assertEqual(current['unvalidated_calls'], 1)
            self.assertEqual(result['by_prompt_version']['v2']['rate'], current['rate'])

    def test_running_and_failed_calls_without_acceptance_remain_unvalidated_not_zero_rejections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempts = [dict(stage='running-extract', log=str(root / 'running'), prompt_version='v2', status='running'),
                        dict(stage='failed-extract', log=str(root / 'failed'), prompt_version='v2',
                             status='completed', s2_status='failed', output_records=7),
                        dict(stage='probe', log=str(root / 'probe'), prompt_version='v2', is_probe=True, status='running')]
            with mock.patch.object(s2_report.s2, 'PROMPT_VERSION', 'v2'):
                result = s2_report.rejection_summary(root, attempts)
            self.assertEqual(len(result['blocks']), 2)
            self.assertEqual([row['status'] for row in result['blocks']], ['running', 'failed'])
            self.assertEqual(result['blocks'][1]['output_records'], 7)
            for row in result['blocks']:
                self.assertIsNone(row['accepted'])
                self.assertIsNone(row['rejected'])
                self.assertIsNone(row['measured_output_records'])
                self.assertIsNone(row['context_only'])
                self.assertIsNone(row['rate'])
            current = result['current_prompt']
            self.assertEqual(current['attempts'], 2)
            self.assertEqual(current['individually_validated_calls'], 0)
            self.assertEqual(current['unvalidated_calls'], 2)
            self.assertEqual(current['status_counts'], {'running': 1, 'failed': 1})
            self.assertIsNone(current['rejected_records'])
            self.assertIsNone(current['measured_output_records'])
            self.assertIsNone(current['rate'])

    def test_report_build_keeps_incomplete_coverage_and_reviews_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'run'
            root.mkdir()
            repo = Path(temporary) / 'repo'
            (repo / 'runs/s1-20261021').mkdir(parents=True)
            (repo / 'runs/p1-20261021').mkdir(parents=True)
            (repo / 'prompts').mkdir()
            (repo / 'prompts/extract-records.md').write_text('synthetic prompt')
            corpus.dump(repo / 'runs/s1-20261021/p1-field-fit.json', {'items': [
                {'item_id': 'S46.c01-I1', 'mapping': {'fit': 'direct'}, 'text': '合成收入125元 [S46 p1]'}]})
            corpus.dump(repo / 'runs/p1-20261021/usage.json', {'attempts': []})
            corpus.dump(root / 'corpus-manifest.json', {'sources': [{'id': 'S46', 'source_id': 'synthetic'}]})
            corpus.dump(root / 'chunks.json', [{'id': 'S46.s01', 'source': 'S46', 'chars': 100}])
            corpus.dump(root / 'usage.json', {'calls_reserved': 0})
            corpus.dump(root / 'PREPARATION.json', {'input_sources': 1, 'input_pages': 1, 'input_chars': 100,
                'structural_chunks': 1, 'core_char_limit': 20000, 'core_chars': 100})
            with mock.patch.object(s2_report, 'HERE', repo), \
                 mock.patch.object(s2_report.s2, 'integrity', return_value={'ok': True}), \
                 mock.patch.object(s2_report.s2, 'collect_records', return_value=([], {'S46': []})), \
                 mock.patch.object(s2_report.s2, 'summarize_usage', return_value=[]):
                result = s2_report.build_report(root, 'pilot-stopped', 'synthetic pilot not passed')
            self.assertFalse(result['extraction_complete'])
            self.assertEqual(result['comparison_counts'], {'待抽取': 1})
            self.assertIsNone(result['reviews']['semantic']['error_rate'])
            self.assertEqual(result['capacity_observations']['rows'], [])
            self.assertTrue((root / 'capacity-observations.json').exists())
            queue_metadata = s2_report.read(root / 'review-queues.json')
            self.assertEqual(queue_metadata['chunk_plan_scope'], 'current structural leaves')
            self.assertEqual(queue_metadata['chunk_plan_ids'], ['S46.s01'])
            self.assertEqual(queue_metadata['sampling']['algorithm'], 'per-stratum-sha256-v1')
            self.assertIsNone(s2_report.read(root / 'usage-comparison.json')['s2']['ratios']['input_per_10000_source_chars'])
            report = (root / 'REPORT.md').read_text()
            self.assertIn('全量抽取尚未完成', report)
            self.assertIn('Schema文件字符数：stored 未测得、model-full 未测得、model-transport 未测得', report)
            self.assertIn('失败后拆分的容量观测', report)
            self.assertIn('不能验证20000字符是密集表的通用上限', report)
            self.assertIn('全部尝试（含旧版prompt）', report)
            self.assertIn('定稿当前版本', report)
            self.assertIn('|prompt版本|尝试|已逐条核验|未逐条核验|', report)
            self.assertIn('整体缓存观测', report)
            self.assertIn('S46单份资料观测（不是总体结论）', report)
            self.assertIn('新增候选无效率', report)
            self.assertIn('不进入80条semantic样本的语义错误率', report)
            self.assertIn('original_source_tokens', (root / 'overall-cost-observations.json').read_text())
            self.assertIn('不视为零错误', (root / 'USAGE.md').read_text())
            self.assertTrue((root / 'schema-diagnostics.json').exists())

    def test_reviews_require_explicit_queue_binding(self):
        queues = s2_compare.make_review_queues({'items': [], 'reverse_unmatched': [], 'extraction_complete': False}, [])
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            s2_compare.apply_reviews(queues, {'queues_sha256': 'wrong', 'decisions': []})


if __name__ == '__main__':
    unittest.main()
