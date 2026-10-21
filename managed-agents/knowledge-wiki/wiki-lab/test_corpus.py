"""Failure-oriented checks for source identity and immutable page preparation."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

import corpus


class RecordChainTests(unittest.TestCase):
    def setUp(self):
        self.root = {'source_id': 'source', 'path': 'original.pdf', 'sha256': 'a' * 64,
                     'published_date': '2026-08-28', 'title': 'old'}
        self.updated = {**self.root, 'title': 'corrected', 'manifest_record_version': 'm2',
                        'supersedes_manifest_ref': {
                            'path': 'manifests/sources.jsonl', 'source_id': 'source',
                            'manifest_record_version': 'm1',
                            'normalized_sha256': corpus.normalized_sha256(self.root)},
                        'metadata_revision': {'reason': 'Correct title'}}

    def rows(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sources.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in records) + '\n')
            return corpus.read_manifest(path)['source']

    def test_predecessor_chain_wins_even_when_old_row_is_last(self):
        selected = corpus.resolve_records(self.rows([self.updated, self.root]))
        self.assertEqual(selected['record']['title'], 'corrected')
        self.assertEqual([r['version'] for r in selected['chain']], ['m1', 'm2'])

    def test_wrong_predecessor_hash_rejected(self):
        changed = copy.deepcopy(self.updated)
        changed['supersedes_manifest_ref']['normalized_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Predecessor hash mismatch'):
            corpus.resolve_records(self.rows([self.root, changed]))

    def test_fork_and_unlinked_duplicate_rejected(self):
        fork = copy.deepcopy(self.updated)
        fork['manifest_record_version'] = 'm3'
        with self.assertRaisesRegex(ValueError, 'fork'):
            corpus.resolve_records(self.rows([self.root, self.updated, fork]))
        with self.assertRaisesRegex(ValueError, 'Duplicate manifest version'):
            corpus.resolve_records(self.rows([self.root, {**self.root, 'title': 'unlinked'}]))

    def test_metadata_revision_cannot_replace_pdf(self):
        changed = {**self.updated, 'sha256': 'b' * 64}
        with self.assertRaisesRegex(ValueError, 'changed source identity'):
            corpus.resolve_records(self.rows([self.root, changed]))


class PagePreparationTests(unittest.TestCase):
    def test_existing_registry_bytes_are_not_rewritten_or_renumbered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registry.json'
            raw = '{"schema":"wiki-lab-registry/1", "cutoff":"2026-10-15", "mapping":{"a":"S17"}}\n'
            path.write_text(raw)
            corpus.freeze_registry(path, [{'source_id': 'a', 'published_date': '2026-08-28'}])
            self.assertEqual(path.read_text(), raw)
            result = corpus.freeze_registry(path, [
                {'source_id': 'a', 'published_date': '2026-08-28'},
                {'source_id': 'b', 'published_date': '2000-01-01'}])
            self.assertEqual(result['mapping'], {'a': 'S17', 'b': 'S18'})

    def test_wrong_original_hash_fails_before_copying_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'original.pdf').write_bytes(b'original')
            row = {'record': {'source_id': 'bad', 'path': 'original.pdf', 'sha256': '0' * 64}}
            with self.assertRaisesRegex(ValueError, 'Original hash mismatch'):
                corpus._prepare_source(row, 'S01', root, root / 'output', True)
            self.assertFalse((root / 'output').exists())

    def test_full_pages_survive_chunk_boundary_including_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pages = []
            original_pages = [b'header\r\nfootnote\r\n', b'NEXT PAGE\n']
            for n, raw in enumerate(original_pages, 1):
                path = root / f'p{n:04d}.txt'
                path.write_bytes(raw)
                pages.append({'page': n, 'chars': len(raw.decode()), 'corpus_path': str(path)})
            source = {'id': 'S01', 'source_id': 'single', 'date': '2026-08-28',
                      'tags': ['公司:yofc'], 'page_records': pages}
            chunks = corpus._chunks([source], root, max_chars=5, merge_monthly=False)
            self.assertEqual([c['sources'][0]['pages'] for c in chunks], [[1], [2]])
            for chunk, raw in zip(chunks, original_pages):
                self.assertTrue(Path(chunk['path']).read_bytes().endswith(raw))
                self.assertTrue(chunk['single_page_over_limit'])

    def test_source_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'escapes'):
                corpus.source_path('../other.pdf', Path(directory))


if __name__ == '__main__':
    unittest.main()
