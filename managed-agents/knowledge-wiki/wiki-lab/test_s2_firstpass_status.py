"""Synthetic firstpass observability after the real collection provenance checks."""
from copy import deepcopy
from pathlib import Path
import unittest

import s2
import s2_report
from run import load, sha, write_json
import test_s2_collect


class FirstpassStatusTests(unittest.TestCase):
    setUp = test_s2_collect.CollectionProvenanceTests.setUp
    make_chunk = test_s2_collect.CollectionProvenanceTests.make_chunk
    make_accepted = test_s2_collect.CollectionProvenanceTests.make_accepted

    def attempt(self, chunk, number, status='completed', **extra):
        value = dict(chunk_id=chunk['id'], chunk_sha256=chunk['sha256'], call_number=number,
            status=status, mode='coverage', prompt_version=s2.PROMPT_VERSION,
            prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md'))
        value.update(extra)
        return value

    def test_completed_failed_and_unstarted_leaves_have_explicit_call_states(self):
        failed = self.make_chunk('S46.failed', [self.fragment])
        waiting = self.make_chunk('S46.waiting', [self.fragment])
        write_json(self.root / 'chunks.json', [self.chunk, failed, waiting])
        log = self.root / 'logs/synthetic-failed/attempt-01'
        write_json(log / 'acceptance.json', dict(status='invalid_output', error='Synthetic whole-output schema error'))
        calls = [self.attempt(self.chunk, 4, log=self.result['log']),
                 self.attempt(failed, 5, s2_status='invalid_output', log=str(log))]
        # Use existing collection to prove the completed response; the status
        # helper supplies observability, not an alternate acceptance path.
        self.assertEqual(len(s2.collect_records(self.root)[0]), 1)
        status = s2_report.firstpass_chunk_status(self.root, calls, 'in-progress')
        rows = {row['id']: row for row in status['rows']}
        self.assertEqual(rows[self.chunk['id']]['status'], 'completed')
        self.assertTrue(rows[self.chunk['id']]['accepted_complete'])
        self.assertEqual(rows[failed['id']]['status'], 'invalid_output')
        self.assertEqual(rows[failed['id']]['calls'][0]['call_number'], 5)
        self.assertEqual(rows[failed['id']]['last_error'], 'Synthetic whole-output schema error')
        self.assertEqual(rows[waiting['id']]['status'], 'not_started')
        self.assertEqual(rows[waiting['id']]['calls'], [])
        self.assertEqual((status['leaves'], status['completed'], status['incomplete']), (3, 1, 2))
        stopped = s2_report.firstpass_chunk_status(self.root, calls, 'budget-stopped')
        self.assertEqual(next(row for row in stopped['rows'] if row['id'] == waiting['id'])['status'], 'budget_not_started')
        text = s2_report.firstpass_status_markdown(stopped)
        self.assertIn('firstpass-chunk-status.json', text)
        self.assertIn(waiting['id'], text)
        self.assertNotIn('|S46/' + self.chunk['id'] + '|', text)

    def test_only_active_split_leaves_are_listed_and_request_chunks_are_omitted(self):
        page = self.root / 'pages/p3.txt'
        page.write_text('Synthetic split leaf 50')
        fragment = dict(page=3, start=0, end=len(page.read_text()), text=page.read_text(), page_sha256=sha(page))
        self.source['pages'].append(3)
        self.source['page_records'].append(dict(page=3, corpus_path=str(page), text_sha256=sha(page)))
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        parent = self.make_chunk('S46.parent', [self.fragment, fragment])
        sibling = self.make_chunk('S46.split-b', [fragment])
        request = self.make_chunk('S46.request1', [self.fragment])
        write_json(self.root / 'chunks.json', [parent])
        write_json(self.root / 'splits/S46.parent.json', dict(parent=parent, children=[self.chunk, sibling],
            prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md')))
        write_json(self.root / 'request-plans/S46.json', dict(source='S46', chunk=request, request='Synthetic request'))
        calls = [self.attempt(parent, 3, 'timed_out'), self.attempt(self.chunk, 4, log=self.result['log']),
                 self.attempt(request, 6, mode='request')]
        self.assertEqual(len(s2.collect_records(self.root)[0]), 1)
        status = s2_report.firstpass_chunk_status(self.root, calls, 'budget-stopped')
        self.assertEqual({row['id'] for row in status['rows']}, {self.chunk['id'], sibling['id']})
        self.assertEqual(next(row for row in status['rows'] if row['id'] == sibling['id'])['status'], 'budget_not_started')

    def test_previous_template_call_does_not_mark_current_input_started(self):
        (self.root / 'accepted' / (self.chunk['id'] + '.json')).unlink()
        historical = self.attempt(self.chunk, 1, 'failed', prompt_template_sha256='older-template')
        stale_input = self.attempt(self.chunk, 2, 'failed', chunk_sha256='different-input')
        self.assertEqual(s2.collect_records(self.root)[0], [])
        row = s2_report.firstpass_chunk_status(self.root, [historical, stale_input], 'in-progress')['rows'][0]
        self.assertEqual(row['status'], 'not_started')
        self.assertEqual(row['calls'], [])


if __name__ == '__main__':
    unittest.main()
