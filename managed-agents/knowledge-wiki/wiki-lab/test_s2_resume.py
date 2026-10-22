"""Synthetic no-call resume and gate checks; do not read any real run inputs."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import s2
from run import load, sha, write_json
import test_s2_collect


class ResumePolicyTests(unittest.TestCase):
    setUp = test_s2_collect.CollectionProvenanceTests.setUp
    make_chunk = test_s2_collect.CollectionProvenanceTests.make_chunk
    make_accepted = test_s2_collect.CollectionProvenanceTests.make_accepted

    def pipeline(self):
        return s2.S2Pipeline(self.root, SimpleNamespace(codex='never-invoke', stage_timeout=1))

    def test_valid_current_success_resumes_without_model_call(self):
        pipeline = self.pipeline()
        with mock.patch.object(pipeline, 'extract_record_chunk', side_effect=AssertionError('model call forbidden')) as call:
            result = pipeline.run_chunk(self.chunk, transport=True)
        self.assertEqual(result['records'], self.result['records'])
        call.assert_not_called()

    def test_resume_checks_logged_tool_and_guide_metadata_before_any_call(self):
        path = Path(self.result['log']) / 'record.json'
        original = load(path)
        variants = [dict(original, field_guide_sha256='changed-guide'),
                    dict(original, tools=[dict(type='web_search')]), dict(original, status='failed')]
        for variant in variants:
            write_json(path, variant)
            pipeline = self.pipeline()
            with self.subTest(variant=variant), \
                 mock.patch.object(pipeline, 'extract_record_chunk', side_effect=AssertionError('model call forbidden')) as call, \
                 self.assertRaises(ValueError):
                pipeline.run_chunk(self.chunk, transport=True)
            call.assert_not_called()

    def test_resume_does_not_rewrite_a_tampered_schema_before_detecting_it(self):
        write_json(self.schema, {'tampered': True})
        before = self.schema.read_bytes()
        pipeline = self.pipeline()
        with mock.patch.object(pipeline, 'extract_record_chunk', side_effect=AssertionError('model call forbidden')) as call, \
             self.assertRaises(ValueError):
            pipeline.run_chunk(self.chunk, transport=True)
        self.assertEqual(self.schema.read_bytes(), before)
        call.assert_not_called()

    def test_historical_template_success_is_not_reused_as_current(self):
        path = self.root / 'accepted' / (self.chunk['id'] + '.json')
        previous = deepcopy(self.result)
        previous['binding']['prompt_template_sha256'] = 'historical-template'
        previous['binding']['prompt_version'] = 'historical-version'
        write_json(path, previous)
        pipeline = self.pipeline()
        with mock.patch.object(pipeline, 'extract_record_chunk', side_effect=AssertionError('model call forbidden')) as call, \
             self.assertRaises(ValueError):
            pipeline.run_chunk(self.chunk, transport=True)
        call.assert_not_called()

    def test_stale_split_metadata_fails_before_child_model_calls(self):
        (self.root / 'accepted' / (self.chunk['id'] + '.json')).unlink()
        split = dict(parent=deepcopy(self.chunk), children=[dict(self.chunk, id='S46.child')],
                     prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md'))
        variants = [dict(split, prompt_template_sha256='historical-template'),
                    dict(split, parent=dict(self.chunk, id='different-parent')),
                    dict(split, children=[])]
        for variant in variants:
            write_json(self.root / 'splits' / (self.chunk['id'] + '.json'), variant)
            pipeline = self.pipeline()
            with self.subTest(variant=variant), \
                 mock.patch.object(pipeline, 'extract_record_chunk', side_effect=AssertionError('model call forbidden')) as call, \
                 self.assertRaises(ValueError):
                pipeline.run_chunk(self.chunk, transport=True)
            call.assert_not_called()

    def pilot_fixture(self):
        source = deepcopy(self.source)
        source['id'] = 'S34'
        source['source_id'] = 'synthetic-periodic'
        second = self.make_chunk('S34.s02', [self.fragment])
        second.update(source='S34', source_id=source['source_id'], sections=['第二节 Synthetic section'])
        note = self.make_chunk('S34.s03', [self.fragment])
        note.update(source='S34', source_id=source['source_id'], sections=['七、合并财务报表项目注释'],
                    note_group='七、合并财务报表项目注释')
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source, source]))
        write_json(self.root / 'chunks.json', [self.chunk, second, note])
        s46 = self.make_accepted(self.chunk, stage='pilot-r2-S46')
        original = self.source
        self.source = source
        self.make_accepted(second, stage='pilot-r2-S34-section2')
        self.make_accepted(note, stage='pilot-r2-S34-note7')
        self.source = original
        write_json(self.root / 'pilot-r2-schema.json', dict(ok=True, transport=True))
        return s46

    def gate(self):
        actual_load = s2.load
        reference = s2.HERE / 'runs/s1-20261021/records/S46.json'
        def synthetic_load(path):
            return [] if Path(path) == reference else actual_load(path)
        with mock.patch.object(s2, 'load', side_effect=synthetic_load), \
             mock.patch('s2_compare.pilot_match', return_value=dict(passed=True)):
            return s2.pilot_gate(self.root, 2)

    def test_gate_accepts_current_bound_pilots_and_rejects_changed_final(self):
        s46 = self.pilot_fixture()
        self.assertTrue(self.gate()['ok'])
        (Path(s46['log']) / 'final.json').write_text('{"tampered":true}')
        gate = self.gate()
        self.assertFalse(gate['ok'])
        self.assertTrue(any('S46: pilot provenance check failed' in error for error in gate['errors']))


if __name__ == '__main__':
    unittest.main()
