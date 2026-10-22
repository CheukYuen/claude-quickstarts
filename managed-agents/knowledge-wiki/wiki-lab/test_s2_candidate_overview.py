"""Candidate projections preserve the frozen existing candidate population."""
from copy import deepcopy
import json
import unittest

import s2_compare
import s2_readback
import test_s2_readback


class CandidateOverviewTests(unittest.TestCase):
    setUp = test_s2_readback.ReadbackTests.setUp
    row = test_s2_readback.ReadbackTests.row
    save_queues = test_s2_readback.ReadbackTests.save_queues

    def test_deduplicated_projection_matches_compact_candidates_and_binds_exact_fields(self):
        second = deepcopy(self.queues['queues']['unmatched'][0])
        second['item_id'] = 'S34.c1.I002'
        second['no_number_candidates'] = ['synthetic-r0']
        self.queues['queues']['unmatched'].append(second)
        self.save_queues()
        original = deepcopy(self.records)
        records_before = (self.root / 'records/S34.json').read_bytes()
        queues_before = (self.root / 'review-queues.json').read_bytes()
        index = s2_readback.export_packets(self.root, 'unmatched', snapshot='overview')
        packet = index['packets'][0]
        directory = self.root / 'readback/overview'
        compact = json.loads((directory / packet['compact_path']).read_bytes())
        data = (directory / packet['candidate_overview_path']).read_bytes()
        overview = json.loads(data)
        candidates = {key for row in compact['rows'] for key in row['candidate_record_ids']}
        self.assertEqual(set(overview['candidate_record_ids']), candidates)
        self.assertEqual({row['record_id'] for row in overview['records']} |
                         set(overview['unavailable_record_ids']), candidates)
        self.assertEqual(overview['unavailable_record_ids'], ['missing-candidate'])
        self.assertEqual(len(overview['records']), 1)
        record = overview['records'][0]
        self.assertEqual(set(record), {'record_id', 'record_type', 'pages', 'object_key', 'nature',
                                      'scope', 'dimensions', 'numbers', 'detail'})
        self.assertEqual(record, s2_readback._candidate_overview_record(self.records[0]))
        for key in ('record_id', 'record_type', 'object_key', 'nature', 'dimensions', 'numbers', 'detail'):
            self.assertEqual(record[key], self.records[0][key])
        self.assertEqual(record['pages'], self.records[0]['locator']['pdf_pages'])
        self.assertEqual(record['scope'], self.records[0]['detail'].get('scope'))
        self.assertEqual(packet['candidate_overview_sha256'], s2_readback._sha(data))
        self.assertEqual(compact['candidate_overview_sha256'], s2_readback._sha(data))
        self.assertEqual(compact['candidate_overview_path'], packet['candidate_overview_path'])
        self.assertEqual(overview['queues_sha256'], s2_compare.json_hash(self.queues))
        self.assertFalse(overview['judgments_created'])
        self.assertIn('](%s)' % packet['candidate_overview_path'], (directory / packet['path']).read_text())
        self.assertEqual(self.records, original)
        self.assertEqual((self.root / 'records/S34.json').read_bytes(), records_before)
        self.assertEqual((self.root / 'review-queues.json').read_bytes(), queues_before)
        projected = s2_readback._candidate_overview_record(self.records[0])
        projected['detail']['scope'] = 'synthetic changed projection'
        self.assertEqual(self.records, original)

    def test_changed_existing_overview_is_preserved_instead_of_overwritten(self):
        index = s2_readback.export_packets(self.root, 'unmatched', snapshot='immutable')
        directory = self.root / 'readback/immutable'
        overview = directory / index['packets'][0]['candidate_overview_path']
        overview.write_text('Explicit synthetic earlier snapshot content')
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with self.assertRaisesRegex(ValueError, 'Existing readback binding differs'):
            s2_readback.export_packets(self.root, 'unmatched', snapshot='immutable')
        self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)

    def test_retained_old_index_without_new_overview_fields_remains_compatible(self):
        index = s2_readback.export_packets(self.root, 'unmatched', snapshot='legacy')
        for packet in index['packets']:
            packet.pop('candidate_overview_path')
            packet.pop('candidate_overview_sha256')
        path = self.root / 'readback/legacy/old-index.json'
        test_s2_readback.write_json(path, index)
        decisions = [dict(queue='unmatched', id='S34.c1.I001')]
        inputs = {}
        bindings = s2_readback._retained_readback_indices(self.root.resolve(),
            dict(readback_indices=['readback/legacy/old-index.json']), decisions,
            index['queues_sha256'], index['corpus_manifest_sha256'], inputs)
        self.assertEqual(bindings[0]['selected_ids'], ['S34.c1.I001'])
        before = {entry.name: entry.read_bytes() for entry in path.parent.iterdir()}
        s2_readback.export_packets(self.root, 'unmatched', snapshot='new-final')
        self.assertEqual({entry.name: entry.read_bytes() for entry in path.parent.iterdir()}, before)


if __name__ == '__main__':
    unittest.main()
