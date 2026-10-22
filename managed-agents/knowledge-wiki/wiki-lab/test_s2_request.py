"""Synthetic request-planning and truncation-observation tests; no model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import s2
import s2_request
from run import load, write_json


class RequestFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.texts = {1: '合成封面。\n', 2: '第二节 合成指标\n收入 100 元\n被排除的主表：900元\n',
                      3: '第三节 合成附注\n借款 200 元\n'}
        page_records = []
        fragments = {}
        for number, text in self.texts.items():
            path = self.root / ('p%d.txt' % number)
            path.write_text(text)
            digest = hashlib.sha256(text.encode()).hexdigest()
            page_records.append(dict(page=number, corpus_path=str(path), text_sha256=digest))
            fragments[number] = dict(page=number, start=0, end=len(text), text=text, page_sha256=digest)
        self.source = dict(id='S34', source_id='synthetic-periodic', date='2026-03-01', page_records=page_records)
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        self.chunks = [dict(id='S34.s01', source='S34', source_id='S34', sections=['封面'],
            fragments=[fragments[1]], context_fragments=[], core_pages=[1], context_pages=[],
            units=[dict(title='封面', fragments=[fragments[1]], start=0, end=10)]),
            dict(id='S34.s02', source='S34', source_id='S34', sections=['第二节 合成指标'],
            fragments=[fragments[2]], context_fragments=[fragments[1]], core_pages=[2], context_pages=[1],
            units=[dict(title='第二节 合成指标', fragments=[fragments[2]], context_fragments=[fragments[1]], start=10, end=40)]),
            dict(id='S34.s03', source='S34', source_id='S34', sections=['第三节 合成附注'],
            fragments=[fragments[3]], context_fragments=[], core_pages=[3], context_pages=[],
            units=[dict(title='第三节 合成附注', fragments=[fragments[3]], start=40, end=60)])]
        write_json(self.root / 'chunks.json', self.chunks)
        removed_start = self.texts[2].index('被排除')
        write_json(self.root / 'excluded-pages.json', [dict(source='S34', pages=[2],
            fragments=[dict(page=2, start=removed_start, end=len(self.texts[2]), text=self.texts[2][removed_start:])])])
        self.coverage = [dict(item_id='A01', name='合成营业收入', status='未覆盖', record_ids=[],
                              checked_pages=[], basis=['请查第二节 合成指标，PDF p2。模型先前笔记秘密不得进入prompt。'])]
        self.save_coverage()

    def save_coverage(self):
        write_json(self.root / 'document-coverage.json', dict(S34=self.coverage))

    def plan(self, **kwargs):
        return s2_request.plan_requests(self.root, **kwargs)[0]


class RequestPlanTests(RequestFixture):
    def test_points_only_to_current_source_section_and_removes_statement_span(self):
        plan = self.plan()
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['item_ids'], ['A01'])
        self.assertEqual(plan['chunk']['core_pages'], [2])
        self.assertEqual(plan['chunk']['context_pages'], [1])
        text = Path(plan['chunk']['path']).read_text()
        self.assertIn('收入 100 元', text)
        self.assertNotIn('900元', text)
        self.assertNotIn('借款 200', text)
        self.assertNotIn('模型先前笔记秘密', plan['request'])
        self.assertEqual(plan['chunk']['sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_no_pointer_never_sends_the_whole_source(self):
        self.coverage[0].update(basis=['当前仍未覆盖。'], checked_pages=[])
        self.save_coverage()
        plan = self.plan()
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['reason'], 'no_available_pointer')
        self.assertIsNone(plan['chunk'])
        self.assertEqual(plan['unresolved_item_ids'], ['A01'])
        self.assertFalse((self.root / 'request-inputs').exists())

    def test_checked_page_is_an_explicit_fallback_pointer(self):
        self.coverage[0].update(basis=['当前仍未覆盖。'], checked_pages=[3])
        self.save_coverage()
        plan = self.plan()
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['chunk']['core_pages'], [3])
        self.assertEqual(plan['item_plans'][0]['method'], 'checked_pages')

    def test_named_section_without_page_pointer_selects_that_section(self):
        self.coverage[0].update(basis=['请查第三节 合成附注。'], checked_pages=[1, 2, 3])
        self.save_coverage()
        plan = self.plan()
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['chunk']['core_pages'], [3])
        self.assertEqual(plan['item_plans'][0]['method'], 'basis_sections')

    def test_overlimit_is_blocked_and_never_split_into_more_calls(self):
        plan = self.plan(max_chars=5)
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['reason'], 'blocked_overlimit')
        self.assertIsNone(plan['chunk'])
        self.assertGreater(plan['core_chars'], 5)
        self.assertFalse((self.root / 'request-inputs').exists())

    def test_failed_or_reserved_supplement_already_consumes_the_one_attempt(self):
        write_json(self.root / 'logs/request-S34/attempt-01/record.json',
                   dict(mode='request', chunk_id='S34.request1', status='failed'))
        plan = self.plan()
        self.assertFalse(plan['eligible'])
        self.assertEqual(plan['reason'], 'already_attempted')
        self.assertIsNone(plan['chunk'])

    def test_call_ledger_also_prevents_duplicate_after_launch_crash(self):
        (self.root / 'calls.jsonl').write_text(json.dumps(dict(number=1, stage='request-S34')) + '\n')
        self.assertEqual(self.plan()['reason'], 'already_attempted')

    def test_shared_span_dedup_and_previous_plan_history(self):
        plan = self.plan()
        original = (self.root / 'request-plans/S34.json').read_bytes()
        self.coverage.append(dict(item_id='A02', name='另一合成项', status='未覆盖', record_ids=[],
                                 checked_pages=[2], basis=['PDF p2']))
        self.save_coverage()
        revised = self.plan()
        self.assertEqual(revised['core_chars'], plan['core_chars'])
        self.assertEqual(revised['item_ids'], ['A01', 'A02'])
        histories = list((self.root / 'request-plans/history').glob('S34-*.json'))
        self.assertEqual(len(histories), 1)
        self.assertEqual(histories[0].read_bytes(), original)

    def test_foreign_source_page_and_plain_numbers_are_not_page_pointers(self):
        self.assertEqual(s2_request.page_pointers('应查S42 p3；金额455，期间2026。', 'S34'), [])
        self.assertEqual(s2_request.page_pointers('PDF pp2–4，第7至8页，页序10。', 'S34'), [2,3,4,7,8,10])

    def test_extracted_and_document_absent_items_get_no_supplement(self):
        self.coverage[0]['status'] = '已抽'
        self.save_coverage()
        self.assertEqual(self.plan()['reason'], 'no_uncovered_items')
        self.coverage[0]['status'] = '本文档无'
        self.save_coverage()
        self.assertEqual(self.plan()['reason'], 'no_uncovered_items')

    def test_incomplete_item_pointer_is_reported_without_blocking_resolvable_item(self):
        self.coverage.append(dict(item_id='A02', name='无指针项', status='未覆盖', checked_pages=[], basis=['暂缺']))
        self.save_coverage()
        plan = self.plan()
        self.assertTrue(plan['eligible'])
        self.assertEqual(plan['item_ids'], ['A01'])
        self.assertEqual(plan['unresolved_item_ids'], ['A02'])
        self.assertNotIn('无指针项', plan['request'])

    def test_changed_frozen_page_fails_before_request_text_is_written(self):
        Path(self.source['page_records'][1]['corpus_path']).write_text('tampered synthetic page')
        with self.assertRaisesRegex(ValueError, 'Frozen page changed'):
            self.plan()
        self.assertFalse((self.root / 'request-inputs').exists())


class TruncationObservationTests(unittest.TestCase):
    def observe(self, text=None, events='', stderr=''):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if text is not None:
                (root / 'final.json').write_text(text)
            (root / 'events.jsonl').write_text(events)
            (root / 'stderr.log').write_text(stderr)
            return s2.truncated_output(root)

    def test_final_closed_nested_record_but_unclosed_root_is_truncation(self):
        self.assertTrue(self.observe('{"records":[{"value":"123"}]'))

    def test_unclosed_string_and_escape_are_truncation(self):
        self.assertTrue(self.observe('{"record":"incomplete'))
        self.assertTrue(self.observe('{"record":"incomplete\\'))

    def test_brackets_and_escaped_quotes_inside_strings_are_not_truncation(self):
        self.assertFalse(self.observe(json.dumps(dict(record='a { [ ] } and "quoted" \\ slash'))))

    def test_runtime_output_limit_signal_detected_even_without_final_file(self):
        events = json.dumps(dict(type='turn.failed', error=dict(message='maximum output tokens exceeded')))
        self.assertTrue(self.observe(events=events))
        self.assertTrue(self.observe('{}', stderr='finish_reason: length'))

    def test_balanced_but_invalid_schema_or_syntax_is_not_called_truncation(self):
        self.assertFalse(self.observe('{"records":[]}'))
        self.assertFalse(self.observe('{bad json}'))


if __name__ == '__main__':
    unittest.main()
