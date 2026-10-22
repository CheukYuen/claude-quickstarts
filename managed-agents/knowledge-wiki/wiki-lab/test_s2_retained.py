"""Synthetic explicit retained-review transfers; no judgments are generated."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest import mock

import s2_compare
import s2_readback
import test_s2_readback


class RetainedReviewTests(unittest.TestCase):
    row = test_s2_readback.ReadbackTests.row
    save_queues = test_s2_readback.ReadbackTests.save_queues

    def setUp(self):
        test_s2_readback.ReadbackTests.setUp(self)
        self.old_queue_hash = s2_compare.json_hash(self.queues)
        test_s2_readback.write_json(self.root / 'comparison.json', dict(items=deepcopy(self.queues['queues']['unmatched'])))
        self.old_readback_indices = []
        for queue in s2_readback.QUEUES:
            s2_readback.export_packets(self.root, queue, snapshot='old-sample')
            path = self.root / ('readback/old-sample/%s-index.json' % queue)
            self.old_readback_indices.append(dict(path=str(path.relative_to(self.root)),
                sha256=s2_readback._sha(path.read_bytes())))

    def entry(self, queue='semantic', key='synthetic-r0', **extra):
        value = dict(queue=queue, id=key, decision='正确' if queue == 'semantic' else
                     '新增的正确内容' if queue == 'reverse' else 'S2漏抽',
                     reason='Explicit synthetic authored judgment.', evidence_pages=[2], error_types=[])
        value.update(extra)
        return value

    def draft(self, entries=None, **extra):
        value = dict(reviewer='Explicit synthetic reviewer', review_id='old-review',
            queues_sha256=self.old_queue_hash,
            corpus_manifest_sha256=s2_readback._sha((self.root / 'corpus-manifest.json').read_bytes()),
            source_records_sha256=dict(S34=s2_readback._sha((self.root / 'records/S34.json').read_bytes())),
            readback_indices=deepcopy(self.old_readback_indices),
            decisions=entries if entries is not None else [self.entry()])
        value.update(extra)
        path = self.root / 'reviews/draft.json'
        test_s2_readback.write_json(path, value)
        return path, value

    def rebind(self, path, output='reviews/retained.json', review_id='final-review'):
        return s2_readback.rebind_retained_review(self.root, path, output, review_id)

    def test_export_stores_raw_file_hash_and_distinct_canonical_queue_hash(self):
        raw = (self.root / 'review-queues.json').read_bytes()
        result = s2_readback.export_packets(self.root, 'semantic', snapshot='with-canonical')
        self.assertEqual(result['queue_sha256'], s2_readback._sha(raw))
        self.assertEqual(result['queues_sha256'], s2_compare.json_hash(self.queues))
        self.assertNotEqual(result['queue_sha256'], result['queues_sha256'])
        compact = json.loads((self.root / 'readback/with-canonical/semantic-001-compact.json').read_bytes())
        self.assertEqual(compact['queue_sha256'], result['queue_sha256'])
        self.assertEqual(compact['queues_sha256'], result['queues_sha256'])

    def test_retains_only_current_intersection_and_preserves_original_decision(self):
        entries = [self.entry(note='Keep every authored field unchanged.'),
                   self.entry(key='synthetic-r1', decision='错误', error_types=['期间'], reason='Explicit wrong period.'),
                   self.entry('reverse', 'synthetic-r0'), self.entry('unmatched', 'S34.c1.I001')]
        path, draft = self.draft(entries)
        self.queues['queues']['semantic'] = self.queues['queues']['semantic'][:1]
        self.queues['queues']['unmatched'] = []
        self.queues['queues']['reverse'] = []
        self.save_queues()
        queue_before = (self.root / 'review-queues.json').read_bytes()
        records_before = (self.root / 'records/S34.json').read_bytes()
        result = self.rebind(path)
        self.assertEqual(result['decisions'], [entries[0]])
        self.assertEqual(result['queues_sha256'], s2_compare.json_hash(self.queues))
        self.assertEqual(result['retention']['original_queues_sha256'], self.old_queue_hash)
        self.assertEqual(result['retention']['draft_sha256'], s2_readback._sha(path.read_bytes()))
        self.assertEqual(result['retention']['original_reviewer'], draft['reviewer'])
        self.assertEqual(result['retention']['original_review_id'], 'old-review')
        self.assertEqual(len(result['retention']['skipped_not_selected']), 3)
        indices = result['retention']['readback_indices']
        self.assertEqual(len(indices), 3)
        for binding in indices:
            self.assertEqual(binding['sha256'], s2_readback._sha((self.root / binding['path']).read_bytes()))
            self.assertEqual(binding['queues_sha256'], self.old_queue_hash)
        semantic = next(index for index in indices if index['queue'] == 'semantic')
        self.assertEqual(semantic['selected_ids'], ['synthetic-r0', 'synthetic-r1', 'synthetic-r2'])
        reviewed = s2_compare.apply_reviews(self.queues, result)
        self.assertEqual(reviewed['review_summary']['semantic']['total'], 1)
        self.assertEqual(reviewed['review_summary']['semantic']['reviewed'], 1)
        self.assertEqual(reviewed['review_summary']['semantic']['counts'], {'正确': 1})
        self.assertFalse(result['decisions_created'])
        self.assertEqual((self.root / 'review-queues.json').read_bytes(), queue_before)
        self.assertEqual((self.root / 'records/S34.json').read_bytes(), records_before)

    def test_source_change_rejected_even_for_valid_entry_no_longer_selected(self):
        path, _ = self.draft([self.entry(key='synthetic-r1')])
        self.queues['queues']['semantic'] = self.queues['queues']['semantic'][:1]
        self.save_queues()
        changed = deepcopy(self.records)
        changed[1]['object_key']['indicator'] = 'Changed synthetic indicator'
        test_s2_readback.write_json(self.root / 'records/S34.json', changed)
        with self.assertRaisesRegex(ValueError, 'source records hash mismatch'):
            self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_semantic_reverse_require_old_queue_sha_and_unchanged_manifest(self):
        for queue in ('semantic', 'reverse'):
            path, _ = self.draft([self.entry(queue)], corpus_manifest_sha256='0' * 64)
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'corpus manifest hash mismatch'):
                self.rebind(path)
            path, value = self.draft([self.entry(queue)])
            value.pop('queues_sha256')
            test_s2_readback.write_json(path, value)
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'requires old queues_sha256'):
                self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_unknown_duplicate_or_unbound_id_rejected_before_selection(self):
        cases = [([self.entry(key='unknown-record')], {}, 'Unknown original stored record'),
                 ([self.entry('unmatched', 'unknown-item')], {}, 'Unknown original P1 item'),
                 ([self.entry(), self.entry()], {}, 'duplicate authored review entry'),
                 ([self.entry(key='synthetic-r1')], dict(source_records_sha256={}), 'lacks matching source records binding')]
        self.queues['queues']['semantic'] = []
        self.save_queues()
        for entries, fields, message in cases:
            path, _ = self.draft(entries, **fields)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_unselected_entries_still_need_explicit_legal_authored_judgments(self):
        self.queues['queues']['semantic'] = []
        self.save_queues()
        for entry in (self.entry(reason=''), self.entry(decision='auto-inferred'),
                      self.entry(decision='错误', error_types=[])):
            path, _ = self.draft([entry])
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_all_retained_queues_require_old_whole_queue_sha_and_indices(self):
        path, value = self.draft([self.entry('unmatched', 'S34.c1.I001')])
        value.pop('queues_sha256')
        test_s2_readback.write_json(path, value)
        with self.assertRaisesRegex(ValueError, 'requires old queues_sha256'):
            self.rebind(path)
        path, _ = self.draft([self.entry('unmatched', 'S34.c1.I001')], queues_sha256='not-a-hash')
        with self.assertRaisesRegex(ValueError, 'Old queues_sha256'):
            self.rebind(path, output='reviews/invalid.json')
        for queue in s2_readback.QUEUES:
            key = 'S34.c1.I001' if queue == 'unmatched' else 'synthetic-r0'
            path, _ = self.draft([self.entry(queue, key)], readback_indices=[])
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'requires saved readback_indices'):
                self.rebind(path)

    def test_old_indices_must_match_queue_hash_and_cover_each_reviewed_queue(self):
        semantic = next(value for value in self.old_readback_indices if '/semantic-' in value['path'])
        reverse = next(value for value in self.old_readback_indices if '/reverse-' in value['path'])
        path, _ = self.draft(readback_indices=[reverse])
        with self.assertRaisesRegex(ValueError, 'Missing old readback index for reviewed queue'):
            self.rebind(path)
        path, _ = self.draft(queues_sha256='0' * 64, readback_indices=[semantic])
        with self.assertRaisesRegex(ValueError, 'old queues_sha256 mismatch'):
            self.rebind(path)
        # A stored record may be a current reverse candidate without ever
        # having belonged to that queue's old sample; no transfer is allowed.
        self.queues['queues']['reverse'].append(self.row(self.records[2]))
        self.save_queues()
        path, _ = self.draft([self.entry('reverse', 'synthetic-r2')], readback_indices=[reverse])
        with self.assertRaisesRegex(ValueError, 'never selected in old readback sample'):
            self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_missing_or_changed_old_index_and_malformed_selected_ids_are_rejected(self):
        semantic = next(value for value in self.old_readback_indices if '/semantic-' in value['path'])
        path, _ = self.draft(readback_indices=['readback/missing-index.json'])
        with self.assertRaisesRegex(ValueError, 'Saved readback index is missing'):
            self.rebind(path)
        wrong = dict(semantic, sha256='0' * 64)
        path, _ = self.draft(readback_indices=[wrong])
        with self.assertRaisesRegex(ValueError, 'raw SHA mismatch'):
            self.rebind(path)
        index_path = self.root / semantic['path']
        original = json.loads(index_path.read_bytes())
        for mutate, message in ((lambda index: index.update(row_count=99), 'selected count'),
                (lambda index: index['packets'][0]['row_ids'].append('synthetic-r0'), 'row identities disagree'),
                (lambda index: index.update(corpus_manifest_sha256='0' * 64), 'corpus manifest hash mismatch')):
            index = deepcopy(original)
            mutate(index)
            test_s2_readback.write_json(index_path, index)
            path, _ = self.draft(readback_indices=[semantic['path']])
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_one_old_index_must_cover_all_authored_ids_for_its_queue(self):
        semantic = next(value for value in self.old_readback_indices if '/semantic-' in value['path'])
        original = json.loads((self.root / semantic['path']).read_bytes())
        references = []
        for number, key in enumerate(('synthetic-r0', 'synthetic-r1')):
            index = deepcopy(original)
            index['row_count'] = 1
            packet = index['packets'][0]
            packet['row_ids'] = [key]
            packet['rows'] = [row for row in packet['rows'] if row['row_id'] == key]
            index['packets'] = [packet]
            path = self.root / ('readback/partial-%d-index.json' % number)
            test_s2_readback.write_json(path, index)
            references.append(str(path.relative_to(self.root)))
        path, _ = self.draft([self.entry(), self.entry(key='synthetic-r1')], readback_indices=references)
        with self.assertRaisesRegex(ValueError, 'No single old readback index covers all authored decisions'):
            self.rebind(path)
        self.assertFalse((self.root / 'reviews/retained.json').exists())

    def test_index_path_cannot_escape_run_or_be_replaced_by_review_output(self):
        semantic = next(value for value in self.old_readback_indices if '/semantic-' in value['path'])
        path, _ = self.draft(readback_indices=[str(Path(self.temp.name) / 'outside.json')])
        with self.assertRaisesRegex(ValueError, 'must stay within the run'):
            self.rebind(path)
        path, _ = self.draft(readback_indices=[semantic['path']])
        before = (self.root / semantic['path']).read_bytes()
        with self.assertRaisesRegex(ValueError, 'must not replace a saved readback index'):
            self.rebind(path, output=semantic['path'])
        self.assertEqual((self.root / semantic['path']).read_bytes(), before)

    def test_different_existing_output_or_unsafe_paths_never_overwritten(self):
        path, value = self.draft()
        result = self.rebind(path)
        output = self.root / 'reviews/retained.json'
        before = output.read_bytes()
        self.assertEqual(self.rebind(path), result)
        with self.assertRaisesRegex(ValueError, 'Existing retained review differs'):
            self.rebind(path, review_id='different-review')
        self.assertEqual(output.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'not a record or source'):
            self.rebind(path, output='records/S34.json')
        outside = Path(self.temp.name) / 'outside.json'
        with self.assertRaisesRegex(ValueError, 'must stay within the run'):
            self.rebind(path, output=outside)
        with self.assertRaisesRegex(ValueError, 'must not replace'):
            self.rebind(path, output='comparison.json')

    def test_cli_requires_explicit_action_and_reports_transfer_counts(self):
        path, _ = self.draft()
        output = io.StringIO()
        with mock.patch('sys.argv', ['s2_readback.py', str(self.root), '--rebind-retained', str(path),
                        '--review-id', 'explicit-cli-review', '--output', 'reviews/cli-retained.json']), redirect_stdout(output):
            s2_readback.main()
        status = json.loads(output.getvalue())
        self.assertEqual((status['decisions'], status['skipped_not_selected']), (1, 0))
        self.assertEqual(status['queues_sha256'], s2_compare.json_hash(self.queues))


if __name__ == '__main__':
    unittest.main()
