"""Synthetic tests for S2 gates, immutable resume and bounded failure recovery."""
import concurrent.futures
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import records
import run as runner
import s2
import s2_report
from run import load, sha, write_json
from test_records import sample


class PolicyFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.here = Path(self.temp.name) / 'code'
        self.root = Path(self.temp.name) / 'run'
        self.root.mkdir()
        template = self.here / 'prompts/extract-records.md'
        template.parent.mkdir(parents=True)
        template.write_text('版本：' + s2.PROMPT_VERSION + '\nSynthetic extraction template\n'
                            '{{MODE}}\n{{SOURCE_METADATA}}\n{{REQUEST}}\n{{CHECKLIST}}\n'
                            '{{FIELD_GUIDE}}\n{{PAGE_TEXT}}\n')
        self.prompt_hash = sha(template)
        self.here_patch = mock.patch.object(s2, 'HERE', self.here)
        self.here_patch.start()
        self.addCleanup(self.here_patch.stop)
        # render_prompt uses run.template's module root. Bind both code roots
        # to the same real synthetic template rather than mocking validation.
        self.template_root_patch = mock.patch.object(runner, 'HERE', self.here)
        self.template_root_patch.start()
        self.addCleanup(self.template_root_patch.stop)
        self.checklist_patch = mock.patch.object(s2, 'checklist_for',
            return_value=dict(items=[dict(id='T01', name='synthetic revenue')]))
        self.checklist_patch.start()
        self.addCleanup(self.checklist_patch.stop)
        page = self.root / 'corpus/synthetic-p2.txt'
        page.parent.mkdir(parents=True)
        page.write_text('Revenue 1,250.50')
        self.source = dict(id='S46', source_id='synthetic-report', date='2026-03-01',
            page_records=[dict(page=2, corpus_path=str(page), text_sha256=sha(page))])
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        chunk_text = self.root / 'chunks/S46.s001.md'
        chunk_text.parent.mkdir()
        chunk_text.write_text('[PAGE: 2]\nRevenue 1,250.50')
        self.chunk = dict(id='S46.s001', source='S46', source_id='synthetic-report',
            sha256=sha(chunk_text), path=str(chunk_text), core_pages=[2], context_pages=[],
            fragments=[dict(page=2, text='Revenue 1,250.50')], context_fragments=[],
            sections=['Synthetic announcement'])
        write_json(self.root / 'chunks.json', [self.chunk])
        self.pipeline = s2.S2Pipeline(self.root, SimpleNamespace())

    def raw_output(self):
        item = sample()
        item['record_id'] = 'local-one'
        for field in records.CODE_FIELDS:
            item.pop(field)
        return dict(schema_version=records.VERSION, records=[item], coverage=[
            dict(item_id='T01', status='已抽', record_ids=['local-one'], checked_pages=[2], basis='Synthetic table')])

    def completed_log(self):
        stage = 'extract-' + self.chunk['id']
        log = self.root / 'logs' / stage / 'attempt-01'
        log.mkdir(parents=True)
        prompt = s2.render_prompt(self.source, Path(self.chunk['path']).read_text(), s2.checklist_for('S46'))
        (log / 'prompt.md').write_text(prompt)
        schema = self.pipeline.schemas(True)
        write_json(log / 'final.json', self.raw_output())
        write_json(log / 'record.json', dict(stage=stage, status='completed', call_number=1, command_count=0,
            prompt_version=s2.PROMPT_VERSION, tools=[],
            chunk_id=self.chunk['id'], chunk_sha256=self.chunk['sha256'], prompt_template_sha256=self.prompt_hash,
            schema_sha256=sha(schema), field_guide_sha256=hashlib.sha256(records.field_guide().encode()).hexdigest(),
            prompt_sha256=sha(log / 'prompt.md'), command=['codex', 'exec', '--output-schema', str(schema)]))
        return log

    def saved_success(self):
        return s2.adopt_pilot(self.root, self.chunk, self.completed_log())

    def failure_log(self, number, status='failed', **extra):
        log = self.root / 'logs' / ('failed-%02d' % number) / 'attempt-01'
        record = dict(stage=log.parent.name, status=status, call_number=number,
            chunk_id=self.chunk['id'], chunk_sha256=self.chunk['sha256'],
            prompt_template_sha256=self.prompt_hash)
        record.update(extra)
        write_json(log / 'record.json', record)
        return log


class PilotGateTests(PolicyFixture):
    def test_empty_s46_never_bypasses_reference_match_gate(self):
        chunks = [self.chunk,
            dict(id='S34.s002', source='S34', sections=['第二节 Synthetic information']),
            dict(id='S34.s007', source='S34', sections=['Synthetic note'], note_group='七、合并财务报表项目注释')]
        write_json(self.root / 'chunks.json', chunks)
        write_json(self.root / 'pilot-r1-schema.json', dict(ok=True, transport=True))
        write_json(self.here / 'runs/s1-20261021/records/S46.json', [sample()])
        for name in ('S46', 'S34-section2', 'S34-note7'):
            log = self.root / 'logs' / ('pilot-r1-' + name) / 'attempt-01'
            write_json(log / 'acceptance.json', dict(status='completed', records=[], output_records=0,
                rejected=[], rejection_rate=0))
            write_json(log / 'record.json', dict(command_count=0, prompt_template_sha256=self.prompt_hash))
        gate = s2.pilot_gate(self.root)
        self.assertFalse(gate['ok'])
        self.assertIn('S46: reference recall below 80%', gate['errors'])
        matched = load(self.root / 'pilot-r1-S46-match.json')
        self.assertEqual(matched['reference_match_ratio'], 0)
        self.assertEqual(len(matched['unmatched_reference']), 1)


class ImmutableResumeTests(PolicyFixture):
    def test_bound_success_resumes_without_call_and_rejects_same_count_final_tamper(self):
        saved = self.saved_success()
        with mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            self.assertEqual(self.pipeline.run_chunk(self.chunk)['records'], saved['records'])
            launch.assert_not_called()
        final = Path(saved['log']) / 'final.json'
        altered = load(final)
        altered['records'][0]['numbers'][0]['value'] = '999999'
        write_json(final, altered)
        self.assertEqual(len(altered['records']), saved['output_records'])
        with mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            with self.assertRaisesRegex(ValueError, '[Ff]inal response changed'):
                self.pipeline.run_chunk(self.chunk)
            launch.assert_not_called()

    def test_accepted_content_is_revalidated_against_original_final(self):
        saved = self.saved_success()
        path = self.root / 'accepted' / (self.chunk['id'] + '.json')
        saved['records'][0]['object_key']['indicator'] = 'tampered indicator'
        write_json(path, saved)
        with self.assertRaisesRegex(ValueError, 'Accepted response changed'):
            self.pipeline.run_chunk(self.chunk)

    def test_template_change_invalidates_bound_success(self):
        self.saved_success()
        (self.here / 'prompts/extract-records.md').write_text('synthetic prompt version two')
        with mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            with self.assertRaisesRegex(ValueError, 'binding changed|generated prompt changed'):
                self.pipeline.run_chunk(self.chunk)
            launch.assert_not_called()

    def test_generated_field_guide_change_invalidates_bound_success(self):
        self.saved_success()
        changed = records.field_guide() + '\nSynthetic guide revision.'
        with mock.patch.object(records, 'field_guide', return_value=changed), \
             mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                self.pipeline.run_chunk(self.chunk)
            launch.assert_not_called()

    def test_generated_schema_change_invalidates_bound_success(self):
        self.saved_success()
        original = records.model_output_schema
        def changed(transport=False):
            value = original(transport)
            value['title'] = 'Synthetic schema revision'
            return value
        with mock.patch.object(records, 'model_output_schema', side_effect=changed), \
             mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                self.pipeline.run_chunk(self.chunk)
            launch.assert_not_called()

    def test_adoption_rejects_stale_generated_guide_even_when_template_unchanged(self):
        log = self.completed_log()
        changed = records.field_guide() + '\nSynthetic guide revision.'
        with mock.patch.object(records, 'field_guide', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'different generated field guide'):
                s2.adopt_pilot(self.root, self.chunk, log)

    def test_adoption_rejects_changed_schema_file_when_launch_hash_is_known(self):
        log = self.completed_log()
        record = load(log / 'record.json')
        schema = Path(record['command'][-1])
        altered = load(schema)
        altered['title'] = 'Synthetic changed schema'
        write_json(schema, altered)
        with self.assertRaisesRegex(ValueError, 'logged schema changed'):
            s2.adopt_pilot(self.root, self.chunk, log)

    def test_same_prompt_version_cannot_change_generated_guide_snapshot(self):
        template = self.here / 'prompts/extract-records.md'
        template.write_text('版本：' + s2.PROMPT_VERSION + '\nSynthetic template')
        original = s2.snapshot_prompt(self.root)
        changed = records.field_guide() + '\nSynthetic guide revision.'
        with mock.patch.object(records, 'field_guide', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'Same prompt version changed'):
                s2.snapshot_prompt(self.root)
        self.assertEqual(load(self.root / 'prompt-versions' / s2.PROMPT_VERSION / 'binding.json'), original)
        self.assertFalse((self.root / 'calls.jsonl').exists())


class RecoveryPolicyTests(PolicyFixture):
    def test_persisted_split_resumes_children_without_replaying_parent(self):
        children = []
        for suffix in ('a', 'b'):
            child = deepcopy(self.chunk)
            child['id'] += '.' + suffix
            children.append(child)
        write_json(self.root / 'splits' / (self.chunk['id'] + '.json'),
            dict(parent=self.chunk, children=children, reason='timed_out', prompt_template_sha256=self.prompt_hash))
        with mock.patch.object(self.pipeline, 'extract_record_chunk',
                               side_effect=lambda chunk, **kwargs: dict(chunk_id=chunk['id'], status='completed')) as launch:
            result = self.pipeline.run_chunk(self.chunk)
        self.assertEqual(result['status'], 'split')
        self.assertEqual([call.args[0]['id'] for call in launch.call_args_list], [c['id'] for c in children])
        self.assertNotIn(self.chunk['id'], [call.args[0]['id'] for call in launch.call_args_list])

    def test_nontruncation_failure_gets_only_one_retry_across_resumes(self):
        self.failure_log(1)
        def fail_retry(chunk, **kwargs):
            self.failure_log(2)
            return dict(chunk_id=chunk['id'], status='failed')
        with mock.patch.object(self.pipeline, 'extract_record_chunk', side_effect=fail_retry) as launch:
            first = self.pipeline.run_chunk(self.chunk)
            second = self.pipeline.run_chunk(self.chunk)
        self.assertEqual(first['status'], 'failed')
        self.assertEqual(second['error'], 'one retry already used')
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(launch.call_args.kwargs['stage'], 'retry-' + self.chunk['id'])

    def test_recorded_timeout_splits_even_without_existing_split_manifest(self):
        self.failure_log(1, 'timed_out')
        with mock.patch.object(self.pipeline, 'extract_record_chunk') as launch, \
             mock.patch.object(self.pipeline, 'split_and_run', return_value=dict(status='split')) as split:
            result = self.pipeline.run_chunk(self.chunk)
        self.assertEqual(result['status'], 'split')
        split.assert_called_once_with(self.chunk, True, 'timed_out')
        launch.assert_not_called()

    def test_unreconciled_running_call_never_launches_duplicate(self):
        self.failure_log(1, 'running')
        with mock.patch.object(self.pipeline, 'extract_record_chunk') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Unreconciled running attempt'):
                self.pipeline.run_chunk(self.chunk)
            launch.assert_not_called()


class AtomicSchemaTests(PolicyFixture):
    def test_concurrent_schema_writers_never_expose_partial_json_to_readers(self):
        path = self.pipeline.schemas(True)
        expected = load(path)
        barrier = threading.Barrier(4)
        def writer():
            barrier.wait()
            for _ in range(15):
                self.pipeline.schemas(True)
        def reader():
            barrier.wait()
            for _ in range(90):
                self.assertEqual(load(path), expected)
                self.assertEqual(load(self.root / 'schemas/stored-records.json'), records.extract_output_schema())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(writer), pool.submit(writer), pool.submit(reader), pool.submit(reader)]
            for future in futures:
                future.result()


class FailureMetricsTests(PolicyFixture):
    def test_whole_block_failure_has_unknown_rejection_rate_even_with_raw_record_count(self):
        failed = self.failure_log(1)
        write_json(failed / 'acceptance.json', dict(status='invalid_output', output_records=7,
            error='Synthetic coverage envelope failure'))
        good = self.failure_log(2, 'completed')
        write_json(good / 'acceptance.json', dict(status='completed', output_records=2,
            records=[dict(record_id='good-1'), dict(record_id='good-2')], rejected=[], discarded=[]))
        attempts = [dict(log=str(failed), stage='failed'), dict(log=str(good), stage='good')]
        summary = s2_report.rejection_summary(self.root, attempts)
        self.assertIsNone(summary['blocks'][0]['rate'])
        self.assertIsNone(summary['blocks'][0]['rejected'])
        self.assertEqual(summary['measured_output_records'], 2)
        self.assertEqual(summary['rejected_records'], 0)
        self.assertEqual(summary['rate'], 0)

    def test_explicit_null_output_count_uses_attempt_backfill_without_inventing_reject_rate(self):
        failed = self.failure_log(1)
        write_json(failed / 'acceptance.json', dict(status='invalid_output', output_records=None))
        summary = s2_report.rejection_summary(self.root,
            [dict(log=str(failed), stage='failed', output_records=7)])
        self.assertEqual(summary['blocks'][0]['output_records'], 7)
        self.assertIsNone(summary['blocks'][0]['rejected'])
        self.assertIsNone(summary['blocks'][0]['rate'])
        self.assertEqual(summary['measured_output_records'], 0)
        # An observed zero is real data, not a missing count to replace.
        write_json(failed / 'acceptance.json', dict(status='invalid_output', output_records=0))
        summary = s2_report.rejection_summary(self.root,
            [dict(log=str(failed), stage='failed', output_records=7)])
        self.assertEqual(summary['blocks'][0]['output_records'], 0)

    def test_backfill_counts_legal_json_records_even_when_call_failed(self):
        log = self.failure_log(1)
        write_json(log / 'final.json', self.raw_output())
        attempts = s2.backfill_output_counts(self.root)
        self.assertEqual(attempts[0]['output_records'], 1)
        self.assertEqual(attempts[0]['status'], 'failed')
        self.assertTrue(attempts[0]['raw_output_count_observed'])


class CompletionReceiptTests(PolicyFixture):
    def test_budget_stop_can_finish_firstpass_only_with_complete_current_prompt_receipt(self):
        saved = self.saved_success()
        chunks = [self.chunk]
        write_json(self.root / 'full-extraction.json', dict(outcomes=[saved]))
        with mock.patch.object(s2_report, 'HERE', self.here):
            self.assertTrue(s2_report.extraction_complete(self.root, chunks, 'budget-stopped'))
            self.assertTrue(s2_report.extraction_complete(self.root, chunks, 'full'))
            self.assertFalse(s2_report.extraction_complete(self.root, chunks, 'in-progress'))

    def test_completed_outcomes_cannot_replace_missing_or_invalid_accepted_provenance(self):
        saved = self.saved_success()
        write_json(self.root / 'full-extraction.json', dict(outcomes=[saved]))
        accepted = self.root / 'accepted' / (self.chunk['id'] + '.json')
        with mock.patch.object(s2_report, 'HERE', self.here):
            accepted.unlink()
            self.assertFalse(s2_report.extraction_complete(self.root, [self.chunk], 'full'))
            write_json(accepted, saved)
            self.assertTrue(s2_report.extraction_complete(self.root, [self.chunk], 'full'))
            final = Path(saved['log']) / 'final.json'
            original = final.read_bytes()
            altered = load(final)
            altered['records'][0]['numbers'][0]['value'] = '9999'
            write_json(final, altered)
            self.assertFalse(s2_report.extraction_complete(self.root, [self.chunk], 'full'))
            final.write_bytes(original)
            changed = deepcopy(saved)
            changed['records'][0]['object_key']['indicator'] = 'Tampered synthetic indicator'
            write_json(accepted, changed)
            self.assertFalse(s2_report.extraction_complete(self.root, [self.chunk], 'budget-stopped'))

    def split_success(self):
        first = deepcopy(self.chunk)
        page = self.root / 'corpus/synthetic-p3.txt'
        page.write_text('Revenue 1,250.50')
        self.source['page_records'].append(dict(page=3, corpus_path=str(page), text_sha256=sha(page)))
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        second = deepcopy(first)
        second.update(id='S46.s002', core_pages=[3], fragments=[dict(page=3, text=page.read_text())])
        second_path = self.root / 'chunks/S46.s002.md'
        second_path.write_text('[PAGE: 3]\n' + page.read_text())
        second.update(path=str(second_path), sha256=sha(second_path))
        parent_path = self.root / 'chunks/S46.parent.md'
        parent_path.write_text(Path(first['path']).read_text() + '\n' + second_path.read_text())
        parent = deepcopy(first)
        parent.update(id='S46.parent', path=str(parent_path), sha256=sha(parent_path), core_pages=[2, 3],
                      fragments=first['fragments'] + second['fragments'])
        write_json(self.root / 'chunks.json', [parent])
        write_json(self.root / 'splits/S46.parent.json', dict(parent=parent, children=[first, second],
            prompt_template_sha256=self.prompt_hash))
        self.chunk = first
        first_saved = self.saved_success()
        self.chunk = second
        log = self.completed_log()
        raw = self.raw_output()
        raw['records'][0]['locator']['pdf_pages'] = [3]
        for excerpt in raw['records'][0]['excerpts']:
            excerpt['page'] = 3
        raw['coverage'][0]['checked_pages'] = [3]
        write_json(log / 'final.json', raw)
        second_saved = s2.adopt_pilot(self.root, second, log)
        self.chunk = parent
        write_json(self.root / 'full-extraction.json', dict(outcomes=[dict(chunk_id=parent['id'], status='split',
            children=[first_saved, second_saved])]))
        return parent, first, second

    def test_split_completion_requires_each_current_leaf_and_not_failed_parent_receipt(self):
        parent, first, second = self.split_success()
        with mock.patch.object(s2_report, 'HERE', self.here):
            self.assertTrue(s2_report.extraction_complete(self.root, [parent], 'full'))
            (self.root / 'accepted' / (second['id'] + '.json')).unlink()
            self.assertFalse(s2_report.extraction_complete(self.root, [parent], 'full'))
            (self.root / 'accepted' / (first['id'] + '.json')).unlink()
            write_json(self.root / 'accepted' / (parent['id'] + '.json'),
                dict(chunk_id=parent['id'], status='failed', binding=dict(prompt_template_sha256=self.prompt_hash)))
            self.assertFalse(s2_report.extraction_complete(self.root, [parent], 'budget-stopped'))

    def test_supplement_gate_blocks_missing_leaf_before_planning_or_model_call(self):
        import s2_request
        saved = self.saved_success()
        write_json(self.root / 'full-extraction.json', dict(outcomes=[saved]))
        (self.root / 'accepted' / (self.chunk['id'] + '.json')).unlink()
        with mock.patch.object(s2_report, 'HERE', self.here), \
             mock.patch.object(s2_request, 'plan_requests') as plan, \
             mock.patch.object(self.pipeline, 'extract_record_chunk') as launch, \
             mock.patch.object(s2, 'collect_records') as collect:
            with self.assertRaisesRegex(ValueError, 'requires completed full extraction'):
                s2.supplement(self.pipeline)
            plan.assert_not_called()
            launch.assert_not_called()
            collect.assert_not_called()

    def test_budget_stop_does_not_turn_partial_failed_or_stale_receipt_into_completion(self):
        chunks = [self.chunk, dict(id='S34.s002')]
        good = dict(status='completed', binding=dict(prompt_template_sha256=self.prompt_hash))
        cases = [
            [dict(chunk_id=self.chunk['id'], **deepcopy(good))],
            [dict(chunk_id=self.chunk['id'], **deepcopy(good)), dict(chunk_id='S34.s002', status='stopped')],
            [dict(chunk_id=self.chunk['id'], **deepcopy(good)),
                dict(chunk_id='S34.s002', status='completed', binding=dict(prompt_template_sha256='stale'))],
            [dict(chunk_id=self.chunk['id'], **deepcopy(good)), dict(chunk_id='S34.s002', status='split', children=[])],
            [dict(chunk_id=self.chunk['id'], **deepcopy(good)), dict(chunk_id=self.chunk['id'], **deepcopy(good))],
        ]
        with mock.patch.object(s2_report, 'HERE', self.here):
            for outcomes in cases:
                with self.subTest(outcomes=outcomes):
                    write_json(self.root / 'full-extraction.json', dict(outcomes=outcomes))
                    self.assertFalse(s2_report.extraction_complete(self.root, chunks, 'budget-stopped'))


if __name__ == '__main__':
    unittest.main()
