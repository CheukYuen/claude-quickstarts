"""Synthetic split-capacity observability; no model calls or licensed text."""
from copy import deepcopy
from pathlib import Path
import unittest

import s2
import s2_report
from run import sha, write_json
import test_s2_collect


class CapacityObservationTests(unittest.TestCase):
    setUp = test_s2_collect.CollectionProvenanceTests.setUp
    make_chunk = test_s2_collect.CollectionProvenanceTests.make_chunk
    make_accepted = test_s2_collect.CollectionProvenanceTests.make_accepted

    def attempt(self, chunk, number, status='completed', **extra):
        value = dict(chunk_id=chunk['id'], chunk_sha256=chunk['sha256'], call_number=number,
            stage='extract-' + chunk['id'], status=status, mode='coverage', prompt_version=s2.PROMPT_VERSION,
            prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md'))
        value.update(extra)
        return value

    def split(self):
        children = [self.make_chunk(self.chunk['id'] + suffix, [self.fragment]) for suffix in ('.a', '.b')]
        write_json(self.root / 'splits' / (self.chunk['id'] + '.json'),
            dict(parent=self.chunk, children=children, reason='timed_out',
                 prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md')))
        (self.root / 'accepted' / (self.chunk['id'] + '.json')).unlink()
        (self.root / 'CHUNKS.md').write_text('# Synthetic initial plan\n')
        return children, s2_report.update_split_chunks_markdown(self.root)

    def test_failed_parent_and_current_leaves_show_real_bound_call_costs(self):
        (first, second), topology = self.split()
        accepted_a, accepted_b = self.make_accepted(first), self.make_accepted(second)
        self.assertEqual(len(s2.collect_records(self.root)[0]), 2)
        calls = [self.attempt(self.chunk, 1, 'timed_out', seconds=90.125, usage=dict(output_tokens=5)),
                 self.attempt(first, 2, seconds=12.5, usage=dict(output_tokens=88), log=accepted_a['log']),
                 self.attempt(second, 3, seconds=11.25, usage=dict(output_tokens=99), log=accepted_b['log']),
                 self.attempt(self.chunk, 4, 'failed', seconds=900, prompt_template_sha256='obsolete'),
                 self.attempt(first, 5, 'failed', seconds=900, chunk_sha256='obsolete'),
                 self.attempt(second, 6, 'failed', seconds=900, mode='request')]
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        result = s2_report.capacity_observations(self.root, calls, topology)
        parent, child_a, child_b = result['rows']
        self.assertEqual((parent['id'], parent['chars'], parent['status'], parent['seconds'], parent['output_tokens']),
                         (self.chunk['id'], self.chunk['chars'], 'timed_out', 90.125, 5))
        self.assertIsNone(parent['accepted_records'])
        self.assertFalse(parent['eligible_for_valid_density'])
        self.assertEqual([row['id'] for row in result['rows']], [self.chunk['id'], first['id'], second['id']])
        self.assertEqual((child_a['seconds'], child_a['output_tokens'], child_a['accepted_records']), (12.5, 88, 1))
        self.assertEqual((child_b['seconds'], child_b['output_tokens'], child_b['accepted_records']), (11.25, 99, 1))
        self.assertTrue(all(row['eligible_for_valid_density'] for row in (child_a, child_b)))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        text = s2_report.capacity_observations_markdown(result, 45)
        self.assertIn('当前上限45字符', text)
        self.assertIn('失败父块', text)
        self.assertIn('90.125', text)
        self.assertIn('|12.500|88|1|', text)
        self.assertIn('不能验证45字符是密集表的通用上限', text)
        self.assertIn('不计有效记录密度', text)

    def test_unknown_costs_and_unaccepted_outputs_never_become_zero_or_accepted(self):
        (first, second), topology = self.split()
        calls = [self.attempt(first, 1, 'running'),
                 self.attempt(second, 2, 'completed', usage=dict(output_tokens=70), accepted_records=8)]
        result = s2_report.capacity_observations(self.root, calls, topology)
        parent, child_a, child_b = result['rows']
        self.assertIsNone(parent['status'])
        self.assertIsNone(parent['seconds'])
        self.assertIsNone(parent['output_tokens'])
        self.assertIsNone(parent['accepted_records'])
        self.assertEqual(child_a['status'], 'running')
        self.assertIsNone(child_a['seconds'])
        self.assertIsNone(child_a['output_tokens'])
        self.assertIsNone(child_a['accepted_records'])
        # A completed CLI and an attempt's claimed count cannot replace the
        # current stored acceptance receipt for capacity's accepted count.
        self.assertEqual(child_b['output_tokens'], 70)
        self.assertIsNone(child_b['accepted_records'])
        self.assertFalse(any(row['eligible_for_valid_density'] for row in result['rows']))
        self.assertIn('未测得', s2_report.capacity_observations_markdown(result, None))

    def test_child_retry_cost_is_retained_once_per_call_with_only_final_acceptance(self):
        (first, _second), topology = self.split()
        accepted = self.make_accepted(first)
        calls = [self.attempt(first, 2, 'failed', seconds=7.5, usage=dict(output_tokens=5), log=str(self.root / 'failed')),
                 self.attempt(first, 3, seconds=12.5, usage=dict(output_tokens=88), log=accepted['log'])]
        result = s2_report.capacity_observations(self.root, calls, topology)
        rows = [row for row in result['rows'] if row['id'] == first['id']]
        self.assertEqual([row['call_number'] for row in rows], [2, 3])
        self.assertEqual([row['seconds'] for row in rows], [7.5, 12.5])
        self.assertEqual([row['accepted_records'] for row in rows], [None, 1])
        self.assertEqual([row['eligible_for_valid_density'] for row in rows], [False, True])

    def test_rechunk_evidence_and_ideal_budget_arithmetic_are_dynamic(self):
        history = self.root / 'chunk-history/synthetic-old'
        write_json(history / 'PREPARATION.json', dict(core_char_limit=20))
        old = [deepcopy(self.chunk), dict(id='old-a', chars=11, sha256='old-a'),
               dict(id='old-b', chars=17, sha256='old-b')]
        write_json(history / 'chunks.json', old)
        merged = dict(id='S46.policy', source='S46', chars=28, sha256='new-policy', sections=['合成非数值政策'])
        write_json(self.root / 'chunks.json', [self.chunk, merged])
        write_json(self.root / 'RECHUNK-REPORT.json', dict(status='installed', max_chars=30,
            history=str(history), reason='只合并两个合成非数值政策片段', protected_ids=[self.chunk['id']],
            protected_hashes_unchanged=True, first_note7_content_unchanged=True,
            content_mapping=[dict(old_id=self.chunk['id'], chars=self.chunk['chars'], unchanged_at=[self.chunk['id']]),
                             dict(old_id='old-a', chars=11, unchanged_at=[]),
                             dict(old_id='old-b', chars=17, unchanged_at=[])]))
        accepted = self.make_accepted(self.chunk, stage='pilot-r2-S46')
        calls = [dict(stage='pilot-r1-schema', is_probe=True), dict(stage='pilot-r1-S46', chunk_id=self.chunk['id']),
                 dict(stage='pilot-r2-schema', is_probe=True),
                 self.attempt(self.chunk, 4, stage='pilot-r2-S46', log=accepted['log'])]
        result = s2_report.capacity_observations(self.root, calls, dict(parents=[], leaves=[]))
        adjustment = result['rechunk_adjustment']
        self.assertEqual((adjustment['old_limit'], adjustment['new_limit']), (20, 30))
        self.assertEqual([row['chars'] for row in adjustment['changed_old_chunks']], [11, 17])
        self.assertEqual([row['chars'] for row in adjustment['changed_new_chunks']], [28])
        budget = result['initial_budget_plan']
        self.assertEqual((budget['root_chunks'], budget['registered_pilot_calls']), (2, 4))
        self.assertEqual(budget['current_pilot_roots'], [self.chunk['id']])
        self.assertEqual(budget['ideal_total_calls_without_extra_failures'], 5)
        self.assertEqual(budget['allowance_without_extra_failures'], s2.CALL_LIMIT - 5)
        text = s2_report.capacity_observations_markdown(result, 30)
        self.assertIn('上限20→30字符', text)
        self.assertIn('11+17字符→变动新块28字符', text)
        self.assertIn('只合并两个合成非数值政策片段', text)
        self.assertIn('首块合并项目附注的哈希不变：True', text)
        self.assertIn('理想合计5/%s次' % s2.CALL_LIMIT, text)


if __name__ == '__main__':
    unittest.main()
