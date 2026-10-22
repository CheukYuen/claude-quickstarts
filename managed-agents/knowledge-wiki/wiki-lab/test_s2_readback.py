"""Readback exporter tests use only synthetic records, items and page text."""
from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import s2_readback
import s2_compare
from test_records import sample


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'runs/s2-synthetic'
        self.root.mkdir(parents=True)
        self.page_text = 'Unit: thousands.\r\nRevenue 1,250.50\r\n```unquoted synthetic footer```\r\n'
        page = self.root / 'corpus/S34/p2.txt'
        page.parent.mkdir(parents=True)
        page.write_bytes(self.page_text.encode())
        self.manifest = dict(sources=[dict(id='S34', source_id='synthetic-report', page_records=[
            dict(page=2, corpus_path=str(page), text_sha256=hashlib.sha256(page.read_bytes()).hexdigest())])])
        self.records = []
        for i in range(3):
            record = sample()
            record['record_id'] = 'synthetic-r%d' % i
            self.records.append(record)
        self.queues = dict(queues=dict(semantic=[self.row(r) for r in self.records],
            reverse=[self.row(self.records[0])], unmatched=[dict(item_id='S34.c1.I001', source='S34', pages=[2],
                text='Synthetic P1 item: revenue 1250.50 [S34 p2]', context=[],
                no_number_candidates=['synthetic-r0', 'missing-candidate'],
                review=dict(status='pending', decision=None, evidence_pages=[]))]))
        write_json(self.root / 'corpus-manifest.json', self.manifest)
        write_json(self.root / 'records/S34.json', self.records)
        self.save_queues()

    def row(self, record):
        return dict(record_id=record['record_id'], source='S34', pages=[2], record=record,
                    review=dict(status='pending', decision=None, evidence_pages=[]))

    def save_queues(self):
        write_json(self.root / 'review-queues.json', self.queues)

    def test_semantic_batches_bind_queue_ids_and_exact_full_frozen_pages(self):
        before = (self.root / 'review-queues.json').read_bytes()
        index = s2_readback.export_packets(self.root, 'semantic', 2)
        self.assertEqual(index['row_count'], 3)
        self.assertEqual(index['queue_sha256'], hashlib.sha256(before).hexdigest())
        self.assertFalse(index['judgments_created'])
        self.assertEqual([p['row_ids'] for p in index['packets']],
                         [['synthetic-r0', 'synthetic-r1'], ['synthetic-r2']])
        self.assertEqual({p.name for p in (self.root / 'readback').iterdir()},
                         {'semantic-001.md', 'semantic-002.md', 'semantic-index.json',
                          'semantic-001-compact.json', 'semantic-002-compact.json',
                          'semantic-001-candidate-overview.json', 'semantic-002-candidate-overview.json'})
        for packet in index['packets']:
            data = (self.root / 'readback' / packet['path']).read_bytes()
            self.assertEqual(packet['sha256'], hashlib.sha256(data).hexdigest())
            compact_data = (self.root / 'readback' / packet['compact_path']).read_bytes()
            self.assertEqual(packet['compact_sha256'], hashlib.sha256(compact_data).hexdigest())
            self.assertIn(self.page_text.encode(), data)
            self.assertIn(b'````text', data)  # A source backtick run cannot close the source fence.
            self.assertEqual(len(packet['pages']), 1)  # Shared pages printed once per batch.
            self.assertEqual(packet['pages'][0]['sha256'], self.manifest['sources'][0]['page_records'][0]['text_sha256'])
        self.assertEqual((self.root / 'review-queues.json').read_bytes(), before)

    def test_unmatched_includes_p1_text_available_candidates_and_missing_ids_without_judgments(self):
        index = s2_readback.export_packets(self.root, 'unmatched')
        text = (self.root / 'readback/unmatched-001.md').read_text()
        self.assertIn('Synthetic P1 item', text)
        self.assertIn('missing-candidate', text)
        self.assertIn('"available": false', text)
        self.assertIn('"record_id": "synthetic-r0"', text)
        self.assertIn('"decision": null', text)
        self.assertNotIn('"decision": "正确"', text)
        self.assertFalse(index['judgments_created'])
        self.assertEqual(index['packets'][0]['rows'][0]['candidate_record_ids'], ['missing-candidate', 'synthetic-r0'])

    def test_reverse_and_evidence_pages_resolve_full_source_id_to_short_id(self):
        other = self.root / 'corpus/S34/p3.txt'
        other.write_text('Synthetic additional evidence page')
        self.manifest['sources'][0]['page_records'].append(dict(page=3, corpus_path=str(other),
            text_sha256=hashlib.sha256(other.read_bytes()).hexdigest()))
        write_json(self.root / 'corpus-manifest.json', self.manifest)
        self.queues['queues']['reverse'][0]['source'] = 'synthetic-report'
        self.queues['queues']['reverse'][0]['review']['evidence_pages'] = [3]
        self.save_queues()
        index = s2_readback.export_packets(self.root, 'reverse')
        self.assertEqual([(p['source'], p['page']) for p in index['packets'][0]['pages']], [('S34', 2), ('S34', 3)])

    def test_shared_candidates_print_once_with_compact_references_and_complete_bindings(self):
        second = deepcopy(self.queues['queues']['unmatched'][0])
        second['item_id'] = 'S34.c1.I002'
        second['text'] = 'Another explicit synthetic P1 item [S34 p2]'
        self.queues['queues']['unmatched'].append(second)
        self.save_queues()
        index = s2_readback.export_packets(self.root, 'unmatched', snapshot='compact-test')
        packet = index['packets'][0]
        directory = self.root / 'readback/compact-test'
        text = (directory / packet['path']).read_text()
        self.assertEqual(text.count('"record_id": "synthetic-r0"'), 1)
        self.assertEqual(text.count('### S34 p2'), 1)
        compact_bytes = (directory / packet['compact_path']).read_bytes()
        compact = json.loads(compact_bytes)
        self.assertEqual(packet['compact_sha256'], hashlib.sha256(compact_bytes).hexdigest())
        self.assertEqual(compact['queue_sha256'], index['queue_sha256'])
        self.assertEqual([row['row_id'] for row in compact['rows']], ['S34.c1.I001', 'S34.c1.I002'])
        self.assertEqual(compact['rows'][1]['p1_text'], second['text'])
        for row in compact['rows']:
            self.assertEqual(row['candidate_record_ids'], ['missing-candidate', 'synthetic-r0'])
            self.assertEqual(row['pages'][0]['sha256'], packet['pages'][0]['sha256'])
            self.assertNotIn('review', row)
        bound = {row['record_id']: row for row in packet['candidate_records']}
        self.assertEqual(len(bound), 2)
        self.assertEqual(bound['synthetic-r0']['sha256'], hashlib.sha256(s2_readback._json_bytes(self.records[0])).hexdigest())
        self.assertIsNone(bound['missing-candidate']['sha256'])
        self.assertFalse(bound['missing-candidate']['available'])

    def test_all_page_bindings_are_validated_before_any_packet_is_written(self):
        self.queues['queues']['semantic'][1]['pages'] = [99]
        self.save_queues()
        with self.assertRaisesRegex(ValueError, 'Missing frozen page binding'):
            s2_readback.export_packets(self.root, 'semantic', 1)
        self.assertFalse((self.root / 'readback').exists())

    def test_changed_page_hash_and_missing_file_are_rejected(self):
        page = Path(self.manifest['sources'][0]['page_records'][0]['corpus_path'])
        page.write_text('changed synthetic text')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            s2_readback.export_packets(self.root, 'semantic')
        page.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing frozen page file'):
            s2_readback.export_packets(self.root, 'semantic')
        self.assertFalse((self.root / 'readback').exists())

    def test_malformed_page_metadata_and_outside_run_paths_are_rejected(self):
        original = deepcopy(self.manifest)
        for change in ('bad_hash', 'bool_page', 'outside_path'):
            self.manifest = deepcopy(original)
            binding = self.manifest['sources'][0]['page_records'][0]
            if change == 'bad_hash':
                binding['text_sha256'] = None
            elif change == 'bool_page':
                binding['page'] = True
            else:
                binding['corpus_path'] = str(Path(self.temp.name) / 'outside.txt')
            write_json(self.root / 'corpus-manifest.json', self.manifest)
            with self.subTest(change=change), self.assertRaises(ValueError):
                s2_readback.export_packets(self.root, 'semantic')
        self.assertFalse((self.root / 'readback').exists())

    def test_record_source_or_excerpt_page_mismatch_is_rejected(self):
        self.queues['queues']['semantic'][0]['record']['excerpts'][0]['page'] = 3
        self.save_queues()
        with self.assertRaisesRegex(ValueError, 'outside the record locator'):
            s2_readback.export_packets(self.root, 'semantic')
        self.queues['queues']['semantic'][0]['record']['excerpts'][0]['page'] = 2
        self.queues['queues']['semantic'][0]['source'] = 'unknown-source'
        self.save_queues()
        with self.assertRaisesRegex(ValueError, 'absent from frozen corpus'):
            s2_readback.export_packets(self.root, 'semantic')

    def test_reexport_is_identical_and_changed_queue_cannot_overwrite_packets(self):
        index = s2_readback.export_packets(self.root, 'semantic', 2)
        before = {p.name: p.read_bytes() for p in (self.root / 'readback').iterdir()}
        self.assertEqual(s2_readback.export_packets(self.root, 'semantic', 2), index)
        self.queues['queues']['semantic'].pop()
        self.save_queues()
        with self.assertRaisesRegex(ValueError, 'Existing readback binding differs'):
            s2_readback.export_packets(self.root, 'semantic', 2)
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / 'readback').iterdir()}, before)

    def test_invalid_batch_size_and_duplicate_rows_are_rejected(self):
        for size in (0, -1, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                s2_readback.export_packets(self.root, 'semantic', size)
        self.queues['queues']['semantic'].append(deepcopy(self.queues['queues']['semantic'][0]))
        self.save_queues()
        with self.assertRaisesRegex(ValueError, 'duplicate review row'):
            s2_readback.export_packets(self.root, 'semantic')

    def test_snapshot_isolated_from_default_packets_and_allows_later_queue_population(self):
        early = s2_readback.export_packets(self.root, 'semantic', 2, snapshot='in-progress_01')
        early_dir = self.root / 'readback/in-progress_01'
        before = {p.name: p.read_bytes() for p in early_dir.iterdir()}
        self.assertEqual(early['snapshot'], 'in-progress_01')
        self.assertFalse((self.root / 'readback/semantic-index.json').exists())
        self.queues['queues']['semantic'].pop()
        self.save_queues()
        final = s2_readback.export_packets(self.root, 'semantic', 2)
        self.assertEqual(final['row_count'], 2)
        self.assertNotIn('snapshot', final)  # Default index format stays compatible.
        self.assertEqual({p.name: p.read_bytes() for p in early_dir.iterdir()}, before)
        with self.assertRaisesRegex(ValueError, 'Existing readback binding differs'):
            s2_readback.export_packets(self.root, 'semantic', 2, snapshot='in-progress_01')

    def test_snapshot_rejects_traversal_absolute_names_and_symlink_escapes(self):
        for name in ('', '.', '..', '../earlier', '/absolute', 'a/b', 'a\\b', 'a b', True):
            with self.subTest(snapshot=name), self.assertRaisesRegex(ValueError, 'simple safe directory name'):
                s2_readback.export_packets(self.root, 'semantic', snapshot=name)
        destination = self.root / 'readback'
        destination.mkdir()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (destination / 'escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'must stay within run/readback'):
            s2_readback.export_packets(self.root, 'semantic', snapshot='escape')
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_reports_the_actual_snapshot_index_path(self):
        args = ['s2_readback.py', str(self.root), '--queue', 'reverse', '--snapshot', 'progress-02', '--batch-size', '1']
        stdout = io.StringIO()
        with mock.patch('sys.argv', args), redirect_stdout(stdout):
            s2_readback.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result['index'], str(self.root.resolve() / 'readback/progress-02/reverse-index.json'))
        self.assertTrue(Path(result['index']).is_file())


class DraftBindingTests(unittest.TestCase):
    row = ReadbackTests.row
    save_queues = ReadbackTests.save_queues

    def setUp(self):
        ReadbackTests.setUp(self)
        self.source_sha = hashlib.sha256((self.root / 'records/S34.json').read_bytes()).hexdigest()

    def draft(self, name='draft.json', queue='unmatched', **overrides):
        row_id = 'S34.c1.I001' if queue == 'unmatched' else 'synthetic-r0'
        entry = dict(queue=queue, id=row_id, decision='S2漏抽' if queue == 'unmatched' else '正确' if queue == 'semantic' else '新增的正确内容',
                     reason='Explicit synthetic reviewer finding', evidence_pages=[2])
        value = dict(reviewer='Synthetic reviewer', review_id='synthetic-original-review',
                     source_records_sha256=dict(S34=self.source_sha), decisions=[entry])
        if queue != 'unmatched':
            value['queues_sha256'] = s2_compare.json_hash(self.queues)
        value.update(overrides)
        path = self.root / 'reviews' / name
        write_json(path, value)
        return path, value

    def test_unmatched_source_hash_can_rebind_after_unrelated_global_queue_change(self):
        path, draft = self.draft(queues_sha256='old-whole-queue-binding')
        self.queues['seed'] = 999
        self.save_queues()
        source_before = (self.root / 'records/S34.json').read_bytes()
        queue_before = (self.root / 'review-queues.json').read_bytes()
        output = self.root / 'reviews/bound.json'
        result = s2_readback.bind_review_drafts(self.root, [path], output, 'final-review')
        self.assertEqual(result['decisions'], draft['decisions'])
        self.assertEqual(result['queues_sha256'], s2_compare.json_hash(self.queues))
        self.assertEqual(result['review_id'], 'final-review')
        self.assertEqual(result['drafts'][0]['review_id'], 'synthetic-original-review')
        self.assertEqual(result['drafts'][0]['path'], 'reviews/draft.json')
        self.assertEqual(result['drafts'][0]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(result['decisions_created'])
        self.assertEqual((self.root / 'records/S34.json').read_bytes(), source_before)
        self.assertEqual((self.root / 'review-queues.json').read_bytes(), queue_before)

    def test_semantic_and_reverse_require_exact_current_canonical_queue_hash(self):
        for queue in ('semantic', 'reverse'):
            path, _ = self.draft(queue + '.json', queue=queue)
            output = self.root / 'reviews' / ('bound-' + queue + '.json')
            self.assertEqual(len(s2_readback.bind_review_drafts(self.root, [path], output, 'final')['decisions']), 1)
            path, _ = self.draft('stale-' + queue + '.json', queue=queue, queues_sha256='stale')
            with self.subTest(queue=queue), self.assertRaisesRegex(ValueError, 'exact current queues_sha256'):
                s2_readback.bind_review_drafts(self.root, [path], 'reviews/rejected.json', 'final')
        self.assertFalse((self.root / 'reviews/rejected.json').exists())

    def test_source_hash_mismatch_missing_binding_or_missing_record_file_is_rejected(self):
        path, _ = self.draft(source_records_sha256={})
        with self.assertRaisesRegex(ValueError, 'lacks matching source records binding'):
            s2_readback.bind_review_drafts(self.root, [path], 'reviews/bound.json', 'final')
        path, _ = self.draft(source_records_sha256=dict(S34='0' * 64))
        with self.assertRaisesRegex(ValueError, 'source records hash mismatch'):
            s2_readback.bind_review_drafts(self.root, [path], 'reviews/bound.json', 'final')
        path, _ = self.draft()
        (self.root / 'records/S34.json').unlink()
        with self.assertRaisesRegex(ValueError, 'source records are missing'):
            s2_readback.bind_review_drafts(self.root, [path], 'reviews/bound.json', 'final')
        self.assertFalse((self.root / 'reviews/bound.json').exists())

    def test_combines_only_authored_entries_and_rejects_duplicate_or_unknown_ids(self):
        first, a = self.draft('unmatched.json')
        second, b = self.draft('semantic.json', queue='semantic')
        result = s2_readback.bind_review_drafts(self.root, [first, second], 'reviews/combined.json', 'combined')
        self.assertEqual(result['decisions'], a['decisions'] + b['decisions'])
        self.assertEqual(len(result['drafts']), 2)
        with self.assertRaisesRegex(ValueError, 'duplicate authored review entry'):
            s2_readback.bind_review_drafts(self.root, [first, first], 'reviews/duplicate.json', 'combined')
        bad = deepcopy(a['decisions'])
        bad[0]['id'] = 'unknown-item'
        unknown, _ = self.draft('unknown.json', decisions=bad)
        with self.assertRaisesRegex(ValueError, 'Unknown or duplicate'):
            s2_readback.bind_review_drafts(self.root, [unknown], 'reviews/unknown-bound.json', 'combined')

    def test_apply_reviews_validates_authored_content_before_any_output(self):
        for label, mutation, error in (
            ('decision', dict(decision='auto-inferred'), 'illegal review decision'),
            ('reason', dict(reason=''), 'review reason'),
            ('evidence', dict(evidence_pages=[]), 'review reason'),
            ('error_type', dict(decision='错误'), 'requires error_types'),
        ):
            path, draft = self.draft(label + '.json', queue='semantic')
            draft['decisions'][0].update(mutation)
            write_json(path, draft)
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, error):
                s2_readback.bind_review_drafts(self.root, [path], 'reviews/invalid.json', 'final')
        self.assertFalse((self.root / 'reviews/invalid.json').exists())

    def test_draft_output_and_source_bindings_cannot_escape_run_or_replace_records(self):
        path, draft = self.draft()
        outside = Path(self.temp.name) / 'outside.json'
        write_json(outside, draft)
        with self.assertRaisesRegex(ValueError, 'must stay within the run'):
            s2_readback.bind_review_drafts(self.root, [outside], 'reviews/bound.json', 'final')
        with self.assertRaisesRegex(ValueError, 'must stay within the run'):
            s2_readback.bind_review_drafts(self.root, [path], outside, 'final')
        source_before = (self.root / 'records/S34.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'not a record or source'):
            s2_readback.bind_review_drafts(self.root, [path], 'records/S34.json', 'final')
        self.assertEqual((self.root / 'records/S34.json').read_bytes(), source_before)
        (self.root / 'records/S34.json').unlink()
        (self.root / 'records/S34.json').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'must stay within run/records'):
            s2_readback.bind_review_drafts(self.root, [path], 'reviews/bound.json', 'final')

    def test_identical_output_reusable_and_different_review_cannot_overwrite_it(self):
        path, _ = self.draft()
        output = self.root / 'reviews/bound.json'
        result = s2_readback.bind_review_drafts(self.root, [path], output, 'review-one')
        before = output.read_bytes()
        self.assertEqual(s2_readback.bind_review_drafts(self.root, [path], output, 'review-one'), result)
        with self.assertRaisesRegex(ValueError, 'Existing bound review differs'):
            s2_readback.bind_review_drafts(self.root, [path], output, 'review-two')
        self.assertEqual(output.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
